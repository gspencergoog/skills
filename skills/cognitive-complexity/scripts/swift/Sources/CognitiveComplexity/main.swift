import Foundation

struct CliOptions {
    var format = "text"
    var threshold = 15
    var verbose = false
    var sortKey = "complexity"
    var pathArg = "-"
    var excludes: [String] = []
}

func printHelp() {
    print("""
Usage: CognitiveComplexity [OPTIONS] [PATH]

Calculate Cognitive Complexity for Swift code according to SonarSource standard.

Arguments:
  [PATH]                     Path to file or directory. If omitted or "-", reads from stdin.

Options:
  -f, --format <FORMAT>      Output format: text (default), json, table, summary
  -t, --threshold <INT>      Threshold for flagging high complexity (default: 15)
  -v, --verbose              Show detailed breakdown of increments
  -s, --sort <KEY>           Sort by: complexity (default), name, line, file
  -e, --exclude <PATTERN>    Glob patterns to exclude during directory traversal
  -h, --help                 Show help message
  -V, --version              Show version
""")
}

func parseCliOptions(_ args: [String]) -> CliOptions {
    var options = CliOptions()
    var i = 0
    while i < args.count {
        let arg = args[i]
        if arg == "-f" || arg == "--format" {
            i += 1
            if i < args.count { options.format = args[i] }
        } else if arg == "-t" || arg == "--threshold" {
            i += 1
            if i < args.count { options.threshold = Int(args[i]) ?? 15 }
        } else if arg == "-v" || arg == "--verbose" {
            options.verbose = true
        } else if arg == "-s" || arg == "--sort" {
            i += 1
            if i < args.count { options.sortKey = args[i] }
        } else if arg == "-e" || arg == "--exclude" {
            i += 1
            if i < args.count { options.excludes.append(args[i]) }
        } else if arg == "-h" || arg == "--help" {
            printHelp()
            exit(0)
        } else if arg == "-V" || arg == "--version" {
            print("CognitiveComplexity 1.0.0")
            exit(0)
        } else if !arg.hasPrefix("-") {
            options.pathArg = arg
        }
        i += 1
    }
    return options
}

func collectSwiftFiles(dir: URL, excludes: [String]) -> [URL] {
    var files: [URL] = []
    let fileManager = FileManager.default
    guard let enumerator = fileManager.enumerator(at: dir, includingPropertiesForKeys: [.isRegularFileKey, .isDirectoryKey]) else {
        return files
    }

    for case let fileURL as URL in enumerator {
        let name = fileURL.lastPathComponent
        if name.hasPrefix(".") || name == "build" || name == ".build" {
            if (try? fileURL.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true {
                enumerator.skipDescendants()
            }
            continue
        }
        if fileURL.pathExtension == "swift" {
            files.append(fileURL)
        }
    }
    return files
}

func analyzeTarget(options: CliOptions, analyzer: SwiftComplexityAnalyzer) -> [FileComplexity] {
    var fileResults: [FileComplexity] = []

    if options.pathArg == "-" {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        if let sourceCode = String(data: data, encoding: .utf8) {
            fileResults.append(analyzer.analyzeSource(sourceCode, filePath: "<stdin>"))
        }
        return fileResults
    }

    let url = URL(fileURLWithPath: options.pathArg)
    var isDir: ObjCBool = false
    if !FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) {
        fputs("Error: Path does not exist: \(options.pathArg)\n", stderr)
        exit(2)
    }

    if isDir.boolValue {
        let files = collectSwiftFiles(dir: url, excludes: options.excludes)
        for f in files {
            do {
                let sourceCode = try String(contentsOf: f, encoding: .utf8)
                fileResults.append(analyzer.analyzeSource(sourceCode, filePath: f.path))
            } catch {
                fputs("Error reading \(f.path): \(error.localizedDescription)\n", stderr)
            }
        }
    } else {
        do {
            let sourceCode = try String(contentsOf: url, encoding: .utf8)
            fileResults.append(analyzer.analyzeSource(sourceCode, filePath: url.path))
        } catch {
            fputs("Error reading \(url.path): \(error.localizedDescription)\n", stderr)
        }
    }
    return fileResults
}

