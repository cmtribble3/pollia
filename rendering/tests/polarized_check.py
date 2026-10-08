"""Run in a fresh interpreter for each variant; no CPU rendering fallback."""
import math
import sys
import numpy as np
from pollia import RenderSession
from pollia.scenes import build_scene

session = RenderSession(sys.argv[1])
mi, dr = session.mi, session.dr


def col(m):
    return np.stack([np.asarray(m[i, 0]).reshape(-1) for i in range(4)], axis=-1)


def expected(normal, forward, target, dop, angle, ratio):
    # Independent NumPy implementation of PolRAY's geometric source convention.
    n, f, t = map(lambda x: np.asarray(x, dtype=float), (normal, forward, target))
    y = np.array([0., 1., 0.]) - n[1] * n
    if y @ y > 1e-10:
        x = np.cross(y, n)
    else:
        x = np.array([1., 0., 0.]) - n[0] * n
    x /= np.linalg.norm(x)
    y = np.cross(n, x)
    b = x - (x @ f) * f
    if b @ b <= 1e-10:
        b = np.cross(y - (y @ f) * f, f)
    b /= np.linalg.norm(b)
    theta = math.atan2(f @ np.cross(b, t), b @ t)
    psi, chi = math.radians(angle), math.atan(ratio)
    q = dop * math.cos(2*psi) * math.cos(2*chi)
    u = dop * math.sin(2*psi) * math.cos(2*chi)
    return np.array([1., q*math.cos(2*theta)+u*math.sin(2*theta),
                     -q*math.sin(2*theta)+u*math.cos(2*theta), dop*math.sin(2*chi)])


# Cover pole fallback, oblique rays, signs of U/V, partial and zero polarization.
for normal in ([0, 0, 1], [0, -1, 0], [0.3, 0.4, math.sqrt(.75)]):
    T = mi.ScalarTransform4f().look_at(origin=[0, 0, 0], target=normal, up=[1, 0, 0])
    for dop, angle, ratio in ((0, 0, 0), (1, 0, 0), (1, 45, 0),
                               (1, 0, 1), (1, 0, -1), (.6, -23, .4)):
        shape = mi.load_dict(dict(type='rectangle', to_world=T,
            emitter=dict(type='pollia_polarized', radiance=2., dop=dop,
                         aolp=angle, ellipticity=ratio)))
        emitter = shape.emitter()
        origin = np.array(normal)*2 + np.array([.2, -.1, .05])
        it = mi.Interaction3f(); it.p = mi.Point3f(origin)
        ds, weight = emitter.sample_direction(it, mi.Point2f(.37, .61))
        forward = -np.asarray(ds.d).reshape(3)
        n = np.asarray(ds.n).reshape(3)
        target = np.asarray(mi.mueller.stokes_basis(mi.Vector3f(forward))).reshape(3)
        ref = expected(n, forward, target, dop, angle, ratio)
        value = col(emitter.eval_direction(it, ds))[0]
        np.testing.assert_allclose(value, 2*ref, atol=3e-6)
        np.testing.assert_allclose(col(weight)[0] * float(ds.pdf[0]), value, atol=3e-6)
        si = mi.SurfaceInteraction3f(); si.n = ds.n; si.sh_frame = mi.Frame3f(ds.n)
        si.wi = si.to_local(-ds.d); si.p = ds.p; si.uv = ds.uv
        np.testing.assert_allclose(col(emitter.eval(si))[0], value, atol=3e-6)
        ray, ray_weight = emitter.sample_ray(0., .3, mi.Point2f(.4, .6), mi.Point2f(.2, .7))
        f = np.asarray(ray.d).reshape(3)
        t = np.asarray(mi.mueller.stokes_basis(ray.d)).reshape(3)
        state = col(ray_weight)[0]
        np.testing.assert_allclose(state / state[0], expected(n, f, t, dop, angle, ratio), atol=3e-6)
        assert np.linalg.norm(value[1:]) <= value[0] + 3e-6

for bad in (dict(dop=1.01), dict(ellipticity=-1.01), dict(aolp=91), dict(dop=float('nan'))):
    try:
        mi.load_dict(dict(type='pollia_polarized', **bad))
    except RuntimeError:
        pass
    else:
        raise AssertionError('Invalid source accepted')

# The custom Cornell uses exactly the stock geometry and color values.
stock, custom = mi.cornell_box(), build_scene(mi, size=32)
for key in stock:
    if isinstance(stock[key], dict) and stock[key].get('type') in ('cube', 'rectangle'):
        assert str(stock[key]['to_world']) == str(custom[key]['to_world'])
