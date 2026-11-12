# JSON Schemas, UI Surfaces & Auditor Prompts (`audit-memory`)

This reference defines the JSON contracts, three-tier interactive UI payloads, and prompt template for shard auditors in `audit-memory`.

## 1. JSON Data Schemas

### 1.1 `memory_inventory.json` (`scripts/scan_memories.py`)

`scripts/scan_memories.py` outputs a JSON document with `schema_version: 1`. Each entry in `files` stores `original_lines` so downstream linting and patching can detect and rebase concurrent edits.

```json
{
  "schema_version": 1,
  "memory_root": "<memory-root>",
  "skipped_mounts": ["<memory-parent>/skills"],
  "promotion_targets": {
    "global_instructions": "<repo-root>/agents/agents.md",
    "workspace_instructions": "<repo-root>/AGENTS.md",
    "writable_skill_dirs": ["<repo-root>/agents/skills"]
  },
  "summary": {
    "total_files": 35,
    "total_lines": 1840,
    "total_claims": 612,
    "direct_claims": 118,
    "recommended_workers": 8,
    "capped_workers": 2
  },
  "shards": [
    {
      "shard_id": "aud-1",
      "files": ["projects/cheats.md", "topics/github-search.md"],
      "claim_count": 133
    }
  ],
  "files": {
    "projects/cheats.md": {
      "sha256": "8f3d9a1c...",
      "mtime_ns": 1794437520000000000,
      "line_count": 168,
      "claim_count": 42,
      "original_lines": ["---", "description: \"Architecture and symlink layout\"", "---", "..."],
      "protected_file": false,
      "frontmatter_valid": true,
      "frontmatter_end_line": 5,
      "frontmatter_keys": {
        "description": "Architecture and symlink layout",
        "confidence": "high",
        "sourced_date": "2026-10-28"
      },
      "frontmatter_description": "Architecture and symlink layout",
      "frontmatter_error": null,
      "direct_lines": [60, 73, 79],
      "outgoing_links": [
        {
          "line": 57,
          "raw_target": "monitor-skill.md",
          "target": "projects/monitor-skill.md",
          "exists": true
        }
      ],
      "incoming_links": [
        {
          "source_file": "topics/agent-models.md",
          "line": 8,
          "raw_target": "../projects/cheats.md"
        }
      ]
    }
  }
}
```

### 1.2 `findings-<shard>.jsonl` (Shard Auditor Output)

Each shard auditor writes one JSON object per line to `<scratch-dir>/findings-<shard>.jsonl`. Confirmed claims (`verdict: "confirmed"`) are omitted.

| Field | Type | Constraints |
| :--- | :--- | :--- |
| `id` | `string` | Unique identifier such as `"cheats-L028"` or `"legacy-retire"`. |
| `file` | `string` | Relative path inside `<memory-root>`, such as `"projects/cheats.md"`. |
| `start_line` | `int` | 1-based inclusive start line (`1` for whole-file `retire`). |
| `end_line` | `int` | 1-based inclusive end line (`line_count` for whole-file `retire`). |
| `expected_excerpt` | `string` | Exact case-sensitive substring present in lines `start_line..end_line`. |
| `lens` | `string` | `"veracity"` or `"promotion"`. |
| `verdict` | `string` | `"stale"`, `"false"`, `"dead-pointer"`, `"superseded"`, `"promote:script"`, `"promote:agents_md"`, or `"promote:skill"`. |
| `action` | `string` | `"update"`, `"delete"`, or `"retire"`. |
| `replacement_lines` | `list[str]` | Exact Markdown replacement lines for `"update"`; empty list `[]` for `"delete"` or `"retire"`. |
| `allow_drop_direct` | `bool` | Set `true` only when a `#direct` claim is verified false or obsolete and should be dropped without promotion or migration. |
| `promote_target` | `string \| null` | Target file path inside `promotion_targets` (or `<repo-root>` for `promote:script`), or `null`. |
| `promote_section` | `string \| null` | Exact Markdown heading in `promote_target` (such as `"## General rules"`), or `null` for `promote:script`. |
| `promote_lines` | `list[str]` | Exact Markdown lines to append under `promote_section`, or `[]`. |
| `direct_migration_target` | `string \| null` | Surviving relative memory file path to receive a `#direct` line when deleting or retiring its source span, or `null`. |
| `link_rewrite_target` | `string \| null` | Surviving relative memory file path that replaces incoming relative links when `action` is `"retire"`, or `null`. |
| `evidence` | `string` | Primary-source command, file path, commit hash, or line citation proving the verdict. |
| `confidence` | `string` | `"high"` or `"medium"`. |

