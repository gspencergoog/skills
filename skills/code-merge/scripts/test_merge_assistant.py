#!/usr/bin/env python3
"""
Unit tests for merge_assistant.py.
Tests multi-language AST extraction, in-memory 3-way merge, squash-merge ancestry,
cross-directory move resolution via git merge-file, semantic hazard detection,
and cross-repo patch porting.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure local script directory is on sys.path for run_skill_tests.py compatibility
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import merge_assistant


class TestMergeAssistant(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_merge_assistant_")
        self.repo = Path(self.test_dir)
        self._git(["init", "-b", "main"])
        self._git(["config", "user.name", "Test Agent"])
        self._git(["config", "user.email", "agent@test.local"])

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _git(self, args, cwd=None):
        target_cwd = str(cwd) if cwd else str(self.repo)
        res = subprocess.run(
            ["git"] + args,
            cwd=target_cwd,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"git command failed: {' '.join(args)}\n{res.stderr}",
        )
        return res.stdout.strip()

    def test_multi_language_ast_extraction(self):
        # 1. Python AST
        py_code = "def calculate_total(items, tax_rate=0.05):\n    return sum(items) * (1 + tax_rate)\n"
        py_syms = merge_assistant.extract_ast_symbols(Path("calc.py"), py_code)
        self.assertTrue(
            any(
                s.name == "calculate_total" and "tax_rate" in s.params
                for s in py_syms
            )
        )

        # 2. TypeScript / JS
        ts_code = "export class DataProcessor {\n  processRecord(payload: string): boolean {\n    return true;\n  }\n}\n"
        ts_syms = merge_assistant.extract_ast_symbols(Path("proc.ts"), ts_code)
        self.assertTrue(any(s.name == "DataProcessor" for s in ts_syms))
        self.assertTrue(any(s.name == "processRecord" for s in ts_syms))

        # 3. Dart
        dart_code = "class MessageHandler {\n  Future<void> handleMessage(String id) async {}\n}\n"
        dart_syms = merge_assistant.extract_ast_symbols(
            Path("handler.dart"), dart_code
        )
        self.assertTrue(any(s.name == "MessageHandler" for s in dart_syms))
        self.assertTrue(any(s.name == "handleMessage" for s in dart_syms))

        # 4. Swift & Kotlin
        swift_code = "public struct SurfaceModel {\n  public func resolvePath(id: String) -> Bool {\n    return true\n  }\n}\n"
        swift_syms = merge_assistant.extract_ast_symbols(
            Path("model.swift"), swift_code
        )
        self.assertTrue(any(s.name == "SurfaceModel" for s in swift_syms))
        self.assertTrue(any(s.name == "resolvePath" for s in swift_syms))

        # 5. JSON Schema
        schema_json = '{"$defs": {"TestCase": {"type": "object"}}, "properties": {"action": {"enum": ["compile", "run"]}}}'
        schema_syms = merge_assistant.extract_ast_symbols(
            Path("spec.json"), schema_json
        )
        self.assertTrue(any(s.name == "TestCase" for s in schema_syms))
        self.assertTrue(any("compile" in s.name for s in schema_syms))

    def test_in_memory_merge_clean_and_conflict(self):
        f = self.repo / "sample.txt"
        f.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
        self._git(["add", "sample.txt"])
        self._git(["commit", "-m", "initial commit"])

        # Branch 1: Modify line 1
        self._git(["checkout", "-b", "branch1"])
        f.write_text("branch1 line 1\nline 2\nline 3\n", encoding="utf-8")
        self._git(["commit", "-am", "branch1 commit"])

        # Branch 2: Modify line 3 (Orthogonal)
        self._git(["checkout", "main"])
        self._git(["checkout", "-b", "branch2"])
        f.write_text("line 1\nline 2\nbranch2 line 3\n", encoding="utf-8")
        self._git(["commit", "-am", "branch2 commit"])

        base = merge_assistant.find_merge_base(self.repo, "branch1", "branch2")
        code, _, conflicts = merge_assistant.run_in_memory_merge(
            self.repo, base, "branch1", "branch2"
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(conflicts), 0)

        # Branch 3: Conflicting edit on line 1
        self._git(["checkout", "main"])
        self._git(["checkout", "-b", "branch3"])
        f.write_text(
            "branch3 conflicting line\nline 2\nline 3\n", encoding="utf-8"
        )
        self._git(["commit", "-am", "branch3 commit"])

        code_c, _, conflicts_c = merge_assistant.run_in_memory_merge(
            self.repo, base, "branch1", "branch3"
        )
        self.assertNotEqual(code_c, 0)
        self.assertEqual(len(conflicts_c), 1)
        self.assertEqual(conflicts_c[0].path, "sample.txt")

    def test_cross_directory_merge_moved_file(self):
        old_file = self.repo / "old_dir" / "service.py"
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_text(
            "def header():\n    return 'v1'\n\ndef run():\n    step = 1\n    return 'base'\n",
            encoding="utf-8",
        )
        self._git(["add", "."])
        self._git(["commit", "-m", "add old_dir/service.py"])
        base_sha = self._git(["rev-parse", "HEAD"])

        # main_refactor: move to new_dir/service.py and edit header()
        self._git(["checkout", "-b", "main_refactor"])
        self._git(["rm", "old_dir/service.py"])
        new_file = self.repo / "new_dir" / "service.py"
        new_file.parent.mkdir(parents=True, exist_ok=True)
        new_file.write_text(
            "def header():\n    return 'v2_refactored'\n\ndef run():\n    step = 1\n    return 'base'\n",
            encoding="utf-8",
        )
        self._git(["add", "."])
        self._git(["commit", "-m", "move to new_dir and refactor header"])

        # feature_fix: edit run() in old_dir/service.py
        self._git(["checkout", "main"])
        self._git(["checkout", "-b", "feature_fix"])
        old_file.write_text(
            "def header():\n    return 'v1'\n\ndef run():\n    step = 1\n    return 'base_bugfixed'\n",
            encoding="utf-8",
        )
        self._git(["commit", "-am", "fix bug in old_dir/service.py"])
        theirs_sha = self._git(["rev-parse", "HEAD"])

        # Switch to main_refactor and run merge_moved_file
        self._git(["checkout", "main_refactor"])
        code, _ = merge_assistant.merge_moved_file(
            repo=self.repo,
            old_path="old_dir/service.py",
            new_path="new_dir/service.py",
            base=base_sha,
            theirs=theirs_sha,
            stage_rm=True,
        )
        self.assertEqual(code, 0)
        merged_content = (self.repo / "new_dir" / "service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("v2_refactored", merged_content)
        self.assertIn("base_bugfixed", merged_content)

    def test_squash_merge_ancestry_and_semantic_hazards(self):
        # Create initial file with DirectJsonParser(catalog, validator=None)
        parser_file = self.repo / "parser.py"
        parser_file.write_text(
            "def create_parser(catalog, validator=None):\n    return catalog\n",
            encoding="utf-8",
        )
        self._git(["add", "parser.py"])
        self._git(["commit", "-m", "base parser"])
        base_sha = self._git(["rev-parse", "HEAD"])

        # Commit on main
        readme = self.repo / "README.md"
        readme.write_text("main update 1\n", encoding="utf-8")
        self._git(["add", "README.md"])
        self._git(["commit", "-m", "main update 1"])
        main_sync_sha = self._git(["rev-parse", "HEAD"])

        # Create v1_0 branch from base_sha, record a squash merge of main_sync_sha, and drop validator param
        self._git(["checkout", "-b", "v1_0", base_sha])
        readme.write_text("main update 1\n", encoding="utf-8")
        parser_file.write_text(
            "def create_parser(catalog):\n    return catalog\n",
            encoding="utf-8",
        )
        self._git(["add", "."])
        self._git(
            [
                "commit",
                "-m",
                f"Merge main into v1_0 (#2680)\n\nIncorporates {main_sync_sha}",
            ]
        )

        # Advance main with a new caller to create_parser
        self._git(["checkout", "main"])
        caller = self.repo / "caller.py"
        caller.write_text(
            "from parser import create_parser\ncreate_parser('cat', validator='v')\n",
            encoding="utf-8",
        )
        self._git(["add", "caller.py"])
        self._git(["commit", "-m", "add caller on main"])

        # Verify squash ancestry detection finds main_sync_sha
        squash = merge_assistant.detect_squash_merge_ancestry(
            self.repo, "v1_0", "main"
        )
        self.assertIsNotNone(squash)
        self.assertEqual(squash["prior_theirs_sha"], main_sync_sha)

        # Verify semantic hazard detection flags signature_drift on create_parser
        hazards = merge_assistant.detect_semantic_hazards(
            self.repo, base_sha, "v1_0", "main", []
        )
        self.assertTrue(
            any(
                h.symbol_name == "create_parser"
                and h.hazard_type == "signature_drift"
                for h in hazards
            )
        )

    def test_verify_clean_and_leftover_markers(self):
        clean, issues = merge_assistant.verify_merge_state(self.repo)
        self.assertTrue(clean)
        self.assertEqual(len(issues), 0)

        dirty_file = self.repo / "dirty.txt"
        dirty_file.write_text(
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>>\n", encoding="utf-8"
        )
        self._git(["add", "dirty.txt"])
        clean_d, issues_d = merge_assistant.verify_merge_state(self.repo)
        self.assertFalse(clean_d)
        self.assertTrue(
            any("Leftover conflict marker" in iss for iss in issues_d)
        )


if __name__ == "__main__":
    unittest.main()
