import Foundation
import SwiftParser
import SwiftSyntax

public final class SwiftComplexityAnalyzer: @unchecked Sendable {
    public let threshold: Int

    public init(threshold: Int = 15) {
        self.threshold = threshold
    }

    public func analyzeSource(_ sourceCode: String, filePath: String = "<stdin>") -> FileComplexity {
        let sourceFile = Parser.parse(source: sourceCode)
        let locationConverter = SourceLocationConverter(fileName: filePath, tree: sourceFile)

        let collector = SwiftFunctionCollector(locationConverter: locationConverter, threshold: threshold)
        collector.walk(sourceFile)

        let functions = collector.functions
        let totalComplexity = functions.reduce(0) { $0 + $1.complexity }
        let avgComplexity = functions.isEmpty ? 0.0 : Double(totalComplexity) / Double(functions.count)
        let highestComplexity = functions.reduce(0) { max($0, $1.complexity) }

        return FileComplexity(
            path: filePath,
            totalComplexity: totalComplexity,
            averageComplexity: (avgComplexity * 100).rounded() / 100,
            highestComplexity: highestComplexity,
            functions: functions
        )
    }
}

final class SwiftFunctionCollector: SyntaxVisitor {
    let locationConverter: SourceLocationConverter
    let threshold: Int
    var functions: [FunctionComplexity] = []
    var currentClass: String? = nil

    init(locationConverter: SourceLocationConverter, threshold: Int) {
        self.locationConverter = locationConverter
        self.threshold = threshold
        super.init(viewMode: .all)
    }

    override func visit(_ node: ClassDeclSyntax) -> SyntaxVisitorContinueKind {
        currentClass = node.name.text
        return .visitChildren
    }

    override func visitPost(_ node: ClassDeclSyntax) {
        currentClass = nil
    }

    override func visit(_ node: StructDeclSyntax) -> SyntaxVisitorContinueKind {
        currentClass = node.name.text
        return .visitChildren
    }

    override func visitPost(_ node: StructDeclSyntax) {
        currentClass = nil
    }

    override func visit(_ node: FunctionDeclSyntax) -> SyntaxVisitorContinueKind {
        let funcName = node.name.text
        guard let body = node.body else { return .skipChildren }

        let visitor = SingleFunctionVisitor(
            functionName: funcName,
            locationConverter: locationConverter,
            threshold: threshold
        )
        visitor.walk(body)

        let startLoc = locationConverter.location(for: node.positionAfterSkippingLeadingTrivia)
        let endLoc = locationConverter.location(for: node.endPositionBeforeTrailingTrivia)

        functions.append(FunctionComplexity(
            name: funcName,
            className: currentClass,
            lineNumber: startLoc.line,
            endLineNumber: endLoc.line,
            complexity: visitor.complexity,
            exceedsThreshold: visitor.complexity > threshold,
            breakdown: visitor.breakdown
        ))

        return .skipChildren
    }

    override func visit(_ node: InitializerDeclSyntax) -> SyntaxVisitorContinueKind {
        guard let body = node.body else { return .skipChildren }

        let visitor = SingleFunctionVisitor(
            functionName: "init",
            locationConverter: locationConverter,
            threshold: threshold
        )
        visitor.walk(body)

        let startLoc = locationConverter.location(for: node.positionAfterSkippingLeadingTrivia)
        let endLoc = locationConverter.location(for: node.endPositionBeforeTrailingTrivia)

        functions.append(FunctionComplexity(
            name: "init",
            className: currentClass,
            lineNumber: startLoc.line,
            endLineNumber: endLoc.line,
            complexity: visitor.complexity,
            exceedsThreshold: visitor.complexity > threshold,
            breakdown: visitor.breakdown
        ))

        return .skipChildren
    }
}

final class SingleFunctionVisitor: SyntaxVisitor {
    let functionName: String
    let locationConverter: SourceLocationConverter
    let threshold: Int

    var complexity = 0
    var currentNesting = 0
    var breakdown: [ComplexityIncrement] = []

    init(functionName: String, locationConverter: SourceLocationConverter, threshold: Int) {
        self.functionName = functionName
        self.locationConverter = locationConverter
        self.threshold = threshold
        super.init(viewMode: .all)
    }

