"""Editable scene presets for the Pollia app and Python experiments."""
import math
from .materials import jarecki_material


def build_scene(mi, *, preset="cornell", size=512, max_depth=4, rr_depth=8,
                integrator="path", light_scale=1.0, yaw=0.0, pitch=0.0, distance=1.0,
                source_dop=0.0, source_aolp=0.0, source_ellipticity=0.0):
    if preset not in ("cornell", "glass", "conductor", "jarecki"):
        raise ValueError("Unknown scene preset")
    if type(size) is not int or not 16 <= size <= 4096:
        raise ValueError("Image size must be between 16 and 4096")
    if type(max_depth) is not int or not 1 <= max_depth <= 64:
        raise ValueError("Depth must be between 1 and 64")
    if type(rr_depth) is not int or not 1 <= rr_depth <= 128:
        raise ValueError("RR depth must be between 1 and 128")
    if integrator not in ("path", "volpath", "pollia_pis", "pollia_mueller"):
        raise ValueError("Unknown integrator")
    for value in (light_scale, yaw, pitch, distance, source_dop, source_aolp, source_ellipticity):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Scene controls must be finite numbers")
    if not 0 <= light_scale <= 100 or not 0.25 <= distance <= 4 or not -80 <= pitch <= 80:
        raise ValueError("Light or camera controls are out of range")
    if not 0 <= source_dop <= 1 or not -90 <= source_aolp <= 90 or not -1 <= source_ellipticity <= 1:
        raise ValueError("Source DoP, AoLP or ellipticity is out of range")
    T = mi.ScalarTransform4f
    if preset == "cornell":
        scene = mi.cornell_box()
        # Preserve Mitsuba's Cornell geometry; use the full PolRAY nylon materials.
        # Every surface, including the emitter shape, uses our material type.
        for name, material in (("white", "nylonWhite"), ("green", "nylonGreen"),
                               ("red", "nylonRed40")):
            scene[name] = jarecki_material(mi, material)
        scene["light"]["emitter"].update(type="pollia_polarized", dop=source_dop,
                                      aolp=source_aolp, ellipticity=source_ellipticity)
        scene["light"]["emitter"]["radiance"]["value"] = [18.387 * light_scale, 13.9873 * light_scale, 6.75357 * light_scale]
        radius, target = 3.9 * distance, [0, 0, 0]
    else:
        material = {"glass": dict(type="dielectric", int_ior=1.5),
                    "conductor": dict(type="roughconductor", material="Al", alpha=0.1),
                    "jarecki": jarecki_material(mi, "nylonWhite")}[preset]
        scene = dict(type="scene", sphere=dict(type="sphere", bsdf=material),
                     environment=dict(type="constant", radiance=dict(type="rgb", value=[light_scale, 0.8 * light_scale, 0.6 * light_scale])))
        radius, target = 5 * distance, [0, 0, 0]
    a, b = math.radians(yaw), math.radians(pitch)
    origin = [radius * math.sin(a) * math.cos(b), radius * math.sin(b), radius * math.cos(a) * math.cos(b)]
    scene["sensor"] = dict(type="perspective", fov=39 if preset == "cornell" else 35,
                           to_world=T().look_at(origin=origin, target=target, up=[0, 1, 0]),
                           sampler=dict(type="independent", sample_count=4),
                           film=dict(type="hdrfilm", width=size, height=size,
                                     rfilter=dict(type="box")))
    scene["integrator"] = dict(type="stokes", integrator=dict(type=integrator,
                                                            max_depth=max_depth, rr_depth=rr_depth))
    return scene
