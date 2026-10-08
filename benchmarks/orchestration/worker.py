"""Metal render worker with a small command protocol shared with worker.cpp."""
import argparse
import json
import mmap
import sys
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('scene')
    p.add_argument('size', type=int)
    p.add_argument('spp', type=int)
    p.add_argument('channels', type=int, choices=(3,15))
    p.add_argument('shared_file')
    p.add_argument('--readback', choices=('owned','view'), default='view')
    p.add_argument('--preview-pack', choices=('cpu','gpu'), default='cpu')
    args = p.parse_args()
    import drjit as dr
    import mitsuba as mi
    import numpy as np
    mi.set_variant('metal_rgb_polarized')
    mi.set_log_level(mi.LogLevel.Warn)
    dr.set_log_level(dr.LogLevel.Error)
    dr.set_thread_count(12)
    if not dr.has_backend(dr.JitBackend.Metal):
        raise RuntimeError('Metal unavailable; no CPU fallback')
    scene = mi.load_file(args.scene, size=str(args.size))
    nbytes = args.size**2 * args.channels * 4
    with open(args.shared_file, 'r+b') as shared_file:
        shared = mmap.mmap(shared_file.fileno(), nbytes)
        out = sys.stdout.buffer
        readback_details = {}

        def reply(value):
            out.write(json.dumps(value,separators=(',',':')).encode()+b'\n')
            out.flush()

        def render(seed):
            start = time.perf_counter()
            image = mi.render(scene, seed=seed, spp=args.spp, postprocess=False)
            dr.eval(image)
            dr.sync_thread()
            return image, (time.perf_counter()-start)*1000

        def readback(image):
            pack_ms = 0
            if args.channels == 3 and args.preview_pack == 'gpu':
                start = time.perf_counter()
                indices = dr.arange(mi.UInt32, args.size**2*3)
                rgb = dr.gather(mi.Float, image.array, (indices//3)*15 + indices%3)
                image = mi.TensorXf(rgb, shape=(args.size,args.size,3))
                dr.eval(image)
                dr.sync_thread()
                pack_ms = (time.perf_counter()-start)*1000
            start = time.perf_counter()
            array = np.array(image) if args.readback == 'owned' else np.asarray(image)
            readback_ms = (time.perf_counter()-start)*1000
            start = time.perf_counter()
            # Keep a contiguous buffer; bytes serialization is in pipe transfer timing.
            packed = np.ascontiguousarray(array[...,:args.channels], dtype='<f4')
            pack_ms += (time.perf_counter()-start)*1000
            readback_details.update(array_owns_data=bool(array.flags.owndata),
                                    packed_owns_data=bool(packed.flags.owndata))
            return packed, readback_ms, pack_ms

        image, first_ms = render(1)
        frame, _, _ = readback(image)
        # Check Metal execution outside timed requests.
        with dr.kernel_history() as history:
            image, _ = render(2)
        entries = list(history)
        if not entries or any(e.backend != dr.JitBackend.Metal for e in entries):
            raise RuntimeError('Expected exclusively Metal history entries')
        reply(dict(ready=True, first_render_ms=first_ms, bytes=nbytes,
                   variant=mi.variant(), history_entries=len(entries), **readback_details))
        for line in sys.stdin:
            command = line.split()
            if command == ['quit']:
                break
            if command == ['ping']:
                reply(dict(ok=True))
                continue
            if command[0] == 'render':
                seed, transport = int(command[1]), command[2]
                image, render_ms = render(seed)
                readback_ms = pack_ms = 0
                if transport != 'none':
                    frame, readback_ms, pack_ms = readback(image)
            elif command[0] == 'send':
                transport = command[1]
                render_ms = readback_ms = pack_ms = 0
            else:
                raise RuntimeError('Unknown command')
            if transport not in ('none','local','pipe','shared'):
                raise RuntimeError('Unknown transport')
            reply(dict(render_ms=render_ms, readback_ms=readback_ms, pack_ms=pack_ms,
                       bytes=nbytes if transport in ('pipe','shared') else 0))
            start = time.perf_counter()
            if transport == 'pipe':
                out.write(memoryview(frame).cast('B'))
                out.flush()
            elif transport == 'shared':
                shared[:nbytes] = memoryview(frame).cast('B')
            transfer_ms = (time.perf_counter()-start)*1000
            reply(dict(transfer_ms=transfer_ms))
        shared.close()


if __name__ == '__main__':
    main()
