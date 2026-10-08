import Foundation
import Darwin

public struct WorkerConfiguration: Sendable {
    public let python: URL
    public let renderingDirectory: URL
    public let backendPythonDirectory: URL
    public let variant: MetalVariant
    public let timeout: TimeInterval
    public let maximumFrameBytes: Int

    public init(python: URL, renderingDirectory: URL, backendPythonDirectory: URL,
                variant: MetalVariant = .rgbPolarized, timeout: TimeInterval = 120,
                maximumFrameBytes: Int = 1024 * 1024 * 1024) {
        self.python = python
        self.renderingDirectory = renderingDirectory
        self.backendPythonDirectory = backendPythonDirectory
        self.variant = variant
        self.timeout = timeout
        self.maximumFrameBytes = maximumFrameBytes
    }
}

/// One long-lived Python process per session. Blocking pipe I/O runs on a serial
/// Dispatch queue, never on the main actor or Swift's cooperative executor.
public final class PythonRenderSession: @unchecked Sendable {
    private let queue = DispatchQueue(label: "pollia.python-render-session")
    private let configuration: WorkerConfiguration
    // All access to wire is confined to queue, including teardown.
    private var wire: WorkerConnection?
    public private(set) var processID: Int32 = 0

    private init(configuration: WorkerConfiguration) { self.configuration = configuration }

    public static func connect(_ configuration: WorkerConfiguration) async throws -> PythonRenderSession {
        let session = PythonRenderSession(configuration: configuration)
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            session.queue.async {
                do {
                    let connection = try WorkerConnection(configuration)
                    session.wire = connection
                    session.processID = connection.process.processIdentifier
                    continuation.resume()
                } catch { continuation.resume(throwing: error) }
            }
        }
        return session
    }

    public func loadScene(_ path: URL, parameters: [String: String] = [:]) async throws {
        try await perform { connection in
            let _: Loaded = try connection.request("load", fields: ["path": path.path, "parameters": parameters])
        }
    }

    public func ping() async throws -> String {
        try await perform { connection in
            let reply: Ping = try connection.request("ping")
            return reply.variant
        }
    }

    public func render(spp: UInt32 = 16, seed: UInt32 = 0, sensor: Int = 0,
                       output: FrameOutput = .preview) async throws -> RenderFrame {
        try await perform { connection in
            let metadata: FrameMetadata = try connection.request("render", fields: [
                "spp": spp, "seed": seed, "sensor": sensor, "output": output.rawValue
            ])
            try metadata.validate(variant: connection.configuration.variant)
            try connection.ensureBuffer(bytes: metadata.bytes)
            let publication: Published = try connection.request("publish", fields: [
                "generation": metadata.generation, "capacity": connection.capacity
            ])
            guard publication.generation == metadata.generation, publication.sharedMs.isFinite else {
                throw WorkerError.protocolFailure("Shared frame generation mismatch")
            }
            let start = ProcessInfo.processInfo.systemUptime
            let snapshot = Data(bytes: connection.mapping!, count: metadata.bytes)
            return RenderFrame(metadata: metadata, pixels: snapshot, sharedMs: publication.sharedMs,
                               snapshotMs: (ProcessInfo.processInfo.systemUptime - start) * 1000)
        }
    }

    /// Graceful shutdown, ordered after outstanding work. A timeout or native
    /// crash closes the session; reconnect and reload to recover.
    public func shutdown() async {
        await withCheckedContinuation { continuation in
            queue.async {
                if let connection = self.wire {
                    let _: Stopped? = try? connection.request("shutdown")
                    connection.close()
                    self.wire = nil
                }
                continuation.resume()
            }
        }
    }

    private func perform<T: Sendable>(_ operation: @escaping @Sendable (WorkerConnection) throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                guard let connection = self.wire else {
                    continuation.resume(throwing: WorkerError.closed)
                    return
                }
                do { continuation.resume(returning: try operation(connection)) }
                catch {
                    // Scene/argument errors are recoverable. Communication failures
                    // invalidate the process and its pending frame transaction.
                    if case WorkerError.backend = error {} else {
                        connection.close()
                        self.wire = nil
                    }
                    continuation.resume(throwing: error)
                }
            }
        }
    }
}

