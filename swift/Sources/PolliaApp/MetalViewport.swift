import SwiftUI
import MetalKit
import PolliaDisplay

struct MetalViewport: NSViewRepresentable {
    let engine: MetalDisplayEngine
    let surface: DisplaySurface

    func makeCoordinator() -> Coordinator { Coordinator(engine: engine) }
    func makeNSView(context: Context) -> MTKView {
        let view = MTKView(frame: .zero, device: engine.device)
        view.colorPixelFormat = .bgra8Unorm
        view.clearColor = MTLClearColorMake(0, 0, 0, 1)
        view.isPaused = true
        view.enableSetNeedsDisplay = true
        view.delegate = context.coordinator
        context.coordinator.surface = surface
        return view
    }
    func updateNSView(_ view: MTKView, context: Context) {
        context.coordinator.surface = surface
        view.setNeedsDisplay(view.bounds)
    }

    final class Coordinator: NSObject, MTKViewDelegate {
        let engine: MetalDisplayEngine
        let queue: MTLCommandQueue?
        var surface: DisplaySurface?
        init(engine: MetalDisplayEngine) {
            self.engine = engine
            queue = engine.device.makeCommandQueue()
        }
        func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}
        func draw(in view: MTKView) {
            guard let surface, let drawable = view.currentDrawable,
                  let pass = view.currentRenderPassDescriptor, let commands = queue?.makeCommandBuffer(),
                  let encoder = commands.makeRenderCommandEncoder(descriptor: pass) else { return }
            encoder.setRenderPipelineState(engine.presentationPipeline)
            encoder.setFragmentTexture(surface.texture, index: 0)
            encoder.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
            encoder.endEncoding()
            let engine = engine
            commands.addCompletedHandler { commands in
                if commands.error == nil { engine.recordPresentedFrame() }
            }
            commands.present(drawable)
            commands.commit()
        }
    }
}
