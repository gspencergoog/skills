#!/usr/bin/env python3
"""
Integration test suite for Cognitive Complexity Multi-Language Orchestrator.
Tests stdin, files, auto-detection, and recursive scanning of the codebase.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ORCHESTRATOR = SCRIPT_DIR / "cognitive_complexity.py"


class TestOrchestrator(unittest.TestCase):
    def test_python_stdin(self) -> None:
        code = "def foo(x):\n    if x > 0:\n        return x\n    return -x\n"
        res = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), "-f", "json", "-l", "python", "-t", "100", "-"],
            input=code,
            text=True,
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Error: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertEqual(data["language"], "python")
        self.assertEqual(data["summary"]["total_functions"], 1)
        self.assertEqual(data["files"][0]["functions"][0]["complexity"], 1)

    def test_typescript_file(self) -> None:
        ts_test_file = SCRIPT_DIR / "typescript" / "test" / "visitor.test.ts"
        if not ts_test_file.exists():
            self.skipTest("TypeScript test file not found")

        res = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), "-f", "json", "-t", "100", str(ts_test_file)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Error: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertEqual(data["language"], "typescript")
        self.assertGreater(data["summary"]["total_files"], 0)

    def test_dart_file(self) -> None:
        dart_file = SCRIPT_DIR / "dart" / "lib" / "src" / "visitor.dart"
        if not dart_file.exists():
            self.skipTest("Dart file not found")

        res = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), "-f", "json", "-t", "100", str(dart_file)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Error: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertEqual(data["language"], "dart")
        self.assertGreater(data["summary"]["total_functions"], 0)

    def test_swift_file(self) -> None:
        swift_file = SCRIPT_DIR / "swift" / "Sources" / "CognitiveComplexity" / "ComplexityVisitor.swift"
        if not swift_file.exists():
            self.skipTest("Swift file not found")

        res = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), "-f", "json", "-t", "100", str(swift_file)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Error: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertEqual(data["language"], "swift")
        self.assertGreater(data["summary"]["total_functions"], 0)

    def test_full_skill_codebase_scan_integration(self) -> None:
        """Integration test: scans the entire cognitive-complexity skill scripts directory."""
        res = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), "-f", "json", "-t", "200", str(SCRIPT_DIR)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Error running scan: {res.stderr}")
        data = json.loads(res.stdout)

        summary = data.get("summary", {})
        self.assertGreaterEqual(summary.get("total_files", 0), 10)
        self.assertGreaterEqual(summary.get("total_functions", 0), 50)
        self.assertGreater(summary.get("total_complexity", 0), 0)
        self.assertLess(summary.get("average_complexity", 0.0), 10.0)

        # Check that multiple languages were scanned and aggregated
        lang_str = data.get("language", "")
        self.assertIn("python", lang_str)
        self.assertIn("typescript", lang_str)
        self.assertIn("dart", lang_str)
        self.assertIn("swift", lang_str)

    def test_multiple_files_same_language(self) -> None:
        py1 = SCRIPT_DIR / "python" / "cognitive_complexity.py"
        py2 = SCRIPT_DIR / "python" / "test_cognitive_complexity.py"
        res = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), "-f", "json", "-t", "100", str(py1), str(py2)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Error: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertEqual(data["summary"]["total_files"], 2)
        paths = [f["path"] for f in data["files"]]
        self.assertIn(str(py1), paths)
        self.assertIn(str(py2), paths)

    def test_multiple_files_different_languages(self) -> None:
        py_file = SCRIPT_DIR / "python" / "cognitive_complexity.py"
        dart_file = SCRIPT_DIR / "dart" / "lib" / "src" / "visitor.dart"
        res = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), "-f", "json", "-t", "100", str(py_file), str(dart_file)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Error: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertEqual(data["summary"]["total_files"], 2)
        self.assertIn("python", data["language"])
        self.assertIn("dart", data["language"])

    def test_multiple_targets_mixed_file_and_directory(self) -> None:
        py_file = SCRIPT_DIR / "python" / "cognitive_complexity.py"
        dart_dir = SCRIPT_DIR / "dart" / "lib"
        res = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), "-f", "json", "-t", "100", str(py_file), str(dart_dir)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Error: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertGreaterEqual(data["summary"]["total_files"], 3)

    def test_deduplication_across_targets(self) -> None:
        py_file = SCRIPT_DIR / "python" / "cognitive_complexity.py"
        py_dir = SCRIPT_DIR / "python"
        res = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), "-f", "json", "-t", "100", str(py_file), str(py_dir)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Error: {res.stderr}")
        data = json.loads(res.stdout)
        paths = [f["path"] for f in data["files"]]
        self.assertEqual(len(paths), len(set(paths)), "Duplicate file entries found in report")


if __name__ == "__main__":
    unittest.main()
