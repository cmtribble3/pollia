"""Channel-specific material and path-state checks on the real Metal backend."""
from pathlib import Path
import subprocess
import sys
import tempfile
import numpy as np
from pollia import RenderSession
from pollia.materials import NYLON_PRESETS, jarecki_material
from pollia.scenes import build_scene


def description(mi, integrator, channel=None):
    scene = build_scene(mi, size=24, max_depth=5, rr_depth=100,
                        integrator=integrator, source_dop=.8,
                        source_aolp=31, source_ellipticity=-.4)
    for name, preset in (("white", "nylonWhite"), ("green", "nylonGreen"),
                         ("red", "nylonRed40")):
        scene[name] = jarecki_material(mi, preset, channel=channel)
    # Force very different channel event probabilities, including a deterministic
    # depolarizer. This catches an accidentally shared PIS event/state.
    xi = (.25, 1., .75)
    scene['green']['xi0_max'] = mi.ScalarColor3f(xi) if channel is None else xi[channel]
    if channel is not None:
        scene['light']['emitter']['radiance'] = scene['light']['emitter']['radiance']['value'][channel]
    return scene


if len(sys.argv) > 1:
    channel, output = int(sys.argv[1]), sys.argv[2]
    session = RenderSession('metal_ad_mono_polarized')
    try:
        session.mi.load_dict(dict(type='pollia_jarecki',
                                  a=session.mi.ScalarColor3f(.1, .2, .3)))
    except RuntimeError:
        pass
    else:
        raise AssertionError('Monochrome variant accepted RGB parameters')
    values = {}
    for integrator in ('pollia_mueller', 'pollia_pis'):
        session.load_dict(description(session.mi, integrator, channel))
        frame = session.render(spp=128, seed=17, output='full')
        values[integrator] = frame.pixels
    np.savez(output, **values)
    sys.exit(0)

session = RenderSession('metal_rgb_polarized')
mi = session.mi
# Every material's channel response equals the response from its scalar input.
si = mi.SurfaceInteraction3f()
si.wi = mi.Vector3f(.3, .2, float(np.sqrt(.87)))
wo = mi.Vector3f(-.2, .4, float(np.sqrt(.8)))
for preset in NYLON_PRESETS:
    rgb = mi.load_dict(jarecki_material(mi, preset))
    matrix = rgb.eval(mi.BSDFContext(), si, wo)
    for channel in range(3):
        scalar = mi.load_dict(jarecki_material(mi, preset, channel=channel))
        reference = scalar.eval(mi.BSDFContext(), si, wo)
        for i in range(4):
            for k in range(4):
                np.testing.assert_allclose(np.asarray(matrix[i,k][channel]),
                                           np.asarray(reference[i,k][0]), atol=2e-7, rtol=2e-6)
        params = rgb.eval_attribute_3(f'pollia_jarecki_params_{channel}', si)
        optics = rgb.eval_attribute_3(f'pollia_optical_constants_{channel}', si)
        expected = NYLON_PRESETS[preset]
        np.testing.assert_allclose(np.asarray(params).reshape(3),
            [expected['a'][channel], expected['b'][channel], expected['xi0_max'][channel]], atol=1e-7)
        np.testing.assert_allclose(np.asarray(optics).reshape(3),
            [expected['n_real'][channel], expected['n_imag'][channel], 0], atol=1e-7)

# Invalid values in any channel must be rejected, not just channel zero.
for key, value in (("n_real", [1.2, 0., 1.3]),
                   ("n_imag", [.08, .1, -.1]),
                   ("a", [.2, .3, float('nan')])):
    material = jarecki_material(mi, 'nylonWhite')
    material[key] = mi.ScalarColor3f(value)
    try:
        mi.load_dict(material)
    except RuntimeError:
        pass
    else:
        raise AssertionError(f'Invalid RGB {key} accepted')

images = {}
for integrator in ('pollia_mueller', 'pollia_pis'):
    session.load_dict(description(mi, integrator))
    images[integrator] = session.render(spp=128, seed=17, output='full').pixels.copy()
# Compare complete multi-bounce RGB paths with three scalar/mono oracles, each
# using its own material parameters. RR is disabled to keep the rays paired.
with tempfile.TemporaryDirectory() as temporary:
    for channel in range(3):
        path = Path(temporary) / f'channel-{channel}.npz'
        subprocess.run([sys.executable, __file__, str(channel), str(path)], check=True, timeout=180)
        with np.load(path) as reference:
            for integrator in images:
                rgb = images[integrator][..., [channel + 3*k for k in range(5)]]
                np.testing.assert_allclose(rgb, reference[integrator], atol=3e-6, rtol=3e-4)
print('All four RGB presets and channel-specific Mueller/PIS paths match scalar channel oracles')
