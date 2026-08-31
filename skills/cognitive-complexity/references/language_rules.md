# Multi-Language AST Cognitive Complexity Mapping

This document details how Abstract Syntax Tree (AST) nodes map to Cognitive Complexity increments and nesting rules across **Python**, **TypeScript**, **Dart**, **Swift**, and **Kotlin**.

---

## 1. Python (`ast`)

| AST Node Type | Node Class | Increment (B1) | Nesting Impact (B2) | Notes |
| :--- | :--- | :--- | :--- | :--- |
| `if` statement | `ast.If` | `+1` | `+1` | Increments nesting level for children |
| `elif` branch | `ast.If` (in orelse) | `+1` | `+0` | Does not increase nesting over parent |
| `else` branch | `orelse` block | `+0` | `+0` | No increment |
| Ternary expression | `ast.IfExp` | `+1` | `+1` | `x if cond else y` |
| `for` loop | `ast.For`, `ast.AsyncFor` | `+1` | `+1` | Loops increment nesting |
| `while` loop | `ast.While` | `+1` | `+1` | While loops increment nesting |
| `except` handler | `ast.ExceptHandler` | `+1` | `+1` | Catches increment nesting |
| `match` statement | `ast.Match` | `+1` | `+1` | Python 3.10+ pattern matching |
| `case` clause | `ast.match_case` | `+0` / `+1` if guarded | `+0` | Guard `case pattern if cond` adds +1 |
| Boolean operations | `ast.BoolOp` (`And`/`Or`) | `+1` per sequence switch | `+0` | Continuous sequences count once |
| Comprehensions | `ast.comprehension` | `+1` per generator/if | `+1` | List/set/dict comprehensions |
| Nested function | `ast.FunctionDef`, `ast.AsyncFunctionDef` | `+0` | `+1` | Enclosed logic incurs nesting penalty |
| Lambda | `ast.Lambda` | `+0` | `+1` | Lambda body incurs nesting penalty |
| Recursion | `ast.Call` (self) | `+1` | `+0` | Calling enclosing function by name |

---

## 2. TypeScript / JavaScript (`typescript` Compiler API)

| AST Node Type | TypeScript `SyntaxKind` | Increment (B1) | Nesting Impact (B2) | Notes |
| :--- | :--- | :--- | :--- | :--- |
| `if` statement | `ts.SyntaxKind.IfStatement` | `+1` | `+1` | Increases nesting for body |
| `else if` | `IfStatement` in `elseStatement` | `+1` | `+0` | No extra nesting |
| Ternary operator | `ts.SyntaxKind.ConditionalExpression` | `+1` | `+1` | `cond ? a : b` |
| `switch` statement | `ts.SyntaxKind.SwitchStatement` | `+1` | `+1` | Increases nesting |
| `case` clause | `ts.SyntaxKind.CaseClause` | `+0` | `+0` | Handled by switch |
| `for` loops | `ForStatement`, `ForInStatement`, `ForOfStatement` | `+1` | `+1` | Increases nesting |
| `while` / `do..while` | `WhileStatement`, `DoWhileStatement` | `+1` | `+1` | Increases nesting |
| `catch` clause | `ts.SyntaxKind.CatchClause` | `+1` | `+1` | Catch block increments nesting |
| Boolean chains | `BinaryExpression` (`&&`, `\|\|`, `??`) | `+1` per sequence switch | `+0` | Continuous sequences count once |
| Nested functions | `ArrowFunction`, `FunctionExpression`, `MethodDeclaration` | `+0` | `+1` | Inner logic incurs nesting penalty |
| Labeled break/continue | `BreakStatement`, `ContinueStatement` (with label) | `+1` | `+0` | Non-local jumps |
| Recursion | `CallExpression` (self) | `+1` | `+0` | Calling enclosing function by name |

---

## 3. Dart (`package:analyzer`)

