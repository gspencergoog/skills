"""Local git diff-tree commit, AST symbol, and Markdown section indexer for packrat.

Indexes the canonical main repository branch (preferring upstream/<default_branch>
or the remote matching repo.full_name over origin/<default_branch>) using zero
GitHub API calls, reading blob contents directly from <target_sha>:<file_path>.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from db_store import PackratStore
from models import (
    CodeSymbolRecord,
    CommitFileRecord,
    CommitRecord,
    DocSectionRecord,
    RepoConfig,
    SyncStats,
    TextChunk,
    compute_content_sha256,
    utc_now_iso,
)
from progress import NULL_REPORTER, ProgressReporter

CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".py",
        ".dart",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
    }
)
MARKDOWN_EXTENSIONS: frozenset[str] = frozenset({".md", ".mdx"})
_IGNORED_PATH_PARTS: frozenset[str] = frozenset(
    {
        "node_modules",
        "dist",
        "build",
        ".dart_tool",
        ".next",
        "coverage",
        "__pycache__",
        "vendor",
        "third_party",
    }
)
_PR_NUMBER_IN_SUBJECT_RE = re.compile(r"\(#(\d+)\)\s*$")

def _find_context_indexer_dir() -> Path:
    """Locates the companion context-indexer skill scripts directory."""
    sibling = Path(__file__).resolve().parents[2] / "context-indexer" / "scripts"
    candidates = [
        sibling,
        Path.home() / ".gemini" / "config" / "skills" / "context-indexer" / "scripts",
        Path.home() / ".gemini" / "skills" / "context-indexer" / "scripts",
        Path.home() / ".claude" / "skills" / "context-indexer" / "scripts",
        Path.home() / ".agents" / "skills" / "context-indexer" / "scripts",
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return sibling


_CACHED_MODULES: dict[str, ModuleType] = {}


def _load_context_indexer_module(script_name: str) -> ModuleType:
    """Dynamically loads stage_diffs.py or stage_markdown.py from context-indexer."""
    if script_name in _CACHED_MODULES:
        return _CACHED_MODULES[script_name]
    indexer_dir = _find_context_indexer_dir()
    script_path = indexer_dir / f"{script_name}.py"
    if not script_path.is_file():
        raise ImportError(
            f"Missing companion skill script '{script_path}'. "
            "Install the 'context-indexer' skill alongside 'packrat'."
        )
    mod_name = f"context_indexer_{script_name}"
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {script_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    _CACHED_MODULES[script_name] = mod
    return mod


def run_git_command(
    repo_dir: Path,
    args: Sequence[str],
    timeout: int = 20,
) -> subprocess.CompletedProcess[str]:
    """Executes git in repo_dir with pagers and interactive prompts disabled."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "PAGER": "cat",
    }
    return subprocess.run(
        ["git", "-C", str(repo_dir), "--no-pager", *args],
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
        timeout=timeout,
        check=False,
    )


def _parse_remotes(repo_dir: Path) -> dict[str, str]:
    """Returns a mapping of remote name to fetch URL for repo_dir."""
    res = run_git_command(repo_dir, ["remote", "-v"])
    if res.returncode != 0:
        return {}
    remotes: dict[str, str] = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in remotes:
            remotes[parts[0]] = parts[1]
    return remotes


def _candidate_canonical_refs(repo_dir: Path, repo: RepoConfig) -> list[str]:
    """Builds ordered candidate refs prioritizing the canonical upstream repository."""
    branch = repo.default_branch or "main"
    remotes = _parse_remotes(repo_dir)
    candidates: list[str] = []

    target_slug = repo.full_name.lower()
    for rem_name, url in remotes.items():
        if target_slug in url.lower():
            candidates.append(f"{rem_name}/{branch}")

    for rem_name in ("upstream", "origin"):
        ref = f"{rem_name}/{branch}"
        if ref not in candidates:
            candidates.append(ref)

    candidates.extend([branch, "HEAD"])
    return candidates


def _fetch_remote_ref(
    repo_dir: Path,
    repo_full_name: str,
    remote_spec: str,
    reporter: ProgressReporter,
    stats: SyncStats | None,
) -> None:
    """Runs git fetch on the candidate remote branch and records a warning if it fails."""
    rem_name, rem_branch = remote_spec.split("/", 1)
    try:
        res = run_git_command(repo_dir, ["fetch", "--quiet", rem_name, rem_branch], timeout=15)
    except subprocess.TimeoutExpired:
        msg = f"git fetch {rem_name}/{rem_branch} timed out; indexing local ref"
        if stats is not None:
            stats.warnings.append(msg)
        reporter.warn(repo_full_name, msg)
        return
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or f"exit {res.returncode}").strip().splitlines()[0]
        msg = f"git fetch {rem_name}/{rem_branch} failed ({detail}); indexing local ref"
        if stats is not None:
            stats.warnings.append(msg)
        reporter.warn(repo_full_name, msg)


