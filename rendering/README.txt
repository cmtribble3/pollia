Pollia rendering

Architecture selected: persistent Python worker, shared-memory image delivery.
Scene construction and rendering live in pollia/session.py. The app-facing JSON
protocol lives in pollia/worker.py. Swift's asynchronous client is in
swift/Sources/PolliaRendering. No Objective-C bridge or embedded interpreter is
required for this interface. Ocular rendering remains in PolRAY.

Use from Python
  source scripts/pollia-env.sh
  export PYTHONPATH="$PWD/rendering:$PYTHONPATH"
  python

  from pollia import RenderSession
  session = RenderSession()  # metal_rgb_polarized; no CPU fallback
  session.load_file("benchmarks/orchestration/scene.xml", size="512")
  frame = session.render(spp=32, seed=7, output="full")
  frame.pixels         # contiguous H x W x C float32 NumPy array
  frame.channel_names  # film channels, followed by integrator AOV names

load_dict() also accepts native Mitsuba dictionaries and Python plugin objects.
The scene remains available between renders. Failed loads preserve the old scene.
Variants are fixed for the interpreter's lifetime; use a new worker to switch.
RGB previews gather the first R/G/B channels on Metal before host readback.
Full output retains every channel, including Stokes AOVs. Mono uses
metal_ad_mono_polarized and full output; an RGB preview request is rejected.

Use from Swift
  Add the local swift package as a dependency of the future Pollia app target.
  Import PolliaRendering. Construct WorkerConfiguration with absolute paths to:
    python: .venv/bin/python
    renderingDirectory: this repository's rendering directory
    backendPythonDirectory: the active build's python directory
  await PythonRenderSession.connect(configuration)
  await session.loadScene(sceneURL, parameters: ["size": "512"])
  await session.render(spp: 32, seed: 7, output: .full)
  await session.shutdown()
  Calls above can throw; use try await for connect/load/render.

The client starts one worker, serializes requests on a background Dispatch queue,
and grows a private memory-mapped buffer as needed (default maximum 1 GiB).
It returns owned Data snapshots after copying the completed shared frame.
This deliberate host copy is included in snapshotMs; it prevents overwrite while
the UI retains a frame. The protocol pipe carries only commands and metadata.
Pixels are linear, interleaved, little-endian float32, not a tonemapped UI image.
Tone mapping and display texture upload will be added with the UI port.

Worker protocol v1
  Startup: {event: ready, protocol: 1, variant: ...}
  Requests: JSON lines with positive integer id and command.
  Replies: matching id, ok, and result or error {type, message}.
  load: path and optional parameters object.
  render: spp, seed, sensor and output (preview/full). Returns generation,
    shape, channel_names, bytes, dtype, Metal kernel_count and timing metadata.
  publish: generation and shared-file capacity. Copies that frame into the
    startup --shared-file and acknowledges completion. Read it after this reply.
  ping: backend identity.
  shutdown: acknowledge, close mappings and exit.
  Scene/argument errors are recoverable; fatal Metal errors end the worker.

Only one frame transaction is in flight. The host must finish reading/copying a
published frame before issuing another request or resizing the backing file.
No buffer flush or file serialization is performed for image delivery.
Python and native plugin diagnostics are routed to stderr; Swift drains and
retains the last 32 KiB for connection errors.

Lifecycle and limits
  A native crash, protocol failure or timeout invalidates the Swift session.
  Reconnect and reload the scene to recover; there is no silent CPU substitution
  or automatic retry of a render. Default timeout is 120 seconds per pipe
  operation and is configurable for longer renders.
  shutdown is ordered after active work. Swift Task cancellation does not yet
  interrupt a native render. Interactive cancellation/progressive batching belongs
  to the next UI integration step.
  The current launch configuration uses the development runtime. Distributing
  the app will require bundling/signing Python and native dependencies.

Verification without opening Xcode
  source scripts/pollia-env.sh
  export PYTHONPATH="$PWD/rendering:$PYTHONPATH"
  python -m unittest discover -s rendering/tests -v
  swift test --package-path swift
  swift run --package-path swift pollia-worker-check "$PWD" "$POLLIA_BUILD_DIR"

Real Metal checks compare worker output with direct Python rendering, preview
with full RGB channels, and exercise custom mono PIS, buffer resizing, failed
scene loads, CPU rejection, snapshot ownership, native crash detection/restart.
Swift contract checks reject corrupt frame metadata and stop a stalled worker.
Render comparisons allow small GPU floating-point reduction differences.
These are correctness checks, not a new Swift integration performance benchmark.
