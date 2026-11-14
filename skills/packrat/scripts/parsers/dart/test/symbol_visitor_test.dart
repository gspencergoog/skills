import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:checks/checks.dart';
import 'package:packrat_dart/packrat_dart.dart';
import 'package:path/path.dart' as p;
import 'package:test/test.dart';

void main() {
  test('extracts all Dart symbol kinds and omits local functions',
      _testSampleFixture);
  test('strips /// and /** */ docstrings cleanly', _testDocComments);
  test('emits distinct getter and setter on the same line',
      _testSameLineAccessorPair);
  test('includes @annotations in line_start while excluding doc comments',
      _testAnnotationSpan);
  test('normalizes unnamed, explicit .new, and named constructors',
      _testConstructors);
  test('formats unnamed extensions as extension@L<line_start>',
      _testUnnamedExtension);
  test('counts lines using LF and CRLF only (form feed stays on same line)',
      _testFormFeedLines);
  test('recovers from syntax errors with syntax_error_count >= 1',
      _testSyntaxErrorFixture);
  test('filters member symbols and caps at 200 on generated one-liners',
      _testGeneratedOneLiner);
  test('serves NDJSON worker batch and enforces 50 ms timeout on deep_nesting',
      _testWorkerAndTimeout);
}

SymbolRecord _findSymbol(
  List<SymbolRecord> symbols,
  String kind,
  String qualifiedName,
) {
  return symbols.firstWhere(
    (s) => s.kind == kind && s.qualifiedName == qualifiedName,
  );
}

void _testSampleFixture() {
  final sampleFile = File(p.join('fixtures', 'sample_symbols.dart'));
  final parsed = parseDartSource(
    sampleFile.readAsStringSync(),
    path: 'fixtures/sample_symbols.dart',
  );

  check(parsed.syntaxErrorCount).equals(0);
  check(parsed.droppedSymbolCount).equals(0);

  final names = parsed.symbols.map((s) => s.name).toSet();
  check(names.contains('localFormatter')).isFalse();
  check(names.contains('nestedLocal')).isFalse();
  check(names.contains('mapper')).isFalse();

  final defLimit = _findSymbol(parsed.symbols, 'variable', 'defaultLimit');
  check(defLimit.visibility).equals('public');
  check(defLimit.docstring).equals('Default maximum item count.');
  check(defLimit.parent).equals('');

  final privFlag = _findSymbol(parsed.symbols, 'variable', '_internalFlag');
  check(privFlag.visibility).equals('private');

  final jsonAlias = _findSymbol(parsed.symbols, 'typedef', 'JsonMap');
  check(jsonAlias.signature).equals('typedef JsonMap = Map<String, Object?>');

  final fnAlias = _findSymbol(parsed.symbols, 'typedef', 'ItemPredicate');
  check(fnAlias.signature).equals('typedef bool ItemPredicate<T>(T item)');

  final sev = _findSymbol(parsed.symbols, 'enum', 'Severity');
  check(sev.docstring).equals(
    'Supported badge severities.\n\nOrdered from lowest to highest.',
  );
  final sevInfo = _findSymbol(parsed.symbols, 'enum_value', 'Severity.info');
  check(sevInfo.parent).equals('Severity');
  check(sevInfo.docstring).equals('Informational badge.');
  _findSymbol(parsed.symbols, 'enum_value', 'Severity.warning');

  final mixinSym = _findSymbol(parsed.symbols, 'mixin', 'DisposableMixin');
  check(mixinSym.signature).equals('base mixin DisposableMixin');
  final mixinField = _findSymbol(
    parsed.symbols,
    'field',
    'DisposableMixin._disposed',
  );
  check(mixinField.visibility).equals('private');
  _findSymbol(parsed.symbols, 'method', 'DisposableMixin.dispose');

  final card = _findSymbol(parsed.symbols, 'class', 'WidgetCard');
  check(card.signature).equals('class WidgetCard with DisposableMixin');
  _findSymbol(parsed.symbols, 'field', 'WidgetCard.title');
  _findSymbol(parsed.symbols, 'constructor', 'WidgetCard.new');
  _findSymbol(parsed.symbols, 'constructor', 'WidgetCard.compact');
  final privCtor = _findSymbol(
    parsed.symbols,
    'constructor',
    'WidgetCard._internal',
  );
  check(privCtor.visibility).equals('private');
  _findSymbol(parsed.symbols, 'getter', 'WidgetCard.counter');
  _findSymbol(parsed.symbols, 'setter', 'WidgetCard.counter');
  _findSymbol(parsed.symbols, 'method', 'WidgetCard.render');
  _findSymbol(parsed.symbols, 'method', 'WidgetCard.+');

  _findSymbol(parsed.symbols, 'extension', 'StringTrimExt');
  _findSymbol(parsed.symbols, 'method', 'StringTrimExt.collapseTrim');

  final entityId = _findSymbol(parsed.symbols, 'extension_type', 'EntityId');
  check(entityId.signature).equals(
    'extension type const EntityId(int raw) implements int',
  );
  _findSymbol(parsed.symbols, 'getter', 'EntityId.isPositive');

  _findSymbol(parsed.symbols, 'getter', 'globalSeed');
  _findSymbol(parsed.symbols, 'setter', 'globalSeed');
  final summarize = _findSymbol(parsed.symbols, 'function', 'summarizeCards');
  check(summarize.signature).equals(
    'int summarizeCards( List<WidgetCard> cards, { bool uppercase = false, })',
  );
  final privFn = _findSymbol(parsed.symbols, 'function', '_privateHelper');
  check(privFn.visibility).equals('private');

  final expectedGolden = jsonDecode(
    File(p.join('fixtures', 'sample_symbols.expected.json')).readAsStringSync(),
  ) as List<Object?>;
  final actualWire = parsed.symbols.map((s) => s.toJson()).toList();
  check(jsonEncode(actualWire)).equals(jsonEncode(expectedGolden));
}