#### Example A: In-Place Veracity Update & Rule Promotion

```json
{"id": "cheats-L028", "file": "projects/cheats.md", "start_line": 28, "end_line": 28, "expected_excerpt": "claude-code-reviewer and gemini-code-reviewer", "lens": "veracity", "verdict": "superseded", "action": "update", "replacement_lines": ["    - `~/.gemini/config/agents` -> `<repo-root>/agents/agents` (directory is empty; static reviewer agents were replaced by `swarm` workers)"], "allow_drop_direct": false, "promote_target": null, "promote_section": null, "promote_lines": [], "direct_migration_target": null, "link_rewrite_target": null, "evidence": "ls -A <repo-root>/agents/agents -> empty directory", "confidence": "high"}
{"id": "cheats-L060", "file": "projects/cheats.md", "start_line": 60, "end_line": 60, "expected_excerpt": "Always pass -batch -dark-mode to duckdb #direct", "lens": "promotion", "verdict": "promote:agents_md", "action": "delete", "replacement_lines": [], "allow_drop_direct": false, "promote_target": "<repo-root>/AGENTS.md", "promote_section": "## Finding information in a workspace", "promote_lines": ["- Always pass `-batch -dark-mode` when invoking `duckdb` in non-interactive shells."], "direct_migration_target": null, "link_rewrite_target": null, "evidence": "Verified in <repo-root>/agents/skills/duckdb/SKILL.md", "confidence": "high"}
```

#### Example B: Whole-File Retirement Coexisting with Per-Line `#direct` Routing

When retiring a superseded memory file that contains multiple `#direct` or promotable claims, emit one file-level `action: "retire"` record (`start_line: 1, end_line: <line_count>`) alongside non-overlapping per-line `action: "delete"` records. Each per-line record routes its claim to a distinct `promote_target` or `direct_migration_target` before the file is unlinked:

```json
{"id": "old-notes-L014", "file": "projects/old-notes.md", "start_line": 14, "end_line": 14, "expected_excerpt": "Use --no-pager on git diff #direct", "lens": "promotion", "verdict": "promote:agents_md", "action": "delete", "replacement_lines": [], "allow_drop_direct": false, "promote_target": "<repo-root>/AGENTS.md", "promote_section": "## General rules", "promote_lines": ["- Pipe git diff and git log output through `| cat` in non-interactive shells."], "direct_migration_target": null, "link_rewrite_target": null, "evidence": "Promoted #direct rule from retired file", "confidence": "high"}
{"id": "old-notes-L029", "file": "projects/old-notes.md", "start_line": 29, "end_line": 29, "expected_excerpt": "Keep scratch files out of repo root #direct", "lens": "veracity", "verdict": "superseded", "action": "delete", "replacement_lines": [], "allow_drop_direct": false, "promote_target": null, "promote_section": null, "promote_lines": [], "direct_migration_target": "projects/cheats.md", "link_rewrite_target": null, "evidence": "Migrated #direct rule to surviving project memory", "confidence": "high"}
{"id": "old-notes-retire", "file": "projects/old-notes.md", "start_line": 1, "end_line": 45, "expected_excerpt": "description:", "lens": "veracity", "verdict": "superseded", "action": "retire", "replacement_lines": [], "allow_drop_direct": false, "promote_target": null, "promote_section": null, "promote_lines": [], "direct_migration_target": null, "link_rewrite_target": "projects/cheats.md", "evidence": "All remaining claims superseded by projects/cheats.md", "confidence": "high"}
```

### 1.3 `verified_plan.json` (`scripts/lint_memory_plan.py`)

`scripts/lint_memory_plan.py` validates findings, expands `link_rewrite_target` entries into synthetic link-rewrite edits on incoming files, deduplicates promotions across shards, and embeds `original_lines` and `sha256` snapshots:

