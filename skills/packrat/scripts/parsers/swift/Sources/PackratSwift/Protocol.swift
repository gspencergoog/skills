import Foundation

public struct ReadyBanner: Codable, Equatable, Sendable {
    public let ready: Bool
    public let parser: String
    public let `protocol`: Int
    public let parserIds: [String: String]
    public let library: String

    enum CodingKeys: String, CodingKey {
        case ready
        case parser
        case `protocol` = "protocol"
        case parserIds = "parser_ids"
        case library
    }

    public init(
        ready: Bool = true,
        parser: String = "swift-syntax",
        protocolVersion: Int = 1,
        parserIds: [String: String] = [".swift": "swift-syntax@1"],
        library: String = "swift-syntax 600.0.1"
    ) {
        self.ready = ready
        self.parser = parser
        self.protocol = protocolVersion
        self.parserIds = parserIds
        self.library = library
    }
}

public struct ParseRequest: Codable, Equatable, Sendable {
    public let id: Int
    public let path: String
    public let content: String
    public let deadlineMs: Int?

    enum CodingKeys: String, CodingKey {
        case id
        case path
        case content
        case deadlineMs = "deadline_ms"
    }

    public init(id: Int, path: String, content: String, deadlineMs: Int? = 5000) {
        self.id = id
        self.path = path
        self.content = content
        self.deadlineMs = deadlineMs
    }
}

public struct SymbolRecord: Codable, Equatable, Sendable {
    public let kind: String
    public let name: String
    public let qualifiedName: String
    public let parent: String
    public let signature: String
    public let docstring: String
    public let lineStart: Int
    public let lineEnd: Int
    public let visibility: String

    enum CodingKeys: String, CodingKey {
        case kind
        case name
        case qualifiedName = "qualified_name"
        case parent
        case signature
        case docstring
        case lineStart = "line_start"
        case lineEnd = "line_end"
        case visibility
    }

    public init(
        kind: String,
        name: String,
        qualifiedName: String,
        parent: String,
        signature: String,
        docstring: String,
        lineStart: Int,
        lineEnd: Int,
        visibility: String
    ) {
        self.kind = kind
        self.name = name
        self.qualifiedName = qualifiedName
        self.parent = parent
        self.signature = signature
        self.docstring = docstring
        self.lineStart = lineStart
        self.lineEnd = lineEnd
        self.visibility = visibility
    }
}

public struct ParseResponse: Codable, Equatable, Sendable {
    public let id: Int
    public let symbols: [SymbolRecord]
    public let syntaxErrorCount: Int

    enum CodingKeys: String, CodingKey {
        case id
        case symbols
        case syntaxErrorCount = "syntax_error_count"
    }

    public init(id: Int, symbols: [SymbolRecord], syntaxErrorCount: Int) {
        self.id = id
        self.symbols = symbols
        self.syntaxErrorCount = syntaxErrorCount
    }
}

public struct ParseErrorResponse: Codable, Equatable, Sendable {
    public let id: Int
    public let error: String

    public init(id: Int, error: String) {
        self.id = id
        self.error = error
    }
}

public struct FileParseResult: Equatable, Sendable {
    public let symbols: [SymbolRecord]
    public let syntaxErrorCount: Int

    public init(symbols: [SymbolRecord], syntaxErrorCount: Int) {
        self.symbols = symbols
        self.syntaxErrorCount = syntaxErrorCount
    }
}
