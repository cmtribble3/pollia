// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PolliaRendering",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "PolliaRendering", targets: ["PolliaRendering"]),
        .executable(name: "pollia-worker-check", targets: ["WorkerCheck"])
    ],
    targets: [
        .target(name: "PolliaRendering"),
        .executableTarget(name: "WorkerCheck", dependencies: ["PolliaRendering"]),
        .testTarget(name: "PolliaRenderingTests", dependencies: ["PolliaRendering"])
    ]
)
