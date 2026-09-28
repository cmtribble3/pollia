"""Compare stock path, paired Mueller, and PIS for each PolRAY RGB channel.

Each run is monochrome and uses one channel's Jarecki coefficients and albedo
for the white, green, and red Cornell materials. The channels are RGB model
channels, not calibrated wavelengths. Geometry is Mitsuba's built-in Cornell
scene, not Abhi's mesh-matched scene.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from pathlib import Path

import mitsuba as mi
import numpy as np

from pis_probe import hardware_metadata, render_timed


# Copied from PolRayMaterials/Material.swift: nylonWhite, nylonGreen,
# nylonRed40, in RGB order. Albedo is the matching channel's scalar BRDF.
MATERIALS = {
    "white": [
        dict(albedo=.97, a=.26, b=2.0, n_real=1.26, n_imag=.08, xi0_max=.26),
        dict(albedo=.97, a=.26, b=2.0, n_real=1.25, n_imag=.08, xi0_max=.26),
        dict(albedo=.97, a=.26, b=2.0, n_real=1.24, n_imag=.08, xi0_max=.26),
    ],
    "green": [
        dict(albedo=.08, a=.07, b=2.1, n_real=1.30, n_imag=.25, xi0_max=.71),
        dict(albedo=.53, a=.17, b=2.5, n_real=1.23, n_imag=.08, xi0_max=.43),
        dict(albedo=.13, a=.13, b=2.4, n_real=1.20, n_imag=.13, xi0_max=.54),
    ],
    "red": [
        dict(albedo=.80, a=.17, b=2.0, n_real=1.30, n_imag=.08, xi0_max=.33),
        dict(albedo=.05, a=.09, b=2.4, n_real=1.23, n_imag=.19, xi0_max=1.0),
        dict(albedo=.05, a=.03, b=2.5, n_real=1.20, n_imag=.25, xi0_max=.75),
    ],
}
CHANNELS = ("red", "green", "blue")
INTEGRATORS = ("path", "pollia_mueller", "pollia_pis")


def make_scene(integrator: str, channel_index: int, size: int,
               max_depth: int) -> mi.Scene:
    scene_dict = mi.cornell_box()
    for name, channels in MATERIALS.items():
        channel = channels[channel_index]
        scene_dict[name].update(
            type="pollia_jarecki",
            reflectance={"type": "uniform", "value": channel["albedo"]},
            **{key: value for key, value in channel.items() if key != "albedo"},
        )
    scene_dict["integrator"] = {
        "type": "stokes",
        "nested": {"type": integrator, "max_depth": max_depth, "rr_depth": 100},
    }
    scene_dict["sensor"]["film"].update(width=size, height=size)
    return mi.load_dict(scene_dict)


def stokes(image: np.ndarray) -> np.ndarray:
    return image[..., 1:5]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--spp", type=int, default=64)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=12)
    parser.add_argument("--quality-size", type=int, default=128)
    parser.add_argument("--quality-spp", type=int, default=1024)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.size, args.spp, args.max_depth, args.repeats,
           args.quality_size, args.quality_spp) < 1 or args.warmups < 0:
        parser.error("sizes, spp, depth, and repeats must be positive")

    hardware = hardware_metadata()
    mi.set_variant("metal_ad_mono_polarized")
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True,
        cwd=Path(__file__).resolve().parents[1]
    ).strip()
    report = {
        "hardware": hardware,
        "mitsuba_version": mi.MI_VERSION,
        "git_revision": revision,
        "variant": mi.variant(),
        "scene": "Mitsuba built-in Cornell geometry; PolRAY RGB channel material coefficients and matching albedos",
        "channel_semantics": "RGB model channels, not specified physical wavelengths",
        "size": args.size,
        "spp": args.spp,
        "max_depth": args.max_depth,
        "warmups": args.warmups,
        "repeats": args.repeats,
        "quality_size": args.quality_size,
        "quality_spp": args.quality_spp,
        "channels": {},
    }

    for channel_index, channel_name in enumerate(CHANNELS):
        scenes = {
            name: make_scene(name, channel_index, args.size, args.max_depth)
            for name in INTEGRATORS
        }
        for warmup in range(args.warmups):
            for name in INTEGRATORS[warmup % 3:] + INTEGRATORS[:warmup % 3]:
                render_timed(scenes[name], args.spp, 10 + warmup)

        times = {name: [] for name in INTEGRATORS}
        for repeat in range(args.repeats):
            # Rotate order to reduce temperature/cache/queue-order bias.
            order = INTEGRATORS[repeat % 3:] + INTEGRATORS[:repeat % 3]
            for name in order:
                seconds, _ = render_timed(scenes[name], args.spp, 100 + repeat)
                times[name].append(seconds)

        medians = {name: statistics.median(values)
                   for name, values in times.items()}
        quality_scenes = {
            name: make_scene(name, channel_index, args.quality_size,
                             args.max_depth)
            for name in INTEGRATORS
        }
        images = {}
        for name in INTEGRATORS:
            _, image = render_timed(quality_scenes[name], args.quality_spp,
                                    2026)
            images[name] = stokes(image)
        reference = images["pollia_mueller"]
        signal_rms = np.sqrt(np.mean(reference ** 2, axis=(0, 1)))
        quality = {}
        for name in INTEGRATORS:
            delta = images[name] - reference
            quality[name] = {
                "mean_stokes": np.mean(images[name], axis=(0, 1)).tolist(),
                "rms_difference_over_mueller_signal": (
                    np.sqrt(np.mean(delta ** 2, axis=(0, 1))) /
                    np.maximum(signal_rms, 1e-12)
                ).tolist(),
            }

        report["channels"][channel_name] = {
            "materials": {name: channels[channel_index]
                          for name, channels in MATERIALS.items()},
            "times_seconds": times,
            "median_seconds": medians,
            "pis_vs_mueller_throughput": (medians["pollia_mueller"] /
                                          medians["pollia_pis"]),
            "mueller_vs_stock_throughput": (medians["path"] /
                                            medians["pollia_mueller"]),
            "pis_vs_stock_throughput": (medians["path"] /
                                        medians["pollia_pis"]),
            "quality": quality,
        }
        print(channel_name, {key: round(value * 1000, 3)
                             for key, value in medians.items()}, flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
