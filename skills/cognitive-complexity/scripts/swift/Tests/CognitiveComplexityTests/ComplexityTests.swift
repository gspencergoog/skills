import XCTest
@testable import CognitiveComplexity

final class CognitiveComplexityTests: XCTestCase {
    let analyzer = SwiftComplexityAnalyzer(threshold: 15)

    func getComplexity(_ code: String, _ funcName: String) -> Int {
        let result = analyzer.analyzeSource(code, filePath: "test.swift")
        guard let fn = result.functions.first(where: { $0.name == funcName }) else {
            XCTFail("Function \(funcName) not found in \(result.functions.map(\.name))")
            return -1
        }
        return fn.complexity
    }

    // TC01: Flat linear code -> 0
    func testTC01LinearCode() {
        let code = """
        func linearFunction(a: Int, b: Int) -> Int {
            let x = a + b
            let y = x * 2
            return y
        }
        """
        XCTAssertEqual(getComplexity(code, "linearFunction"), 0)
    }

    // TC02: Single if expression -> 1
    func testTC02SingleIf() {
        let code = """
        func singleIf(x: Int) -> Int {
            if x > 0 {
                return x
            }
            return -x
        }
        """
        XCTAssertEqual(getComplexity(code, "singleIf"), 1)
    }

    // TC03: Nested if inside for loop -> 3 (Loop +1, If +2)
    func testTC03NestedIfLoop() {
        let code = """
        func nestedIfLoop(items: [Int]) -> Int {
            var total = 0
            for x in items {
                if x > 0 {
                    total += x
                }
            }
            return total
        }
        """
        XCTAssertEqual(getComplexity(code, "nestedIfLoop"), 3)
    }

    // TC04: Switch expression -> 1
    func testTC04Switch() {
        let code = """
        func processCommand(cmd: String) -> Int {
            switch cmd {
            case "start": return 1
            case "stop": return 0
            default: return -1
            }
        }
        """
        XCTAssertEqual(getComplexity(code, "processCommand"), 1)
    }

    // TC05: Boolean chain same operator -> 2 (if + 1, bool + 1)
    func testTC05BoolChain() {
        let code = """
        func boolChain(a: Bool, b: Bool, c: Bool) -> Bool {
            if a && b && c {
                return true
            }
            return false
        }
        """
        XCTAssertEqual(getComplexity(code, "boolChain"), 2)
    }

    // TC06: Boolean switch operator -> 3 (if + 1, and + 1, or + 1)
    func testTC06BoolSwitch() {
        let code = """
        func boolSwitch(a: Bool, b: Bool, c: Bool) -> Bool {
            if (a && b) || c {
                return true
            }
            return false
        }
        """
        XCTAssertEqual(getComplexity(code, "boolSwitch"), 3)
    }

    // TC07: else if chain -> 1 per branch, 0 nesting penalty
    func testTC07ElseIfChain() {
        let code = """
        func elseIfChain(x: Int) -> String {
            if x == 1 {
                return "one"
            } else if x == 2 {
                return "two"
            } else if x == 3 {
                return "three"
            } else {
                return "other"
            }
        }
        """
        XCTAssertEqual(getComplexity(code, "elseIfChain"), 3)
    }

    // TC08: 3-level nested loop -> 1 + 2 + 3 = 6
    func testTC08TripleLoop() {
        let code = """
        func tripleLoop(matrix: [[[Int]]]) {
            for row in matrix {
                for col in row {
                    for item in col {
                        print(item)
                    }
                }
            }
        }
        """
        XCTAssertEqual(getComplexity(code, "tripleLoop"), 6)
    }

    // TC09: Direct recursion -> +1
    func testTC09Recursion() {
        let code = """
        func factorial(n: Int) -> Int {
            if n <= 1 {
                return 1
            }
            return n * factorial(n: n - 1)
        }
        """
        XCTAssertEqual(getComplexity(code, "factorial"), 2)
    }

    // TC10: try/catch block -> +1 for catch
    func testTC10TryCatch() {
        let code = """
        func safeDivide(a: Double, b: Double) -> Double {
            do {
                return try performDivide(a, b)
            } catch {
                return 0.0
            }
        }
        """
        XCTAssertEqual(getComplexity(code, "safeDivide"), 1)
    }

