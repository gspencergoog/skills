import 'dart:convert';
import 'dart:io';

import 'package:args/args.dart';
import 'package:path/path.dart' as p;

import 'protocol.dart';
import 'worker.dart';

/// Builds the command-line argument parser for `packrat_dart`.
ArgParser buildArgParser() {
  return ArgParser()
    ..addFlag(
      'worker',
      negatable: false,
      help: 'Run persistent NDJSON worker loop over stdin and stdout.',
    )
    ..addOption(
      'selftest',
      valueHelp: 'fixture-dir',
      help:
          'Parse .dart fixtures in <fixture-dir>, print JSON summary, exit 0.',
    )
    ..addFlag(
      'help',
      abbr: 'h',
      negatable: false,
      help: 'Print usage information.',
    );
}

List<String> _normalizeArgs(List<String> rawArgs) {
  final normalized = <String>[];
  for (var i = 0; i < rawArgs.length; i++) {
    final arg = rawArgs[i];
    normalized.add(arg);
    if (arg == '--selftest') {
      final hasValue =
          i + 1 < rawArgs.length && !rawArgs[i + 1].startsWith('-');
      if (!hasValue) {
        normalized.add('fixtures');
      }
    }
  }
  return normalized;
}

/// Runs the `packrat_dart` CLI and returns the desired process exit code.
Future<int> runPackratDartCli(List<String> rawArguments) async {
  final argParser = buildArgParser();
  final ArgResults parsed;
  try {
    parsed = argParser.parse(_normalizeArgs(rawArguments));
  } on FormatException catch (err) {
    stderr.writeln('Argument error: ${err.message}');
    stderr.writeln(argParser.usage);
    return 64;
  }

  if (parsed['help'] as bool) {
    stdout.writeln('Usage: packrat_dart [--worker | --selftest <fixture-dir>]');
    stdout.writeln(argParser.usage);
    return 0;
  }

  final selftestDir = parsed['selftest'] as String?;
  if (selftestDir != null) {
    return _runSelftest(selftestDir);
  }

  await runNdjsonWorker(input: stdin, output: stdout.nonBlocking);
  return 0;
}

Future<int> _runSelftest(String fixtureDirPath) async {
  final dir = Directory(fixtureDirPath);
  if (!dir.existsSync()) {
    stderr.writeln('Selftest directory not found: $fixtureDirPath');
    return 66;
  }

  final files = _collectFixtureFiles(dir);
  final pool = DartParseWorkerPool();
  await pool.start();
  try {
    final results = <Map<String, Object?>>[];
    var nextId = 1;
    for (final file in files) {
      final relPath = p.posix.normalize(
        p.relative(file.path, from: dir.path).replaceAll(r'\', '/'),
      );
      final isDeepNesting =
          p.basenameWithoutExtension(file.path) == 'deep_nesting';
      final response = await pool.parse(
        ParseRequest(
          id: nextId++,
          path: relPath,
          content: file.readAsStringSync(),
          deadlineMs: isDeepNesting ? 50 : defaultDeadlineMs,
        ),
      );
      results.add(_summarizeSelftestFile(relPath, isDeepNesting, response));
    }
    final allOk = results.every((r) => r['ok'] == true);
    stdout.writeln(
      const JsonEncoder.withIndent('  ').convert(<String, Object?>{
        'ok': allOk,
        'parser': parserName,
        'protocol': protocolVersion,
        'files_checked': results.length,
        'results': results,
      }),
    );
    await stdout.flush();
    return allOk ? 0 : 1;
  } finally {
    await pool.close();
  }
}

List<File> _collectFixtureFiles(Directory root) {
  final files = <File>[];
  for (final entity in root.listSync(recursive: true, followLinks: false)) {
    if (entity is File && entity.path.endsWith('.dart')) {
      files.add(entity);
    }
  }
  files.sort((a, b) => a.path.compareTo(b.path));
  return files;
}

Map<String, Object?> _summarizeSelftestFile(
  String relPath,
  bool isDeepNesting,
  ParseResponse response,
) {
  if (isDeepNesting) {
    return <String, Object?>{
      'path': relPath,
      'ok': response.error == 'timeout',
      'error': response.error,
    };
  }
  final isSyntaxError = p.basenameWithoutExtension(relPath) == 'syntax_error';
  if (response.error != null) {
    // Verify direct parse succeeds if worker hit an unexpected error.
    return <String, Object?>{
      'path': relPath,
      'ok': false,
      'error': response.error,
    };
  }
  final ok = isSyntaxError ? response.syntaxErrorCount >= 1 : true;
  return <String, Object?>{
    'path': relPath,
    'ok': ok,
    'symbol_count': response.symbols?.length ?? 0,
    'syntax_error_count': response.syntaxErrorCount,
  };
}
