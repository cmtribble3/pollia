import Foundation
import Metal
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers
import PolliaRendering

public struct DisplaySurface: @unchecked Sendable {
    public let texture: MTLTexture
    public let scalarValues: MTLBuffer
    public let width: Int
    public let height: Int

    public func pngData() throws -> Data {
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        texture.getBytes(&pixels, bytesPerRow: width * 4,
                         from: MTLRegionMake2D(0, 0, width, height), mipmapLevel: 0)
        let data = Data(pixels)
        guard let provider = CGDataProvider(data: data as CFData),
              let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let image = CGImage(width: width, height: height, bitsPerComponent: 8,
                                  bitsPerPixel: 32, bytesPerRow: width * 4,
                                  space: colorSpace, bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.last.rawValue),
                                  provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent) else {
            throw WorkerError.backend("Cannot prepare the displayed image for export")
        }
        let result = NSMutableData()
        guard let writer = CGImageDestinationCreateWithData(result, UTType.png.identifier as CFString, 1, nil) else {
            throw WorkerError.backend("Cannot create PNG writer")
        }
        CGImageDestinationAddImage(writer, image, nil)
        guard CGImageDestinationFinalize(writer) else { throw WorkerError.backend("Cannot write PNG") }
        return result as Data
    }
}

private struct DisplayUniforms {
    var dimensions: SIMD4<UInt32>
    var settings: SIMD4<Float>
    var mode: SIMD4<UInt32>
    var red: SIMD4<UInt32>
    var green: SIMD4<UInt32>
    var blue: SIMD4<UInt32>
}

public final class MetalDisplayEngine: @unchecked Sendable {
    public let device: MTLDevice
    public let presentationPipeline: MTLRenderPipelineState
    private let queue: MTLCommandQueue
    private let compute: MTLComputePipelineState
    private let work = DispatchQueue(label: "pollia.display")
    // Confined to work; changing display settings reuses this GPU buffer.
    private var pixels: MTLBuffer?
    private var metadata: FrameMetadata?
    private var channels: StokesChannels?
    private let presentationLock = NSLock()
    private var presentations = 0

    public var presentedFrameCount: Int {
        presentationLock.lock(); defer { presentationLock.unlock() }
        return presentations
    }

    public func recordPresentedFrame() {
        presentationLock.lock(); defer { presentationLock.unlock() }
        presentations += 1
    }

    public init() throws {
        guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else {
            throw WorkerError.unavailable("Metal is unavailable; Pollia requires a GPU")
        }
        self.device = device
        self.queue = queue
        guard let sourceURL = Bundle.module.url(forResource: "Display", withExtension: "metal", subdirectory: "Shaders") else {
            throw WorkerError.unavailable("Metal display shaders are missing")
        }
        let options = MTLCompileOptions()
        if #available(macOS 15, *) { options.mathMode = .safe }
        else { options.fastMathEnabled = false }
        let library = try device.makeLibrary(source: DisplayColorMap.metalSource + String(contentsOf: sourceURL, encoding: .utf8), options: options)
        guard let function = library.makeFunction(name: "display_frame") else {
            throw WorkerError.unavailable("Metal display kernel is missing")
        }
        compute = try device.makeComputePipelineState(function: function)
        let pipeline = MTLRenderPipelineDescriptor()
        pipeline.vertexFunction = library.makeFunction(name: "display_vertex")
        pipeline.fragmentFunction = library.makeFunction(name: "display_fragment")
        pipeline.colorAttachments[0].pixelFormat = .bgra8Unorm
        presentationPipeline = try device.makeRenderPipelineState(descriptor: pipeline)
    }

    public func display(frame: RenderFrame? = nil, settings: DisplaySettings) async throws -> DisplaySurface {
        try await withCheckedThrowingContinuation { continuation in
            work.async {
                do { continuation.resume(returning: try self.draw(frame: frame, settings: settings)) }
                catch { continuation.resume(throwing: error) }
            }
        }
    }

    private func draw(frame: RenderFrame?, settings: DisplaySettings) throws -> DisplaySurface {
        if let frame {
            guard let variant = MetalVariant(rawValue: frame.metadata.variant) else {
                throw WorkerError.backend("Frame is not from a supported Metal backend")
            }
            try frame.metadata.validate(variant: variant)
            guard frame.pixels.count == frame.metadata.bytes else { throw WorkerError.backend("Frame byte count mismatch") }
            let layout = try StokesChannels(names: frame.metadata.channelNames)
            let buffer = frame.pixels.withUnsafeBytes { bytes in
                device.makeBuffer(bytes: bytes.baseAddress!, length: bytes.count, options: .storageModeShared)
            }
            guard let buffer else { throw WorkerError.unavailable("Cannot allocate display input") }
            pixels = buffer
            metadata = frame.metadata
            channels = layout
        }
        guard let pixels, let metadata, let channels else { throw WorkerError.backend("Render an image first") }
        guard channels.hasPolarization || settings.filter == .rgbIntensity || settings.filter == .intensity else {
            throw WorkerError.backend("This scene's integrator did not output Stokes channels")
        }
        guard settings.minimum.isFinite, settings.maximum.isFinite, settings.maximum > settings.minimum,
              Float(settings.minimum).isFinite, Float(settings.maximum).isFinite,
              Float(settings.maximum) > Float(settings.minimum),
              settings.exposureEV.isFinite, settings.analyzerAngle.isFinite,
              channels.labels.indices.contains(settings.channel) else {
            throw WorkerError.backend("Invalid display range or channel")
        }
        let width = metadata.shape[1], height = metadata.shape[0]
        let descriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm,
                                                                 width: width, height: height, mipmapped: false)
        descriptor.storageMode = .shared
        descriptor.usage = [.shaderWrite, .shaderRead]
        guard let output = device.makeTexture(descriptor: descriptor),
              let values = device.makeBuffer(length: width * height * 4, options: .storageModeShared),
              let commands = queue.makeCommandBuffer(), let encoder = commands.makeComputeCommandEncoder() else {
            throw WorkerError.unavailable("Cannot allocate Metal display resources")
        }
        var uniforms = DisplayUniforms(dimensions: SIMD4(UInt32(width), UInt32(height), UInt32(metadata.shape[2]), settings.filter.rawValue),
                                       settings: SIMD4(Float(settings.exposureEV), Float(settings.analyzerAngle), Float(settings.minimum), Float(settings.maximum)),
                                       mode: SIMD4(UInt32(settings.channel), settings.logarithmic ? 1 : 0, settings.effectiveColorMap.rawValue, 0),
                                       red: channels.indices[0], green: channels.indices[1], blue: channels.indices[2])
        encoder.setComputePipelineState(compute)
        encoder.setBuffer(pixels, offset: 0, index: 0)
        encoder.setBytes(&uniforms, length: MemoryLayout<DisplayUniforms>.stride, index: 1)
        encoder.setBuffer(values, offset: 0, index: 2)
        encoder.setTexture(output, index: 0)
        encoder.dispatchThreads(MTLSize(width: width, height: height, depth: 1),
                                threadsPerThreadgroup: MTLSize(width: 16, height: 16, depth: 1))
        encoder.endEncoding()
        commands.commit()
        commands.waitUntilCompleted()
        if let error = commands.error { throw error }
        return DisplaySurface(texture: output, scalarValues: values, width: width, height: height)
    }
}