void _testDocComments() {
  const source = '''
/// Single line one.
/// Single line two.
void slashDoc() {}

/**
 * Block paragraph one.
 *
 * Block paragraph two.
 */
void blockDoc() {}

/** Inline block doc. */
void inlineBlockDoc() {}
''';
  final parsed = parseDartSource(source);
  final slash = _findSymbol(parsed.symbols, 'function', 'slashDoc');
  check(slash.docstring).equals('Single line one.\nSingle line two.');
  check(slash.lineStart).equals(3);

  final block = _findSymbol(parsed.symbols, 'function', 'blockDoc');
  check(block.docstring).equals('Block paragraph one.\n\nBlock paragraph two.');
  check(block.lineStart).equals(10);

  final inline = _findSymbol(parsed.symbols, 'function', 'inlineBlockDoc');
  check(inline.docstring).equals('Inline block doc.');
  check(inline.lineStart).equals(13);
}

void _testSameLineAccessorPair() {
  const source =
      'class Box {\n  int _x = 0;\n  int get x => _x; set x(int v) { _x = v; }\n}\n';
  final parsed = parseDartSource(source);
  final getter = _findSymbol(parsed.symbols, 'getter', 'Box.x');
  final setter = _findSymbol(parsed.symbols, 'setter', 'Box.x');
  check(getter.lineStart).equals(3);
  check(setter.lineStart).equals(3);
  check(getter.signature).equals('int get x');
  check(setter.signature).equals('set x(int v)');
}

void _testAnnotationSpan() {
  const source = '''
/// Documented method.
@Deprecated('old')
@override
void annotatedFn(
  int value,
) {
  return;
}
''';
  final parsed = parseDartSource(source);
  final fn = _findSymbol(parsed.symbols, 'function', 'annotatedFn');
  check(fn.lineStart).equals(2);
  check(fn.lineEnd).equals(8);
  check(fn.signature).equals('void annotatedFn( int value, )');
  check(fn.docstring).equals('Documented method.');
}

void _testConstructors() {
  const source = '''
class ExplicitNew {
  ExplicitNew.new();
  ExplicitNew.named(int a);
}

class ImplicitNew {
  const ImplicitNew();
}

mixin M {}
class AliasClass = Object with M;
''';
  final parsed = parseDartSource(source);
  final c1 = _findSymbol(parsed.symbols, 'constructor', 'ExplicitNew.new');
  check(c1.name).equals('new');
  final c2 = _findSymbol(parsed.symbols, 'constructor', 'ExplicitNew.named');
  check(c2.name).equals('named');
  final c3 = _findSymbol(parsed.symbols, 'constructor', 'ImplicitNew.new');
  check(c3.name).equals('new');
  final alias = _findSymbol(parsed.symbols, 'class', 'AliasClass');
  check(alias.signature).equals('class AliasClass = Object with M');
}

