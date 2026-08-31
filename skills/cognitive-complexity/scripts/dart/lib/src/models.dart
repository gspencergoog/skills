class ComplexityIncrement {
  final int line;
  final int column;
  final String nodeType;
  final int increment;
  final int nesting;
  final String reason;

  const ComplexityIncrement({
    required this.line,
    required this.column,
    required this.nodeType,
    required this.increment,
    required this.nesting,
    required this.reason,
  });

  Map<String, dynamic> toJson() => {
        'line': line,
        'column': column,
        'type': nodeType,
        'increment': increment,
        'nesting': nesting,
        'reason': reason,
      };
}

class FunctionComplexity {
  final String name;
  final String? className;
  final int lineNumber;
  final int endLineNumber;
  final int complexity;
  final bool exceedsThreshold;
  final List<ComplexityIncrement> breakdown;

  const FunctionComplexity({
    required this.name,
    this.className,
    required this.lineNumber,
    required this.endLineNumber,
    required this.complexity,
    required this.exceedsThreshold,
    this.breakdown = const [],
  });

  Map<String, dynamic> toJson() => {
        'name': name,
        'class_name': className,
        'line_number': lineNumber,
        'end_line_number': endLineNumber,
        'complexity': complexity,
        'exceeds_threshold': exceedsThreshold,
        'breakdown': breakdown.map((b) => b.toJson()).toList(),
      };
}

class FileComplexity {
  final String path;
  final int totalComplexity;
  final double averageComplexity;
  final int highestComplexity;
  final List<FunctionComplexity> functions;

  const FileComplexity({
    required this.path,
    required this.totalComplexity,
    required this.averageComplexity,
    required this.highestComplexity,
    this.functions = const [],
  });

  Map<String, dynamic> toJson() => {
        'path': path,
        'total_complexity': totalComplexity,
        'average_complexity': double.parse(averageComplexity.toStringAsFixed(2)),
        'highest_complexity': highestComplexity,
        'functions': functions.map((f) => f.toJson()).toList(),
      };
}

class ComplexityReport {
  final String version;
  final String language;
  final Map<String, dynamic> summary;
  final List<FileComplexity> files;

  const ComplexityReport({
    this.version = '1.0.0',
    this.language = 'dart',
    required this.summary,
    required this.files,
  });

  Map<String, dynamic> toJson() => {
        'version': version,
        'language': language,
        'summary': summary,
        'files': files.map((f) => f.toJson()).toList(),
      };
}
