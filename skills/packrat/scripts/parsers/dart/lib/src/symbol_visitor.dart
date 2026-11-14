import 'package:analyzer/dart/analysis/utilities.dart';
import 'package:analyzer/dart/ast/ast.dart';
import 'package:analyzer/dart/ast/token.dart';
import 'package:analyzer/dart/ast/visitor.dart';

import 'protocol.dart';
import 'source_utils.dart';

/// Parses [source] with `package:analyzer` and extracts top-level and member
/// [SymbolRecord] declarations without descending into executable bodies.
DartParseResult parseDartSource(String source, {String path = ''}) {
  final parseResult = parseString(
    content: source,
    path: path.isEmpty ? null : path,
    throwIfDiagnostics: false,
  );
  final lineInfo = LfLineInfo(source);
  final visitor = DartSymbolVisitor(source: source, lineInfo: lineInfo);
  parseResult.unit.accept(visitor);
  final (symbols, dropped) = _finalizeSymbols(source, visitor.symbols);
  return DartParseResult(
    symbols: symbols,
    syntaxErrorCount: parseResult.errors.length,
    droppedSymbolCount: dropped,
  );
}

(List<SymbolRecord>, int) _finalizeSymbols(
  String source,
  List<SymbolRecord> rawSymbols,
) {
  final lines = splitLinesLf(source);
  final generated = isGeneratedSource(source, lines);
  final cap = generated ? generatedSymbolCap : normalSymbolCap;
  final kept = <SymbolRecord>[];
  var dropped = 0;
  for (final sym in rawSymbols) {
    if (generated && sym.parent.isNotEmpty) {
      dropped += 1;
      continue;
    }
    kept.add(sym);
  }
  kept.sort(_compareSymbols);
  if (kept.length > cap) {
    dropped += kept.length - cap;
    return (kept.sublist(0, cap), dropped);
  }
  return (kept, dropped);
}

int _compareSymbols(SymbolRecord a, SymbolRecord b) {
  final byStart = a.lineStart.compareTo(b.lineStart);
  if (byStart != 0) return byStart;
  final byEnd = a.lineEnd.compareTo(b.lineEnd);
  if (byEnd != 0) return byEnd;
  final byName = a.qualifiedName.compareTo(b.qualifiedName);
  if (byName != 0) return byName;
  return a.kind.compareTo(b.kind);
}

/// AST visitor collecting Dart module-level and type-member declarations.
///
/// Executable bodies and variable initializers are intentionally not traversed
/// so local functions, closures, and lambdas are omitted.
final class DartSymbolVisitor extends RecursiveAstVisitor<void> {
  /// Full source text of the compilation unit being visited.
  final String source;

  /// LF-only 1-based line lookup table for [source].
  final LfLineInfo lineInfo;

  /// Collected symbols in AST visitation order.
  final List<SymbolRecord> symbols = <SymbolRecord>[];

  String _currentParent = '';

  /// Creates a visitor over [source] and [lineInfo].
  DartSymbolVisitor({required this.source, required this.lineInfo});

  String _qualify(String name) =>
      _currentParent.isEmpty ? name : '$_currentParent.$name';

  void _withParent(String qualifiedParent, void Function() visitMembers) {
    final previous = _currentParent;
    _currentParent = qualifiedParent;
    try {
      visitMembers();
    } finally {
      _currentParent = previous;
    }
  }

  void _emitDeclaration({
    required AnnotatedNode node,
    required String kind,
    required String name,
    required int signatureEndOffset,
    Comment? docOverride,
    int? beginOffsetOverride,
    int? endOffsetOverride,
    String? signatureOverride,
  }) {
    final (lineStart, lineEnd) = declarationLineSpan(
      node,
      lineInfo,
      beginOffsetOverride: beginOffsetOverride,
      endOffsetOverride: endOffsetOverride,
    );
    final signature = signatureOverride ??
        sliceSignature(
          source,
          node.firstTokenAfterCommentAndMetadata.offset,
          signatureEndOffset,
          name,
        );
    symbols.add(
      SymbolRecord(
        kind: kind,
        name: name,
        qualifiedName: _qualify(name),
        parent: _currentParent,
        signature: signature,
        docstring: extractDocstring(docOverride ?? node.documentationComment),
        lineStart: lineStart,
        lineEnd: lineEnd,
        visibility: dartVisibility(name),
      ),
    );
  }

