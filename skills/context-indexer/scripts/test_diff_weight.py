"""Tests for diff_weight.py."""

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import diff_weight
from diff_weight import (
    TEST_LINE_WEIGHT,
    classify_low_content_path,
    compute_substantive_diff_stats,
    is_logic_line,
    is_test_path,
)

SCRIPTS_DIR = Path(__file__).resolve().parent


def _file_diff(path: str, added: list[str], removed: list[str] | None = None) -> str:
    """Builds a -U0 single-hunk file diff like review_engine's pr.diff."""
    removed = removed or []
    header = (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{len(removed)} +1,{len(added)} @@\n"
    )
    body = "".join(f"-{line}\n" for line in removed) + "".join(
        f"+{line}\n" for line in added
    )
    return header + body


def _code_lines(n: int, prefix: str = "x") -> list[str]:
    return [f"{prefix}{i} = compute({i})" for i in range(n)]


class TestClassifyLowContentPath(unittest.TestCase):
    def test_lockfiles(self):
        for path in [
            "package-lock.json",
            "web/yarn.lock",
            "pubspec.lock",
            "go.sum",
            "Cargo.lock",
            "uv.lock",
        ]:
            self.assertEqual(classify_low_content_path(path), "lockfile", path)

    def test_generated(self):
        for path in [
            "lib/model.g.dart",
            "api/foo.pb.go",
            "proto/x_pb2.py",
            "dist/app.js",
            "app.min.js",
            "vendor/lib/a.go",
            "a.js.map",
        ]:
            self.assertEqual(classify_low_content_path(path), "generated", path)

    def test_build_dir_only_at_top_level(self):
        self.assertIsNone(classify_low_content_path("lib/src/build/runner.dart"))

    def test_snapshot_or_fixture(self):
        for path in [
            "src/__snapshots__/a.test.ts.snap",
            "pkg/testdata/in.json",
            "test/fixtures/x.json",
            "goldens/button.png",
            "out.snap",
        ]:
            self.assertEqual(
                classify_low_content_path(path), "snapshot_or_fixture", path
            )

    def test_asset_or_l10n(self):
        for path in ["icons/logo.svg", "lib/l10n/app_en.arb", "locale/fr.po"]:
            self.assertEqual(classify_low_content_path(path), "asset_or_l10n", path)

    def test_docs(self):
        for path in [
            "docs/guide.adoc",
            "docs/guide.rst",
            "CHANGELOG",
            "LICENSE.txt",
            "notes.txt",
        ]:
            self.assertEqual(classify_low_content_path(path), "docs", path)

    def test_code_counts(self):
        for path in [
            "src/main.py",
            "lib/widget.dart",
            "pubspec.yaml",
            "package.json",
            "BUILD",
        ]:
            self.assertIsNone(classify_low_content_path(path), path)


class TestIsTestPath(unittest.TestCase):
    def test_test_paths(self):
        for path in [
            "test/foo_test.dart",
            "pkg/tests/util.py",
            "src/a.test.ts",
            "src/a.spec.js",
            "scripts/test_engine.py",
            "FooTest.java",
        ]:
            self.assertTrue(is_test_path(path), path)

    def test_prod_paths(self):
        for path in ["src/main.py", "lib/testing_utils_impl.dart", "contest.py"]:
            self.assertFalse(is_test_path(path), path)


class TestIsLogicLine(unittest.TestCase):
    def test_non_logic(self):
        for line in [
            "",
            "   ",
            "// comment",
            "# comment",
            "/* block",
            " * doc",
            "*/",
            "<!-- x -->",
            "-- sql comment",
            "}",
            "});",
            "],",
            "import os",
            "from a import b",
            "export * from './x';",
            "part 'a.g.dart';",
        ]:
            self.assertFalse(is_logic_line(line), repr(line))

    def test_logic(self):
        for line in [
            "x = 1",
            "return value;",
            "*ptr = 3;",
            "if (a) {",
            "def foo():",
            "--count;",
        ]:
            self.assertTrue(is_logic_line(line), repr(line))


