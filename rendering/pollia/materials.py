"""Full RGB nylon presets from PolRAY MaterialPresets (Material.swift).

Values are RGB model channels, not specified physical wavelengths.
Monochrome previews explicitly use the green model channel.
"""
from types import MappingProxyType


NYLON_PRESETS = MappingProxyType({
    "nylonWhite": MappingProxyType({
        "reflectance": (0.97, 0.97, 0.97),
        "a": (0.26, 0.26, 0.26),
        "b": (2.0, 2.0, 2.0),
        "n_real": (1.26, 1.25, 1.24),
        "n_imag": (0.08, 0.08, 0.08),
        "xi0_max": (0.26, 0.26, 0.26),
    }),
    "nylonGreen": MappingProxyType({
        "reflectance": (0.08, 0.53, 0.13),
        "a": (0.07, 0.17, 0.13),
        "b": (2.1, 2.5, 2.4),
        "n_real": (1.3, 1.23, 1.2),
        "n_imag": (0.25, 0.08, 0.13),
        "xi0_max": (0.71, 0.43, 0.54),
    }),
    "nylonRed40": MappingProxyType({
        "reflectance": (0.8, 0.05, 0.05),
        "a": (0.17, 0.09, 0.03),
        "b": (2.0, 2.4, 2.5),
        "n_real": (1.3, 1.23, 1.2),
        "n_imag": (0.08, 0.19, 0.25),
        "xi0_max": (0.33, 1.0, 0.75),
    }),
    "nylonRed3": MappingProxyType({
        "reflectance": (0.9, 0.1, 0.1),
        "a": (0.17, 0.09, 0.03),
        "b": (2.0, 2.4, 2.5),
        "n_real": (1.3, 1.23, 1.2),
        "n_imag": (0.08, 0.19, 0.25),
        "xi0_max": (0.33, 0.99, 0.75),
    }),
})


def jarecki_material(mi, preset="nylonWhite", *, channel=None):
    """Native BSDF dictionary; scalar inputs keep monochrome rendering explicit."""
    values = NYLON_PRESETS[preset]
    rgb = "rgb" in mi.variant()
    if channel is None and not rgb:
        channel = 1
    if channel is not None and channel not in (0, 1, 2):
        raise ValueError("Material channel must be 0, 1 or 2")
    if channel is not None:
        return dict(type="pollia_jarecki",
                    **{key: value[channel] for key, value in values.items()})
    return dict(type="pollia_jarecki",
                reflectance=dict(type="rgb", value=list(values["reflectance"])),
                **{key: mi.ScalarColor3f(value) for key, value in values.items()
                   if key != "reflectance"})
