import 'package:checks/checks.dart';
import 'package:cognitive_complexity_cli/cognitive_complexity.dart';
import 'package:test/test.dart';

void main() {
  const analyzer = DartComplexityAnalyzer(threshold: 15);

  int getComplexity(String code, String funcName) {
    final result = analyzer.analyzeSource(code, filePath: 'test.dart');
    final func = result.functions.firstWhere(
      (f) => f.name == funcName,
      orElse: () => throw StateError(
          'Function $funcName not found in ${result.functions.map((f) => f.name)}'),
    );
    return func.complexity;
  }

  group('Dart Cognitive Complexity Compliance Suite', () {
    test('TC01: Flat linear code', () {
      const code = '''
int linearFunction(int a, int b) {
  final x = a + b;
  final y = x * 2;
  return y;
}
''';
      check(getComplexity(code, 'linearFunction')).equals(0);
    });

    test('TC02: Single if statement', () {
      const code = '''
int singleIf(int x) {
  if (x > 0) {
    return x;
  }
  return -x;
}
''';
      check(getComplexity(code, 'singleIf')).equals(1);
    });

    test('TC03: Nested if inside for loop', () {
      const code = '''
int nestedIfLoop(List<int> items) {
  var total = 0;
  for (final x in items) {
    if (x > 0) {
      total += x;
    }
  }
  return total;
}
''';
      check(getComplexity(code, 'nestedIfLoop')).equals(3);
    });

    test('TC04: Switch statement / expression', () {
      const code = '''
int processCommand(String cmd) {
  switch (cmd) {
    case 'start': return 1;
    case 'stop': return 0;
    default: return -1;
  }
}
''';
      check(getComplexity(code, 'processCommand')).equals(1);
    });

    test('TC05: Boolean chain same operator', () {
      const code = '''
bool boolChain(bool a, bool b, bool c) {
  if (a && b && c) {
    return true;
  }
  return false;
}
''';
      check(getComplexity(code, 'boolChain')).equals(2);
    });

    test('TC06: Boolean switch operator', () {
      const code = '''
bool boolSwitch(bool a, bool b, bool c) {
  if ((a && b) || c) {
    return true;
  }
  return false;
}
''';
      check(getComplexity(code, 'boolSwitch')).equals(3);
    });

    test('TC07: else if chain', () {
      const code = '''
String elseIfChain(int x) {
  if (x == 1) {
    return 'one';
  } else if (x == 2) {
    return 'two';
  } else if (x == 3) {
    return 'three';
  } else {
    return 'other';
  }
}
''';
      check(getComplexity(code, 'elseIfChain')).equals(3);
    });

    test('TC08: 3-level nested loop', () {
      const code = '''
void tripleLoop(List<List<List<int>>> matrix) {
  for (final row in matrix) {
    for (final col in row) {
      for (final item in col) {
        print(item);
      }
    }
  }
}
''';
      check(getComplexity(code, 'tripleLoop')).equals(6);
    });

    test('TC09: Direct recursion', () {
      const code = '''
int factorial(int n) {
  if (n <= 1) {
    return 1;
  }
  return n * factorial(n - 1);
}
''';
      check(getComplexity(code, 'factorial')).equals(2);
    });

    test('TC10: try/catch block', () {
      const code = '''
double safeDivide(double a, double b) {
  try {
    return a / b;
  } catch (e) {
    return 0.0;
  }
}
''';
      check(getComplexity(code, 'safeDivide')).equals(1);
    });

    test('TC11: Nested closure with branch', () {
      const code = '''
List<int> outerWithClosure(List<int> items) {
  int f(int x) {
    if (x > 0) return x * 2;
    return 0;
  }
  return items.map(f).toList();
}
''';
      check(getComplexity(code, 'outerWithClosure')).equals(2);
    });

    test('TC12: Ternary nested inside loop', () {
      const code = '''
void ternaryNested(List<int> items) {
  for (final x in items) {
    final val = x > 0 ? 1 : -1;
  }
}
''';
      check(getComplexity(code, 'ternaryNested')).equals(3);
    });

    test('TC13: Guard clause', () {
      const code = '''
String? guardClause(dynamic user) {
  if (user == null) {
    return null;
  }
  if (!user.isActive) {
    return null;
  }
  return user.name;
}
''';
      check(getComplexity(code, 'guardClause')).equals(2);
    });

    test('TC14: Dart 3 switch expression and collection if/for', () {
      const code = '''
List<String> describeItems(List<int> items) {
  return [
    for (final x in items)
      if (x > 0)
        switch (x) {
          1 => 'one',
          _ => 'other',
        }
      else
        'negative'
  ];
}
''';
      check(getComplexity(code, 'describeItems')).equals(6);
    });

    test('TC15: SonarSource Appendix B', () {
      const code = '''
int? getElement(List<List<int?>> matrix) {
  for (var i = 0; i < matrix.length; i++) {              // +1 (nesting 0)
    for (var j = 0; j < matrix[i].length; j++) {         // +2 (nesting 1)
      if (matrix[i][j] != null) {                        // +3 (nesting 2)
        if (matrix[i][j]! > 0 && matrix[i][j]! < 100) {  // +4 (nesting 3) + 1 (bool) = +5
          return matrix[i][j];
        } else if (matrix[i][j] == 0) {                  // +1 (else if base)
          continue;
        }
      }
    }
  }
  return null;
}
''';
      check(getComplexity(code, 'getElement')).equals(12);
    });

    test('Mixins, extensions, constructors, and do-while', () {
      const code = '''
mixin LogMixin {
  void logMsg(String msg) {
    if (msg.isNotEmpty) print(msg);
  }
}

extension IntExt on int {
  int get doubled => this > 0 ? this * 2 : 0;
}

class Service with LogMixin {
  int count;
  Service(this.count) {
    if (count < 0) count = 0;
  }

  void loopWork() {
    do {
      count--;
    } while (count > 0);
  }
}
''';
      final res = analyzer.analyzeSource(code, filePath: 'service.dart');
      check(res.functions.length).equals(4);
      final jsonMap = res.toJson();
      check(jsonMap['total_complexity']).isA<int>();

      final report = ComplexityReport(
        summary: {'total_files': 1},
        files: [res],
      );
      check(report.toJson()['language']).equals('dart');
    });
  });
}
