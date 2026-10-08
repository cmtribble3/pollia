import Foundation
import Metal
import PolliaRendering
import PolliaDisplay

@main
struct DisplayCheck {
    static func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw WorkerError.backend(message) }
    }
    static func main() async throws {
        let engine = try MetalDisplayEngine()
        let names = ["R", "G", "B"] + (0..<4).flatMap { ["S\($0).R", "S\($0).G", "S\($0).B"] }
        var values = [Float]()
        for stokes: [Float] in [[1, 0.6, 0.8, 0], [1, 0, 0, 1], [0, 0, 0, 0]] {
            values.append(contentsOf: [stokes[0], stokes[0], stokes[0]])
            for value in stokes { values.append(contentsOf: [value, value, value]) }
        }
        let fields: [String: Any] = ["generation": 1, "shape": [1, 3, 15], "channel_names": names,
                                    "bytes": values.count * 4, "dtype": "float32-le", "variant": "metal_rgb_polarized",
                                    "render_ms": 0, "readback_ms": 0, "kernel_count": 1]
        let metadata = try JSONDecoder().decode(FrameMetadata.self, from: JSONSerialization.data(withJSONObject: fields))
        let fixture = RenderFrame(metadata: metadata, pixels: values.withUnsafeBytes { Data($0) })
        var settings = DisplaySettings()
        let cases: [(DisplayFilter, Float, Float)] = [(.intensity, 1, 1), (.dop, 1, 1),
                                                    (.aolp, 26.56505, .nan), (.ellipticity, 0, 1),
                                                    (.q, 0.6, 0), (.u, 0.8, 0), (.v, 0, 1),
                                                    (.analyzer, 0.8, 0.5)]
        for (index, test) in cases.enumerated() {
            settings.filter = test.0
            settings.minimum = test.0.defaultRange.lowerBound
            settings.maximum = test.0.defaultRange.upperBound
            let image = try await engine.display(frame: index == 0 ? fixture : nil, settings: settings)
            let scalar = image.scalarValues.contents().bindMemory(to: Float.self, capacity: 3)
            try require(abs(scalar[0] - test.1) < 2e-5 &&
                        (test.2.isNaN ? scalar[1].isNaN : abs(scalar[1] - test.2) < 2e-5),
                        "Wrong \(test.0.title) display values: \(scalar[0]), \(scalar[1]); expected \(test.1), \(test.2)")
            if test.0 == .intensity || test.0 == .analyzer {
                try require(scalar[2] == 0, "Dark intensity and analyzer pixels must remain zero")
            } else {
                try require(scalar[2].isNaN, "Dark pixels must be undefined for polarization ratios/angles")
            }
        }
        try require(DisplayFilter.aolp.title == "AoLP [°]" &&
                    DisplayFilter.ellipticity.title == "Ellipticity" &&
                    DisplayFilter.ellipticity.defaultRange == -1...1,
                    "Polarization labels or ellipticity bounds are incorrect")
        try require(DisplayFilter.aolp.defaultColorMap == .phase &&
                    DisplayFilter.ellipticity.defaultColorMap == .viridis &&
                    [.q, .u, .v].allSatisfy { (filter: DisplayFilter) in
                        filter.defaultColorMap == .diverging && filter.availableColorMaps.contains(.seismic)
                    } &&
                    !DisplayFilter.ellipticity.availableColorMaps.contains(.seismic),
                    "PolRAY defaults or seismic availability are incorrect")

        func frame(_ samples: [[Float]]) throws -> RenderFrame {
            var pixels = [Float]()
            for sample in samples {
                pixels.append(contentsOf: [sample[0], sample[0], sample[0]])
                for value in sample { pixels.append(contentsOf: [value, value, value]) }
            }
            var sampleFields: [String: Any] = fields
            sampleFields["shape"] = [1, samples.count, 15]
            sampleFields["bytes"] = pixels.count * 4
            let metadata = try JSONDecoder().decode(FrameMetadata.self, from: JSONSerialization.data(withJSONObject: sampleFields))
            return RenderFrame(metadata: metadata, pixels: pixels.withUnsafeBytes { Data($0) })
        }

        // Circular endpoints, elliptical states, linear/unpolarized/dark states,
        // partial polarization and large values cover the ratio and handedness.
        let ellipseFrame = try frame([[1, 0, 0, 1], [1, 0, 0, -1],
                                      [1, 0.6, 0, 0.8], [1, 0, 0.6, -0.8],
                                      [1, 1, 0, 0], [1, 0, 0, 0], [0, 0, 0, 0],
                                      [1, 0.3, 0, 0.4], [1e30, 6e29, 0, 8e29]])
        settings.filter = .ellipticity
        settings.colorMap = .viridis
        settings.minimum = -1
        settings.maximum = 1
        let ellipse = try await engine.display(frame: ellipseFrame, settings: settings)
        let ratios = ellipse.scalarValues.contents().bindMemory(to: Float.self, capacity: 9)
        let expected: [Float] = [1, -1, 0.5, -0.5, 0, .nan, .nan, 0.5, 0.5]
        for i in expected.indices {
            try require(expected[i].isNaN ? ratios[i].isNaN : abs(ratios[i] - expected[i]) < 2e-5,
                        "Wrong ellipticity ratio at pixel \(i): \(ratios[i])")
        }

        let ramp = try frame((0...256).map { [1, 2 * Float($0) / 256 - 1, 0, 0] })
        settings.filter = .q
        for palette in DisplayColorMap.allCases {
            settings.colorMap = palette
            let surface = try await engine.display(frame: ramp, settings: settings)
            var bytes = [UInt8](repeating: 0, count: 257 * 4)
            surface.texture.getBytes(&bytes, bytesPerRow: bytes.count,
                                     from: MTLRegionMake2D(0, 0, 257, 1), mipmapLevel: 0)
            for pixel in 0...256 {
                let color = palette.color(at: Float(pixel) / 256)
                for channel in 0..<3 {
                    try require(abs(Float(bytes[pixel * 4 + channel]) / 255 - color[channel]) <= 1.1 / 255,
                                "Image/colorbar mismatch for \(palette.title), pixel \(pixel), channel \(channel)")
                }
            }
        }
        print("PASS: signed ellipticity [-1, 1], handedness, all imported palettes, seismic and colorbar/image parity")
        settings = DisplaySettings()
        _ = try await engine.display(frame: fixture, settings: settings)
        settings.filter = .analyzer
        settings.analyzerAngle = 90
        let crossed = try await engine.display(settings: settings)
        try require(abs(crossed.scalarValues.contents().load(as: Float.self) - 0.2) < 2e-6,
                    "Crossed analyzer violates Malus/Stokes projection")
        let png = try crossed.pngData()
        try require(png.prefix(8) == Data([137, 80, 78, 71, 13, 10, 26, 10]), "Invalid PNG export")
        print("PASS: Metal display I, DoP, AoLP, ellipticity, Q/I, U/I, V/I, analyzer projection, dark pixels and PNG export")

        if CommandLine.arguments.count >= 3 {
            let root = URL(fileURLWithPath: CommandLine.arguments[1])
            let build = URL(fileURLWithPath: CommandLine.arguments[2])
            let configuration = WorkerConfiguration(python: root.appendingPathComponent(".venv/bin/python"),
                                                    renderingDirectory: root.appendingPathComponent("rendering"),
                                                    backendPythonDirectory: build.appendingPathComponent("python"))
            let worker = try await PythonRenderSession.connect(configuration)
            do {
                var scene = SceneSettings(); scene.size = 64
                try await worker.loadPreset(scene)
                _ = try await worker.render(spp: 4, output: .full)
                let accumulated = try await worker.render(spp: 4, seed: 1, output: .full, accumulate: true)
                try require(accumulated.metadata.sampleCount == 8, "Progressive sample count mismatch")
                settings = DisplaySettings()
                let image = try await engine.display(frame: accumulated, settings: settings)
                if CommandLine.arguments.count == 4 {
                    try image.pngData().write(to: URL(fileURLWithPath: CommandLine.arguments[3]))
                }
                await worker.shutdown()
                print("PASS: Python Cornell preset → progressive Metal rendering → Metal display → PNG")
            } catch {
                await worker.shutdown()
                throw error
            }
        }
    }
}