for name in ('white', 'red', 'green'):
    assert custom[name]['type'] == 'pollia_jarecki'
    assert stock[name]['reflectance'] == custom[name]['reflectance']

# Check direct lighting against the stock Mitsuba Path integrator.
images = {}
for integrator in ('path', 'pollia_mueller', 'pollia_pis'):
    session.load_preset(size=32, max_depth=2, integrator=integrator,
                        source_dop=.8, source_aolp=31, source_ellipticity=-.4)
    images[integrator] = session.render(spp=128, seed=7, output='full').pixels
    assert np.isfinite(images[integrator]).all()
np.testing.assert_allclose(images['path'], images['pollia_mueller'], rtol=2e-4, atol=2e-5)
# Q/U/V may approach zero through cancellation. Bound their error relative
# to the corresponding S0, rather than relative to a vanishing component.
channel_count = 3 if 'rgb' in mi.variant() else 1
intensity = np.tile(np.abs(images['pollia_mueller'][..., :channel_count]), (1, 1, 5))
error = np.abs(images['pollia_pis'] - images['pollia_mueller'])
assert np.all(error <= 2e-4 * intensity + 2e-6), float(error.max())
print('Direct PIS/Mueller maximum error relative to S0:',
      float((error / (intensity + 1e-6)).max()))
# Pure depolarizers give deterministic equality through arbitrary bounces.
# A larger coherent mixture exercises Jones products and source adjoints.
for xi in (.25, 1.):
    differences = []
    for seed in range(12):
        values = []
        for integrator in ('pollia_mueller', 'pollia_pis'):
            description = build_scene(mi, size=32, max_depth=4, integrator=integrator,
                source_dop=.8, source_aolp=31, source_ellipticity=-.4)
            for name in ('white', 'red', 'green'):
                description[name]['xi0_max'] = xi
            session.load_dict(description)
            image = session.render(spp=32, seed=seed, output='full').pixels
            assert np.isfinite(image).all()
            values.append(image.mean(axis=(0, 1)))
        differences.append(values[1]-values[0])
    diffs = np.asarray(differences)
    if xi == .25:
        np.testing.assert_allclose(diffs, 0, atol=2e-6)
    else:
        assert np.all(np.abs(diffs.mean(axis=0)) <
            6*diffs.std(axis=0, ddof=1)/math.sqrt(len(diffs)) + 2e-5), diffs
# Source controls must influence transported Q/U/V, including a dark source.
session.load_preset(size=32, integrator='pollia_mueller', light_scale=0,
                    source_dop=1, source_ellipticity=1)
np.testing.assert_array_equal(session.render(spp=4, output='full').pixels, 0)

if 'rgb' in mi.variant():
    # RGB is transported in one render, with no leakage between channels.
    for integrator in ('pollia_mueller', 'pollia_pis'):
        description = build_scene(mi, size=32, max_depth=4, integrator=integrator,
            source_dop=.8, source_aolp=31, source_ellipticity=-.4)
        for name in ('white', 'red', 'green'):
            description[name]['xi0_max'] = 1.
        radiance = description['light']['emitter']['radiance']['value'].copy()
        session.load_dict(description)
        frame = session.render(spp=64, seed=9, output='full')
        assert frame.pixels.shape == (32, 32, 15)
        full = frame.pixels.copy()
        for channel, label in enumerate(('R', 'G', 'B')):
            description['light']['emitter']['radiance']['value'] = [
                value if i == channel else 0. for i, value in enumerate(radiance)]
            session.load_dict(description)
            isolated = session.render(spp=64, seed=9, output='full')
            for other, name in enumerate(('R', 'G', 'B')):
                indices = [isolated.channel_names.index(name)] + [
                    isolated.channel_names.index(f'S{k}.{name}') for k in range(4)]
                if other == channel:
                    np.testing.assert_allclose(isolated.pixels[..., indices],
                        full[..., indices], rtol=2e-4, atol=2e-5)
                else:
                    np.testing.assert_array_equal(isolated.pixels[..., indices], 0)
    # Exercise roulette early with colored throughput; all-channel termination
    # and maximum-channel survival must retain finite results.
    for integrator in ('pollia_mueller', 'pollia_pis'):
        session.load_preset(size=32, max_depth=6, rr_depth=1, integrator=integrator,
            source_dop=1., source_aolp=-18, source_ellipticity=.6)
        assert np.isfinite(session.render(spp=128, seed=3, output='full').pixels).all()
print(mi.variant(), 'polarized emitter / Cornell / transport checks passed')
