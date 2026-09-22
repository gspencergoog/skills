#!/usr/bin/env python3
"""
merge_assistant.py - Intent-Driven Merge Support & Intent Harvester CLI.

Provides in-memory 3-way merge pre-flight, multi-language AST hazard detection,
squash-merge ancestry recovery, cross-directory move resolution via git merge-file,
conflict tier classification, cross-repo remote setup, and dry-run patch porting.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def run_cmd(
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Runs a subprocess command and returns (exit_code, stdout, stderr)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    res = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=full_env,
    )
    return res.returncode, res.stdout, res.stderr


# ----------------------------------------------------------------------
# 1. Base & Ancestry Discovery (including Squash-Merged History)
# ----------------------------------------------------------------------


def find_merge_base(
    repo: Path,
    ours: str,
    theirs: str,
    subtree: Optional[str] = None,
) -> Optional[str]:
    """Finds the standard merge-base between two refs."""
    code, out, _ = run_cmd(["git", "merge-base", ours, theirs], cwd=repo)
    if code == 0 and out.strip():
        return out.strip().splitlines()[0].strip()
    return None


def _is_ancestor(repo: Path, candidate: str, target: str) -> bool:
    """Returns True if candidate is a valid commit ancestor of target."""
    code, _, _ = run_cmd(
        ["git", "merge-base", "--is-ancestor", candidate, target], cwd=repo
    )
    return code == 0


def _find_theirs_sha_in_text(repo: Path, text: str, theirs: str) -> Optional[str]:
    """Scans text for a commit SHA that is an ancestor of theirs."""
    for cand in re.findall(r"\b([0-9a-f]{7,40})\b", text, re.IGNORECASE):
        if _is_ancestor(repo, cand, theirs):
            return cand
    return None


def _lookup_pr_merge_sha(repo: Path, pr_num: str, theirs: str) -> Optional[str]:
    """Queries gh pr view for a squash-merged PR body mentioning a theirs SHA."""
    if not shutil.which("gh"):
        return None
    code, out, _ = run_cmd(
        ["gh", "pr", "view", pr_num, "--json", "body,title,headRefOid"],
        cwd=repo,
    )
    if code != 0:
        return None
    try:
        pr_data = json.loads(out)
        return _find_theirs_sha_in_text(repo, pr_data.get("body", ""), theirs)
    except Exception:
        return None


def _inspect_commit_for_squash(
    repo: Path, entry: str, theirs: str
) -> Optional[Dict[str, Any]]:
    """Checks a single git log entry for squash-merge ancestry markers."""
    parts = entry.strip().split("|", 2)
    if len(parts) < 2:
        return None
    commit_sha = parts[0].strip()
    subject = parts[1].strip()
    body = parts[2].strip() if len(parts) > 2 else ""

    found_sha = _find_theirs_sha_in_text(repo, f"{subject}\n{body}", theirs)
    if found_sha and found_sha != commit_sha:
        return {
            "squash_commit": commit_sha,
            "prior_theirs_sha": found_sha,
            "method": "commit_message_mention",
            "subject": subject,
        }

    pr_match = re.search(r"\(#(\d+)\)", subject)
    if pr_match:
        pr_sha = _lookup_pr_merge_sha(repo, pr_match.group(1), theirs)
        if pr_sha:
            return {
                "squash_commit": commit_sha,
                "prior_theirs_sha": pr_sha,
                "method": f"gh_pr_view_#{pr_match.group(1)}",
                "subject": subject,
            }
    return None


def detect_squash_merge_ancestry(
    repo: Path,
    ours: str,
    theirs: str,
) -> Optional[Dict[str, Any]]:
    """Detects if a recent commit on ours was a squash-merge from theirs."""
    code, out, _ = run_cmd(
        ["git", "log", "-n", "30", "--format=%H|%s|%b<END_COMMIT>", ours],
        cwd=repo,
    )
    if code != 0:
        return None
    for entry in out.split("<END_COMMIT>"):
        if entry.strip():
            match = _inspect_commit_for_squash(repo, entry, theirs)
            if match:
                return match
    return None


def record_squash_ancestry(
    repo: Path,
    prior_theirs_sha: str,
    subject_note: str = "",
) -> Tuple[int, str]:
    """Records git merge -s ours <prior_theirs_sha> to repair a lost second parent."""
    msg = f"chore: record ancestry for {prior_theirs_sha}"
    if subject_note:
        msg += f" ({subject_note})"
    code, out, err = run_cmd(
        ["git", "merge", "-s", "ours", prior_theirs_sha, "-m", msg],
        cwd=repo,
    )
    return code, out or err


# ----------------------------------------------------------------------
# 2. Multi-Language AST & Symbol Extraction
# ----------------------------------------------------------------------


@dataclass
class AstSymbol:
    kind: str  # function, method, class, interface, enum, schema_def
    name: str
    signature: str = ""
    params: List[str] = field(default_factory=list)
    line_start: int = 1
    line_end: int = 1


def _extract_py_ast(content: str) -> List[AstSymbol]:
    """Extracts Python functions, classes, and methods using stdlib ast."""
    symbols: List[AstSymbol] = []
    try:
        tree = ast.parse(content)
    except Exception:
        return symbols

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = [arg.arg for arg in node.args.args]
            symbols.append(
                AstSymbol(
                    kind="function",
                    name=node.name,
                    signature=f"def {node.name}({', '.join(params)})",
                    params=params,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                )
            )
        elif isinstance(node, ast.ClassDef):
            symbols.append(
                AstSymbol(
                    kind="class",
                    name=node.name,
                    signature=f"class {node.name}",
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                )
            )
    return symbols


def _extract_schema_enums(props: Dict[str, Any]) -> List[AstSymbol]:
    """Extracts enum values from JSON Schema properties."""
    symbols: List[AstSymbol] = []
    for prop_name, prop_val in props.items():
        if isinstance(prop_val, dict) and "enum" in prop_val:
            for enum_val in prop_val.get("enum", []):
                symbols.append(
                    AstSymbol(
                        kind="enum_value",
                        name=f"{prop_name}::{enum_val}",
                        signature=str(enum_val),
                    )
                )
    return symbols


def _extract_json_schema_symbols(content: str) -> List[AstSymbol]:
    """Extracts JSON Schema $defs and enum keys."""
    try:
        data = json.loads(content)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    defs = data.get("$defs") or data.get("definitions") or {}
    symbols = [
        AstSymbol(kind="schema_def", name=k, signature=f"$defs.{k}")
        for k in defs.keys()
    ]
    props = data.get("properties")
    if isinstance(props, dict):
        symbols.extend(_extract_schema_enums(props))
    return symbols


CLASS_DECL_RE = re.compile(
    r"^\s*(?:export\s+|public\s+|abstract\s+|final\s+|sealed\s+)*(?:class|struct|interface|protocol|enum|extension\s+type)\s+([A-Za-z0-9_]+)"
)
FUNC_DECL_RE = re.compile(
    r"^\s*(?:export\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|override\s+)*(?:fun|func|function|fn|def)\s+([A-Za-z0-9_]+)\s*\(([^)]*)\)"
)
METHOD_DECL_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+|Future<[^>]+>\s+|Stream<[^>]+>\s+|[A-Za-z0-9_<>?]+\s+)?([A-Za-z0-9_]+)\s*\(([^)]*)\)\s*(?::\s*[A-Za-z0-9_<>?|\[\]\s]+)?\s*(?:async\s*)?\{"
)
CONTROL_KEYWORDS = {"if", "for", "while", "switch", "catch", "when", "return"}


def _parse_param_names(raw_param_str: str) -> List[str]:
    """Extracts clean parameter names from a comma-separated signature."""
    names: List[str] = []
    for p in raw_param_str.split(","):
        cleaned = p.strip().split("=")[0].strip()
        if ":" in cleaned:
            cleaned = cleaned.split(":")[0].strip()
        elif " " in cleaned:
            cleaned = cleaned.split()[-1].strip()
        if cleaned:
            names.append(cleaned)
    return names


def _match_declaration_line(line: str, line_num: int) -> Optional[AstSymbol]:
    """Matches a single line against multi-language class/function/method declarations."""
    m_cls = CLASS_DECL_RE.match(line)
    if m_cls:
        return AstSymbol(
            kind="class",
            name=m_cls.group(1),
            signature=line.strip(),
            line_start=line_num,
        )

    m_fn = FUNC_DECL_RE.match(line)
    if m_fn:
        return AstSymbol(
            kind="function",
            name=m_fn.group(1),
            signature=line.strip(),
            params=_parse_param_names(m_fn.group(2)),
            line_start=line_num,
        )

    m_meth = METHOD_DECL_RE.match(line)
    if m_meth and m_meth.group(1) not in CONTROL_KEYWORDS:
        return AstSymbol(
            kind="method",
            name=m_meth.group(1),
            signature=line.strip(),
            params=_parse_param_names(m_meth.group(2)),
            line_start=line_num,
        )
    return None


def _extract_regex_symbols(content: str) -> List[AstSymbol]:
    """Language-agnostic declaration extractor for TypeScript, Dart, Swift, Kotlin, Go, Rust."""
    symbols: List[AstSymbol] = []
    for i, line in enumerate(content.splitlines(), start=1):
        sym = _match_declaration_line(line, i)
        if sym:
            symbols.append(sym)
    return symbols


def _try_sem_entities(file_path: Path) -> Optional[List[AstSymbol]]:
    """Queries the `sem` CLI for AST entities if `sem` is installed and file exists."""
    if not shutil.which("sem") or not file_path.exists():
        return None
    code, out, _ = run_cmd(
        ["sem", "entities", str(file_path), "--format", "json"]
    )
    if code != 0:
        return None
    try:
        items = json.loads(out)
        return [
            AstSymbol(
                kind=it.get("type", "symbol"),
                name=it.get("name", ""),
                signature=it.get("signature", ""),
                line_start=it.get("line_start", 1),
                line_end=it.get("line_end", 1),
            )
            for it in items
            if it.get("name")
        ]
    except Exception:
        return None


def extract_ast_symbols(file_path: Path, content: str) -> List[AstSymbol]:
    """Tiered multi-language symbol extractor (`sem` CLI -> stdlib/schema -> multi-lang regex)."""
    sem_syms = _try_sem_entities(file_path)
    if sem_syms:
        return sem_syms

    ext = file_path.suffix.lower()
    if ext == ".py":
        return _extract_py_ast(content)
    if ext in (".json", ".yaml", ".yml"):
        return _extract_json_schema_symbols(content)
    return _extract_regex_symbols(content)


# ----------------------------------------------------------------------
# 3. Rename Mapping & Cross-Directory Move Resolution
# ----------------------------------------------------------------------


@dataclass
class RenameRecord:
    old_path: str
    new_path: str
    similarity: int = 100
    is_symbol_rename: bool = False
    old_symbol: Optional[str] = None
    new_symbol: Optional[str] = None


def _parse_diff_name_status(
    out: str,
) -> Tuple[List[RenameRecord], List[str], List[str]]:
    """Parses `git diff --name-status` output into renames, deleted paths, and added paths."""
    renames: List[RenameRecord] = []
    deleted: List[str] = []
    added: List[str] = []

    for raw in out.splitlines():
        parts = raw.strip().split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            sim = int(status[1:]) if len(status) > 1 else 100
            renames.append(
                RenameRecord(
                    old_path=parts[1], new_path=parts[2], similarity=sim
                )
            )
        elif status == "D":
            deleted.append(parts[1])
        elif status == "A":
            added.append(parts[1])
    return renames, deleted, added


def _pair_by_basename(
    deleted: List[str], added: List[str]
) -> List[RenameRecord]:
    """Pairs high-churn deleted and added files sharing the same basename."""
    by_name: Dict[str, List[str]] = {}
    for a in added:
        by_name.setdefault(Path(a).name, []).append(a)

    paired: List[RenameRecord] = []
    for d in deleted:
        matches = by_name.get(Path(d).name, [])
        if len(matches) == 1:
            paired.append(
                RenameRecord(old_path=d, new_path=matches[0], similarity=30)
            )
    return paired


def build_rename_map(
    repo: Path,
    base: str,
    branch1: str,
    branch2: str,
) -> List[RenameRecord]:
    """Builds a path and symbol rename map across base -> branch1 and base -> branch2."""
    renames: List[RenameRecord] = []
    for branch in (branch1, branch2):
        code, out, _ = run_cmd(
            ["git", "diff", "--find-renames=25%", "--name-status", base, branch],
            cwd=repo,
        )
        if code == 0:
            found, deleted, added = _parse_diff_name_status(out)
            renames.extend(found)
            renames.extend(_pair_by_basename(deleted, added))
    return renames


def merge_moved_file(
    repo: Path,
    old_path: str,
    new_path: str,
    base: str,
    theirs: str,
    stage_rm: bool = True,
) -> Tuple[int, str]:
    """Performs a 3-way merge across relocated paths using `git merge-file`."""
    code_b, base_blob, _ = run_cmd(
        ["git", "cat-file", "-p", f"{base}:{old_path}"], cwd=repo
    )
    code_t, theirs_blob, _ = run_cmd(
        ["git", "cat-file", "-p", f"{theirs}:{old_path}"], cwd=repo
    )
    if code_b != 0 or code_t != 0:
        return 1, f"Failed to read base or theirs blob for {old_path}"

    ours_target = repo / new_path
    if not ours_target.exists():
        return 1, f"New path does not exist on disk: {new_path}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_base = Path(tmp_dir) / "base.tmp"
        tmp_theirs = Path(tmp_dir) / "theirs.tmp"
        tmp_base.write_text(base_blob, encoding="utf-8")
        tmp_theirs.write_text(theirs_blob, encoding="utf-8")

        code_m, out_m, err_m = run_cmd(
            [
                "git",
                "merge-file",
                "-L",
                "ours",
                "-L",
                "base",
                "-L",
                "theirs",
                str(ours_target),
                str(tmp_base),
                str(tmp_theirs),
            ],
            cwd=repo,
        )

    if stage_rm and old_path != new_path:
        if (repo / old_path).exists():
            run_cmd(["git", "rm", "-f", old_path], cwd=repo)
        run_cmd(["git", "add", new_path], cwd=repo)

    return code_m, out_m or err_m


# ----------------------------------------------------------------------
# 4. In-Memory 3-Way Merge & Intent Harvesting
# ----------------------------------------------------------------------


@dataclass
class ConflictedFile:
    path: str
    stage_oids: Dict[int, str] = field(default_factory=dict)
    conflict_type: str = "content"  # content, modify_delete
    tier: str = "TIER_4_INTENT_CLASH"
    culprit_commits_ours: List[str] = field(default_factory=list)
    culprit_commits_theirs: List[str] = field(default_factory=list)
    relocated_path: Optional[str] = None


def _parse_stage_lines(lines: List[str]) -> List[ConflictedFile]:
    """Parses `<mode> <oid> <stage>\\t<path>` lines into ConflictedFile objects."""
    file_stages: Dict[str, Dict[int, str]] = {}
    for raw in lines:
        if "\t" not in raw:
            continue
        meta, path_str = raw.strip().split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3:
            file_stages.setdefault(path_str, {})[int(parts[2])] = parts[1]

    conflicts: List[ConflictedFile] = []
    for path_str, stages in file_stages.items():
        is_mod_del = (2 not in stages) or (3 not in stages)
        ctype = "modify_delete" if is_mod_del else "content"
        conflicts.append(
            ConflictedFile(path=path_str, stage_oids=stages, conflict_type=ctype)
        )
    return conflicts


def run_in_memory_merge(
    repo: Path,
    base: Optional[str],
    ours: str,
    theirs: str,
    subtree: Optional[str] = None,
) -> Tuple[int, str, List[ConflictedFile]]:
    """Executes an in-memory 3-way merge via `git merge-tree --write-tree`."""
    args = ["git", "merge-tree", "--write-tree"]
    if base:
        args.append(f"--merge-base={base}")
    if subtree:
        args.append(f"-Xsubtree={subtree}")
    args.extend([ours, theirs])

    code, out, _ = run_cmd(args, cwd=repo)
    sections = out.split("\n\n", 2)
    stage_lines = sections[0].splitlines()[1:] if sections else []
    conflicts = _parse_stage_lines(stage_lines)

    if not conflicts:
        code_ls, out_ls, _ = run_cmd(["git", "ls-files", "-u"], cwd=repo)
        if code_ls == 0 and out_ls.strip():
            conflicts = _parse_stage_lines(out_ls.strip().splitlines())

    return code, out, conflicts


def harvest_commit_intents(
    repo: Path,
    base: str,
    branch: str,
    file_path: str,
) -> List[Dict[str, str]]:
    """Harvests concise commit messages touching file_path between base and branch."""
    code, out, _ = run_cmd(
        [
            "git",
            "log",
            "--follow",
            "--oneline",
            "--no-merges",
            f"{base}..{branch}",
            "--",
            file_path,
        ],
        cwd=repo,
    )
    if code != 0 or not out.strip():
        return []
    intents: List[Dict[str, str]] = []
    for line in out.strip().splitlines():
        parts = line.strip().split(" ", 1)
        intents.append(
            {"sha": parts[0], "subject": parts[1] if len(parts) > 1 else ""}
        )
    return intents


# ----------------------------------------------------------------------
# 5. Semantic Hazard & Silent Conflict Detection
# ----------------------------------------------------------------------


@dataclass
class SemanticHazard:
    symbol_name: str
    hazard_type: str  # dropped_parameter, renamed_symbol, signature_drift, schema_enum
    source_branch: str
    target_branch: str
    detail: str
    tier: str = "TIER_4_INTENT_CLASH"
    affected_files: List[str] = field(default_factory=list)


def _read_blob(repo: Path, ref: str, path_str: str) -> Optional[str]:
    """Reads file content at ref:path_str."""
    code, out, _ = run_cmd(["git", "cat-file", "-p", f"{ref}:{path_str}"], cwd=repo)
    return out if code == 0 else None


def _compare_file_symbols(
    path_str: str, base_content: str, ours_content: str
) -> List[Tuple[str, str, str]]:
    """Returns (symbol_name, change_kind, detail) for symbols modified/deleted in base->ours."""
    p = Path(path_str)
    base_map = {s.name: s for s in extract_ast_symbols(p, base_content)}
    ours_map = {s.name: s for s in extract_ast_symbols(p, ours_content)}
    changes: List[Tuple[str, str, str]] = []

    for name, b_sym in base_map.items():
        if name not in ours_map:
            if len(base_map) == 1 and len(ours_map) == 1:
                new_name = next(iter(ours_map.keys()))
                changes.append(
                    (
                        name,
                        "renamed_symbol",
                        f"Renamed '{name}' -> '{new_name}' in {path_str}",
                    )
                )
            else:
                changes.append(
                    (name, "deleted_symbol", f"Removed '{name}' in {path_str}")
                )
        else:
            o_sym = ours_map[name]
            if b_sym.params != o_sym.params:
                changes.append(
                    (
                        name,
                        "signature_drift",
                        f"Params changed from {b_sym.params} to {o_sym.params} in {path_str}",
                    )
                )
    return changes


def _collect_file_hazards(
    repo: Path,
    base: str,
    ours: str,
    theirs: str,
    path_str: str,
    added_theirs_lines: str,
) -> List[SemanticHazard]:
    """Checks a single modified file for symbol changes referenced in theirs."""
    b_txt = _read_blob(repo, base, path_str)
    o_txt = _read_blob(repo, ours, path_str)
    if not b_txt or not o_txt:
        return []

    found: List[SemanticHazard] = []
    for sym_name, kind, detail in _compare_file_symbols(path_str, b_txt, o_txt):
        if re.search(rf"\b{re.escape(sym_name)}\b", added_theirs_lines):
            tier = (
                "TIER_1_MECHANICAL"
                if kind == "renamed_symbol"
                else "TIER_4_INTENT_CLASH"
            )
            found.append(
                SemanticHazard(
                    symbol_name=sym_name,
                    hazard_type=kind,
                    source_branch=ours,
                    target_branch=theirs,
                    detail=detail,
                    tier=tier,
                    affected_files=[path_str],
                )
            )
    return found


def detect_semantic_hazards(
    repo: Path,
    base: str,
    ours: str,
    theirs: str,
    rename_map: List[RenameRecord],
) -> List[SemanticHazard]:
    """Detects silent semantic conflicts where signatures/symbols changed on ours while theirs added callers."""
    code, mod_out, _ = run_cmd(
        ["git", "diff", "--name-only", base, ours], cwd=repo
    )
    if code != 0 or not mod_out.strip():
        return []

    _, theirs_diff, _ = run_cmd(["git", "diff", base, theirs], cwd=repo)
    added_theirs_lines = "\n".join(
        line[1:]
        for line in theirs_diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )

    hazards: List[SemanticHazard] = []
    for path_str in mod_out.strip().splitlines():
        hazards.extend(
            _collect_file_hazards(
                repo, base, ours, theirs, path_str, added_theirs_lines
            )
        )
    return hazards


# ----------------------------------------------------------------------
# 6. Conflict Tier Classification & Clustering
# ----------------------------------------------------------------------

LOCKFILE_SUFFIXES = (
    "package-lock.json",
    "yarn.lock",
    "pubspec.lock",
    "poetry.lock",
    "cargo.lock",
)


def classify_conflict_tier(
    conf: ConflictedFile,
    rename_map: List[RenameRecord],
) -> str:
    """Classifies a conflict into Tiers 1-4."""
    if conf.path.lower().endswith(LOCKFILE_SUFFIXES):
        return "TIER_1_MECHANICAL"

    for r in rename_map:
        if conf.path in (r.old_path, r.new_path):
            conf.relocated_path = (
                r.new_path if conf.path == r.old_path else r.old_path
            )
            return "TIER_1_MECHANICAL"

    if conf.conflict_type == "modify_delete":
        return "TIER_1_MECHANICAL"

    return "TIER_4_INTENT_CLASH"


def cluster_intent_clashes(
    conflicts: List[ConflictedFile],
    hazards: List[SemanticHazard],
) -> List[Dict[str, Any]]:
    """Clusters Tier 4 conflicts by shared culprit commit or symbol."""
    clusters: Dict[str, Dict[str, Any]] = {}
    for c in conflicts:
        if c.tier != "TIER_4_INTENT_CLASH":
            continue
        key = "general_logic"
        if c.culprit_commits_theirs:
            key = f"commit_{c.culprit_commits_theirs[0]}"
        elif c.culprit_commits_ours:
            key = f"commit_{c.culprit_commits_ours[0]}"

        clusters.setdefault(
            key,
            {
                "key": key,
                "files": [],
                "description": "Mutually exclusive logic modifications",
            },
        )["files"].append(c.path)

    for h in hazards:
        if h.tier == "TIER_4_INTENT_CLASH":
            key = f"symbol_{h.symbol_name}"
            clusters.setdefault(
                key,
                {
                    "key": key,
                    "files": list(h.affected_files),
                    "description": h.detail,
                },
            )
    return list(clusters.values())


# ----------------------------------------------------------------------
# 7. Cross-Repo Preparation, Patch Porting, Blob Extraction & Verification
# ----------------------------------------------------------------------


def prepare_cross_repo(
    repo: Path,
    source_repo: str,
    source_ref: str,
    remote_name: str = "_merge_source",
) -> Tuple[int, Dict[str, Any]]:
    """Registers/updates a temporary remote, fetches source_ref, and finds merge-base."""
    run_cmd(["git", "remote", "remove", remote_name], cwd=repo)
    c_add, _, err_add = run_cmd(
        ["git", "remote", "add", remote_name, source_repo], cwd=repo
    )
    if c_add != 0:
        return c_add, {"error": err_add.strip()}

    c_fetch, _, err_fetch = run_cmd(
        ["git", "fetch", "--no-tags", remote_name, source_ref], cwd=repo
    )
    if c_fetch != 0:
        return c_fetch, {"error": err_fetch.strip()}

    fetched_ref = f"{remote_name}/{source_ref}"
    base = find_merge_base(repo, "HEAD", fetched_ref)
    squash = detect_squash_merge_ancestry(repo, "HEAD", fetched_ref)
    return 0, {
        "remote": remote_name,
        "fetched_ref": fetched_ref,
        "merge_base": base,
        "squash_ancestry": squash,
    }


def port_patches(
    source_repo: Path,
    target_repo: Path,
    commit_range: str,
    source_subdir: Optional[str] = None,
    target_dir: Optional[str] = None,
    apply_changes: bool = False,
) -> Tuple[int, str]:
    """Ports commits via `git format-patch --full-index` with GIT_ALTERNATE_OBJECT_DIRECTORIES."""
    fp_cmd = ["git", "format-patch", "--full-index", "--stdout", commit_range]
    if source_subdir:
        fp_cmd.extend(["--", source_subdir])
    c_fp, patch_data, err_fp = run_cmd(fp_cmd, cwd=source_repo)
    if c_fp != 0 or not patch_data.strip():
        return 1, err_fp or "No patches generated."

    strip_count = 1 + (len(Path(source_subdir).parts) if source_subdir else 0)
    alt_obj_dir = str((source_repo / ".git" / "objects").resolve())
    env = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": alt_obj_dir}

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(patch_data)
        patch_path = tf.name

    try:
        if apply_changes:
            am_cmd = ["git", "am", "--3way", f"-p{strip_count}"]
            if target_dir:
                am_cmd.append(f"--directory={target_dir}")
            am_cmd.append(patch_path)
            code, out, err = run_cmd(am_cmd, cwd=target_repo, env=env)
            return code, out or err

        # Dry-run check using a temporary GIT_INDEX_FILE
        with tempfile.NamedTemporaryFile(delete=False) as idx_f:
            temp_idx = idx_f.name
        try:
            env["GIT_INDEX_FILE"] = temp_idx
            run_cmd(["git", "read-tree", "HEAD"], cwd=target_repo, env=env)
            chk_cmd = [
                "git",
                "apply",
                "--cached",
                "--3way",
                f"-p{strip_count}",
            ]
            if target_dir:
                chk_cmd.append(f"--directory={target_dir}")
            chk_cmd.append(patch_path)
            code, out, err = run_cmd(chk_cmd, cwd=target_repo, env=env)
            return code, out or err or "Dry-run 3-way patch check passed."
        finally:
            if os.path.exists(temp_idx):
                os.remove(temp_idx)
    finally:
        if os.path.exists(patch_path):
            os.remove(patch_path)


def extract_three_way_blobs(
    repo: Path,
    file_path: str,
    output_dir: Path,
    base: Optional[str] = None,
    ours: Optional[str] = None,
    theirs: Optional[str] = None,
) -> Dict[str, str]:
    """Extracts clean BASE, OURS, THEIRS versions of file_path to output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = file_path.replace("/", "__")
    specs = [
        ("base", f"{base}:{file_path}" if base else f":1:{file_path}"),
        ("ours", f"{ours}:{file_path}" if ours else f":2:{file_path}"),
        ("theirs", f"{theirs}:{file_path}" if theirs else f":3:{file_path}"),
    ]
    written: Dict[str, str] = {}
    for label, spec in specs:
        code, content, _ = run_cmd(["git", "cat-file", "-p", spec], cwd=repo)
        if code == 0:
            dest = output_dir / f"{safe_name}.{label}"
            dest.write_text(content, encoding="utf-8")
            written[label] = str(dest)
    return written


