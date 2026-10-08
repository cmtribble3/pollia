"""Persistent-session Python harness; invoked by run.py in a fresh subprocess."""
import argparse
import json
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene")
    parser.add_argument("size", type=int)
    parser.add_argument("spp", type=int)
    parser.add_argument("repeats", type=int)
    parser.add_argument("warmup", type=int)
    parser.add_argument("output")
    parser.add_argument("--api", choices=("direct", "public"), default="public")
    args = parser.parse_args()
    if min(args.size, args.spp, args.repeats, args.warmup) < 1:
        parser.error("Benchmark settings must be positive")
    start = time.perf_counter()
    import drjit as dr
    import mitsuba as mi
    import numpy as np
    mi.set_variant("metal_rgb_polarized")
    mi.set_log_level(mi.LogLevel.Warn)
    dr.set_log_level(dr.LogLevel.Error)
    if not dr.has_backend(dr.JitBackend.Metal):
        raise RuntimeError("Metal unavailable; no CPU fallback")
    dr.set_thread_count(12)
    initialize_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    scene = mi.load_file(args.scene, size=str(args.size))
    sensor = scene.sensors()[0]
    integrator = scene.integrator()
    film = sensor.film()
    dr.sync_thread()
    scene_load_ms = (time.perf_counter() - start) * 1000

    def render(seed):
        start = time.perf_counter()
        if args.api == "public":
            image = mi.render(scene, sensor=sensor, seed=seed, spp=args.spp, postprocess=False)
        else:
            integrator.render(scene, sensor, seed, args.spp, False, False, False)
            image = film.develop(False)
        trace_ms = (time.perf_counter() - start) * 1000
        evaluation = time.perf_counter()
        dr.eval(image)
        dr.sync_thread()
        end = time.perf_counter()
        return image, {"trace_ms": trace_ms, "eval_sync_ms": (end-evaluation)*1000,
                       "total_ms": (end-start)*1000}

    image, first = render(1)
    del image
    for i in range(args.warmup):
        image, _ = render(50+i)
        del image
    warm = []
    for i in range(args.repeats):
        image, sample = render(100+i)
        warm.append(sample)
        del image
    with dr.kernel_history() as history:
        image, _ = render(1)
    entries = list(history)
    if not entries or any(e.backend != dr.JitBackend.Metal for e in entries):
        raise RuntimeError("Expected exclusively Metal history entries")
    start = time.perf_counter()
    array = np.array(image)
    readback_ms = (time.perf_counter()-start)*1000
    if array.shape != (args.size,args.size,15) or not np.isfinite(array).all():
        raise RuntimeError("Invalid Stokes image")
    array.astype('<f4', copy=False).tofile(args.output)
    print(json.dumps(dict(language="python", api=args.api, variant=mi.variant(),
                         initialize_ms=initialize_ms, scene_load_ms=scene_load_ms,
                         first=first, warm=warm, readback_ms=readback_ms,
                         history_device_sum_ms=sum(e.execution_time for e in entries),
                         history_entries=len(entries), shape=list(array.shape))))


if __name__ == '__main__':
    main()