private struct Loaded: Decodable { let loaded: Bool }
private struct Stopped: Decodable { let stopped: Bool }
private struct Ping: Decodable { let variant: String }
private struct Published: Decodable {
    let generation: Int
    let sharedMs: Double
    enum CodingKeys: String, CodingKey { case generation; case sharedMs = "shared_ms" }
}
private struct BackendFailure: Decodable { let type: String; let message: String }
private struct Envelope<T: Decodable>: Decodable {
    let id: Int
    let ok: Bool
    let result: T?
    let error: BackendFailure?
}
private struct Ready: Decodable {
    let event: String
    let `protocol`: Int?
    let variant: String?
    let error: BackendFailure?
}

private final class DiagnosticBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    func append(_ bytes: Data) {
        lock.lock(); defer { lock.unlock() }
        data.append(bytes)
        if data.count > 32768 { data.removeFirst(data.count - 32768) }
    }
    var text: String {
        lock.lock(); defer { lock.unlock() }
        return String(decoding: data, as: UTF8.self)
    }
}

private final class WorkerConnection {
    let configuration: WorkerConfiguration
    let process = Process()
    private let input = Pipe(), output = Pipe(), errors = Pipe()
    private let diagnostics = DiagnosticBuffer()
    private let directory: URL
    private let sharedFile: URL
    private var fd: Int32 = -1
    private var nextID = 0
    private var received = Data()
    private var closed = false
    var mapping: UnsafeMutableRawPointer?
    var capacity = 0

