import unittest
from unittest.mock import patch, MagicMock, mock_open
import sys
import os
import importlib
import json
import tempfile
import shutil

import split_diff
from split_diff import (
    extract_diff_from_json,
    split_diff as split_diff_fn,
    main
)

class TestSplitDiff(unittest.TestCase):
    def setUp(self):
        importlib.reload(split_diff)
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
            '+new line\n'
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
            printed = "\n".join([call[0][0] for call in mock_print.call_args_list if call[0]])
            self.assertIn(f"Successfully split diff into 1 files in {self.test_dir}", printed)
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
            
            printed = "\n".join([call[0][0] for call in mock_print.call_args_list if call[0]])
            self.assertIn(f"Successfully split diff into 1 files in {self.test_dir}", printed)
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
            printed_err = "\n".join([call[0][0] for call in mock_print.call_args_list if call[0]])
            self.assertIn("Error: Input is not valid JSON", printed_err)
            self.assertEqual(cm.exception.code, 1)

    @patch("sys.exit")
    @patch("builtins.print")
    def test_main_json_missing_diff_key(self, mock_print, mock_exit):
        json_data = json.dumps({"not_diff": "value"})
        
        mock_stdin = MagicMock()
        mock_stdin.read.return_value = json_data
        
        test_args = ["split_diff.py", "--json", "--json-key", "my_missing_key", "--output-dir", self.test_dir]
        with patch.object(sys, "argv", test_args), patch("sys.stdin", mock_stdin):
            main()
            
            # Since my_missing_key is not in json_data, extract_diff_from_json returns "" (empty string)
            # which splits into 0 files
            printed = "\n".join([call[0][0] for call in mock_print.call_args_list if call[0]])
            self.assertIn(f"Successfully split diff into 0 files in {self.test_dir}", printed)
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
            printed_err = "\n".join([call[0][0] for call in mock_print.call_args_list if call[0]])
            self.assertIn("Error: Could not find diff in JSON data", printed_err)
            self.assertEqual(cm.exception.code, 1)

if __name__ == "__main__":
    unittest.main()
