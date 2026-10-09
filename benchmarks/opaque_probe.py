"""Archived four-way opaque/literal Jarecki parameter experiment.

Requires results/2026-10-09/opaque-parameters.patch applied to 417b66f2
and rebuilt pollia_jarecki. No integrator or material coefficient changes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time
import numpy as np
from pollia import RenderSession
from pollia.scenes import build_scene
from pis_probe import hardware_metadata

CONFIGS = [('mueller_opaque', 'pollia_mueller', True),
           ('mueller_literal', 'pollia_mueller', False),
           ('pis_opaque', 'pollia_pis', True),
           ('pis_literal', 'pollia_pis', False)]


def description(mi, engine, opaque, **settings):
    scene = build_scene(mi, integrator=engine, **settings)
    def visit(value):
        if isinstance(value, dict):
            if value.get('type') == 'pollia_jarecki':
                value['opaque_parameters'] = opaque
            for child in value.values():
                visit(child)
    visit(scene)
    return scene


def verify(session):
    records=[]
    for scenario, extra in [('multi_bounce', {}), ('polarized', dict(source_dop=.8, source_aolp=31, source_ellipticity=-.4)),
                            ('roulette', dict(rr_depth=1)), ('divergent_channels', {})]:
        for engine in ('pollia_mueller', 'pollia_pis'):
            scenes=[]
            for opaque in (True, False):
                options=dict(size=24, max_depth=5, rr_depth=100)
                options.update(extra)
                d=description(session.mi, engine, opaque, **options)
                if scenario == 'divergent_channels':
                    d['green']['xi0_max'] = session.mi.ScalarColor3f(.25, 1., .75)
                scenes.append(d)
            for seed in range(4):
                images=[]
                for d in scenes:
                    session.load_dict(d)
                    images.append(session.render(spp=128, seed=seed, output='full').pixels.copy())
                a,b=images
                if not np.isfinite(a).all() or not np.isfinite(b).all():
                    raise AssertionError('Nonfinite output')
                scale=np.tile(np.abs(a[..., :3]), (1,1,5))
                error=np.abs(a-b)
                bound=2e-6+2e-5*scale
                if np.any(error>bound):
                    raise AssertionError(f'{scenario}/{engine}/{seed}: output changed, max error {error.max()}')
                records.append(dict(scenario=scenario, engine=engine, seed=seed,
                    bit_identical=bool(np.array_equal(a,b)), max_abs_difference=float(error.max()),
                    max_bound_ratio=float((error/bound).max())))
        print('verified', scenario, flush=True)
    return records


def timed(session, scene, spp, seed):
    start=time.perf_counter()
    image=session.mi.render(scene, spp=spp, seed=seed, postprocess=False)
    session.dr.eval(image)
    session.dr.sync_thread()
    return (time.perf_counter()-start)*1000


def history(session, scene, spp):
    with session.dr.kernel_history() as entries:
        timed(session, scene, spp, 50000)
    if not entries or any(e.backend != session.dr.JitBackend.Metal for e in entries):
        raise AssertionError('Not exclusively Metal')
    return [dict(type=str(e.type), size=e.size, operation_count=e.operation_count,
                 execution_time_ms=e.execution_time) for e in entries]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    session=RenderSession('metal_rgb_polarized')
    settings=dict(size=512, max_depth=4, rr_depth=100)
    spp=64
    report=dict(hardware=hardware_metadata(), variant=session.variant,
        baseline_commit=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
        source_sha256={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [root/'src/bsdfs/pollia_jarecki.cpp', root/'src/integrators/pollia_mueller.cpp',
                      root/'src/integrators/pollia_pis.cpp', root/'rendering/pollia/materials.py', Path(__file__)]},
        power=subprocess.check_output(['pmset','-g','batt'], text=True).strip(),
        protocol=dict(blocks=4, repeats=12, warmups=3, settings=settings, spp=spp,
            order='rotate each iteration; reverse alternating blocks',
            timing='GPU-synchronized complete render, warmed JIT, excludes readback/display',
            seeds='same seed per four-way iteration', jit_flags='unchanged defaults'),
        correctness=verify(session))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    scenes={label:session.mi.load_dict(description(session.mi,engine,opaque,**settings))
            for label,engine,opaque in CONFIGS}
    blocks={label:[] for label in scenes}
    for block in range(4):
        labels=list(scenes)
        if block%2: labels.reverse()
        for warmup in range(3):
            for label in labels: timed(session,scenes[label],spp,100+warmup)
        samples={label:[] for label in scenes}
        for iteration in range(12):
            shift=(block+iteration)%4
            for label in labels[shift:]+labels[:shift]:
                samples[label].append(timed(session,scenes[label],spp,1000+block*12+iteration))
        for label in scenes: blocks[label].append(samples[label])
        report['samples_ms']=blocks
        args.output.write_text(json.dumps(report,indent=2)+'\n')
        print('block',block+1,{k:round(statistics.median(v),3) for k,v in samples.items()},flush=True)
    medians={label:[statistics.median(b) for b in values] for label,values in blocks.items()}
    summary={label:dict(median_ms=statistics.median(values),block_medians_ms=values,
                       block_range_ms=[min(values),max(values)]) for label,values in medians.items()}
    report['summary']=summary
    report['opaque_gains']={engine:dict(throughput_gain_percent=100*(summary[engine+'_literal']['median_ms']/summary[engine+'_opaque']['median_ms']-1),
        paired_block_gain_percent=[100*(a/b-1) for a,b in zip(medians[engine+'_literal'],medians[engine+'_opaque'])])
        for engine in ('mueller','pis')}
    report['kernel_history']={label:history(session,scene,spp) for label,scene in scenes.items()}
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print('SUMMARY',{k:round(v['median_ms'],3) for k,v in summary.items()},report['opaque_gains'],flush=True)


if __name__=='__main__':
    main()
