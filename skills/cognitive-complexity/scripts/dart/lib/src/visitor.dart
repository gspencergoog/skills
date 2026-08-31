import 'package:analyzer/dart/analysis/utilities.dart';
import 'package:analyzer/dart/ast/ast.dart';
import 'package:analyzer/dart/ast/token.dart';
import 'package:analyzer/dart/ast/visitor.dart';
import 'package:analyzer/source/line_info.dart';

import 'models.dart';

class DartComplexityAnalyzer {
  final int threshold;

  const DartComplexityAnalyzer({this.threshold = 15});

  FileComplexity analyzeSource(String sourceCode, {String filePath = '<stdin>'}) {
    final parseResult = parseString(content: sourceCode, throwIfDiagnostics: false);
    final unit = parseResult.unit;
    final lineInfo = parseResult.lineInfo;

    final collector = _FunctionCollector(lineInfo: lineInfo, threshold: threshold);
    unit.accept(collector);

    final functions = collector.functions;
    final totalComplexity = functions.fold<int>(0, (sum, f) => sum + f.complexity);
    final avgComplexity = functions.isNotEmpty ? totalComplexity / functions.length : 0.0;
    final highestComplexity = functions.fold<int>(0, (max, f) => f.complexity > max ? f.complexity : max);

    return FileComplexity(
      path: filePath,
      totalComplexity: totalComplexity,
      averageComplexity: avgComplexity,
      highestComplexity: highestComplexity,
      functions: functions,
    );
  }
}

class _FunctionCollector extends RecursiveAstVisitor<void> {
  final LineInfo lineInfo;
  final int threshold;
  final List<FunctionComplexity> functions = [];
  String? _currentClass;

  _FunctionCollector({required this.lineInfo, required this.threshold});

  @override
  void visitClassDeclaration(ClassDeclaration node) {
    final previousClass = _currentClass;
    _currentClass = node.name.lexeme;
    super.visitClassDeclaration(node);
    _currentClass = previousClass;
  }

  @override
  void visitMixinDeclaration(MixinDeclaration node) {
    final previousClass = _currentClass;
    _currentClass = node.name.lexeme;
    super.visitMixinDeclaration(node);
    _currentClass = previousClass;
  }

  @override
  void visitExtensionDeclaration(ExtensionDeclaration node) {
    final previousClass = _currentClass;
    _currentClass = node.name?.lexeme ?? '<anonymous_extension>';
    super.visitExtensionDeclaration(node);
    _currentClass = previousClass;
  }

  @override
  void visitFunctionDeclaration(FunctionDeclaration node) {
    final funcName = node.name.lexeme;
    final visitor = _FunctionComplexityVisitor(
      functionName: funcName,
      lineInfo: lineInfo,
      threshold: threshold,
    );
    node.functionExpression.body.accept(visitor);

    final startLine = lineInfo.getLocation(node.offset).lineNumber;
    final endLine = lineInfo.getLocation(node.end).lineNumber;

    functions.add(FunctionComplexity(
      name: funcName,
      className: _currentClass,
      lineNumber: startLine,
      endLineNumber: endLine,
      complexity: visitor.complexity,
      exceedsThreshold: visitor.complexity > threshold,
      breakdown: visitor.breakdown,
    ));

    super.visitFunctionDeclaration(node);
  }

  @override
  void visitMethodDeclaration(MethodDeclaration node) {
    final funcName = node.name.lexeme;
    final visitor = _FunctionComplexityVisitor(
      functionName: funcName,
      lineInfo: lineInfo,
      threshold: threshold,
    );
    node.body.accept(visitor);

    final startLine = lineInfo.getLocation(node.offset).lineNumber;
    final endLine = lineInfo.getLocation(node.end).lineNumber;

    functions.add(FunctionComplexity(
      name: funcName,
      className: _currentClass,
      lineNumber: startLine,
      endLineNumber: endLine,
      complexity: visitor.complexity,
      exceedsThreshold: visitor.complexity > threshold,
      breakdown: visitor.breakdown,
    ));

    super.visitMethodDeclaration(node);
  }

  @override
  void visitConstructorDeclaration(ConstructorDeclaration node) {
    final funcName = node.name?.lexeme ?? 'new';
    final visitor = _FunctionComplexityVisitor(
      functionName: funcName,
      lineInfo: lineInfo,
      threshold: threshold,
    );
    node.body.accept(visitor);

    final startLine = lineInfo.getLocation(node.offset).lineNumber;
    final endLine = lineInfo.getLocation(node.end).lineNumber;

    functions.add(FunctionComplexity(
      name: funcName,
      className: _currentClass,
      lineNumber: startLine,
      endLineNumber: endLine,
      complexity: visitor.complexity,
      exceedsThreshold: visitor.complexity > threshold,
      breakdown: visitor.breakdown,
    ));

    super.visitConstructorDeclaration(node);
  }
}

class _FunctionComplexityVisitor extends RecursiveAstVisitor<void> {
  final String functionName;
  final LineInfo lineInfo;
  final int threshold;

  int complexity = 0;
  int _currentNesting = 0;
  final List<ComplexityIncrement> breakdown = [];

  _FunctionComplexityVisitor({
    required this.functionName,
    required this.lineInfo,
    required this.threshold,
  });

