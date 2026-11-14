"""Local git diff-tree commit, AST symbol, and Markdown section indexer for packrat.

Indexes the canonical main repository branch (preferring upstream/<default_branch>
or the remote matching repo.full_name over origin/<default_branch>) using zero
GitHub API calls, reading blob contents directly from <target_sha>:<file_path>.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

try:
    from .blob_parsers import parse_markdown_sections
    from .git_cmd import (
        _build_git_ssh_env,  # noqa: F401
        _extract_remote_host,  # noqa: F401
        build_git_ssh_env,
        run_git_command,
    )
except ImportError:
    from blob_parsers import parse_markdown_sections  # type: ignore[no-redef]
    from git_cmd import (  # type: ignore[no-redef]
        _build_git_ssh_env,  # noqa: F401
        _extract_remote_host,  # noqa: F401
        build_git_ssh_env,
        run_git_command,
    )

from dataclasses import dataclass

from db_store import PackratStore
from mentions import caching_resolver, extract_item_mentions
from models import (
    CODE_EXTENSIONS,
    MARKDOWN_EXTENSIONS,
    CodeSymbolRecord,
    CommitFileRecord,
    CommitRecord,
    DocSectionRecord,
    RepoConfig,
    SyncStats,
    TextChunk,
    compute_content_sha256,
    normalize_utc_iso,
    utc_now_iso,
)
from progress import NULL_REPORTER, ProgressReporter
from symbol_parsers import (
    MARKDOWN_PARSER_ID,
    REGEX_PARSER_ID,
    ROUTING,
    ParsedFile,
    SymbolParsers,
    split_lines,
)

MAX_INDEXED_BLOB_BYTES = 2 * 1024 * 1024
_BLOB_BATCH_SIZE = 250
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


def _is_git_repository(repo_dir: Path) -> bool:
    """Returns True when `repo_dir` is an existing git checkout or bare repository."""
    if not repo_dir.is_dir():
        return False
    return (
        run_git_command(repo_dir, ["rev-parse", "--git-dir"], timeout=10).returncode
        == 0
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


def _candidate_canonical_refs(
    repo_dir: Path,
    repo: RepoConfig,
    remotes: dict[str, str] | None = None,
) -> list[str]:
    """Builds ordered candidate refs prioritizing the canonical upstream repository."""
    branch = repo.default_branch or "main"
    remote_map = _parse_remotes(repo_dir) if remotes is None else remotes
    candidates: list[str] = []

    target_slug = repo.full_name.lower()
    for rem_name, url in remote_map.items():
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
    remote_url: str | None = None,
) -> None:
    """Runs git fetch on the candidate remote branch and records a warning if it fails."""
    rem_name, rem_branch = remote_spec.split("/", 1)
    resolved_url = (
        _parse_remotes(repo_dir).get(rem_name) if remote_url is None else remote_url
    )
    extra_env = build_git_ssh_env(resolved_url)
    try:
        res = run_git_command(
            repo_dir,
            ["fetch", "--quiet", rem_name, rem_branch],
            timeout=15,
            extra_env=extra_env,
        )
    except subprocess.TimeoutExpired:
        msg = f"git fetch {rem_name}/{rem_branch} timed out; indexing local ref"
        if stats is not None:
            stats.warnings.append(msg)
        reporter.warn(repo_full_name, msg)
        return
    if res.returncode != 0:
        detail = (
            res.stderr.strip() or res.stdout.strip() or f"exit {res.returncode}"
        ).splitlines()[0]
        msg = f"git fetch {rem_name}/{rem_branch} failed ({detail}); indexing local ref"
        if stats is not None:
            stats.warnings.append(msg)
        reporter.warn(repo_full_name, msg)


def _resolve_ref_commit(repo_dir: Path, ref: str) -> str | None:
    """Resolves `ref` to its commit SHA in `repo_dir`, or None if missing."""
    res = run_git_command(repo_dir, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().splitlines()[0]
    return None


def resolve_canonical_ref(
    repo_dir: Path,
    repo: RepoConfig,
    fetch: bool = False,
    *,
    reporter: ProgressReporter = NULL_REPORTER,
    stats: SyncStats | None = None,
) -> tuple[str, str] | None:
    """Resolves the canonical main-repository ref and its commit SHA."""
    if not _is_git_repository(repo_dir):
        return None
    if repo.git_source == "managed":
        branch = repo.default_branch or "main"
        ref = f"refs/heads/{branch}"
        sha = _resolve_ref_commit(repo_dir, ref)
        return (ref, sha) if sha else None

    remotes = _parse_remotes(repo_dir)
    candidates = _candidate_canonical_refs(repo_dir, repo, remotes=remotes)
    if fetch and candidates and "/" in candidates[0]:
        rem_name = candidates[0].split("/", 1)[0]
        _fetch_remote_ref(
            repo_dir,
            repo.full_name,
            candidates[0],
            reporter,
            stats,
            remote_url=remotes.get(rem_name, ""),
        )

    for ref in candidates:
        sha = _resolve_ref_commit(repo_dir, ref)
        if sha:
            return ref, sha
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


def _ls_tree_blobs(repo_dir: Path, sha: str) -> dict[str, str]:
    """Returns `{file_path: blob_oid}` for all indexable blobs at `sha` via `git ls-tree`."""
    res = run_git_command(repo_dir, ["ls-tree", "-r", "-z", sha], timeout=30)
    if res.returncode != 0:
        return {}
    blobs: dict[str, str] = {}
    for entry in res.stdout.split("\x00"):
        if "\t" not in entry:
            continue
        meta, file_path = entry.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob" and _is_indexable_path(file_path):
            blobs[file_path] = parts[2]
    return blobs


def _record_diff_tree_entry(
    status: str,
    new_mode: str,
    new_oid: str,
    first_path: str,
    second_path: str | None,
    modified: dict[str, str],
    deleted: set[str],
) -> None:
    """Records one parsed `git diff-tree` entry into `modified` (`path -> oid`) and `deleted`."""
    if second_path is not None:
        if status.startswith("R") and _is_indexable_path(first_path):
            deleted.add(first_path)
        if new_mode != "160000" and _is_indexable_path(second_path):
            modified[second_path] = new_oid
        return
    if not _is_indexable_path(first_path):
        return
    if status.startswith("D"):
        deleted.add(first_path)
    elif new_mode != "160000":
        modified[first_path] = new_oid


def _diff_tree_blobs(
    repo_dir: Path, old_sha: str, new_sha: str
) -> tuple[dict[str, str], set[str]]:
    """Returns `(modified_blob_oids, deleted_paths)` between `old_sha` and `new_sha`."""
    res = run_git_command(
        repo_dir,
        ["diff-tree", "-r", "-z", "--no-commit-id", "-M", old_sha, new_sha],
        timeout=30,
    )
    if res.returncode != 0:
        return {}, set()
    tokens = [t for t in res.stdout.split("\x00") if t]
    modified: dict[str, str] = {}
    deleted: set[str] = set()
    idx = 0
    while idx < len(tokens):
        parts = tokens[idx].lstrip(":").split()
        idx += 1
        if len(parts) < 5 or idx >= len(tokens):
            break
        new_mode, new_oid, status = parts[1], parts[3], parts[4]
        has_two_paths = status.startswith(("R", "C"))
        if has_two_paths and idx + 1 >= len(tokens):
            break
        first_p = tokens[idx]
        second_p = tokens[idx + 1] if has_two_paths else None
        idx += 2 if has_two_paths else 1
        _record_diff_tree_entry(
            status, new_mode, new_oid, first_p, second_p, modified, deleted
        )
    deleted -= set(modified)
    return modified, deleted


def _collect_changed_files(
    repo_dir: Path,
    old_sha: str | None,
    new_sha: str,
    *,
    force: bool = False,
    ancestor_exists: bool | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str], bool]:
    """Collects `(modified_paths, deleted_paths, blob_oids, is_full_tree)` for a commit range."""
    if not force and old_sha == new_sha:
        return (), (), {}, False
    has_ancestor = bool(old_sha) and (
        ancestor_exists
        if ancestor_exists is not None
        else _commit_exists(repo_dir, old_sha)
    )
    if force or not has_ancestor:
        blobs = _ls_tree_blobs(repo_dir, new_sha)
        return tuple(sorted(blobs)), (), blobs, True
    assert old_sha is not None
    modified, deleted = _diff_tree_blobs(repo_dir, old_sha, new_sha)
    return tuple(sorted(modified)), tuple(sorted(deleted)), modified, False


def list_changed_files(
    repo_dir: Path,
    old_sha: str | None,
    new_sha: str,
    force: bool = False,
    *,
    ancestor_exists: bool | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Lists indexable (modified_or_added, deleted) files between old_sha and new_sha."""
    modified, deleted, _, _ = _collect_changed_files(
        repo_dir,
        old_sha,
        new_sha,
        force=force,
        ancestor_exists=ancestor_exists,
    )
    return modified, deleted


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
    normalized_ts = normalize_utc_iso(committed_at) or utc_now_iso()

    commit = CommitRecord(
        repo_id=repo.repo_id,
        sha=sha,
        author_name=author_name or None,
        author_email=author_email or None,
        committed_at=normalized_ts,
        title=subject,
        message=full_msg,
        pr_number=pr_number,
        files=files,
    )
    touched_preview = ", ".join(f.file_path for f in files[:15])
    chunk_text = (
        f"{full_msg}\n\nFiles: {touched_preview}".strip()
        if touched_preview
        else full_msg
    )
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
        updated_at=normalized_ts,
    )
    return commit, chunk


