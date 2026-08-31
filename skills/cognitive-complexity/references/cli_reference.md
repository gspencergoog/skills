# Cognitive Complexity CLI Reference

This document describes the command-line interface arguments, options, output formats, and exit codes for the Cognitive Complexity tools.

---

## 1. Synopsis

```bash
# Top-level polyglot orchestrator
python3 scripts/cognitive_complexity.py [OPTIONS] [PATH]

# Language-specific engines
python3 scripts/python/cognitive_complexity.py [OPTIONS] [PATH]
node scripts/typescript/dist/cli.js [OPTIONS] [PATH]
dart run scripts/dart/bin/cognitive_complexity.dart [OPTIONS] [PATH]
swift run --package-path scripts/swift CognitiveComplexity [OPTIONS] [PATH]
java -jar scripts/kotlin/cognitive-complexity-kt.jar [OPTIONS] [PATH]
```

---

## 2. Arguments & Options

| Option / Flag | Long Form | Description | Default |
| :--- | :--- | :--- | :--- |
| `[PATH]` | N/A | Path to a file or directory. If omitted or `-`, reads from standard input (`stdin`). | `-` |
| `-l` | `--lang <LANG>` | Override language detection (`python`, `typescript`, `dart`, `swift`, `kotlin`). | `auto` |
| `-f` | `--format <FORMAT>` | Output format: `text`, `json`, `table`, `summary`. | `text` |
| `-t` | `--threshold <INT>` | Flag functions exceeding this cognitive complexity score. | `15` |
| `-v` | `--verbose` | Include line-by-line breakdown of increments and nesting penalties. | `false` |
| `-s` | `--sort <KEY>` | Sort results by: `complexity`, `name`, `line`, `file`. | `complexity` |
| `-e` | `--exclude <GLOB>` | Glob pattern to exclude during directory traversal. | `None` |
| `-h` | `--help` | Display help message and options. | N/A |
| `-V` | `--version` | Display version information. | N/A |

---

## 3. Output Formats

### 3.1. Text Format (`-f text`)
Human-readable terminal summary with color indicators:
```
File: src/calculator.py
  calculate_total (lines 12-45) -> Complexity: 14 [PASS]
  process_transactions (lines 50-112) -> Complexity: 22 [WARN: Exceeds threshold 15]

Summary:
  Files analyzed: 1
  Total functions: 2
  Average complexity: 18.0
  Functions exceeding threshold (15): 1
```

### 3.2. JSON Format (`-f json`)
Machine-readable structured output:
```json
{
  "version": "1.0.0",
  "language": "python",
  "summary": {
    "total_files": 1,
    "total_functions": 2,
    "total_complexity": 36,
    "average_complexity": 18.0,
    "highest_complexity": 22,
    "functions_exceeding_threshold": 1,
    "threshold": 15
  },
  "files": [
    {
      "path": "src/calculator.py",
      "total_complexity": 36,
      "average_complexity": 18.0,
      "highest_complexity": 22,
      "functions": [
        {
          "name": "process_transactions",
          "class_name": null,
          "line_number": 50,
          "end_line_number": 112,
          "complexity": 22,
          "exceeds_threshold": true,
          "breakdown": [
            {
              "line": 52,
              "type": "if",
              "increment": 1,
              "nesting": 0,
              "reason": "if statement (+1)"
            },
            {
              "line": 55,
              "type": "for",
              "increment": 2,
              "nesting": 1,
              "reason": "nested for loop (+1 + nesting 1 = +2)"
            }
          ]
        }
      ]
    }
  ]
}
```

---

## 4. Exit Codes

| Exit Code | Meaning |
| :--- | :--- |
| `0` | Success: Analysis completed and no functions exceeded the threshold. |
| `1` | Warning: Analysis completed, but one or more functions exceeded the threshold (`--threshold`). |
| `2` | Error: Parsing error, file not found, invalid syntax, or argument error. |
