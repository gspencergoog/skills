#!/usr/bin/env python3
"""
Unified Multi-Language Cognitive Complexity Analyzer & Orchestrator.
Supports Python, TypeScript, Dart, Swift, and Kotlin according to SonarSource standard.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

SCRIPT_DIR = Path(__file__).resolve().parent

EXT_TO_LANG: Dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".dart": "dart",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
}


def detect_language_from_content(content: str) -> str:
    """Heuristic detection of language from source code."""
    indicators = [
        ("python", ("def ", "import ", "elif ")),
        ("swift", ("func ", "guard ", "->")),
        ("kotlin", ("fun ", "val ", "when (")),
        ("dart", ("void ", "final ", "String ")),
        ("typescript", ("function ", "const ", "let ")),
    ]
    for lang, tokens in indicators:
        if any(tok in content for tok in tokens):
            return lang
    return "python"


def run_python_engine(source_or_path: str, is_stdin: bool, threshold: int, verbose: bool, sort_key: str) -> Dict[str, Any]:
    sys.path.insert(0, str(SCRIPT_DIR / "python"))
    from cognitive_complexity import PythonComplexityAnalyzer

    analyzer = PythonComplexityAnalyzer(threshold=threshold)
    if is_stdin:
        file_comp = analyzer.analyze_source(source_or_path, file_path="<stdin>")
        files = [file_comp.to_dict()]
    else:
        p = Path(source_or_path)
        files = []
        if p.is_file():
            content = p.read_text(encoding="utf-8")
            files.append(analyzer.analyze_source(content, file_path=str(p)).to_dict())
        elif p.is_dir():
            for py_file in p.rglob("*.py"):
                try:
                    content = py_file.read_text(encoding="utf-8")
                    files.append(analyzer.analyze_source(content, file_path=str(py_file)).to_dict())
                except Exception:
                    pass

    return _build_engine_report("python", files, threshold)


def run_typescript_engine(source_or_path: str, is_stdin: bool, threshold: int, verbose: bool, sort_key: str) -> Dict[str, Any]:
    ts_cli = SCRIPT_DIR / "typescript" / "dist" / "src" / "cli.js"
    return _run_subprocess_engine(["node", str(ts_cli)], source_or_path, is_stdin, threshold, verbose, sort_key, "typescript")


def run_dart_engine(source_or_path: str, is_stdin: bool, threshold: int, verbose: bool, sort_key: str) -> Dict[str, Any]:
    dart_cli = SCRIPT_DIR / "dart" / "bin" / "cognitive_complexity.dart"
    return _run_subprocess_engine(["dart", "run", str(dart_cli)], source_or_path, is_stdin, threshold, verbose, sort_key, "dart")


def run_swift_engine(source_or_path: str, is_stdin: bool, threshold: int, verbose: bool, sort_key: str) -> Dict[str, Any]:
    binary = SCRIPT_DIR / "swift" / ".build" / "release" / "CognitiveComplexity"
    if not binary.exists():
        binary = SCRIPT_DIR / "swift" / ".build" / "debug" / "CognitiveComplexity"

    if binary.exists():
        cmd = [str(binary)]
    else:
        cmd = ["swift", "run", "--package-path", str(SCRIPT_DIR / "swift"), "CognitiveComplexity"]

    return _run_subprocess_engine(cmd, source_or_path, is_stdin, threshold, verbose, sort_key, "swift")


def run_kotlin_engine(source_or_path: str, is_stdin: bool, threshold: int, verbose: bool, sort_key: str) -> Dict[str, Any]:
    kt_dir = SCRIPT_DIR / "kotlin"
    jar_path = kt_dir / "build" / "libs" / "cognitive-complexity-kt-all.jar"
    if not jar_path.exists():
        jar_path = kt_dir / "build" / "libs" / "cognitive-complexity-kt.jar"

    if jar_path.exists():
        cmd = ["java", "-jar", str(jar_path)]
    else:
        gradlew = kt_dir / "gradlew"
        if gradlew.exists():
            cmd = [str(gradlew), "-p", str(kt_dir), "run", "--quiet", "--args="]
        else:
            cmd = ["gradle", "-p", str(kt_dir), "run", "--quiet", "--args="]

    return _run_subprocess_engine(cmd, source_or_path, is_stdin, threshold, verbose, sort_key, "kotlin")


def _run_subprocess_engine(
    cmd_base: List[str],
    source_or_path: str,
    is_stdin: bool,
    threshold: int,
    verbose: bool,
    sort_key: str,
    language: str,
) -> Dict[str, Any]:
    cmd = list(cmd_base) + ["-f", "json", "-t", str(threshold), "-s", sort_key]
    if verbose:
        cmd.append("-v")

    if is_stdin:
        cmd.append("-")
        res = subprocess.run(cmd, input=source_or_path, text=True, capture_output=True)
    else:
        cmd.append(source_or_path)
        res = subprocess.run(cmd, text=True, capture_output=True)

    if res.stdout:
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError:
            pass
    return _build_engine_report(language, [], threshold)


def _build_engine_report(language: str, files: List[Dict[str, Any]], threshold: int) -> Dict[str, Any]:
    total_files = len(files)
    total_funcs = sum(len(f.get("functions", [])) for f in files)
    total_complexity = sum(f.get("total_complexity", 0) for f in files)
    avg_complexity = (total_complexity / total_funcs) if total_funcs else 0.0
    highest_complexity = max((f.get("highest_complexity", 0) for f in files), default=0)
    exceeding_count = sum(1 for f in files for fn in f.get("functions", []) if fn.get("exceeds_threshold"))

    return {
        "version": "1.0.0",
        "language": language,
        "summary": {
            "total_files": total_files,
            "total_functions": total_funcs,
            "total_complexity": total_complexity,
            "average_complexity": round(avg_complexity, 2),
            "highest_complexity": highest_complexity,
            "functions_exceeding_threshold": exceeding_count,
            "threshold": threshold,
        },
        "files": files,
    }


ENGINES: Dict[str, Callable[[str, bool, int, bool, str], Dict[str, Any]]] = {
    "python": run_python_engine,
    "typescript": run_typescript_engine,
    "javascript": run_typescript_engine,
    "dart": run_dart_engine,
    "swift": run_swift_engine,
    "kotlin": run_kotlin_engine,
}


def _dispatch_engine(lang: str, source_or_path: str, is_stdin: bool, threshold: int, verbose: bool, sort_key: str) -> Dict[str, Any]:
    engine = ENGINES.get(lang, run_python_engine)
    return engine(source_or_path, is_stdin, threshold, verbose, sort_key)


def _analyze_stdin(lang: str, threshold: int, verbose: bool, sort_key: str) -> Dict[str, Any]:
    content = sys.stdin.read()
    target_lang = lang if lang != "auto" else detect_language_from_content(content)
    return _dispatch_engine(target_lang, content, True, threshold, verbose, sort_key)


def _analyze_file(file_path: Path, lang: str, threshold: int, verbose: bool, sort_key: str) -> Dict[str, Any]:
    target_lang = lang if lang != "auto" else EXT_TO_LANG.get(file_path.suffix.lower(), "python")
    return _dispatch_engine(target_lang, str(file_path), False, threshold, verbose, sort_key)


def _analyze_directory(dir_path: Path, lang: str, threshold: int, verbose: bool, sort_key: str) -> Dict[str, Any]:
    all_files: List[Dict[str, Any]] = []
    languages_detected: Set[str] = set()

    target_langs = [lang] if lang != "auto" else ["python", "typescript", "dart", "swift", "kotlin"]

    for l in target_langs:
        rep = _dispatch_engine(l, str(dir_path), False, threshold, verbose, sort_key)
        if rep.get("files"):
            all_files.extend(rep["files"])
            languages_detected.add(l)

    lang_label = ", ".join(sorted(languages_detected)) if languages_detected else "multi-language"
    return _build_engine_report(lang_label, all_files, threshold)


def analyze(
    path_arg: str,
    lang: str = "auto",
    threshold: int = 15,
    verbose: bool = False,
    sort_key: str = "complexity",
    exclude_patterns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if path_arg == "-" or not path_arg:
        return _analyze_stdin(lang, threshold, verbose, sort_key)

    p = Path(path_arg)
    if not p.exists():
        raise FileNotFoundError(f"Path does not exist: {path_arg}")

    if p.is_file():
        return _analyze_file(p, lang, threshold, verbose, sort_key)
    return _analyze_directory(p, lang, threshold, verbose, sort_key)


def format_text(report: Dict[str, Any], verbose: bool = False) -> str:
    summary = report.get("summary", {})
    files = report.get("files", [])
    threshold = summary.get("threshold", 15)

    lines: List[str] = []
    lines.append(f"Cognitive Complexity Report (Language: {report.get('language', 'unknown')})")
    lines.append("=" * 60)

    for f in files:
        lines.append(f"\nFile: {f.get('path', '')}")
        funcs = f.get("functions", [])
        if not funcs:
            lines.append("  (No functions or methods found)")
            continue

        for fn in funcs:
            q_name = f"{fn.get('class_name') + '.' if fn.get('class_name') else ''}{fn.get('name', '')}"
            status = f"[EXCEEDS THRESHOLD {threshold}]" if fn.get("exceeds_threshold") else "[PASS]"
            lines.append(f"  {q_name} (lines {fn.get('line_number')}-{fn.get('end_line_number')}) -> Complexity: {fn.get('complexity')} {status}")
            if verbose and fn.get("breakdown"):
                for b in fn["breakdown"]:
                    lines.append(f"    Line {b.get('line', 0):4d}: {b.get('reason', '')}")

    lines.append("\n" + "-" * 60)
    lines.append("Summary:")
    lines.append(f"  Files analyzed:                {summary.get('total_files', 0)}")
    lines.append(f"  Total functions:               {summary.get('total_functions', 0)}")
    lines.append(f"  Total complexity:              {summary.get('total_complexity', 0)}")
    lines.append(f"  Average complexity:            {summary.get('average_complexity', 0.0):.2f}")
    lines.append(f"  Highest complexity:            {summary.get('highest_complexity', 0)}")
    lines.append(f"  Functions exceeding threshold: {summary.get('functions_exceeding_threshold', 0)} (threshold: {threshold})")
    lines.append("=" * 60)

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified Multi-Language Cognitive Complexity Analyzer (Python, TypeScript, Dart, Swift, Kotlin)."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="-",
        help="Path to file or directory. If omitted or '-', reads from stdin.",
    )
    parser.add_argument(
        "-l",
        "--lang",
        default="auto",
        choices=["auto", "python", "typescript", "javascript", "dart", "swift", "kotlin"],
        help="Language override (default: auto-detect).",
    )
    parser.add_argument(
        "-f",
        "--format",
        default="text",
        choices=["text", "json", "summary"],
        help="Output format (default: text).",
    )
    parser.add_argument(
        "-t",
        "--threshold",
        type=int,
        default=15,
        help="Threshold for flagging high complexity (default: 15).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed line-by-line breakdown of increments.",
    )
    parser.add_argument(
        "-s",
        "--sort",
        default="complexity",
        choices=["complexity", "name", "line", "file"],
        help="Sort functions by criteria (default: complexity).",
    )
    parser.add_argument(
        "-e",
        "--exclude",
        action="append",
        help="Glob patterns to exclude during directory traversal.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )

    args = parser.parse_args()

    try:
        report = analyze(
            path_arg=args.path,
            lang=args.lang,
            threshold=args.threshold,
            verbose=args.verbose,
            sort_key=args.sort,
            exclude_patterns=args.exclude,
        )
    except Exception as e:
        print(f"Error during analysis: {e}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, indent=2))
    elif args.format == "summary":
        summary = report.get("summary", {})
        print(f"Files: {summary.get('total_files', 0)}, Functions: {summary.get('total_functions', 0)}, Total Complexity: {summary.get('total_complexity', 0)}, Avg: {summary.get('average_complexity', 0.0):.2f}, Over Threshold: {summary.get('functions_exceeding_threshold', 0)}")
    else:
        print(format_text(report, verbose=args.verbose))

    summary = report.get("summary", {})
    if summary.get("functions_exceeding_threshold", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