    init(_ configuration: WorkerConfiguration) throws {
        self.configuration = configuration
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("pollia-" + UUID().uuidString)
        sharedFile = directory.appendingPathComponent("frame.f32")
        do {
            guard configuration.timeout.isFinite, configuration.timeout > 0,
                  configuration.maximumFrameBytes > 0 else {
                throw WorkerError.unavailable("Invalid worker timeout or frame limit")
            }
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                                    attributes: [.posixPermissions: 0o700])
            fd = Darwin.open(sharedFile.path, O_CREAT | O_EXCL | O_RDWR, 0o600)
            guard fd >= 0 else { throw WorkerError.unavailable("Cannot create shared frame file") }
            process.executableURL = configuration.python
            process.arguments = ["-u", "-m", "pollia.worker", "--shared-file", sharedFile.path,
                                 "--variant", configuration.variant.rawValue]
            var environment = ProcessInfo.processInfo.environment
            // Pin both packages to this checkout/build; don't depend on global Python.
            environment["PYTHONPATH"] = configuration.renderingDirectory.path + ":" + configuration.backendPythonDirectory.path
            environment["PYTHONNOUSERSITE"] = "1"
            process.environment = environment
            process.currentDirectoryURL = configuration.renderingDirectory
            process.standardInput = input
            process.standardOutput = output
            process.standardError = errors
            let log = diagnostics
            errors.fileHandleForReading.readabilityHandler = { handle in
                if let bytes = try? handle.read(upToCount: 4096), !bytes.isEmpty { log.append(bytes) }
                else { handle.readabilityHandler = nil }
            }
            try process.run()
            guard fcntl(input.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) == 0 else {
                throw WorkerError.unavailable("Cannot suppress SIGPIPE on the worker command pipe")
            }
            // Close parent copies of the child's pipe endpoints so EOF is observable.
            try input.fileHandleForReading.close()
            try output.fileHandleForWriting.close()
            try errors.fileHandleForWriting.close()
            let flags = fcntl(output.fileHandleForReading.fileDescriptor, F_GETFL)
            _ = fcntl(output.fileHandleForReading.fileDescriptor, F_SETFL, flags | O_NONBLOCK)
            let writeFlags = fcntl(input.fileHandleForWriting.fileDescriptor, F_GETFL)
            _ = fcntl(input.fileHandleForWriting.fileDescriptor, F_SETFL, writeFlags | O_NONBLOCK)
            let ready = try JSONDecoder().decode(Ready.self, from: readLine())
            guard ready.event == "ready", ready.protocol == 1, ready.variant == configuration.variant.rawValue else {
                throw WorkerError.unavailable(ready.error?.message ?? "Worker handshake mismatch")
            }
        } catch {
            close()
            throw error
        }
    }

    deinit { close() }

    func request<T: Decodable>(_ command: String, fields: [String: Any] = [:]) throws -> T {
        guard !closed, process.isRunning else {
            throw WorkerError.unavailable("Python worker exited. " + diagnostics.text)
        }
        nextID += 1
        var request = fields
        request["id"] = nextID
        request["command"] = command
        var data = try JSONSerialization.data(withJSONObject: request, options: [.sortedKeys])
        guard data.count < 1024 * 1024 else { throw WorkerError.backend("Command is too large") }
        data.append(10)
        try send(data)
        let envelope = try JSONDecoder().decode(Envelope<T>.self, from: readLine())
        guard envelope.id == nextID else { throw WorkerError.protocolFailure("Worker reply id mismatch") }
        guard envelope.ok, let result = envelope.result else {
            let failure = envelope.error
            if failure?.type == "MetalBackendError" {
                throw WorkerError.unavailable(failure?.message ?? "Metal rendering failed")
            }
            throw WorkerError.backend(failure.map { "\($0.type): \($0.message)" } ?? "Unknown backend error")
        }
        return result
    }

    private func send(_ data: Data) throws {
        let deadline = ProcessInfo.processInfo.systemUptime + configuration.timeout
        let descriptor = input.fileHandleForWriting.fileDescriptor
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let remaining = deadline - ProcessInfo.processInfo.systemUptime
                guard remaining > 0 else { throw WorkerError.timeout }
                let count = Darwin.write(descriptor, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if count > 0 { offset += count; continue }
                if count < 0 && errno == EINTR { continue }
                guard count < 0 && errno == EAGAIN else {
                    throw WorkerError.unavailable("Cannot send to Python worker. " + diagnostics.text)
                }
                var event = pollfd(fd: descriptor, events: Int16(POLLOUT), revents: 0)
                let status = poll(&event, 1, Int32(min(remaining * 1000 + 1, 1000)))
                if status < 0 && errno != EINTR {
                    throw WorkerError.unavailable("Cannot poll worker command pipe")
                }
            }
        }
    }

    func ensureBuffer(bytes: Int) throws {
        guard bytes > 0, bytes <= configuration.maximumFrameBytes else {
            throw WorkerError.backend("Frame exceeds the configured shared-memory limit")
        }
        if bytes <= capacity { return }
        if let mapping { munmap(mapping, capacity); self.mapping = nil }
        guard ftruncate(fd, off_t(bytes)) == 0 else { throw WorkerError.unavailable("Cannot grow shared frame buffer") }
        let address = mmap(nil, bytes, PROT_READ, MAP_SHARED, fd, 0)
        guard address != MAP_FAILED, let address else { throw WorkerError.unavailable("Cannot map shared frame buffer") }
        mapping = address
        capacity = bytes
    }

    private func readLine() throws -> Data {
        let deadline = ProcessInfo.processInfo.systemUptime + configuration.timeout
        while true {
            if let newline = received.firstIndex(of: 10) {
                let line = received.prefix(upTo: newline)
                received.removeSubrange(...newline)
                return Data(line)
            }
            guard received.count <= 1024 * 1024 else { throw WorkerError.protocolFailure("Worker reply too large") }
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            guard remaining > 0 else { throw WorkerError.timeout }
            var descriptor = pollfd(fd: output.fileHandleForReading.fileDescriptor, events: Int16(POLLIN), revents: 0)
            let status = poll(&descriptor, 1, Int32(min(remaining * 1000 + 1, 1000)))
            if status < 0 {
                if errno == EINTR { continue }
                throw WorkerError.unavailable("Cannot poll Python worker")
            }
            if status == 0 { continue }
            var bytes = [UInt8](repeating: 0, count: 4096)
            let count = Darwin.read(descriptor.fd, &bytes, bytes.count)
            if count > 0 { received.append(contentsOf: bytes.prefix(count)) }
            else if count < 0 && (errno == EAGAIN || errno == EINTR) { continue }
            else { throw WorkerError.unavailable("Python worker closed its output. " + diagnostics.text) }
        }
    }

    func close() {
        guard !closed else { return }
        closed = true
        if process.isRunning {
            // Teardown must remain bounded even if a native render is stuck.
            kill(process.processIdentifier, SIGKILL)
            process.waitUntilExit()
        }
        errors.fileHandleForReading.readabilityHandler = nil
        try? input.fileHandleForWriting.close()
        try? output.fileHandleForReading.close()
        try? errors.fileHandleForReading.close()
        if let mapping { munmap(mapping, capacity); self.mapping = nil }
        if fd >= 0 { Darwin.close(fd); fd = -1 }
        try? FileManager.default.removeItem(at: directory)
    }
}
