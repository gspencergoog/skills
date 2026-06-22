#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import json
import re
import subprocess
import argparse
import glob
import zipfile

def run_benchmark(workspace, command, environment="local", podman_image=None):
    """Runs the benchmark command locally or inside a Podman container."""
    workspace_abs = os.path.abspath(workspace)
    if not os.path.exists(workspace_abs):
        raise FileNotFoundError(f"Workspace path does not exist: {workspace_abs}")

    if environment == "podman":
        if not podman_image:
            raise ValueError("Podman image must be specified for containerized environment.")
        # Ensure podman mounts the worktree directory into /workspace
        run_cmd = [
            "podman", "run", "--rm",
            "-v", f"{workspace_abs}:/workspace",
            "-w", "/workspace",
            podman_image,
            "/bin/sh", "-c", command
        ]
        # In podman mode, run inside the current folder but mount the workspace
        cwd = None
    else:
        # Local execution: run inside the workspace folder
        run_cmd = ["/bin/sh", "-c", command]
        cwd = workspace_abs

    print(f"Running command: {' '.join(run_cmd)}")
    proc = subprocess.run(
        run_cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    return proc.stdout, proc.stderr, proc.returncode

def extract_json_path(data, json_path):
    """Simple helper to extract a nested key from a dict using dot-notation path (e.g. 'results.throughput')."""
    parts = json_path.split('.')
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current

def parse_metrics(workspace, stdout, stderr, config_path):
    """Parses benchmark outputs and files to extract configured metrics."""
    if not config_path:
        return {}

    if isinstance(config_path, dict):
        config = config_path
    else:
        if not os.path.exists(config_path):
            print(f"Warning: Metrics config path '{config_path}' not found. No metrics will be extracted.")
            return {}
        with open(config_path, "r") as f:
            config = json.load(f)

    metrics_results = {}
    for metric in config.get("metrics", []):
        name = metric.get("name")
        m_type = metric.get("type", "float")
        source = metric.get("source", "stdout")
        val = None

        if source in ("stdout", "stderr"):
            text_to_search = stdout if source == "stdout" else stderr
            pattern = metric.get("regex")
            if pattern:
                match = re.search(pattern, text_to_search)
                if match:
                    # Take the first capture group
                    try:
                        val = match.group(1)
                    except IndexError:
                        val = match.group(0)

        elif source == "file":
            rel_file_path = metric.get("file_path")
            json_path = metric.get("json_path")
            file_abs = os.path.join(workspace, rel_file_path)
            
            if os.path.exists(file_abs):
                try:
                    with open(file_abs, "r") as f_in:
                        file_data = json.load(f_in)
                    if json_path:
                        val = extract_json_path(file_data, json_path)
                    else:
                        val = file_data
                except Exception as e:
                    print(f"Error reading metric file {rel_file_path}: {e}")

        elif source == "inspect_eval_zip":
            rel_file_path = metric.get("file_path")
            json_path = metric.get("json_path")
            
            search_path = os.path.join(workspace, rel_file_path)
            matching_files = glob.glob(search_path)
            if matching_files:
                # Sort by modification time to get the latest run
                matching_files.sort(key=os.path.getmtime, reverse=True)
                latest_eval = matching_files[0]
                
                try:
                    with zipfile.ZipFile(latest_eval, "r") as z:
                        summaries_data = json.loads(z.read("summaries.json"))
                    
                    if json_path == "mean_a2ui_score":
                        vals = []
                        for sample in summaries_data:
                            v = sample.get("scores", {}).get("a2ui_scorer", {}).get("value", 0.0)
                            if isinstance(v, str):
                                v = 1.0 if v == "C" else (0.5 if v == "P" else 0.0)
                            vals.append(v)
                        val = sum(vals) / len(vals) if vals else 0.0
                        
                    elif json_path == "mean_qa_score":
                        vals = []
                        for sample in summaries_data:
                            v = sample.get("scores", {}).get("measured_model_graded_qa", {}).get("value", 0.0)
                            if isinstance(v, str):
                                v = 1.0 if v == "C" else (0.5 if v == "P" else 0.0)
                            vals.append(v)
                        val = sum(vals) / len(vals) if vals else 0.0
                        
                    elif json_path == "total_input_tokens":
                        tot = 0
                        for sample in summaries_data:
                            for model, usage in sample.get("model_usage", {}).items():
                                tot += usage.get("input_tokens", 0)
                        val = tot
                        
                    elif json_path == "total_output_tokens":
                        tot = 0
                        for sample in summaries_data:
                            for model, usage in sample.get("model_usage", {}).items():
                                tot += usage.get("output_tokens", 0)
                        val = tot
                        
                    elif json_path == "total_tokens":
                        tot = 0
                        for sample in summaries_data:
                            for model, usage in sample.get("model_usage", {}).items():
                                tot += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                        val = tot
                except Exception as e:
                    print(f"Error parsing Inspect eval zip {latest_eval}: {e}")

        # Cast type
        if val is not None:
            try:
                if m_type == "float":
                    metrics_results[name] = float(val)
                elif m_type == "int":
                    metrics_results[name] = int(val)
                elif m_type == "str":
                    metrics_results[name] = str(val)
                elif m_type == "bool":
                    metrics_results[name] = str(val).lower() in ("true", "1", "yes")
            except ValueError:
                print(f"Warning: Failed to cast metric '{name}' value '{val}' to type '{m_type}'")
                
    return metrics_results

def main():
    parser = argparse.ArgumentParser(description="Performance Climbing Benchmark Runner")
    parser.add_argument("--workspace", required=True, help="Path to the workspace")
    parser.add_argument("--command", required=True, help="Benchmark command to run")
    parser.add_argument("--config", help="Path to metrics configuration JSON")
    parser.add_argument("--environment", default="local", choices=["local", "podman"], help="Execution environment")
    parser.add_argument("--podman-image", help="Podman container image name")

    args = parser.parse_args()

    try:
        stdout, stderr, exit_code = run_benchmark(
            workspace=args.workspace,
            command=args.command,
            environment=args.environment,
            podman_image=args.podman_image
        )
        print("--- Benchmark Completed ---")
        print(f"Exit Code: {exit_code}")
        print("--- Stdout Output ---")
        print(stdout)
        print("--- Stderr Output ---")
        print(stderr)

        if args.config:
            metrics = parse_metrics(args.workspace, stdout, stderr, args.config)
            print("--- Parsed Metrics ---")
            print(json.dumps(metrics, indent=2))
            
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
