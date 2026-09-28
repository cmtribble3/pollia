# Pollia PIS capability probe

This branch tests whether Mitsuba's C++ and Dr.Jit extension points can carry
PolRAY's Jarecki material and reduced PIS transport on Apple Silicon. It is a
meeting prototype, not a paper benchmark or final integrator.

## Fixed setup

- Base: Mitsuba commit `fd9c9ceb38deb511a5c3f8479c8b14c0b552167e`
  (`3.10.0.dev1`); compare with Abhi's build only after matching his commit.
- Variant: `metal_ad_mono_polarized`; `scalar_mono_polarized` is available for
  debugging. RGB and spectral ports are separate extensions.
- Material: PolRAY's Jarecki Jones/depolarizer mixture with Lambertian scalar
  BRDF. Material properties are per BSDF; the prototype accesses them through
  Mitsuba's material attribute API.
- Baseline: matched C++ full Mueller integrator using the same restricted scene,
  Jarecki helper, material attribute calls, and sampling schedule. The stock
  Mitsuba path tracer with the Jarecki BSDF is a separate reference. Candidate:
  C++ PIS integrator using an eight-float polarization state overlaid between
  coherent Jones and post-depolarization states.
- Scope: diffuse surfaces, unpolarized area emission, no participating media,
  depth four, Russian roulette disabled. Both paths use the same geometry,
  camera, light, scalar BRDF, samples per pixel, and Stokes output wrapper.

## Sequence

1. Compile the clean source fork and confirm a polarized Metal render.
2. Port and check Jarecki's local response, frame rotations, normalization,
   and Stokes sign using existing PolRAY numerical references.
3. Render one scene with full Mueller and PIS; compare mean Stokes channels at
   increasing sample counts before interpreting performance.
4. Measure first render, first render after a parameter edit, and steady
   repeated renders, reporting median time and samples per second. Keep warmups
   and alternate integrator order.
5. Record state size, generated-kernel behavior if inspectable, build effort,
   implementation friction, and unsupported features alongside timings.

The first scene can use Mitsuba's built-in Cornell geometry. It is not the
matched 8,088-triangle scene from Abhi, so its results answer the within-Mitsuba
PIS question only. His scene can be substituted later without changing the
material or integrator plugins.

## Interpretation gates

- A build and correct render establish that the customization is possible.
- A smaller source-level loop state does not prove less GPU memory traffic; the
  compiled kernel and measurements must support that claim.
- A PIS speed ratio is meaningful only when both paths use the same material,
  sampling, output channels, and scene, and when quality differences are shown.
- The stock Stokes wrapper develops intensity twice; both within-Mitsuba paths
  share that output cost, but a later matched PolRAY test needs channel parity.
- A cross-engine ratio requires the same Mitsuba commit and scene configuration
  used by the PolRAY comparison.
