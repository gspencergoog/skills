import unittest
from unittest.mock import patch, MagicMock, mock_open
import sys
import os
import json

import container_runner

class TestContainerRunner(unittest.TestCase):
    @patch("subprocess.run")
    def test_run_benchmark_local(self, mock_run):
        mock_res = MagicMock()
        mock_res.stdout = "Bench OK\n"
        mock_res.stderr = ""
        mock_res.returncode = 0
        mock_run.return_value = mock_res

        stdout, stderr, exit_code = container_runner.run_benchmark(".", "echo 'hello'", "local")
        self.assertEqual(stdout, "Bench OK\n")
        self.assertEqual(stderr, "")
        self.assertEqual(exit_code, 0)
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_run_benchmark_path_not_exists(self, mock_run):
        with self.assertRaises(FileNotFoundError):
            container_runner.run_benchmark("/nonexistent_path_abc_123", "echo")

    @patch("subprocess.run")
    def test_run_benchmark_podman_no_image(self, mock_run):
        with self.assertRaises(ValueError):
            container_runner.run_benchmark(".", "echo", "podman")

    @patch("subprocess.run")
    def test_run_benchmark_podman(self, mock_run):
        mock_res = MagicMock()
        mock_res.stdout = "Podman OK\n"
        mock_res.stderr = ""
        mock_res.returncode = 0
        mock_run.return_value = mock_res

        stdout, stderr, exit_code = container_runner.run_benchmark(
            ".", "echo 'hello'", "podman", podman_image="python:slim"
        )
        self.assertEqual(stdout, "Podman OK\n")
        mock_run.assert_called_once()

    def test_get_dot_path_value_non_dict(self):
        val = container_runner.extract_json_path("not-a-dict", "key")
        self.assertIsNone(val)

    def test_parse_metrics_stdout_regex(self):
        config = {
            "metrics": [
                {
                    "name": "latency",
                    "type": "float",
                    "source": "stdout",
                    "regex": "Latency:\\s*([0-9.]+)",
                    "higher_is_better": False,
                    "priority": 1
                }
            ]
        }
        stdout = "Some logs\nLatency: 0.12345s\nDone"
        metrics = container_runner.parse_metrics(".", stdout, "", config)
        self.assertEqual(metrics.get("latency"), 0.12345)

    def test_parse_metrics_regex_no_capture_group(self):
        # Pattern matches "Latency", but since there are no parentheses, group(1) fails and falls back to group(0)
        config = {
            "metrics": [
                {
                    "name": "latency",
                    "type": "str",
                    "source": "stdout",
                    "regex": "Latency",
                    "higher_is_better": False,
                    "priority": 1
                }
            ]
        }
        metrics = container_runner.parse_metrics(".", "Latency", "", config)
        self.assertEqual(metrics.get("latency"), "Latency")

    def test_parse_metrics_type_casting(self):
        config = {
            "metrics": [
                {"name": "v_int", "type": "int", "source": "stdout", "regex": "Int:\\s*([0-9]+)"},
                {"name": "v_str", "type": "str", "source": "stdout", "regex": "Str:\\s*(\\w+)"},
                {"name": "v_bool", "type": "bool", "source": "stdout", "regex": "Bool:\\s*(\\w+)"},
                {"name": "v_fail", "type": "int", "source": "stdout", "regex": "Fail:\\s*(\\w+)"}
            ]
        }
        stdout = "Int: 42\nStr: hello\nBool: yes\nFail: notanint"
        metrics = container_runner.parse_metrics(".", stdout, "", config)
        self.assertEqual(metrics.get("v_int"), 42)
        self.assertEqual(metrics.get("v_str"), "hello")
        self.assertEqual(metrics.get("v_bool"), True)
        self.assertNotIn("v_fail", metrics)

    @patch("builtins.open", new_callable=mock_open, read_data='{"latency": 0.5}')
    @patch("os.path.exists", return_value=True)
    def test_parse_metrics_json(self, mock_exists, mock_file):
        config = {
            "metrics": [
                {
                    "name": "latency",
                    "type": "float",
                    "source": "file",
                    "file_path": "metrics.json",
                    "json_path": "latency",
                    "higher_is_better": False,
                    "priority": 1
                }
            ]
        }
        metrics = container_runner.parse_metrics(".", "", "", config)
        self.assertEqual(metrics.get("latency"), 0.5)

    @patch("builtins.open", side_effect=OSError("Read error"))
    @patch("os.path.exists", return_value=True)
    def test_parse_metrics_file_read_error(self, mock_exists, mock_file):
        config = {
            "metrics": [
                {"name": "latency", "type": "float", "source": "file", "file_path": "metrics.json", "json_path": "latency"}
            ]
        }
        metrics = container_runner.parse_metrics(".", "", "", config)
        self.assertNotIn("latency", metrics)

    @patch("os.path.getmtime")
    @patch("glob.glob")
    @patch("zipfile.ZipFile")
    def test_parse_metrics_inspect_eval_zip(self, mock_zip, mock_glob, mock_getmtime):
        mock_glob.return_value = ["/workspace/eval.zip"]
        mock_getmtime.return_value = 12345.0
        
        mock_zip_instance = MagicMock()
        # Mock summaries.json with a grade score "C" (which translates to 1.0) and "P" (0.5) and model usage
        mock_summaries_data = """[
            {
                "scores": {
                    "a2ui_scorer": {
                        "value": "C"
                    }
                },
                "model_usage": {
                    "gemini-2.0-flash": {
                        "input_tokens": 100,
                        "output_tokens": 200
                    }
                }
            },
            {
                "scores": {
                    "a2ui_scorer": {
                        "value": "P"
                    }
                },
                "model_usage": {
                    "gemini-2.0-flash": {
                        "input_tokens": 150,
                        "output_tokens": 250
                    }
                }
            }
        ]"""
        mock_zip_instance.read.return_value = mock_summaries_data.encode("utf-8")
        mock_zip.return_value.__enter__.return_value = mock_zip_instance

        config = {
          "metrics": [
            {
              "name": "mean_score",
              "type": "float",
              "source": "inspect_eval_zip",
              "file_path": "logs/*.eval",
              "json_path": "mean_a2ui_score"
            },
            {
              "name": "total_tokens",
              "type": "int",
              "source": "inspect_eval_zip",
              "file_path": "logs/*.eval",
              "json_path": "total_tokens"
            }
          ]
        }

        metrics = container_runner.parse_metrics(".", "", "", config)
        # mean of 1.0 and 0.5 is 0.75
        self.assertEqual(metrics.get("mean_score"), 0.75)
        self.assertEqual(metrics.get("total_tokens"), 700)

    @patch("container_runner.run_benchmark")
    @patch("container_runner.parse_metrics")
    @patch("sys.argv", ["container_runner.py", "--workspace", ".", "--command", "echo 'hello'", "--config", "c.json"])
    def test_main_local(self, mock_parse, mock_run):
        mock_run.return_value = ("stdout", "stderr", 0)
        mock_parse.return_value = {"latency": 0.1}
        container_runner.main()
        mock_run.assert_called_once()

    @patch("container_runner.run_benchmark", side_effect=Exception("Execution error"))
    @patch("sys.argv", ["container_runner.py", "--workspace", ".", "--command", "echo 'hello'"])
    def test_main_error(self, mock_run):
        with self.assertRaises(SystemExit):
            container_runner.main()

    # --- Additional Coverage Tests to cover >90% ---

    def test_parse_metrics_no_config_path(self):
        self.assertEqual(container_runner.parse_metrics(".", "stdout", "", None), {})

    @patch("os.path.exists", return_value=False)
    @patch("builtins.print")
    def test_parse_metrics_config_file_not_found(self, mock_print, mock_exists):
        self.assertEqual(container_runner.parse_metrics(".", "stdout", "", "missing_config.json"), {})
        mock_print.assert_called()

    @patch("builtins.open", new_callable=mock_open, read_data='{"metrics": [{"name": "latency", "source": "stdout", "regex": "([0-9.]+)"}]}')
    @patch("os.path.exists", return_value=True)
    def test_parse_metrics_config_file_success(self, mock_exists, mock_open_file):
        metrics = container_runner.parse_metrics(".", "0.123", "", "config.json")
        self.assertEqual(metrics.get("latency"), 0.123)

    @patch("builtins.open", new_callable=mock_open, read_data='{"latency": 0.5}')
    @patch("os.path.exists", return_value=True)
    def test_parse_metrics_json_no_json_path(self, mock_exists, mock_file):
        config = {
            "metrics": [
                {
                    "name": "all_data",
                    "type": "str",
                    "source": "file",
                    "file_path": "metrics.json"
                }
            ]
        }
        metrics = container_runner.parse_metrics(".", "", "", config)
        self.assertEqual(metrics.get("all_data"), "{'latency': 0.5}")

    @patch("os.path.getmtime")
    @patch("glob.glob")
    @patch("zipfile.ZipFile")
    def test_parse_metrics_inspect_eval_zip_more_paths(self, mock_zip, mock_glob, mock_getmtime):
        mock_glob.return_value = ["/workspace/eval.zip"]
        mock_getmtime.return_value = 12345.0
        
        mock_zip_instance = MagicMock()
        mock_summaries_data = """[
            {
                "scores": {
                    "measured_model_graded_qa": {
                        "value": "C"
                    }
                },
                "model_usage": {
                    "gemini-2.0-flash": {
                        "input_tokens": 100,
                        "output_tokens": 200
                    }
                }
            },
            {
                "scores": {
                    "measured_model_graded_qa": {
                        "value": "P"
                    }
                },
                "model_usage": {
                    "gemini-2.0-flash": {
                        "input_tokens": 150,
                        "output_tokens": 250
                    }
                }
            }
        ]"""
        mock_zip_instance.read.return_value = mock_summaries_data.encode("utf-8")
        mock_zip.return_value.__enter__.return_value = mock_zip_instance

        config = {
          "metrics": [
            {
              "name": "mean_qa_score",
              "type": "float",
              "source": "inspect_eval_zip",
              "file_path": "logs/*.eval",
              "json_path": "mean_qa_score"
            },
            {
              "name": "total_input_tokens",
              "type": "int",
              "source": "inspect_eval_zip",
              "file_path": "logs/*.eval",
              "json_path": "total_input_tokens"
            },
            {
              "name": "total_output_tokens",
              "type": "int",
              "source": "inspect_eval_zip",
              "file_path": "logs/*.eval",
              "json_path": "total_output_tokens"
            }
          ]
        }

        metrics = container_runner.parse_metrics(".", "", "", config)
        self.assertEqual(metrics.get("mean_qa_score"), 0.75)
        self.assertEqual(metrics.get("total_input_tokens"), 250)
        self.assertEqual(metrics.get("total_output_tokens"), 450)

    @patch("os.path.getmtime")
    @patch("glob.glob")
    @patch("zipfile.ZipFile")
    @patch("builtins.print")
    def test_parse_metrics_inspect_eval_zip_exception(self, mock_print, mock_zip, mock_glob, mock_getmtime):
        mock_glob.return_value = ["/workspace/eval.zip"]
        mock_zip.side_effect = Exception("Zip error")
        
        config = {
          "metrics": [
            {
              "name": "mean_score",
              "type": "float",
              "source": "inspect_eval_zip",
              "file_path": "logs/*.eval",
              "json_path": "mean_a2ui_score"
            }
          ]
        }
        metrics = container_runner.parse_metrics(".", "", "", config)
        self.assertEqual(metrics, {})
        mock_print.assert_called()

if __name__ == '__main__':
    unittest.main()