def _parse_commit_log_output(
    repo: RepoConfig, raw_output: str
) -> tuple[list[CommitRecord], list[TextChunk]]:
    """Splits formatted `git log` stdout into CommitRecord and TextChunk lists."""
    commits: list[CommitRecord] = []
    chunks: list[TextChunk] = []
    for raw_block in raw_output.split("__COMMIT__\x00"):
        if not raw_block.strip():
            continue
        parsed = _parse_single_commit_block(repo, raw_block)
        if parsed is not None:
            commits.append(parsed[0])
            chunks.append(parsed[1])
    return commits, chunks


def _index_commits_window(
    repo_dir: Path,
    repo: RepoConfig,
    stop_sha: str | None,
    tip_sha: str,
    max_commits: int = 300,
    *,
    ancestor_exists: bool | None = None,
) -> tuple[list[CommitRecord], list[TextChunk], str | None]:
    """Indexes up to `max_commits` from `tip_sha` down to `stop_sha` and returns any next cursor."""
    if max_commits <= 0 or stop_sha == tip_sha:
        return [], [], None
    has_stop = bool(stop_sha) and (
        ancestor_exists
        if ancestor_exists is not None
        else _commit_exists(repo_dir, stop_sha)
    )
    range_arg = f"{stop_sha}..{tip_sha}" if has_stop else tip_sha
    fmt = "__COMMIT__%x00%H%x00%an%x00%ae%x00%cI%x00%s%x00%b%x00__FILES__"
    res = run_git_command(
        repo_dir,
        [
            "log",
            "--no-merges",
            f"-n{max_commits + 1}",
            f"--pretty=format:{fmt}",
            "--numstat",
            range_arg,
        ],
        timeout=30,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return [], [], None

    commits, chunks = _parse_commit_log_output(repo, res.stdout)
    if len(commits) > max_commits:
        next_sha = commits[max_commits].sha
        next_cursor = f"{stop_sha}..{next_sha}" if has_stop else next_sha
        return commits[:max_commits], chunks[:max_commits], next_cursor
    return commits, chunks, None


def index_commits(
    repo_dir: Path,
    repo: RepoConfig,
    old_sha: str | None,
    new_sha: str,
    max_commits: int = 300,
    *,
    ancestor_exists: bool | None = None,
) -> tuple[list[CommitRecord], list[TextChunk]]:
    """Extracts CommitRecord and TextChunk entries between old_sha and new_sha."""
    commits, chunks, _ = _index_commits_window(
        repo_dir,
        repo,
        old_sha,
        new_sha,
        max_commits=max_commits,
        ancestor_exists=ancestor_exists,
    )
    return commits, chunks


def _is_indexable_blob_payload(
    obj_type: str, raw: bytes, size: int, max_blob_bytes: int
) -> bool:
    """Returns True when a `cat-file --batch` object is a valid non-binary text blob within size limits."""
    return (
        obj_type == "blob"
        and len(raw) == size
        and size <= max_blob_bytes
        and b"\x00" not in raw[:4096]
    )


def _parse_cat_file_stream(
    out: bytes,
    requested_paths: Sequence[str],
    resolved_oids: dict[str, str] | None = None,
    max_blob_bytes: int = MAX_INDEXED_BLOB_BYTES,
) -> dict[str, str]:
    """Parses `git cat-file --batch` stdout into `{file_path: decoded_text}`."""
    pos = 0
    blobs: dict[str, str] = {}
    for file_path in requested_paths:
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        header_line = out[pos:nl].decode("utf-8", errors="replace")
        pos = nl + 1
        if header_line.endswith(" missing"):
            continue
        parts = header_line.split()
        if len(parts) < 3 or not parts[2].isdigit():
            break
        blob_oid, obj_type, size = parts[0], parts[1], int(parts[2])
        raw = out[pos : pos + size]
        pos += size + 1
        if _is_indexable_blob_payload(obj_type, raw, size, max_blob_bytes):
            blobs[file_path] = raw.decode("utf-8", errors="replace")
            if resolved_oids is not None:
                resolved_oids.setdefault(file_path, blob_oid)
    return blobs


def _read_blobs_at_sha(
    repo_dir: Path,
    sha: str,
    file_paths: Sequence[str],
    blob_oids: Mapping[str, str] | None = None,
    resolved_oids: dict[str, str] | None = None,
    max_blob_bytes: int = MAX_INDEXED_BLOB_BYTES,
) -> dict[str, str]:
    """Reads multiple `<sha>:<file_path>` blobs in a single `git cat-file --batch` subprocess."""
    valid_paths = [p for p in file_paths if "\n" not in p and "\r" not in p]
    if not valid_paths:
        return {}
    oid_map = blob_oids or {}
    specs = "".join(f"{oid_map.get(p) or f'{sha}:{p}'}\n" for p in valid_paths).encode(
        "utf-8"
    )
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "PAGER": "cat",
    }
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "--no-pager", "cat-file", "--batch"],
            input=specs,
            capture_output=True,
            timeout=max(30, len(valid_paths) // 20),
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    return _parse_cat_file_stream(
        proc.stdout,
        valid_paths,
        resolved_oids=resolved_oids,
        max_blob_bytes=max_blob_bytes,
    )


def _read_blob_at_sha(repo_dir: Path, sha: str, file_path: str) -> str | None:
    """Reads `<sha>:<file_path>` from git object storage without touching worktree files."""
    return _read_blobs_at_sha(repo_dir, sha, (file_path,)).get(file_path)


def _extract_markdown_sections_from_blob(
    repo: RepoConfig,
    sha: str,
    file_path: str,
    blob_text: str,
    blob_oid: str = "",
) -> tuple[list[DocSectionRecord], list[TextChunk]]:
    """Parses Markdown sections in memory using blob_parsers."""
    raw_lines = blob_text.splitlines()
    raw_sections = parse_markdown_sections(file_path, raw_lines)

    now = utc_now_iso()
    sections: list[DocSectionRecord] = []
    chunks: list[TextChunk] = []
    for sec in raw_sections:
        l_start = int(sec["line_start"])
        l_end = int(sec["line_end"])
        sub_end = int(sec["subtree_line_end"])
        body_text = str(sec["content"]).strip()
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
                blob_oid=blob_oid,
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
                    is_untrusted=True,
                    updated_at=now,
                )
            )
    return sections, chunks