void _testUnnamedExtension() {
  const source = '''
// Header comment on line 1.
/// Extension docs on line 2.
extension on String {
  bool get isBlank => trim().isEmpty;
}
''';
  final parsed = parseDartSource(source);
  final ext = _findSymbol(parsed.symbols, 'extension', 'extension@L3');
  check(ext.name).equals('extension@L3');
  check(ext.lineStart).equals(3);
  check(ext.lineEnd).equals(5);
  check(ext.docstring).equals('Extension docs on line 2.');

  final member = _findSymbol(parsed.symbols, 'getter', 'extension@L3.isBlank');
  check(member.parent).equals('extension@L3');
}

void _testFormFeedLines() {
  const source = 'int a = 1;\fint b = 2;\r\nvoid onLineTwo() {}\n';
  final parsed = parseDartSource(source);
  final a = _findSymbol(parsed.symbols, 'variable', 'a');
  final b = _findSymbol(parsed.symbols, 'variable', 'b');
  final fn = _findSymbol(parsed.symbols, 'function', 'onLineTwo');
  check(a.lineStart).equals(1);
  check(b.lineStart).equals(1);
  check(fn.lineStart).equals(2);
  check(fn.lineEnd).equals(2);
}

void _testSyntaxErrorFixture() {
  final file = File(p.join('fixtures', 'conformance', 'syntax_error.dart'));
  final parsed = parseDartSource(
    file.readAsStringSync(),
    path: 'fixtures/conformance/syntax_error.dart',
  );
  check(parsed.syntaxErrorCount).isGreaterOrEqual(1);
  _findSymbol(parsed.symbols, 'class', 'RecoveredBefore');
  _findSymbol(parsed.symbols, 'method', 'RecoveredBefore.okMethod');
}

void _testGeneratedOneLiner() {
  final buffer = StringBuffer();
  for (var i = 0; i < 240; i++) {
    buffer.write('class Gen$i { void m$i() {} } ');
  }
  final oneLiner = buffer.toString();
  check(oneLiner.length).isGreaterThan(generatedLineChars);

  final parsed = parseDartSource(oneLiner, path: 'lib/foo.g.dart');
  check(parsed.symbols.length).equals(generatedSymbolCap);
  check(parsed.droppedSymbolCount).equals(280);
  check(parsed.symbols.every((s) => s.parent.isEmpty)).isTrue();
}

Future<void> _testWorkerAndTimeout() async {
  final deepSource = File(
    p.join('fixtures', 'conformance', 'deep_nesting.dart'),
  ).readAsStringSync();
  final badSyntaxSource = File(
    p.join('fixtures', 'conformance', 'syntax_error.dart'),
  ).readAsStringSync();

  final proc = await Process.start(
    Platform.resolvedExecutable,
    <String>['run', 'bin/packrat_dart.dart', '--worker'],
  );
  final linesQueue = StreamIterator<String>(
    proc.stdout.transform(utf8.decoder).transform(const LineSplitter()),
  );

  check(await linesQueue.moveNext()).isTrue();
  final banner = jsonDecode(linesQueue.current) as Map<String, Object?>;
  check(banner['ready']).equals(true);
  check(banner['parser']).equals('dart-analyzer');
  check(banner['protocol']).equals(1);
  check(banner['parser_ids']).isA<Map<String, Object?>>();

  proc.stdin.writeln(
    jsonEncode(<String, Object?>{
      'id': 10,
      'path': 'deep_nesting.dart',
      'content': deepSource,
      'deadline_ms': 50,
    }),
  );
  proc.stdin.writeln(
    jsonEncode(<String, Object?>{
      'id': 11,
      'path': 'syntax_error.dart',
      'content': badSyntaxSource,
      'deadline_ms': 5000,
    }),
  );
  proc.stdin.writeln(
    jsonEncode(<String, Object?>{
      'id': 12,
      'path': 'clean.dart',
      'content': '/// Clean.\nclass CleanAfterTimeout {}\n',
      'deadline_ms': 5000,
    }),
  );
  await proc.stdin.flush();
  await proc.stdin.close();

  final responses = <int, Map<String, Object?>>{};
  while (await linesQueue.moveNext()) {
    final msg = jsonDecode(linesQueue.current) as Map<String, Object?>;
    responses[msg['id'] as int] = msg;
  }
  final code = await proc.exitCode;
  check(code).equals(0);
  check(responses.length).equals(3);

  check(responses[10]!['error']).equals('timeout');
  check((responses[11]!['syntax_error_count'] as int)).isGreaterOrEqual(1);
  final cleanSyms = responses[12]!['symbols'] as List<Object?>;
  check(cleanSyms.length).equals(1);
}
