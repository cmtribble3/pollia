// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PolliaRendering",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "PolliaRendering", targets: ["PolliaRendering"]),
        .library(name: "PolliaDisplay", targets: ["PolliaDisplay"]),
        .executable(name: "Pollia", targets: ["PolliaApp"]),
        .executable(name: "pollia-worker-check", targets: ["WorkerCheck"]),
        .executable(name: "pollia-display-check", targets: ["DisplayCheck"])
    ],
    targets: [
        .target(name: "PolliaRendering"),
        .target(name: "PolliaDisplay", dependencies: ["PolliaRendering"], resources: [.copy("Shaders")]),
        .executableTarget(name: "PolliaApp", dependencies: ["PolliaRendering", "PolliaDisplay"]),
        .executableTarget(name: "WorkerCheck", dependencies: ["PolliaRendering"]),
        .executableTarget(name: "DisplayCheck", dependencies: ["PolliaRendering", "PolliaDisplay"]),
        .testTarget(name: "PolliaRenderingTests", dependencies: ["PolliaRendering"])
    ]
)
