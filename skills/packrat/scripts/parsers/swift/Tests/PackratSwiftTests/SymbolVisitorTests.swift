import Foundation
import XCTest
@testable import PackratSwift

final class SymbolVisitorTests: XCTestCase {
    private func fixturesRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("fixtures")
    }

    func testGoldenSampleSymbolsCoversAllSwiftKindsAndDocstrings() throws {
        let sampleURL = fixturesRoot().appendingPathComponent("sample_symbols.swift")
        let source = try String(contentsOf: sampleURL, encoding: .utf8)
        let result = parseSwiftFile(path: "sample_symbols.swift", source: source)

        XCTAssertEqual(result.syntaxErrorCount, 0)

        struct TupleKey: Equatable {
            let kind: String
            let name: String
            let qualifiedName: String
            let parent: String
            let visibility: String
            let docstring: String
        }

        let actual = result.symbols.map {
            TupleKey(
                kind: $0.kind,
                name: $0.name,
                qualifiedName: $0.qualifiedName,
                parent: $0.parent,
                visibility: $0.visibility,
                docstring: $0.docstring
            )
        }

        let expected: [TupleKey] = [
            TupleKey(kind: "variable", name: "defaultLimit", qualifiedName: "defaultLimit", parent: "", visibility: "public", docstring: "Default limit for widget queries."),
            TupleKey(kind: "variable", name: "secretCounter", qualifiedName: "secretCounter", parent: "", visibility: "private", docstring: ""),
            TupleKey(kind: "typedef", name: "WidgetHandler", qualifiedName: "WidgetHandler", parent: "", visibility: "public", docstring: "Callback signature for widget events."),
            TupleKey(kind: "enum", name: "Color", qualifiedName: "Color", parent: "", visibility: "public", docstring: "Supported widget colors."),
            TupleKey(kind: "enum_value", name: "red", qualifiedName: "Color.red", parent: "Color", visibility: "", docstring: "Bright red variant."),
            TupleKey(kind: "enum_value", name: "green", qualifiedName: "Color.green", parent: "Color", visibility: "", docstring: ""),
            TupleKey(kind: "protocol", name: "Renderable", qualifiedName: "Renderable", parent: "", visibility: "public", docstring: "Contract for renderable UI elements."),
            TupleKey(kind: "method", name: "render", qualifiedName: "Renderable.render", parent: "Renderable", visibility: "", docstring: ""),
            TupleKey(kind: "struct", name: "LayoutConfig", qualifiedName: "LayoutConfig", parent: "", visibility: "internal", docstring: "Value-type layout configuration."),
            TupleKey(kind: "field", name: "width", qualifiedName: "LayoutConfig.width", parent: "LayoutConfig", visibility: "public", docstring: ""),
            TupleKey(kind: "field", name: "height", qualifiedName: "LayoutConfig.height", parent: "LayoutConfig", visibility: "package", docstring: ""),
            TupleKey(kind: "constructor", name: "init", qualifiedName: "LayoutConfig.init", parent: "LayoutConfig", visibility: "public", docstring: ""),
            TupleKey(kind: "class", name: "TelemetryActor", qualifiedName: "TelemetryActor", parent: "", visibility: "public", docstring: "Thread-safe telemetry collector."),
            TupleKey(kind: "method", name: "record", qualifiedName: "TelemetryActor.record", parent: "TelemetryActor", visibility: "public", docstring: ""),
            TupleKey(kind: "class", name: "BaseWidget", qualifiedName: "BaseWidget", parent: "", visibility: "public", docstring: "Base UI widget with lifecycle hooks."),
            TupleKey(kind: "field", name: "count", qualifiedName: "BaseWidget.count", parent: "BaseWidget", visibility: "public", docstring: ""),
            TupleKey(kind: "field", name: "internalTag", qualifiedName: "BaseWidget.internalTag", parent: "BaseWidget", visibility: "private", docstring: ""),
            TupleKey(kind: "constructor", name: "init", qualifiedName: "BaseWidget.init", parent: "BaseWidget", visibility: "public", docstring: "Initialize the widget with a label."),
            TupleKey(kind: "field", name: "_label", qualifiedName: "BaseWidget._label", parent: "BaseWidget", visibility: "private", docstring: ""),
            TupleKey(kind: "getter", name: "label", qualifiedName: "BaseWidget.label", parent: "BaseWidget", visibility: "public", docstring: "Current widget label."),
            TupleKey(kind: "setter", name: "label", qualifiedName: "BaseWidget.label", parent: "BaseWidget", visibility: "public", docstring: ""),
            TupleKey(kind: "getter", name: "sameLineAccessor", qualifiedName: "BaseWidget.sameLineAccessor", parent: "BaseWidget", visibility: "public", docstring: ""),
            TupleKey(kind: "setter", name: "sameLineAccessor", qualifiedName: "BaseWidget.sameLineAccessor", parent: "BaseWidget", visibility: "public", docstring: ""),
            TupleKey(kind: "method", name: "subscript", qualifiedName: "BaseWidget.subscript", parent: "BaseWidget", visibility: "public", docstring: "Access child item by index."),
            TupleKey(kind: "method", name: "render", qualifiedName: "BaseWidget.render", parent: "BaseWidget", visibility: "public", docstring: "Render the widget description."),
            TupleKey(kind: "extension", name: "extension@L76", qualifiedName: "extension@L76", parent: "", visibility: "", docstring: ""),
            TupleKey(kind: "method", name: "shout", qualifiedName: "String.shout", parent: "extension@L76", visibility: "public", docstring: "Uppercased string with exclamation mark."),
            TupleKey(kind: "function", name: "renderAll", qualifiedName: "renderAll", parent: "", visibility: "public", docstring: "Render every widget and return total count."),
        ]

        XCTAssertEqual(actual, expected)

        let allNames = Set(result.symbols.map(\.name))
        XCTAssertFalse(allNames.contains("localHelper"))
        XCTAssertFalse(allNames.contains("nestedLocal"))
        XCTAssertFalse(allNames.contains("closure"))

        let sameLine = result.symbols.filter { $0.name == "sameLineAccessor" }
        XCTAssertEqual(sameLine.count, 2)
        XCTAssertEqual(sameLine[0].lineStart, sameLine[1].lineStart)
        XCTAssertEqual(Set(sameLine.map(\.kind)), Set(["getter", "setter"]))

        let renderAllSym = try XCTUnwrap(result.symbols.first { $0.name == "renderAll" })
        let lines = splitLinesLF(source)
        XCTAssertEqual(lines[renderAllSym.lineStart - 1].trimmingCharacters(in: .whitespaces), "@MainActor")
        XCTAssertEqual(
            renderAllSym.signature,
            "@MainActor public func renderAll( items: [BaseWidget], strict: Bool = false ) -> Int"
        )
    }

    func testFormFeedLineNumbersUseLFOnly() {
        let source = "let a = 1\u{000C}// still line 1\npublic func afterFormFeed() -> Int {\n    return 2\n}\n"
        let parsed = parseSwiftFile(path: "ff.swift", source: source)
        let fn = parsed.symbols.first { $0.name == "afterFormFeed" }
        XCTAssertNotNil(fn)
        XCTAssertEqual(fn?.lineStart, 2)
        XCTAssertEqual(fn?.lineEnd, 4)
    }

    func testSyntaxErrorFixtureRecoversAndReportsErrors() throws {
        let errURL = fixturesRoot()
            .appendingPathComponent("conformance")
            .appendingPathComponent("syntax_error.swift")
        let source = try String(contentsOf: errURL, encoding: .utf8)
        let parsed = parseSwiftFile(path: "syntax_error.swift", source: source)
        XCTAssertGreaterThanOrEqual(parsed.syntaxErrorCount, 1)
        XCTAssertTrue(parsed.symbols.contains { $0.name == "ValidBeforeError" })
    }

    func testGeneratedOneLinerDropsNestedAndCapsAt200() {
        var chunks: [String] = ["// " + String(repeating: "x", count: 4100)]
        for i in 0..<230 {
            chunks.append("public struct S\(i) { public var f: Int = 0 }")
        }
        let source = chunks.joined(separator: "\n")
        let parsed = parseSwiftFile(path: "generated.swift", source: source)
        XCTAssertEqual(parsed.symbols.count, 200)
        XCTAssertTrue(parsed.symbols.allSatisfy { $0.parent.isEmpty })
    }

    func testDeepNestingDeadlineTimesOutAt50ms() throws {
        let deepURL = fixturesRoot()
            .appendingPathComponent("conformance")
            .appendingPathComponent("deep_nesting.swift")
        let source = try String(contentsOf: deepURL, encoding: .utf8)
        let req = ParseRequest(id: 99, path: "deep_nesting.swift", content: source, deadlineMs: 50)
        let data = try XCTUnwrap(runRequestWithDeadline(req))
        let errResp = try JSONDecoder().decode(ParseErrorResponse.self, from: data)
        XCTAssertEqual(errResp.id, 99)
        XCTAssertEqual(errResp.error, "timeout")
    }
}