    // TC11: Nested closure with branch
    func testTC11NestedClosure() {
        let code = """
        func outerWithClosure(items: [Int]) -> [Int] {
            let f = { (x: Int) -> Int in
                if x > 0 { return x * 2 }
                return 0
            }
            return items.map(f)
        }
        """
        XCTAssertEqual(getComplexity(code, "outerWithClosure"), 2)
    }

    // TC12: Ternary nested inside loop
    func testTC12TernaryNested() {
        let code = """
        func ternaryNested(items: [Int]) {
            for x in items {
                let val = x > 0 ? 1 : -1
            }
        }
        """
        XCTAssertEqual(getComplexity(code, "ternaryNested"), 3)
    }

    // TC13: Swift Guard clause (early return idiom -> 1 base, 0 nesting)
    func testTC13GuardClause() {
        let code = """
        func guardClause(user: User?) -> String? {
            guard let user = user else {
                return nil
            }
            guard user.isActive else {
                return nil
            }
            return user.name
        }
        """
        XCTAssertEqual(getComplexity(code, "guardClause"), 2)
    }

    // TC14: Nil-coalescing sequence ??
    func testTC14NilCoalescing() {
        let code = """
        func nilCoalescing(a: Int?, b: Int?, c: Int?) -> Bool {
            if (a ?? b ?? c) != nil {
                return true
            }
            return false
        }
        """
        XCTAssertEqual(getComplexity(code, "nilCoalescing"), 2)
    }

    // TC15: SonarSource Appendix B Example
    func testTC15SonarSourceAppendixB() {
        let code = """
        func getElement(matrix: [[Int?]]) -> Int? {
            for i in 0..<matrix.count {                          // +1 (nesting 0)
                for j in 0..<matrix[i].count {                   // +2 (nesting 1)
                    if let val = matrix[i][j] {                  // +3 (nesting 2)
                        if val > 0 && val < 100 {                // +4 (nesting 3) + 1 (bool) = +5
                            return val
                        } else if val == 0 {                     // +1 (else if base)
                            continue
                        }
                    }
                }
            }
            return nil
        }
        """
        XCTAssertEqual(getComplexity(code, "getElement"), 12)
    }

    // Classes, structs, inits, while, and repeat-while loops
    func testClassesStructsInitsAndLoops() {
        let code = """
        class DataService {
            var count = 0
            init(count: Int) {
                if count > 0 { self.count = count }
            }
            func process() {
                var x = 10
                while x > 0 {
                    x -= 1
                }
                repeat {
                    x += 1
                } while x < 5
            }
        }
        struct Worker {
            func work() {}
        }
        """
        let res = analyzer.analyzeSource(code, filePath: "service.swift")
        XCTAssertEqual(res.functions.count, 3)
        let initFn = res.functions.first(where: { $0.name == "init" })
        XCTAssertEqual(initFn?.complexity, 1)

        let processFn = res.functions.first(where: { $0.name == "process" })
        XCTAssertEqual(processFn?.complexity, 2)
    }

    // Models serialization and report creation
    func testModelsSerialization() throws {
        let inc = ComplexityIncrement(line: 1, column: 1, type: "if", increment: 1, nesting: 0, reason: "if")
        let fn = FunctionComplexity(name: "foo", className: "Bar", lineNumber: 1, endLineNumber: 10, complexity: 1, exceedsThreshold: false, breakdown: [inc])
        let file = FileComplexity(path: "test.swift", totalComplexity: 1, averageComplexity: 1.0, highestComplexity: 1, functions: [fn])
        let summary = ComplexitySummary(totalFiles: 1, totalFunctions: 1, totalComplexity: 1, averageComplexity: 1.0, highestComplexity: 1, functionsExceedingThreshold: 0, threshold: 15)
        let report = ComplexityReport(summary: summary, files: [file])

        let data = try JSONEncoder().encode(report)
        let decoded = try JSONDecoder().decode(ComplexityReport.self, from: data)
        XCTAssertEqual(decoded.language, "swift")
        XCTAssertEqual(decoded.files.count, 1)
        XCTAssertEqual(decoded.files[0].functions[0].name, "foo")
    }
}
