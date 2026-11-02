"""Manages background packrat sync timers via systemd --user (Linux) or launchd (macOS)."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from db_store import DEFAULT_DB_PATH

SERVICE_NAME = "packrat-sync.service"
TIMER_NAME = "packrat-sync.timer"
LAUNCHD_LABEL = "io.packrat.sync"
LAUNCHD_PLIST_NAME = f"{LAUNCHD_LABEL}.plist"

DEFAULT_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
DEFAULT_LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"

_INTERVAL_RE = re.compile(r"^OnUnitActiveSec=(\d+)min\s*$", re.MULTILINE)


@dataclass(frozen=True)
class CmdResult:
    """Result of a service-manager command execution."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class AutoSyncStatus:
    """Current configuration and runtime status of the packrat auto-sync timer."""

    enabled: bool
    active: bool
    interval_min: int | None
    fetch: bool
    backend: str
    timer_unit_path: str
    service_unit_path: str
    detail: str = ""


CmdRunner = Callable[[Sequence[str]], CmdResult]


def _default_backend() -> str:
    """Selects launchd on macOS and systemd-user on Linux."""
    return "launchd" if sys.platform == "darwin" else "systemd-user"


def _build_portable_path() -> str:
    """Builds a portable PATH string for background service execution."""
    home = Path.home()
    standard = [
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    current = [p for p in os.environ.get("PATH", "").split(":") if p]
    merged = list(dict.fromkeys([*standard, *current]))
    return ":".join(merged)


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
    """Renders the systemd user service unit file for `packrat sync`."""
    py_exec = python_bin or sys.executable
    packrat_py = script_path or (Path(__file__).resolve().parent / "packrat.py")
    fetch_flag = " --fetch" if fetch else ""
    env_path = _build_portable_path()
    ssh_key = Path.home() / ".ssh" / "github"
    ssh_env = (
        f'Environment="GIT_SSH_COMMAND=ssh -i {ssh_key} -o IdentitiesOnly=yes"\n'
        if ssh_key.exists()
        else ""
    )
    return (
        "[Unit]\n"
        "Description=Incremental sync of packrat multi-repository SQL/vector mirror\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f'Environment="PATH={env_path}"\n'
        f"{ssh_env}"
        f'ExecStart="{py_exec}" "{packrat_py}" --db "{db_path}" sync{fetch_flag} --json\n'
        "Nice=10\n"
    )


def render_timer_unit(interval_min: int = 10) -> str:
    """Renders the systemd user timer unit file for `packrat sync`."""
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
    """Renders the macOS launchd property list XML bytes for `packrat sync`."""
    if interval_min < 1 or interval_min > 1440:
        raise ValueError("interval_min must be between 1 and 1440 minutes.")
    py_exec = python_bin or sys.executable
    packrat_py = str(script_path or (Path(__file__).resolve().parent / "packrat.py"))
    prog_args = [py_exec, packrat_py, "--db", str(db_path), "sync"]
    if fetch:
        prog_args.append("--fetch")
    prog_args.append("--json")

    env_vars: dict[str, str] = {"PATH": _build_portable_path()}
    ssh_key = Path.home() / ".ssh" / "github"
    if ssh_key.exists():
        env_vars["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o IdentitiesOnly=yes"

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
) -> tuple[int | None, bool]:
    """Extracts (interval_min, fetch) from systemd unit files on disk."""
    interval_min: int | None = None
    fetch = False
    if timer_path.exists():
        timer_text = timer_path.read_text(encoding="utf-8", errors="replace")
        match = _INTERVAL_RE.search(timer_text)
        if match:
            interval_min = int(match.group(1))
    if service_path.exists():
        service_text = service_path.read_text(encoding="utf-8", errors="replace")
        fetch = " --fetch" in service_text
    return interval_min, fetch


def _parse_launchd_metadata(plist_path: Path) -> tuple[int | None, bool]:
    """Extracts (interval_min, fetch) from a macOS launchd plist file."""
    if not plist_path.exists():
        return None, False
    try:
        data = plistlib.loads(plist_path.read_bytes())
        sec = int(data.get("StartInterval", 0))
        interval_min = max(1, sec // 60) if sec > 0 else None
        args = data.get("ProgramArguments") or []
        return interval_min, ("--fetch" in args)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None, False


def _status_launchd(
    launchd_dir: Path,
    runner: CmdRunner,
) -> AutoSyncStatus:
    """Checks macOS launchd agent status."""
    plist_path = launchd_dir / LAUNCHD_PLIST_NAME
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
        )
    res = runner(["list", LAUNCHD_LABEL])
    active = res.returncode == 0
    detail = "loaded" if active else (res.stderr or "unloaded")
    return AutoSyncStatus(
        enabled=True,
        active=active,
        interval_min=interval_min,
        fetch=fetch,
        backend="launchd",
        timer_unit_path=str(plist_path),
        service_unit_path=str(plist_path),
        detail=detail,
    )


def get_auto_sync_status(
    systemd_dir: Path = DEFAULT_SYSTEMD_USER_DIR,
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
    backend: str | None = None,
    runner: CmdRunner | None = None,
) -> AutoSyncStatus:
    """Inspects timer unit or launchd plist files and queries runtime status."""
    active_backend = backend or _default_backend()
    if active_backend == "launchd":
        return _status_launchd(launchd_dir, runner or run_launchctl)

    active_runner = runner or run_systemctl_user
    service_path = systemd_dir / SERVICE_NAME
    timer_path = systemd_dir / TIMER_NAME
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
        )

    en_res = active_runner(["is-enabled", TIMER_NAME])
    ac_res = active_runner(["is-active", TIMER_NAME])
    enabled = en_res.returncode == 0 and en_res.stdout == "enabled"
    active = ac_res.returncode == 0 and ac_res.stdout == "active"
    detail = (
        f"is-enabled={en_res.stdout or en_res.stderr or 'unknown'}, "
        f"is-active={ac_res.stdout or ac_res.stderr or 'unknown'}"
    )
    return AutoSyncStatus(
        enabled=enabled,
        active=active,
        interval_min=interval_min,
        fetch=fetch,
        backend="systemd-user",
        timer_unit_path=str(timer_path),
        service_unit_path=str(service_path),
        detail=detail,
    )


