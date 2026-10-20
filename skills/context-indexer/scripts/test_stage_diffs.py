import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import stage_diffs
from stage_diffs import (
    extract_diff_entities,
    extract_diff_from_json,
    main,
    parse_diff_records,
    resolve_repo_dir,
    split_diff_grouped,
)
from stage_diffs import (
    split_diff as split_diff_fn,
)


class TestSplitDiff(unittest.TestCase):
    def setUp(self):
        importlib.reload(stage_diffs)
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_extract_diff_from_json_with_key(self):
        data = {"my_key": "diff_content"}
        self.assertEqual(extract_diff_from_json(data, "my_key"), "diff_content")
        self.assertEqual(extract_diff_from_json(data, "missing_key"), "")

    def test_extract_diff_from_json_string(self):
        self.assertEqual(extract_diff_from_json("raw_diff_string"), "raw_diff_string")

    def test_extract_diff_from_json_common_keys(self):
        self.assertEqual(extract_diff_from_json({"diff": "d1"}), "d1")
        self.assertEqual(extract_diff_from_json({"patch": "p1"}), "p1")
        self.assertEqual(extract_diff_from_json({"content": "c1"}), "c1")

        # Priority test: diff over patch
        self.assertEqual(extract_diff_from_json({"diff": "d1", "patch": "p1"}), "d1")

    def test_extract_diff_from_json_failure(self):
        with self.assertRaises(ValueError):
            extract_diff_from_json({"invalid_key": "value"})

    def test_split_diff_git_format(self):
        diff_content = (
            "diff --git a/file1.py b/file1.py\n"
            "index 12345..67890 100644\n"
            "--- a/file1.py\n"
            "+++ b/file1.py\n"
            "@@ -1,1 +1,2 @@\n"
            " print('hello')\n"
            "+print('world')\n"
            "diff --git a/dir/file2.md b/dir/file2.md\n"
            "--- a/dir/file2.md\n"
            "+++ b/dir/file2.md\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )

        summary = split_diff_fn(diff_content, self.test_dir)
        self.assertEqual(len(summary), 2)
        self.assertIn("- file1.py -> file1.py", summary[0])
        self.assertIn("- dir/file2.md -> dir_file2.md", summary[1])

        # Check files were written
        file1_path = os.path.join(self.test_dir, "file1.py")
        file2_path = os.path.join(self.test_dir, "dir_file2.md")

        self.assertTrue(os.path.exists(file1_path))
        self.assertTrue(os.path.exists(file2_path))

        with open(file1_path, "r") as f:
            content = f.read()
            self.assertTrue(content.startswith("diff --git a/file1.py"))
            self.assertIn("+print('world')", content)

        with open(file2_path, "r") as f:
            content = f.read()
            self.assertTrue(content.startswith("diff --git a/dir/file2.md"))

    def test_split_diff_git_format_with_quotes(self):
        # Diff containing file name with spaces and quotes
        diff_content = (
            'diff --git "a/file name.py" "b/file name.py"\n'
            '--- "a/file name.py"\n'
            '+++ "b/file name.py"\n'
            "+new line\n"
        )
        summary = split_diff_fn(diff_content, self.test_dir)
        self.assertEqual(len(summary), 1)
        self.assertIn("- file name.py -> file name.py", summary[0])

        file_path = os.path.join(self.test_dir, "file name.py")
        self.assertTrue(os.path.exists(file_path))

    def test_split_diff_non_git_format(self):
        # Diff containing only --- a/ format
        diff_content = (
            "--- a/file1.py\t2026-06-25\n"
            "+++ b/file1.py\t2026-06-25\n"
            "+new line\n"
            "--- a/dir/file2.txt\n"
            "+++ b/dir/file2.txt\n"
            "+another line\n"
        )
        summary = split_diff_fn(diff_content, self.test_dir)
        self.assertEqual(len(summary), 2)
        self.assertIn("- file1.py -> file1.py", summary[0])
        self.assertIn("- dir/file2.txt -> dir_file2.txt", summary[1])

    def test_split_diff_no_markers(self):
        diff_content = "some random text with no diff markers"
        summary = split_diff_fn(diff_content, self.test_dir)
        self.assertEqual(len(summary), 1)
        self.assertIn("- chunk_0.diff -> chunk_0.diff", summary[0])

        file_path = os.path.join(self.test_dir, "chunk_0.diff")
        self.assertTrue(os.path.exists(file_path))
        with open(file_path, "r") as f:
            self.assertEqual(f.read(), "some random text with no diff markers")

    # --- Main command line interface tests ---

    @patch("sys.exit")
    @patch("builtins.print")
    def test_main_plain_text(self, mock_print, mock_exit):
        diff_content = "diff --git a/file1.py b/file1.py\n+new line"

        # Mock stdin reading by patching sys.stdin directly
        mock_stdin = MagicMock()
        mock_stdin.read.return_value = diff_content

        test_args = ["split_diff.py", "--output-dir", self.test_dir]
        with patch.object(sys, "argv", test_args), patch("sys.stdin", mock_stdin):
            main()

            # Check success message printed
            printed = "\n".join(
                [call[0][0] for call in mock_print.call_args_list if call[0]]
            )
            self.assertIn(
                f"Successfully split diff into 1 files in {self.test_dir}", printed
            )
            self.assertIn("- file1.py -> file1.py", printed)
            mock_exit.assert_not_called()

    @patch("sys.exit")
    @patch("builtins.print")
    def test_main_json_success(self, mock_print, mock_exit):
        json_data = json.dumps({"diff": "diff --git a/file1.py b/file1.py\n+new line"})

        mock_stdin = MagicMock()
        mock_stdin.read.return_value = json_data

        test_args = ["split_diff.py", "--json", "--output-dir", self.test_dir]
        with patch.object(sys, "argv", test_args), patch("sys.stdin", mock_stdin):
            main()

            printed = "\n".join(
                [call[0][0] for call in mock_print.call_args_list if call[0]]
            )
            self.assertIn(
                f"Successfully split diff into 1 files in {self.test_dir}", printed
            )
            mock_exit.assert_not_called()

    @patch("sys.exit")
    @patch("builtins.print")
    def test_main_json_invalid(self, mock_print, mock_exit):
        def raise_system_exit(code=0):
            raise SystemExit(code)

        mock_exit.side_effect = raise_system_exit

        mock_stdin = MagicMock()
        mock_stdin.read.return_value = "{invalid json"

        test_args = ["split_diff.py", "--json", "--output-dir", self.test_dir]
        with patch.object(sys, "argv", test_args), patch("sys.stdin", mock_stdin):
            with self.assertRaises(SystemExit) as cm:
                main()

            # Check error printed and exited with 1
            printed_err = "\n".join(
                [call[0][0] for call in mock_print.call_args_list if call[0]]
            )
            self.assertIn("Error: Input is not valid JSON", printed_err)
            self.assertEqual(cm.exception.code, 1)

    @patch("sys.exit")
    @patch("builtins.print")
    def test_main_json_missing_diff_key(self, mock_print, mock_exit):
        json_data = json.dumps({"not_diff": "value"})

        mock_stdin = MagicMock()
        mock_stdin.read.return_value = json_data

        test_args = [
            "split_diff.py",
            "--json",
            "--json-key",
            "my_missing_key",
            "--output-dir",
            self.test_dir,
        ]
        with patch.object(sys, "argv", test_args), patch("sys.stdin", mock_stdin):
            main()

            # Since my_missing_key is not in json_data, extract_diff_from_json returns "" (empty string)
            # which splits into 0 files
            printed = "\n".join(
                [call[0][0] for call in mock_print.call_args_list if call[0]]
            )
            self.assertIn(
                f"Successfully split diff into 0 files in {self.test_dir}", printed
            )
            mock_exit.assert_not_called()

    @patch("sys.exit")
    @patch("builtins.print")
    def test_main_json_value_error(self, mock_print, mock_exit):
        def raise_system_exit(code=0):
            raise SystemExit(code)

        mock_exit.side_effect = raise_system_exit

        json_data = json.dumps({"not_diff": "value"})

        mock_stdin = MagicMock()
        mock_stdin.read.return_value = json_data

        test_args = ["split_diff.py", "--json", "--output-dir", self.test_dir]
        with patch.object(sys, "argv", test_args), patch("sys.stdin", mock_stdin):
            with self.assertRaises(SystemExit) as cm:
                main()

            # Check error printed and exited with 1
            printed_err = "\n".join(
                [call[0][0] for call in mock_print.call_args_list if call[0]]
            )
            self.assertIn("Error: Could not find diff in JSON data", printed_err)
            self.assertEqual(cm.exception.code, 1)

    def test_split_diff_grouped_packs_small_files(self):
        diff_content = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old1\n"
            "+new1\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old2\n"
            "+new2\n"
        )
        chunks = split_diff_grouped(
            diff_content, self.test_dir, max_bytes=35000, max_lines=700
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["chunk_name"], "diff_chunk_01.diff")
        self.assertEqual(chunks[0]["files"], ["a.py", "b.py"])
        self.assertTrue(os.path.isfile(chunks[0]["chunk_file"]))

        manifest_path = os.path.join(self.test_dir, "manifest.json")
        self.assertTrue(os.path.isfile(manifest_path))
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["chunk_count"], 1)
        self.assertEqual(manifest["chunks"][0]["files"], ["a.py", "b.py"])

    def test_split_diff_grouped_splits_across_chunks_when_exceeding_limits(self):
        diff_content = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,2 +1,2 @@\n"
            "+line1\n"
            "+line2\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,2 +1,2 @@\n"
            "+line3\n"
            "+line4\n"
        )
        chunks = split_diff_grouped(
            diff_content, self.test_dir, max_bytes=35000, max_lines=8
        )
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["files"], ["a.py"])
        self.assertEqual(chunks[1]["files"], ["b.py"])

    def test_split_diff_grouped_splits_oversized_single_file_by_hunks(self):
        diff_content = (
            "diff --git a/big.py b/big.py\n"
            "--- a/big.py\n"
            "+++ b/big.py\n"
            "@@ -1,3 +1,3 @@\n"
            "+hunk1_a\n"
            "+hunk1_b\n"
            "+hunk1_c\n"
            "@@ -20,3 +20,3 @@\n"
            "+hunk2_a\n"
            "+hunk2_b\n"
            "+hunk2_c\n"
        )
        chunks = split_diff_grouped(
            diff_content, self.test_dir, max_bytes=35000, max_lines=9
        )
        self.assertEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertEqual(chunk["files"], ["big.py"])
            with open(chunk["chunk_file"], "r", encoding="utf-8") as f:
                text = f.read()
            self.assertTrue(
                text.startswith(
                    "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
                )
            )

    def test_split_diff_grouped_splits_oversized_single_hunk_by_lines(self):
        diff_content = (
            "diff --git a/huge.py b/huge.py\n"
            "--- a/huge.py\n"
            "+++ b/huge.py\n"
            "@@ -1,6 +1,6 @@\n"
            "+line1\n"
            "+line2\n"
            "+line3\n"
            "+line4\n"
            "+line5\n"
            "+line6\n"
        )
        chunks = split_diff_grouped(
            diff_content, self.test_dir, max_bytes=35000, max_lines=7
        )
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(chunk["lines"], 7)
            with open(chunk["chunk_file"], "r", encoding="utf-8") as f:
                text = f.read()
            self.assertTrue(text.startswith("diff --git a/huge.py b/huge.py\n"))

    @patch("sys.exit")
    @patch("builtins.print")
    def test_main_grouped(self, mock_print, mock_exit):
        diff_content = (
            "diff --git a/file1.py b/file1.py\n"
            "--- a/file1.py\n"
            "+++ b/file1.py\n"
            "@@ -1,1 +1,1 @@\n"
            "+new line\n"
        )
        mock_stdin = MagicMock()
        mock_stdin.read.return_value = diff_content

        test_args = ["split_diff.py", "--grouped", "--output-dir", self.test_dir]
        with patch.object(sys, "argv", test_args), patch("sys.stdin", mock_stdin):
            main()
            printed = "\n".join(
                [call[0][0] for call in mock_print.call_args_list if call[0]]
            )
            self.assertIn("Successfully split diff into 1 grouped chunk(s)", printed)
            self.assertIn("diff_chunk_01.diff", printed)
            mock_exit.assert_not_called()

    def test_parse_diff_records_line_numbers_and_edge_cases(self):
        diff_content = (
            'diff --git "a/dir/file\\303\\251.py" "b/dir/file\\303\\251.py"\n'
            '--- "a/dir/file\\303\\251.py"\n'
            '+++ "b/dir/file\\303\\251.py"\n'
            "@@ -10,3 +10,3 @@ def greet():\n"
            " context_before\n"
            "-old_val\n"
            "+new_val\n"
            " context_after\n"
            "\\ No newline at end of file\n"
            "diff --git a/deleted.py b/deleted.py\n"
            "--- a/deleted.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-gone\n"
        )
        hunks, lines = parse_diff_records(diff_content)
        self.assertEqual(len(hunks), 2)
        self.assertEqual(hunks[0]["file_path"], "dir/fileé.py")
        self.assertEqual(hunks[0]["func_context"], "def greet():")
        self.assertEqual(hunks[0]["added_lines"], 1)
        self.assertEqual(hunks[0]["deleted_lines"], 1)

        self.assertEqual(hunks[1]["file_path"], "deleted.py")
        self.assertEqual(hunks[1]["old_start"], 1)
        self.assertEqual(hunks[1]["old_count"], 1)
        self.assertEqual(hunks[1]["new_count"], 0)

        self.assertEqual(len(lines), 5)
        self.assertEqual(
            [(r["op"], r["old_line"], r["new_line"], r["content"]) for r in lines],
            [
                (" ", 10, 10, "context_before"),
                ("-", 11, None, "old_val"),
                ("+", None, 11, "new_val"),
                (" ", 12, 12, "context_after"),
                ("-", 1, None, "gone"),
            ],
        )

    def test_split_diff_grouped_preserves_monotonic_lines_on_split_hunk(self):
        diff_content = (
            "diff --git a/huge.py b/huge.py\n"
            "--- a/huge.py\n"
            "+++ b/huge.py\n"
            "@@ -100,6 +200,6 @@ def big():\n"
            "+line200\n"
            "+line201\n"
            "+line202\n"
            "+line203\n"
            "+line204\n"
            "+line205\n"
        )
        chunks = split_diff_grouped(
            diff_content, self.test_dir, max_bytes=35000, max_lines=7
        )
        self.assertGreaterEqual(len(chunks), 2)

        lines_path = os.path.join(self.test_dir, "diff_lines.jsonl")
        hunks_path = os.path.join(self.test_dir, "diff_hunks.jsonl")
        entities_path = os.path.join(self.test_dir, "diff_entities.jsonl")
        self.assertTrue(os.path.isfile(lines_path))
        self.assertTrue(os.path.isfile(hunks_path))
        self.assertTrue(os.path.isfile(entities_path))

        with open(lines_path, "r", encoding="utf-8") as f:
            line_recs = [json.loads(line) for line in f if line.strip()]
        with open(hunks_path, "r", encoding="utf-8") as f:
            hunk_recs = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(
            [r["new_line"] for r in line_recs], [200, 201, 202, 203, 204, 205]
        )
        self.assertEqual(line_recs[0]["chunk_name"], "diff_chunk_01.diff")
        self.assertEqual(
            line_recs[-1]["chunk_name"], f"diff_chunk_{len(chunks):02d}.diff"
        )
        self.assertEqual(len(hunk_recs), 1)
        self.assertEqual(hunk_recs[0]["hunk_id"], 1)
        self.assertEqual(hunk_recs[0]["chunk_name"], "diff_chunk_01.diff")

    def test_extract_diff_entities_python_and_brace_languages(self):
        py_path = os.path.join(self.test_dir, "mod.py")
        ts_path = os.path.join(self.test_dir, "service.ts")
        with open(py_path, "w", encoding="utf-8") as f:
            f.write(
                "class Worker:\n"
                "    def run(self, count: int) -> bool:\n"
                "        return count > 0\n\n"
                "async def helper(name: str) -> str:\n"
                "    return name\n"
            )
        with open(ts_path, "w", encoding="utf-8") as f:
            f.write(
                "export class Client {\n"
                "  public fetch(url: string): void {\n"
                "    if (url) {\n"
                "      return;\n"
                "    }\n"
                "  }\n"
                "}\n"
                "export function makeClient(id: number) {\n"
                "  return id;\n"
                "}\n"
            )

        with patch("stage_diffs._read_git_blob") as mock_blob:
            mock_blob.side_effect = lambda repo, ref, path: (
                "def helper(name: str) -> int:\n    return 0\n"
                if path == "mod.py"
                else None
            )
            entities = extract_diff_entities(
                ["mod.py", "service.ts"], self.test_dir, base_ref="HEAD~1"
            )

        by_key = {(e["file_path"], e["side"], e["name"]): e for e in entities}
        self.assertEqual(by_key[("mod.py", "NEW", "Worker")]["kind"], "class")
        self.assertEqual(by_key[("mod.py", "NEW", "Worker")]["line_end"], 3)
        self.assertEqual(by_key[("mod.py", "NEW", "run")]["kind"], "method")
        self.assertIn(
            "def run(self, count: int) -> bool",
            by_key[("mod.py", "NEW", "run")]["signature"],
        )
        self.assertEqual(by_key[("mod.py", "NEW", "helper")]["kind"], "function")
        self.assertIn(
            "async def helper(name: str) -> str",
            by_key[("mod.py", "NEW", "helper")]["signature"],
        )
        self.assertIn(
            "def helper(name: str) -> int",
            by_key[("mod.py", "OLD", "helper")]["signature"],
        )

        self.assertEqual(by_key[("service.ts", "NEW", "Client")]["kind"], "class")
        self.assertEqual(by_key[("service.ts", "NEW", "Client")]["line_end"], 7)
        self.assertEqual(by_key[("service.ts", "NEW", "fetch")]["kind"], "method")
        self.assertEqual(
            by_key[("service.ts", "NEW", "makeClient")]["kind"], "function"
        )

    def test_resolve_repo_dir_formats(self):
        txt_file = os.path.join(self.test_dir, "workspace_dir.txt")
        with open(txt_file, "w", encoding="utf-8") as f:
            f.write(f"{self.test_dir}\n")
        self.assertEqual(resolve_repo_dir(txt_file), self.test_dir)

        json_file = os.path.join(self.test_dir, "pr_coords.json")
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump({"workspace_dir": self.test_dir}, f)
        self.assertEqual(resolve_repo_dir(json_file), self.test_dir)


if __name__ == "__main__":
    unittest.main()
