# Dart & Flutter Commit Checks

Clean up and verify Dart/Flutter code integrity before committing:

1.  **Cleanup**: Prefer the `mcp_dart_dart_fix` and `mcp_dart_dart_format` (or `dart_fix` and `dart_format`) tools to clean up the code. If these MCP tools are not available, use the standard CLI commands:
    - `dart fix --apply`
    - `dart format .`
2.  **Analyze**: Run the `mcp_dart_analyze_files` tool to find and fix any issues.
3.  **Test**: Run the `mcp_dart_run_tests` tool to ensure all tests pass.
