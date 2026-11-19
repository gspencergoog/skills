#!/usr/bin/env python3
"""Bundles code-review pre-flight steps into one structured JSON artifact.

Resolves git SHAs and unified diffs under hardened git settings, runs static
execution-risk scanning, classifies artifacts and reuse rings, stages grouped
diff chunks with AST entity tables, computes pure-Python entity churn,
signature drift, untested functions, and deleted symbols without invoking
`duckdb`, indexes 1-hop call sites via one batched `rg` query, and queries
read-only `packrat` history with salted untrusted Markdown fencing.

Usage:
  prepare_code_review.py [--root DIR] [--base REF | --staged | --whole-file PATH | --diff-file PATH]
                         [--untracked] [--pr N] [--scratch-dir DIR] [--output PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

_CODE_REVIEW_SCRIPTS = str(Path(__file__).resolve().parent)
_REVIEW_CORE_SCRIPTS = str(
    Path(__file__).resolve().parents[2] / "review-core" / "scripts"
)
_CONTEXT_INDEXER_SCRIPTS = str(
    Path(__file__).resolve().parents[2] / "context-indexer" / "scripts"
)
for _script_dir in (
    _CODE_REVIEW_SCRIPTS,
    _REVIEW_CORE_SCRIPTS,
    _CONTEXT_INDEXER_SCRIPTS,
):
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)

import classify_artifacts
import packrat_context
import security_prescan
import stage_diffs

_TEST_FILE_RE = re.compile(r"(^|/)(test_|.*_test\.|.*\.test\.|.*\.spec\.|tests?/)")
_WORD_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GIT_CONFIG_PREFIX = ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _run_git(repo_dir: Path, args: Sequence[str]) -> tuple[int, str, str]:
    """Runs a git command under hardened config and environment settings."""
    env = security_prescan.apply_hardened_git_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PAGER"] = "cat"
    cmd = [
        "git",
        *security_prescan.HARDENED_GIT_CONFIG_ARGS,
        "-c",
        "core.quotePath=false",
        "-C",
        str(repo_dir),
        *args,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


@contextmanager
def _hardened_process_git_env() -> Iterator[None]:
    """Temporarily applies hardened git variables to `os.environ` and restores them."""
    saved = {
        key: os.environ[key]
        for key in list(os.environ)
        if key.startswith(_GIT_CONFIG_PREFIX)
    }
    try:
        security_prescan.apply_hardened_git_env_in_place(os.environ)
        yield
    finally:
        for key in list(os.environ):
            if key.startswith(_GIT_CONFIG_PREFIX) and key not in saved:
                os.environ.pop(key, None)
        os.environ.update(saved)


def resolve_git_ref(repo_dir: Path, ref: str) -> str | None:
    """Resolves `ref` to a commit SHA, falling back to `origin/<ref>`.

    Verifies `<ref>^{commit}` first. When that fails and `ref` is not already
    `origin/`-prefixed, verifies `origin/<ref>^{commit}` so detached-HEAD clones
    without local tracking branches still resolve cleanly.

    Args:
        repo_dir: Path inside a git repository.
        ref: Branch name, tag, revision expression, or commit SHA.

    Returns:
        The resolved full commit SHA, or `None` if neither ref exists.
    """
    cleaned = (ref or "").strip()
    if not cleaned:
        return None
    candidates = (
        [cleaned] if cleaned.startswith("origin/") else [cleaned, f"origin/{cleaned}"]
    )
    for candidate in candidates:
        rc, stdout, _ = _run_git(
            repo_dir, ["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"]
        )
        sha = stdout.strip()
        if rc == 0 and sha:
            return sha
    return None


def _resolve_merge_base(repo_dir: Path, base_sha: str, head_sha: str | None) -> str:
    """Returns `git merge-base` between `base_sha` and `head_sha`, or `base_sha`."""
    if not head_sha:
        return base_sha
    rc, stdout, _ = _run_git(repo_dir, ["merge-base", base_sha, head_sha])
    merged = stdout.strip()
    return merged if rc == 0 and merged else base_sha


def _join_diffs(parts: Sequence[str]) -> str:
    """Concatenates non-empty unified diff fragments with trailing newlines."""
    chunks: list[str] = []
    for part in parts:
        if not part:
            continue
        chunks.append(part if part.endswith("\n") else part + "\n")
    return "".join(chunks)


def _diff_no_index_file(repo_dir: Path, target_arg: str) -> str:
    """Produces an all-added unified diff for one file via `git diff --no-index`."""
    rc, stdout, stderr = _run_git(
        repo_dir,
        [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-index",
            "--",
            "/dev/null",
            target_arg,
        ],
    )
    if rc in (0, 1):
        return stdout
    raise RuntimeError(f"git diff --no-index failed for {target_arg}: {stderr.strip()}")


def _diff_untracked_files(repo_dir: Path) -> str:
    """Builds unified diffs for all untracked, non-ignored files in `repo_dir`."""
    rc, stdout, stderr = _run_git(
        repo_dir, ["ls-files", "--others", "--exclude-standard", "-z"]
    )
    if rc != 0:
        raise RuntimeError(f"git ls-files failed: {stderr.strip()}")
    paths = [p for p in stdout.split("\0") if p]
    return _join_diffs([_diff_no_index_file(repo_dir, rel) for rel in paths])


def _diff_whole_file(repo_dir: Path, whole_file: Path) -> str:
    """Generates a `/dev/null`-to-file unified diff for `whole_file`."""
    target = whole_file if whole_file.is_absolute() else (repo_dir / whole_file)
    if not target.is_file():
        raise FileNotFoundError(f"Whole file not found: {target}")
    resolved_root = repo_dir.resolve()
    resolved_target = target.resolve()
    if resolved_target.is_relative_to(resolved_root):
        arg = resolved_target.relative_to(resolved_root).as_posix()
    else:
        arg = str(resolved_target)
    return _diff_no_index_file(repo_dir, arg)


def _extract_git_diff_text(
    repo_dir: Path,
    *,
    base_ref: str | None,
    staged: bool,
    include_untracked: bool,
    head_sha: str | None,
) -> tuple[str, str | None, str | None]:
    """Extracts a tracked (and optional untracked) diff from git."""
    if base_ref is not None:
        resolved_base = resolve_git_ref(repo_dir, base_ref)
        if resolved_base is None:
            raise ValueError(f"Could not resolve git base ref: {base_ref}")
        base_sha = _resolve_merge_base(repo_dir, resolved_base, head_sha)
        diff_args = [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            f"{resolved_base}...HEAD",
        ]
    elif staged:
        base_sha = head_sha
        diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--cached"]
    else:
        base_sha = head_sha
        diff_args = (
            ["diff", "--no-ext-diff", "--no-textconv", "HEAD"]
            if head_sha
            else ["diff", "--no-ext-diff", "--no-textconv"]
        )

    rc, stdout, stderr = _run_git(repo_dir, diff_args)
    if rc != 0:
        raise RuntimeError(f"git diff failed: {stderr.strip()}")
    parts = [stdout]
    if include_untracked:
        parts.append(_diff_untracked_files(repo_dir))
    return _join_diffs(parts), base_sha, base_sha


def _resolve_diff_input(
    repo_dir: Path,
    *,
    diff_path: Path | None,
    base_ref: str | None,
    staged: bool,
    include_untracked: bool,
    whole_file: Path | None,
) -> tuple[str, str | None, str | None, str | None]:
    """Resolves `(diff_text, base_sha, head_sha, stage_base_ref)` for any input mode."""
    head_sha = resolve_git_ref(repo_dir, "HEAD") if repo_dir.is_dir() else None
    if diff_path is not None:
        if not diff_path.is_file():
            raise FileNotFoundError(f"Diff file not found: {diff_path}")
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        resolved_base = resolve_git_ref(repo_dir, base_ref) if base_ref else None
        base_sha = (
            _resolve_merge_base(repo_dir, resolved_base, head_sha)
            if resolved_base
            else None
        )
        return diff_text, base_sha, head_sha, base_sha or resolved_base

    if whole_file is not None:
        return _diff_whole_file(repo_dir, whole_file), None, head_sha, None

    diff_text, base_sha, stage_ref = _extract_git_diff_text(
        repo_dir,
        base_ref=base_ref,
        staged=staged,
        include_untracked=include_untracked,
        head_sha=head_sha,
    )
    return diff_text, base_sha, head_sha, stage_ref


def stage_diff_for_review(
    diff_path: Path,
    repo_dir: Path,
    base_ref: str | None,
    diff_dir: str | Path,
) -> str | None:
    """Stages `diff_path` into grouped chunks and JSONL tables with AST entities.

    Runs `stage_diffs.split_diff_grouped` in-process with hardened git
    environment variables so `git show` never triggers hooks or fsmonitor.

    Args:
        diff_path: Unified diff file to stage.
        repo_dir: Repository checkout directory for AST entity extraction.
        base_ref: Optional git ref for OLD-side AST entity spans.
        diff_dir: Destination directory for chunks and JSONL files.

    Returns:
        `None` on success, or an error message string on failure.
    """
    try:
        diff_text = Path(diff_path).read_text(encoding="utf-8", errors="replace")
        with _hardened_process_git_env():
            stage_diffs.split_diff_grouped(
                diff_text,
                str(diff_dir),
                repo_dir=str(repo_dir),
                base_ref=base_ref or None,
                with_entities=True,
            )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return str(exc)
    return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Reads newline-delimited JSON objects, skipping blank or malformed lines."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _round_half_up_tenth(numerator: int, denominator: int) -> float | None:
    """Computes `ROUND(numerator * 100.0 / NULLIF(denominator, 0), 1)` matching DuckDB."""
    if denominator == 0:
        return None
    val = (Decimal(numerator * 100) / Decimal(denominator)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return float(val)


def _index_new_entities(
    entities: Sequence[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Groups valid `NEW`-side AST entities by `file_path`."""
    by_file: dict[str, list[dict[str, Any]]] = {}
    for ent in entities:
        if ent.get("side") != "NEW":
            continue
        try:
            start = int(ent["line_start"])
            end = int(ent["line_end"])
        except (KeyError, TypeError, ValueError):
            continue
        fp = str(ent.get("file_path") or "")
        by_file.setdefault(fp, []).append(
            {
                "file_path": fp,
                "kind": str(ent.get("kind") or ""),
                "name": str(ent.get("name") or ""),
                "signature": str(ent.get("signature") or ""),
                "line_start": start,
                "line_end": end,
            }
        )
    return by_file


