---
name: github-collab-search
description: >-
  Discover, extract, and summarize collaborative GitHub interactions (pull requests, issues, reviews, and line comments) between two users across repositories or organizations. Use when preparing peer feedback (e.g. peer review cycles or performance reviews), analyzing collaboration history, auditing co-authored changes, or overcoming GitHub's search API boolean OR qualifier bug and 1,000-result caps.
---

# GitHub Collaboration Search (`github-collab-search`)

This skill discovers, filters, and digests collaborative GitHub history between two engineers across repositories and organizations.

[TOC]

---

## Why This Skill Exists

Native GitHub search commands fail when searching for collaborative interactions between two specific users:

1. **Boolean OR Defect on Repeated Qualifiers**:
   In GitHub search syntax, repeating user qualifiers (`involves:A involves:B` or `commenter:A commenter:B`) behaves as boolean **OR**, not AND. In large repositories, this returns hundreds or thousands of threads where only one of the two users participated.
2. **Reviewer & Commenter Gaps**:
   Queries like `--author A --reviewed-by B` act as boolean AND, but miss:
   - PRs where A authored and B commented on lines without submitting a formal review.
   - PRs where a third party (C) authored and both A and B actively reviewed or discussed.
   - Issues where both A and B collaborated.
3. **1,000-Result Search Cap**:
   GitHub's search API truncates queries at 1,000 results. In active repositories, queries covering long time periods lose critical threads unless partitioned across date windows.

`github-collab-search` uses partitioned GraphQL queries and participant set filtering to reliably extract every shared thread.

---

## Canonical CLI Invocation

The skill CLI is located at `scripts/collab_search.py` within this skill directory:

```bash
COLLAB_SEARCH="python3 scripts/collab_search.py"
```

### 1. Standard Collaboration Digest (Markdown)

Extract collaborative interactions between two engineers in a target repository:

```bash
# Search within a specific repository over a date range
$COLLAB_SEARCH --repo owner/repo --user-a alice --user-b bob --since 2026-01-01 --until 2026-06-30

# Automatically detect repository from the current working directory
$COLLAB_SEARCH --user-a alice --user-b bob --since 2026-01-01 --until 2026-06-30

# Save output to a file
$COLLAB_SEARCH --repo owner/repo --user-a alice --user-b bob --out collaboration_report.md
```

### 2. High-Level Summary Statistics (`--format summary`)

Quickly check interaction counts without generating full comment transcripts:

```bash
$COLLAB_SEARCH --repo owner/repo --user-a alice --user-b bob --format summary
```

### 3. Machine-Readable JSON Export (`--format json`)

Export raw structured thread data for programmatic processing or LLM digestion:

```bash
$COLLAB_SEARCH --repo owner/repo --user-a alice --user-b bob --format json --out data.json
```

### 4. Partitioning for Parallel Subagents (`--split-chunks`)

When the extracted collaboration history is large (>150 KB), partition it along thread boundaries into smaller files ($\le 120$ KB) suitable for parallel subagent analysis:

```bash
$COLLAB_SEARCH --repo owner/repo --user-a alice --user-b bob --split-chunks 120 --out /path/to/chunks_dir
```

---

## Output Structure

The generated Markdown report categorizes interactions into:
- **Overview Statistics**: Total threads, total PRs (merged, open, closed), and total issues.
- **Pull Request Collaborations**:
  - PR title, number, state, URL, author, and last update date.
  - User A's role and actions (authored, reviews submitted with verdict, comments).
  - User B's role and actions.
- **Issue Discussions**:
  - Issue title, number, state, URL, author, and comment counts from each participant.

---

## Agent Guidelines & Workflows

1. **For Peer Feedback / Review Prep**:
   - Run `$COLLAB_SEARCH --user-a <current_user> --user-b <peer_handle> --since <cycle_start> --until <cycle_end>`.
   - Review merged PRs and review comments to identify key architectural contributions, constructive feedback threads, and co-designed features.
2. **Handling Large Histories**:
   - If the output is large, use `--split-chunks 120`.
   - If fanning out subagents to summarize chunks, remember global subagent guardrails: keep fan-out small, pass `enable_subagent_tools: false`, and use capable models for synthesis.
