"""Compare C++ and persistent Python render sessions without launching Xcode."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=Path(os.environ.get(
        'POLLIA_BUILD_DIR', str(Path.home()/'Library/Developer/Xcode/DerivedData/pollia-runtime'))))
    parser.add_argument('--cases', default='64:4,256:16,512:32', help='size:spp comma-separated')
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--repeats', type=int, default=15)
    parser.add_argument('--warmup', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if min(args.trials,args.repeats,args.warmup) < 1:
        parser.error('Trials, repeats and warmup must be positive')
    root = Path(__file__).resolve().parents[2]
    build = args.build_dir.resolve()
    cases = [tuple(map(int,c.split(':'))) for c in args.cases.split(',')]
    if any(len(c)!=2 or min(c)<1 for c in cases):
        parser.error('Expected positive size:spp pairs')
    output = args.output or root/'benchmarks/results'/datetime.date.today().isoformat()/'orchestration.json'
    output = output.resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    assets = output.parent/(output.stem+'-arrays')
    assets.mkdir(exist_ok=True)
    import numpy as np
    env = dict(os.environ, PYTHONPATH=str(build/'python'))
    python = root/'.venv/bin/python'
    native = build/'pollia-cpp-benchmark'
    if not native.exists():
        raise RuntimeError('Build pollia-cpp-benchmark first (see benchmarks/orchestration/README.txt)')
    scene = Path(__file__).with_name('scene.xml')
    report = dict(date=datetime.datetime.now().astimezone().isoformat(),
                  machine=platform.platform(), processor=platform.machine(),
                  source_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
                  build_dir=str(build), configuration='RelWithDebInfo', variant='metal_rgb_polarized',
                  scene=str(scene), scene_sha256=hashlib.sha256(scene.read_bytes()).hexdigest(),
                  trials=args.trials, repeats=args.repeats, warmup=args.warmup, threads=12,
                  cache_policy='Preserve disk cache; separate first render and warmed session samples.',
                  notes=['No UI or Swift bridge; process startup is paid once per session.',
                         'Host tracing may submit GPU work; eval_sync includes JIT compilation and waiting.',
                         'GPU history is a separate diagnostic pass; summed durations are not wall time.',
                         'Image readback/file IO excluded from render samples.',
                         'Worker IPC, shared-memory/image transport, scene edits and differentiation unmeasured.',
                         'Simple fixed geometry; changing seeds; stock polarized path integrator.'], cases=[])
    modes = ['cpp','python_direct','python_public']
    for size,spp in cases:
        case = dict(size=size,spp=spp,runs=[],summary={})
        reference = None
        for trial in range(args.trials):
            order = modes[trial%3:]+modes[:trial%3]
            for mode in order:
                array_path = assets/f'{size}-{spp}-{trial}-{mode}.f32'
                settings = [str(scene),str(size),str(spp),str(args.repeats),str(args.warmup),str(array_path)]
                if mode == 'cpp':
                    command = [str(native),*settings]
                else:
                    command = [str(python),str(Path(__file__).with_name('python.py')),*settings,
                               '--api',mode.removeprefix('python_')]
                start = time.perf_counter()
                result = subprocess.run(command,cwd=build,env=env,text=True,capture_output=True,timeout=240)
                wall = (time.perf_counter()-start)*1000
                if result.returncode:
                    raise RuntimeError(f'{mode} failed ({result.returncode}): {result.stderr}\n{result.stdout[-2000:]}')
                run = json.loads(result.stdout)
                run.update(mode=mode,trial=trial,process_wall_ms=wall,stderr=result.stderr)
                array = np.fromfile(array_path,dtype='<f4').reshape(size,size,15)
                if not np.isfinite(array).all() or array[...,0].max()<=0:
                    raise RuntimeError('Nonfinite or empty output')
                if reference is None:
                    reference = array.copy()
                delta = np.abs(array-reference)
                run['output_max_abs_difference'] = float(delta.max())
                run['output_rms_difference'] = float(np.sqrt(np.mean(delta.astype(np.float64)**2)))
                if not np.allclose(array,reference,rtol=2e-5,atol=2e-6):
                    raise RuntimeError(f'{mode} output differs from C++ reference: {delta.max()}')
                case['runs'].append(run)
                median = statistics.median(s['total_ms'] for s in run['warm'])
                print(f'{size}x{size} {spp}spp trial {trial+1} {mode}: warm median {median:.3f} ms',flush=True)
        for mode in modes:
            runs = [r for r in case['runs'] if r['mode']==mode]
            medians = [statistics.median(s['total_ms'] for s in r['warm']) for r in runs]
            case['summary'][mode] = dict(
                warm_median_ms=statistics.median(medians), trial_medians_ms=medians,
                initialize_median_ms=statistics.median(r['initialize_ms'] for r in runs),
                scene_load_median_ms=statistics.median(r['scene_load_ms'] for r in runs),
                first_render_median_ms=statistics.median(r['first']['total_ms'] for r in runs),
                trace_median_ms=statistics.median(s['trace_ms'] for r in runs for s in r['warm']),
                eval_sync_median_ms=statistics.median(s['eval_sync_ms'] for r in runs for s in r['warm']),
                history_device_sum_median_ms=statistics.median(r['history_device_sum_ms'] for r in runs),
                readback_median_ms=statistics.median(r['readback_ms'] for r in runs))
        baseline = case['summary']['cpp']['warm_median_ms']
        for summary in case['summary'].values():
            summary.update(ratio_to_cpp=summary['warm_median_ms']/baseline,
                           difference_from_cpp_ms=summary['warm_median_ms']-baseline)
        report['cases'].append(case)
        output.write_text(json.dumps(report,indent=2)+'\n')
    print(f'Results: {output}')


if __name__ == '__main__':
    main()