def _find_innermost_entity(
    candidates: Sequence[dict[str, Any]], new_line: int
) -> dict[str, Any] | None:
    """Returns the narrowest enclosing entity for `new_line`, breaking ties by latest start."""
    best: dict[str, Any] | None = None
    best_key: tuple[int, int] | None = None
    for ent in candidates:
        if ent["line_start"] <= new_line <= ent["line_end"]:
            key = (ent["line_end"] - ent["line_start"], -ent["line_start"])
            if best_key is None or key < best_key:
                best = ent
                best_key = key
    return best


def _str_field(record: dict[str, Any], key: str) -> str:
    """Returns a string field from `record`, coercing `None` to `""`."""
    value = record.get(key)
    return "" if value is None else str(value)


def _int_field(record: dict[str, Any], key: str) -> int:
    """Returns an integer field from `record`, defaulting to `0`."""
    value = record.get(key)
    return 0 if value is None else int(value)


def _entity_key(ent: dict[str, Any]) -> tuple[str, str, str]:
    """Returns `(file_path, kind, name)` identity tuple for one entity record."""
    return (
        _str_field(ent, "file_path"),
        _str_field(ent, "kind"),
        _str_field(ent, "name"),
    )


def _iter_unique_added_lines(
    lines: Sequence[dict[str, Any]],
) -> Iterator[tuple[str, int, str]]:
    """Yields unique `(file_path, new_line, chunk_name)` triples for `+` lines."""
    seen: set[tuple[str, int]] = set()
    for rec in lines:
        if rec.get("op") != "+" or rec.get("new_line") is None:
            continue
        fp = _str_field(rec, "file_path")
        try:
            new_line = int(rec["new_line"])
        except (TypeError, ValueError):
            continue
        line_key = (fp, new_line)
        if line_key not in seen:
            seen.add(line_key)
            yield fp, new_line, _str_field(rec, "chunk_name")


