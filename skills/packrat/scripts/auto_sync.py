"""Manages background packrat sync timers via systemd --user (Linux) or launchd (macOS)."""

from __future__ import annotations

import contextlib
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from db_store import DEFAULT_DB_PATH
from sync_runtime import AutoSyncRunRecord, read_auto_sync_run_state

SERVICE_NAME = "packrat-sync.service"
TIMER_NAME = "packrat-sync.timer"
LAUNCHD_LABEL = "io.packrat.sync"
LAUNCHD_PLIST_NAME = f"{LAUNCHD_LABEL}.plist"

DEFAULT_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
DEFAULT_LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"

_INTERVAL_RE = re.compile(r"^OnUnitActiveSec=(\d+)min\s*$", re.MULTILINE)


@dataclass(frozen=True)
class CmdResult:
    """Result of a service-manager command execution.

    Attributes:
        returncode: Process exit status code.
        stdout: Trimmed standard output text.
        stderr: Trimmed standard error text.
    """

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class AutoSyncStatus:
    """Current configuration and runtime status of the packrat auto-sync timer.

    Attributes:
        enabled: Whether the timer unit or launchd agent is installed and enabled.
        active: Whether the timer unit or launchd agent is currently active.
        interval_min: Configured interval in minutes, or None when uninstalled.
        fetch: Whether background sync runs fetch remote git refs.
        backend: Active scheduler backend ("systemd-user" or "launchd").
        timer_unit_path: Path to the timer unit or launchd plist file.
        service_unit_path: Path to the service unit or launchd plist file.
        detail: Diagnostic summary from systemctl or launchctl.
        action: Operation outcome ("status", "enabled", "already_enabled",
            "updated", or "disabled").
        service_state: Most recent service state (for example "failed" or "inactive").
        last_run: Persisted summary of the latest background run, if present.
    """

    enabled: bool
    active: bool
    interval_min: int | None
    fetch: bool
    backend: str
    timer_unit_path: str
    service_unit_path: str
    detail: str = ""
    action: str = "status"
    service_state: str = ""
    last_run: AutoSyncRunRecord | None = None


CmdRunner = Callable[[Sequence[str]], CmdResult]


def _default_backend() -> str:
    """Selects launchd on macOS and systemd-user on Linux."""
    return "launchd" if sys.platform == "darwin" else "systemd-user"


