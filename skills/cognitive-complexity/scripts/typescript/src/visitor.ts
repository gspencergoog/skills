import * as ts from "typescript";
import {
  ComplexityIncrement,
  ComplexityReport,
  FileComplexity,
  FunctionComplexity,
} from "./types";

export class TypeScriptComplexityAnalyzer {
  constructor(private threshold: number = 15) {}

  public analyzeSource(sourceCode: string, filePath: string = "<stdin>"): FileComplexity {
    const sourceFile = ts.createSourceFile(
      filePath,
      sourceCode,
      ts.ScriptTarget.Latest,
      true,
      filePath.endsWith(".tsx") || filePath.endsWith(".jsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS
    );

    const functions: FunctionComplexity[] = [];
    this.collectFunctions(sourceFile, sourceFile, undefined, functions);

    const totalComplexity = functions.reduce((acc, f) => acc + f.complexity, 0);
    const avgComplexity = functions.length ? totalComplexity / functions.length : 0;
    const highestComplexity = functions.reduce((max, f) => Math.max(max, f.complexity), 0);

    return {
      path: filePath,
      total_complexity: totalComplexity,
      average_complexity: Math.round(avgComplexity * 100) / 100,
      highest_complexity: highestComplexity,
      functions,
    };
  }

  private collectFunctions(
    node: ts.Node,
    sourceFile: ts.SourceFile,
    className: string | undefined,
    functions: FunctionComplexity[]
  ): void {
    if (ts.isClassDeclaration(node) || ts.isClassExpression(node) || ts.isInterfaceDeclaration(node)) {
      const currentClassName = node.name?.text || "AnonymousClass";
      const fullClassName = className ? `${className}.${currentClassName}` : currentClassName;
      ts.forEachChild(node, (child) => this.collectFunctions(child, sourceFile, fullClassName, functions));
      return;
    }

    const funcInfo = this.extractFunctionInfo(node, sourceFile, className);
    if (funcInfo) {
      functions.push(this.analyzeFunctionNode(funcInfo.node, sourceFile, funcInfo.name, className));
    }

    ts.forEachChild(node, (child) => this.collectFunctions(child, sourceFile, className, functions));
  }

  private extractFunctionInfo(
    node: ts.Node,
    sourceFile: ts.SourceFile,
    className?: string
  ): { node: ts.Node; name: string } | null {
    if (ts.isFunctionDeclaration(node) && node.name) {
      return { node, name: node.name.text };
    }
    if (ts.isMethodDeclaration(node) && node.name) {
      return { node, name: node.name.getText(sourceFile) };
    }
    if (ts.isConstructorDeclaration(node)) {
      return { node, name: "constructor" };
    }
    if (ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node)) {
      return { node, name: node.name.getText(sourceFile) };
    }
    if (ts.isVariableDeclaration(node) && node.initializer && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))) {
      return { node: node.initializer, name: node.name.getText(sourceFile) };
    }
    return null;
  }

  public analyzeFunctionNode(
    functionNode: ts.Node,
    sourceFile: ts.SourceFile,
    functionName: string,
    className?: string
  ): FunctionComplexity {
    let complexity = 0;
    const breakdown: ComplexityIncrement[] = [];

    const addIncrement = (node: ts.Node, type: string, base: number, nesting: number, reason: string) => {
      const total = base + nesting;
      complexity += total;
      const { line, character } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
      breakdown.push({
        line: line + 1,
        column: character + 1,
        type,
        increment: total,
        nesting,
        reason: `${reason} (+${base}${nesting > 0 ? ` + nesting ${nesting}` : ""} = +${total})`,
      });
    };

    const visitor = new FunctionAstVisitor(sourceFile, functionName, addIncrement);
    const body = (functionNode as any).body || functionNode;
    visitor.visit(body, 0);

    const { line: startLine } = sourceFile.getLineAndCharacterOfPosition(functionNode.getStart(sourceFile));
    const { line: endLine } = sourceFile.getLineAndCharacterOfPosition(functionNode.getEnd());

    return {
      name: functionName,
      class_name: className || null,
      line_number: startLine + 1,
      end_line_number: endLine + 1,
      complexity,
      exceeds_threshold: complexity > this.threshold,
      breakdown,
    };
  }
}

class FunctionAstVisitor {
  constructor(
    private sourceFile: ts.SourceFile,
    private functionName: string,
    private addIncrement: (node: ts.Node, type: string, base: number, nesting: number, reason: string) => void
  ) {}