_CHUNK_TEXT_CAP = 2500
_CHUNK_DOCSTRING_CAP = 1500
_CHUNK_PREVIEW_LINES = 36


def _compose_symbol_chunk_text(
    signature: str, docstring: str, body_preview: str
) -> str:
    """Formats chunk text (`signature` + `docstring` + body preview), trimming preview first."""
    doc_trimmed = docstring.strip()[:_CHUNK_DOCSTRING_CAP]
    header = f"{signature}\n\n{doc_trimmed}" if doc_trimmed else signature
    if not body_preview:
        return header[:_CHUNK_TEXT_CAP]
    if len(header) >= _CHUNK_TEXT_CAP:
        return header[:_CHUNK_TEXT_CAP]
    budget = _CHUNK_TEXT_CAP - len(header) - 2
    if budget <= 0:
        return header[:_CHUNK_TEXT_CAP]
    return f"{header}\n\n{body_preview[:budget]}"


def _extract_code_symbols_from_blob(
    repo: RepoConfig,
    sha: str,
    file_path: str,
    blob_text: str,
    parsed: ParsedFile,
    blob_oid: str = "",
) -> tuple[list[CodeSymbolRecord], list[TextChunk], int]:
    """Builds `CodeSymbolRecord` and `TextChunk` rows from a `ParsedFile`."""
    raw_lines = split_lines(blob_text)
    now = utc_now_iso()

    seen_keys: set[tuple[str, str, int]] = set()
    symbols: list[CodeSymbolRecord] = []
    chunks: list[TextChunk] = []
    dup_dropped = 0
    for sym in parsed.symbols:
        key = (sym.kind, sym.qualified_name, sym.line_start)
        if key in seen_keys:
            dup_dropped += 1
            continue
        seen_keys.add(key)
        snippet_lines = raw_lines[
            max(0, sym.line_start - 1) : min(
                len(raw_lines),
                min(sym.line_end, sym.line_start + _CHUNK_PREVIEW_LINES - 1),
            )
        ]
        body_preview = "\n".join(snippet_lines).strip()

        symbols.append(
            CodeSymbolRecord(
                repo_id=repo.repo_id,
                file_path=file_path,
                symbol_name=sym.name,
                qualified_name=sym.qualified_name,
                parent=sym.parent,
                kind=sym.kind,
                line_start=sym.line_start,
                line_end=sym.line_end,
                signature=sym.signature,
                docstring=sym.docstring,
                visibility=sym.visibility,
                sha=sha,
                blob_oid=blob_oid,
            )
        )
        c_title = f"{sym.kind} {sym.qualified_name} in {file_path}"
        c_text = _compose_symbol_chunk_text(sym.signature, sym.docstring, body_preview)
        chunks.append(
            TextChunk(
                chunk_id=(
                    f"{repo.full_name}@symbol:{file_path}:{sym.kind}:"
                    f"{sym.qualified_name}:{sym.line_start}"
                ),
                repo_id=repo.repo_id,
                source_kind="code_symbol",
                source_ref=f"{file_path}#L{sym.line_start}-L{sym.line_end}",
                file_path=file_path,
                title=c_title,
                text=c_text,
                content_sha256=compute_content_sha256(c_title, c_text),
                is_untrusted=bool(sym.docstring.strip()),
                updated_at=now,
            )
        )
    return symbols, chunks, dup_dropped