    private func addIncrement(node: any SyntaxProtocol, type: String, baseIncrement: Int, nestingPenalty: Bool, reason: String) {
        let penalty = nestingPenalty ? currentNesting : 0
        let totalInc = baseIncrement + penalty
        complexity += totalInc

        let loc = locationConverter.location(for: node.positionAfterSkippingLeadingTrivia)
        let detail = "\(reason) (+\(baseIncrement)\(penalty > 0 ? " + nesting \(penalty)" : "") = +\(totalInc))"

        breakdown.append(ComplexityIncrement(
            line: loc.line,
            column: loc.column,
            type: type,
            increment: totalInc,
            nesting: currentNesting,
            reason: detail
        ))
    }

    override func visit(_ node: IfExprSyntax) -> SyntaxVisitorContinueKind {
        let isElseIf = node.parent?.is(IfExprSyntax.self) ?? false
        if isElseIf {
            addIncrement(node: node, type: "else_if", baseIncrement: 1, nestingPenalty: false, reason: "else if branch")
        } else {
            addIncrement(node: node, type: "if", baseIncrement: 1, nestingPenalty: true, reason: "if expression")
        }

        processConditions(node.conditions)

        currentNesting += 1
        walk(node.body)
        currentNesting -= 1

        if let elseBody = node.elseBody {
            walk(elseBody)
        }

        return .skipChildren
    }

    override func visit(_ node: GuardStmtSyntax) -> SyntaxVisitorContinueKind {
        addIncrement(node: node, type: "guard", baseIncrement: 1, nestingPenalty: false, reason: "guard statement")
        processConditions(node.conditions)

        walk(node.body)
        return .skipChildren
    }

    override func visit(_ node: TernaryExprSyntax) -> SyntaxVisitorContinueKind {
        addIncrement(node: node, type: "ternary", baseIncrement: 1, nestingPenalty: true, reason: "ternary conditional expression")
        walk(node.condition)

        currentNesting += 1
        walk(node.thenExpression)
        walk(node.elseExpression)
        currentNesting -= 1

        return .skipChildren
    }

    override func visit(_ node: UnresolvedTernaryExprSyntax) -> SyntaxVisitorContinueKind {
        addIncrement(node: node, type: "ternary", baseIncrement: 1, nestingPenalty: true, reason: "ternary conditional expression")
        currentNesting += 1
        walk(node.thenExpression)
        currentNesting -= 1
        return .visitChildren
    }

    override func visit(_ node: SwitchExprSyntax) -> SyntaxVisitorContinueKind {
        addIncrement(node: node, type: "switch", baseIncrement: 1, nestingPenalty: true, reason: "switch expression")
        walk(node.subject)

        currentNesting += 1
        for c in node.cases {
            walk(c)
        }
        currentNesting -= 1

        return .skipChildren
    }

    override func visit(_ node: ForStmtSyntax) -> SyntaxVisitorContinueKind {
        addIncrement(node: node, type: "for_loop", baseIncrement: 1, nestingPenalty: true, reason: "for-in loop")
        currentNesting += 1
        walk(node.body)
        currentNesting -= 1
        return .skipChildren
    }

    override func visit(_ node: WhileStmtSyntax) -> SyntaxVisitorContinueKind {
        addIncrement(node: node, type: "while_loop", baseIncrement: 1, nestingPenalty: true, reason: "while loop")
        currentNesting += 1
        walk(node.body)
        currentNesting -= 1
        return .skipChildren
    }

    override func visit(_ node: RepeatStmtSyntax) -> SyntaxVisitorContinueKind {
        addIncrement(node: node, type: "repeat_while_loop", baseIncrement: 1, nestingPenalty: true, reason: "repeat-while loop")
        currentNesting += 1
        walk(node.body)
        currentNesting -= 1
        return .skipChildren
    }

    override func visit(_ node: CatchClauseSyntax) -> SyntaxVisitorContinueKind {
        addIncrement(node: node, type: "catch", baseIncrement: 1, nestingPenalty: true, reason: "catch clause")
        currentNesting += 1
        walk(node.body)
        currentNesting -= 1
        return .skipChildren
    }

