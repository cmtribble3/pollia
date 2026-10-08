"""Persistent Mitsuba scene state. No process or UI dependencies."""
from dataclasses import dataclass
import time


class MetalBackendError(RuntimeError):
    pass


@dataclass
class RenderFrame:
    pixels: object
    channel_names: list[str]
    render_ms: float
    readback_ms: float
    kernel_count: int
    sample_count: int


class RenderSession:
    # Changing Mitsuba variants in a running interpreter invalidates typed objects.
    # Use a new session/process for a different variant.
    variants = ("metal_rgb_polarized", "metal_ad_mono_polarized")

    def __init__(self, variant="metal_rgb_polarized"):
        if variant not in self.variants:
            raise ValueError("Pollia requires a supported polarized Metal variant")
        import drjit as dr
        import mitsuba as mi
        import numpy as np
        if mi.variant() is not None and mi.variant() != variant:
            raise ValueError("Start a fresh interpreter to change the active variant")
        mi.set_variant(variant)
        mi.set_log_level(mi.LogLevel.Warn)
        dr.set_log_level(dr.LogLevel.Error)
        if not dr.has_backend(dr.JitBackend.Metal):
            raise MetalBackendError("Metal unavailable; Pollia has no CPU fallback")
        self.dr, self.mi, self.np = dr, mi, np
        self.variant = variant
        self.scene = None
        self._mean = None
        self._sample_count = 0

    def load_preset(self, **settings):
        from .scenes import build_scene
        self.load_dict(build_scene(self.mi, **settings))

    def load_file(self, path, **parameters):
        scene = self.mi.load_file(str(path), **parameters)
        self._accept_scene(scene)

    def load_dict(self, description):
        """Accept native Mitsuba dictionaries, including Python plugin objects."""
        scene = self.mi.load_dict(description)
        self._accept_scene(scene)

    def _accept_scene(self, scene):
        if not scene.sensors() or scene.integrator() is None:
            raise ValueError("Scene needs a sensor and an integrator")
        # A failed load leaves the previous scene usable.
        self.scene = scene
        self._mean = None
        self._sample_count = 0

    def render(self, *, spp=16, seed=0, sensor=0, output="preview", accumulate=False):
        if self.scene is None:
            raise ValueError("Load a scene before rendering")
        if type(spp) is not int or not 0 < spp <= 2**32 - 1:
            raise ValueError("spp must be a positive uint32")
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError("seed must be a uint32")
        if type(sensor) is not int or not 0 <= sensor < len(self.scene.sensors()):
            raise ValueError("Invalid sensor index")
        if output not in ("preview", "full"):
            raise ValueError("output must be preview or full")
        if type(accumulate) is not bool:
            raise ValueError("accumulate must be a boolean")
        mi, dr, np = self.mi, self.dr, self.np
        film = self.scene.sensors()[sensor].film()
        names = list(film.base_channels()) + list(self.scene.integrator().aov_names(film))
        if output == "preview" and names[:3] != ["R", "G", "B"]:
            raise ValueError("RGB preview requires RGB film channels; use full for mono")
        start = time.perf_counter()
        with dr.kernel_history() as history:
            image = mi.render(self.scene, spp=spp, seed=seed, sensor=sensor,
                              postprocess=False)
            if accumulate and self._mean is not None:
                if image.shape != self._mean.shape or sensor != self._mean_sensor:
                    raise ValueError("Start a fresh estimate when changing sensor or image shape")
                weight = spp / (self._sample_count + spp)
                image = self._mean * (1.0 - weight) + image * weight
            dr.eval(image)
            dr.sync_thread()
        render_ms = (time.perf_counter() - start) * 1000
        entries = list(history)
        if not entries or any(e.backend != dr.JitBackend.Metal for e in entries):
            raise MetalBackendError("Render did not execute exclusively on Metal")
        if len(image.shape) != 3 or image.shape[-1] != len(names):
            raise ValueError("Film channel names do not match the rendered tensor")
        self._sample_count = (self._sample_count if accumulate else 0) + spp
        self._mean, self._mean_sensor = image, sensor
        # Crop previews on Metal; avoid Python's expensive strided CPU RGB copy.
        if output == "preview":
            h, w, channels = image.shape
            indices = dr.arange(mi.UInt32, h * w * 3)
            rgb = dr.gather(mi.Float, image.array,
                            (indices // 3) * channels + indices % 3)
            image = mi.TensorXf(rgb, shape=(h, w, 3))
            names = names[:3]
        start = time.perf_counter()
        pixels = np.ascontiguousarray(np.asarray(image), dtype="<f4")
        readback_ms = (time.perf_counter() - start) * 1000
        # Holding pixels retains the host buffer while it is published.
        return RenderFrame(pixels, names, render_ms, readback_ms, len(entries), self._sample_count)
