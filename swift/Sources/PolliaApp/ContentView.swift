import SwiftUI
import PolliaRendering
import PolliaDisplay

struct ContentView: View {
    @Bindable var model: PolliaViewModel
    @State private var dragOrigin: (Double, Double)?
    var body: some View {
        HSplitView {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    sceneControls
                    estimateControls
                    displayControls
                    cameraControls
                    exportControls
                    if let error = model.errorMessage {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .font(.caption).foregroundStyle(.red)
                            .textSelection(.enabled)
                    }
                }.padding()
            }.frame(minWidth: 280, idealWidth: 330, maxWidth: 400)
            VStack(spacing: 12) {
                HStack {
                    if !model.imageTitle.isEmpty { Text(model.imageTitle).font(.callout) }
                    Text(model.status).font(.callout).foregroundStyle(.secondary)
                    Spacer()
                    if model.isRendering || model.isStarting { ProgressView().controlSize(.small) }
                }
                HStack(spacing: 16) {
                    viewport
                    if model.showColorBar, !model.display.filter.isRGB, model.surface != nil { colorBar }
                }
                if model.isRendering { ProgressView(value: model.progress) }
            }.padding().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .onChange(of: model.sceneChoice) { _, _ in model.choosePreset() }
        .onChange(of: model.display.filter) { _, _ in model.changeFilter() }
        .onChange(of: model.display) { _, _ in model.displayChanged() }
        .onChange(of: model.variant) { _, variant in
            if variant == .rgbPolarized, model.scene.integrator.requiresMono { model.scene.integrator = .path }
            model.display.filter = variant == .rgbPolarized ? .rgbIntensity : .intensity
            model.requestFresh()
        }
    }

    private var sceneControls: some View {
        GroupBox("Scene") {
            VStack(alignment: .leading, spacing: 10) {
                Picker("Scene", selection: $model.sceneChoice) {
                    ForEach(ScenePreset.allCases, id: \.rawValue) { Text($0.title).tag($0.rawValue) }
                    if let file = model.importedScene { Text(file.lastPathComponent).tag("file") }
                }
                Button("Open XML Scene…") { model.openScene() }
                Picker("Channels", selection: $model.variant) {
                    Text("RGB polarized").tag(MetalVariant.rgbPolarized)
                    Text("Monochrome polarized").tag(MetalVariant.monoPolarized)
                }
                Picker("Integrator", selection: $model.scene.integrator) {
                    ForEach(SceneIntegrator.allCases.filter { !$0.requiresMono || model.variant == .monoPolarized }, id: \.self) {
                        Text($0.title).tag($0)
                    }
                }.disabled(model.isFileScene)
                if model.isFileScene {
                    Text("Camera, lighting, integrator and image size come from this scene file.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Picker("Image size", selection: $model.scene.size) {
                        ForEach([128, 256, 512, 1024, 2048], id: \.self) { Text("\($0) × \($0)").tag($0) }
                    }
                    Stepper("Max depth: \(model.scene.maxDepth)", value: $model.scene.maxDepth, in: 1...64)
                    Stepper("RR starts at: \(model.scene.rrDepth)", value: $model.scene.rrDepth, in: 1...128)
                    Text(String(format: "Light scale: %.2f", model.scene.lightScale))
                    Slider(value: $model.scene.lightScale, in: 0...10)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }.disabled(model.isRendering || model.isStarting)
    }

    private var estimateControls: some View {
        GroupBox("Statistics") {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("Samples per estimate")
                    TextField("SPP", value: $model.estimateSPP, format: .number)
                        .frame(width: 70).textFieldStyle(.roundedBorder)
                }
                HStack {
                    Button("Fresh Estimate") { model.requestFresh() }.disabled(model.isStarting || model.isRendering)
                    Button("Continue") { model.render(fresh: false) }.disabled(!model.canContinue)
                    Button("Stop") { model.stop() }.disabled(!model.isRendering)
                }.controlSize(.small)
                metric("Completed", "\(model.completedSPP) SPP")
                if let frame = model.frame {
                    metric("Last batch", String(format: "%.2f ms", frame.metadata.renderMs))
                    metric("Image delivery", String(format: "%.2f ms", frame.sharedMs + frame.snapshotMs))
                    metric("Image", "\(frame.metadata.shape[1]) × \(frame.metadata.shape[0])")
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var displayControls: some View {
        GroupBox("Display") {
            VStack(alignment: .leading, spacing: 10) {
                Picker("Filter", selection: $model.display.filter) {
                    ForEach(model.availableFilters) { Text($0.title).tag($0) }
                }
                if !model.display.filter.isRGB, let channels = model.channels, channels.labels.count > 1 {
                    Picker("Display channel", selection: $model.display.channel) {
                        ForEach(Array(channels.labels.enumerated()), id: \.offset) { Text($0.element).tag($0.offset) }
                    }
                }
                if model.display.filter == .ellipticity {
                    Text("−1: left circular · 0: linear · +1: right circular")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if model.display.filter.usesExposure {
                    Text(String(format: "Exposure: %+.1f EV", model.display.exposureEV))
                    Slider(value: $model.display.exposureEV, in: -8...8)
                }
                if model.display.filter.usesAnalyzer {
                    Text(String(format: "Analyzer: %.1f°", model.display.analyzerAngle))
                    Slider(value: $model.display.analyzerAngle, in: -90...90)
                }
                if !model.display.filter.isRGB {
                    Picker("Color map", selection: $model.display.colorMap) {
                        ForEach(model.display.filter.availableColorMaps) { Text($0.title).tag($0) }
                    }
                    Toggle("Show colorbar", isOn: $model.showColorBar)
                    Toggle("Log scale", isOn: $model.display.logarithmic).disabled(!model.display.filter.supportsLog)
                    HStack {
                        Text("Range")
                        TextField("Minimum", value: $model.display.minimum, format: .number).textFieldStyle(.roundedBorder)
                        TextField("Maximum", value: $model.display.maximum, format: .number).textFieldStyle(.roundedBorder)
                    }
                }
                Text(String(format: "Display zoom: %.1f×", model.zoom))
                Slider(value: $model.zoom, in: 1...4)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var cameraControls: some View {
        GroupBox("Camera") {
            VStack(alignment: .leading, spacing: 10) {
                Text("Drag the image to orbit. Render a fresh estimate after editing camera controls.")
                    .font(.caption).foregroundStyle(.secondary)
                Text(String(format: "Yaw: %.1f°", model.scene.yaw))
                Slider(value: $model.scene.yaw, in: -180...180)
                Text(String(format: "Pitch: %.1f°", model.scene.pitch))
                Slider(value: $model.scene.pitch, in: -80...80)
                Text(String(format: "Camera distance: %.2f×", model.scene.distance))
                Slider(value: $model.scene.distance, in: 0.25...4)
                Button("Reset Camera") { model.resetCamera() }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }.disabled(model.isFileScene || model.isRendering)
    }

    private var exportControls: some View {
        GroupBox("Export") {
            HStack {
                Button("Save Image…") { model.exportImage() }
                Button("Save Channels…") { model.exportChannels() }
            }.controlSize(.small).frame(maxWidth: .infinity, alignment: .leading)
        }.disabled(model.surface == nil)
    }

    private var viewport: some View {
        ZStack {
            Color.black
            if let engine = model.engine, let surface = model.surface {
                MetalViewport(engine: engine, surface: surface)
                    .aspectRatio(CGFloat(surface.width) / CGFloat(surface.height), contentMode: .fit)
                    .scaleEffect(model.zoom)
                    .gesture(DragGesture(minimumDistance: 2)
                        .onChanged { drag in
                            guard !model.isFileScene else { return }
                            if dragOrigin == nil { dragOrigin = (model.scene.yaw, model.scene.pitch); model.stop() }
                            model.scene.yaw = dragOrigin!.0 - drag.translation.width * 0.2
                            model.scene.pitch = max(-80, min(80, dragOrigin!.1 + drag.translation.height * 0.2))
                        }
                        .onEnded { _ in
                            guard !model.isFileScene else { return }
                            dragOrigin = nil
                            model.requestFresh()
                        })
            } else {
                Text("Pollia").font(.title).foregroundStyle(.white.opacity(0.6))
            }
        }.clipped().frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var colorBar: some View {
        VStack(spacing: 6) {
            Text(model.display.filter.title).font(.caption2)
            Text(model.colorBarRange.upperBound, format: .number.precision(.significantDigits(4))).font(.caption2)
            LinearGradient(colors: colorBarColors, startPoint: .bottom, endPoint: .top).frame(width: 22)
            Text(model.colorBarRange.lowerBound, format: .number.precision(.significantDigits(4))).font(.caption2)
        }.frame(width: 70)
    }
    private var colorBarColors: [Color] {
        model.display.effectiveColorMap.colors.map {
            Color(.sRGB, red: Double($0.x), green: Double($0.y), blue: Double($0.z), opacity: 1)
        }
    }
    private func metric(_ label: String, _ value: String) -> some View {
        HStack { Text(label).foregroundStyle(.secondary); Spacer(); Text(value).monospacedDigit() }.font(.caption)
    }
}