```json
{
  "schema_version": 1,
  "memory_root": "<memory-root>",
  "skipped_mounts": ["<memory-parent>/skills"],
  "files": {
    "projects/cheats.md": {
      "sha256": "8f3d9a1c...",
      "line_count": 168,
      "protected_file": false,
      "frontmatter_keys": {
        "description": "Architecture and symlink layout",
        "confidence": "high"
      },
      "original_lines": ["---", "description: \"Architecture and symlink layout\"", "---", "..."]
    }
  },
  "promotion_targets": {
    "global_instructions": "<repo-root>/agents/agents.md",
    "workspace_instructions": "<repo-root>/AGENTS.md",
    "writable_skill_dirs": ["<repo-root>/agents/skills"]
  },
  "target_files": {
    "<repo-root>/AGENTS.md": {
      "sha256": "4a1c7b2e...",
      "line_count": 48,
      "original_lines": ["# Rules", "..."]
    }
  },
  "findings": [
    {
      "id": "cheats-L028",
      "file": "projects/cheats.md",
      "start_line": 28,
      "end_line": 28,
      "expected_excerpt": "claude-code-reviewer and gemini-code-reviewer",
      "lens": "veracity",
      "verdict": "superseded",
      "action": "update",
      "replacement_lines": ["    - `~/.gemini/config/agents` -> `<repo-root>/agents/agents` (empty directory)"],
      "allow_drop_direct": false,
      "promote_target": null,
      "promote_section": null,
      "promote_lines": [],
      "direct_migration_target": null,
      "link_rewrite_target": null,
      "evidence": "ls -A <repo-root>/agents/agents -> empty directory",
      "confidence": "high"
    }
  ],
  "link_rewrites": [
    {
      "source_file": "topics/agent-models.md",
      "line": 8,
      "old_target": "../projects/old-notes.md",
      "new_target": "../projects/cheats.md",
      "retired_finding_id": "old-notes-retire"
    }
  ],
  "dropped_warnings": []
}
```

### 1.4 `backups/<YYYYMMDD-HHMMSS>/manifest.json` (`scripts/apply_memory_plan.py`)

Before modifying any file, `scripts/apply_memory_plan.py` copies every touched memory file (`kind: "memory"`) and external promotion target (`kind: "promotion_target"`) into `~/.cache/audit-memory/backups/<YYYYMMDD-HHMMSS>/`:

```json
{
  "schema_version": 1,
  "backup_id": "20261111-230500",
  "memory_root": "<memory-root>",
  "files": [
    {
      "kind": "memory",
      "path": "<memory-root>/projects/cheats.md",
      "backup_relpath": "memory/projects/cheats.md",
      "pre_apply_sha256": "8f3d9a1c...",
      "existed_before": true
    },
    {
      "kind": "promotion_target",
      "path": "<repo-root>/AGENTS.md",
      "backup_relpath": "targets/AGENTS.md",
      "pre_apply_sha256": "4a1c7b2e...",
      "existed_before": true
    }
  ]
}
```

## 2. Step 2 UI Templates: Pre-Audit Scope, Fan-Out & Model Gate

Present this gate whenever `recommended_workers > 2` or the user did not specify scope.

### 2.1 Tier 1: A2UI Express `ChoiceCards` Surface (`a2ui-pane`)

Call `a2ui_instructions` once per session if not already loaded, then call `render_a2ui`:

```json
{
  "conversationId": "<conversation-id>",
  "surfaceId": "audit_memory_scope_gate",
  "mode": "pane",
  "replace": true,
  "dsl": "root = Card(child=main_col)\nmain_col = Column(children=[title_txt, summary_txt, div1, q1_hdr, q1_cards, q2_hdr, q2_cards, q3_hdr, q3_cards, notes_field, btn_row], gap=\"small\", align=\"stretch\")\ntitle_txt = Text(text=\"### Memory Audit Scope, Concurrency & Model Gate\", variant=\"body\")\nsummary_txt = Text(text=$/summary_text, variant=\"caption\")\ndiv1 = Divider()\nq1_hdr = Text(text=\"#### 1. Audit Lens\", variant=\"body\")\nq1_cards = ChoiceCards(options=$/lens_options, value=$/lens_choice, variant=\"mutuallyExclusive\")\nq2_hdr = Text(text=\"#### 2. Worker Concurrency\", variant=\"body\")\nq2_cards = ChoiceCards(options=$/workers_options, value=$/workers_choice, variant=\"mutuallyExclusive\")\nq3_hdr = Text(text=\"#### 3. Worker Model (swarm / subagents)\", variant=\"body\")\nq3_cards = ChoiceCards(options=$/model_options, value=$/model_choice, variant=\"mutuallyExclusive\")\nnotes_field = TextField(label=\"Optional scope glob or notes\", value=$/notes)\nbtn_row = Row(children=[start_btn], justify=\"end\")\nstart_btn = Button(child=start_lbl, variant=\"primary\", action=Event(name=\"start_memory_audit\", context={lens: $/lens_choice, workers: $/workers_choice, model: $/model_choice, notes: $/notes}))\nstart_lbl = Text(text=\"Start Memory Audit\")\n$/summary_text = \"Scanned 35 memory files (612 claims, 118 #direct rules) across 8 balanced shards.\"\n$/lens_choice = [\"both\"]\n$/lens_options = [{value: \"both\", label: \"Veracity + Promotion\", description: \"Check every claim against primary repository sources and route durable rules to AGENTS.md, skills, or scripts in one pass.\", recommended: true}, {value: \"veracity\", label: \"Veracity Only\", description: \"Update stale or false claims, fix dead file pointers, and retire superseded notes without promoting rules.\"}, {value: \"promotion\", label: \"Promotion Only\", description: \"Find durable #direct rules and recurring procedures to codify in AGENTS.md, skills, or scripts.\"}]\n$/workers_choice = [\"wide\"]\n$/workers_options = [{value: \"wide\", label: \"Wide fan-out: 8 concurrent workers\", description: \"Audit all 8 shards in parallel (3 to 4 files per worker) for the fastest turnaround.\", recommended: true}, {value: \"capped_2\", label: \"Fork 2 concurrent workers (batched)\", description: \"Process shards in batches of 2 workers to conserve concurrent quota.\"}, {value: \"in_session\", label: \"In-session only (1 agent)\", description: \"Audit shards sequentially in the current conversation without spawning workers.\"}]\n$/model_choice = [\"auto\"]\n$/model_options = [{value: \"auto\", label: \"auto / inherit\", description: \"Run workers on the coordinator's default model pool without consuming third-party model quota.\", recommended: true}, {value: \"gemini-launch-candidate-high\", label: \"Gemini 4 Argon (gemini-launch-candidate-high)\", description: \"Top-tier internal coding and verification pool.\"}, {value: \"claude-opus-4-6-thinking\", label: \"Claude 4.6 Opus (claude-opus-4-6-thinking)\", description: \"Separate third-party quota pool; select only when explicitly desired.\"}]\n$/notes = \"\""
}
```