@dataclass(frozen=True)
class IndexedBatch:
    """Symbols, sections, chunks, and per-file parser IDs extracted from a file batch."""

    symbols: list[CodeSymbolRecord]
    sections: list[DocSectionRecord]
    chunks: list[TextChunk]
    parser_ids: dict[str, str]
    syntax_error_count: int = 0
    dropped_symbol_count: int = 0


def _index_single_blob(
    repo: RepoConfig,
    target_sha: str,
    file_path: str,
    blob: str,
    file_oid: str,
    parsed_map: Mapping[str, ParsedFile],
    batch: IndexedBatch,
) -> tuple[int, int]:
    """Extracts symbols or Markdown sections for one blob and appends them into `batch`."""
    ext = Path(file_path).suffix.lower()
    if ext in MARKDOWN_EXTENSIONS:
        secs, chs = _extract_markdown_sections_from_blob(
            repo, target_sha, file_path, blob, blob_oid=file_oid
        )
        batch.sections.extend(secs)
        batch.chunks.extend(chs)
        batch.parser_ids[file_path] = MARKDOWN_PARSER_ID
        return 0, 0
    if ext in CODE_EXTENSIONS:
        parsed = parsed_map[file_path]
        syms, chs, dup_dropped = _extract_code_symbols_from_blob(
            repo, target_sha, file_path, blob, parsed, blob_oid=file_oid
        )
        batch.symbols.extend(syms)
        batch.chunks.extend(chs)
        batch.parser_ids[file_path] = parsed.parser_id
        return parsed.syntax_error_count, parsed.dropped_symbol_count + dup_dropped
    return 0, 0


