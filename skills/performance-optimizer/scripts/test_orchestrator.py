import unittest
from unittest.mock import patch, MagicMock, mock_open
import sys
import os
import json
import subprocess

import orchestrator

class TestOrchestrator(unittest.TestCase):
    def test_compare_metrics_single_better(self):
        config = {
            "metrics": [
                {"name": "latency", "type": "float", "higher_is_better": False, "priority": 1}
            ]
        }
        cand = {"latency": 0.1}
        base = {"latency": 0.5}
        self.assertTrue(orchestrator.compare_metrics(cand, base, config))

    def test_compare_metrics_single_worse(self):
        config = {
            "metrics": [
                {"name": "latency", "type": "float", "higher_is_better": False, "priority": 1}
            ]
        }
        cand = {"latency": 0.8}
        base = {"latency": 0.5}
        self.assertFalse(orchestrator.compare_metrics(cand, base, config))

    def test_compare_metrics_priority_tie_break(self):
        config = {
            "metrics": [
                {"name": "accuracy", "type": "float", "higher_is_better": True, "priority": 1},
                {"name": "latency", "type": "float", "higher_is_better": False, "priority": 2}
            ]
        }
        cand1 = {"accuracy": 0.9, "latency": 0.1}
        base = {"accuracy": 0.9, "latency": 0.3}
        self.assertTrue(orchestrator.compare_metrics(cand1, base, config))

        cand2 = {"accuracy": 0.8, "latency": 0.1}
        self.assertFalse(orchestrator.compare_metrics(cand2, base, config))

    @patch("orchestrator.subprocess.run")
    def test_get_git_diff_success(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.stdout = "git diff data"
        mock_run.return_value = mock_proc
        diff = orchestrator.get_git_diff(".", "branch1", "main")
        self.assertEqual(diff, "git diff data")

    @patch("orchestrator.subprocess.run", side_effect=Exception("Git error"))
    def test_get_git_diff_error(self, mock_run):
        diff = orchestrator.get_git_diff(".", "branch1", "main")
        self.assertEqual(diff, "")

    def test_load_metrics_config_not_found(self):
        with self.assertRaises(FileNotFoundError):
            orchestrator.load_metrics_config("nonexistent_spec_abc.json")

    @patch("orchestrator.subprocess.run")
    def test_setup_worktree(self, mock_run):
        orchestrator.setup_worktree("/workspace", "my-branch", "/wt/path")
        ws_abs = os.path.abspath("/workspace")
        wt_abs = os.path.abspath("/wt/path")
        mock_run.assert_any_call(["git", "worktree", "prune"], cwd=ws_abs, stdout=subprocess.DEVNULL)
        mock_run.assert_any_call(["git", "worktree", "add", "-f", wt_abs, "my-branch"], cwd=ws_abs, check=True)

    @patch("os.path.exists", return_value=True)
    @patch("orchestrator.subprocess.run")
    def test_cleanup_worktree(self, mock_run, mock_exists):
        orchestrator.cleanup_worktree("/workspace", "/wt/path")
        ws_abs = os.path.abspath("/workspace")
        wt_abs = os.path.abspath("/wt/path")
        mock_run.assert_any_call(["git", "worktree", "remove", "--force", wt_abs], cwd=ws_abs, stderr=subprocess.DEVNULL)
        mock_run.assert_any_call(["git", "worktree", "prune"], cwd=ws_abs, stdout=subprocess.DEVNULL)

    def test_update_history_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "history.json")
            md_path = os.path.join(tmpdir, "history.md")
            
            step_entry = {
                "step": 1,
                "timestamp": "2026-06-17T11:00:00",
                "base_branch": "main",
                "baseline_metrics": {"latency": 0.5},
                "candidates": [{"name": "cand1", "branch": "cand1", "metrics": {"latency": 0.2}, "commentary": "Nice"}],
                "winner": "cand1",
                "status": "IMPROVED"
            }
            
            orchestrator.update_history_files(tmpdir, json_path, md_path, step_entry)
            
            self.assertTrue(os.path.exists(json_path))
            self.assertTrue(os.path.exists(md_path))
            
            with open(json_path, "r") as f:
                data = json.load(f)
                self.assertEqual(len(data), 1)
                self.assertEqual(data[0]["step"], 1)

    @patch("os.path.expanduser")
    @patch("os.path.exists", return_value=True)
    @patch("os.listdir")
    @patch("shutil.copy2")
    @patch("shutil.copytree")
    @patch("builtins.open", new_callable=mock_open)
    def test_register_sidecar_dashboard(self, mock_file, mock_copytree, mock_copy2, mock_listdir, mock_exists, mock_expanduser):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_expanduser.return_value = tmpdir
            mock_listdir.return_value = ["sidecar.json", "server.py"]
            
            orchestrator.register_sidecar_dashboard(".")
            
            target_dir = os.path.join(tmpdir, "sidecars", "performance_optimizer_dashboard")
            mock_copy2.assert_called()
            mock_file.assert_called_with(os.path.join(target_dir, "workspace_path.txt"), "w")

    @patch("container_runner.run_benchmark")
    @patch("container_runner.parse_metrics")
    @patch("builtins.open", new_callable=mock_open)
    def test_do_verify(self, mock_file, mock_parse, mock_run):
        mock_run.return_value = ("stdout", "stderr", 0)
        mock_parse.return_value = {"latency": 0.5}
        
        args = MagicMock()
        args.workspace = "."
        args.benchmark_cmd = "echo"
        args.environment = "local"
        args.podman_image = None
        args.metrics_config = "config.json"
        
        orchestrator.do_verify(args)
        
        mock_run.assert_called_once_with(workspace=".", command="echo", environment="local", podman_image=None)
        mock_parse.assert_called_once_with(".", "stdout", "stderr", "config.json")
        mock_file.assert_called_with("./optimization_baseline.json", "w")

    @patch("container_runner.run_benchmark")
    def test_do_verify_failure(self, mock_run):
        mock_run.return_value = ("", "Error stderr", 1)
        args = MagicMock()
        args.workspace = "."
        args.benchmark_cmd = "echo"
        args.environment = "local"
        args.podman_image = None
        args.metrics_config = "c.json"
        with self.assertRaises(SystemExit):
            orchestrator.do_verify(args)

    def test_do_evaluate_empty_branches(self):
        args = MagicMock()
        args.branches = ""
        with self.assertRaises(SystemExit):
            orchestrator.do_evaluate(args)

    @patch("orchestrator.setup_worktree")
    @patch("orchestrator.cleanup_worktree")
    def test_do_evaluate_podman_no_image(self, mock_cleanup, mock_setup):
        args = MagicMock()
        args.branches = "b1"
        args.environment = "podman"
        args.podman_image = None
        with self.assertRaises(SystemExit):
            orchestrator.do_evaluate(args)

    @patch("orchestrator.setup_worktree")
    @patch("orchestrator.cleanup_worktree")
    @patch("orchestrator.subprocess.Popen")
    @patch("container_runner.parse_metrics")
    @patch("builtins.open", new_callable=mock_open)
    def test_do_evaluate(self, mock_file, mock_parse, mock_popen, mock_cleanup, mock_setup):
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc
        mock_parse.return_value = {"latency": 0.1}
        
        args = MagicMock()
        args.workspace = "."
        args.branches = "branch1,branch2"
        args.benchmark_cmd = "echo"
        args.environment = "local"
        args.podman_image = None
        args.metrics_config = "config.json"
        
        orchestrator.do_evaluate(args)
        
        self.assertEqual(mock_setup.call_count, 2)
        self.assertEqual(mock_cleanup.call_count, 2)
        mock_popen.assert_called()

    @patch("orchestrator.setup_worktree")
    @patch("orchestrator.cleanup_worktree")
    @patch("orchestrator.subprocess.Popen")
    @patch("container_runner.parse_metrics")
    @patch("builtins.open", new_callable=mock_open)
    def test_do_evaluate_podman(self, mock_file, mock_parse, mock_popen, mock_cleanup, mock_setup):
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc
        mock_parse.return_value = {"latency": 0.1}
        
        args = MagicMock()
        args.workspace = "."
        args.branches = "branch1"
        args.benchmark_cmd = "echo"
        args.environment = "podman"
        args.podman_image = "python:slim"
        args.metrics_config = "config.json"
        
        orchestrator.do_evaluate(args)
        
        mock_setup.assert_called_once()
        mock_popen.assert_called_once()
        called_args = mock_popen.call_args[0][0]
        self.assertIn("podman", called_args)

    def test_do_select_missing_baseline(self):
        args = MagicMock()
        args.workspace = "./nonexistent_workspace"
        with self.assertRaises(SystemExit):
            orchestrator.do_select(args)

    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open, read_data='{"latency": 0.5}')
    def test_do_select_missing_results(self, mock_file, mock_exists):
        mock_exists.side_effect = lambda p: p.endswith("optimization_baseline.json")
        args = MagicMock()
        args.workspace = "."
        with self.assertRaises(SystemExit):
            orchestrator.do_select(args)

    @patch("orchestrator.register_sidecar_dashboard")
    @patch("orchestrator.update_history_files")
    @patch("orchestrator.get_git_diff", return_value="diff")
    @patch("orchestrator.load_metrics_config")
    @patch("orchestrator.subprocess.run")
    @patch("builtins.open", new_callable=mock_open)
    @patch("os.path.exists", return_value=True)
    def test_do_select_winner(self, mock_exists, mock_open_file, mock_sub, mock_load_config, mock_diff, mock_update, mock_register):
        mock_exists.side_effect = lambda path: True
        
        file_contents = {
            "./optimization_baseline.json": '{"latency": 0.5}',
            "./tmp/candidates_eval_results.json": '{"branch1": {"metrics": {"latency": 0.1}, "log_path": "log"}}',
            "./tmp/candidates_commentary.json": '{"branch1": "Rational comment"}',
            "./optimization_history.json": '[]'
        }
        
        def mock_open_impl(path, mode="r"):
            content = file_contents.get(path, "[]")
            m = mock_open(read_data=content).return_value
            return m
        
        mock_open_file.side_effect = mock_open_impl
        mock_load_config.return_value = {
            "metrics": [{"name": "latency", "type": "float", "higher_is_better": False, "priority": 1}]
        }
        
        args = MagicMock()
        args.workspace = "."
        args.base_branch = "main"
        args.metrics_config = "config.json"
        
        orchestrator.do_select(args)
        
        mock_sub.assert_any_call(["git", "checkout", "main"], cwd=".", check=True)
        mock_sub.assert_any_call(["git", "merge", "branch1", "--no-edit"], cwd=".", check=True)
        mock_register.assert_called_once_with(".")
        mock_update.assert_called_once()

    @patch("orchestrator.register_sidecar_dashboard")
    @patch("orchestrator.update_history_files")
    @patch("orchestrator.get_git_diff", return_value="diff")
    @patch("orchestrator.load_metrics_config")
    @patch("orchestrator.subprocess.run")
    @patch("builtins.open", new_callable=mock_open)
    @patch("os.path.exists", return_value=True)
    def test_do_select_no_improvement(self, mock_exists, mock_open_file, mock_sub, mock_load_config, mock_diff, mock_update, mock_register):
        file_contents = {
            "./optimization_baseline.json": '{"latency": 0.5}',
            "./tmp/candidates_eval_results.json": '{"branch1": {"metrics": {"latency": 0.8}, "log_path": "log"}}',
            "./tmp/candidates_commentary.json": '{"branch1": "Comment"}',
            "./optimization_history.json": '[]'
        }
        def mock_open_impl(path, mode="r"):
            return mock_open(read_data=file_contents.get(path, "[]")).return_value
        mock_open_file.side_effect = mock_open_impl
        
        mock_load_config.return_value = {
            "metrics": [{"name": "latency", "type": "float", "higher_is_better": False, "priority": 1}]
        }
        
        args = MagicMock()
        args.workspace = "."
        args.base_branch = "main"
        args.metrics_config = "config.json"
        
        orchestrator.do_select(args)
        
        for call in mock_sub.call_args_list:
            self.assertNotIn("merge", call[0][0])
        mock_register.assert_called_once()
        mock_update.assert_called_once()

    @patch("orchestrator.do_verify")
    @patch("sys.argv", ["orchestrator.py", "--workspace", ".", "--benchmark-cmd", "echo", "--metrics-config", "c.json", "verify"])
    def test_main_verify(self, mock_verify):
        orchestrator.main()
        mock_verify.assert_called_once()

    # --- Additional Coverage Tests to cover >90% ---

    @patch("os.path.exists", return_value=True)
    @patch("orchestrator.subprocess.run")
    @patch("shutil.rmtree")
    def test_setup_worktree_exists(self, mock_rmtree, mock_run, mock_exists):
        orchestrator.setup_worktree("/workspace", "my-branch", "/wt/path")
        mock_run.assert_any_call(["git", "worktree", "remove", "--force", os.path.abspath("/wt/path")], cwd=os.path.abspath("/workspace"), stderr=subprocess.DEVNULL)
        mock_rmtree.assert_called_once_with(os.path.abspath("/wt/path"), ignore_errors=True)

    @patch("os.path.exists", return_value=True)
    @patch("builtins.open", new_callable=mock_open, read_data='{"metrics": []}')
    def test_load_metrics_config_success(self, mock_file, mock_exists):
        config = orchestrator.load_metrics_config("config.json")
        self.assertEqual(config, {"metrics": []})

    def test_compare_metrics_edge_cases(self):
        config = {
            "metrics": [
                {"name": "latency", "type": "float", "higher_is_better": False, "priority": 1},
                {"name": "throughput", "type": "float", "higher_is_better": True, "priority": 2}
            ]
        }
        self.assertFalse(orchestrator.compare_metrics({"latency": None}, {"latency": None}, config))
        self.assertFalse(orchestrator.compare_metrics({"latency": None}, {"latency": 0.5}, config))
        self.assertTrue(orchestrator.compare_metrics({"latency": 0.5}, {"latency": None}, config))

    @patch("builtins.open")
    @patch("os.path.exists", return_value=True)
    @patch("builtins.print")
    def test_update_history_files_json_load_failure(self, mock_print, mock_exists, mock_file):
        def mock_open_impl(path, mode="r"):
            if "r" in mode:
                raise Exception("Read error")
            return mock_open().return_value
        mock_file.side_effect = mock_open_impl
        
        step_entry = {"step": 1, "timestamp": "now", "base_branch": "main", "baseline_metrics": {"latency": 0.5}}
        orchestrator.update_history_files(".", "history.json", "history.md", step_entry)
        mock_print.assert_any_call("Warning: Failed to load history JSON: Read error")

    @patch("builtins.open")
    @patch("os.path.exists", return_value=True)
    @patch("builtins.print")
    def test_update_history_files_md_write_failure(self, mock_print, mock_exists, mock_open_file):
        mock_open_file.side_effect = [
            mock_open(read_data="[]").return_value,
            mock_open().return_value,
            Exception("Write error")
        ]
        step_entry = {"step": 1, "timestamp": "now", "base_branch": "main", "baseline_metrics": {"latency": "not-a-float"}}
        orchestrator.update_history_files(".", "history.json", "history.md", step_entry)
        mock_print.assert_any_call("Warning: Failed to generate history Markdown: Write error")

    @patch("os.path.expanduser")
    @patch("os.makedirs", side_effect=Exception("Disk full"))
    @patch("builtins.print")
    def test_register_sidecar_dashboard_exception(self, mock_print, mock_makedirs, mock_expanduser):
        mock_expanduser.return_value = "/fake/home"
        orchestrator.register_sidecar_dashboard(".")
        mock_print.assert_any_call("Warning: Could not register Sidecar Dashboard: Disk full")

    @patch("orchestrator.register_sidecar_dashboard")
    @patch("orchestrator.update_history_files")
    @patch("orchestrator.get_git_diff", return_value="diff")
    @patch("orchestrator.load_metrics_config")
    @patch("orchestrator.subprocess.run")
    @patch("builtins.open", new_callable=mock_open)
    @patch("os.path.exists", return_value=True)
    @patch("builtins.print")
    def test_do_select_merge_failure(self, mock_print, mock_exists, mock_open_file, mock_sub, mock_load_config, mock_diff, mock_update, mock_register):
        file_contents = {
            "./optimization_baseline.json": '{"latency": 0.5}',
            "./tmp/candidates_eval_results.json": '{"branch1": {"metrics": {"latency": 0.1}, "log_path": "log"}}',
            "./tmp/candidates_commentary.json": '{"branch1": "Rational comment"}',
            "./optimization_history.json": '[]'
        }
        def mock_open_impl(path, mode="r"):
            return mock_open(read_data=file_contents.get(path, "[]")).return_value
        mock_open_file.side_effect = mock_open_impl
        mock_load_config.return_value = {"metrics": [{"name": "latency", "type": "float", "higher_is_better": False, "priority": 1}]}
        
        mock_sub.side_effect = [
            None,
            subprocess.CalledProcessError(1, "git merge"),
            None
        ]
        
        args = MagicMock()
        args.workspace = "."
        args.base_branch = "main"
        args.metrics_config = "config.json"
        
        orchestrator.do_select(args)
        
        mock_print.assert_any_call("Error merging winning branch 'branch1': Command 'git merge' returned non-zero exit status 1.")
        mock_sub.assert_any_call(["git", "merge", "--abort"], cwd=".", stderr=subprocess.DEVNULL)

    @patch("orchestrator.register_sidecar_dashboard")
    @patch("orchestrator.update_history_files")
    @patch("orchestrator.get_git_diff", return_value="diff")
    @patch("orchestrator.load_metrics_config")
    @patch("orchestrator.subprocess.run")
    @patch("builtins.open", new_callable=mock_open)
    @patch("os.path.exists", return_value=True)
    @patch("builtins.print")
    def test_do_select_commentary_load_failure(self, mock_print, mock_exists, mock_open_file, mock_sub, mock_load_config, mock_diff, mock_update, mock_register):
        file_contents = {
            "./optimization_baseline.json": '{"latency": 0.5}',
            "./tmp/candidates_eval_results.json": '{"branch1": {"metrics": {"latency": 0.1}, "log_path": "log"}}',
            "./tmp/candidates_commentary.json": '{invalid json',
            "./optimization_history.json": '[]'
        }
        def mock_open_impl(path, mode="r"):
            return mock_open(read_data=file_contents.get(path, "[]")).return_value
        mock_open_file.side_effect = mock_open_impl
        mock_load_config.return_value = {"metrics": [{"name": "latency", "type": "float", "higher_is_better": False, "priority": 1}]}
        
        args = MagicMock()
        args.workspace = "."
        args.base_branch = "main"
        args.metrics_config = "config.json"
        
        orchestrator.do_select(args)
        # Verify it printed warning for failing to load commentary
        printed = "\n".join([call[0][0] for call in mock_print.call_args_list if call[0] and isinstance(call[0][0], str)])
        self.assertIn("Warning: Failed to load commentary JSON", printed)

if __name__ == '__main__':
    unittest.main()