def _build_portable_path() -> str:
    """Builds a portable PATH string for background service execution."""
    home = Path.home()
    standard = [
        str(home / ".local" / "bin"),
        str(home / ".local" / "share" / "mise" / "shims"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    current = [p for p in os.environ.get("PATH", "").split(":") if p]
    merged = list(dict.fromkeys([*standard, *current]))
    return ":".join(merged)


def _atomic_write_bytes(target_path: Path, content: bytes) -> None:
    """Writes content to target_path atomically via a temporary sibling file."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=target_path.parent,
        prefix=f".{target_path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(tmp_fd, 0o644)
        with os.fdopen(tmp_fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target_path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _file_matches_bytes(path: Path, expected: bytes) -> bool:
    """Returns whether path exists on disk with exact bytes matching expected."""
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def run_systemctl_user(args: Sequence[str]) -> CmdResult:
    """Executes `systemctl --user <args>` with a 10-second timeout."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return CmdResult(
            returncode=proc.returncode,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CmdResult(returncode=1, stdout="", stderr=str(exc))


def run_launchctl(args: Sequence[str]) -> CmdResult:
    """Executes `launchctl <args>` on macOS with a 10-second timeout."""
    try:
        proc = subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return CmdResult(
            returncode=proc.returncode,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CmdResult(returncode=1, stdout="", stderr=str(exc))


def render_service_unit(
    db_path: Path,
    fetch: bool = True,
    python_bin: str | None = None,
    script_path: Path | None = None,
) -> str:
    """Renders the systemd user service unit file for `packrat sync --auto`.

    Args:
        db_path: Path to the target SQLite database file.
        fetch: Whether to append `--fetch` to the background sync command.
        python_bin: Optional Python interpreter path override.
        script_path: Optional `packrat.py` entrypoint path override.

    Returns:
        Rendered `packrat-sync.service` unit contents.
    """
    py_exec = python_bin or sys.executable
    packrat_py = script_path or (Path(__file__).resolve().parent / "packrat.py")
    fetch_flag = " --fetch" if fetch else ""
    env_path = _build_portable_path()
    return (
        "[Unit]\n"
        "Description=Incremental sync of packrat multi-repository SQL/vector mirror\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f'Environment="PATH={env_path}"\n'
        'Environment="GH_PROMPT_DISABLED=1" "GIT_TERMINAL_PROMPT=0" "GCM_INTERACTIVE=never"\n'
        f'ExecStart="{py_exec}" "{packrat_py}" --db "{db_path}" sync --auto{fetch_flag} --json\n'
        "TimeoutStartSec=300\n"
        "TimeoutStopSec=10\n"
        "KillMode=control-group\n"
        "Nice=10\n"
    )


def render_timer_unit(interval_min: int = 10) -> str:
    """Renders the systemd user timer unit file for `packrat sync`.

    Args:
        interval_min: Timer interval in minutes (1 to 1440).

    Returns:
        Rendered `packrat-sync.timer` unit contents.

    Raises:
        ValueError: If `interval_min` is outside `[1, 1440]`.
    """
    if interval_min < 1 or interval_min > 1440:
        raise ValueError("interval_min must be between 1 and 1440 minutes.")
    return (
        "[Unit]\n"
        "Description=Periodic incremental sync timer for packrat\n\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        f"OnUnitActiveSec={interval_min}min\n"
        "RandomizedDelaySec=30s\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def render_launchd_plist(
    db_path: Path,
    interval_min: int = 10,
    fetch: bool = True,
    python_bin: str | None = None,
    script_path: Path | None = None,
) -> bytes:
    """Renders the macOS launchd property list XML bytes for `packrat sync --auto`.

    Args:
        db_path: Path to the target SQLite database file.
        interval_min: Launch interval in minutes (1 to 1440).
        fetch: Whether to append `--fetch` to `ProgramArguments`.
        python_bin: Optional Python interpreter path override.
        script_path: Optional `packrat.py` entrypoint path override.

    Returns:
        UTF-8 XML plist bytes for `io.packrat.sync.plist`.

    Raises:
        ValueError: If `interval_min` is outside `[1, 1440]`.
    """
    if interval_min < 1 or interval_min > 1440:
        raise ValueError("interval_min must be between 1 and 1440 minutes.")
    py_exec = python_bin or sys.executable
    packrat_py = str(script_path or (Path(__file__).resolve().parent / "packrat.py"))
    prog_args = [py_exec, packrat_py, "--db", str(db_path), "sync", "--auto"]
    if fetch:
        prog_args.append("--fetch")
    prog_args.append("--json")

    env_vars: dict[str, str] = {
        "PATH": _build_portable_path(),
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
    }

    log_path = str(db_path.parent / "auto-sync.log")
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": prog_args,
        "StartInterval": interval_min * 60,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "EnvironmentVariables": env_vars,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    return plistlib.dumps(payload)


def _parse_systemd_metadata(
    service_path: Path,
    timer_path: Path,
) -> tuple[int | None, bool | None]:
    """Extracts `(interval_min, fetch)` from systemd unit files when present on disk."""
    interval_min: int | None = None
    fetch: bool | None = None
    if timer_path.exists():
        timer_text = timer_path.read_text(encoding="utf-8", errors="replace")
        match = _INTERVAL_RE.search(timer_text)
        if match:
            interval_min = int(match.group(1))
    if service_path.exists():
        service_text = service_path.read_text(encoding="utf-8", errors="replace")
        fetch = " --fetch" in service_text
    return interval_min, fetch


def _parse_launchd_metadata(plist_path: Path) -> tuple[int | None, bool | None]:
    """Extracts `(interval_min, fetch)` from a macOS launchd plist file when present."""
    if not plist_path.exists():
        return None, None
    try:
        data = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None, None
    if not isinstance(data, dict):
        return None, None
    try:
        sec = int(data.get("StartInterval", 0))
    except (TypeError, ValueError):
        sec = 0
    interval_min = max(1, sec // 60) if sec > 0 else None
    args = data.get("ProgramArguments")
    fetch = ("--fetch" in args) if isinstance(args, list) else None
    return interval_min, fetch


def _resolve_sync_options(
    interval_min: int | None,
    fetch: bool | None,
    existing_interval: int | None,
    existing_fetch: bool | None,
) -> tuple[int, bool]:
    """Merges requested auto-sync options with on-disk metadata and defaults."""
    default_interval = 10 if existing_interval is None else existing_interval
    resolved_interval = default_interval if interval_min is None else interval_min
    if resolved_interval < 1 or resolved_interval > 1440:
        raise ValueError("interval_min must be between 1 and 1440 minutes.")
    default_fetch = True if existing_fetch is None else existing_fetch
    resolved_fetch = default_fetch if fetch is None else fetch
    return resolved_interval, resolved_fetch


def _query_systemd_runtime(runner: CmdRunner) -> tuple[bool, bool, bool, str, str]:
    """Queries enabled, active, and failed states from `systemctl --user`."""
    en_res = runner(["is-enabled", TIMER_NAME])
    ac_res = runner(["is-active", TIMER_NAME])
    fail_res = runner(["is-failed", SERVICE_NAME])
    enabled = en_res.returncode == 0 and en_res.stdout == "enabled"
    active = ac_res.returncode == 0 and ac_res.stdout == "active"
    fail_out = fail_res.stdout.strip()
    is_failed = fail_res.returncode == 0 or fail_out == "failed"
    service_state = "failed" if is_failed else fail_out
    detail = (
        f"is-enabled={en_res.stdout or en_res.stderr or 'unknown'}, "
        f"is-active={ac_res.stdout or ac_res.stderr or 'unknown'}"
    )
    if is_failed:
        detail = f"{detail}, service=failed"
    return enabled, active, is_failed, service_state, detail


def _status_launchd(
    launchd_dir: Path,
    runner: CmdRunner,
    *,
    db_path: Path = DEFAULT_DB_PATH,
    action: str = "status",
) -> AutoSyncStatus:
    """Checks macOS launchd agent status and loads persisted run state."""
    plist_path = launchd_dir / LAUNCHD_PLIST_NAME
    last_run = read_auto_sync_run_state(db_path)
    interval_min, fetch = _parse_launchd_metadata(plist_path)
    if not plist_path.exists():
        return AutoSyncStatus(
            enabled=False,
            active=False,
            interval_min=None,
            fetch=False,
            backend="launchd",
            timer_unit_path=str(plist_path),
            service_unit_path=str(plist_path),
            detail="not installed",
            action=action,
            service_state="",
            last_run=last_run,
        )
    res = runner(["list", LAUNCHD_LABEL])
    active = res.returncode == 0
    detail = "loaded" if active else (res.stderr or "unloaded")
    return AutoSyncStatus(
        enabled=True,
        active=active,
        interval_min=interval_min,
        fetch=bool(fetch),
        backend="launchd",
        timer_unit_path=str(plist_path),
        service_unit_path=str(plist_path),
        detail=detail,
        action=action,
        service_state="loaded" if active else "unloaded",
        last_run=last_run,
    )


def _status_systemd(
    systemd_dir: Path,
    runner: CmdRunner,
    *,
    db_path: Path = DEFAULT_DB_PATH,
    action: str = "status",
) -> AutoSyncStatus:
    """Checks Linux `systemd --user` timer status and loads persisted run state."""
    service_path = systemd_dir / SERVICE_NAME
    timer_path = systemd_dir / TIMER_NAME
    last_run = read_auto_sync_run_state(db_path)
    interval_min, fetch = _parse_systemd_metadata(service_path, timer_path)
    if not timer_path.exists():
        return AutoSyncStatus(
            enabled=False,
            active=False,
            interval_min=None,
            fetch=False,
            backend="systemd-user",
            timer_unit_path=str(timer_path),
            service_unit_path=str(service_path),
            detail="not installed",
            action=action,
            service_state="",
            last_run=last_run,
        )
    enabled, active, _, service_state, detail = _query_systemd_runtime(runner)
    return AutoSyncStatus(
        enabled=enabled,
        active=active,
        interval_min=interval_min,
        fetch=bool(fetch),
        backend="systemd-user",
        timer_unit_path=str(timer_path),
        service_unit_path=str(service_path),
        detail=detail,
        action=action,
        service_state=service_state,
        last_run=last_run,
    )


def get_auto_sync_status(
    db_path: Path = DEFAULT_DB_PATH,
    systemd_dir: Path = DEFAULT_SYSTEMD_USER_DIR,
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
    backend: str | None = None,
    runner: CmdRunner | None = None,
) -> AutoSyncStatus:
    """Inspects timer unit or launchd plist files and queries runtime status.

    Args:
        db_path: Path to the target SQLite database file (for reading `last_run`).
        systemd_dir: Directory containing `systemd --user` unit files.
        launchd_dir: Directory containing macOS `LaunchAgents` plist files.
        backend: Optional scheduler backend override ("systemd-user" or "launchd").
        runner: Optional command runner override for testing.

    Returns:
        An `AutoSyncStatus` snapshot including the latest recorded background run.
    """
    resolved_db = db_path.expanduser().resolve()
    active_backend = backend or _default_backend()
    if active_backend == "launchd":
        return _status_launchd(
            launchd_dir,
            runner or run_launchctl,
            db_path=resolved_db,
        )
    return _status_systemd(
        systemd_dir,
        runner or run_systemctl_user,
        db_path=resolved_db,
    )


def _enable_launchd(
    resolved_db: Path,
    interval_min: int | None,
    fetch: bool | None,
    launchd_dir: Path,
    runner: CmdRunner,
) -> AutoSyncStatus:
    """Idempotently installs or updates the macOS launchd plist for auto-sync."""
    plist_path = launchd_dir / LAUNCHD_PLIST_NAME
    had_existing = plist_path.exists()
    existing_interval, existing_fetch = _parse_launchd_metadata(plist_path)
    resolved_interval, resolved_fetch = _resolve_sync_options(
        interval_min,
        fetch,
        existing_interval,
        existing_fetch,
    )
    plist_bytes = render_launchd_plist(
        resolved_db,
        interval_min=resolved_interval,
        fetch=resolved_fetch,
    )
    was_active = had_existing and runner(["list", LAUNCHD_LABEL]).returncode == 0
    if had_existing and was_active and _file_matches_bytes(plist_path, plist_bytes):
        return AutoSyncStatus(
            enabled=True,
            active=True,
            interval_min=resolved_interval,
            fetch=resolved_fetch,
            backend="launchd",
            timer_unit_path=str(plist_path),
            service_unit_path=str(plist_path),
            detail="loaded",
            action="already_enabled",
            service_state="loaded",
            last_run=read_auto_sync_run_state(resolved_db),
        )

    if had_existing:
        runner(["unload", "-w", str(plist_path)])
    _atomic_write_bytes(plist_path, plist_bytes)
    load_res = runner(["load", "-w", str(plist_path)])
    if load_res.returncode != 0:
        raise RuntimeError(f"launchctl load -w {plist_path} failed: {load_res.stderr}")
    action = "updated" if had_existing else "enabled"
    return _status_launchd(launchd_dir, runner, db_path=resolved_db, action=action)


def _apply_systemd_changes(
    runner: CmdRunner,
    *,
    was_failed: bool,
    was_active: bool,
) -> None:
    """Reloads systemd user units, resets failures, and starts or restarts the timer."""
    if was_failed:
        runner(["reset-failed", SERVICE_NAME])
    reload_res = runner(["daemon-reload"])
    if reload_res.returncode != 0:
        msg = f"systemctl --user daemon-reload failed: {reload_res.stderr}"
        raise RuntimeError(msg)
    enable_res = runner(["enable", "--now", TIMER_NAME])
    if enable_res.returncode != 0:
        msg = f"systemctl --user enable --now {TIMER_NAME} failed: {enable_res.stderr}"
        raise RuntimeError(msg)
    if was_active:
        restart_res = runner(["restart", TIMER_NAME])
        if restart_res.returncode != 0:
            msg = f"systemctl --user restart {TIMER_NAME} failed: {restart_res.stderr}"
            raise RuntimeError(msg)


def _enable_systemd(
    resolved_db: Path,
    interval_min: int | None,
    fetch: bool | None,
    systemd_dir: Path,
    runner: CmdRunner,
) -> AutoSyncStatus:
    """Idempotently installs or updates `systemd --user` units for auto-sync."""
    service_path = systemd_dir / SERVICE_NAME
    timer_path = systemd_dir / TIMER_NAME
    had_existing = service_path.exists() or timer_path.exists()
    old_interval, old_fetch = _parse_systemd_metadata(service_path, timer_path)
    resolved_interval, resolved_fetch = _resolve_sync_options(
        interval_min,
        fetch,
        old_interval,
        old_fetch,
    )
    service_text = render_service_unit(resolved_db, fetch=resolved_fetch)
    service_bytes = service_text.encode("utf-8")
    timer_bytes = render_timer_unit(interval_min=resolved_interval).encode("utf-8")

    was_en, was_ac, was_fail, svc_state, detail = _query_systemd_runtime(runner)
    service_matches = _file_matches_bytes(service_path, service_bytes)
    timer_matches = _file_matches_bytes(timer_path, timer_bytes)
    units_match = service_matches and timer_matches
    if was_en and was_ac and not was_fail and units_match:
        return AutoSyncStatus(
            enabled=True,
            active=True,
            interval_min=resolved_interval,
            fetch=resolved_fetch,
            backend="systemd-user",
            timer_unit_path=str(timer_path),
            service_unit_path=str(service_path),
            detail=detail,
            action="already_enabled",
            service_state=svc_state,
            last_run=read_auto_sync_run_state(resolved_db),
        )

    if not service_matches:
        _atomic_write_bytes(service_path, service_bytes)
    if not timer_matches:
        _atomic_write_bytes(timer_path, timer_bytes)
    _apply_systemd_changes(runner, was_failed=was_fail, was_active=was_ac)
    action = "updated" if had_existing else "enabled"
    return _status_systemd(systemd_dir, runner, db_path=resolved_db, action=action)


def enable_auto_sync(
    db_path: Path = DEFAULT_DB_PATH,
    interval_min: int | None = None,
    fetch: bool | None = None,
    systemd_dir: Path = DEFAULT_SYSTEMD_USER_DIR,
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
    backend: str | None = None,
    runner: CmdRunner | None = None,
) -> AutoSyncStatus:
    """Installs or updates background sync units idempotently on Linux or macOS.

    Preserves existing `interval_min` and `fetch` values on disk when omitted
    (defaulting to `10` minutes and `fetch=True` on initial installation), and
    skips file writes and service reloads when already active and byte-identical.

    Args:
        db_path: Path to the target SQLite database file.
        interval_min: Optional timer interval in minutes (1 to 1440).
        fetch: Optional flag controlling remote git ref fetches.
        systemd_dir: Directory containing `systemd --user` unit files.
        launchd_dir: Directory containing macOS `LaunchAgents` plist files.
        backend: Optional scheduler backend override ("systemd-user" or "launchd").
        runner: Optional command runner override for testing.

    Returns:
        Updated `AutoSyncStatus` with `action` set to `"enabled"`,
        `"already_enabled"`, or `"updated"`.

    Raises:
        ValueError: If the resolved `interval_min` is outside `[1, 1440]`.
        RuntimeError: If `systemctl` or `launchctl` fails to enable the timer.
    """
    active_backend = backend or _default_backend()
    resolved_db = db_path.expanduser().resolve()
    if active_backend == "launchd":
        return _enable_launchd(
            resolved_db,
            interval_min,
            fetch,
            launchd_dir,
            runner or run_launchctl,
        )
    return _enable_systemd(
        resolved_db,
        interval_min,
        fetch,
        systemd_dir,
        runner or run_systemctl_user,
    )


def disable_auto_sync(
    db_path: Path = DEFAULT_DB_PATH,
    systemd_dir: Path = DEFAULT_SYSTEMD_USER_DIR,
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
    backend: str | None = None,
    runner: CmdRunner | None = None,
) -> AutoSyncStatus:
    """Stops and disables the background sync timer and removes unit or plist files.

    Args:
        db_path: Path to the target SQLite database file (for reading `last_run`).
        systemd_dir: Directory containing `systemd --user` unit files.
        launchd_dir: Directory containing macOS `LaunchAgents` plist files.
        backend: Optional scheduler backend override ("systemd-user" or "launchd").
        runner: Optional command runner override for testing.

    Returns:
        An `AutoSyncStatus` snapshot with `action="disabled"`.
    """
    resolved_db = db_path.expanduser().resolve()
    active_backend = backend or _default_backend()
    if active_backend == "launchd":
        active_runner = runner or run_launchctl
        plist_path = launchd_dir / LAUNCHD_PLIST_NAME
        if plist_path.exists():
            active_runner(["unload", "-w", str(plist_path)])
            os.unlink(plist_path)
        return _status_launchd(
            launchd_dir,
            active_runner,
            db_path=resolved_db,
            action="disabled",
        )

    active_runner = runner or run_systemctl_user
    service_path = systemd_dir / SERVICE_NAME
    timer_path = systemd_dir / TIMER_NAME

    active_runner(["disable", "--now", TIMER_NAME])
    if timer_path.exists():
        os.unlink(timer_path)
    if service_path.exists():
        os.unlink(service_path)
    active_runner(["daemon-reload"])

    return _status_systemd(
        systemd_dir,
        active_runner,
        db_path=resolved_db,
        action="disabled",
    )
