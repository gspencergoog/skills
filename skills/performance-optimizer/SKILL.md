---
name: performance-optimizer
description: Automates code performance optimization using iterative mutation, parallel evaluation in Podman containers or local git worktrees, selection of best candidates, and history tracking. Use when you need to optimize the runtime performance (latency, throughput, memory, or CPU) of a specific function, module, or algorithm.
---

# Performance Optimizer Guide

Follow this guide to run automated, iterative performance optimizations on a target codebase.

## 1. Initial Alignment & Parameter Collection

Before executing any commands or spawning sub-agents, interview the user to align on goals and collect parameters:
1. **Target Code**: Identify the specific file(s) and function(s) or module(s) to optimize.
2. **Benchmark Setup**: Confirm there is an existing benchmark suite or execution command (e.g. `uv run pytest tests/benchmark.py`, `cargo bench`, `go test -bench`).
3. **Metrics Selection**: Align on what metrics to collect (e.g. latency, throughput, memory footprint, CPU cycles, token counts). Recommend tracking secondary metrics (e.g. binary size, accuracy) to prevent optimizations from degrading functionality or increasing footprint.
4. **Execution Environment**: Determine whether to run trials locally in git worktrees, or inside an isolated container via Podman (Docker is not supported). If containerized, get the Podman container image name.
5. **Loop Limits**:
   * Number of parallel sub-agents (Recommend a default of 3).
   * Maximum iteration limit (Or run until a specific performance target is met).

## 2. Setup Verification

Before launching the optimization loop, verify the benchmark works and set the initial baseline:
1. Run the benchmark on the current codebase (baseline) using the local runner.
2. Verify the output metrics are extracted correctly.
3. Present the baseline results to the user and confirm they match expectations.

## 3. Running the Optimization Loop

Run the orchestrator script to automate the optimization cycle:

```bash
python3 /Users/gspencer/.gemini/config/skills/performance-optimizer/scripts/orchestrator.py \
  --workspace /path/to/workspace \
  --mutations-count 3 \
  --max-iterations 5 \
  --benchmark-cmd "uv run pytest tests/benchmark.py" \
  --metrics-config "metrics_config.json" \
  --environment [local|podman] \
  --podman-image my-benchmark-image:latest
```

During execution, the orchestrator will:
* Register the sidecar web dashboard at `~/.gemini/jetski/sidecars/performance_optimizer_dashboard/` so the user can monitor progress.
* Extract failure files or profiling logs to guide mutation planning.
* Design mutually exclusive mutation plans.
* Spawn sub-agents in parallel branches to implement and commit those mutations.
* Run the benchmark on each branch.
* Choose the candidate that shows the greatest improvement according to the configured metric priority.
* Document all trials (both winners and losers) with diffs, metrics, and commentary in `optimization_history.json` and `optimization_history.md`.
* Merge the winning candidate into the baseline and start the next iteration.

## 4. Reviewing Results

Once iterations complete or target performance is met:
1. Point the user to the Web UI dashboard sidecar.
2. Guide the user through reviewing the history of mutations, comparing successful vs failed strategies, and inspecting diffs.
3. Confirm if the final optimized codebase is ready to be committed to the main production branch.
