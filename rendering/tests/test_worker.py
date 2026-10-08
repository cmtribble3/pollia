"""Real Metal integration checks; run with Pollia's configured PYTHONPATH."""
import json
import mmap
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from pollia import RenderSession

ROOT = Path(__file__).resolve().parents[2]


class Worker:
    def __init__(self, variant="metal_rgb_polarized"):
        self.directory = tempfile.TemporaryDirectory(prefix="pollia test ")
        self.path = Path(self.directory.name) / "shared frame.f32"
        self.path.touch(mode=0o600)
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen([sys.executable, "-u", "-m", "pollia.worker",
                                         "--shared-file", str(self.path), "--variant", variant],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.log)
        self.request_id = 0
        self.ready = self.receive()

    def receive(self):
        if not select.select([self.process.stdout], [], [], 30)[0]:
            raise TimeoutError("Worker reply timed out")
        line = self.process.stdout.readline()
        if not line:
            self.log.seek(0)
            raise RuntimeError(self.log.read().decode())
        return json.loads(line)

    def request(self, command, **fields):
        self.request_id += 1
        self.process.stdin.write(json.dumps(dict(id=self.request_id, command=command, **fields)).encode() + b"\n")
        self.process.stdin.flush()
        reply = self.receive()
        assert reply["id"] == self.request_id
        return reply

    def frame(self, **options):
        response = self.request("render", **options)
        assert response["ok"], response
        metadata = response["result"]
        with open(self.path, "r+b") as file:
            file.truncate(metadata["bytes"])
            published = self.request("publish", generation=metadata["generation"], capacity=metadata["bytes"])
            assert published["ok"], published
            with mmap.mmap(file.fileno(), metadata["bytes"], access=mmap.ACCESS_READ) as shared:
                pixels = np.frombuffer(shared, dtype="<f4").reshape(metadata["shape"]).copy()
        return pixels, metadata

    def close(self):
        if self.process.poll() is None:
            try:
                self.request("shutdown")
                self.process.wait(timeout=10)
            finally:
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        self.log.close()
        self.directory.cleanup()


class MetalWorkerTests(unittest.TestCase):
    def test_presets_and_weighted_progressive_estimate(self):
        direct = RenderSession()
        for preset in ("cornell", "glass", "conductor", "jarecki"):
            direct.load_preset(preset=preset, size=32)
            frame = direct.render(spp=4, output="full")
            self.assertEqual(frame.pixels.shape, (32, 32, 15))
            self.assertTrue(np.isfinite(frame.pixels).all())
            self.assertGreater(frame.pixels[..., :3].max(), 0)
        direct.load_preset(preset="conductor", size=32)
        first = direct.render(spp=2, seed=13, output="full").pixels.copy()
        second = direct.render(spp=6, seed=14, output="full").pixels.copy()
        direct.render(spp=2, seed=13, output="full")
        combined = direct.render(spp=6, seed=14, output="full", accumulate=True)
        self.assertEqual(combined.sample_count, 8)
        np.testing.assert_allclose(combined.pixels, first * 0.25 + second * 0.75, rtol=2e-5, atol=2e-6)
        self.assertEqual(direct.render(spp=3, output="full").sample_count, 3)

    def test_frames_match_direct_api_and_errors_preserve_session(self):
        worker = Worker()
        self.addCleanup(worker.close)
        self.assertEqual(worker.ready, dict(event="ready", protocol=1, variant="metal_rgb_polarized"))
        scene = ROOT / "benchmarks/orchestration/scene.xml"
        self.assertFalse(worker.request("render")["ok"])
        self.assertTrue(worker.request("load", path=str(scene), parameters={"size": "32"})["ok"])
        full, metadata = worker.frame(spp=4, seed=7, output="full")
        direct = RenderSession()
        direct.load_file(scene, size="32")
        baseline = direct.render(spp=4, seed=7, output="full")
        np.testing.assert_allclose(full, baseline.pixels, rtol=2e-5, atol=2e-6)
        self.assertEqual(metadata["channel_names"], baseline.channel_names)
        self.assertGreater(metadata["kernel_count"], 0)
        preview, _ = worker.frame(spp=4, seed=7)
        np.testing.assert_allclose(preview, full[..., :3], rtol=2e-5, atol=2e-6)
        self.assertFalse(worker.request("load", path="/no/such/scene.xml")["ok"])
        self.assertFalse(worker.request("render", spp=-1)["ok"])
        self.assertFalse(worker.request("render", seed=2**32)["ok"])
        self.assertFalse(worker.request("publish", generation=-1, capacity=1)["ok"])
        self.assertTrue(worker.request("ping")["ok"])
        after, _ = worker.frame(spp=4, seed=7, output="full")
        np.testing.assert_allclose(after, full, rtol=2e-5, atol=2e-6)
        # Grow and then shrink the mapping without losing the retained scene.
        for size in (64, 16):
            self.assertTrue(worker.request("load", path=str(scene), parameters={"size": str(size)})["ok"])
            pixels, _ = worker.frame(spp=4, output="full")
            self.assertEqual(pixels.shape, (size, size, 15))
            self.assertTrue(np.isfinite(pixels).all())

    def test_mono_custom_integrator(self):
        worker = Worker("metal_ad_mono_polarized")
        self.addCleanup(worker.close)
        reply = worker.request("load", path=str(ROOT / "scenes/pollia-plugin-smoke.xml"),
                               parameters={"integrator": "pollia_pis"})
        self.assertTrue(reply["ok"], reply)
        self.assertFalse(worker.request("render", output="preview")["ok"])
        pixels, metadata = worker.frame(spp=4, output="full")
        self.assertEqual(pixels.shape, (32, 32, 5))
        self.assertEqual(metadata["variant"], "metal_ad_mono_polarized")
        self.assertTrue(np.isfinite(pixels).all())

    def test_polarized_cornell_worker_controls(self):
        for variant, count in (("metal_ad_mono_polarized", 5), ("metal_rgb_polarized", 15)):
            worker = Worker(variant)
            self.addCleanup(worker.close)
            for integrator in ("pollia_mueller", "pollia_pis"):
                reply = worker.request("preset", settings=dict(preset="cornell", size=32,
                    integrator=integrator, source_dop=.8, source_aolp=31,
                    source_ellipticity=-.4))
                self.assertTrue(reply["ok"], reply)
                pixels, metadata = worker.frame(spp=8, seed=7, output="full")
                self.assertEqual(pixels.shape, (32, 32, count))
                self.assertTrue(np.isfinite(pixels).all())
                self.assertGreater(np.abs(pixels[..., 2:]).max(), 0)
                if count == 15:
                    for color in ("R", "G", "B"):
                        self.assertIn(f"S3.{color}", metadata["channel_names"])
                    preview, _ = worker.frame(spp=8, seed=7)
                    np.testing.assert_allclose(preview, pixels[..., :3], rtol=2e-5, atol=2e-6)

    def test_cpu_variant_is_rejected(self):
        worker = Worker("scalar_rgb")
        self.addCleanup(worker.close)
        self.assertEqual(worker.ready["event"], "fatal")
        self.assertIn("Metal", worker.ready["error"]["message"])
        self.assertEqual(worker.process.wait(timeout=10), 1)


if __name__ == "__main__":
    unittest.main()
