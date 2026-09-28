# Pollia integrator comparison for the decision meeting

Measured 2026-09-27 on a **MacBook Pro, Apple M4 Max** (Mac16,6; 16 CPU cores;
128 GB RAM). Pollia commit `2a4e2517` is based on Mitsuba `3.10.0.dev1`.
These are not Abhi's M5 or Mitsuba 3.9 measurements.

## What the three integrators are

- `path`: Mitsuba's existing general-purpose path tracer. It queries the new
  `pollia_jarecki` BSDF through Mitsuba's ordinary polarized BSDF methods. In
  this variant, `Spectrum` is a 4×4 Mueller matrix.
- `pollia_mueller`: a **new C++ Mitsuba control**, not a copy of PolRAY's Metal
  Mueller renderer. It uses the same Jarecki equations but reads material
  coefficients as attributes, computes scattering directly, tracks one 4×4
  polarization product and one scalar albedo throughput, and accumulates a
  four-element Stokes result. It supports only the Jarecki diffuse scene used
  in this probe. Its light/direction sampling and path logic were paired with
  `pollia_pis` to isolate transport representation.
- `pollia_pis`: the same restricted scene and path logic as `pollia_mueller`,
  with an eight-scalar Jones/depolarizer state instead of the 16-scalar Mueller
  product. It samples the coherent/depolarizing event, adding variance.

Thus the stock-to-custom Mueller difference is **integrator specialization**.
The custom Mueller-to-PIS difference is the closer test of PIS's transport
representation. The stock-to-PIS percentage combines both changes.

## Channel-corrected measurements

Each run is monochrome at 512×512, depth four, Russian roulette effectively
disabled (`rr_depth=100`), using the `metal_ad_mono_polarized` variant and the
stock Stokes output wrapper. The geometry is Mitsuba's built-in Cornell scene.
For each red, green, or blue run, the white/green/red surfaces use the matching
channel's Jarecki coefficients **and albedo** from PolRAY's `nylonWhite`,
`nylonGreen`, and `nylonRed40` presets. These are RGB model channels, **not
specified physical wavelengths**. All three integrators see identical inputs
within a channel. Two warmups preceded each run; integrator order rotated, and
GPU completion was synchronized before recording wall time.

Median milliseconds at **64 SPP**, 12 measured runs per integrator:

| RGB channel | Stock `path` | Custom Mueller | PIS | Mueller faster than stock | PIS faster than Mueller |
| --- | ---: | ---: | ---: | ---: | ---: |
| Red | 98.69 | 80.88 | 77.77 | 22.0% | 4.0% |
| Green | 98.65 | 79.17 | 77.19 | 24.6% | 2.6% |
| Blue | 99.02 | 79.25 | 76.89 | 25.0% | 3.1% |

Median milliseconds at **1,024 SPP**, six measured runs per integrator:

| RGB channel | Stock `path` | Custom Mueller | PIS | Mueller faster than stock | PIS faster than Mueller |
| --- | ---: | ---: | ---: | ---: | ---: |
| Red | 1,536.13 | 1,260.03 | 1,222.32 | 21.9% | 3.1% |
| Green | 1,584.63 | 1,304.14 | 1,272.00 | 21.5% | 2.5% |
| Blue | 1,600.30 | 1,344.48 | 1,280.63 | 19.0% | 5.0% |

Percentages are **throughput ratios** (`comparison time / candidate time - 1`),
not percentage decreases in elapsed time. For example, at 64 SPP in green,
stock-to-custom Mueller saves 19.48 ms, then custom Mueller-to-PIS saves 1.98
ms. About 91% of the total stock-to-PIS time reduction in that case comes
before changing to PIS. The combined PIS-versus-stock throughput gain is
roughly 25–29% across these runs; it should not be called a PIS-only gain.

## Why the stock path is slower here

The source code supports these concrete differences:

1. Stock `path` calls `BSDF::eval_pdf_sample` at each vertex. Our Jarecki BSDF
   uses its default implementation, which calls `eval_pdf` and `sample`; those
   evaluate the Jarecki Mueller response for the light and continuation
   directions. It also processes BSDF flags, samples, PDFs, null/delta event
   handling, eta, and its differentiable-rendering branch. The restricted
   control reads Jarecki attributes and directly uses its known cosine diffuse
   sampler, scalar BRDF, and Jarecki helper. Some stock branches can fold away
   for this scene; their individual cost has not been isolated.
2. Stock polarized `Spectrum` represents a Mueller matrix. Its generic loop
   multiplies the BSDF weight, including scalar albedo, into matrix throughput
   and accumulates a `Spectrum` result. The control keeps normalized Mueller
   transport and albedo throughput separate, and accumulates only a Stokes
   four-vector because the source is unpolarized. The compiler may eliminate
   unused stock outputs; register and memory costs have not been measured.
3. Stock transforms BSDF Mueller matrices from the surface frame using
   `to_world_mueller`. The control forms a world-basis Jones response and then
   converts it to a Mueller matrix. Both use the Jarecki helper but take
   different computational paths. Their isolated costs have not been measured.

For one green-channel 64-SPP render, Dr.Jit reported 4,193 IR operations in
the main stock Metal kernel, 2,131 in custom Mueller, and 2,252 in PIS. This
supports substantially more generated work in stock `path`; IR operation
counts are **not** GPU instruction counts, register counts, or a causal
breakdown of the 20–25% timing gap. Single kernel execution times fluctuate,
so the tables above use medians from repeated complete renders.

## Polarization and correctness limits

In this Jarecki model, `gamma` is the coherent-mixture weight: zero means an
ideal depolarizer and one means a Jones response. It depends on scattering
angle and complex refractive index as well as `xi0_max`. The white preset has
`xi0_max=0.26` in every RGB channel, while the green and red presets vary by
channel. These runs show the PIS speed advantage persists across all three
channel parameter sets, but albedo and other Jarecki coefficients also vary;
they do not isolate the effect of gamma alone.

| Material `xi0_max` | Red | Green | Blue |
| --- | ---: | ---: | ---: |
| White nylon | 0.26 | 0.26 | 0.26 |
| Green nylon | 0.71 | 0.43 | 0.54 |
| Red40 nylon | 0.33 | 1.00 | 0.75 |

These values are material parameters, not measured gamma values. In the port,
`gamma` is proportional to `(4*xi0_max - 1)/3` times a Fresnel term and is
clamped to `[0, 1]`.

At 128×128 and 1,024 SPP, the single-seed pixelwise PIS-minus-custom-Mueller
RMS differences relative to Mueller channel RMS signal were approximately:

| RGB channel | Q | U | V |
| --- | ---: | ---: | ---: |
| Red | 8.2% | 6.2% | 10.5% |
| Green | 5.4% | 3.7% | 5.2% |
| Blue | 7.1% | 5.1% | 8.2% |

Those are differences between noisy images, not bias or equal-error speed.
Mean Stokes values and all per-run timings are in `rgb-channel-64.json` and
`rgb-channel-1024.json`. A paper comparison still needs Abhi's exact 3.9
commit and geometry, output-channel parity with PolRAY, and multiple-seed
variance or equal-error measurements. The custom Mitsuba integrators currently
do not implement gradients, media, arbitrary BSDFs, or polarized emitters.
