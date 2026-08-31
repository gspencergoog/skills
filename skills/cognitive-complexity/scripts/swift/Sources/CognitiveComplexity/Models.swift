import Foundation

public struct ComplexityIncrement: Codable, Sendable {
    public let line: Int
    public let column: Int
    public let type: String
    public let increment: Int
    public let nesting: Int
    public let reason: String

    public init(line: Int, column: Int, type: String, increment: Int, nesting: Int, reason: String) {
        self.line = line
        self.column = column
        self.type = type
        self.increment = increment
        self.nesting = nesting
        self.reason = reason
    }
}

public struct FunctionComplexity: Codable, Sendable {
    public let name: String
    public let className: String?
    public let lineNumber: Int
    public let endLineNumber: Int
    public let complexity: Int
    public let exceedsThreshold: boolValue
    public let breakdown: [ComplexityIncrement]

    enum CodingKeys: String, CodingKey {
        case name
        case className = "class_name"
        case lineNumber = "line_number"
        case endLineNumber = "end_line_number"
        case complexity
        case exceedsThreshold = "exceeds_threshold"
        case breakdown
    }

    public init(name: String, className: String?, lineNumber: Int, endLineNumber: Int, complexity: Int, exceedsThreshold: Bool, breakdown: [ComplexityIncrement]) {
        self.name = name
        self.className = className
        self.lineNumber = lineNumber
        self.endLineNumber = endLineNumber
        self.complexity = complexity
        self.exceedsThreshold = exceedsThreshold
        self.breakdown = breakdown
    }
}

public typealias boolValue = Bool

public struct FileComplexity: Codable, Sendable {
    public let path: String
    public let totalComplexity: Int
    public let averageComplexity: Double
    public let highestComplexity: Int
    public var functions: [FunctionComplexity]

    enum CodingKeys: String, CodingKey {
        case path
        case totalComplexity = "total_complexity"
        case averageComplexity = "average_complexity"
        case highestComplexity = "highest_complexity"
        case functions
    }

    public init(path: String, totalComplexity: Int, averageComplexity: Double, highestComplexity: Int, functions: [FunctionComplexity]) {
        self.path = path
        self.totalComplexity = totalComplexity
        self.averageComplexity = averageComplexity
        self.highestComplexity = highestComplexity
        self.functions = functions
    }
}

public struct ComplexitySummary: Codable, Sendable {
    public let totalFiles: Int
    public let totalFunctions: Int
    public let totalComplexity: Int
    public let averageComplexity: Double
    public let highestComplexity: Int
    public let functionsExceedingThreshold: Int
    public let threshold: Int

    enum CodingKeys: String, CodingKey {
        case totalFiles = "total_files"
        case totalFunctions = "total_functions"
        case totalComplexity = "total_complexity"
        case averageComplexity = "average_complexity"
        case highestComplexity = "highest_complexity"
        case functionsExceedingThreshold = "functions_exceeding_threshold"
        case threshold
    }
}

public struct ComplexityReport: Codable, Sendable {
    public let version: String
    public let language: String
    public let summary: ComplexitySummary
    public let files: [FileComplexity]

    public init(version: String = "1.0.0", language: String = "swift", summary: ComplexitySummary, files: [FileComplexity]) {
        self.version = version
        self.language = language
        self.summary = summary
        self.files = files
    }
}