  int _functionBodyOpenerOffset(FunctionBody body, int fallbackEnd) {
    if (body is BlockFunctionBody) {
      return body.block.leftBracket.offset;
    }
    if (body is ExpressionFunctionBody) {
      return body.functionDefinition.offset;
    }
    if (body is EmptyFunctionBody) {
      return body.semicolon.offset;
    }
    final begin = body.beginToken.offset;
    return begin <= fallbackEnd ? begin : fallbackEnd;
  }

  @override
  void visitClassDeclaration(ClassDeclaration node) {
    final name = node.namePart.typeName.lexeme.trim();
    if (name.isEmpty) return;
    final qname = _qualify(name);
    _emitDeclaration(
      node: node,
      kind: 'class',
      name: name,
      signatureEndOffset: node.body.beginToken.offset,
    );
    _withParent(qname, () => node.body.accept(this));
  }

  @override
  void visitClassTypeAlias(ClassTypeAlias node) {
    final name = node.name.lexeme.trim();
    if (name.isEmpty) return;
    _emitDeclaration(
      node: node,
      kind: 'class',
      name: name,
      signatureEndOffset: node.semicolon.offset,
    );
  }

  @override
  void visitMixinDeclaration(MixinDeclaration node) {
    final name = node.name.lexeme.trim();
    if (name.isEmpty) return;
    final qname = _qualify(name);
    _emitDeclaration(
      node: node,
      kind: 'mixin',
      name: name,
      signatureEndOffset: node.body.beginToken.offset,
    );
    _withParent(qname, () => node.body.accept(this));
  }

  @override
  void visitEnumDeclaration(EnumDeclaration node) {
    final name = node.namePart.typeName.lexeme.trim();
    if (name.isEmpty) return;
    final qname = _qualify(name);
    _emitDeclaration(
      node: node,
      kind: 'enum',
      name: name,
      signatureEndOffset: node.body.beginToken.offset,
    );
    _withParent(qname, () => node.body.accept(this));
  }

  @override
  void visitEnumConstantDeclaration(EnumConstantDeclaration node) {
    final name = node.name.lexeme.trim();
    if (name.isEmpty) return;
    _emitDeclaration(
      node: node,
      kind: 'enum_value',
      name: name,
      signatureEndOffset: node.end,
    );
  }

  @override
  void visitExtensionDeclaration(ExtensionDeclaration node) {
    final (lineStart, _) = declarationLineSpan(node, lineInfo);
    final rawName = node.name?.lexeme.trim() ?? '';
    final name = rawName.isEmpty ? 'extension@L$lineStart' : rawName;
    final qname = _qualify(name);
    _emitDeclaration(
      node: node,
      kind: 'extension',
      name: name,
      signatureEndOffset: node.body.beginToken.offset,
    );
    _withParent(qname, () => node.body.accept(this));
  }

  @override
  void visitExtensionTypeDeclaration(ExtensionTypeDeclaration node) {
    final name = node.namePart.typeName.lexeme.trim();
    if (name.isEmpty) return;
    final qname = _qualify(name);
    _emitDeclaration(
      node: node,
      kind: 'extension_type',
      name: name,
      signatureEndOffset: node.body.beginToken.offset,
    );
    _withParent(qname, () => node.body.accept(this));
  }

  @override
  void visitGenericTypeAlias(GenericTypeAlias node) {
    final name = node.name.lexeme.trim();
    if (name.isEmpty) return;
    _emitDeclaration(
      node: node,
      kind: 'typedef',
      name: name,
      signatureEndOffset: node.semicolon.offset,
    );
  }

  @override
  void visitFunctionTypeAlias(FunctionTypeAlias node) {
    final name = node.name.lexeme.trim();
    if (name.isEmpty) return;
    _emitDeclaration(
      node: node,
      kind: 'typedef',
      name: name,
      signatureEndOffset: node.semicolon.offset,
    );
  }