  public visit(node: ts.Node, nesting: number): void {
    if (ts.isIfStatement(node)) {
      this.handleIf(node, nesting);
    } else if (ts.isConditionalExpression(node)) {
      this.handleConditional(node, nesting);
    } else if (ts.isForStatement(node) || ts.isForInStatement(node) || ts.isForOfStatement(node) || ts.isWhileStatement(node) || ts.isDoStatement(node)) {
      this.handleLoop(node, nesting);
    } else if (ts.isSwitchStatement(node)) {
      this.handleSwitch(node, nesting);
    } else if (ts.isCatchClause(node)) {
      this.handleCatch(node, nesting);
    } else if (ts.isBreakStatement(node) || ts.isContinueStatement(node)) {
      this.handleJump(node);
    } else if (ts.isCallExpression(node)) {
      this.handleCall(node, nesting);
    } else if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
      this.handleClosure(node, nesting);
    } else {
      ts.forEachChild(node, (child) => this.visit(child, nesting));
    }
  }

  private handleIf(node: ts.IfStatement, nesting: number): void {
    const isElseIf = node.parent && ts.isIfStatement(node.parent) && node.parent.elseStatement === node;
    if (isElseIf) {
      this.addIncrement(node, "else_if", 1, 0, "else if statement");
    } else {
      this.addIncrement(node, "if", 1, nesting, "if statement");
    }

    this.processBoolOps(node.expression, undefined);
    this.visit(node.thenStatement, isElseIf ? nesting : nesting + 1);

    if (node.elseStatement) {
      if (ts.isIfStatement(node.elseStatement)) {
        this.visit(node.elseStatement, nesting);
      } else {
        this.visit(node.elseStatement, nesting + 1);
      }
    }
  }

  private handleConditional(node: ts.ConditionalExpression, nesting: number): void {
    this.addIncrement(node, "ternary", 1, nesting, "ternary conditional expression");
    this.processBoolOps(node.condition, undefined);
    this.visit(node.whenTrue, nesting + 1);
    this.visit(node.whenFalse, nesting + 1);
  }

  private handleLoop(node: ts.Node, nesting: number): void {
    this.addIncrement(node, "loop", 1, nesting, "loop statement");
    ts.forEachChild(node, (child) => {
      if (child === (node as any).statement) {
        this.visit(child, nesting + 1);
      } else {
        this.visit(child, nesting);
      }
    });
  }

  private handleSwitch(node: ts.SwitchStatement, nesting: number): void {
    this.addIncrement(node, "switch", 1, nesting, "switch statement");
    this.visit(node.expression, nesting);
    this.visit(node.caseBlock, nesting + 1);
  }

  private handleCatch(node: ts.CatchClause, nesting: number): void {
    this.addIncrement(node, "catch", 1, nesting, "catch block");
    this.visit(node.block, nesting + 1);
  }

  private handleJump(node: ts.BreakStatement | ts.ContinueStatement): void {
    if (node.label) {
      this.addIncrement(node, "labeled_jump", 1, 0, "labeled break/continue");
    }
  }

  private handleCall(node: ts.CallExpression, nesting: number): void {
    if (ts.isIdentifier(node.expression) && node.expression.text === this.functionName) {
      this.addIncrement(node, "recursion", 1, 0, "direct recursion call");
    }
    ts.forEachChild(node, (child) => this.visit(child, nesting));
  }

  private handleClosure(node: ts.Node, nesting: number): void {
    const body = (node as any).body;
    if (body) {
      this.visit(body, nesting + 1);
    }
  }

  private unwrapParentheses(node: ts.Node): ts.Node {
    let current = node;
    while (ts.isParenthesizedExpression(current)) {
      current = current.expression;
    }
    return current;
  }

  private processBoolOps(node: ts.Node, currentOp: ts.SyntaxKind | undefined): void {
    const expr = this.unwrapParentheses(node);
    if (!ts.isBinaryExpression(expr)) {
      this.visit(expr, 0);
      return;
    }

    const op = expr.operatorToken.kind;
    const isLogical = op === ts.SyntaxKind.AmpersandAmpersandToken || op === ts.SyntaxKind.BarBarToken || op === ts.SyntaxKind.QuestionQuestionToken;

    if (!isLogical) {
      this.visit(expr.left, 0);
      this.visit(expr.right, 0);
      return;
    }

    const opName = op === ts.SyntaxKind.AmpersandAmpersandToken ? "&&" : op === ts.SyntaxKind.BarBarToken ? "||" : "??";
    if (currentOp === undefined) {
      this.addIncrement(expr, "bool_op_sequence", 1, 0, `boolean operator sequence (${opName})`);
    } else if (currentOp !== op) {
      this.addIncrement(expr, "bool_op_switch", 1, 0, `boolean operator switch to (${opName})`);
    }

    this.processBoolOps(expr.left, op);
    this.processBoolOps(expr.right, op);
  }
}