def enable_auto_sync(
    db_path: Path = DEFAULT_DB_PATH,
    interval_min: int = 10,
    fetch: bool = True,
    systemd_dir: Path = DEFAULT_SYSTEMD_USER_DIR,
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
    backend: str | None = None,
    runner: CmdRunner | None = None,
) -> AutoSyncStatus:
    """Writes service/timer files and enables background sync on Linux or macOS."""
    if interval_min < 1 or interval_min > 1440:
        raise ValueError("interval_min must be between 1 and 1440 minutes.")
    active_backend = backend or _default_backend()
    resolved_db = db_path.expanduser().resolve()

    if active_backend == "launchd":
        active_runner = runner or run_launchctl
        launchd_dir.mkdir(parents=True, exist_ok=True)
        plist_path = launchd_dir / LAUNCHD_PLIST_NAME
        plist_path.write_bytes(
            render_launchd_plist(resolved_db, interval_min=interval_min, fetch=fetch)
        )
        active_runner(["unload", "-w", str(plist_path)])
        load_res = active_runner(["load", "-w", str(plist_path)])
        if load_res.returncode != 0:
            raise RuntimeError(f"launchctl load -w {plist_path} failed: {load_res.stderr}")
        return _status_launchd(launchd_dir, active_runner)

    active_runner = runner or run_systemctl_user
    systemd_dir.mkdir(parents=True, exist_ok=True)
    service_path = systemd_dir / SERVICE_NAME
    timer_path = systemd_dir / TIMER_NAME

    service_path.write_text(
        render_service_unit(resolved_db, fetch=fetch),
        encoding="utf-8",
    )
    timer_path.write_text(render_timer_unit(interval_min=interval_min), encoding="utf-8")

    reload_res = active_runner(["daemon-reload"])
    if reload_res.returncode != 0:
        raise RuntimeError(f"systemctl --user daemon-reload failed: {reload_res.stderr}")

    enable_res = active_runner(["enable", "--now", TIMER_NAME])
    if enable_res.returncode != 0:
        raise RuntimeError(f"systemctl --user enable --now {TIMER_NAME} failed: {enable_res.stderr}")

    return get_auto_sync_status(systemd_dir=systemd_dir, backend="systemd-user", runner=active_runner)


def disable_auto_sync(
    systemd_dir: Path = DEFAULT_SYSTEMD_USER_DIR,
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
    backend: str | None = None,
    runner: CmdRunner | None = None,
) -> AutoSyncStatus:
    """Stops/disables the background sync timer and removes the unit or plist files."""
    active_backend = backend or _default_backend()
    if active_backend == "launchd":
        active_runner = runner or run_launchctl
        plist_path = launchd_dir / LAUNCHD_PLIST_NAME
        if plist_path.exists():
            active_runner(["unload", "-w", str(plist_path)])
            os.unlink(plist_path)
        return _status_launchd(launchd_dir, active_runner)

    active_runner = runner or run_systemctl_user
    service_path = systemd_dir / SERVICE_NAME
    timer_path = systemd_dir / TIMER_NAME

    active_runner(["disable", "--now", TIMER_NAME])
    if timer_path.exists():
        os.unlink(timer_path)
    if service_path.exists():
        os.unlink(service_path)
    active_runner(["daemon-reload"])

    return get_auto_sync_status(systemd_dir=systemd_dir, backend="systemd-user", runner=active_runner)
