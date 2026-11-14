// Intentionally malformed Dart file for syntax-error recovery conformance.

/// Valid class preceding syntax error.
class RecoveredBefore {
  int okMethod() => 42;
}

class BrokenSyntax {
  void malformed(int a, ) {
    final x = ;
  // Missing closing brace for malformed and BrokenSyntax
