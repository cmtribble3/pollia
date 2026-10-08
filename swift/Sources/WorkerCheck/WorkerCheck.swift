import Foundation
import Darwin
import PolliaRendering

@main
struct WorkerCheck {
    static func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw WorkerError.protocolFailure(message) }
    }

    static func floats(_ frame: RenderFrame) -> [Float] {
        frame.pixels.withUnsafeBytes { bytes in
            stride(from: 0, to: bytes.count, by: 4).map { offset in
                Float(bitPattern: UInt32(littleEndian: bytes.loadUnaligned(fromByteOffset: offset, as: UInt32.self)))
            }
        }
    }

    static func main() async throws {
        guard CommandLine.arguments.count == 3 else {
            throw WorkerError.unavailable("Usage: pollia-worker-check REPOSITORY BACKEND_BUILD")
        }
        let root = URL(fileURLWithPath: CommandLine.arguments[1])
        let build = URL(fileURLWithPath: CommandLine.arguments[2])
        let config = WorkerConfiguration(python: root.appendingPathComponent(".venv/bin/python"),
                                         renderingDirectory: root.appendingPathComponent("rendering"),
                                         backendPythonDirectory: build.appendingPathComponent("python"))
        let session = try await PythonRenderSession.connect(config)
        do {
            let pid = session.processID
            let scene = root.appendingPathComponent("benchmarks/orchestration/scene.xml")
            try await session.loadScene(scene, parameters: ["size": "32"])
            let preview = try await session.render(spp: 4, seed: 7)
            let original = preview.pixels
            let full = try await session.render(spp: 4, seed: 7, output: .full)
            try require(preview.metadata.shape == [32, 32, 3], "Wrong preview shape")
            try require(full.metadata.shape == [32, 32, 15], "Wrong Stokes shape")
            let rgb = floats(preview), stokes = floats(full)
            try require(stokes.allSatisfy(\.isFinite) && rgb.contains(where: { $0 > 0 }), "Invalid pixels")
            for pixel in 0..<(32 * 32) {
                for channel in 0..<3 {
                    try require(abs(rgb[pixel * 3 + channel] - stokes[pixel * 15 + channel]) < 2e-6,
                                "GPU preview differs from full frame")
                }
            }
            do {
                try await session.loadScene(root.appendingPathComponent("scenes/does-not-exist.xml"))
                throw WorkerError.protocolFailure("Missing scene unexpectedly loaded")
            } catch WorkerError.backend {}
            let recovered = try await session.render(spp: 4, seed: 7, output: .full)
            let recoveredPixels = floats(recovered)
            try require(zip(recoveredPixels, stokes).allSatisfy { abs($0 - $1) < 2e-6 },
                        "Scene lost after recoverable error")
            try await session.loadScene(scene, parameters: ["size": "64"])
            let larger = try await session.render(spp: 4, seed: 8, output: .full)
            try require(larger.metadata.shape == [64, 64, 15], "Shared buffer failed to grow")
            try require(preview.pixels == original && session.processID == pid, "Frame changed or worker restarted")
            let variant = try await session.ping()
            try require(variant == "metal_rgb_polarized", "Wrong backend")
            // Simulate a native renderer crash. The Swift host must survive.
            kill(pid, SIGKILL)
            do {
                _ = try await session.ping()
                throw WorkerError.protocolFailure("Killed worker responded")
            } catch WorkerError.unavailable {}
            await session.shutdown()
            let restarted = try await PythonRenderSession.connect(config)
            try await restarted.loadScene(scene, parameters: ["size": "32"])
            let again = try await restarted.render(spp: 4, seed: 7)
            await restarted.shutdown()
            try require(zip(floats(again), rgb).allSatisfy { abs($0 - $1) < 2e-6 },
                        "Restarted worker changed preview beyond floating-point tolerance")
            print("PASS: Swift → persistent Python → Metal; RGB/full frames, buffer growth, snapshot ownership, scene error recovery, crash detection and restart")
            print("32² preview: render \(preview.metadata.renderMs) ms; shared \(preview.sharedMs) ms; owned snapshot \(preview.snapshotMs) ms")
        } catch {
            await session.shutdown()
            throw error
        }
    }
}
