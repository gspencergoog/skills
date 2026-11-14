"""Isolated git subprocess helpers for packrat.

This leaf module imports only the standard library and `sync_runtime`, so both
`managed_git` and store/indexer modules can run bounded git commands without
import cycles or accidental dependence on a user's `~/.gitconfig`.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path

try:
    from .sync_runtime import run_bounded_subprocess
except ImportError:
    from sync_runtime import run_bounded_subprocess  # type: ignore[no-redef]

_SCP_REMOTE_RE = re.compile(r"^(?:[^:/\s]+@)?(?:\[([^\]]+)\]|([^@:/\s]+)):(.+)$")
_BASE_BATCH_SSH_CMD = (
    "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
)

GIT_ISOLATED_ENV: dict[str, str] = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_PROTOCOL_FROM_USER": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "PAGER": "cat",
}
"""Environment overrides preventing interactive prompts and `~/.gitconfig` side effects."""

GIT_ISOLATED_CONFIG_ARGS: tuple[str, ...] = (
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "protocol.ssh.allow=always",
    "-c",
    "credential.helper=",
    "-c",
    "core.hooksPath=/dev/null",
)
"""Git `-c` overrides enforcing allowed protocols, no credential helper, and no hooks."""


def _extract_remote_host(remote_url: str) -> str | None:
    """Extracts the lowercase remote hostname from an SSH, HTTPS, or SCP-style git URL."""
    cleaned = remote_url.strip()
    if not cleaned:
        return None
    if "://" in cleaned:
        hostname = urllib.parse.urlsplit(cleaned).hostname
        return hostname.lower() if hostname else None
    scp_match = _SCP_REMOTE_RE.match(cleaned)
    if not scp_match:
        return None
    host = (scp_match.group(1) or scp_match.group(2) or "").lower()
    rest = scp_match.group(3)
    if len(host) == 1 and host.isalpha() and rest.startswith(("/", "\\")):
        return None
    return host or None


def build_git_ssh_env(
    remote_url: str | None = None,
    ssh_key_path: Path | None = None,
) -> dict[str, str]:
    """Builds non-interactive git and SSH environment variables for a remote URL.

    Preserves a caller-supplied `GIT_SSH_COMMAND` when already set in the process
    environment. Otherwise configures batch SSH flags and appends `-i <ssh_key_path>
    -o IdentitiesOnly=yes` only when the remote host equals `github.com` and the
    SSH key exists on disk.
    """
    env: dict[str, str] = {
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "PAGER": "cat",
    }
    existing_ssh_cmd = os.environ.get("GIT_SSH_COMMAND", "").strip()
    if existing_ssh_cmd:
        env["GIT_SSH_COMMAND"] = os.environ["GIT_SSH_COMMAND"]
        return env

    ssh_cmd = _BASE_BATCH_SSH_CMD
    key_path = (
        ssh_key_path.expanduser()
        if ssh_key_path is not None
        else Path.home() / ".ssh" / "github"
    )
    if (
        remote_url
        and _extract_remote_host(remote_url) == "github.com"
        and key_path.exists()
    ):
        ssh_cmd = f"{ssh_cmd} -i {shlex.quote(str(key_path))} -o IdentitiesOnly=yes"
    env["GIT_SSH_COMMAND"] = ssh_cmd
    return env


_build_git_ssh_env = build_git_ssh_env


def run_git_command(
    repo_dir: Path | None,
    args: Sequence[str],
    timeout: int = 20,
    extra_env: Mapping[str, str] | None = None,
    *,
    isolated: bool = True,
    extra_config: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Executes git in a bounded process group with isolated config by default.

    Args:
        repo_dir: Optional directory passed to `git -C`; pass `None` for
            remote-only commands such as `ls-remote` or `init --bare`.
        args: Git subcommand and arguments.
        timeout: Maximum wall seconds before killing the process group.
        extra_env: Additional environment overrides (such as SSH command vars).
        isolated: When True, strips user/system gitconfig, restricts protocols
            to `https`/`ssh`, disables credential helpers, and clears hooks.
        extra_config: Extra `-c key=val` pairs appended after isolated config
            (e.g. `("-c", "protocol.file.allow=always")` in local test fixtures).

    Raises:
        subprocess.TimeoutExpired: When execution exceeds `timeout` after killing
            the spawned process group.
    """
    env: dict[str, str] = dict(os.environ)
    if isolated:
        env.update(GIT_ISOLATED_ENV)
    else:
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["PAGER"] = "cat"
    if extra_env:
        env.update(extra_env)

    cmd: list[str] = ["git"]
    if isolated:
        cmd.extend(GIT_ISOLATED_CONFIG_ARGS)
    if extra_config:
        cmd.extend(extra_config)
    if repo_dir is not None:
        cmd.extend(["-C", str(repo_dir)])
    cmd.append("--no-pager")
    cmd.extend(args)

    res = run_bounded_subprocess(cmd, env=env, timeout=timeout)
    if res.timed_out:
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output=res.stdout,
            stderr=res.stderr,
        )
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=res.returncode,
        stdout=res.stdout,
        stderr=res.stderr,
    )