def _compute_entity_churn(
    entities: Sequence[dict[str, Any]], lines: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pure-Python equivalent of `staged_diff_macros.sql::diff_entity_churn`."""
    by_file = _index_new_entities(entities)
    grouped: dict[tuple[str, str, str, str, int, int], list[Any]] = {}

    for fp, new_line, chunk_name in _iter_unique_added_lines(lines):
        ent = _find_innermost_entity(by_file.get(fp, ()), new_line)
        if ent is None:
            continue
        group_key = (
            ent["file_path"],
            ent["kind"],
            ent["name"],
            ent["signature"],
            ent["line_start"],
            ent["line_end"],
        )
        entry = grouped.get(group_key)
        if entry is None:
            grouped[group_key] = [chunk_name, 1]
        else:
            entry[1] += 1

    rows = [
        {
            "file_path": fp,
            "kind": kind,
            "name": name,
            "signature": sig,
            "line_start": start,
            "line_end": end,
            "chunk_name": chunk_name,
            "lines_added": added,
            "total_entity_lines": end - start + 1,
            "pct_rewritten": _round_half_up_tenth(added, end - start + 1),
        }
        for (fp, kind, name, sig, start, end), (chunk_name, added) in grouped.items()
    ]
    rows.sort(key=lambda r: (-r["lines_added"], r["file_path"], r["line_start"]))
    return rows


def _index_old_entities(
    entities: Sequence[dict[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Indexes `OLD`-side AST entities by `(file_path, kind, name)`."""
    old_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for ent in entities:
        if ent.get("side") == "OLD":
            old_by_key.setdefault(_entity_key(ent), []).append(ent)
    return old_by_key


def _drift_row(
    key: tuple[str, str, str],
    o_ent: dict[str, Any],
    n_ent: dict[str, Any],
) -> dict[str, Any]:
    """Builds one `signature_drift` record from matching OLD and NEW entities."""
    return {
        "file_path": key[0],
        "kind": key[1],
        "name": key[2],
        "old_signature": _str_field(o_ent, "signature"),
        "new_signature": _str_field(n_ent, "signature"),
        "old_line_start": _int_field(o_ent, "line_start"),
        "new_line_start": _int_field(n_ent, "line_start"),
        "new_line_end": _int_field(n_ent, "line_end"),
    }


def _compute_signature_drift(
    entities: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pure-Python equivalent of `staged_diff_macros.sql::diff_signature_drift`."""
    old_by_key = _index_old_entities(entities)
    drift: list[dict[str, Any]] = []
    for n_ent in entities:
        if n_ent.get("side") != "NEW":
            continue
        key = _entity_key(n_ent)
        n_sig = _str_field(n_ent, "signature")
        for o_ent in old_by_key.get(key, ()):
            if _str_field(o_ent, "signature") != n_sig:
                drift.append(_drift_row(key, o_ent, n_ent))
    drift.sort(key=lambda r: (r["file_path"], r["new_line_start"], r["old_line_start"]))
    return drift


def _compute_untested_functions(
    all_churn: Sequence[dict[str, Any]], lines: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pure-Python equivalent of `staged_diff_macros.sql::diff_untested_functions`."""
    test_contents = {
        _str_field(rec, "content")
        for rec in lines
        if rec.get("op") in ("+", " ")
        and _TEST_FILE_RE.search(_str_field(rec, "file_path"))
    }
    untested = [
        {
            "file_path": row["file_path"],
            "kind": row["kind"],
            "name": row["name"],
            "signature": row["signature"],
            "chunk_name": row["chunk_name"],
            "lines_added": row["lines_added"],
            "pct_rewritten": row["pct_rewritten"],
        }
        for row in all_churn
        if row["kind"] in ("function", "method")
        and not _TEST_FILE_RE.search(row["file_path"])
        and not row["name"].startswith("_")
        and not any(row["name"] in content for content in test_contents)
    ]
    untested.sort(key=lambda r: (-r["lines_added"], r["file_path"]))
    return untested


def _compute_deleted_symbols(
    entities: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Finds OLD-side AST symbols absent from the NEW side of the same file."""
    new_keys = {_entity_key(ent) for ent in entities if ent.get("side") == "NEW"}
    deleted: list[dict[str, Any]] = []
    for ent in entities:
        if ent.get("side") != "OLD":
            continue
        key = _entity_key(ent)
        if key not in new_keys:
            deleted.append(
                {
                    "file_path": key[0],
                    "kind": key[1],
                    "name": key[2],
                    "old_signature": _str_field(ent, "signature"),
                    "old_line_start": _int_field(ent, "line_start"),
                    "old_line_end": _int_field(ent, "line_end"),
                }
            )
    deleted.sort(key=lambda r: (r["file_path"], r["old_line_start"], r["name"]))
    return deleted


def compute_staged_entity_summaries(
    diff_dir: Path, *, limit: int = 25
) -> dict[str, list[dict[str, Any]]]:
    """Computes AST entity churn, signature drift, untested functions, and deleted symbols.

    Reads `diff_entities.jsonl` and `diff_lines.jsonl` emitted by
    `stage_diffs.py --grouped --with-entities` in pure Python without spawning
    `duckdb`.

    Args:
        diff_dir: Directory containing staged diff JSONL files.
        limit: Maximum rows returned per summary table. Zero or negative returns all.

    Returns:
        A dict with keys `entity_churn`, `signature_drift`, `untested_functions`,
        and `deleted_symbols`.
    """
    base = Path(diff_dir)
    entities = _read_jsonl(base / "diff_entities.jsonl")
    lines = _read_jsonl(base / "diff_lines.jsonl")

    all_churn = _compute_entity_churn(entities, lines)
    drift = _compute_signature_drift(entities)
    untested = _compute_untested_functions(all_churn, lines)
    deleted = _compute_deleted_symbols(entities)

    if limit > 0:
        return {
            "entity_churn": all_churn[:limit],
            "signature_drift": drift[:limit],
            "untested_functions": untested[:limit],
            "deleted_symbols": deleted[:limit],
        }
    return {
        "entity_churn": all_churn,
        "signature_drift": drift,
        "untested_functions": untested,
        "deleted_symbols": deleted,
    }


def _dedupe_symbols(symbols: Sequence[str], max_symbols: int) -> list[str]:
    """Deduplicates valid word symbols while preserving order, capped at `max_symbols`."""
    if max_symbols <= 0:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in symbols:
        sym = str(raw).strip()
        if sym and sym not in seen and _WORD_SYMBOL_RE.match(sym):
            seen.add(sym)
            ordered.append(sym)
            if len(ordered) >= max_symbols:
                break
    return ordered


def _parse_rg_call_sites(
    stdout: str,
    selected: Sequence[str],
    max_matches_per_symbol: int,
) -> list[dict[str, Any]]:
    """Groups `rg -n --no-heading --color=never -w` output lines by matched symbol."""
    ordered_by_len = sorted(selected, key=len, reverse=True)
    symbol_re = re.compile(
        r"\b(" + "|".join(re.escape(sym) for sym in ordered_by_len) + r")\b"
    )
    counts: dict[str, int] = {sym: 0 for sym in selected}
    matches: dict[str, list[dict[str, Any]]] = {sym: [] for sym in selected}

    for raw_line in stdout.splitlines():
        parts = raw_line.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        rel_path = parts[0].removeprefix("./")
        line_num = int(parts[1])
        snippet = parts[2].strip()[:160]
        matched_syms = set(symbol_re.findall(parts[2]))
        for sym in matched_syms:
            counts[sym] += 1
            if len(matches[sym]) < max_matches_per_symbol:
                matches[sym].append(
                    {"file_path": rel_path, "line": line_num, "snippet": snippet}
                )

    return [
        {
            "symbol": sym,
            "match_count": counts[sym],
            "truncated": counts[sym] > max_matches_per_symbol,
            "matches": matches[sym],
        }
        for sym in selected
    ]


def collect_one_hop_call_sites(
    repo_dir: Path,
    symbols: Sequence[str],
    *,
    max_symbols: int = 25,
    max_matches_per_symbol: int = 30,
) -> list[dict[str, Any]]:
    """Indexes 1-hop repository call sites for `symbols` in a single batched `rg` call.

    Args:
        repo_dir: Repository root directory to search.
        symbols: Candidate symbol names (typically from `signature_drift` and
            `deleted_symbols`).
        max_symbols: Maximum unique symbols included in the batched query.
        max_matches_per_symbol: Maximum recorded matches per symbol.

    Returns:
        A list of `{symbol, match_count, truncated, matches}` dicts in symbol order,
        or an empty list when no valid symbols exist or `rg` is unavailable.
    """
    selected = _dedupe_symbols(symbols, max_symbols)
    if not selected or not Path(repo_dir).is_dir():
        return []
    rg_bin = shutil.which("rg")
    if not rg_bin:
        return []

    pattern = "|".join(
        re.escape(sym) for sym in sorted(selected, key=len, reverse=True)
    )
    cmd = [rg_bin, "-n", "--no-heading", "--color=never", "-w", "-e", pattern, "."]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode not in (0, 1):
        return []
    return _parse_rg_call_sites(proc.stdout, selected, max_matches_per_symbol)


def query_packrat_bundle(
    repo_dir: Path,
    changed_files: Sequence[str],
    *,
    pull_number: int | None = None,
    exclude_head_sha: str | None = None,
) -> dict[str, Any] | None:
    """Queries `packrat_context` read-only in-process and degrades to `None` on errors."""
    try:
        repo_slug, _ = packrat_context.resolve_github_repo(repo_dir)
        if repo_slug is None:
            probe = packrat_context.probe_packrat(repo_dir)
            return packrat_context._empty_context_payload(probe)
        return packrat_context.query_changed_files_context(
            None,
            repo_slug,
            changed_files,
            exclude_pr=pull_number,
            exclude_head_sha=exclude_head_sha,
            root=repo_dir,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ):
        return None


def _persist_generated_diff(
    diff_text: str,
    diff_path: Path | None,
    scratch_dir: Path | None,
    diff_dir: str | None,
) -> Path | None:
    """Returns `diff_path` unchanged, or writes generated diff text to scratch/temp."""
    if diff_path is not None:
        return diff_path
    if not diff_text.strip():
        return None
    if scratch_dir is not None:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        target = scratch_dir / "review.diff"
    elif diff_dir is not None:
        target = Path(diff_dir) / "review.diff"
    else:
        return None
    target.write_text(diff_text, encoding="utf-8")
    return target


def _run_staging_and_entities(
    *,
    diff_text: str,
    diff_path: Path | None,
    repo_dir: Path,
    stage_base_ref: str | None,
    scratch_dir: Path | None,
    warnings: list[str],
    mkdtemp: Callable[..., str],
    stage_fn: Callable[[Path, Path, str | None, str], str | None],
) -> tuple[
    str | None, str | None, dict[str, list[dict[str, Any]] | None], list[dict[str, Any]]
]:
    """Stages the diff, computes AST entity summaries, and collects 1-hop call sites."""
    empty_summaries: dict[str, list[dict[str, Any]] | None] = {
        "entity_churn": [],
        "signature_drift": [],
        "untested_functions": [],
        "deleted_symbols": [],
    }
    if not diff_text.strip():
        warnings.append("diff is empty")
        return str(diff_path) if diff_path else None, None, empty_summaries, []

    diff_dir = mkdtemp(prefix="pr-review-diffs-")
    written_diff = _persist_generated_diff(diff_text, diff_path, scratch_dir, diff_dir)
    stage_input = written_diff or (Path(diff_dir) / "review.diff")
    if not stage_input.is_file():
        stage_input.write_text(diff_text, encoding="utf-8")

    stage_error = stage_fn(stage_input, repo_dir, stage_base_ref, diff_dir)
    if stage_error:
        shutil.rmtree(diff_dir, ignore_errors=True)
        warnings.append(f"stage_diffs.py failed: {stage_error}")
        none_summaries: dict[str, list[dict[str, Any]] | None] = {
            "entity_churn": None,
            "signature_drift": None,
            "untested_functions": None,
            "deleted_symbols": None,
        }
        diff_file_out = (
            str(diff_path)
            if diff_path
            else (
                str(written_diff) if written_diff and written_diff.is_file() else None
            )
        )
        return diff_file_out, None, none_summaries, []

    summaries = compute_staged_entity_summaries(Path(diff_dir))
    symbol_targets = [row["name"] for row in summaries["signature_drift"]] + [
        row["name"] for row in summaries["deleted_symbols"]
    ]
    call_sites = collect_one_hop_call_sites(repo_dir, symbol_targets)
    return (
        str(written_diff) if written_diff else str(stage_input),
        diff_dir,
        dict(summaries),
        call_sites,
    )


def prepare_review_bundle(
    repo_dir: Path,
    *,
    diff_path: Path | None = None,
    base_ref: str | None = None,
    staged: bool = False,
    include_untracked: bool = False,
    whole_file: Path | None = None,
    pull_number: int | None = None,
    scratch_dir: Path | None = None,
    untrusted_nonce: str | None = None,
    _mkdtemp: Callable[..., str] | None = None,
    _stage_fn: Callable[[Path, Path, str | None, str], str | None] | None = None,
    _packrat_fn: Callable[[Sequence[str], Path, int | None], dict[str, Any] | None]
    | None = None,
) -> dict[str, Any]:
    """Runs all code-review pre-flight checks and returns a `code_review_prep.json` dict.

    Args:
        repo_dir: Repository checkout root directory.
        diff_path: Optional existing unified diff file to inspect read-only.
        base_ref: Optional git base ref for `<base>...HEAD` diffs and OLD-side AST.
        staged: Whether to diff staged index changes (`git diff --cached`).
        include_untracked: Whether to append untracked file diffs.
        whole_file: Optional single file path to review as a `/dev/null` diff.
        pull_number: Optional PR number to exclude from `packrat` history.
        scratch_dir: Optional conversation scratch directory to record and store diffs.
        untrusted_nonce: Optional pre-generated hex nonce for salted fencing.

    Returns:
        A JSON-serializable `code_review_prep.json` dictionary.
    """
    root_path = Path(repo_dir)
    scratch_path = Path(scratch_dir) if scratch_dir is not None else None
    diff_file_path = Path(diff_path) if diff_path is not None else None
    whole_file_path = Path(whole_file) if whole_file is not None else None

    nonce = (
        untrusted_nonce
        if untrusted_nonce is not None
        else security_prescan.generate_untrusted_nonce()
    )
    security_prescan.wrap_salted_untrusted("validate", "", nonce=nonce)

    diff_text, base_sha, head_sha, stage_base_ref = _resolve_diff_input(
        root_path,
        diff_path=diff_file_path,
        base_ref=base_ref,
        staged=staged,
        include_untracked=include_untracked,
        whole_file=whole_file_path,
    )

    warnings: list[str] = []
    changed_files = classify_artifacts._extract_paths_from_diff(diff_text)
    prescan = security_prescan.scan_diff_execution_risks(diff_text)
    classification = classify_artifacts.classify_combined(
        changed_files, root_path, diff_text
    )

    diff_file_str, diff_dir, summaries, call_sites = _run_staging_and_entities(
        diff_text=diff_text,
        diff_path=diff_file_path,
        repo_dir=root_path,
        stage_base_ref=stage_base_ref,
        scratch_dir=scratch_path,
        warnings=warnings,
        mkdtemp=_mkdtemp or tempfile.mkdtemp,
        stage_fn=_stage_fn or stage_diff_for_review,
    )

    if _packrat_fn is not None:
        packrat = _packrat_fn(changed_files, root_path, pull_number)
    else:
        packrat = query_packrat_bundle(
            root_path,
            changed_files,
            pull_number=pull_number,
            exclude_head_sha=head_sha,
        )
    if packrat is None:
        warnings.append("packrat_context.py unavailable or failed")
        packrat_markdown = ""
    else:
        packrat_markdown = packrat_context.format_markdown_context(packrat, nonce=nonce)

    return {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changed_files": changed_files,
        "diff_file": diff_file_str,
        "diff_dir": diff_dir,
        "scratch_dir": str(scratch_path) if scratch_path is not None else None,
        "untrusted_nonce": nonce,
        "security_prescan": prescan,
        "classification": classification,
        "entity_churn": summaries["entity_churn"],
        "signature_drift": summaries["signature_drift"],
        "untested_functions": summaries["untested_functions"],
        "deleted_symbols": summaries["deleted_symbols"],
        "call_sites": call_sites,
        "packrat": packrat,
        "packrat_markdown": packrat_markdown,
        "warnings": warnings,
    }


def _build_parser() -> argparse.ArgumentParser:
    """Constructs the CLI argument parser for `prepare_code_review.py`."""
    parser = argparse.ArgumentParser(
        prog="prepare_code_review.py",
        description="Run bundled code-review pre-flight checks and emit code_review_prep.json.",
        exit_on_error=False,
    )
    parser.add_argument("--root", default=".", help="Repository checkout directory.")
    parser.add_argument(
        "--base", default=None, help="Git base ref for <base>...HEAD diff."
    )
    parser.add_argument(
        "--staged", action="store_true", help="Diff staged index changes (--cached)."
    )
    parser.add_argument(
        "--untracked",
        action="store_true",
        help="Include untracked files via git diff --no-index.",
    )
    parser.add_argument(
        "--whole-file",
        default=None,
        help="Review a single whole file via git diff --no-index /dev/null <path>.",
    )
    parser.add_argument(
        "--diff-file", default=None, help="Existing unified diff file to bundle."
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="Pull request number to exclude from packrat.",
    )
    parser.add_argument(
        "--scratch-dir",
        default=None,
        help="Conversation scratch directory for storing generated diffs and metadata.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output path to write the code_review_prep.json payload.",
    )
    return parser


def _validate_cli_args(args: argparse.Namespace) -> None:
    """Rejects contradictory diff-source CLI flag combinations."""
    if args.diff_file and args.whole_file:
        raise argparse.ArgumentError(
            None, "cannot specify both --diff-file and --whole-file"
        )
    if args.whole_file and (args.staged or args.base or args.untracked):
        raise argparse.ArgumentError(
            None,
            "--whole-file cannot be combined with --staged, --base, or --untracked",
        )
    if args.diff_file and (args.staged or args.untracked):
        raise argparse.ArgumentError(
            None, "--diff-file cannot be combined with --staged or --untracked"
        )
    if args.staged and args.base:
        raise argparse.ArgumentError(None, "cannot specify both --staged and --base")


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the CLI entrypoint and returns the process exit code."""
    parser = _build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
        _validate_cli_args(args)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    except argparse.ArgumentError as exc:
        print(f"Usage error: {exc}", file=sys.stderr)
        return 2

    try:
        bundle = prepare_review_bundle(
            Path(args.root),
            diff_path=Path(args.diff_file) if args.diff_file else None,
            base_ref=args.base,
            staged=args.staged,
            include_untracked=args.untracked,
            whole_file=Path(args.whole_file) if args.whole_file else None,
            pull_number=args.pr,
            scratch_dir=Path(args.scratch_dir) if args.scratch_dir else None,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error preparing code review bundle: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(bundle, indent=2)
    if args.output:
        out_path = Path(args.output)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(payload + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Error writing output file: {exc}", file=sys.stderr)
            return 1
    sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