| AST Node Type | Dart Analyzer Class | Increment (B1) | Nesting Impact (B2) | Notes |
| :--- | :--- | :--- | :--- | :--- |
| `if` statement | `IfStatement` | `+1` | `+1` | Increases nesting for body |
| Collection `if` | `IfElement` | `+1` | `+1` | In list/set/map literals |
| Ternary expression | `ConditionalExpression` | `+1` | `+1` | `cond ? a : b` |
| `switch` statement | `SwitchStatement` | `+1` | `+1` | Traditional switch |
| `switch` expression | `SwitchExpression` | `+1` | `+1` | Dart 3 switch expression |
| `for` loop | `ForStatement`, `ForEachStatement` | `+1` | `+1` | Traditional and for-in |
| Collection `for` | `ForElement` | `+1` | `+1` | In collection literals |
| `while` / `do..while` | `WhileStatement`, `DoStatement` | `+1` | `+1` | Increases nesting |
| `catch` clause | `CatchClause` | `+1` | `+1` | Catch block |
| Binary operators | `BinaryExpression` (`&&`, `\|\|`, `??`) | `+1` per sequence switch | `+0` | Sequence tracking |
| Closures / nested fn | `FunctionExpression`, `BlockFunctionBody` | `+0` | `+1` | Increases nesting for enclosed code |
| Recursion | `MethodInvocation` / `FunctionExpressionInvocation` (self) | `+1` | `+0` | Self calls |

---

## 4. Swift (`SwiftSyntax`)

| AST Node Type | SwiftSyntax Class | Increment (B1) | Nesting Impact (B2) | Notes |
| :--- | :--- | :--- | :--- | :--- |
| `if` expression | `IfExprSyntax` | `+1` | `+1` | Increases nesting |
| `guard` statement | `GuardStmtSyntax` | `+1` | `+0` | Guard clause (early exit idiom) |
| Ternary operator | `TernaryExprSyntax` | `+1` | `+1` | `cond ? a : b` |
| `switch` expression | `SwitchExprSyntax` | `+1` | `+1` | Switch statement/expression |
| `switch` case | `SwitchCaseSyntax` | `+0` / `+1` with where | `+0` | `where` clause adds +1 |
| `for` loop | `ForStmtSyntax` | `+1` | `+1` | `for x in list` |
| `while` / `repeat` | `WhileStmtSyntax`, `RepeatWhileStmtSyntax` | `+1` | `+1` | Loops increment nesting |
| `catch` clause | `CatchClauseSyntax` | `+1` | `+1` | In `do..catch` |
| Binary operators | `InfixOperatorExprSyntax` (`&&`, `\|\|`, `??`) | `+1` per sequence switch | `+0` | Sequence tracking |
| Closures / nested fn | `ClosureExprSyntax`, `FunctionDeclSyntax` | `+0` | `+1` | Nested function bodies |
| Recursion | `FunctionCallExprSyntax` (self) | `+1` | `+0` | Direct recursion |

---

## 5. Kotlin (Kotlin PSI / `kotlin-compiler-embeddable`)

| AST Node Type | Kotlin PSI Class | Increment (B1) | Nesting Impact (B2) | Notes |
| :--- | :--- | :--- | :--- | :--- |
| `if` expression | `KtIfExpression` | `+1` | `+1` | Increases nesting |
| `when` expression | `KtWhenExpression` | `+1` | `+1` | Kotlin when expression |
| `when` entry | `KtWhenEntry` | `+0` / `+1` with condition | `+0` | Multi-case branching |
| `for` loop | `KtForExpression` | `+1` | `+1` | For loop |
| `while` / `do..while` | `KtWhileExpression`, `KtDoWhileExpression` | `+1` | `+1` | Loops increment nesting |
| `catch` clause | `KtCatchClause` | `+1` | `+1` | In `try..catch` |
| Binary operators | `KtBinaryExpression` (`&&`, `\|\|`, `?:`) | `+1` per sequence switch | `+0` | Elvis `?:` counts when branching |
| Lambdas / local fn | `KtLambdaExpression`, nested `KtNamedFunction` | `+0` | `+1` | Increases nesting for body |
| Labeled jumps | `KtBreakExpression`, `KtContinueExpression` (labeled) | `+1` | `+0` | `break@loop` |
| Recursion | `KtCallExpression` (self) | `+1` | `+0` | Direct recursion |