  void _addIncrement(
    AstNode node,
    String nodeType,
    int baseIncrement,
    bool nestingPenalty,
    String reason,
  ) {
    final penalty = nestingPenalty ? _currentNesting : 0;
    final totalInc = baseIncrement + penalty;
    complexity += totalInc;

    final loc = lineInfo.getLocation(node.offset);
    final detail =
        '$reason (+$baseIncrement${penalty > 0 ? ' + nesting $penalty' : ''} = +$totalInc)';

    breakdown.add(ComplexityIncrement(
      line: loc.lineNumber,
      column: loc.columnNumber,
      nodeType: nodeType,
      increment: totalInc,
      nesting: _currentNesting,
      reason: detail,
    ));
  }

  void _processBoolOps(Expression expr, TokenType? parentOp) {
    Expression unwrapped = expr;
    while (unwrapped is ParenthesizedExpression) {
      unwrapped = unwrapped.expression;
    }

    if (unwrapped is BinaryExpression) {
      final op = unwrapped.operator.type;
      final isLogical = op == TokenType.AMPERSAND_AMPERSAND ||
          op == TokenType.BAR_BAR ||
          op == TokenType.QUESTION_QUESTION;

      if (isLogical) {
        final opName = op == TokenType.AMPERSAND_AMPERSAND
            ? '&&'
            : op == TokenType.BAR_BAR
                ? '||'
                : '??';

        if (parentOp == null) {
          _addIncrement(
              unwrapped, 'bool_op_sequence', 1, false, 'boolean operator sequence ($opName)');
        } else if (parentOp != op) {
          _addIncrement(
              unwrapped, 'bool_op_switch', 1, false, 'boolean operator switch to ($opName)');
        }

        _processBoolOps(unwrapped.leftOperand, op);
        _processBoolOps(unwrapped.rightOperand, op);
        return;
      }
    }
    unwrapped.accept(this);
  }

  @override
  void visitIfStatement(IfStatement node) {
    final parent = node.parent;
    final isElseIf = parent is IfStatement && parent.elseStatement == node;

    if (isElseIf) {
      _addIncrement(node, 'else_if', 1, false, 'else if branch');
    } else {
      _addIncrement(node, 'if', 1, true, 'if statement');
    }

    _processBoolOps(node.expression, null);

    _currentNesting++;
    node.thenStatement.accept(this);
    _currentNesting--;

    node.elseStatement?.accept(this);
  }

  @override
  void visitIfElement(IfElement node) {
    final parent = node.parent;
    final isElseIf = parent is IfElement && parent.elseElement == node;

    if (isElseIf) {
      _addIncrement(node, 'else_if_element', 1, false, 'else if element');
    } else {
      _addIncrement(node, 'if_element', 1, true, 'collection if element');
    }

    _processBoolOps(node.expression, null);

    _currentNesting++;
    node.thenElement.accept(this);
    _currentNesting--;

    node.elseElement?.accept(this);
  }

  @override
  void visitConditionalExpression(ConditionalExpression node) {
    _addIncrement(node, 'ternary', 1, true, 'ternary conditional expression');
    _processBoolOps(node.condition, null);

    _currentNesting++;
    node.thenExpression.accept(this);
    node.elseExpression.accept(this);
    _currentNesting--;
  }

  @override
  void visitSwitchStatement(SwitchStatement node) {
    _addIncrement(node, 'switch', 1, true, 'switch statement');
    node.expression.accept(this);

    _currentNesting++;
    for (final member in node.members) {
      member.accept(this);
    }
    _currentNesting--;
  }

  @override
  void visitSwitchExpression(SwitchExpression node) {
    _addIncrement(node, 'switch_expression', 1, true, 'switch expression');
    node.expression.accept(this);

    _currentNesting++;
    for (final member in node.cases) {
      member.accept(this);
    }
    _currentNesting--;
  }

  @override
  void visitForStatement(ForStatement node) {
    _addIncrement(node, 'for_loop', 1, true, 'for loop');
    _currentNesting++;
    node.body.accept(this);
    _currentNesting--;
  }

  @override
  void visitForElement(ForElement node) {
    _addIncrement(node, 'for_element', 1, true, 'collection for element');
    _currentNesting++;
    node.body.accept(this);
    _currentNesting--;
  }

  @override
  void visitWhileStatement(WhileStatement node) {
    _addIncrement(node, 'while_loop', 1, true, 'while loop');
    _currentNesting++;
    node.body.accept(this);
    _currentNesting--;
  }

  @override
  void visitDoStatement(DoStatement node) {
    _addIncrement(node, 'do_while_loop', 1, true, 'do-while loop');
    _currentNesting++;
    node.body.accept(this);
    _currentNesting--;
  }

  @override
  void visitCatchClause(CatchClause node) {
    _addIncrement(node, 'catch', 1, true, 'catch clause');
    _currentNesting++;
    node.body.accept(this);
    _currentNesting--;
  }

  @override
  void visitFunctionExpression(FunctionExpression node) {
    _currentNesting++;
    node.body.accept(this);
    _currentNesting--;
  }

  @override
  void visitMethodInvocation(MethodInvocation node) {
    if (node.methodName.name == functionName) {
      _addIncrement(node, 'recursion', 1, false, 'direct recursion call');
    }
    super.visitMethodInvocation(node);
  }

  @override
  void visitFunctionExpressionInvocation(FunctionExpressionInvocation node) {
    final function = node.function;
    if (function is SimpleIdentifier && function.name == functionName) {
      _addIncrement(node, 'recursion', 1, false, 'direct recursion call');
    }
    super.visitFunctionExpressionInvocation(node);
  }
}