def index_files_at_sha(
    repo_dir: Path,
    repo: RepoConfig,
    target_sha: str,
    modified_files: Sequence[str],
    *,
    parsers: SymbolParsers,
    reporter: ProgressReporter = NULL_REPORTER,
    blob_oids: Mapping[str, str] | None = None,
) -> IndexedBatch:
    """Extracts symbols, Markdown sections, and chunks for `modified_files` at `target_sha`."""
    acc = IndexedBatch(symbols=[], sections=[], chunks=[], parser_ids={})
    syntax_error_count = 0
    dropped_symbol_count = 0
    total_files = len(modified_files)
    resolved_oids: dict[str, str] = dict(blob_oids) if blob_oids else {}
    reporter.start_phase(repo.full_name, "git files", total=total_files)

    for start in range(0, total_files, _BLOB_BATCH_SIZE):
        batch_paths = modified_files[start : start + _BLOB_BATCH_SIZE]
        blobs = _read_blobs_at_sha(
            repo_dir,
            target_sha,
            batch_paths,
            blob_oids=resolved_oids,
            resolved_oids=resolved_oids,
        )
        code_blobs = {
            p: b
            for p in batch_paths
            if (b := blobs.get(p)) and Path(p).suffix.lower() in CODE_EXTENSIONS
        }
        parsed_map = parsers.parse_batch(code_blobs) if code_blobs else {}
        for offset, file_path in enumerate(batch_paths, start=1):
            blob = blobs.get(file_path)
            if blob:
                se, ds = _index_single_blob(
                    repo,
                    target_sha,
                    file_path,
                    blob,
                    resolved_oids.get(file_path, ""),
                    parsed_map,
                    acc,
                )
                syntax_error_count += se
                dropped_symbol_count += ds
            reporter.advance(start + offset, total_files, detail=file_path)
    reporter.finish_phase()
    return IndexedBatch(
        symbols=acc.symbols,
        sections=acc.sections,
        chunks=acc.chunks,
        parser_ids=acc.parser_ids,
        syntax_error_count=syntax_error_count,
        dropped_symbol_count=dropped_symbol_count,
    )


def _list_indexed_file_paths(store: PackratStore, repo_id: int) -> set[str]:
    """Returns all file paths with indexed symbols, doc sections, or file chunks."""
    return set(store.list_indexed_file_paths(repo_id))


def _enqueue_commit_mentions(
    store: PackratStore, repo: RepoConfig, commits: Sequence[CommitRecord]
) -> None:
    """Queues items mentioned in commit messages for a timeline refresh.

    This catches `ReferencedEvent` targets on the indexed branch without any
    GitHub calls. A squash-merge commit's mention of its own PR (`(#N)` in the
    subject) is skipped, since merging already refreshes that PR's timeline.
    """
    resolve = caching_resolver(store.get_repo)
    all_targets: set[tuple[int, int]] = set()
    for commit in commits:
        targets = set(extract_item_mentions(commit.message, repo, resolve))
        if commit.pr_number is not None:
            targets.discard((repo.repo_id, commit.pr_number))
        all_targets |= targets
    if all_targets:
        store.enqueue_timeline_dirty(all_targets, reason="commit")


