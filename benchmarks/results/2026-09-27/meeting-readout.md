# Pollia PIS port: preliminary meeting readout

Date: 2026-09-27 (America/Phoenix). These are feasibility results, not a paper
benchmark. Pollia started from Mitsuba commit `fd9c9ceb38deb511a5c3f8479c8b14c0b552167e`
(`3.10.0.dev1`) on the `pis-port` branch. Abhi's Mitsuba commit and matched mesh
scene were not yet available.

## What was implemented

- `pollia_jarecki`: a C++ BSDF with PolRAY's normalized Jarecki Jones plus
  ideal-depolarizer response and a separate Lambertian scalar BRDF. It exposes
  per-material parameters through Mitsuba's BSDF attribute interface.
- `pollia_pis`: a C++ integrator with an eight-scalar polarization loop state.
  The same two four-vectors hold a Jones product before the first sampled
  depolarizer, then a frozen camera Stokes column and source adjoint row.
- `pollia_mueller`: a matching C++ full-Mueller control. It uses the same
  Jarecki helper, scalar BRDF, light and direction samples, MIS, and Russian
  roulette schedule. Its loop carries sixteen polarization scalars.
- Both integrators return a first Mueller column to Mitsuba's stock Stokes
  wrapper. The wrapper develops duplicate intensity, so the film has five
  monochrome channels. This cost is shared in the within-Mitsuba comparison.

All three plugins compiled and rendered in `metal_ad_mono_polarized` on an Apple
M5. The Jarecki BSDF's normalized Mueller matrix matched 15 PolRAY reference
cases with maximum absolute entry error `4.31e-7`, including Stokes V and basis
rotation cases. An initial 16×16, 4,096-SPP scene had mean intensity within
`0.028%` between stock path and PIS; this is a convergence check, not an
equal-error result.

## Paired speed observations

All timings below used Mitsuba's built-in Cornell scene, 512×512 monochrome,
unpolarized area emission, Russian roulette disabled, and alternating order
after warmups. Entries are median wall time per render. The matched control
is the relevant test of PIS state cost. Detailed per-run times and parameters
are in the JSON files beside this report.

| Material / depth / SPP | Full Mueller | PIS | PIS speedup |
| --- | ---: | ---: | ---: |
| Nylon presets / 4 / 64 | 78.97 ms | 77.28 ms | 2.18% |
| Nylon presets / 4 / 1,024 | 1,255.56 ms | 1,226.86 ms | 2.34% |
| Coherent-leaning stress preset / 4 / 64 | 73.51 ms | 71.27 ms | 3.13% |
| Nylon presets / 8 / 64 | 147.05 ms | 142.20 ms | 3.41% |

As a separate reference, Mitsuba's **stock** path integrator with the Jarecki
BSDF took 99.04 ms in the nylon/depth-four/64-SPP setup, while PIS took
77.71 ms (27.45% faster). That comparison includes the effect of specializing
the entire integrator; it does **not** isolate the PIS representation.

Dr.Jit's Metal history for one 64-SPP depth-four render reported about 77.78 ms
for the matched Mueller main kernel and 75.84 ms for PIS. The PIS kernel had
2,252 low-level IR operations versus 2,131 for Mueller. Thus the smaller
source-level state has not yielded a large speed gain here; the state machine
adds work. These operation counts do not establish register allocation or
memory traffic.

## Output and latency observations

At 256 SPP in the nylon scene, pixelwise RMS PIS-minus-Mueller differences
relative to each full-Mueller channel's RMS signal were approximately 0.001%
for I, 3.8% for Q, 2.7% for U, and 5.1% for V. These differences include
Monte Carlo variance and cannot establish bias or equal-error speed. The
stress preset gave about 1.8% for Q, 2.4% for U, and 14% for V; its V signal
was tiny compared with I. This preset was intended to exercise coherent
transport; it does not establish a large degree of polarization in the image.

After editing a material reflectance value, the next-frame time was close to
steady time: about 80/77 ms for Mueller/PIS at 64 SPP in the nylon scene. The
first render of each integrator varied with process and cache state, and the
benchmark always loaded Mueller first. No defensible cold-start comparison is
available yet.

## Interpretation for the engine decision

This port establishes that Mitsuba's C++/Dr.Jit path can carry the Jarecki
material, per-material parameters, and a reduced PIS transport state on Metal
without modifying Mitsuba's core BSDF interface. It also exposes the current
friction: PIS gets Jones/depolarizer data through material attributes because
Mitsuba's ordinary polarized BSDF calls return Mueller matrices. A general
material system would need a cleaner dedicated interface or continued
specialization of the PIS integrator.

The matched result is a repeatable but small 2–3% gain at depth four, below
PolRAY's reported ~15%. The much larger gap to stock Mitsuba path reflects
integrator specialization as well as polarization transport. This is evidence
that a Mitsuba backend is technically workable; it is not enough by itself to
choose the long-term engine. The next decisive comparison needs Abhi's exact
commit and matched triangle scene, plus output-channel parity and equal-error
analysis.

Prototype limits: monochrome polarized only; diffuse `pollia_jarecki` surfaces
only; unpolarized emitters; no media, general materials, or gradients. Although
the variant is AD-capable, the custom integrators do not implement derivatives.
Xcode project generation and a Release build of `pollia_pis` now succeed with
the CMake workarounds in this branch.
