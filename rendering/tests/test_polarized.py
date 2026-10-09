"""Independent source-basis and real Metal polarized-transport checks."""
from pathlib import Path
import subprocess
import sys
import unittest


class PolarizedEmitterTests(unittest.TestCase):
    def test_metal_mono(self):
        subprocess.run([sys.executable, str(Path(__file__).with_name('polarized_check.py')),
                        'metal_ad_mono_polarized'], check=True, timeout=180)

    def test_metal_rgb(self):
        subprocess.run([sys.executable, str(Path(__file__).with_name('polarized_check.py')),
                        'metal_rgb_polarized'], check=True, timeout=180)


class RGBMaterialTests(unittest.TestCase):
    def test_channel_specific_materials_and_states(self):
        subprocess.run([sys.executable, str(Path(__file__).with_name('rgb_material_check.py'))],
                       check=True, timeout=600)
