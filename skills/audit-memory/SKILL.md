---
name: audit-memory
description: >-
  Audits agent memory files across Jetski mfs, Claude Code, Gemini CLI, and
  repo-local directories for primary-source veracity and promotes durable rules
  into AGENTS.md, GEMINI.md, CLAUDE.md, user skills, or scripts with backup
  snapshots and rollback. Use when asked to audit, verify, clean up, or
  consolidate agent memories, check memory files for stale or false claims and
  dead paths, promote #direct rules into instructions or skills, restore a
  memory backup, or when the user runs /audit-memory. Don't use for mining
  conversation transcripts for repeated tool sequences or latency (use
  conversation-auditor). Flags: --veracity-only, --promote-only, --memory-dir,
  --scope, --dry-run, --restore.
---

# Memory Veracity & Promotion Auditor (`audit-memory`)

`audit-memory` checks agent memory files against live repository evidence and promotes durable rules into permanent instructions, skills, or scripts. It uses four Python 3 standard-library scripts (`scripts/memory_common.py`, `scripts/scan_memories.py`, `scripts/lint_memory_plan.py`, and `scripts/apply_memory_plan.py`) so discovery, line-span checks, backups, and patching stay deterministic.

For JSON schemas, A2UI Express surfaces, fallback prompts, and the worker prompt template, read [schemas_and_ui.md](references/schemas_and_ui.md).

## Supported Flags

| Flag | Effect |
| :--- | :--- |
| `--veracity-only` | Check factual accuracy against primary sources without proposing rule promotions. |
| `--promote-only` | Scan for durable rules to promote without running line-by-line veracity checks. |
| `--memory-dir <path>` | Override automatic root discovery and audit an explicit memory directory. |
| `--scope <filter>` | Filter by `all`, `projects`, `topics`, `preferences`, or a file glob such as `projects/a2ui*.md`. |
| `--dry-run` | Run discovery, linting, and unified diff preview in `scripts/apply_memory_plan.py` without modifying files. |
| `--restore <backup-id>` | Restore memory and promotion target files from `~/.cache/audit-memory/backups/<backup-id>/`. |

## Six-Step Workflow

### Step 1: Pre-Scan Memory Roots & Promotion Targets

Run `scripts/scan_memories.py` to index the active memory directory, build the relative link graph, locate `#direct` lines, and partition files into balanced shards:

```bash
python3 <skill-dir>/scripts/scan_memories.py \
  [--memory-dir <memory-root>] \
  [--workspace <repo-root>] \
  [--scope all|projects|topics|preferences|<glob>] \
  --output <scratch-dir>/memory_inventory.json
```

`scripts/scan_memories.py` probes `--memory-dir`, `~/memory/default`, `~/.gemini/memory`, `<repo-root>/.agents/memory`, `<repo-root>/.claude/memory`, and `~/.claude/projects/*/memory` in order. It inspects `mfs status` when available and adds `~/memory/skills` (plus any read-only FUSE mount with over 10,000 entries) to `skipped_mounts`. It never traverses `~/memory/skills`.

The script also discovers writable promotion targets (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, and user skill directories outside read-only FUSE symlinks). It groups memory files into balanced shards (`ceil(files / 3)`, up to 10 workers) based on list-bullet claim counts.

### Step 2: Pre-Audit Scope, Fan-Out & Model Gate

Inspect `summary.recommended_workers` in `<scratch-dir>/memory_inventory.json`.

- If `recommended_workers <= 2` and the user already specified the audit scope and lens, proceed directly to Step 3 on the default model (`auto` / `inherit`).
- If `recommended_workers > 2` or the scope is open, present the pre-audit gate before spawning workers.

Use the three-tier interactive hierarchy from [schemas_and_ui.md](references/schemas_and_ui.md):

1. Tier 1 (`a2ui-pane` worksheet): call `render_a2ui` with the Step 2 `ChoiceCards` surface in `mode: "inline"` (or `mode: "pane"` when pairing with a shard preview table).
2. Tier 2 (host question tool): pass the three questions in one batch to `ask_question` or `AskUserQuestion`.
3. Tier 3 (numbered chat): print the three numbered questions in chat and end the turn to wait for the user's reply.

