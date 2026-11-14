import 'dart:convert';

/// Canonical parser identifier reported in worker banners and stamps.
const String parserName = 'dart-analyzer';

/// Supported NDJSON wire protocol version.
const int protocolVersion = 1;

/// Stored `indexed_files.parser_id` tag for `.dart` sources.
const String dartParserId = 'dart-analyzer@1';

/// Extension-to-parser-id mapping advertised in the startup banner.
const Map<String, String> parserIds = <String, String>{'.dart': dartParserId};

/// Analyzer package version string reported in the startup banner.
const String analyzerLibrary = 'analyzer 14.5.0';

/// Default per-file parse deadline in milliseconds when omitted by caller.
const int defaultDeadlineMs = 5000;

/// Maximum length in characters of a collapsed declaration signature.
const int maxSignatureChars = 500;

/// Maximum length in characters of a stripped declaration docstring.
const int maxDocstringChars = 4000;

/// Single-line character threshold classifying a file as generated.
const int generatedLineChars = 4000;

/// Average UTF-8 bytes-per-line threshold classifying a file as generated.
const int generatedBytesPerLine = 500;

/// Maximum symbols emitted for a generated or minified file.
const int generatedSymbolCap = 200;

/// Maximum symbols emitted for a normal source file.
const int normalSymbolCap = 1000;

/// Startup banner written as the first NDJSON line on worker stdout.
final class ReadyBanner {
  /// Whether the worker is ready to accept parse requests.
  final bool ready;

  /// Canonical parser identifier.
  final String parser;

  /// Wire protocol version.
  final int protocol;

  /// Extension-to-parser-id routing map.
  final Map<String, String> ids;

  /// Backing analyzer library description.
  final String libraryName;

  /// Creates a startup banner with protocol defaults.
  const ReadyBanner({
    this.ready = true,
    this.parser = parserName,
    this.protocol = protocolVersion,
    this.ids = parserIds,
    this.libraryName = analyzerLibrary,
  });

  /// Converts this banner into a JSON-encodable map.
  Map<String, Object?> toJson() => <String, Object?>{
        'ready': ready,
        'parser': parser,
        'protocol': protocol,
        'parser_ids': ids,
        'library': libraryName,
      };
}

/// One incoming per-file NDJSON parse request from the orchestrator.
final class ParseRequest {
  /// Opaque request correlation identifier echoed in the response.
  final Object? id;

  /// Repository-relative file path.
  final String path;

  /// Full UTF-8 source text of the Dart file.
  final String content;

  /// Per-file parse timeout in milliseconds.
  final int deadlineMs;

  /// Creates a parse request.
  const ParseRequest({
    required this.id,
    required this.path,
    required this.content,
    this.deadlineMs = defaultDeadlineMs,
  });
}

/// Result of decoding one raw NDJSON request line.
final class DecodedRequest {
  /// Request identifier extracted when available.
  final Object? id;

  /// Parsed request when valid, or `null` when [error] is set.
  final ParseRequest? request;

  /// Wire error code when the request payload could not be decoded.
  final String? error;

  /// Creates a decoded request wrapper.
  const DecodedRequest({this.id, this.request, this.error});

  /// Decodes [rawLine] into a [ParseRequest] or a per-request error.
  static DecodedRequest fromJsonLine(String rawLine) {
    final Object? decoded;
    try {
      decoded = jsonDecode(rawLine);
    } on FormatException {
      return const DecodedRequest(error: 'malformed-json');
    }
    if (decoded is! Map) {
      return const DecodedRequest(error: 'invalid-request');
    }
    final id = decoded['id'];
    final content = decoded['content'];
    if (content is! String) {
      return DecodedRequest(id: id, error: 'missing-content');
    }
    final rawPath = decoded['path'];
    final path = rawPath is String ? rawPath : '';
    final deadlineMs = _parseDeadlineMs(decoded['deadline_ms']);
    return DecodedRequest(
      id: id,
      request: ParseRequest(
        id: id,
        path: path,
        content: content,
        deadlineMs: deadlineMs,
      ),
    );
  }

