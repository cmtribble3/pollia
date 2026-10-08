import AppKit
import Foundation
import Observation
import UniformTypeIdentifiers
import PolliaRendering
import PolliaDisplay

private struct RuntimePaths: Decodable {
    let python: String
    let rendering: String
    let backend: String

    static func load() throws -> RuntimePaths {
        if let file = Bundle.main.url(forResource: "PolliaRuntime", withExtension: "plist") {
            return try PropertyListDecoder().decode(Self.self, from: Data(contentsOf: file))
        }
        let environment = ProcessInfo.processInfo.environment
        guard let root = environment["POLLIA_ROOT"], let build = environment["POLLIA_BUILD_DIR"] else {
            throw WorkerError.unavailable("Pollia's rendering environment is missing. Build the app with scripts/build-pollia-app.sh.")
        }
        return Self(python: root + "/.venv/bin/python", rendering: root + "/rendering", backend: build + "/python")
    }
    func configuration(variant: MetalVariant) -> WorkerConfiguration {
        WorkerConfiguration(python: URL(fileURLWithPath: python), renderingDirectory: URL(fileURLWithPath: rendering),
                            backendPythonDirectory: URL(fileURLWithPath: backend), variant: variant)
    }
}

@Observable @MainActor
final class PolliaViewModel {
    var scene = SceneSettings()
    var sceneChoice = ScenePreset.cornell.rawValue
    var variant: MetalVariant = .rgbPolarized
    var estimateSPP = 64
    var display = DisplaySettings()
    var showColorBar = true
    var zoom = 1.0
    private(set) var engine: MetalDisplayEngine?
    private(set) var surface: DisplaySurface?
    private(set) var frame: RenderFrame?
    private(set) var channels: StokesChannels?
    private(set) var importedScene: URL?
    private(set) var isRendering = false
    private(set) var isStarting = false
    private(set) var completedSPP = 0
    private(set) var progress = 0.0
    private(set) var status = "Starting Metal backend…"
    private(set) var imageTitle = ""
    var errorMessage: String?
    @ObservationIgnored private var paths: RuntimePaths?
    @ObservationIgnored private var worker: PythonRenderSession?
    @ObservationIgnored private var workerVariant: MetalVariant?
    @ObservationIgnored private var renderTask: Task<Void, Never>?
    @ObservationIgnored private var loadedScene: SceneSettings?
    @ObservationIgnored private var loadedFile: URL?
    @ObservationIgnored private var seed: UInt32 = 0
    @ObservationIgnored private var displayRevision = 0
    @ObservationIgnored private var pendingFresh = false
    @ObservationIgnored private var sampleHistory: [[String: Int]] = []
    @ObservationIgnored private var imageSettings: SceneSettings?
    @ObservationIgnored private var imageFile: URL?
    @ObservationIgnored private var imageSampleHistory: [[String: Int]] = []
    @ObservationIgnored private var displayErrorActive = false

    var isFileScene: Bool { sceneChoice == "file" }
    var canContinue: Bool {
        !isRendering && !isStarting && completedSPP > 0 && workerVariant == variant &&
        (isFileScene ? loadedFile == importedScene && loadedFile != nil : loadedFile == nil && loadedScene == scene)
    }
    var availableFilters: [DisplayFilter] {
        DisplayFilter.allCases.filter { filter in
            (!filter.isRGB || channels?.isRGB != false) &&
            (channels?.hasPolarization != false || filter == .rgbIntensity || filter == .intensity)
        }
    }
    var colorBarRange: ClosedRange<Double> {
        let gain = display.filter.usesExposure ? pow(2, display.exposureEV) : 1
        guard display.minimum.isFinite, display.maximum.isFinite, display.maximum > display.minimum,
              gain.isFinite, gain > 0 else { return display.filter.defaultRange }
        return (display.minimum / gain)...(display.maximum / gain)
    }

    func startup() async {
        guard !isStarting, engine == nil else { return }
        isStarting = true
        defer { isStarting = false }
        do {
            paths = try RuntimePaths.load()
            engine = try MetalDisplayEngine()
            status = "Ready"
            requestFresh()
        } catch { errorMessage = String(describing: error); status = "Unable to start" }
    }

    /// Optional command-line verification of the real app window and drawable.
    /// Ordinary launches do not write diagnostic files or exit automatically.
    func verifyLaunchIfRequested() async {
        let args = CommandLine.arguments
        guard let index = args.firstIndex(of: "--verify-report"), args.indices.contains(index + 1) else { return }
        let deadline = ProcessInfo.processInfo.systemUptime + 30
        while errorMessage == nil && (isRendering || surface == nil || engine?.presentedFrameCount == 0) &&
                ProcessInfo.processInfo.systemUptime < deadline {
            try? await Task.sleep(for: .milliseconds(50))
        }
        let success = errorMessage == nil && !isRendering && completedSPP == estimateSPP &&
            surface != nil && (engine?.presentedFrameCount ?? 0) > 0
        let report: [String: Any] = ["passed": success, "status": status,
                                    "error": errorMessage ?? "", "samples": completedSPP,
                                    "shape": frame?.metadata.shape ?? [],
                                    "variant": frame?.metadata.variant ?? "",
                                    "presented_frames": engine?.presentedFrameCount ?? 0,
                                    "device": engine?.device.name ?? ""]
        do {
            try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
                .write(to: URL(fileURLWithPath: args[index + 1]), options: .atomic)
        } catch { print("Pollia launch verification: \(error)") }
        if args.contains("--exit-after-check") {
            if let worker { await worker.shutdown() }
            NSApplication.shared.terminate(nil)
        }
    }

    func choosePreset() {
        if let preset = ScenePreset(rawValue: sceneChoice) { scene.preset = preset }
        requestFresh()
    }

