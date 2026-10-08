import Foundation

public enum ScenePreset: String, CaseIterable, Codable, Sendable {
    case cornell, glass, conductor, jarecki
    public var title: String {
        switch self {
        case .cornell: "Cornell Box"
        case .glass: "Glass Sphere"
        case .conductor: "Aluminum Sphere"
        case .jarecki: "Jarecki Sphere"
        }
    }
}

public enum SceneIntegrator: String, CaseIterable, Codable, Sendable {
    case path, volpath, pis = "pollia_pis", mueller = "pollia_mueller"
    public var title: String {
        switch self {
        case .path: "Path"
        case .volpath: "Volumetric Path"
        case .pis: "Pollia PIS"
        case .mueller: "Pollia Mueller"
        }
    }
    public var requiresMono: Bool { self == .pis || self == .mueller }
}

public struct SceneSettings: Codable, Equatable, Sendable {
    public var preset: ScenePreset = .cornell
    public var size = 512
    public var maxDepth = 4
    public var rrDepth = 8
    public var integrator: SceneIntegrator = .path
    public var lightScale = 1.0
    public var yaw = 0.0
    public var pitch = 0.0
    public var distance = 1.0
    public init() {}
    enum CodingKeys: String, CodingKey {
        case preset, size, integrator, yaw, pitch, distance
        case maxDepth = "max_depth", rrDepth = "rr_depth", lightScale = "light_scale"
    }
}
