"""Small, paired Mitsuba-only PIS capability benchmark for Apple Silicon.

Run with the Python used to build Pollia and build-make/python on PYTHONPATH.
The built-in Cornell geometry is *not* Abhi's matched PolRAY geometry.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import drjit as dr
import mitsuba as mi
import numpy as np


MATERIALS = {
    # Green RGB-channel Jarecki coefficients from three PolRAY presets.
    # Original Cornell reflectances remain in place in this mono scene.
    # This is not a wavelength-specific material comparison.
    "white": dict(a=0.26, b=2.0, n_real=1.25, n_imag=0.08, xi0_max=0.26),
    "green": dict(a=0.17, b=2.5, n_real=1.23, n_imag=0.08, xi0_max=0.43),
    "red": dict(a=0.09, b=2.4, n_real=1.23, n_imag=0.19, xi0_max=1.00),
}
STRESS_MATERIAL = dict(a=0.0, b=2.0, n_real=1.5, n_imag=0.25, xi0_max=1.0)


def make_scene(integrator: str, size: int, profile: str,
               max_depth: int) -> mi.Scene:
    scene_dict = mi.cornell_box()
    for material, values in MATERIALS.items():
        if profile == "stress":
            values = STRESS_MATERIAL
        scene_dict[material].update(type="pollia_jarecki", **values)
    scene_dict["integrator"] = {
        "type": "stokes",
        "nested": {"type": integrator, "max_depth": max_depth, "rr_depth": 100},
    }
    scene_dict["sensor"]["film"].update(width=size, height=size)
    return mi.load_dict(scene_dict)


def render_timed(scene: mi.Scene, spp: int, seed: int) -> tuple[float, np.ndarray]:
    start = time.perf_counter()
    image = mi.render(scene, spp=spp, seed=seed)
    dr.eval(image)
    dr.sync_thread()
    elapsed = time.perf_counter() - start
    return elapsed, np.array(image)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=128)
    parser.add_argument("--spp", type=int, default=64)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=8)
    parser.add_argument("--quality-spp", type=int, default=512)
    parser.add_argument("--material-profile", default="nylon",
                        choices=("nylon", "stress"))
    parser.add_argument("--mueller-integrator", default="pollia_mueller",
                        choices=("pollia_mueller", "path"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--kernel-history", action="store_true")
    args = parser.parse_args()
    if min(args.size, args.spp, args.repeats, args.quality_spp,
           args.max_depth) < 1:
        parser.error("size, spp, repeats, quality-spp, and max-depth must be positive")

    mi.set_variant("metal_ad_mono_polarized")
    scenes = {
        "mueller": make_scene(args.mueller_integrator, args.size,
                              args.material_profile, args.max_depth),
        "pis": make_scene("pollia_pis", args.size, args.material_profile,
                          args.max_depth),
    }

    first = {}
    for name in ("mueller", "pis"):
        first[name], _ = render_timed(scenes[name], args.spp, seed=1)

    for i in range(args.warmups):
        for name in (("pis", "mueller") if i % 2 else ("mueller", "pis")):
            render_timed(scenes[name], args.spp, seed=10 + i)

    changed = {}
    for name, scene in scenes.items():
        params = mi.traverse(scene)
        old = params["white.reflectance.value"]
        start = time.perf_counter()
        params["white.reflectance.value"] = old * 0.99
        params.update()
        render_timed(scene, args.spp, seed=31)
        changed[name] = time.perf_counter() - start
        params["white.reflectance.value"] = old
        params.update()
        render_timed(scene, args.spp, seed=32)

    times = {"mueller": [], "pis": []}
    for i in range(args.repeats):
        order = ("mueller", "pis") if i % 2 == 0 else ("pis", "mueller")
        for name in order:
            seconds, _ = render_timed(scenes[name], args.spp, seed=100 + i)
            times[name].append(seconds)

    # Independent seeds show agreement within sampling noise, not pixel parity.
    quality_images = {}
    for name in ("mueller", "pis"):
        _, quality_images[name] = render_timed(
            scenes[name], args.quality_spp, seed=999
        )
    # The stock Stokes wrapper stores intensity twice (base and S0).
    delta = quality_images["pis"][..., 1:5] - quality_images["mueller"][..., 1:5]
    mae = np.mean(np.abs(delta), axis=(0, 1)).tolist()
    rms = np.sqrt(np.mean(delta * delta, axis=(0, 1))).tolist()
    signal_rms = np.sqrt(np.mean(
        quality_images["mueller"][..., 1:5] ** 2, axis=(0, 1)
    )).tolist()

    medians = {name: statistics.median(values) for name, values in times.items()}
    samples = args.size * args.size * args.spp
    report = {
        "mitsuba_version": mi.MI_VERSION,
        "variant": mi.variant(),
        "mueller_integrator": args.mueller_integrator,
        "material_profile": args.material_profile,
        "scene": "Mitsuba built-in Cornell box, not PolRAY mesh matched",
        "size": args.size,
        "spp": args.spp,
        "max_depth": args.max_depth,
        "warmups": args.warmups,
        "repeats": args.repeats,
        "quality_spp": args.quality_spp,
        "first_render_seconds": first,
        "changed_material_seconds": changed,
        "steady_seconds": times,
        "steady_median_seconds": medians,
        "steady_million_samples_per_second": {
            name: samples / seconds / 1e6 for name, seconds in medians.items()
        },
        "steady_speedup_pis_vs_mueller": medians["mueller"] / medians["pis"],
        "stokes_mae_by_channel": mae,
        "stokes_rms_difference_by_channel": rms,
        "mueller_stokes_rms_signal_by_channel": signal_rms,
        "rms_difference_over_signal_by_channel": [
            error / max(signal, 1e-12) for error, signal in zip(rms, signal_rms)
        ],
        "stokes_channel_order": ["I", "Q", "U", "V"],
        "limitations": [
            "The two quality renders use the same seed but different random streams.",
            "PIS is expected to have higher variance; RMS difference is not bias.",
            "Only mono polarized, diffuse Jarecki surfaces, unpolarized emission.",
            "Mitsuba's stock Stokes wrapper develops duplicate intensity; both paths share this cost.",
            "First render order is Mueller then PIS, so cache effects may differ.",
        ],
    }
    if args.kernel_history:
        report["kernel_history"] = {}
        for name, scene in scenes.items():
            with dr.kernel_history() as history:
                image = mi.render(scene, spp=args.spp, seed=2026)
                dr.eval(image)
            report["kernel_history"][name] = [
                {
                    "type": str(entry.type),
                    "size": entry.size,
                    "input_count": entry.input_count,
                    "output_count": entry.output_count,
                    "operation_count": entry.operation_count,
                    "execution_time_ms": entry.execution_time,
                }
                for entry in history
            ]
    content = json.dumps(report, indent=2)
    print(content)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n")


if __name__ == "__main__":
    main()
