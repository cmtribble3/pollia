"""Metal-only environment smoke check; not a benchmark or scientific validation.

Run: source scripts/pollia-env.sh && python scripts/verify-metal.py
"""
import argparse
import json
import sys

import drjit as dr
import mitsuba as mi
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("metal_ad_mono_polarized", "metal_rgb_polarized"),
                        default="metal_ad_mono_polarized")
    args = parser.parse_args()
    mi.set_variant(args.variant)
    mi.set_log_level(mi.LogLevel.Warn)
    if not dr.has_backend(dr.JitBackend.Metal):
        raise RuntimeError("Metal is unavailable; CPU fallback is disabled")
    scene_dict = mi.cornell_box()
    scene_dict["sensor"]["film"].update(
        width=32, height=32,
        pixel_format="rgb" if "rgb" in args.variant else "luminance"
    )
    scene_dict["integrator"] = {
        "type": "stokes",
        "nested": {"type": "path", "max_depth": 4},
    }
    scene = mi.load_dict(scene_dict)
    image = mi.render(scene, spp=4, seed=1)
    dr.eval(image)
    dr.sync_thread()
    array = np.array(image)
    channels = 15 if "rgb" in args.variant else 5
    if array.shape != (32, 32, channels):
        raise RuntimeError(f"Unexpected Stokes output shape: {array.shape}")
    if not np.isfinite(array).all() or array[..., 0].max() <= 0:
        raise RuntimeError("Render contains invalid values or no positive intensity")
    print(json.dumps({
        "status": "passed",
        "python": sys.version,
        "python_executable": sys.executable,
        "mitsuba": mi.MI_VERSION,
        "mitsuba_module": mi.__file__,
        "drjit": dr.__version__,
        "drjit_module": dr.__file__,
        "variant": mi.variant(),
        "shape": list(array.shape),
        "spp": 4,
        "seed": 1,
        "finite": True,
        "mean_channels": array.mean(axis=(0, 1)).tolist(),
    }, indent=2))


if __name__ == "__main__":
    main()