def verify_merge_state(repo: Path) -> Tuple[bool, List[str]]:
    """Checks repository for leftover conflict markers, unmerged index stages, or temp remotes."""
    issues: List[str] = []
    code_idx, out_idx, _ = run_cmd(["git", "ls-files", "-u"], cwd=repo)
    if code_idx == 0 and out_idx.strip():
        issues.append(
            f"Unmerged index stages exist for {len(out_idx.strip().splitlines())} file(s)."
        )

    code_rg, out_rg, _ = run_cmd(
        ["git", "grep", "-n", "-E", "^(<<<<<<<|=======|>>>>>>>)"], cwd=repo
    )
    if code_rg == 0 and out_rg.strip():
        for line in out_rg.strip().splitlines()[:5]:
            issues.append(f"Leftover conflict marker: {line}")

    code_rem, out_rem, _ = run_cmd(["git", "remote"], cwd=repo)
    if code_rem == 0 and out_rem.strip():
        for rem in out_rem.strip().splitlines():
            if rem.startswith(("_temp_", "_merge_source")):
                issues.append(f"Lingering temporary remote found: '{rem}'")

    return len(issues) == 0, issues


# ----------------------------------------------------------------------
# CLI Subcommands & Main Dispatcher
# ----------------------------------------------------------------------