### 2.2 Tier 2: Host Structured Question Tool (`ask_question`)

When `a2ui-pane` is unavailable, pass all three questions in a single `ask_question` call:

```json
{
  "questions": [
    {
      "question": "Which audit lens should run across the discovered memory files?",
      "options": [
        "(Recommended) Veracity + Promotion: check factual accuracy against primary sources and promote durable rules",
        "Veracity Only: update stale or false claims and dead pointers without promoting rules",
        "Promotion Only: extract durable rules into AGENTS.md, skills, or scripts"
      ]
    },
    {
      "question": "How many concurrent auditor workers should run?",
      "options": [
        "(Recommended) Wide fan-out: <N> concurrent workers (1 shard per worker)",
        "Fork 2 concurrent workers (batched shards)",
        "In-session only (1 agent, sequential)"
      ]
    },
    {
      "question": "Which model should auditor workers use?",
      "options": [
        "(Recommended) auto / inherit (use current session model pool)",
        "gemini-launch-candidate-high (Gemini 4 Argon pool)",
        "claude-opus-4-6-thinking (Claude 4.6 Opus third-party pool)"
      ]
    }
  ]
}
```

### 2.3 Tier 3: Numbered Chat Fallback

When no structured question tool is available, print the summary and three numbered choices in chat, then end the turn:

```markdown
Scanned `<total_files>` memory files (`<total_claims>` claims, `<direct_claims>` `#direct` rules) across `<N>` shards. Please reply with your choices (or press Enter for the recommended defaults):

