#!/usr/bin/env dart

import 'dart:io';

import 'package:packrat_dart/packrat_dart.dart';

Future<void> main(List<String> arguments) async {
  try {
    exitCode = await runPackratDartCli(arguments);
  } catch (err, stack) {
    stderr.writeln('packrat_dart fatal error: $err');
    stderr.writeln(stack);
    exitCode = 1;
  }
}