func sortSwiftFunctions(_ fileResults: inout [FileComplexity], sortKey: String) {
    for idx in fileResults.indices {
        if sortKey == "complexity" {
            fileResults[idx].functions.sort { $0.complexity > $1.complexity }
        } else if sortKey == "name" {
            fileResults[idx].functions.sort { $0.name < $1.name }
        } else if sortKey == "line" {
            fileResults[idx].functions.sort { $0.lineNumber < $1.lineNumber }
        }
    }
}

func buildSwiftReport(_ fileResults: [FileComplexity], threshold: Int) -> ComplexityReport {
    let totalFiles = fileResults.count
    let totalFuncs = fileResults.reduce(0) { $0 + $1.functions.count }
    let totalComplexity = fileResults.reduce(0) { $0 + $1.totalComplexity }
    let avgComplexity = totalFuncs > 0 ? Double(totalComplexity) / Double(totalFuncs) : 0.0
    let highestComplexity = fileResults.reduce(0) { max($0, $1.highestComplexity) }
    let exceedingCount = fileResults.reduce(0) { $0 + $1.functions.filter(\.exceedsThreshold).count }

    let summary = ComplexitySummary(
        totalFiles: totalFiles,
        totalFunctions: totalFuncs,
        totalComplexity: totalComplexity,
        averageComplexity: (avgComplexity * 100).rounded() / 100,
        highestComplexity: highestComplexity,
        functionsExceedingThreshold: exceedingCount,
        threshold: threshold
    )

    return ComplexityReport(
        version: "1.0.0",
        language: "swift",
        summary: summary,
        files: fileResults
    )
}

func renderSwiftReport(_ report: ComplexityReport, options: CliOptions) {
    if options.format == "json" {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let data = try? encoder.encode(report), let jsonStr = String(data: data, encoding: .utf8) {
            print(jsonStr)
        }
        return
    }
    if options.format == "summary" {
        let s = report.summary
        print("Files: \(s.totalFiles), Functions: \(s.totalFunctions), Total Complexity: \(s.totalComplexity), Avg: \(String(format: "%.2f", s.averageComplexity)), Over Threshold: \(s.functionsExceedingThreshold)")
        return
    }

    print("Cognitive Complexity Report (Language: \(report.language))")
    print(String(repeating: "=", count: 60))
    for f in report.files {
        print("\nFile: \(f.path)")
        if f.functions.isEmpty {
            print("  (No functions or methods found)")
            continue
        }
        for fn in f.functions {
            let qName = fn.className != nil ? "\(fn.className!).\(fn.name)" : fn.name
            let status = fn.exceedsThreshold ? "[EXCEEDS THRESHOLD \(options.threshold)]" : "[PASS]"
            print("  \(qName) (lines \(fn.lineNumber)-\(fn.endLineNumber)) -> Complexity: \(fn.complexity) \(status)")
            if options.verbose && !fn.breakdown.isEmpty {
                for b in fn.breakdown {
                    print("    Line \(String(format: "%4d", b.line)): \(b.reason)")
                }
            }
        }
    }
    let s = report.summary
    print("\n" + String(repeating: "-", count: 60))
    print("Summary:")
    print("  Files analyzed:                \(s.totalFiles)")
    print("  Total functions:               \(s.totalFunctions)")
    print("  Total complexity:              \(s.totalComplexity)")
    print("  Average complexity:            \(String(format: "%.2f", s.averageComplexity))")
    print("  Highest complexity:            \(s.highestComplexity)")
    print("  Functions exceeding threshold: \(s.functionsExceedingThreshold) (threshold: \(options.threshold))")
    print(String(repeating: "=", count: 60))
}

func main() {
    let args = Array(CommandLine.arguments.dropFirst())
    let options = parseCliOptions(args)
    let analyzer = SwiftComplexityAnalyzer(threshold: options.threshold)
    var fileResults = analyzeTarget(options: options, analyzer: analyzer)

    sortSwiftFunctions(&fileResults, sortKey: options.sortKey)
    let report = buildSwiftReport(fileResults, threshold: options.threshold)
    renderSwiftReport(report, options: options)

    exit(report.summary.functionsExceedingThreshold > 0 ? 1 : 0)
}

main()
