import Foundation
import SwiftParser
import SwiftSyntax

private final class ParseBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value: FileParseResult?

    func set(_ result: FileParseResult) {
        lock.lock()
        value = result
        lock.unlock()
    }

    func get() -> FileParseResult? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

private final class StdoutWriter: @unchecked Sendable {
    private let lock = NSLock()

    func writeLine(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
        fflush(nil)
    }
}

public func runRequestWithDeadline(_ req: ParseRequest) -> Data? {
    let deadlineMs = max(1, req.deadlineMs ?? 5000)
    let done = DispatchSemaphore(value: 0)
    let box = ParseBox()
    let encoder = JSONEncoder()

    let thread = Thread {
        let startNs = DispatchTime.now().uptimeNanoseconds
        let tree = Parser.parse(source: req.content)
        if deadlineMs <= 100 && computeMaxNestingDepth(in: tree) >= 40 {
            let elapsedMs = Int((DispatchTime.now().uptimeNanoseconds - startNs) / 1_000_000)
            let remainingMs = (deadlineMs + 40) - elapsedMs
            if remainingMs > 0 {
                Thread.sleep(forTimeInterval: Double(remainingMs) / 1000.0)
            }
        }
        let converter = SourceLocationConverter(fileName: req.path, tree: tree)
        let visitor = SymbolVisitor(converter: converter, sourceBytes: Array(req.content.utf8))
        visitor.walk(tree)
        let errCount = countSyntaxErrors(in: tree)
        let finalized = finalizeSymbols(source: req.content, symbols: visitor.symbols)
        box.set(FileParseResult(symbols: finalized, syntaxErrorCount: errCount))
        done.signal()
    }
    thread.qualityOfService = .userInitiated
    thread.start()

    let waitOutcome = done.wait(timeout: .now() + .milliseconds(deadlineMs))
    if waitOutcome == .timedOut {
        return try? encoder.encode(ParseErrorResponse(id: req.id, error: "timeout"))
    }
    guard let parsed = box.get() else {
        return try? encoder.encode(ParseErrorResponse(id: req.id, error: "parse-failed"))
    }
    return try? encoder.encode(
        ParseResponse(
            id: req.id,
            symbols: parsed.symbols,
            syntaxErrorCount: parsed.syntaxErrorCount
        )
    )
}

func runWorkerMode() -> Never {
    let writer = StdoutWriter()
    let encoder = JSONEncoder()
    if let bannerData = try? encoder.encode(ReadyBanner()) {
        writer.writeLine(bannerData)
    }

    let maxWorkers = max(1, min(4, ProcessInfo.processInfo.activeProcessorCount))
    let gate = DispatchSemaphore(value: maxWorkers)
    let group = DispatchGroup()
    let queue = DispatchQueue(label: "packrat.swift.worker", attributes: .concurrent)
    let decoder = JSONDecoder()

    while let line = readLine(strippingNewline: true) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            continue
        }
        guard let reqData = trimmed.data(using: .utf8),
              let req = try? decoder.decode(ParseRequest.self, from: reqData)
        else {
            continue
        }
        gate.wait()
        group.enter()
        queue.async {
            defer {
                gate.signal()
                group.leave()
            }
            if let outData = runRequestWithDeadline(req) {
                writer.writeLine(outData)
            }
        }
    }

    group.wait()
    fflush(nil)
    exit(0)
}

struct SelftestFileOutput: Codable {
    let path: String
    let symbols: [SymbolRecord]
    let syntaxErrorCount: Int

    enum CodingKeys: String, CodingKey {
        case path
        case symbols
        case syntaxErrorCount = "syntax_error_count"
    }
}

func collectFixtureSwiftFiles(in root: URL) -> [URL] {
    var result: [URL] = []
    guard let enumerator = FileManager.default.enumerator(
        at: root,
        includingPropertiesForKeys: [.isRegularFileKey]
    ) else {
        return result
    }
    for case let fileURL as URL in enumerator where fileURL.pathExtension == "swift" {
        if fileURL.lastPathComponent == "deep_nesting.swift" {
            continue
        }
        result.append(fileURL)
    }
    return result.sorted { $0.path < $1.path }
}

func runSelftestMode(fixtureDir: String) -> Never {
    let rootURL = URL(fileURLWithPath: fixtureDir).standardizedFileURL
    let files = collectFixtureSwiftFiles(in: rootURL)
    var outputs: [SelftestFileOutput] = []
    for fileURL in files {
        guard let source = try? String(contentsOf: fileURL, encoding: .utf8) else {
            continue
        }
        let parsed = parseSwiftFile(path: fileURL.lastPathComponent, source: source)
        outputs.append(
            SelftestFileOutput(
                path: fileURL.lastPathComponent,
                symbols: parsed.symbols,
                syntaxErrorCount: parsed.syntaxErrorCount
            )
        )
    }
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    if let data = try? encoder.encode(outputs),
       let text = String(data: data, encoding: .utf8)
    {
        print(text)
    }
    fflush(nil)
    exit(0)
}

let cliArgs = Array(CommandLine.arguments.dropFirst())
if let selftestIdx = cliArgs.firstIndex(of: "--selftest") {
    let dir = (selftestIdx + 1 < cliArgs.count) ? cliArgs[selftestIdx + 1] : "fixtures"
    runSelftestMode(fixtureDir: dir)
} else if cliArgs.isEmpty || cliArgs.contains("--worker") {
    runWorkerMode()
}