  @override
  void visitFunctionDeclaration(FunctionDeclaration node) {
    if (node.parent is! CompilationUnit || _currentParent.isNotEmpty) return;
    final name = node.name.lexeme.trim();
    if (name.isEmpty) return;
    final kind =
        node.isGetter ? 'getter' : (node.isSetter ? 'setter' : 'function');
    final sigEnd = _functionBodyOpenerOffset(
      node.functionExpression.body,
      node.end,
    );
    _emitDeclaration(
      node: node,
      kind: kind,
      name: name,
      signatureEndOffset: sigEnd,
    );
  }

  @override
  void visitMethodDeclaration(MethodDeclaration node) {
    final name = node.name.lexeme.trim();
    if (name.isEmpty) return;
    final kind =
        node.isGetter ? 'getter' : (node.isSetter ? 'setter' : 'method');
    final sigEnd = _functionBodyOpenerOffset(node.body, node.end);
    _emitDeclaration(
      node: node,
      kind: kind,
      name: name,
      signatureEndOffset: sigEnd,
    );
  }

  @override
  void visitConstructorDeclaration(ConstructorDeclaration node) {
    final rawName = node.name?.lexeme.trim() ?? '';
    if (node.period != null && node.newKeyword == null && rawName.isEmpty) {
      return;
    }
    final name = (rawName.isEmpty || rawName == 'new') ? 'new' : rawName;
    final sigEnd = _functionBodyOpenerOffset(node.body, node.end);
    _emitDeclaration(
      node: node,
      kind: 'constructor',
      name: name,
      signatureEndOffset: sigEnd,
    );
  }

  @override
  void visitFieldDeclaration(FieldDeclaration node) {
    _emitVariableList(
      outerNode: node,
      list: node.fields,
      semicolon: node.semicolon,
      kind: 'field',
    );
  }

  @override
  void visitTopLevelVariableDeclaration(TopLevelVariableDeclaration node) {
    _emitVariableList(
      outerNode: node,
      list: node.variables,
      semicolon: node.semicolon,
      kind: 'variable',
    );
  }

  void _emitVariableList({
    required AnnotatedNode outerNode,
    required VariableDeclarationList list,
    required Token semicolon,
    required String kind,
  }) {
    final vars = list.variables;
    if (vars.isEmpty) return;
    if (vars.length == 1) {
      _emitSingleVariable(outerNode, vars.first, semicolon, kind);
      return;
    }
    _emitMultiVariables(outerNode, list, vars, kind);
  }

  void _emitSingleVariable(
    AnnotatedNode outerNode,
    VariableDeclaration variable,
    Token semicolon,
    String kind,
  ) {
    final name = variable.name.lexeme.trim();
    if (name.isEmpty) return;
    _emitDeclaration(
      node: outerNode,
      kind: kind,
      name: name,
      signatureEndOffset: semicolon.offset,
      docOverride:
          variable.documentationComment ?? outerNode.documentationComment,
    );
  }

  void _emitMultiVariables(
    AnnotatedNode outerNode,
    VariableDeclarationList list,
    NodeList<VariableDeclaration> vars,
    String kind,
  ) {
    final headerStart = outerNode.firstTokenAfterCommentAndMetadata.offset;
    final prefixEnd = list.type?.end ??
        list.keyword?.end ??
        outerNode.firstTokenAfterCommentAndMetadata.end;
    final prefix = sliceSignature(source, headerStart, prefixEnd, '');
    for (var i = 0; i < vars.length; i++) {
      final variable = vars[i];
      final name = variable.name.lexeme.trim();
      if (name.isEmpty) continue;
      final varSlice = sliceSignature(
        source,
        variable.name.offset,
        variable.end,
        name,
      );
      final combined = collapseWhitespace('$prefix $varSlice');
      final beginOffset = i == 0
          ? declarationBeginToken(outerNode).offset
          : declarationBeginToken(variable).offset;
      final endOffset = i == vars.length - 1 ? outerNode.end : variable.end;
      _emitDeclaration(
        node: outerNode,
        kind: kind,
        name: name,
        signatureEndOffset: variable.end,
        docOverride:
            variable.documentationComment ?? outerNode.documentationComment,
        beginOffsetOverride: beginOffset,
        endOffsetOverride: endOffset,
        signatureOverride: combined.isEmpty ? name : combined,
      );
    }
  }
}