    func requestFresh() {
        if isRendering { pendingFresh = true; stop(discardPending: false); return }
        render(fresh: true)
    }

    func render(fresh: Bool) {
        guard !isRendering, let paths, engine != nil else { return }
        guard fresh || canContinue else { return }
        let settings = scene, file = isFileScene ? importedScene : nil, selectedVariant = variant
        let requested = max(1, min(4096, estimateSPP))
        isRendering = true
        progress = 0
        errorMessage = nil
        displayErrorActive = false
        status = fresh ? "Loading scene…" : "Rendering…"
        renderTask = Task { [weak self] in
            guard let self else { return }
            defer {
                isRendering = false
                renderTask = nil
                if pendingFresh {
                    pendingFresh = false
                    requestFresh()
                }
            }
            do {
                if worker == nil || workerVariant != selectedVariant {
                    if let worker { await worker.shutdown() }
                    worker = nil
                    worker = try await PythonRenderSession.connect(paths.configuration(variant: selectedVariant))
                    workerVariant = selectedVariant
                }
                guard let worker else { throw WorkerError.closed }
                try Task.checkCancellation()
                if fresh {
                    if let file { try await worker.loadScene(file) }
                    else { try await worker.loadPreset(settings) }
                    loadedScene = settings
                    loadedFile = file
                    frame = nil
                    surface = nil
                    imageTitle = ""
                    completedSPP = 0
                    seed = 0
                    sampleHistory = []
                }
                try Task.checkCancellation()
                var rendered = 0
                while rendered < requested {
                    let batch = min(8, requested - rendered)
                    status = "Rendering…"
                    let next = try await worker.render(spp: UInt32(batch), seed: seed, output: .full,
                                                       accumulate: !fresh || rendered > 0)
                    sampleHistory.append(["seed": Int(seed), "spp": batch])
                    seed &+= 1
                    rendered += batch
                    completedSPP = next.metadata.sampleCount ?? (completedSPP + batch)
                    progress = Double(rendered) / Double(requested)
                    frame = next
                    imageTitle = file?.lastPathComponent ?? settings.preset.title
                    imageFile = file
                    imageSettings = file == nil ? settings : nil
                    imageSampleHistory = sampleHistory
                    channels = try StokesChannels(names: next.metadata.channelNames)
                    if !availableFilters.contains(display.filter) {
                        display.filter = channels?.isRGB == true ? .rgbIntensity : .intensity
                    }
                    display.channel = min(display.channel, (channels?.labels.count ?? 1) - 1)
                    await refreshDisplay(newFrame: next)
                    try Task.checkCancellation()
                }
                status = "Ready · \(completedSPP) SPP"
            } catch is CancellationError {
                status = "Stopped · \(completedSPP) SPP"
            } catch {
                errorMessage = String(describing: error)
                displayErrorActive = false
                status = "Render failed"
                loadedScene = nil
                loadedFile = nil
                if case WorkerError.backend = error {} else {
                    if let worker { await worker.shutdown() }
                    worker = nil
                    workerVariant = nil
                }
            }
        }
    }

    func stop(discardPending: Bool = true) {
        if discardPending { pendingFresh = false }
        renderTask?.cancel()
        if isRendering { status = "Stopping after this sample batch…" }
    }

    func changeFilter() {
        let range = display.filter.defaultRange
        display.minimum = range.lowerBound
        display.maximum = range.upperBound
        if !display.filter.supportsLog { display.logarithmic = false }
    }

    func displayChanged() {
        guard frame != nil else { return }
        Task { await refreshDisplay() }
    }

    private func refreshDisplay(newFrame: RenderFrame? = nil) async {
        guard let engine else { return }
        displayRevision += 1
        let revision = displayRevision
        do {
            let image = try await engine.display(frame: newFrame, settings: display)
            if revision == displayRevision {
                surface = image
                if displayErrorActive { errorMessage = nil; displayErrorActive = false }
            }
        } catch {
            if revision == displayRevision { errorMessage = String(describing: error); displayErrorActive = true }
        }
    }

    func resetCamera() {
        scene.yaw = 0; scene.pitch = 0; scene.distance = 1; zoom = 1
        requestFresh()
    }

    func openScene() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.xml]
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let file = panel.url else { return }
        let wasFile = isFileScene
        importedScene = file
        sceneChoice = "file"
        if wasFile { requestFresh() } // Otherwise the scene picker change starts it.
    }

    func exportImage() {
        guard let surface else { return }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.png]
        panel.nameFieldStringValue = "pollia.png"
        guard panel.runModal() == .OK, let file = panel.url else { return }
        Task {
            do {
                let data = try await Task.detached { try surface.pngData() }.value
                try data.write(to: file, options: .atomic)
                status = "Saved \(file.lastPathComponent)"
            } catch { errorMessage = String(describing: error) }
        }
    }

    func exportChannels() {
        guard let frame else { return }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "pollia-channels.f32"
        guard panel.runModal() == .OK, let file = panel.url else { return }
        do {
            try frame.pixels.write(to: file, options: .atomic)
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            var metadata = try JSONSerialization.jsonObject(with: encoder.encode(frame.metadata)) as! [String: Any]
            metadata["render_batches"] = imageSampleHistory
            if let imageFile { metadata["scene_file"] = imageFile.path }
            if let imageSettings {
                metadata["scene_settings"] = try JSONSerialization.jsonObject(with: encoder.encode(imageSettings))
            }
            try JSONSerialization.data(withJSONObject: metadata, options: [.prettyPrinted, .sortedKeys])
                .write(to: file.appendingPathExtension("json"), options: .atomic)
            status = "Saved channels and metadata"
        } catch { errorMessage = String(describing: error) }
    }
}