    override func visit(_ node: ClosureExprSyntax) -> SyntaxVisitorContinueKind {
        currentNesting += 1
        for stmt in node.statements {
            walk(stmt)
        }
        currentNesting -= 1
        return .skipChildren
    }

    override func visit(_ node: SequenceExprSyntax) -> SyntaxVisitorContinueKind {
        processSequenceExpr(node, parentOp: nil)
        return .skipChildren
    }

    override func visit(_ node: InfixOperatorExprSyntax) -> SyntaxVisitorContinueKind {
        processInfixBoolOps(node, parentOp: nil)
        return .skipChildren
    }

    private func processConditions(_ conditions: ConditionElementListSyntax) {
        for element in conditions {
            walk(element)
        }
    }

    private func processSequenceExpr(_ node: SequenceExprSyntax, parentOp: String?) {
        var currentOp: String? = parentOp
        var isFirstInSeq = (parentOp == nil)

        for element in node.elements {
            if let binOp = element.as(BinaryOperatorExprSyntax.self) {
                let opText = binOp.operator.text.trimmingCharacters(in: .whitespacesAndNewlines)
                if opText == "&&" || opText == "||" || opText == "??" {
                    if isFirstInSeq {
                        addIncrement(node: binOp, type: "bool_op_sequence", baseIncrement: 1, nestingPenalty: false, reason: "boolean operator sequence (\(opText))")
                        currentOp = opText
                        isFirstInSeq = false
                    } else if currentOp != opText {
                        addIncrement(node: binOp, type: "bool_op_switch", baseIncrement: 1, nestingPenalty: false, reason: "boolean operator switch to (\(opText))")
                        currentOp = opText
                    }
                }
            } else if let tuple = element.as(TupleExprSyntax.self) {
                for tupleElement in tuple.elements {
                    if let seq = tupleElement.expression.as(SequenceExprSyntax.self) {
                        processSequenceExpr(seq, parentOp: currentOp)
                    } else {
                        walk(tupleElement.expression)
                    }
                }
            } else if let innerSeq = element.as(SequenceExprSyntax.self) {
                processSequenceExpr(innerSeq, parentOp: currentOp)
            } else {
                walk(element)
            }
        }
    }

    private func processInfixBoolOps(_ node: InfixOperatorExprSyntax, parentOp: String?) {
        let opName = node.operator.trimmedDescription

        if opName == "&&" || opName == "||" || opName == "??" {
            if parentOp == nil {
                addIncrement(node: node, type: "bool_op_sequence", baseIncrement: 1, nestingPenalty: false, reason: "boolean operator sequence (\(opName))")
            } else if parentOp != opName {
                addIncrement(node: node, type: "bool_op_switch", baseIncrement: 1, nestingPenalty: false, reason: "boolean operator switch to (\(opName))")
            }

            if let leftInfix = node.leftOperand.as(InfixOperatorExprSyntax.self) {
                processInfixBoolOps(leftInfix, parentOp: opName)
            } else if let leftSeq = node.leftOperand.as(SequenceExprSyntax.self) {
                processSequenceExpr(leftSeq, parentOp: opName)
            } else {
                walk(node.leftOperand)
            }

            if let rightInfix = node.rightOperand.as(InfixOperatorExprSyntax.self) {
                processInfixBoolOps(rightInfix, parentOp: opName)
            } else if let rightSeq = node.rightOperand.as(SequenceExprSyntax.self) {
                processSequenceExpr(rightSeq, parentOp: opName)
            } else {
                walk(node.rightOperand)
            }
            return
        }

        walk(node.leftOperand)
        walk(node.rightOperand)
    }

    override func visit(_ node: FunctionCallExprSyntax) -> SyntaxVisitorContinueKind {
        if let calledIdentifier = node.calledExpression.as(DeclReferenceExprSyntax.self) {
            if calledIdentifier.baseName.text == functionName {
                addIncrement(node: node, type: "recursion", baseIncrement: 1, nestingPenalty: false, reason: "direct recursion call")
            }
        }
        return .visitChildren
    }
}