1. Audit Lens: (a) `(Recommended) Veracity + Promotion`, (b) `Veracity Only`, (c) `Promotion Only`
2. Worker Concurrency: (a) `(Recommended) Wide fan-out (<N> workers)`, (b) `2 concurrent workers (batched)`, (c) `In-session only (1 agent)`
3. Worker Model: (a) `(Recommended) auto / inherit`, (b) `<model-2>`, (c) `<model-3>`
```

## 3. Step 5 UI Templates: Post-Lint Interactive Triage Matrix

After `scripts/lint_memory_plan.py` writes `<scratch-dir>/verified_plan.json`, write `<conversation-artifact-dir>/memory_audit_report.md` and present the triage surface before running `scripts/apply_memory_plan.py`.

### 3.1 Tier 1: A2UI `triage_matrix` Surface (`a2ui-pane`)

Render `template: "triage_matrix"` in `mode: "pane"` so the user can approve, keep in memory, or skip each finding:

```json
{
  "conversationId": "<conversation-id>",
  "surfaceId": "audit_memory_triage",
  "mode": "pane",
  "replace": true,
  "template": "triage_matrix",
  "dsl": "bucket_picker = ChoicePicker(options=[{label: \"Apply (Recommended)\", value: \"apply\"}, {label: \"Keep in memory\", value: \"keep_in_memory\"}, {label: \"Skip\", value: \"skip\"}], value=$bucket, variant=\"mutuallyExclusive\", displayStyle=\"chips\", label=\"Disposition\")",
  "data": {
    "title": "### Memory Audit Triage & Promotion Approval",
    "intro": "Approve updates, promotions, and retirements before `apply_memory_plan.py` snapshots and patches files.",
    "submitLabel": "Apply Approved Changes",
    "items": [
      {
        "icon": "warning",
        "title": "projects/cheats.md:28: Update superseded reviewer agent directory note",
        "summary": "Replace static reviewer agent reference with empty directory note.",
        "trace": "ls -A <repo-root>/agents/agents -> empty directory",
        "bucket": ["apply"],
        "note": ""
      },
      {
        "icon": "info",
        "title": "projects/cheats.md:60: Promote duckdb -batch -dark-mode #direct rule to AGENTS.md",
        "summary": "Append rule under '## Finding information in a workspace' in <repo-root>/AGENTS.md and remove duplicate from memory.",
        "trace": "Verified in <repo-root>/agents/skills/duckdb/SKILL.md",
        "bucket": ["apply"],
        "note": ""
      }
    ]
  }
}
```

If the user places a `promote:*` finding into `keep_in_memory`, omit that finding ID from `--approve-ids` so the rule remains in its memory file untouched.

### 3.2 Tier 2 (`ask_question`) & Tier 3 (Numbered Chat) Triage Fallbacks

When `a2ui-pane` is unavailable, ask the user via `ask_question` (or numbered chat options):

- `(Recommended) Apply all <K> verified findings (with backup snapshot in ~/.cache/audit-memory/backups/)`
- `Apply veracity updates only (skip promotions and file retirements)`
- `Apply promotions only (skip veracity updates and file retirements)`
- `Run --dry-run first and show the unified diff`
- `Select or exclude specific finding IDs (write-in)`

## 4. Prompt Template for Shard Auditors

Use this prompt brief when dispatching each read-only shard worker (`aud-1`..`aud-<k>`):

```markdown
You are a read-only memory shard auditor (`<shard-id>`) for `audit-memory`.
Do not edit, write, or delete any file in `<memory-root>` or `<repo-root>`, and do not spawn subagents or helpers.
Write your output exclusively to `<scratch-dir>/findings-<shard-id>.jsonl`.

### Assigned Shard & Context
- Inventory JSON: `<scratch-dir>/memory_inventory.json`
- Memory Root: `<memory-root>`
- Active Workspace: `<repo-root>`
- Active Lens: `<both | veracity | promotion>`
- Assigned Memory Files: `<comma-separated relative paths from shard.files>`
- Allowed Promotion Targets: see `promotion_targets` in `<scratch-dir>/memory_inventory.json`

### Audit Instructions
1. Read each assigned memory file from `<memory-root>/<relpath>` with line numbers.
2. Check every factual claim (file paths, CLI flags, symbols, test counts, status claims, architecture assertions) against primary sources in `<repo-root>` (`git log`, file reads, symbol definitions).
3. Classify veracity claims as `confirmed` (omit from output), `stale`, `false`, `dead-pointer`, or `superseded`.
4. Unless running under `--veracity-only`, evaluate durable rules and `#direct` items for 4-tier promotion routing:
   - `promote:script` (Tier 0): rule can be enforced by a linter, script, or unit test in `<repo-root>`.
   - `promote:agents_md` (Tier 1): repo-independent behavioral rule or preference belonging in `global_instructions` or `workspace_instructions`.
   - `promote:skill` (Tier 2): domain workflow or flag recipe belonging in a writable skill under `writable_skill_dirs`.
5. Protect `#direct` and `visibility: PINNED` files:
   - Never emit `action: "retire"` for `preferences.md`, `communication_guidelines.md`, `user.md`, or any file with `protected_file: true`.
   - Never drop a line containing `#direct` unless you preserve `#direct` in `replacement_lines`, route it to `promote_target`, migrate it via `direct_migration_target`, or set `allow_drop_direct: true` with primary-source proof that the rule is false.
   - When retiring a whole file (`action: "retire"`, `start_line: 1`, `end_line: <line_count>`) that contains `#direct` or promotable lines, emit separate per-line `action: "delete"` records for those lines alongside the file-level `retire` record, and set `link_rewrite_target` when incoming links exist.
6. Write exact Markdown lines in `replacement_lines` and `promote_lines` (preserving list indentation). Never write meta-instructions such as `"Update line..."` or `"Remove claim..."`.
7. Write one JSON record per line to `<scratch-dir>/findings-<shard-id>.jsonl` matching the `findings-<shard>.jsonl` schema.
```