def _parse_commits_cursor(cursor: str) -> tuple[str | None, str]:
    """Splits a `commits_backfill_cursor` (`<stop_sha>..<tip_sha>` or `<tip_sha>`)."""
    if ".." in cursor:
        stop_sha, tip_sha = cursor.split("..", 1)
        return (stop_sha or None), tip_sha
    return None, cursor


def _merge_cursor_with_prior(delta_cursor: str, prior_cursor: str | None) -> str:
    """Preserves the older floor when a new commit delta is truncated above a pending backfill."""
    if not prior_cursor:
        return delta_cursor
    prior_stop, _ = _parse_commits_cursor(prior_cursor)
    _, delta_next = _parse_commits_cursor(delta_cursor)
    return f"{prior_stop}..{delta_next}" if prior_stop else delta_next


def _collect_commits_for_phase(
    repo_dir: Path,
    repo: RepoConfig,
    old_sha: str | None,
    target_sha: str,
    *,
    has_new_head: bool,
    ancestor_exists: bool,
    prior_cursor: str | None,
    max_commits: int,
) -> tuple[list[CommitRecord], list[TextChunk], str | None]:
    """Collects commits from a new HEAD delta and/or pending backfill cursor."""
    if not has_new_head:
        if prior_cursor is None:
            return [], [], None
        c_stop, c_tip = _parse_commits_cursor(prior_cursor)
        return _index_commits_window(
            repo_dir, repo, c_stop, c_tip, max_commits=max_commits
        )

    commits, chunks, delta_cursor = _index_commits_window(
        repo_dir,
        repo,
        old_sha,
        target_sha,
        max_commits=max_commits,
        ancestor_exists=ancestor_exists,
    )
    if delta_cursor is not None:
        return commits, chunks, _merge_cursor_with_prior(delta_cursor, prior_cursor)
    if prior_cursor is None:
        return commits, chunks, None

    remaining = max_commits - len(commits)
    if remaining <= 0:
        return commits, chunks, prior_cursor
    c_stop, c_tip = _parse_commits_cursor(prior_cursor)
    more_c, more_ch, next_cursor = _index_commits_window(
        repo_dir, repo, c_stop, c_tip, max_commits=remaining
    )
    commits.extend(more_c)
    chunks.extend(more_ch)
    return commits, chunks, next_cursor


def _sync_commits_phase(
    store: PackratStore,
    repo_dir: Path,
    repo: RepoConfig,
    old_sha: str | None,
    target_sha: str,
    *,
    force: bool,
    ancestor_exists: bool,
    max_commits: int,
    stats: SyncStats,
) -> tuple[list[CommitRecord], list[TextChunk]]:
    """Indexes new commits and drains any pending `commits_backfill_cursor` within `max_commits`."""
    prior_cursor = (
        None if force or not ancestor_exists else repo.commits_backfill_cursor
    )
    has_new_head = force or old_sha != target_sha
    commits, chunks, next_cursor = _collect_commits_for_phase(
        repo_dir,
        repo,
        old_sha,
        target_sha,
        has_new_head=has_new_head,
        ancestor_exists=ancestor_exists,
        prior_cursor=prior_cursor,
        max_commits=max_commits,
    )
    if commits:
        store.upsert_commits(commits)
        store.upsert_chunks(chunks)
        _enqueue_commit_mentions(store, repo, commits)

    if next_cursor is not None:
        store.set_commits_backfill_state(repo.repo_id, cursor=next_cursor, done_at=None)
        if "commits" not in stats.backfill_incomplete:
            stats.backfill_incomplete.append("commits")
    elif (
        force
        or repo.commits_backfill_cursor is not None
        or repo.commits_backfill_done_at is None
    ):
        store.set_commits_backfill_state(
            repo.repo_id, cursor=None, done_at=utc_now_iso()
        )
    return commits, chunks


def _should_skip_indexed_path(
    path: str,
    blob_oid: str,
    stored: tuple[str, str] | None,
    parsers: SymbolParsers,
) -> bool:
    """Applies skip rule 2 with ratchet: unchanged blobs keep real-parser rows on outage."""
    if not blob_oid or stored is None:
        return False
    stored_oid, stored_parser_id = stored
    if stored_oid != blob_oid:
        return False
    expected_id = parsers.parser_id_for(path)
    if stored_parser_id == expected_id:
        return True
    ext = Path(path).suffix.lower()
    if (
        ROUTING.get(ext) != "md-sections"
        and expected_id == REGEX_PARSER_ID
        and stored_parser_id not in ("", REGEX_PARSER_ID)
    ):
        return True
    return False