  static int _parseDeadlineMs(Object? raw) {
    if (raw is int && raw > 0) return raw;
    if (raw is num && raw > 0) return raw.toInt();
    return defaultDeadlineMs;
  }
}

/// One extracted code symbol matching the packrat v1 wire schema.
final class SymbolRecord {
  /// Canonical symbol kind (such as `class`, `method`, or `getter`).
  final String kind;

  /// Unqualified leaf symbol name (`new` for unnamed constructors).
  final String name;

  /// Dot-joined qualified symbol name (`MyWidget.build`).
  final String qualifiedName;

  /// Enclosing declaration's qualified name, or `""` at top level.
  final String parent;

  /// Single-line whitespace-collapsed declaration header.
  final String signature;

  /// Stripped documentation comment text, or `""` when undocumented.
  final String docstring;

  /// 1-based starting line number including annotations and modifiers.
  final int lineStart;

  /// 1-based inclusive ending line number (`>= lineStart`).
  final int lineEnd;

  /// Symbol visibility (`public` or `private`).
  final String visibility;

  /// Creates an extracted symbol record.
  const SymbolRecord({
    required this.kind,
    required this.name,
    required this.qualifiedName,
    required this.parent,
    required this.signature,
    required this.docstring,
    required this.lineStart,
    required this.lineEnd,
    required this.visibility,
  });

  /// Reconstructs a [SymbolRecord] from its JSON wire representation.
  factory SymbolRecord.fromJson(Map<String, Object?> json) {
    return SymbolRecord(
      kind: json['kind'] as String? ?? '',
      name: json['name'] as String? ?? '',
      qualifiedName: json['qualified_name'] as String? ?? '',
      parent: json['parent'] as String? ?? '',
      signature: json['signature'] as String? ?? '',
      docstring: json['docstring'] as String? ?? '',
      lineStart: (json['line_start'] as num?)?.toInt() ?? 1,
      lineEnd: (json['line_end'] as num?)?.toInt() ?? 1,
      visibility: json['visibility'] as String? ?? '',
    );
  }

  /// Serializes this symbol to its NDJSON wire map.
  Map<String, Object?> toJson() => <String, Object?>{
        'kind': kind,
        'name': name,
        'qualified_name': qualifiedName,
        'parent': parent,
        'signature': signature,
        'docstring': docstring,
        'line_start': lineStart,
        'line_end': lineEnd,
        'visibility': visibility,
      };
}

/// Output of parsing one Dart source buffer before wire framing.
final class DartParseResult {
  /// Extracted, sorted, and cap-filtered symbols.
  final List<SymbolRecord> symbols;

  /// Count of scanner and parser diagnostics recovered during parsing.
  final int syntaxErrorCount;

  /// Count of symbols filtered by generated-file rules or per-file caps.
  final int droppedSymbolCount;

  /// Creates a parse result.
  const DartParseResult({
    required this.symbols,
    required this.syntaxErrorCount,
    this.droppedSymbolCount = 0,
  });
}

/// Outgoing NDJSON response for one parse request.
final class ParseResponse {
  /// Echoed request correlation identifier.
  final Object? id;

  /// Extracted symbols when the parse succeeded.
  final List<SymbolRecord>? symbols;

  /// Count of tolerated syntax errors when the parse succeeded.
  final int syntaxErrorCount;

  /// Error reason (such as `timeout`) when the parse failed.
  final String? error;

  /// Creates a successful parse response.
  const ParseResponse.success({
    required this.id,
    required List<SymbolRecord> this.symbols,
    required this.syntaxErrorCount,
  }) : error = null;

  /// Creates a per-file error response.
  const ParseResponse.failure({
    required this.id,
    required String this.error,
  })  : symbols = null,
        syntaxErrorCount = 0;

  /// Serializes this response to its NDJSON wire map.
  Map<String, Object?> toJson() {
    if (error != null) {
      return <String, Object?>{'id': id, 'error': error};
    }
    return <String, Object?>{
      'id': id,
      'symbols': symbols!.map((s) => s.toJson()).toList(growable: false),
      'syntax_error_count': syntaxErrorCount,
    };
  }
}
