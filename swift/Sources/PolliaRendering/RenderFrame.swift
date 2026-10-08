import Foundation

public enum MetalVariant: String, Sendable {
    case rgbPolarized = "metal_rgb_polarized"
    case monoPolarized = "metal_ad_mono_polarized"
}

public enum FrameOutput: String, Sendable {
    case preview, full
}

public struct FrameMetadata: Decodable, Sendable {
    public let generation: Int
    public let shape: [Int]
    public let channelNames: [String]
    public let bytes: Int
    public let dtype: String
    public let renderMs: Double
    public let readbackMs: Double
    public let kernelCount: Int
    public let variant: String

    enum CodingKeys: String, CodingKey {
        case generation, shape, bytes, dtype, variant
        case channelNames = "channel_names", renderMs = "render_ms"
        case readbackMs = "readback_ms", kernelCount = "kernel_count"
    }

    func validate(variant expected: MetalVariant) throws {
        guard shape.count == 3, shape.allSatisfy({ $0 > 0 }),
              channelNames.count == shape[2], dtype == "float32-le",
              variant == expected.rawValue, kernelCount > 0, generation > 0,
              renderMs.isFinite, readbackMs.isFinite else {
            throw WorkerError.protocolFailure("Invalid frame metadata")
        }
        var count = 4
        for dimension in shape {
            let product = count.multipliedReportingOverflow(by: dimension)
            guard !product.overflow else { throw WorkerError.protocolFailure("Frame size overflow") }
            count = product.partialValue
        }
        guard bytes == count else { throw WorkerError.protocolFailure("Frame byte count mismatch") }
    }
}

/// An owned snapshot: subsequent worker renders cannot change these pixels.
/// Pixels are linear, interleaved float32, with channels named in metadata.
public struct RenderFrame: Sendable {
    public let metadata: FrameMetadata
    public let pixels: Data
    public let sharedMs: Double
    public let snapshotMs: Double
}

public enum WorkerError: Error, CustomStringConvertible, Sendable {
    case backend(String)
    case protocolFailure(String)
    case unavailable(String)
    case timeout
    case closed

    public var description: String {
        switch self {
        case .backend(let message), .protocolFailure(let message), .unavailable(let message): message
        case .timeout: "Python worker timed out and was stopped"
        case .closed: "Python worker session is closed"
        }
    }
}
