import 'dart:convert';

import 'package:analyzer/dart/ast/ast.dart';
import 'package:analyzer/dart/ast/token.dart';

import 'protocol.dart';

final RegExp _whitespaceRuns = RegExp(r'\s+');

/// Maps UTF-16 code unit offsets to 1-based line numbers using LF (`\n`) and
/// CRLF (`\r\n`) line endings only.
///
/// Form feed (`\f`) and standalone carriage returns (`\r` not followed by `\n`)
/// do not start new lines, matching `PROTOCOL.md` LF line splitting.
final class LfLineInfo {
  final List<int> _lineStarts;
  final int _sourceLength;

  /// Builds an LF-only line index over [source].
  LfLineInfo(String source)
      : _lineStarts = _computeLineStarts(source),
        _sourceLength = source.length;

  /// Total number of LF-delimited lines in the indexed source.
  int get lineCount => _lineStarts.length;

  static List<int> _computeLineStarts(String source) {
    final starts = <int>[0];
    for (var i = 0; i < source.length; i++) {
      if (source.codeUnitAt(i) == 0x0A) {
        starts.add(i + 1);
      }
    }
    return starts;
  }

  /// Returns the 1-based line number containing [offset].
  int lineAt(int offset) {
    final clamped = _clampOffset(offset, _sourceLength);
    var low = 0;
    var high = _lineStarts.length - 1;
    while (low < high) {
      final mid = (low + high + 1) >> 1;
      if (_lineStarts[mid] <= clamped) {
        low = mid;
      } else {
        high = mid - 1;
      }
    }
    return low + 1;
  }

  static int _clampOffset(int offset, int maxOffset) {
    if (offset < 0) return 0;
    if (offset > maxOffset) return maxOffset;
    return offset;
  }
}

/// Splits [text] on LF (`\n`) or CRLF (`\r\n`) boundaries only.
List<String> splitLinesLf(String text) {
  final raw = text.split('\n');
  return List<String>.generate(raw.length, (i) {
    final line = raw[i];
    return line.endsWith('\r') ? line.substring(0, line.length - 1) : line;
  }, growable: false);
}

/// Returns whether [text] (with pre-split [lines]) exceeds the generated-file
/// line-length or byte-density thresholds from `PROTOCOL.md`.
bool isGeneratedSource(String text, List<String> lines) {
  if (text.isEmpty) return false;
  for (final line in lines) {
    if (line.length > generatedLineChars) return true;
  }
  var lineCount = lines.length;
  if (text.endsWith('\n') && lineCount > 1) {
    lineCount -= 1;
  }
  if (lineCount < 1) lineCount = 1;
  return utf8.encode(text).length > lineCount * generatedBytesPerLine;
}

/// Returns Dart symbol visibility (`private` for `_` prefix, else `public`).
String dartVisibility(String name) =>
    name.startsWith('_') ? 'private' : 'public';

/// Collapses consecutive whitespace characters in [raw] into single spaces.
String collapseWhitespace(String raw) =>
    raw.replaceAll(_whitespaceRuns, ' ').trim();

/// Extracts a single-line signature from [source] between [startOffset] and
/// [endOffset], falling back to [fallback] when empty.
String sliceSignature(
  String source,
  int startOffset,
  int endOffset,
  String fallback,
) {
  final start = _clamp(startOffset, 0, source.length);
  final end = _clamp(endOffset, 0, source.length);
  if (end <= start) {
    return _truncate(collapseWhitespace(fallback), maxSignatureChars);
  }
  final collapsed = collapseWhitespace(source.substring(start, end));
  final chosen = collapsed.isEmpty ? collapseWhitespace(fallback) : collapsed;
  return _truncate(chosen, maxSignatureChars);
}

/// Returns the first token of [node] including metadata annotations and
/// modifiers, while excluding any leading documentation comment.
Token declarationBeginToken(AnnotatedNode node) {
  final firstDecl = node.firstTokenAfterCommentAndMetadata;
  if (node.metadata.isEmpty) return firstDecl;
  final firstMeta = node.metadata.first.beginToken;
  return firstMeta.offset <= firstDecl.offset ? firstMeta : firstDecl;
}

/// Computes the 1-based inclusive `(lineStart, lineEnd)` span for [node].
(int, int) declarationLineSpan(
  AnnotatedNode node,
  LfLineInfo lineInfo, {
  int? beginOffsetOverride,
  int? endOffsetOverride,
}) {
  final beginOffset = beginOffsetOverride ?? declarationBeginToken(node).offset;
  final rawEnd = endOffsetOverride ?? node.end;
  final endOffset = rawEnd > beginOffset ? rawEnd - 1 : beginOffset;
  final lineStart = lineInfo.lineAt(beginOffset);
  final rawLineEnd = lineInfo.lineAt(endOffset);
  final lineEnd = rawLineEnd < lineStart ? lineStart : rawLineEnd;
  return (lineStart, lineEnd);
}

/// Strips `///`, `/**`, `*/`, and leading `*` markers from [comment].
String extractDocstring(Comment? comment) {
  if (comment == null || comment.tokens.isEmpty) return '';
  final lines = <String>[];
  for (final token in comment.tokens) {
    final lexeme = token.lexeme;
    if (lexeme.startsWith('///')) {
      lines.add(_stripSlashDocLine(lexeme));
    } else if (lexeme.startsWith('/*')) {
      lines.addAll(_extractBlockDocLines(lexeme));
    }
  }
  final joined = lines.join('\n').trim();
  return _truncate(joined, maxDocstringChars);
}

String _stripSlashDocLine(String lexeme) {
  var body = lexeme.substring(3);
  if (body.endsWith('\r')) {
    body = body.substring(0, body.length - 1);
  }
  if (body.startsWith(' ')) {
    body = body.substring(1);
  }
  return body.trimRight();
}

List<String> _extractBlockDocLines(String lexeme) {
  if (lexeme == '/**/' || lexeme == '/***/') return const <String>[];
  final prefixLen = lexeme.startsWith('/**') ? 3 : 2;
  var inner = lexeme.substring(prefixLen);
  if (inner.endsWith('*/')) {
    inner = inner.substring(0, inner.length - 2);
  }
  final rawLines = splitLinesLf(inner);
  return rawLines.map(_stripBlockDocLine).toList(growable: false);
}

String _stripBlockDocLine(String line) {
  final trimmedLeft = line.trimLeft();
  if (!trimmedLeft.startsWith('*')) {
    return line.trimRight();
  }
  var rest = trimmedLeft.substring(1);
  if (rest.startsWith(' ')) {
    rest = rest.substring(1);
  }
  return rest.trimRight();
}

int _clamp(int value, int minVal, int maxVal) {
  if (value < minVal) return minVal;
  if (value > maxVal) return maxVal;
  return value;
}

String _truncate(String value, int maxChars) =>
    value.length <= maxChars ? value : value.substring(0, maxChars);