def _populate_conflict_details(
    repo: Path,
    base_ref: str,
    ours: str,
    theirs: str,
    conflicts: List[ConflictedFile],
    rename_map: List[RenameRecord],
) -> None:
    """Enriches each conflicted file with tier classification and commit history."""
    for c in conflicts:
        c.tier = classify_conflict_tier(c, rename_map)
        c.culprit_commits_ours = [
            x["sha"]
            for x in harvest_commit_intents(repo, base_ref, ours, c.path)
        ]
        c.culprit_commits_theirs = [
            x["sha"]
            for x in harvest_commit_intents(repo, base_ref, theirs, c.path)
        ]


def cmd_analyze(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    base = args.base or find_merge_base(repo, args.ours, args.theirs, args.subtree)
    squash_info = detect_squash_merge_ancestry(repo, args.ours, args.theirs)
    if squash_info and args.record_ancestry:
        record_squash_ancestry(
            repo, squash_info["prior_theirs_sha"], squash_info["subject"]
        )
        base = squash_info["prior_theirs_sha"]

    base_ref = base or args.ours
    rename_map = build_rename_map(repo, base_ref, args.ours, args.theirs)
    merge_code, _, conflicts = run_in_memory_merge(
        repo, base, args.ours, args.theirs, args.subtree
    )
    _populate_conflict_details(
        repo, base_ref, args.ours, args.theirs, conflicts, rename_map
    )
    hazards = detect_semantic_hazards(
        repo, base_ref, args.ours, args.theirs, rename_map
    )
    tier4_clusters = cluster_intent_clashes(conflicts, hazards)

    report = {
        "repo": str(repo),
        "base": base,
        "ours": args.ours,
        "theirs": args.theirs,
        "clean_merge": merge_code == 0 and len(conflicts) == 0,
        "squash_merge_detected": squash_info,
        "rename_map": [asdict(r) for r in rename_map],
        "conflicts_count": len(conflicts),
        "conflicts": [asdict(c) for c in conflicts],
        "semantic_hazards": [asdict(h) for h in hazards],
        "tier4_clusters": tier4_clusters,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"Merge Base: {base} | Conflicts: {len(conflicts)} | Hazards: {len(hazards)}"
        )
    return 0 if merge_code == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Intent-driven Git merge and conflict assistant."
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_ana = sub.add_parser("analyze", help="Run in-memory 3-way merge and intent scan")
    p_ana.add_argument("--repo", default=".")
    p_ana.add_argument("--ours", default="HEAD")
    p_ana.add_argument("--theirs", required=True)
    p_ana.add_argument("--base", default=None)
    p_ana.add_argument("--subtree", default=None)
    p_ana.add_argument("--record-ancestry", action="store_true")
    p_ana.add_argument("--json", action="store_true")

    p_mov = sub.add_parser(
        "merge-moved-file",
        help="3-way merge across relocated old_path and new_path via git merge-file",
    )
    p_mov.add_argument("--repo", default=".")
    p_mov.add_argument("--old-path", required=True)
    p_mov.add_argument("--new-path", required=True)
    p_mov.add_argument("--base", required=True)
    p_mov.add_argument("--theirs", required=True)
    p_mov.add_argument("--stage-rm", action="store_true", default=True)

    p_prep = sub.add_parser(
        "prepare-cross-repo", help="Set up ephemeral remote and detect merge base"
    )
    p_prep.add_argument("--repo", default=".")
    p_prep.add_argument("--source-repo", required=True)
    p_prep.add_argument("--source-ref", default="main")
    p_prep.add_argument("--subtree", default=None)

    p_port = sub.add_parser(
        "port-patches", help="Port commits via git format-patch / git am --3way"
    )
    p_port.add_argument("--repo", default=".")
    p_port.add_argument("--source-repo", required=True)
    p_port.add_argument("--range", required=True)
    p_port.add_argument("--source-subdir", default=None)
    p_port.add_argument("--target-dir", default=None)
    p_port.add_argument("--apply", action="store_true")

    p_ext = sub.add_parser(
        "extract-blobs", help="Extract BASE, OURS, THEIRS blobs for inspection"
    )
    p_ext.add_argument("--repo", default=".")
    p_ext.add_argument("--file", required=True)
    p_ext.add_argument("--output-dir", required=True)
    p_ext.add_argument("--base", default=None)
    p_ext.add_argument("--ours", default=None)
    p_ext.add_argument("--theirs", default=None)

    p_ver = sub.add_parser("verify", help="Verify clean post-merge repository state")
    p_ver.add_argument("--repo", default=".")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()

    if args.subcommand == "analyze":
        return cmd_analyze(args)
    if args.subcommand == "merge-moved-file":
        code, msg = merge_moved_file(
            repo, args.old_path, args.new_path, args.base, args.theirs, args.stage_rm
        )
        print(msg or f"Merged {args.old_path} -> {args.new_path}")
        return code
    if args.subcommand == "prepare-cross-repo":
        code, info = prepare_cross_repo(repo, args.source_repo, args.source_ref)
        print(json.dumps(info, indent=2))
        return code
    if args.subcommand == "port-patches":
        code, msg = port_patches(
            Path(args.source_repo).resolve(),
            repo,
            args.range,
            args.source_subdir,
            args.target_dir,
            args.apply,
        )
        print(msg)
        return code
    if args.subcommand == "extract-blobs":
        written = extract_three_way_blobs(
            repo,
            args.file,
            Path(args.output_dir).resolve(),
            args.base,
            args.ours,
            args.theirs,
        )
        print(json.dumps(written, indent=2))
        return 0 if written else 1

    clean, issues = verify_merge_state(repo)
    print("✓ Clean" if clean else "\n".join(issues))
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
