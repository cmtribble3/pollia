import SwiftUI
import AppKit

@main
struct PolliaApp: App {
    @State private var model = PolliaViewModel()
    var body: some Scene {
        Window("Pollia", id: "pollia") {
            ContentView(model: model)
                .frame(minWidth: 960, minHeight: 640)
                .task {
                    await model.startup()
                    await model.verifyLaunchIfRequested()
                }
        }
        .defaultSize(width: 1280, height: 850)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Open Scene…") { model.openScene() }.keyboardShortcut("o")
            }
            CommandMenu("Render") {
                Button("Fresh Estimate") { model.requestFresh() }.keyboardShortcut("r")
                Button("Continue Estimate") { model.render(fresh: false) }.disabled(!model.canContinue)
                Button("Stop") { model.stop() }.disabled(!model.isRendering)
            }
        }
    }
}