The gate collects three choices:

- Audit lens: `Veracity + Promotion (Recommended)`, `Veracity Only`, or `Promotion Only`.
- Worker concurrency: `Wide fan-out: <N> concurrent workers (Recommended)`, `Fork 2 concurrent workers (batched)`, or `In-session only (1 agent)`.
- Worker model picker: populated from `swarm{action: "models"}` and annotated with quota-pool notes from `$MY_CHEATS_DIR/models.md` when present. Preselect `auto` (`inherit`) so external model quota is used only when explicitly chosen.

### Step 3: Parallel Veracity & Promotion Audit

Dispatch read-only shard auditors using the user's chosen concurrency and model:

- On hosts with `swarm`, spawn one read-only worker per shard (`aud-1`..`aud-<k>`) with `workspace: "inherit"` and the selected `model`.
- On hosts without `swarm`, use read-only subagents (respecting the chosen worker cap) or audit shards sequentially in-session.
- Give each auditor the shard prompt template from [schemas_and_ui.md](references/schemas_and_ui.md). Auditors must not edit memory or repository files directly.

Each auditor evaluates every claim in its assigned files and writes `<scratch-dir>/findings-<shard>.jsonl`:

| Lens | Verdict | Meaning | Action on Memory Span |
| :--- | :--- | :--- | :--- |
| Promotion (Tier 0) | `promote:script` | Mechanical rule enforceable by a script, linter, or unit test. | `delete` or `update` after Tier 0 pre-apply. |
| Promotion (Tier 1) | `promote:agents_md` | Repo-independent rule, workflow constraint, or user preference. | `delete` or `update`; append to `AGENTS.md` / `GEMINI.md` / `CLAUDE.md`. |
| Promotion (Tier 2) | `promote:skill` | Domain procedure, CLI recipe, or review check for an existing user skill. | `delete` or `update`; append to target `SKILL.md` section. |
| Veracity | `confirmed` | Matches primary sources (`HEAD` code, git history, live CLI flags). | Omit from `findings-<shard>.jsonl`. |
| Veracity | `stale` / `false` | Outdated after recent commits or contradicted by primary sources. | `update` with exact replacement Markdown lines. |
| Veracity | `dead-pointer` | Points to a deleted file, moved script, dead symlink, or missing anchor. | `update` path or `delete` obsolete bullet. |
| Veracity | `superseded` | Build-diary entry or duplicate note superseded by a later doc or skill. | `delete` span or `retire` whole file. |

When retiring an entire file (`action: "retire"`, `start_line: 1`, `end_line: <line_count>`), the auditor emits separate per-line `action: "delete"` records for any `#direct` or promotable claims inside that file. Those per-line records route each claim to its own `promote_target` or `direct_migration_target` before the file is unlinked.

### Step 4: Deterministic Plan Linting

Check all shard findings against `<scratch-dir>/memory_inventory.json`:

```bash
python3 <skill-dir>/scripts/lint_memory_plan.py \
  --inventory <scratch-dir>/memory_inventory.json \
  --findings <scratch-dir>/findings-*.jsonl \
  --output-plan <scratch-dir>/verified_plan.json
```

`scripts/lint_memory_plan.py` enforces seven validation checks:

- `root-containment`: `file`, `direct_migration_target`, and `link_rewrite_target` must resolve inside `<memory-root>` and outside `skipped_mounts`. Neither migration target nor link-rewrite target may be marked `action: "retire"` in the same plan.
- `line-range-valid`: `1 <= start_line <= end_line <= line_count`, with no overlapping line spans in the same file (including synthetic spans from incoming link rewrites). One file-level `action: "retire"` record may coexist with non-overlapping per-line `action: "delete"` routing records in that file.
- `excerpt-match`: `expected_excerpt` must appear as a case-sensitive substring of lines `start_line..end_line`.
- `no-meta-instruction`: rejects `replacement_lines` and `promote_lines` that contain imperative meta-instructions (`"Update line "`, `"Remove the claim "`, `"Change X to Y"`, `"Delete "`) or drop list indentation when replacing a bullet.
- `direct-and-pinned-conservation`: blocks `action: "retire"` on `preferences.md`, `communication_guidelines.md`, `user.md`, and files with `visibility: PINNED`. Any touched or retired line containing `#direct` must preserve `#direct` in `replacement_lines`, set `allow_drop_direct: true`, promote to `promote_target`, or migrate to `direct_migration_target`.
- `promotion-target-allowlist`: checks that `promote_target` resolves inside `promotion_targets` (or `<repo-root>` for `promote:script`). It also checks that `promote_section` exists in instruction or skill targets and deduplicates identical promotions across shards.
- `frontmatter-safe`: checks that any edit touching YAML frontmatter preserves all existing keys (`visibility`, `confidence`, `sourced_date`) and formats `description:` as a single-line double-quoted string.

If `scripts/lint_memory_plan.py` exits `1`, send the lint errors back to the responsible shard auditor for repair. Cap lint repair retries at 2 passes per shard. If errors remain after 2 repair passes, re-run `scripts/lint_memory_plan.py` with `--allow-partial` to drop the invalid records as warnings and emit `<scratch-dir>/verified_plan.json`.

### Step 5: Audit Report & Interactive Triage Matrix

Write the human-readable audit report to `<conversation-artifact-dir>/memory_audit_report.md`. Summarize corpus metrics, veracity findings, proposed promotions, `#direct` migrations, incoming link rewrites, and any dropped lint warnings.

Present the checked plan for approval using the Step 5 templates in [schemas_and_ui.md](references/schemas_and_ui.md):

1. Tier 1 (`a2ui-pane`): render `template: "triage_matrix"` in `mode: "pane"` with per-finding buckets (`Apply (Recommended)`, `Keep in memory (skip promotion)`, `Skip`).
2. Tier 2 (`ask_question`) or Tier 3 (numbered chat): present grouped approval options when `a2ui-pane` is unavailable.

Wait for the user's triage selection before modifying any file.

### Step 6: Tier 0 Pre-Apply & Transactional Patching

Execute approved changes in two phases:

1. Tier 0 (`promote:script`) pre-apply: edit the target script, linter, or unit test in `<repo-root>` for any approved `promote:script` findings. Run its test suite first, and include a `promote:script` finding ID in `--approve-ids` only after tests pass.
2. Transactional memory and promotion patching: run `scripts/apply_memory_plan.py` with the approved finding IDs (or `--approve-all` when all items are approved).

```bash
python3 <skill-dir>/scripts/apply_memory_plan.py \
  --plan <scratch-dir>/verified_plan.json \
  --approve-ids <id1,id2,...> \
  [--backup-root ~/.cache/audit-memory/backups] \
  [--max-backups 20] \
  [--dry-run]
```

`scripts/apply_memory_plan.py` executes five safety layers:

- Snapshot before write: copies every touched memory file and external `promote_target` (`AGENTS.md`, `GEMINI.md`, `CLAUDE.md`, `SKILL.md`) to `~/.cache/audit-memory/backups/<YYYYMMDD-HHMMSS>/` with `manifest.json`, then prunes backups beyond `--max-backups 20`.
- Concurrent-edit rebase or two-legged skip: compares each file's current SHA-256 against `verified_plan.json`. If another session edited the file, it diffs against `original_lines` via `difflib.SequenceMatcher`. Non-overlapping edits rebase automatically; overlapping edits skip both the memory edit and any paired `promote_target` append.
- Bottom-up patching: sorts line edits in descending `start_line` order and migrates `#direct` lines to `direct_migration_target`. It then rewrites incoming relative links, appends promoted rules under `promote_section`, and unlinks retired files.
- Post-write frontmatter verification: parses the first 30 lines of each modified memory file to check that `description:` and all prior YAML keys survived.
- Two-legged rollback and restore: restores both memory files and promotion targets from `manifest.json` on `OSError(errno.EROFS)`, I/O failure, or stripped frontmatter. Users can list or restore prior snapshots at any time with `--list-backups` or `--restore <YYYYMMDD-HHMMSS>`.
