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
import shutil
import subprocess
import argparse
import time
from datetime import datetime

# Import runner functions directly from container_runner.py
import container_runner

def get_git_diff(workspace, branch, parent_branch="main"):
    """Gets the diff of the branch relative to its parent branch."""
    try:
        proc = subprocess.run(
            ["git", "diff", f"{parent_branch}...{branch}"],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        return proc.stdout
    except Exception as e:
        print(f"Warning: Could not get git diff for {branch}: {e}")
        return ""

def setup_worktree(workspace, branch, worktree_path):
    """Creates a git worktree for a specific branch."""
    workspace_abs = os.path.abspath(workspace)
    wt_abs = os.path.abspath(worktree_path)
    
    # Prune stale worktrees first
    subprocess.run(["git", "worktree", "prune"], cwd=workspace_abs, stdout=subprocess.DEVNULL)
    
    # Remove existing folder if any
    if os.path.exists(wt_abs):
        subprocess.run(["git", "worktree", "remove", "--force", wt_abs], cwd=workspace_abs, stderr=subprocess.DEVNULL)
        shutil.rmtree(wt_abs, ignore_errors=True)
        
    print(f"Creating git worktree for branch '{branch}' at '{wt_abs}'...")
    subprocess.run(["git", "worktree", "add", "-f", wt_abs, branch], cwd=workspace_abs, check=True)

def cleanup_worktree(workspace, worktree_path):
    """Removes a git worktree."""
    workspace_abs = os.path.abspath(workspace)
    wt_abs = os.path.abspath(worktree_path)
    if os.path.exists(wt_abs):
        print(f"Cleaning up git worktree at '{wt_abs}'...")
        subprocess.run(["git", "worktree", "remove", "--force", wt_abs], cwd=workspace_abs, stderr=subprocess.DEVNULL)
        shutil.rmtree(wt_abs, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=workspace_abs, stdout=subprocess.DEVNULL)

def load_metrics_config(config_path):
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Metrics configuration file not found: {config_path}")
    with open(config_path, "r") as f:
        return json.load(f)

def compare_metrics(candidate, baseline, config):
    """Compares candidate metrics against baseline based on config priorities.
    Returns True if candidate is strictly better, False otherwise.
    """
    metrics = config.get("metrics", [])
    # Sort by priority
    sorted_metrics = sorted(metrics, key=lambda x: x.get("priority", 99))
    
    for m in sorted_metrics:
        name = m.get("name")
        higher_better = m.get("higher_is_better", True)
        
        c_val = candidate.get(name)
        b_val = baseline.get(name)
        
        if c_val is None and b_val is None:
            continue
        if c_val is None:
            return False # Candidate missing metric is worse
        if b_val is None:
            return True # Candidate has metric that baseline is missing
            
        if c_val == b_val:
            continue
            
        if higher_better:
            return c_val > b_val
        else:
            return c_val < b_val
            
    return False

def update_history_files(workspace, history_json_path, history_md_path, step_entry):
    """Appends to the history JSON and regenerates the history Markdown summary."""
    # 1. Update JSON
    history = []
    if os.path.exists(history_json_path):
        try:
            with open(history_json_path, "r") as f:
                history = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load history JSON: {e}")
            
    history.append(step_entry)
    with open(history_json_path, "w") as f:
        json.dump(history, f, indent=2)
        
    # 2. Update Markdown
    try:
        with open(history_md_path, "w") as f:
            f.write("# Performance Optimizer History\n\n")
            f.write("This file summarizes the optimization trials and progress of the performance optimization process.\n\n")
            
            # Write summary table
            f.write("## Trial Progression Summary\n\n")
            f.write("| Step | Timestamp | Target Branch | Status | Winner | Metrics |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- | :--- |\n")
            
            for entry in history:
                step = entry.get("step")
                ts = entry.get("timestamp")
                base_b = entry.get("base_branch")
                status = entry.get("status")
                winner = entry.get("winner", "None")
                
                # Format metrics
                metrics_summary = []
                metrics_dict = entry.get("baseline_metrics", {})
                for k, v in metrics_dict.items():
                    if isinstance(v, float):
                        metrics_summary.append(f"{k}: {v:.3f}")
                    else:
                        metrics_summary.append(f"{k}: {v}")
                m_str = ", ".join(metrics_summary)
                
                f.write(f"| {step} | {ts} | {base_b} | {status} | {winner} | {m_str} |\n")
                
            f.write("\n---\n\n")
            
            # Write detailed details for each step
            f.write("## Step Details\n\n")
            for entry in reversed(history):
                f.write(f"### Step {entry.get('step')}: {entry.get('timestamp')}\n\n")
                f.write(f"* **Base Branch**: `{entry.get('base_branch')}`\n")
                f.write(f"* **Status**: {entry.get('status')}\n")
                f.write(f"* **Winner**: `{entry.get('winner', 'None')}`\n\n")
                
                f.write("#### Baseline Metrics\n")
                f.write("```json\n" + json.dumps(entry.get("baseline_metrics"), indent=2) + "\n```\n\n")
                
                f.write("#### Mutation Candidates Evaluated\n\n")
                for cand in entry.get("candidates", []):
                    f.write(f"##### Candidate: `{cand.get('name')}`\n")
                    f.write(f"* **Branch**: `{cand.get('branch')}`\n")
                    f.write(f"* **Outcome**: {'WON (Merged)' if cand.get('name') == entry.get('winner') else 'LOST'}\n")
                    f.write(f"* **Commentary**: {cand.get('commentary', 'No description provided')}\n")
                    f.write("* **Metrics**:\n")
                    f.write("```json\n" + json.dumps(cand.get("metrics"), indent=2) + "\n```\n")
                    
                    diff = cand.get("diff", "")
                    if diff:
                        f.write("* **Code Changes (Diff)**:\n")
                        f.write("```diff\n" + diff + "\n```\n")
                    f.write("\n")
                f.write("---\n\n")
    except Exception as e:
        print(f"Warning: Failed to generate history Markdown: {e}")

def register_sidecar_dashboard(workspace):
    """Link sidecar files to Jetski App Data Sidecars directory to launch the dashboard."""
    try:
        app_data_dir = os.path.expanduser("~/.gemini/jetski")
        sidecars_root = os.path.join(app_data_dir, "sidecars")
        os.makedirs(sidecars_root, exist_ok=True)
        
        target_sidecar_dir = os.path.join(sidecars_root, "performance_optimizer_dashboard")
        os.makedirs(target_sidecar_dir, exist_ok=True)
        
        # Skill sidecar source folder
        skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        source_sidecar_dir = os.path.join(skill_dir, "sidecar")
        
        if os.path.exists(source_sidecar_dir):
            for filename in os.listdir(source_sidecar_dir):
                src = os.path.join(source_sidecar_dir, filename)
                dest = os.path.join(target_sidecar_dir, filename)
                if os.path.isdir(src):
                    shutil.copytree(src, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dest)
                    
            # Write workspace target path config file so the sidecar server knows where to read history
            with open(os.path.join(target_sidecar_dir, "workspace_path.txt"), "w") as f:
                f.write(os.path.abspath(workspace))
            print("Successfully registered Performance Optimizer Dashboard Sidecar.")
    except Exception as e:
        print(f"Warning: Could not register Sidecar Dashboard: {e}")

def do_verify(args):
    """Sanity checks the benchmark command on the current workspace."""
    print("Executing Setup Verification...")
    stdout, stderr, exit_code = container_runner.run_benchmark(
        workspace=args.workspace,
        command=args.benchmark_cmd,
        environment=args.environment,
        podman_image=args.podman_image
    )
    
    if exit_code != 0:
        print(f"Error: Benchmark command failed with exit code {exit_code}")
        print(stderr)
        sys.exit(1)
        
    metrics = container_runner.parse_metrics(args.workspace, stdout, stderr, args.metrics_config)
    print("\n--- Verification Results ---")
    print(f"Benchmark run completed successfully.")
    print("Baseline Metrics extracted:")
    print(json.dumps(metrics, indent=2))
    
    # Save baseline to file
    baseline_path = os.path.join(args.workspace, "optimization_baseline.json")
    with open(baseline_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"Saved baseline metrics to: {baseline_path}")

def do_evaluate(args):
    """Evaluates multiple branches in parallel and saves candidate metrics."""
    if not args.branches:
        print("Error: Comma-separated list of --branches is required.")
        sys.exit(1)
        
    branches = [b.strip() for b in args.branches.split(",")]
    print(f"Launching evaluations for candidates: {branches}")
    
    worktrees_root = os.path.join(args.workspace, "tmp/worktrees")
    os.makedirs(worktrees_root, exist_ok=True)
    
    results = {}
    active_runs = []
    
    for branch in branches:
        wt_path = os.path.join(worktrees_root, branch)
        try:
            setup_worktree(args.workspace, branch, wt_path)
        except Exception as e:
            print(f"Error setting up worktree for {branch}: {e}")
            continue
            
        # Run benchmark asynchronously
        if args.environment == "podman":
            if not args.podman_image:
                print("Error: --podman-image must be specified for Podman execution environment.")
                sys.exit(1)
            # Podman cmd uses absolute worktree path
            wt_path_abs = os.path.abspath(wt_path)
            cmd = [
                "podman", "run", "--rm",
                "-v", f"{wt_path_abs}:/workspace",
                "-w", "/workspace",
                args.podman_image,
                "/bin/sh", "-c", args.benchmark_cmd
            ]
            cwd = None
        else:
            cmd = ["/bin/sh", "-c", args.benchmark_cmd]
            cwd = os.path.abspath(wt_path)
            
        log_file_path = os.path.join(args.workspace, f"tmp/{branch}_bench.log")
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        log_file = open(log_file_path, "w")
        
        print(f"Executing benchmark for '{branch}' in background...")
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=log_file,
            stderr=log_file
        )
        
        active_runs.append({
            "branch": branch,
            "proc": proc,
            "log_file": log_file,
            "log_file_path": log_file_path,
            "wt_path": wt_path
        })
        
    print("\nAll benchmark processes launched. Waiting for completion...")
    for run in active_runs:
        run["proc"].wait()
        run["log_file"].close()
        print(f"Branch '{run['branch']}' benchmark completed.")
        
        # Read logs to extract metrics
        with open(run["log_file_path"], "r") as f:
            stdout = f.read()
        
        # Extract metrics using the worktree path (where files might have been generated)
        metrics = container_runner.parse_metrics(run["wt_path"], stdout, "", args.metrics_config)
        results[run["branch"]] = {
            "metrics": metrics,
            "log_path": run["log_file_path"]
        }
        print(f"Branch '{run['branch']}' metrics: {metrics}")
        
    # Clean up worktrees
    print("\nCleaning up git worktrees...")
    for run in active_runs:
        cleanup_worktree(args.workspace, run["wt_path"])
        
    # Save candidate results
    candidates_eval_path = os.path.join(args.workspace, "tmp/candidates_eval_results.json")
    with open(candidates_eval_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Saved evaluation results of candidates to: {candidates_eval_path}")

def do_select(args):
    """Compares candidates, selects the best, merges it, and logs history."""
    baseline_path = os.path.join(args.workspace, "optimization_baseline.json")
    if not os.path.exists(baseline_path):
        print("Error: Baseline metrics file optimization_baseline.json not found. Run 'verify' first.")
        sys.exit(1)
        
    with open(baseline_path, "r") as f:
        baseline_metrics = json.load(f)
        
    candidates_eval_path = os.path.join(args.workspace, "tmp/candidates_eval_results.json")
    if not os.path.exists(candidates_eval_path):
        print("Error: Candidates evaluation results not found. Run 'evaluate' first.")
        sys.exit(1)
        
    with open(candidates_eval_path, "r") as f:
        eval_results = json.load(f)
        
    # Also load sub-agent commentary/metadata if available from a previous step
    # We expect a JSON file with commentary details for each branch
    commentary_path = os.path.join(args.workspace, "tmp/candidates_commentary.json")
    commentary = {}
    if os.path.exists(commentary_path):
        try:
            with open(commentary_path, "r") as f:
                commentary = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load commentary JSON: {e}")
            
    config = load_metrics_config(args.metrics_config)
    
    # Load history to get step index
    history_json_path = os.path.join(args.workspace, "optimization_history.json")
    history_md_path = os.path.join(args.workspace, "optimization_history.md")
    
    step_index = 1
    if os.path.exists(history_json_path):
        try:
            with open(history_json_path, "r") as f:
                prev_hist = json.load(f)
                step_index = len(prev_hist) + 1
        except Exception:
            pass
            
    # Compare each candidate
    best_branch = None
    best_metrics = baseline_metrics
    
    candidates_log = []
    
    for branch, res in eval_results.items():
        metrics = res["metrics"]
        diff_str = get_git_diff(args.workspace, branch, args.base_branch)
        cand_comment = commentary.get(branch, "Proposes optimizations for target files.")
        
        candidates_log.append({
            "name": branch,
            "branch": branch,
            "metrics": metrics,
            "diff": diff_str,
            "commentary": cand_comment
        })
        
        # Check if candidate is better than current best
        if compare_metrics(metrics, best_metrics, config):
            best_branch = branch
            best_metrics = metrics
            
    step_entry = {
        "step": step_index,
        "timestamp": datetime.now().isoformat(),
        "base_branch": args.base_branch,
        "baseline_metrics": baseline_metrics,
        "candidates": candidates_log,
        "winner": best_branch,
        "status": "IMPROVED" if best_branch else "NO_IMPROVEMENT"
    }
    
    if best_branch:
        print(f"\nWINNING CANDIDATE SELECTED: {best_branch}")
        print("Baseline metrics improved to:")
        print(json.dumps(best_metrics, indent=2))
        
        # Merge best branch
        print(f"Merging winning branch '{best_branch}' into '{args.base_branch}'...")
        subprocess.run(["git", "checkout", args.base_branch], cwd=args.workspace, check=True)
        subprocess.run(["git", "merge", best_branch, "--no-edit"], cwd=args.workspace, check=True)
        
        # Update baseline metrics
        with open(baseline_path, "w") as f:
            json.dump(best_metrics, f, indent=2)
            
        step_entry["baseline_metrics"] = best_metrics
    else:
        print("\nNo candidate branch outperformed the baseline.")
        print(f"Baseline remains at: {json.dumps(baseline_metrics, indent=2)}")
        
    # Update JSON and Markdown logs
    update_history_files(args.workspace, history_json_path, history_md_path, step_entry)
    
    # Clean up candidate branches
    for cand in candidates_log:
        branch_name = cand["branch"]
        if branch_name != best_branch:
            print(f"Deleting candidate branch '{branch_name}'...")
            subprocess.run(["git", "branch", "-D", branch_name], cwd=args.workspace, stderr=subprocess.DEVNULL)
            
    # Keep the winner branch or clean it up since it's merged
    if best_branch:
        subprocess.run(["git", "branch", "-D", best_branch], cwd=args.workspace, stderr=subprocess.DEVNULL)
        
    # Register / refresh dashboard sidecar config
    register_sidecar_dashboard(args.workspace)
    print("Step selection completed!")

def main():
    parser = argparse.ArgumentParser(description="Performance Optimizer Orchestrator")
    parser.add_argument("--workspace", required=True, help="Path to the workspace")
    parser.add_argument("--benchmark-cmd", required=True, help="Benchmark command to run")
    parser.add_argument("--metrics-config", required=True, help="Metrics configuration spec JSON file")
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Verify subcommand
    verify_parser = subparsers.add_parser("verify", help="Sanity check benchmark on baseline")
    verify_parser.add_argument("--environment", default="local", choices=["local", "podman"])
    verify_parser.add_argument("--podman-image", help="Podman container image")
    
    # Evaluate subcommand
    eval_parser = subparsers.add_parser("evaluate", help="Benchmark candidate branches in parallel")
    eval_parser.add_argument("--branches", required=True, help="Comma-separated list of candidate branches")
    eval_parser.add_argument("--environment", default="local", choices=["local", "podman"])
    eval_parser.add_argument("--podman-image", help="Podman container image")
    
    # Select subcommand
    select_parser = subparsers.add_parser("select", help="Merge winning candidate and write history logs")
    select_parser.add_argument("--base-branch", default="main", help="The baseline target branch (e.g. main)")
    
    args = parser.parse_args()
    
    if args.command == "verify":
        do_verify(args)
    elif args.command == "evaluate":
        do_evaluate(args)
    elif args.command == "select":
        do_select(args)

if __name__ == "__main__":
    main()