def _filter_unchanged_blob_paths(
    store: PackratStore,
    repo_id: int,
    tree_modified: tuple[str, ...],
    blob_oids: Mapping[str, str],
    *,
    force: bool,
    parsers: SymbolParsers,
) -> tuple[str, ...]:
    """Filters out paths whose `blob_oid` and active `parser_id` already match `indexed_files`."""
    if force or not tree_modified:
        return tree_modified
    indexed_map = store.get_indexed_files(repo_id)
    return tuple(
        p
        for p in tree_modified
        if not _should_skip_indexed_path(
            p, blob_oids.get(p, ""), indexed_map.get(p), parsers
        )
    )


@dataclass(frozen=True)
class _FilesPhaseResult:
    symbols_upserted: int
    sections_upserted: int
    chunks_upserted: int
    syntax_error_count: int
    dropped_symbol_count: int
    reparse_capped: bool


def _sync_files_phase(
    store: PackratStore,
    repo_dir: Path,
    repo: RepoConfig,
    old_sha: str | None,
    target_sha: str,
    *,
    force: bool,
    fingerprint_mismatch: bool,
    ancestor_exists: bool,
    parsers: SymbolParsers,
    max_reparse_files: int | None,
    reporter: ProgressReporter,
) -> _FilesPhaseResult:
    """Enumerates files, applies ratchet & cap, and commits index updates per 250-file batch."""
    tree_modified, deleted_files, blob_oids, is_full_tree = _collect_changed_files(
        repo_dir,
        old_sha,
        target_sha,
        force=(force or fingerprint_mismatch),
        ancestor_exists=ancestor_exists,
    )
    if is_full_tree:
        extra_deleted = _list_indexed_file_paths(store, repo.repo_id) - set(
            tree_modified
        )
        deleted_files = tuple(sorted(set(deleted_files) | extra_deleted))

    modified_files = _filter_unchanged_blob_paths(
        store, repo.repo_id, tree_modified, blob_oids, force=force, parsers=parsers
    )
    reparse_capped = (
        max_reparse_files is not None
        and max_reparse_files >= 0
        and len(modified_files) > max_reparse_files
    )
    if reparse_capped:
        assert max_reparse_files is not None
        modified_files = modified_files[:max_reparse_files]

    if deleted_files:
        store.replace_code_index_for_files(
            repo_id=repo.repo_id,
            changed_paths=(),
            deleted_paths=deleted_files,
            symbols=(),
            doc_sections=(),
            chunks=(),
            reporter=reporter,
            repo_label=repo.full_name,
        )

    sym_total = sec_total = chunk_total = syn_err_total = dropped_total = 0
    for start in range(0, len(modified_files), _BLOB_BATCH_SIZE):
        batch_paths = modified_files[start : start + _BLOB_BATCH_SIZE]
        batch = index_files_at_sha(
            repo_dir,
            repo,
            target_sha,
            batch_paths,
            parsers=parsers,
            reporter=reporter,
            blob_oids=blob_oids,
        )
        store.replace_code_index_for_files(
            repo_id=repo.repo_id,
            changed_paths=batch_paths,
            deleted_paths=(),
            symbols=batch.symbols,
            doc_sections=batch.sections,
            chunks=batch.chunks,
            reporter=reporter,
            repo_label=repo.full_name,
        )
        indexed_rows = [
            (
                p,
                blob_oids[p],
                target_sha,
                batch.parser_ids.get(p, parsers.parser_id_for(p)),
            )
            for p in batch_paths
            if blob_oids.get(p)
        ]
        if indexed_rows:
            store.upsert_indexed_files(repo.repo_id, indexed_rows)
        sym_total += len(batch.symbols)
        sec_total += len(batch.sections)
        chunk_total += len(batch.chunks)
        syn_err_total += batch.syntax_error_count
        dropped_total += batch.dropped_symbol_count

    return _FilesPhaseResult(
        symbols_upserted=sym_total,
        sections_upserted=sec_total,
        chunks_upserted=chunk_total,
        syntax_error_count=syn_err_total,
        dropped_symbol_count=dropped_total,
        reparse_capped=reparse_capped,
    )


def _resolve_ancestor(
    repo_dir: Path, target_sha: str, raw_old_sha: str | None, *, force: bool
) -> tuple[str | None, bool]:
    """Resolves valid ancestor SHA and whether it exists in the local object store."""
    if force or not raw_old_sha:
        return None, False
    exists = raw_old_sha == target_sha or _commit_exists(repo_dir, raw_old_sha)
    return (raw_old_sha if exists else None), exists


