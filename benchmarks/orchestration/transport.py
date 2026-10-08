"""Matched persistent C++/Python IPC and image-transfer measurements, Metal only."""
import argparse
import datetime
import hashlib
import json
import mmap
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import threading
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build-dir', type=Path, default=Path(os.environ.get('POLLIA_BUILD_DIR',
                   str(Path.home()/'Library/Developer/Xcode/DerivedData/pollia-runtime'))))
    p.add_argument('--cases', default='256:16,512:32,1024:4')
    p.add_argument('--channels', default='3,15')
    p.add_argument('--trials', type=int, default=3)
    p.add_argument('--repeats', type=int, default=12)
    p.add_argument('--warmup', type=int, default=3)
    p.add_argument('--pings', type=int, default=100)
    p.add_argument('--languages', default='cpp,python_view,python_owned')
    p.add_argument('--preview-pack', choices=('cpu','gpu'), default='cpu')
    p.add_argument('--output', type=Path, default=Path('benchmarks/results/2026-10-08/transport-optimized.json'))
    args = p.parse_args()
    if min(args.trials,args.repeats,args.warmup,args.pings)<1:
        p.error('Counts must be positive')
    cases = [tuple(map(int,v.split(':'))) for v in args.cases.split(',')]
    channels = [int(v) for v in args.channels.split(',')]
    languages = args.languages.split(',')
    if not languages or any(v not in ('cpp','python_view','python_owned') for v in languages):
        p.error('Invalid worker language/readback mode')
    if any(len(v)!=2 or min(v)<1 for v in cases) or any(c not in (3,15) for c in channels):
        p.error('Invalid cases or channel count')
    import numpy as np
    root = Path(__file__).resolve().parents[2]
    build = args.build_dir.resolve()
    scene = Path(__file__).with_name('scene.xml')
    output = args.output.resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    assets = output.parent/(output.stem+'-arrays')
    assets.mkdir(exist_ok=True)
    env = dict(os.environ,PYTHONPATH=str(build/'python'))
    native = build/'pollia-cpp-worker'
    if not native.is_file():
        raise RuntimeError('Build pollia-cpp-worker first')
    report = dict(date=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-7))).isoformat(),
                  variant='metal_rgb_polarized',gpu='Apple M4 Max',configuration='RelWithDebInfo',
                  trials=args.trials,repeats=args.repeats,warmup=args.warmup,pings=args.pings,
                  preview_pack=args.preview_pack,
                  threads=12,build_dir=str(build),cases=[],
                  scene_sha256=hashlib.sha256(scene.read_bytes()).hexdigest(),
                  notes=['Workers retain one Metal scene; Python uses mi.render.',
                         'python_view uses np.asarray; python_owned uses np.array.',
                         'C++ keeps a host readback view; full 15-channel output avoids packing copies.',
                         'preview_pack chooses CPU packing or a Metal gather in both workers.',
                         'Parent wall time ends after the completion footer; binary pipe reads included.',
                         'Shared buffer uses MAP_SHARED/file-backed mmap; no per-frame disk writes or msync.',
                         'Shared output is a borrowed view valid until the next request, not an owned copy.',
                         'Validation and optional parent snapshot copies are outside parent RTT.',
                         'Native inner render+readback excludes IPC and packing: proxy for C++ in-process host access.',
                         'Swift FFI, SwiftUI display, GPU uploads, embedded Python and direct GPU sharing unmeasured.',
                         'RGB preview is first three film channels as float32; full Stokes output has 15.',
                         'Retained disk caches; order rotates; no CPU rendering or Xcode app launch.'])
    modes=['none','local','pipe','shared']
    for size,spp in cases:
        for nc in channels:
            case=dict(size=size,spp=spp,channels=nc,bytes=size*size*nc*4,runs=[],summary={})
            reference={}
            for trial in range(args.trials):
                for language in (languages[trial%len(languages):]+languages[:trial%len(languages)]):
                    stderr_path=assets/f'{size}-{nc}-{trial}-{language}.stderr.txt'
                    with tempfile.TemporaryDirectory(prefix='pollia-transport-',dir='/private/tmp') as temporary:
                        shared_path=Path(temporary)/'frame'
                        with shared_path.open('w+b') as file:
                            file.truncate(case['bytes'])
                            shared=mmap.mmap(file.fileno(),case['bytes'])
                        settings=[str(scene),str(size),str(spp),str(nc),str(shared_path)]
                        cmd=([str(native)] if language=='cpp' else
                             [str(root/'.venv/bin/python'),'-u',str(Path(__file__).with_name('worker.py'))])+settings
                        if language != 'cpp':
                            cmd += ['--readback',language.removeprefix('python_'),
                                    '--preview-pack',args.preview_pack]
                        else:
                            cmd += [args.preview_pack]
                        with stderr_path.open('wb') as log:
                            start=time.perf_counter()
                            process=subprocess.Popen(cmd,cwd=build,env=env,stdin=subprocess.PIPE,
                                                     stdout=subprocess.PIPE,stderr=log)
                            watchdog=threading.Timer(180,process.kill)
                            watchdog.daemon=True
                            watchdog.start()
                            def line():
                                data=process.stdout.readline()
                                if not data:
                                    raise RuntimeError(f'{language} worker stopped: {stderr_path.read_text()}')
                                return json.loads(data)
                            def request(command):
                                start=time.perf_counter()
                                process.stdin.write(command.encode()+b'\n')
                                process.stdin.flush()
                                header=line()
                                if command=='ping':
                                    return dict(parent_ms=(time.perf_counter()-start)*1000),None
                                raw=None
                                if command.endswith('pipe'):
                                    raw=process.stdout.read(header['bytes'])
                                    if len(raw)!=header['bytes']:
                                        raise RuntimeError('Truncated frame')
                                footer=line()
                                sample=dict(header,**footer,parent_ms=(time.perf_counter()-start)*1000)
                                # A zero-copy view of received bytes or shared memory; no presentation.
                                array=None
                                if command.endswith(('pipe','shared')):
                                    array=np.frombuffer(raw if raw is not None else shared,dtype='<f4').reshape(size,size,nc)
                                return sample,array
                            def validate(array,key):
                                if not np.isfinite(array).all() or array[...,0].max()<=0:
                                    raise RuntimeError('Invalid frame')
                                if key not in reference:
                                    reference[key]=array.copy()
                                    reference[key].tofile(assets/f'{size}-{nc}-{key}.f32')
                                delta=float(np.max(np.abs(array-reference[key])))
                                if not np.allclose(array,reference[key],rtol=2e-5,atol=2e-6):
                                    raise RuntimeError(f'{language} frame mismatch {delta}')
                                return delta
                            try:
                                ready=line()
                                ready_ms=(time.perf_counter()-start)*1000
                                if ready['bytes']!=case['bytes'] or ready['variant']!='metal_rgb_polarized':
                                    raise RuntimeError('Unexpected worker configuration')
                                samples=dict(ping=[],send_pipe=[],send_shared=[],**{m:[] for m in modes})
                                for _ in range(10): request('ping')
                                for _ in range(args.pings): samples['ping'].append(request('ping')[0])
                                max_diff=0
                                for i in range(args.warmup):
                                    for mode in ['pipe','shared']:
                                        _,array=request('send '+mode)
                                        del array
                                for i in range(args.repeats):
                                    for mode in (['pipe','shared'] if i%2==0 else ['shared','pipe']):
                                        sample,array=request('send '+mode)
                                        if i in (0,args.repeats-1):
                                            max_diff=max(max_diff,validate(array,'send-seed1'))
                                        samples['send_'+mode].append(sample)
                                        del array
                                for i in range(args.warmup):
                                    for mode in modes:
                                        _,array=request(f'render {50+i} {mode}')
                                        del array
                                snapshot=[]
                                for i in range(args.repeats):
                                    order=modes[i%4:]+modes[:i%4]
                                    for mode in order:
                                        sample,array=request(f'render {100+i} {mode}')
                                        samples[mode].append(sample)
                                        if array is not None and i in (0,args.repeats-1):
                                            max_diff=max(max_diff,validate(array,f'render-seed{100+i}'))
                                        if mode=='shared':
                                            # Optional ownership cost, excluded from the borrowed-view RTT.
                                            copy_start=time.perf_counter()
                                            owned=array.copy()
                                            snapshot.append((time.perf_counter()-copy_start)*1000)
                                            del owned
                                        del array
                                process.stdin.write(b'quit\n'); process.stdin.flush()
                                process.wait(timeout=10)
                                if process.returncode:
                                    raise RuntimeError(stderr_path.read_text())
                                run=dict(language=language,trial=trial,ready_ms=ready_ms,ready=ready,
                                         samples=samples,parent_owned_copy_ms=snapshot,
                                         output_max_abs_difference=max_diff)
                                case['runs'].append(run)
                                print(f'{size}x{size} {nc}ch trial {trial+1} {language}: '
                                      f"local {statistics.median(s['parent_ms'] for s in samples['local']):.3f}, "
                                      f"pipe {statistics.median(s['parent_ms'] for s in samples['pipe']):.3f}, "
                                      f"shared {statistics.median(s['parent_ms'] for s in samples['shared']):.3f} ms",flush=True)
                            finally:
                                watchdog.cancel()
                                if process.poll() is None:
                                    process.kill(); process.wait()
                                process.stdin.close(); process.stdout.close()
                                shared.close()
            for language in languages:
                runs=[r for r in case['runs'] if r['language']==language]
                summary=dict(ready_median_ms=statistics.median(r['ready_ms'] for r in runs),
                             parent_owned_copy_median_ms=statistics.median(t for r in runs for t in r['parent_owned_copy_ms']))
                for mode in ['ping','send_pipe','send_shared',*modes]:
                    trial_medians=[statistics.median(s['parent_ms'] for s in r['samples'][mode]) for r in runs]
                    summary[mode]=dict(parent_median_ms=statistics.median(trial_medians),trial_medians_ms=trial_medians)
                    if mode!='ping':
                        for field in ['render_ms','readback_ms','pack_ms','transfer_ms']:
                            summary[mode][field.replace('_ms','_median_ms')]=statistics.median(
                                s[field] for r in runs for s in r['samples'][mode])
                        inner=[s['render_ms']+s['readback_ms'] for r in runs for s in r['samples'][mode]]
                        summary[mode]['render_plus_readback_median_ms']=statistics.median(inner)
                        summary[mode]['prepared_frame_median_ms']=statistics.median(
                            s['render_ms']+s['readback_ms']+s['pack_ms']
                            for r in runs for s in r['samples'][mode])
                case['summary'][language]=summary
            report['cases'].append(case)
            report['harness_sha256']={f.name:hashlib.sha256(f.read_bytes()).hexdigest()
                                      for f in Path(__file__).parent.iterdir() if f.is_file()}
            output.write_text(json.dumps(report,indent=2)+'\n')
            reference.clear()
    print(f'Results: {output}',flush=True)


if __name__=='__main__':
    main()
