import Foundation

public enum ScenePreset: String, CaseIterable, Codable, Sendable {
    case cornell
    public var title: String { "Cornell Box" }
}

public enum SceneIntegrator: String, CaseIterable, Codable, Sendable {
    case mueller = "pollia_mueller", pis = "pollia_pis"
    public var title: String { self == .mueller ? "Mueller" : "PIS" }
}

public struct SceneSettings: Codable, Equatable, Sendable {
    public var preset: ScenePreset = .cornell
    public var size = 512
    public var maxDepth = 4
    public var rrDepth = 8
    public var integrator: SceneIntegrator = .mueller
    public var lightScale = 1.0
    public var sourceDoP = 0.0
    public var sourceAoLP = 0.0
    public var sourceEllipticity = 0.0
    public var yaw = 0.0
    public var pitch = 0.0
    public var distance = 1.0
    public init() {}
    enum CodingKeys: String, CodingKey {
        case preset, size, integrator, yaw, pitch, distance
        case sourceDoP = "source_dop", sourceAoLP = "source_aolp", sourceEllipticity = "source_ellipticity"
        case maxDepth = "max_depth", rrDepth = "rr_depth", lightScale = "light_scale"
    }
}
