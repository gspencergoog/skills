import 'dart:convert';
import 'dart:io';

import 'package:args/args.dart';
import 'package:cognitive_complexity_cli/cognitive_complexity.dart';

ArgParser _createParser() {
  return ArgParser()
    ..addOption('format',
        abbr: 'f',
        allowed: ['text', 'json', 'table', 'summary'],
        defaultsTo: 'text',
        help: 'Output format.')
    ..addOption('threshold',
        abbr: 't',
        defaultsTo: '15',
        help: 'Threshold for flagging high complexity.')
    ..addFlag('verbose',
        abbr: 'v',
        negatable: false,
        help: 'Show detailed line-by-line breakdown of increments.')
    ..addOption('sort',
        abbr: 's',
        allowed: ['complexity', 'name', 'line', 'file'],
        defaultsTo: 'complexity',
        help: 'Sort results.')
    ..addMultiOption('exclude',
        abbr: 'e', help: 'Glob patterns to exclude during directory traversal.')
    ..addFlag('help', abbr: 'h', negatable: false, help: 'Show help message.')
    ..addFlag('version',
        abbr: 'V', negatable: false, help: 'Show version information.');
}

Future<List<FileComplexity>> _analyzeTarget(
    String pathArg, DartComplexityAnalyzer analyzer) async {
  final fileResults = <FileComplexity>[];

  if (pathArg == '-') {
    final sourceCode = await stdin.transform(utf8.decoder).join();
    fileResults.add(analyzer.analyzeSource(sourceCode, filePath: '<stdin>'));
    return fileResults;
  }

  final type = FileSystemEntity.typeSync(pathArg);
  if (type == FileSystemEntityType.notFound) {
    stderr.writeln('Error: Path does not exist: $pathArg');
    exit(2);
  }

  if (type == FileSystemEntityType.file) {
    final file = File(pathArg);
    fileResults.add(analyzer.analyzeSource(file.readAsStringSync(), filePath: pathArg));
  } else if (type == FileSystemEntityType.directory) {
    final dir = Directory(pathArg);
    for (final entity in dir.listSync(recursive: true, followLinks: false)) {
      if (entity is File && entity.path.endsWith('.dart') && !entity.path.contains('.dart_tool')) {
        try {
          fileResults.add(analyzer.analyzeSource(entity.readAsStringSync(), filePath: entity.path));
        } catch (e) {
          stderr.writeln('Error reading ${entity.path}: $e');
        }
      }
    }
  }

  return fileResults;
}

void _sortResults(List<FileComplexity> fileResults, String sortKey) {
  for (final fileComp in fileResults) {
    if (sortKey == 'complexity') {
      fileComp.functions.sort((a, b) => b.complexity.compareTo(a.complexity));
    } else if (sortKey == 'name') {
      fileComp.functions.sort((a, b) => a.name.compareTo(b.name));
    } else if (sortKey == 'line') {
      fileComp.functions.sort((a, b) => a.lineNumber.compareTo(b.lineNumber));
    }
  }
}

ComplexityReport _buildReport(List<FileComplexity> fileResults, int threshold) {
  final totalFiles = fileResults.length;
  final totalFuncs = fileResults.fold<int>(0, (sum, f) => sum + f.functions.length);
  final totalComplexity = fileResults.fold<int>(0, (sum, f) => sum + f.totalComplexity);
  final avgComplexity = totalFuncs > 0 ? totalComplexity / totalFuncs : 0.0;
  final highestComplexity = fileResults.fold<int>(
      0, (max, f) => f.highestComplexity > max ? f.highestComplexity : max);
  final exceedingCount = fileResults.fold<int>(
      0, (sum, f) => sum + f.functions.where((fn) => fn.exceedsThreshold).length);

  return ComplexityReport(
    summary: {
      'total_files': totalFiles,
      'total_functions': totalFuncs,
      'total_complexity': totalComplexity,
      'average_complexity': double.parse(avgComplexity.toStringAsFixed(2)),
      'highest_complexity': highestComplexity,
      'functions_exceeding_threshold': exceedingCount,
      'threshold': threshold,
    },
    files: fileResults,
  );
}

void _renderReport(ComplexityReport report, String format, bool verbose, int threshold) {
  if (format == 'json') {
    stdout.writeln(const JsonEncoder.withIndent('  ').convert(report.toJson()));
    return;
  }
  if (format == 'summary') {
    final s = report.summary;
    stdout.writeln('Files: ${s['total_files']}, Functions: ${s['total_functions']}, Total Complexity: ${s['total_complexity']}, Avg: ${s['average_complexity']}, Over Threshold: ${s['functions_exceeding_threshold']}');
    return;
  }

  stdout.writeln('Cognitive Complexity Report (Language: ${report.language})');
  stdout.writeln('=' * 60);
  for (final f in report.files) {
    stdout.writeln('\nFile: ${f.path}');
    if (f.functions.isEmpty) {
      stdout.writeln('  (No functions or methods found)');
      continue;
    }
    for (final fn in f.functions) {
      final qName = fn.className != null ? '${fn.className}.${fn.name}' : fn.name;
      final status = fn.exceedsThreshold ? '[EXCEEDS THRESHOLD $threshold]' : '[PASS]';
      stdout.writeln('  $qName (lines ${fn.lineNumber}-${fn.endLineNumber}) -> Complexity: ${fn.complexity} $status');
      if (verbose && fn.breakdown.isNotEmpty) {
        for (final b in fn.breakdown) {
          stdout.writeln('    Line ${b.line.toString().padLeft(4)}: ${b.reason}');
        }
      }
    }
  }
  final s = report.summary;
  stdout.writeln('\n${'-' * 60}');
  stdout.writeln('Summary:');
  stdout.writeln('  Files analyzed:                ${s['total_files']}');
  stdout.writeln('  Total functions:               ${s['total_functions']}');
  stdout.writeln('  Total complexity:              ${s['total_complexity']}');
  stdout.writeln('  Average complexity:            ${s['average_complexity']}');
  stdout.writeln('  Highest complexity:            ${s['highest_complexity']}');
  stdout.writeln('  Functions exceeding threshold: ${s['functions_exceeding_threshold']} (threshold: $threshold)');
  stdout.writeln('=' * 60);
}

Future<void> main(List<String> arguments) async {
  final parser = _createParser();

  ArgResults args;
  try {
    args = parser.parse(arguments);
  } catch (e) {
    stderr.writeln('Error parsing arguments: $e');
    exit(2);
  }

  if (args['help'] as bool) {
    stdout.writeln('Usage: cognitive_complexity [OPTIONS] [PATH]\nCalculate Cognitive Complexity for Dart code according to SonarSource standard.\n\n${parser.usage}');
    exit(0);
  }

  if (args['version'] as bool) {
    stdout.writeln('cognitive_complexity 1.0.0');
    exit(0);
  }

  final threshold = int.tryParse(args['threshold'] as String) ?? 15;
  final format = args['format'] as String;
  final verbose = args['verbose'] as bool;
  final sortKey = args['sort'] as String;
  final pathArg = args.rest.isNotEmpty ? args.rest.first : '-';

  final analyzer = DartComplexityAnalyzer(threshold: threshold);
  final fileResults = await _analyzeTarget(pathArg, analyzer);

  _sortResults(fileResults, sortKey);
  final report = _buildReport(fileResults, threshold);
  _renderReport(report, format, verbose, threshold);

  final exceedingCount = report.summary['functions_exceeding_threshold'] as int? ?? 0;
  exit(exceedingCount > 0 ? 1 : 0);
}