def resolve_canonical_ref(
    repo_dir: Path,
    repo: RepoConfig,
    fetch: bool = False,
    *,
    reporter: ProgressReporter = NULL_REPORTER,
    stats: SyncStats | None = None,
) -> tuple[str, str] | None:
    """Resolves the canonical main-repository ref and its commit SHA."""
    if not repo_dir.exists() or not (repo_dir / ".git").exists():
        return None
    candidates = _candidate_canonical_refs(repo_dir, repo)
    if fetch and candidates and "/" in candidates[0]:
        _fetch_remote_ref(repo_dir, repo.full_name, candidates[0], reporter, stats)

    for ref in candidates:
        res = run_git_command(repo_dir, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
        if res.returncode == 0 and res.stdout.strip():
            return ref, res.stdout.strip().splitlines()[0]
    return None


def _is_indexable_path(file_path: str) -> bool:
    """Returns True if file_path is a non-vendored source or Markdown file."""
    path_obj = Path(file_path)
    if any(part in _IGNORED_PATH_PARTS for part in path_obj.parts):
        return False
    ext = path_obj.suffix.lower()
    return ext in CODE_EXTENSIONS or ext in MARKDOWN_EXTENSIONS


def _commit_exists(repo_dir: Path, sha: str | None) -> bool:
    """Returns True if sha resolves to a valid commit object in repo_dir."""
    if not sha:
        return False
    res = run_git_command(repo_dir, ["cat-file", "-t", sha])
    return res.returncode == 0 and res.stdout.strip() == "commit"


def _parse_diff_tree_line(line: str, modified: set[str], deleted: set[str]) -> None:
    """Parses one `git diff-tree --name-status` line into modified and deleted sets."""
    parts = line.split("\t")
    if len(parts) < 2:
        return
    status = parts[0].strip()
    if status.startswith("R") and len(parts) >= 3:
        old_p, new_p = parts[1].strip(), parts[2].strip()
        if _is_indexable_path(old_p):
            deleted.add(old_p)
        if _is_indexable_path(new_p):
            modified.add(new_p)
        return
    path_str = parts[1].strip()
    if not _is_indexable_path(path_str):
        return
    if status.startswith("D"):
        deleted.add(path_str)
    else:
        modified.add(path_str)


def list_changed_files(
    repo_dir: Path,
    old_sha: str | None,
    new_sha: str,
    force: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Lists indexable (modified_or_added, deleted) files between old_sha and new_sha."""
    if not force and old_sha == new_sha:
        return (), ()

    if force or not _commit_exists(repo_dir, old_sha):
        res = run_git_command(repo_dir, ["ls-tree", "-r", "--name-only", new_sha], timeout=30)
        if res.returncode != 0:
            return (), ()
        all_files = sorted(
            line.strip()
            for line in res.stdout.splitlines()
            if line.strip() and _is_indexable_path(line.strip())
        )
        return tuple(all_files), ()

    assert old_sha is not None
    res = run_git_command(
        repo_dir,
        ["diff-tree", "-r", "--no-commit-id", "--name-status", "-M", old_sha, new_sha],
        timeout=30,
    )
    if res.returncode != 0:
        return (), ()

    modified: set[str] = set()
    deleted: set[str] = set()
    for line in res.stdout.splitlines():
        if line.strip():
            _parse_diff_tree_line(line, modified, deleted)
    deleted -= modified
    return tuple(sorted(modified)), tuple(sorted(deleted))


_NUMSTAT_BRACE_RENAME_RE = re.compile(r"^(.*?)\{(.*?) => (.*?)\}(.*)$")


def _normalize_numstat_path(raw_path: str) -> tuple[str, str | None, str]:
    """Normalizes git log --numstat paths, extracting (file_path, old_path, change_type)."""
    brace_match = _NUMSTAT_BRACE_RENAME_RE.match(raw_path)
    if brace_match:
        prefix, old_part, new_part, suffix = brace_match.groups()
        old_p = re.sub(r"/+", "/", f"{prefix}{old_part}{suffix}").strip("/")
        new_p = re.sub(r"/+", "/", f"{prefix}{new_part}{suffix}").strip("/")
        return new_p, old_p, "R"
    if " => " in raw_path:
        old_p, new_p = raw_path.split(" => ", 1)
        return new_p.strip(), old_p.strip(), "R"
    return raw_path, None, "M"


def _parse_numstat_lines(
    repo_id: int,
    sha: str,
    lines: Sequence[str],
) -> tuple[CommitFileRecord, ...]:
    """Parses git log --numstat file lines for a single commit."""
    files: list[CommitFileRecord] = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_str, del_str, raw_path = parts[0], parts[1], parts[2].strip()
        if not raw_path:
            continue
        file_path, old_path, change_type = _normalize_numstat_path(raw_path)
        additions = int(add_str) if add_str.isdigit() else 0
        deletions = int(del_str) if del_str.isdigit() else 0
        files.append(
            CommitFileRecord(
                repo_id=repo_id,
                sha=sha,
                file_path=file_path,
                change_type=change_type,
                old_path=old_path,
                additions=additions,
                deletions=deletions,
            )
        )
    return tuple(files)


def _parse_single_commit_block(
    repo: RepoConfig,
    block: str,
) -> tuple[CommitRecord, TextChunk] | None:
    """Parses one formatted git log commit block into CommitRecord and TextChunk."""
    if "\x00__FILES__" not in block:
        return None
    header_part, files_part = block.split("\x00__FILES__", 1)
    fields = header_part.strip("\n").split("\x00")
    if len(fields) < 6:
        return None
    sha, author_name, author_email, committed_at, subject, body = (
        fields[0].strip(),
        fields[1].strip(),
        fields[2].strip(),
        fields[3].strip(),
        fields[4].strip(),
        fields[5].strip(),
    )
    if not sha:
        return None

    pr_match = _PR_NUMBER_IN_SUBJECT_RE.search(subject)
    pr_number = int(pr_match.group(1)) if pr_match else None
    file_lines = [ln for ln in files_part.splitlines() if ln.strip()]
    files = _parse_numstat_lines(repo.repo_id, sha, file_lines)
    full_msg = f"{subject}\n\n{body}".strip() if body else subject

    commit = CommitRecord(
        repo_id=repo.repo_id,
        sha=sha,
        author_name=author_name or None,
        author_email=author_email or None,
        committed_at=committed_at or utc_now_iso(),
        title=subject,
        message=full_msg,
        pr_number=pr_number,
        files=files,
    )
    touched_preview = ", ".join(f.file_path for f in files[:15])
    chunk_text = f"{full_msg}\n\nFiles: {touched_preview}".strip() if touched_preview else full_msg
    chunk = TextChunk(
        chunk_id=f"{repo.full_name}@commit:{sha[:12]}",
        repo_id=repo.repo_id,
        source_kind="commit",
        source_ref=f"{repo.full_name}@{sha[:10]}",
        title=f"Commit {sha[:10]}: {subject}",
        text=chunk_text,
        content_sha256=compute_content_sha256(subject, chunk_text),
        is_untrusted=False,
        item_number=pr_number,
        updated_at=committed_at or utc_now_iso(),
    )
    return commit, chunk


def index_commits(
    repo_dir: Path,
    repo: RepoConfig,
    old_sha: str | None,
    new_sha: str,
    max_commits: int = 300,
) -> tuple[list[CommitRecord], list[TextChunk]]:
    """Extracts CommitRecord and TextChunk entries between old_sha and new_sha."""
    if old_sha == new_sha:
        return [], []
    range_arg = f"{old_sha}..{new_sha}" if _commit_exists(repo_dir, old_sha) else new_sha
    fmt = "__COMMIT__%x00%H%x00%an%x00%ae%x00%cI%x00%s%x00%b%x00__FILES__"
    res = run_git_command(
        repo_dir,
        [
            "log",
            "--no-merges",
            f"-n{max_commits}",
            f"--pretty=format:{fmt}",
            "--numstat",
            range_arg,
        ],
        timeout=30,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return [], []

    commits: list[CommitRecord] = []
    chunks: list[TextChunk] = []
    for raw_block in res.stdout.split("__COMMIT__\x00"):
        if not raw_block.strip():
            continue
        parsed = _parse_single_commit_block(repo, raw_block)
        if parsed is not None:
            commits.append(parsed[0])
            chunks.append(parsed[1])
    return commits, chunks


def _read_blob_at_sha(repo_dir: Path, sha: str, file_path: str) -> str | None:
    """Reads `<sha>:<file_path>` from git object storage without touching worktree files."""
    res = run_git_command(repo_dir, ["show", f"{sha}:{file_path}"], timeout=15)
    if res.returncode != 0:
        return None
    if "\x00" in res.stdout[:4096]:
        return None
    return res.stdout


def _extract_markdown_sections_from_blob(
    repo: RepoConfig,
    sha: str,
    file_path: str,
    blob_text: str,
) -> tuple[list[DocSectionRecord], list[TextChunk]]:
    """Parses Markdown sections in memory using context-indexer's stage_markdown."""
    stage_md = _load_context_indexer_module("stage_markdown")
    raw_lines = blob_text.splitlines()
    records = [
        stage_md.LineRecord(line_num=i + 1, raw=ln)
        for i, ln in enumerate(raw_lines)
    ]
    stage_md._mark_frontmatter(records)
    stage_md._mark_fences(records)
    stage_md._mark_headings_and_sections(records)
    raw_sections = stage_md._build_sections(file_path, records)

    now = utc_now_iso()
    sections: list[DocSectionRecord] = []
    chunks: list[TextChunk] = []
    for sec in raw_sections:
        l_start = int(sec["line_start"])
        l_end = int(sec["line_end"])
        sub_end = int(sec["subtree_line_end"])
        body_text = "\n".join(raw_lines[max(0, l_start - 1) : min(len(raw_lines), l_end)]).strip()
        heading = str(sec["heading"])
        breadcrumb = str(sec["breadcrumb"])
        sec_id = int(sec["section_id"])
        sections.append(
            DocSectionRecord(
                repo_id=repo.repo_id,
                file_path=file_path,
                section_id=sec_id,
                level=int(sec["level"]),
                heading=heading,
                breadcrumb=breadcrumb,
                anchor_slug=str(sec["anchor_slug"]),
                line_start=l_start,
                line_end=l_end,
                subtree_line_end=sub_end,
                content=body_text[:4000],
                sha=sha,
            )
        )
        if body_text:
            c_title = f"{file_path}: {breadcrumb}"
            chunks.append(
                TextChunk(
                    chunk_id=f"{repo.full_name}@doc:{file_path}:{sec_id}",
                    repo_id=repo.repo_id,
                    source_kind="doc_section",
                    source_ref=f"{file_path}#L{l_start}-L{l_end}",
                    file_path=file_path,
                    title=c_title,
                    text=body_text[:3000],
                    content_sha256=compute_content_sha256(c_title, body_text[:3000]),
                    is_untrusted=False,
                    updated_at=now,
                )
            )
    return sections, chunks


def _extract_code_symbols_from_blob(
    repo: RepoConfig,
    sha: str,
    file_path: str,
    blob_text: str,
) -> tuple[list[CodeSymbolRecord], list[TextChunk]]:
    """Extracts AST symbol spans in memory using context-indexer's stage_diffs."""
    stage_diffs = _load_context_indexer_module("stage_diffs")
    entities = stage_diffs._extract_content_entities(file_path, "NEW", blob_text, full_path=None)
    raw_lines = blob_text.splitlines()
    now = utc_now_iso()

    seen_keys: set[tuple[str, int]] = set()
    symbols: list[CodeSymbolRecord] = []
    chunks: list[TextChunk] = []
    for ent in entities:
        sym_name = str(ent.get("name") or ent.get("entity_name") or "").strip()
        l_start = int(ent.get("line_start") or ent.get("start_line") or 0)
        l_end = int(ent.get("line_end") or ent.get("end_line") or l_start)
        if not sym_name or l_start <= 0 or (sym_name, l_start) in seen_keys:
            continue
        seen_keys.add((sym_name, l_start))
        kind = str(ent.get("kind") or ent.get("entity_kind") or "symbol")
        signature = str(ent.get("signature") or sym_name)
        snippet_lines = raw_lines[max(0, l_start - 1) : min(len(raw_lines), min(l_end, l_start + 35))]
        body_preview = "\n".join(snippet_lines).strip()

        symbols.append(
            CodeSymbolRecord(
                repo_id=repo.repo_id,
                file_path=file_path,
                symbol_name=sym_name,
                kind=kind,
                line_start=l_start,
                line_end=l_end,
                signature=signature,
                docstring="",
                sha=sha,
            )
        )
        c_title = f"{kind} {sym_name} in {file_path}"
        c_text = f"{signature}\n\n{body_preview}".strip()
        chunks.append(
            TextChunk(
                chunk_id=f"{repo.full_name}@symbol:{file_path}:{sym_name}:{l_start}",
                repo_id=repo.repo_id,
                source_kind="code_symbol",
                source_ref=f"{file_path}#L{l_start}-L{l_end}",
                file_path=file_path,
                title=c_title,
                text=c_text[:2500],
                content_sha256=compute_content_sha256(c_title, c_text[:2500]),
                is_untrusted=False,
                updated_at=now,
            )
        )
    return symbols, chunks


def index_files_at_sha(
    repo_dir: Path,
    repo: RepoConfig,
    target_sha: str,
    modified_files: Sequence[str],
    *,
    reporter: ProgressReporter = NULL_REPORTER,
) -> tuple[list[CodeSymbolRecord], list[DocSectionRecord], list[TextChunk]]:
    """Extracts symbols, Markdown sections, and chunks for modified_files at target_sha."""
    all_symbols: list[CodeSymbolRecord] = []
    all_sections: list[DocSectionRecord] = []
    all_chunks: list[TextChunk] = []
    total_files = len(modified_files)
    reporter.start_phase(repo.full_name, "git files", total=total_files)

    for idx, file_path in enumerate(modified_files, start=1):
        blob = _read_blob_at_sha(repo_dir, target_sha, file_path)
        if not blob:
            reporter.advance(idx, total_files, detail=file_path)
            continue
        ext = Path(file_path).suffix.lower()
        if ext in MARKDOWN_EXTENSIONS:
            secs, chs = _extract_markdown_sections_from_blob(repo, target_sha, file_path, blob)
            all_sections.extend(secs)
            all_chunks.extend(chs)
        elif ext in CODE_EXTENSIONS:
            syms, chs = _extract_code_symbols_from_blob(repo, target_sha, file_path, blob)
            all_symbols.extend(syms)
            all_chunks.extend(chs)
        reporter.advance(idx, total_files, detail=file_path)
    reporter.finish_phase()
    return all_symbols, all_sections, all_chunks


def sync_git_codebase(
    store: PackratStore,
    repo: RepoConfig,
    force: bool = False,
    fetch: bool = False,
    max_commits: int = 300,
    reporter: ProgressReporter = NULL_REPORTER,
) -> SyncStats:
    """Incrementally indexes canonical git commits, AST symbols, and Markdown sections."""
    stats = SyncStats(repo_full_name=repo.full_name)
    if not repo.local_path:
        stats.skipped_reason = "no_local_path"
        return stats

    repo_dir = Path(repo.local_path).expanduser()
    resolved = resolve_canonical_ref(repo_dir, repo, fetch=fetch, reporter=reporter, stats=stats)
    if resolved is None:
        stats.skipped_reason = f"missing_git_repo:{repo_dir}"
        return stats

    ref_name, target_sha = resolved
    stats.indexed_ref = ref_name
    stats.indexed_sha = target_sha

    old_sha = None if force else repo.last_indexed_sha
    if old_sha == target_sha and not force:
        return stats

    commits, commit_chunks = index_commits(repo_dir, repo, old_sha, target_sha, max_commits=max_commits)
    if commits:
        store.upsert_commits(commits)
        store.upsert_chunks(commit_chunks)

    modified_files, deleted_files = list_changed_files(repo_dir, old_sha, target_sha, force=force)
    if (force or not _commit_exists(repo_dir, old_sha)) and modified_files:
        indexed_rows = store.conn.execute(
            """
            SELECT file_path FROM code_symbols WHERE repo_id = ?
            UNION
            SELECT file_path FROM doc_sections WHERE repo_id = ?
            UNION
            SELECT file_path FROM chunks
            WHERE repo_id = ? AND source_kind IN ('symbol', 'doc_section') AND file_path IS NOT NULL
            """,
            (repo.repo_id, repo.repo_id, repo.repo_id),
        ).fetchall()
        live_set = set(modified_files)
        deleted_files = tuple(
            sorted(str(r["file_path"]) for r in indexed_rows if str(r["file_path"]) not in live_set)
        )
    symbols, sections, file_chunks = index_files_at_sha(
        repo_dir, repo, target_sha, modified_files, reporter=reporter
    )
    if modified_files or deleted_files:
        store.replace_code_index_for_files(
            repo_id=repo.repo_id,
            changed_paths=modified_files,
            deleted_paths=deleted_files,
            symbols=symbols,
            doc_sections=sections,
            chunks=file_chunks,
        )

    store.update_repo_sync_state(repo.repo_id, last_indexed_sha=target_sha)
    stats.commits_upserted = len(commits)
    stats.symbols_upserted = len(symbols)
    stats.doc_sections_upserted = len(sections)
    stats.chunks_upserted = len(commit_chunks) + len(file_chunks)
    return stats
