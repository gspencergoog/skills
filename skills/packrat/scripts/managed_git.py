"""Managed bare git repository lifecycle, validation, and remote checks.

Owns `<data-dir>/git/<owner>/<name>.git` and external locks under
`<data-dir>/git/.locks/<repo_id>.lock`. Imports only `git_cmd` and `models`
so it never reads or writes SQLite directly.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

try:
    from .git_cmd import build_git_ssh_env, run_git_command
    from .models import RepoConfig
except ImportError:
    from git_cmd import build_git_ssh_env, run_git_command  # type: ignore[no-redef]
    from models import RepoConfig  # type: ignore[no-redef]

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SCP_URL_RE = re.compile(
    r"^[A-Za-z0-9_.-]+@(?:\[[A-Za-z0-9:.]+\]|[A-Za-z0-9.-]+):[^\s/][^\s]*$|^[A-Za-z0-9_.-]+@(?:\[[A-Za-z0-9:.]+\]|[A-Za-z0-9.-]+):/[^\s]+$"
)
_CLONING_MARKER = ".packrat-cloning"
_LOCKS_DIR = ".locks"


@dataclass(frozen=True)
class GitResult:
    """Outcome from `ensure_git`, `check_remote`, or `fetch_git`."""

    repo_id: int
    old_tip: str
    new_tip: str
    remote_default_branch: str
    duration_s: float
    error: str = ""
    timed_out: bool = False


def _has_control_or_space(value: str) -> bool:
    return any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def validate_slug(value: str, label: str = "slug") -> str:
    """Validates an owner or repository slug against path traversal and option injection."""
    if not isinstance(value, str) or value in (".", "..") or not _SLUG_RE.match(value):
        raise ValueError(
            f"Invalid {label} {value!r}; expected 1-100 chars of [A-Za-z0-9_.-], not '.' or '..'."
        )
    if value.startswith("-"):
        raise ValueError(f"Invalid {label} {value!r}; cannot start with '-'.")
    return value


def validate_branch(branch: str) -> str:
    """Validates a branch name with `git check-ref-format --branch`."""
    if (
        not isinstance(branch, str)
        or not branch
        or branch in (".", "..", "HEAD")
        or branch.startswith("-")
        or _has_control_or_space(branch)
    ):
        raise ValueError(f"Invalid branch name {branch!r}.")
    res = run_git_command(None, ["check-ref-format", "--branch", branch], timeout=10)
    if res.returncode != 0:
        raise ValueError(f"Invalid branch name {branch!r}.")
    return branch


def _validate_url_with_scheme(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    has_path = bool(parsed.path and parsed.path != "/")
    if parsed.scheme == "https":
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                f"HTTPS git URL must include host and omit userinfo: {url!r}."
            )
        if not has_path:
            raise ValueError(f"HTTPS git URL must include repository path: {url!r}.")
        return url
    if parsed.scheme == "ssh":
        if not parsed.hostname or not has_path:
            raise ValueError(f"SSH git URL must include host and path: {url!r}.")
        return url
    raise ValueError(
        f"Unsupported git URL scheme {parsed.scheme!r}; expected https or ssh."
    )


def validate_url(url: str) -> str:
    """Validates `git_url` as HTTPS (no userinfo), SSH URL, or SCP-style SSH path."""
    if (
        not isinstance(url, str)
        or not url
        or url.startswith("-")
        or _has_control_or_space(url)
    ):
        raise ValueError(f"Invalid git URL {url!r}.")
    if "://" in url:
        return _validate_url_with_scheme(url)
    if not _SCP_URL_RE.match(url):
        raise ValueError(
            f"Invalid git URL {url!r}; expected https://, ssh://, or user@host:path."
        )
    return url


def git_root_for_db(db_path: Path) -> Path:
    """Returns the managed clone root directory paired with `db_path`."""
    return Path(db_path).parent / "git"


def _assert_safe_dir(root: Path, owner: str, name: str) -> Path:
    """Checks containment and absence of symlinks inside `root` for `<owner>/<name>.git`."""
    owner_dir = root / owner
    target = owner_dir / f"{name}.git"
    for candidate in (root, owner_dir, target):
        if candidate.is_symlink():
            raise ValueError(f"Refusing symlink in managed git path: {candidate}")
    expected = root.resolve() / owner / f"{name}.git"
    if target.resolve() != expected:
        raise ValueError(f"Managed git path escapes root: {target}")
    return target


def git_dir(root: Path, owner: str, name: str) -> Path:
    """Returns the validated bare clone path `<root>/<owner>/<name>.git`."""
    clean_owner = validate_slug(owner, "owner")
    clean_name = validate_slug(name, "name")
    return _assert_safe_dir(root, clean_owner, clean_name)


def git_lock_path(root: Path, repo_id: int) -> Path:
    """Returns `<root>/.locks/<repo_id>.lock` outside the repository directory."""
    return root / _LOCKS_DIR / f"{int(repo_id)}.lock"


@contextlib.contextmanager
def _repo_lock(root: Path, repo_id: int, *, blocking: bool) -> Iterator[bool]:
    lock_path = git_lock_path(root, repo_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_local_tip(repo_dir: Path, branch: str) -> str:
    if not repo_dir.exists():
        return ""
    res = run_git_command(
        repo_dir,
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
        timeout=10,
    )
    if res.returncode != 0:
        return ""
    lines = res.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def _parse_ls_remote_output(stdout: str, branch: str) -> tuple[str, str]:
    """Extracts `(remote_default_branch, tip_sha)` from `git ls-remote --symref`."""
    default_branch = ""
    head_sha = ""
    branch_sha = ""
    target_ref = f"refs/heads/{branch}" if branch else ""
    for line in stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        left, ref_name = parts[0].strip(), parts[1].strip()
        if ref_name == "HEAD" and left.startswith("ref: refs/heads/"):
            candidate = left.removeprefix("ref: refs/heads/").strip()
            with contextlib.suppress(ValueError):
                default_branch = validate_branch(candidate)
        elif ref_name == "HEAD":
            head_sha = left
        elif target_ref and ref_name == target_ref:
            branch_sha = left
    if branch_sha:
        return default_branch, branch_sha
    if not branch or branch == default_branch:
        return default_branch, head_sha
    return default_branch, ""


def _classify_git_error(repo: RepoConfig, clone_url: str, stderr: str) -> str:
    lower = stderr.lower()
    is_auth_or_404 = (
        "authentication failed" in lower
        or "returned error: 401" in lower
        or "returned error: 403" in lower
        or "returned error: 404" in lower
        or "could not read username" in lower
        or "repository not found" in lower
    )
    if is_auth_or_404 and clone_url.startswith("https://"):
        return (
            f"auth or not found; set an SSH URL with "
            f"packrat repo set-git {repo.full_name} managed --url git@github.com:{repo.full_name}.git"
        )
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    return lines[-1] if lines else "git command failed"


def _allow_test_url(clone_url: str, extra_git_config: Sequence[str]) -> None:
    if clone_url.startswith("file://") and "protocol.file.allow=always" in " ".join(
        extra_git_config
    ):
        return
    validate_url(clone_url)


def check_remote(
    repo: RepoConfig,
    *,
    timeout_s: int = 20,
    extra_git_config: Sequence[str] = (),
    target_branch: str | None = None,
) -> GitResult:
    """Probes `repo.clone_url` via `ls-remote --symref` without writing local files.

    Never passes `--heads` so git emits `ref: refs/heads/<name>\\tHEAD`.
    """
    started = time.monotonic()
    clone_url = repo.clone_url
    branch = repo.default_branch if target_branch is None else target_branch
    try:
        _allow_test_url(clone_url, extra_git_config)
        if branch:
            branch = validate_branch(branch)
    except ValueError as exc:
        return GitResult(
            repo.repo_id, "", "", "", round(time.monotonic() - started, 3), str(exc)
        )

    patterns = ["HEAD", f"refs/heads/{branch}"] if branch else ["HEAD"]
    args = ["ls-remote", "--symref", "--", clone_url, *patterns]
    try:
        res = run_git_command(
            None,
            args,
            timeout=timeout_s,
            extra_env=build_git_ssh_env(clone_url),
            extra_config=extra_git_config,
        )
    except subprocess.TimeoutExpired:
        return GitResult(
            repo.repo_id,
            "",
            "",
            "",
            round(time.monotonic() - started, 3),
            error="ls-remote timed out",
            timed_out=True,
        )
    elapsed = round(time.monotonic() - started, 3)
    if res.returncode != 0:
        return GitResult(
            repo.repo_id,
            "",
            "",
            "",
            elapsed,
            error=_classify_git_error(repo, clone_url, res.stderr),
        )
    remote_default, tip = _parse_ls_remote_output(res.stdout, branch)
    return GitResult(repo.repo_id, "", tip, remote_default, elapsed)


def _clean_stale_tmp_packs(repo_dir: Path) -> None:
    pack_dir = repo_dir / "objects" / "pack"
    if not pack_dir.is_dir():
        return
    for item in pack_dir.glob("tmp_pack_*"):
        if item.is_file() and not item.is_symlink():
            with contextlib.suppress(OSError):
                item.unlink()


def _git_config_get(
    repo_dir: Path, key: str, extra_config: Sequence[str]
) -> str | None:
    res = run_git_command(
        repo_dir, ["config", "--get", "--", key], timeout=10, extra_config=extra_config
    )
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _git_config_set(
    repo_dir: Path, key: str, value: str, extra_config: Sequence[str]
) -> None:
    if _git_config_get(repo_dir, key, extra_config) == value:
        return
    res = run_git_command(
        repo_dir, ["config", "--", key, value], timeout=10, extra_config=extra_config
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"git config {key} failed")


def _converge_repo_config(
    repo_dir: Path,
    clone_url: str,
    branch: str,
    extra_config: Sequence[str],
) -> None:
    current_url = run_git_command(
        repo_dir,
        ["remote", "get-url", "--", "origin"],
        timeout=10,
        extra_config=extra_config,
    )
    if current_url.returncode != 0:
        res = run_git_command(
            repo_dir,
            ["remote", "add", "--", "origin", clone_url],
            timeout=10,
            extra_config=extra_config,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "git remote add failed")
    elif current_url.stdout.strip() != clone_url:
        res = run_git_command(
            repo_dir,
            ["remote", "set-url", "--", "origin", clone_url],
            timeout=10,
            extra_config=extra_config,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "git remote set-url failed")

    _git_config_set(
        repo_dir,
        "remote.origin.fetch",
        f"+refs/heads/{branch}:refs/heads/{branch}",
        extra_config,
    )
    _git_config_set(repo_dir, "remote.origin.tagOpt", "--no-tags", extra_config)
    _git_config_set(repo_dir, "maintenance.auto", "false", extra_config)
    _git_config_set(repo_dir, "gc.autoDetach", "false", extra_config)

    sym = run_git_command(
        repo_dir,
        ["symbolic-ref", "--quiet", "HEAD"],
        timeout=10,
        extra_config=extra_config,
    )
    target_ref = f"refs/heads/{branch}"
    if sym.returncode != 0 or sym.stdout.strip() != target_ref:
        res = run_git_command(
            repo_dir,
            ["symbolic-ref", "HEAD", target_ref],
            timeout=10,
            extra_config=extra_config,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "git symbolic-ref HEAD failed")


def _run_fetch_locked(
    repo_dir: Path,
    repo: RepoConfig,
    branch: str,
    *,
    timeout_s: int,
    maintenance: Literal["skip", "auto"],
    extra_git_config: Sequence[str],
) -> tuple[str, bool, str]:
    """Runs `git fetch` + optional `git maintenance run --auto`; returns `(new_tip, timed_out, err)`."""
    _clean_stale_tmp_packs(repo_dir)
    clone_url = repo.clone_url
    try:
        res = run_git_command(
            repo_dir,
            ["fetch", "--no-tags", "--no-auto-maintenance", "--", "origin"],
            timeout=timeout_s,
            extra_env=build_git_ssh_env(clone_url),
            extra_config=extra_git_config,
        )
    except subprocess.TimeoutExpired:
        return _read_local_tip(repo_dir, branch), True, "git fetch timed out"
    if res.returncode != 0:
        return (
            _read_local_tip(repo_dir, branch),
            False,
            _classify_git_error(repo, clone_url, res.stderr),
        )
    if maintenance == "auto":
        run_git_command(
            repo_dir,
            ["maintenance", "run", "--auto"],
            timeout=min(timeout_s, 300),
            extra_config=extra_git_config,
        )
    return _read_local_tip(repo_dir, branch), False, ""


def _resolve_branch_from_remote(
    repo: RepoConfig,
    branch: str,
    extra_git_config: Sequence[str],
) -> tuple[str, str, str]:
    """Probes remote HEAD when `branch` is unverified; returns `(branch, discovered, err)`."""
    probe = check_remote(
        repo,
        timeout_s=20,
        extra_git_config=extra_git_config,
        target_branch=branch,
    )
    if probe.error or probe.timed_out:
        return branch, "", probe.error or "ls-remote timed out"
    if probe.remote_default_branch and (
        not branch or (not probe.new_tip and probe.remote_default_branch != branch)
    ):
        return probe.remote_default_branch, probe.remote_default_branch, ""
    return branch or "main", "", ""


def _converge_and_fetch_ensure(
    target: Path,
    repo: RepoConfig,
    branch: str,
    discovered: str,
    old_tip: str,
    started: float,
    *,
    timeout_s: int,
    reporter: Any,
    extra_git_config: Sequence[str],
) -> GitResult:
    clone_url = repo.clone_url
    marker = target / _CLONING_MARKER
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        init_res = run_git_command(
            None,
            ["init", "--bare", "--", str(target)],
            timeout=30,
            extra_config=extra_git_config,
        )
        if init_res.returncode != 0:
            err = init_res.stderr.strip() or "git init --bare failed"
            return GitResult(
                repo.repo_id,
                old_tip,
                old_tip,
                discovered,
                round(time.monotonic() - started, 3),
                err,
            )
        marker.write_text("cloning\n", encoding="utf-8")
    try:
        _converge_repo_config(target, clone_url, branch, extra_git_config)
    except RuntimeError as exc:
        return GitResult(
            repo.repo_id,
            old_tip,
            old_tip,
            discovered,
            round(time.monotonic() - started, 3),
            str(exc),
        )

    if reporter is not None:
        reporter.start_phase(repo.full_name, "git clone", total=None)
        reporter.advance(0, None, detail=f"cloning {clone_url}")
    new_tip, timed_out, fetch_err = _run_fetch_locked(
        target,
        repo,
        branch,
        timeout_s=timeout_s,
        maintenance="skip",
        extra_git_config=extra_git_config,
    )
    if reporter is not None:
        summary = (
            f"cloned {branch} ({new_tip[:8]})" if not fetch_err and new_tip else ""
        )
        reporter.finish_phase(summary)
    if not fetch_err and marker.exists():
        with contextlib.suppress(OSError):
            marker.unlink()
    return GitResult(
        repo.repo_id,
        old_tip,
        new_tip,
        discovered,
        round(time.monotonic() - started, 3),
        fetch_err,
        timed_out,
    )


def ensure_git(
    root: Path,
    repo: RepoConfig,
    *,
    timeout_s: int = 1800,
    blocking_lock: bool = True,
    reporter: Any = None,
    extra_git_config: Sequence[str] = (),
) -> GitResult:
    """Initializes, converges, and fetches a bare clone if uncloned or incomplete."""
    started = time.monotonic()
    try:
        _allow_test_url(repo.clone_url, extra_git_config)
        target = git_dir(root, repo.owner, repo.name)
        branch = validate_branch(repo.default_branch) if repo.default_branch else ""
    except ValueError as exc:
        return GitResult(
            repo.repo_id, "", "", "", round(time.monotonic() - started, 3), str(exc)
        )

    with _repo_lock(root, repo.repo_id, blocking=blocking_lock) as locked:
        if not locked:
            return GitResult(
                repo.repo_id,
                "",
                "",
                "",
                round(time.monotonic() - started, 3),
                "repo lock held",
            )
        _assert_safe_dir(root, repo.owner, repo.name)
        marker = target / _CLONING_MARKER
        old_tip = _read_local_tip(target, branch) if branch else ""
        if target.exists() and not marker.exists() and old_tip:
            try:
                _converge_repo_config(target, repo.clone_url, branch, extra_git_config)
            except RuntimeError as exc:
                return GitResult(
                    repo.repo_id,
                    old_tip,
                    old_tip,
                    "",
                    round(time.monotonic() - started, 3),
                    str(exc),
                )
            return GitResult(
                repo.repo_id, old_tip, old_tip, "", round(time.monotonic() - started, 3)
            )

        branch, discovered, probe_err = _resolve_branch_from_remote(
            repo, branch, extra_git_config
        )
        if probe_err:
            return GitResult(
                repo.repo_id,
                old_tip,
                old_tip,
                "",
                round(time.monotonic() - started, 3),
                probe_err,
            )
        old_tip = _read_local_tip(target, branch)
        return _converge_and_fetch_ensure(
            target,
            repo,
            branch,
            discovered,
            old_tip,
            started,
            timeout_s=timeout_s,
            reporter=reporter,
            extra_git_config=extra_git_config,
        )


def fetch_git(
    root: Path,
    repo: RepoConfig,
    *,
    timeout_s: int = 1800,
    maintenance: Literal["skip", "auto"] = "auto",
    blocking_lock: bool = True,
    extra_git_config: Sequence[str] = (),
) -> GitResult:
    """Converges and fetches a managed bare repository under its lock."""
    started = time.monotonic()
    clone_url = repo.clone_url
    try:
        _allow_test_url(clone_url, extra_git_config)
        target = git_dir(root, repo.owner, repo.name)
        branch = validate_branch(repo.default_branch or "main")
    except ValueError as exc:
        return GitResult(
            repo.repo_id, "", "", "", round(time.monotonic() - started, 3), str(exc)
        )

    if not target.exists():
        return ensure_git(
            root,
            repo,
            timeout_s=timeout_s,
            blocking_lock=blocking_lock,
            extra_git_config=extra_git_config,
        )

    with _repo_lock(root, repo.repo_id, blocking=blocking_lock) as locked:
        if not locked:
            return GitResult(
                repo.repo_id,
                "",
                "",
                "",
                round(time.monotonic() - started, 3),
                "repo lock held",
            )
        _assert_safe_dir(root, repo.owner, repo.name)
        old_tip = _read_local_tip(target, branch)
        try:
            _converge_repo_config(target, clone_url, branch, extra_git_config)
        except RuntimeError as exc:
            return GitResult(
                repo.repo_id,
                old_tip,
                old_tip,
                "",
                round(time.monotonic() - started, 3),
                str(exc),
            )
        new_tip, timed_out, fetch_err = _run_fetch_locked(
            target,
            repo,
            branch,
            timeout_s=timeout_s,
            maintenance=maintenance,
            extra_git_config=extra_git_config,
        )
        discovered = ""
        if "couldn't find remote ref" in fetch_err.lower():
            next_branch, discovered, probe_err = _resolve_branch_from_remote(
                repo, branch, extra_git_config
            )
            if not probe_err and discovered and next_branch != branch:
                branch = next_branch
                try:
                    _converge_repo_config(target, clone_url, branch, extra_git_config)
                    new_tip, timed_out, fetch_err = _run_fetch_locked(
                        target,
                        repo,
                        branch,
                        timeout_s=timeout_s,
                        maintenance=maintenance,
                        extra_git_config=extra_git_config,
                    )
                except RuntimeError as exc:
                    fetch_err = str(exc)
        marker = target / _CLONING_MARKER
        if not fetch_err and marker.exists():
            with contextlib.suppress(OSError):
                marker.unlink()
        return GitResult(
            repo.repo_id,
            old_tip,
            new_tip,
            discovered,
            round(time.monotonic() - started, 3),
            fetch_err,
            timed_out,
        )


def remove_git(root: Path, repo: RepoConfig, *, blocking_lock: bool = True) -> bool:
    """Removes a managed bare clone directory under the repo lock after containment check."""
    target = git_dir(root, repo.owner, repo.name)
    with _repo_lock(root, repo.repo_id, blocking=blocking_lock) as locked:
        if not locked or not target.exists():
            return False
        _assert_safe_dir(root, repo.owner, repo.name)
        shutil.rmtree(target)
        with contextlib.suppress(OSError):
            target.parent.rmdir()
        return True


def move_git(
    root: Path,
    old_repo: RepoConfig,
    new_owner: str,
    new_name: str,
    *,
    clone_url: str,
    blocking_lock: bool = True,
    extra_git_config: Sequence[str] = (),
) -> Path | None:
    """Moves a managed bare clone cleanly (supporting case-only renames) and updates origin."""
    _allow_test_url(clone_url, extra_git_config)
    src = git_dir(root, old_repo.owner, old_repo.name)
    dst = git_dir(root, new_owner, new_name)
    with _repo_lock(root, old_repo.repo_id, blocking=blocking_lock) as locked:
        if not locked or not src.exists():
            return None
        _assert_safe_dir(root, old_repo.owner, old_repo.name)
        _assert_safe_dir(root, new_owner, new_name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        is_case_only = (
            src.parent.resolve() == dst.parent.resolve()
            and src.name.lower() == dst.name.lower()
        )
        if str(src) != str(dst):
            if is_case_only:
                staging = root / f".rename-{old_repo.repo_id}-{os.getpid()}"
                shutil.move(str(src), str(staging))
                shutil.move(str(staging), str(dst))
            else:
                shutil.move(str(src), str(dst))
                with contextlib.suppress(OSError):
                    src.parent.rmdir()
        res = run_git_command(
            dst,
            ["remote", "set-url", "--", "origin", clone_url],
            timeout=10,
            extra_config=extra_git_config,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "git remote set-url failed")
        return dst


def run_gc(
    root: Path,
    repo: RepoConfig,
    *,
    timeout_s: int = 1800,
    blocking_lock: bool = True,
    extra_git_config: Sequence[str] = (),
) -> GitResult:
    """Runs `git maintenance run --auto` on a managed bare clone under lock."""
    started = time.monotonic()
    target = git_dir(root, repo.owner, repo.name)
    if not target.exists():
        return GitResult(
            repo.repo_id,
            "",
            "",
            "",
            round(time.monotonic() - started, 3),
            "clone directory absent",
        )
    with _repo_lock(root, repo.repo_id, blocking=blocking_lock) as locked:
        if not locked:
            return GitResult(
                repo.repo_id,
                "",
                "",
                "",
                round(time.monotonic() - started, 3),
                "repo lock held",
            )
        _assert_safe_dir(root, repo.owner, repo.name)
        try:
            res = run_git_command(
                target,
                ["maintenance", "run", "--auto"],
                timeout=timeout_s,
                extra_config=extra_git_config,
            )
        except subprocess.TimeoutExpired:
            return GitResult(
                repo.repo_id,
                "",
                "",
                "",
                round(time.monotonic() - started, 3),
                "gc timed out",
                timed_out=True,
            )
        err = (
            ""
            if res.returncode == 0
            else (res.stderr.strip() or "git maintenance failed")
        )
        return GitResult(
            repo.repo_id, "", "", "", round(time.monotonic() - started, 3), err
        )


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root_dir, _dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root_dir)
        for name in files:
            fp = root_path / name
            if not fp.is_symlink():
                with contextlib.suppress(OSError):
                    total += fp.stat().st_size
    return total


def git_status(root: Path, repo: RepoConfig) -> dict[str, Any]:
    """Inspects local clone disk state for status reporting."""
    target = git_dir(root, repo.owner, repo.name)
    exists = target.is_dir()
    cloning = exists and (target / _CLONING_MARKER).exists()
    branch = repo.default_branch or "main"
    local_tip = _read_local_tip(target, branch) if exists else ""
    size_bytes = _dir_size_bytes(target) if exists else 0
    return {
        "git_dir": str(target),
        "exists": exists,
        "cloning": cloning,
        "local_tip": local_tip,
        "size_bytes": size_bytes,
    }


def _prune_owner_dir(
    owner_entry: Path, managed_paths: set[Path], *, dry_run: bool
) -> list[Path]:
    found: list[Path] = []
    for repo_entry in sorted(owner_entry.iterdir()):
        if (
            not repo_entry.name.endswith(".git")
            or repo_entry.is_symlink()
            or not repo_entry.is_dir()
        ):
            continue
        if repo_entry.resolve() not in managed_paths:
            found.append(repo_entry)
            if not dry_run:
                shutil.rmtree(repo_entry)
    if not dry_run:
        with contextlib.suppress(OSError):
            owner_entry.rmdir()
    return found


def prune_orphans(
    root: Path,
    known_repos: Iterable[RepoConfig],
    *,
    dry_run: bool = True,
) -> list[Path]:
    """Finds (and removes unless `dry_run`) clone dirs under `root` lacking a managed row."""
    if not root.is_dir() or root.is_symlink():
        return []
    managed_paths = {
        git_dir(root, r.owner, r.name).resolve()
        for r in known_repos
        if r.git_source == "managed"
    }
    orphans: list[Path] = []
    for owner_entry in sorted(root.iterdir()):
        if (
            owner_entry.name.startswith(".")
            or not owner_entry.is_dir()
            or owner_entry.is_symlink()
        ):
            continue
        orphans.extend(_prune_owner_dir(owner_entry, managed_paths, dry_run=dry_run))
    return orphans