def _apply_files_phase_and_persist_state(
    store: PackratStore,
    repo_dir: Path,
    repo: RepoConfig,
    old_sha: str | None,
    target_sha: str,
    *,
    force: bool,
    fingerprint_mismatch: bool,
    current_fp: str,
    ancestor_exists: bool,
    parsers: SymbolParsers,
    max_reparse_files: int | None,
    reporter: ProgressReporter,
    stats: SyncStats,
) -> None:
    """Runs `_sync_files_phase` and persists `last_indexed_sha` and `parser_fingerprint` when clean."""
    file_res = _sync_files_phase(
        store,
        repo_dir,
        repo,
        old_sha,
        target_sha,
        force=force,
        fingerprint_mismatch=fingerprint_mismatch,
        ancestor_exists=ancestor_exists,
        parsers=parsers,
        max_reparse_files=max_reparse_files,
        reporter=reporter,
    )
    if not file_res.reparse_capped:
        store.update_repo_sync_state(repo.repo_id, last_indexed_sha=target_sha)
        if not parsers.unavailable() and repo.parser_fingerprint != current_fp:
            store.set_parser_fingerprint(repo.repo_id, current_fp)
    stats.symbols_upserted = file_res.symbols_upserted
    stats.doc_sections_upserted = file_res.sections_upserted
    stats.chunks_upserted += file_res.chunks_upserted
    stats.syntax_error_count = file_res.syntax_error_count
    stats.dropped_symbol_count = file_res.dropped_symbol_count
    stats.reparse_capped = file_res.reparse_capped


def _resolve_sync_repo_dir(
    store: PackratStore, repo: RepoConfig
) -> tuple[Path | None, str | None]:
    """Resolves `repo`'s git directory or returns `(None, skipped_reason)`."""
    if repo.git_source == "none":
        return None, "no_local_path"
    repo_dir = store.resolve_git_dir(repo)
    if repo_dir is None:
        return None, "no_local_path"
    if repo.git_source == "managed" and not repo_dir.exists():
        return None, "pending_clone"
    if not _is_git_repository(repo_dir):
        return None, f"missing_git_repo:{repo_dir}"
    return repo_dir, None


def _sync_with_resolved_parsers(
    store: PackratStore,
    repo: RepoConfig,
    parsers: SymbolParsers,
    *,
    force: bool,
    fetch: bool,
    max_commits: int,
    max_reparse_files: int | None,
    reporter: ProgressReporter,
) -> SyncStats:
    """Executes git commits and symbol/doc file indexing inside an active `SymbolParsers` session."""
    stats = SyncStats(repo_full_name=repo.full_name)
    repo_dir, skipped_reason = _resolve_sync_repo_dir(store, repo)
    if skipped_reason is not None or repo_dir is None:
        stats.skipped_reason = skipped_reason
        return stats
    resolved = resolve_canonical_ref(
        repo_dir, repo, fetch=fetch, reporter=reporter, stats=stats
    )
    if resolved is None:
        stats.skipped_reason = f"missing_git_repo:{repo_dir}"
        return stats

    stats.indexed_ref, target_sha = resolved
    stats.indexed_sha = target_sha
    current_fp = parsers.fingerprint()
    fingerprint_mismatch = force or (repo.parser_fingerprint != current_fp)
    if (
        not fingerprint_mismatch
        and repo.last_indexed_sha == target_sha
        and not repo.commits_backfill_cursor
    ):
        return stats

    old_sha, ancestor_exists = _resolve_ancestor(
        repo_dir, target_sha, repo.last_indexed_sha, force=force
    )
    commits, commit_chunks = _sync_commits_phase(
        store,
        repo_dir,
        repo,
        old_sha,
        target_sha,
        force=force,
        ancestor_exists=ancestor_exists,
        max_commits=max_commits,
        stats=stats,
    )
    stats.commits_upserted = len(commits)
    stats.chunks_upserted = len(commit_chunks)

    if fingerprint_mismatch or old_sha != target_sha:
        _apply_files_phase_and_persist_state(
            store,
            repo_dir,
            repo,
            old_sha,
            target_sha,
            force=force,
            fingerprint_mismatch=fingerprint_mismatch,
            current_fp=current_fp,
            ancestor_exists=ancestor_exists,
            parsers=parsers,
            max_reparse_files=max_reparse_files,
            reporter=reporter,
            stats=stats,
        )
    return stats


def sync_git_codebase(
    store: PackratStore,
    repo: RepoConfig,
    force: bool = False,
    fetch: bool = False,
    max_commits: int = 300,
    reporter: ProgressReporter = NULL_REPORTER,
    *,
    parsers: SymbolParsers | None = None,
    max_reparse_files: int | None = None,
) -> SyncStats:
    """Incrementally indexes canonical git commits, AST symbols, and Markdown sections."""
    if parsers is not None:
        return _sync_with_resolved_parsers(
            store,
            repo,
            parsers,
            force=force,
            fetch=fetch,
            max_commits=max_commits,
            max_reparse_files=max_reparse_files,
            reporter=reporter,
        )
    with SymbolParsers(
        overrides=store.get_parser_overrides(), reporter=reporter
    ) as local_parsers:
        return _sync_with_resolved_parsers(
            store,
            repo,
            local_parsers,
            force=force,
            fetch=fetch,
            max_commits=max_commits,
            max_reparse_files=max_reparse_files,
            reporter=reporter,
        )
