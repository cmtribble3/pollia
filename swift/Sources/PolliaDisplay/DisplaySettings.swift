import Foundation
import PolliaRendering

public enum DisplayFilter: UInt32, CaseIterable, Sendable, Identifiable {
    case rgbIntensity, rgbAnalyzer, intensity, analyzer, dop, aolp, ellipticity, q, u, v
    public var id: Self { self }
    public var title: String {
        switch self {
        case .rgbIntensity: "RGB Intensity"
        case .rgbAnalyzer: "RGB Analyzer"
        case .intensity: "Intensity"
        case .analyzer: "Analyzer"
        case .dop: "Degree of Polarization"
        case .aolp: "AoLP [°]"
        case .ellipticity: "Ellipticity"
        case .q: "Q / I"
        case .u: "U / I"
        case .v: "V / I"
        }
    }
    public var isRGB: Bool { self == .rgbIntensity || self == .rgbAnalyzer }
    public var usesExposure: Bool { isRGB || self == .intensity || self == .analyzer }
    public var usesAnalyzer: Bool { self == .analyzer || self == .rgbAnalyzer }
    public var supportsLog: Bool { self == .intensity || self == .analyzer || self == .dop }
    public var defaultColorMap: DisplayColorMap {
        switch self {
        case .q, .u, .v: .diverging
        case .aolp: .phase
        case .ellipticity: .viridis
        default: .grayscale
        }
    }
    public var availableColorMaps: [DisplayColorMap] {
        DisplayColorMap.allCases.filter { $0 != .seismic || self == .q || self == .u || self == .v }
    }
    public var defaultRange: ClosedRange<Double> {
        switch self {
        case .q, .u, .v: -1...1
        case .aolp: -90...90
        case .ellipticity: -1...1
        default: 0...1
        }
    }
}

public struct DisplaySettings: Equatable, Sendable {
    public var filter: DisplayFilter = .rgbIntensity {
        didSet {
            if filter != oldValue { colorMap = filter.defaultColorMap }
        }
    }
    public var colorMap: DisplayColorMap = .grayscale
    public var effectiveColorMap: DisplayColorMap {
        filter.availableColorMaps.contains(colorMap) ? colorMap : filter.defaultColorMap
    }
    public var channel = 0
    public var exposureEV = 0.0
    public var analyzerAngle = 0.0
    public var logarithmic = false
    public var minimum = 0.0
    public var maximum = 1.0
    public init() {}
}

public struct StokesChannels: Sendable {
    public let isRGB: Bool
    public let hasPolarization: Bool
    public let labels: [String]
    let indices: [SIMD4<UInt32>]

    public init(names: [String]) throws {
        isRGB = names.contains("R") && names.contains("G") && names.contains("B")
        let bases = isRGB ? ["R", "G", "B"] : [names.first ?? "Y"]
        labels = isRGB ? ["Red", "Green", "Blue"] : ["Monochrome"]
        var found: [SIMD4<UInt32>] = []
        var polarized = true
        for base in bases {
            let quartet = (0..<4).map { names.firstIndex(of: "S\($0).\(base)") }
            if quartet.allSatisfy({ $0 != nil }) {
                found.append(SIMD4(quartet.map { UInt32($0!) }))
            } else {
                polarized = false
                guard let index = names.firstIndex(of: base) else {
                    throw WorkerError.backend("Unsupported film channel layout")
                }
                found.append(SIMD4(UInt32(index), UInt32(index), UInt32(index), UInt32(index)))
            }
        }
        hasPolarization = polarized
        indices = isRGB ? found : [found[0], found[0], found[0]]
    }
}
