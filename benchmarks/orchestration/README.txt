Pollia Python/C++ orchestration comparison

Build without opening Xcode:
  cmake -S . -B "$HOME/Library/Developer/Xcode/DerivedData/pollia-runtime" -DPOLLIA_BUILD_BENCHMARKS=ON
  cmake --build "$HOME/Library/Developer/Xcode/DerivedData/pollia-runtime" --config RelWithDebInfo --target pollia-cpp-benchmark --parallel 8

Run from the repository root:
  .venv/bin/python benchmarks/orchestration/run.py

Three paths use the same XML, Metal RGB polarized variant, 12 host threads,
Stokes film, sample counts and seeds:
  cpp: native integrator render, then film development.
  python_direct: the same integrator/film API through Python bindings.
  python_public: mi.render, including its public wrapper/custom-operation setup.

Each fresh process retains one loaded scene for repeated renders, as a persistent
Python module or worker would. Timings separate initialization/imports, scene
loading, first render, warmed render calls and image readback. Host trace timing
may include early GPU submissions; evaluation/synchronization includes JIT work
and device waiting. Neither phase is a pure CPU/GPU split. A separate kernel
history pass records summed device durations outside the timed samples.

The first render is not a clean-cache measurement. Disk caches are retained.
Three process trials rotate API order to reduce cache and order bias. The runner
checks that all output arrays agree with the C++ output at the same seed. Raw
float32 arrays and every timing sample are retained beside the JSON report.

These are forward-rendering API benchmarks, not a Swift UI benchmark, IPC test,
or scientific validation of custom Pollia integrators. The simple stock scene
exercises host orchestration and rendering at three scales. Large research
scenes, scene edits, worker IPC/image transport, embedded Python and automatic
differentiation need their own measurements before making claims about them.
Do not launch Xcode for these benchmarks.

Persistent workers and image transport
  cmake -S . -B "$HOME/Library/Developer/Xcode/DerivedData/pollia-runtime" -DPOLLIA_BUILD_BENCHMARKS=ON
  cmake --build "$HOME/Library/Developer/Xcode/DerivedData/pollia-runtime" --config RelWithDebInfo --target pollia-cpp-worker --parallel 8
  .venv/bin/python benchmarks/orchestration/transport.py

transport.py compares a native worker, Python mi.render with np.asarray readback,
and Python mi.render with np.array readback. These use the same command protocol,
loaded scene, thread count, seeds and payload format. A native host view and a
NumPy view retain their owners until transfer completes. RGB preview extraction
packs the first three channels from the 15-channel film; full polarization output
retains all 15 float32 channels. Neither payload is tone-mapped or converted to a
UI display format.

Measures:
  ping: command/response latency with no rendering or image data.
  send_pipe/send_shared: transfer of a cached frame, with no rendering/readback.
  none: completed Metal render, with no host readback.
  local: render and readback/packing inside the worker, with no image sent back.
  pipe: render/readback followed by binary image bytes over stdout.
  shared: render/readback followed by a copy into a shared mmap and a notification.

Parent round-trip time includes commands, response parsing and pipe reads. The
shared-memory endpoint returns a borrowed view after the completion notification;
it remains valid until the next request. A parent-owned snapshot requires another
copy, whose cost is measured separately. The mapped file is temporary, uses
MAP_SHARED and needs no msync/disk write per frame. All receivers must coordinate
buffer reuse; this test runs one synchronous request at a time.

Inner native render+readback timing excludes IPC and packing, and estimates the
cost of an in-process C++ API returning host-resident data. It is not a measured
Swift bridge. GPU uploads, SwiftUI/AppKit presentation, an embedded Python host,
and direct Metal-buffer/texture interoperability remain outside this harness.

Default matrix: 256x256/16spp, 512x512/32spp, 1024x1024/4spp; 3 and 15 channels;
3 process trials per path; 12 measured requests per mode, 3 warmups, 100 pings.
Worker order and transport order rotate. All samples, raw comparison frames,
readback ownership flags, hashes and process stderr are retained with the report.

Equivalent GPU preview packing
  .venv/bin/python benchmarks/orchestration/transport.py --preview-pack gpu --channels 3 --languages cpp,python_view --output benchmarks/results/2026-10-08/transport-gpu-preview.json

This gathers RGB from the full Stokes tensor on Metal before host readback.
Both languages use the same index expression. GPU packing and synchronization
are counted in pack_ms, not render_ms or readback_ms. The worker's prepared-frame
timing includes render, packing and readback. The CPU packing comparison and
full-image results remain in transport-optimized.json. Python's optimized full
readback is a NumPy view; the owned-copy path is kept as a separate baseline.
Borrowed views require keeping the image/buffer owner alive until consumption
finishes. A production worker needs buffer reuse/ownership coordination.
