"""Four-way first-hit identity experiment on Pollia's shared-path transport.

Measures Mueller and PIS with identity_shortcut false/true in one process,
rotating order within each block. This is an archived experiment: apply
results/2026-10-09/identity-shortcut.patch to baseline 417b66f2 and rebuild
the integrators before running. Production transport no longer has the flag.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import time
from pathlib import Path

import numpy as np
from pollia import RenderSession
from pollia.scenes import build_scene
from pis_probe import hardware_metadata

CONFIGURATIONS = [('mueller_off', 'pollia_mueller', False),
                  ('mueller_on', 'pollia_mueller', True),
                  ('pis_off', 'pollia_pis', False),
                  ('pis_on', 'pollia_pis', True)]
CASES = [('nylon_depth4', dict(size=512, spp=64, max_depth=4)),
         ('nylon_depth8', dict(size=512, spp=64, max_depth=8)),
         ('polarized_depth4', dict(size=512, spp=64, max_depth=4,
                                  source_dop=.8, source_aolp=31, source_ellipticity=-.4)),
         ('preview_depth4', dict(size=256, spp=4, max_depth=4))]


def scene_description(mi, engine, shortcut, parameters, *, profile='nylon'):
    options = {k: v for k, v in parameters.items() if k != 'spp'}
    options.setdefault('rr_depth', 100)
    options['integrator'] = engine
    description = build_scene(mi, **options)
    description['integrator']['integrator']['identity_shortcut'] = shortcut
    if profile != 'nylon':
        for material in ('white', 'green', 'red'):
            description[material]['xi0_max'] = (.25 if profile == 'depolarizer'
                else mi.ScalarColor3f(.25, 1., .75) if 'rgb' in mi.variant() else 1.)
    return description


def emitter_description(mi, engine, shortcut):
    T = mi.ScalarTransform4f
    return dict(type='scene', integrator=dict(type='stokes', integrator=dict(
        type=engine, max_depth=4, rr_depth=100, identity_shortcut=shortcut)),
        sensor=dict(type='perspective', fov=35,
            to_world=T.look_at(origin=[0, 0, 2], target=[0, 0, 0], up=[0, 1, 0]),
            sampler=dict(type='independent'),
            film=dict(type='hdrfilm', width=24, height=24, rfilter=dict(type='box'))),
        light=dict(type='rectangle', bsdf=dict(type='pollia_jarecki', reflectance=0.),
            emitter=dict(type='pollia_polarized', radiance=2., dop=.8,
                         aolp=31, ellipticity=-.4)))


def verify(session):
    checks = []
    scenarios = [('direct_emitter', None, 'nylon'),
                 ('direct_light', dict(max_depth=2), 'nylon'),
                 ('multi_bounce', dict(max_depth=5), 'nylon'),
                 ('depolarizer', dict(max_depth=5), 'depolarizer'),
                 ('divergent_channels', dict(max_depth=5), 'divergent'),
                 ('roulette', dict(max_depth=6, rr_depth=1), 'nylon')]
    for scenario, parameters, profile in scenarios:
        for engine in ('pollia_mueller', 'pollia_pis'):
            for seed in range(4):
                images = []
                for shortcut in (False, True):
                    description = (emitter_description(session.mi, engine, shortcut)
                        if parameters is None else scene_description(session.mi, engine, shortcut,
                            dict(size=24, source_dop=.8, source_aolp=31,
                                 source_ellipticity=-.4, **parameters), profile=profile))
                    session.load_dict(description)
                    images.append(session.render(spp=128, seed=seed, output='full').pixels.copy())
                a, b = images
                if not np.isfinite(a).all() or not np.isfinite(b).all():
                    raise AssertionError(f'{scenario}/{engine}: nonfinite output')
                error = np.abs(a-b)
                # Same samples and estimator; tolerance permits rounding only.
                intensity_channels = 3 if 'rgb' in session.variant else 1
                scale = np.tile(np.abs(a[..., :intensity_channels]), (1, 1, 5))
                bound = 2e-6 + 2e-5*scale
                if np.any(error > bound):
                    raise AssertionError(f'{scenario}/{engine}/{seed}: shortcut changed output; '
                                         f'max abs {error.max()}, max bound ratio {(error/bound).max()}')
                checks.append(dict(scenario=scenario, engine=engine, seed=seed,
                    bit_identical=bool(np.array_equal(a,b)), max_abs_difference=float(error.max()),
                    max_intensity_scaled_difference=float((error/(scale+1e-6)).max())))
        print('verified', scenario, flush=True)
    return checks


def timed(mi, dr, scene, spp, seed):
    start = time.perf_counter()
    image = mi.render(scene, spp=spp, seed=seed, postprocess=False)
    dr.eval(image)
    dr.sync_thread()
    return (time.perf_counter()-start)*1000


def kernel_history(mi, dr, scene, spp):
    with dr.kernel_history() as history:
        image = mi.render(scene, spp=spp, seed=50000, postprocess=False)
        dr.eval(image)
        dr.sync_thread()
    if not history or any(entry.backend != dr.JitBackend.Metal for entry in history):
        raise AssertionError('Benchmark did not run exclusively on Metal')
    return [dict(type=str(e.type), size=e.size, operation_count=e.operation_count,
                 execution_time_ms=e.execution_time) for e in history]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=RenderSession.variants, default='metal_rgb_polarized')
    parser.add_argument('--blocks', type=int, default=4)
    parser.add_argument('--repeats', type=int, default=12)
    parser.add_argument('--warmups', type=int, default=3)
    parser.add_argument('--case', choices=[name for name, _ in CASES], action='append')
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if min(args.blocks, args.repeats, args.warmups) < 1:
        parser.error('blocks/repeats/warmups must be positive')
    root = Path(__file__).resolve().parents[1]
    session = RenderSession(args.variant)
    report = dict(hardware=hardware_metadata(), variant=args.variant,
        mitsuba_version=session.mi.MI_VERSION,
        base_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
        source_sha256={str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [root/'src/integrators/pollia_mueller.cpp', root/'src/integrators/pollia_pis.cpp',
                      root/'rendering/pollia/materials.py', Path(__file__)]},
        power=subprocess.check_output(['pmset', '-g', 'batt'], text=True).strip(),
        protocol=dict(blocks=args.blocks, repeats_per_block=args.repeats, warmups=args.warmups,
            order='rotate by block and iteration; reverse alternating blocks',
            seeds='same seed for all four configurations in each paired iteration',
            timing='synchronized complete render; host readback and display excluded',
            jit_flags='unchanged defaults', identity_shortcut_default=False),
        correctness=verify(session), cases={})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    if args.verify_only:
        return
    for case_index, (name, parameters) in enumerate(CASES):
        if args.case and name not in args.case:
            continue
        scenes = {label: session.mi.load_dict(scene_description(session.mi, engine, shortcut, parameters))
                  for label, engine, shortcut in CONFIGURATIONS}
        blocks = {label: [] for label in scenes}
        for block in range(args.blocks):
            labels = list(scenes)
            if block % 2:
                labels.reverse()
            for warmup in range(args.warmups):
                for label in labels:
                    timed(session.mi, session.dr, scenes[label], parameters['spp'], 100+warmup)
            samples = {label: [] for label in scenes}
            for iteration in range(args.repeats):
                shift = (block+iteration) % 4
                order = labels[shift:]+labels[:shift]
                seed = 1000 + case_index*10000 + block*args.repeats + iteration
                for label in order:
                    samples[label].append(timed(session.mi, session.dr, scenes[label], parameters['spp'], seed))
            for label in scenes:
                blocks[label].append(samples[label])
            print(name, 'block', block+1, {k: round(statistics.median(v),3) for k,v in samples.items()}, flush=True)
        medians = {label: [statistics.median(block) for block in values] for label, values in blocks.items()}
        summary = {label: dict(median_ms=statistics.median(values), block_medians_ms=values,
                              block_range_ms=[min(values), max(values)]) for label, values in medians.items()}
        gains = {engine: dict(throughput_gain_percent=100*(summary[engine+'_off']['median_ms']/summary[engine+'_on']['median_ms']-1),
            paired_block_gain_percent=[100*(a/b-1) for a,b in zip(medians[engine+'_off'], medians[engine+'_on'])])
            for engine in ('mueller','pis')}
        report['cases'][name] = dict(settings=parameters, samples_ms=blocks, summary=summary, shortcut_gains=gains,
            kernel_history={label:kernel_history(session.mi, session.dr, scene, parameters['spp'])
                            for label, scene in scenes.items()})
        args.output.write_text(json.dumps(report, indent=2)+'\n')
        print(name, 'SUMMARY', {k:round(v['median_ms'],3) for k,v in summary.items()}, gains, flush=True)


if __name__ == '__main__':
    main()