class TestComputeSubstantiveDiffStats(unittest.TestCase):
    def test_test_weighting(self):
        diff = _file_diff("src/fix.py", _code_lines(40)) + _file_diff(
            "tests/test_fix.py", _code_lines(280, "t")
        )
        stats = compute_substantive_diff_stats(diff)
        self.assertEqual(stats.prod_logic_lines, 40)
        self.assertEqual(stats.test_logic_lines, 280)
        self.assertEqual(stats.substantive_lines, 40 + TEST_LINE_WEIGHT * 280)
        self.assertEqual(stats.substantive_lines, 110)
        self.assertEqual(stats.code_files, 2)

    def test_lockfile_does_not_inflate(self):
        lock = [f'  "dep{i}": "1.0.{i}",' for i in range(7000)]
        diff = _file_diff("package-lock.json", lock) + _file_diff(
            "src/app.ts", _code_lines(40)
        )
        stats = compute_substantive_diff_stats(diff)
        self.assertEqual(stats.substantive_lines, 40)
        self.assertEqual(stats.raw_lines, 7040)
        self.assertEqual(stats.excluded_files, {"package-lock.json": "lockfile"})

    def test_each_low_content_kind_excluded(self):
        paths = [
            "yarn.lock",
            "lib/a.g.dart",
            "testdata/x.json",
            "logo.svg",
            "README.rst",
        ]
        diff = "".join(_file_diff(p, _code_lines(10)) for p in paths)
        stats = compute_substantive_diff_stats(diff)
        self.assertEqual(stats.substantive_lines, 0)
        self.assertEqual(
            set(stats.excluded_files.values()),
            {"lockfile", "generated", "snapshot_or_fixture", "asset_or_l10n", "docs"},
        )

    def test_comments_imports_and_delimiters_excluded(self):
        lines = ["import os", "# note", "", "def f():", "    return 1", "}"]
        stats = compute_substantive_diff_stats(_file_diff("src/a.py", lines))
        self.assertEqual(stats.prod_logic_lines, 2)

    def test_pure_rename(self):
        diff = (
            "diff --git a/old/name.py b/new/name.py\n"
            "similarity index 100%\n"
            "rename from old/name.py\n"
            "rename to new/name.py\n"
        )
        stats = compute_substantive_diff_stats(diff)
        self.assertEqual(stats.substantive_lines, 0)
        self.assertEqual(stats.excluded_files, {"new/name.py": "pure_rename"})

    def test_whitespace_reflow_not_counted(self):
        removed = ["foo(a, b, c)", "x = 1"]
        added = ["foo(", "a, b, c)", "    x  =  1", "y = 2"]
        # "foo(a, b, c)" vs the split lines do not match as single lines, but
        # "x = 1" and "    x  =  1" do, so they cancel out.
        stats = compute_substantive_diff_stats(_file_diff("src/a.py", added, removed))
        self.assertEqual(stats.raw_lines, 6)
        self.assertEqual(stats.prod_logic_lines, 4)

    def test_trailing_comma_reflow_not_counted(self):
        stats = compute_substantive_diff_stats(
            _file_diff("src/a.dart", ["  value: 1,"], ["  value: 1"])
        )
        self.assertEqual(stats.prod_logic_lines, 0)

    def test_empty_diff(self):
        stats = compute_substantive_diff_stats("")
        self.assertEqual(stats.substantive_lines, 0)
        self.assertEqual(stats.excluded_files, {})

    def test_to_dict_is_json_serializable(self):
        stats = compute_substantive_diff_stats(_file_diff("src/a.py", ["x = 1"]))
        self.assertEqual(json.loads(json.dumps(stats.to_dict()))["prod_logic_lines"], 1)


class TestMarkdown(unittest.TestCase):
    def test_all_markdown_counts(self):
        for path in [
            "agents/skills/pr-review/SKILL.md",
            "AGENTS.md",
            "agents/skills/code-review/references/lanes.md",
            "README.md",
            "docs/blueprint.md",
            "CHANGELOG.md",
            "site/page.mdx",
        ]:
            self.assertTrue(diff_weight.is_markdown_path(path), path)
            self.assertIsNone(classify_low_content_path(path), path)

    def test_non_markdown_docs_still_low_content(self):
        for path in ["docs/guide.rst", "references/notes.txt", "CHANGELOG", "LICENSE"]:
            self.assertFalse(diff_weight.is_markdown_path(path), path)
            self.assertEqual(classify_low_content_path(path), "docs", path)

    def test_markdown_counts_every_non_blank_line(self):
        lines = ["## Step 1", "- Run the tool.", "* Check output.", "", "Then stop."]
        for path in ["agents/skills/foo/SKILL.md", "docs/blueprint.md"]:
            stats = compute_substantive_diff_stats(_file_diff(path, lines))
            self.assertEqual(stats.prod_logic_lines, 4, path)
            self.assertEqual(stats.excluded_files, {}, path)

    def test_markdown_reflow_not_counted(self):
        stats = compute_substantive_diff_stats(
            _file_diff("README.md", ["  Some   text."], ["Some text."])
        )
        self.assertEqual(stats.prod_logic_lines, 0)


class TestCli(unittest.TestCase):
    def test_main_reads_file(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as f:
            f.write(_file_diff("src/a.py", ["x = 1", "y = 2"]))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(diff_weight.main([f.name]), 0)
        Path(f.name).unlink()
        self.assertEqual(json.loads(out.getvalue())["substantive_lines"], 2)


class TestImportOrder(unittest.TestCase):
    def test_both_import_orders_succeed(self):
        for code in [
            "import stage_diffs, diff_weight",
            "import diff_weight, stage_diffs",
        ]:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code + "; diff_weight.compute_substantive_diff_stats('')",
                ],
                cwd=SCRIPTS_DIR,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
