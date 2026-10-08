import Foundation
import XCTest
@testable import PolliaRendering

@MainActor
final class FrameContractTests: XCTestCase {
    func testRejectsCorruptAndNonMetalMetadata() throws {
        let valid: [String: Any] = ["generation": 1, "shape": [32, 32, 15],
                                  "channel_names": Array(repeating: "channel", count: 15),
                                  "bytes": 61440, "dtype": "float32-le", "render_ms": 1.0,
                                  "readback_ms": 0.1, "kernel_count": 1, "variant": "metal_rgb_polarized"]
        func decode(_ fields: [String: Any]) throws -> FrameMetadata {
            try JSONDecoder().decode(FrameMetadata.self, from: JSONSerialization.data(withJSONObject: fields))
        }
        try decode(valid).validate(variant: .rgbPolarized)
        for (key, value) in [("bytes", 61439 as Any), ("shape", [Int.max, 32, 15] as Any),
                             ("variant", "scalar_rgb" as Any), ("kernel_count", 0 as Any),
                             ("dtype", "float64" as Any)] {
            var invalid = valid
            invalid[key] = value
            XCTAssertThrowsError(try decode(invalid).validate(variant: .rgbPolarized))
        }
    }

    func testUnresponsiveWorkerIsStoppedAndSessionCloses() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let executable = directory.appendingPathComponent("unresponsive-worker")
        let script = """
        #!/bin/sh
        printf '%s\\n' '{"event":"ready","protocol":1,"variant":"metal_rgb_polarized"}'
        read command
        exec /bin/sleep 5
        """
        try Data(script.utf8).write(to: executable)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        let config = WorkerConfiguration(python: executable, renderingDirectory: directory,
                                         backendPythonDirectory: directory, timeout: 0.5)
        let session = try await PythonRenderSession.connect(config)
        let start = ProcessInfo.processInfo.systemUptime
        do {
            _ = try await session.ping()
            XCTFail("Expected timeout")
        } catch WorkerError.timeout {}
        XCTAssertLessThan(ProcessInfo.processInfo.systemUptime - start, 2)
        do {
            _ = try await session.ping()
            XCTFail("Expected closed session")
        } catch WorkerError.closed {}
        await session.shutdown()
    }
}
