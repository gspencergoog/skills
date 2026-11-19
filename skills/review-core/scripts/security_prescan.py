#!/usr/bin/env python3
"""Screens a change for local execution hazards before reviewer tools run.

Review skills run linters, build queries, and tests against changes that may
come from an untrusted author. This module holds the guards those skills share:

- `scan_diff_execution_risks` reads a unified diff and reports the files and
  added lines that could run code on the reviewer's machine.
- `apply_hardened_git_env` and `HARDENED_GIT_CONFIG_ARGS` stop git child
  processes from running repository hooks or external diff drivers.
- `wrap_salted_untrusted` fences untrusted text in a tag whose name carries a
  random nonce, so text inside the fence cannot close it.

Run it as a script to scan a diff file (or `-` for stdin) and print the result
as JSON:

    python3 security_prescan.py <diff-file-or--> [--output <path>]

The script exits 0 after printing a scan result and 1 when the diff cannot be
read or the output file cannot be written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Git settings that would otherwise let a checkout run its own programs:
# repository hooks, the filesystem monitor hook, and external diff drivers.
#
# The empty `diff.external` replaces a configured driver with a command that
# cannot run. It does not restore the built-in diff: a `git diff` that prints a
# patch exits with "external diff died" unless it also passes `--no-ext-diff`.
_HARDENED_GIT_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("diff.external", ""),
)

# The same settings as `-c key=value` arguments. Place them right after `git`,
# and pass `--no-ext-diff` to every command that prints a patch:
# `["git", *HARDENED_GIT_CONFIG_ARGS, "diff", "--no-ext-diff", ...]`.
HARDENED_GIT_CONFIG_ARGS: tuple[str, ...] = tuple(
    arg for key, value in _HARDENED_GIT_CONFIG for arg in ("-c", f"{key}={value}")
)


def apply_hardened_git_env_in_place(env: MutableMapping[str, str]) -> None:
    """Appends the hardened git settings to `env` as command-scope config.

    Git reads `GIT_CONFIG_COUNT` and the numbered `GIT_CONFIG_KEY_<n>` and
    `GIT_CONFIG_VALUE_<n>` variables as config that outranks every config
    file. A git process started with `env` therefore never runs repository
    hooks, the filesystem monitor hook, or a configured external diff driver.

    The external diff driver is replaced by an empty command, not by the
    built-in diff. Pass `--no-ext-diff` to every `git diff` that prints a
    patch. Without the flag git exits with an error instead of printing one.

    Entries already in `env` keep their numbers and the new entries follow
    them. A missing, unparsable, or negative `GIT_CONFIG_COUNT` counts as zero.

    Args:
        env: The environment mapping to update.
    """
    try:
        base_count = max(0, int(env.get("GIT_CONFIG_COUNT", "0")))
    except ValueError:
        base_count = 0

    for offset, (key, value) in enumerate(_HARDENED_GIT_CONFIG):
        idx = base_count + offset
        env[f"GIT_CONFIG_KEY_{idx}"] = key
        env[f"GIT_CONFIG_VALUE_{idx}"] = value
    env["GIT_CONFIG_COUNT"] = str(base_count + len(_HARDENED_GIT_CONFIG))


def apply_hardened_git_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Returns a copy of an environment with the hardened git settings added.

    See `apply_hardened_git_env_in_place` for what the settings do and for the
    `--no-ext-diff` flag that patch-printing commands need.

    Args:
        base_env: The environment to copy. Defaults to `os.environ`.

    Returns:
        A new dict to pass as `env` to `subprocess`. `base_env` is unchanged.
    """
    env = dict(os.environ if base_env is None else base_env)
    apply_hardened_git_env_in_place(env)
    return env


_UNTRUSTED_TAG = "untrusted_repo_content"
_NONCE_RE = re.compile(r"[0-9A-Fa-f]{4,64}")
# An opening or closing fence tag, with or without a nonce suffix or a final
# `>`. The body stops at the next `<` so that one match never hides a second
# tag start.
_UNTRUSTED_TAG_RE = re.compile(rf"<(\s*/?\s*{_UNTRUSTED_TAG}[^<>]*)(>?)", re.IGNORECASE)
_TRUNCATION_MARK = "..."


def generate_untrusted_nonce() -> str:
    """Returns a fresh 8-character hexadecimal nonce for one fence tag name.

    Draw one nonce per prompt and pass it to every `wrap_salted_untrusted`
    call for that prompt. Text written before the nonce existed cannot name
    the tag, so it cannot forge the end of the fence.
    """
    return secrets.token_hex(4)


def _escape_fence_tags(text: str) -> str:
    """Replaces the angle brackets of every fence tag in `text` with entities."""

    def _escape(match: re.Match[str]) -> str:
        closer = "&gt;" if match.group(2) else ""
        return f"&lt;{match.group(1)}{closer}"

    return _UNTRUSTED_TAG_RE.sub(_escape, text)


def _escape_attribute(value: str) -> str:
    """Escapes `value` for use inside a double-quoted tag attribute."""
    collapsed = re.sub(r"\s+", " ", value).strip()
    return (
        collapsed.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _truncate(text: str, max_chars: int) -> str:
    """Cuts `text` to `max_chars` characters, ending with `...` when it fits."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= len(_TRUNCATION_MARK):
        return text[:max_chars]
    return text[: max_chars - len(_TRUNCATION_MARK)].rstrip() + _TRUNCATION_MARK


def wrap_salted_untrusted(
    source: str,
    text: str,
    *,
    nonce: str | None = None,
    max_chars: int = 0,
    multiline: bool = False,
) -> str:
    """Fences untrusted text in a nonce-named tag that the text cannot close.

    The result has the form
    `<untrusted_repo_content_<nonce> source="...">...</untrusted_repo_content_<nonce>>`.
    Every `untrusted_repo_content` tag inside `text`, opening or closing, with
    or without a nonce suffix, has its angle brackets replaced with `&lt;` and
    `&gt;`. A pull request body or CI log that contains
    `</untrusted_repo_content_deadbeef>` therefore stays inside the fence.

    Args:
        source: Where the text came from, such as `"pr_body"` or `"issue#7"`.
            It is escaped and written to the `source` attribute.
        text: The untrusted text. `None` is treated as empty.
        nonce: The tag suffix from `generate_untrusted_nonce`. Defaults to a
            fresh nonce, which suits a prompt with one fenced block. Pass the
            same nonce to every block of one prompt so the prompt can name the
            tag once.
        max_chars: The longest fenced text to keep, counted after escaping.
            Longer text is cut and ends with `...`. Zero keeps all of it.
        multiline: Whether to keep line breaks. When false, every run of
            whitespace becomes one space and the block is one line. When true,
            the text keeps its lines and sits on its own lines between the
            tags, which suits bodies and log tails.

    Returns:
        The fenced block.

    Raises:
        ValueError: `nonce` is not 4 to 64 hexadecimal characters.
    """
    tag_nonce = generate_untrusted_nonce() if nonce is None else nonce
    if not _NONCE_RE.fullmatch(tag_nonce):
        raise ValueError(
            f"nonce must be 4 to 64 hexadecimal characters, got {tag_nonce!r}"
        )
    raw = text or ""
    if multiline:
        body = raw.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    else:
        body = re.sub(r"\s+", " ", raw).strip()
    body = _truncate(_escape_fence_tags(body), max_chars)
    tag = f"{_UNTRUSTED_TAG}_{tag_nonce}"
    joint = "\n" if multiline else ""
    return f'<{tag} source="{_escape_attribute(source)}">{joint}{body}{joint}</{tag}>'


_RISKY_CONFIG_EXACT = frozenset(
    {
        "package.json",
        ".npmrc",
        "setup.py",
        "setup.cfg",
        "pyproject.toml",
        "tox.ini",
        "noxfile.py",
        "makefile",
        "gnumakefile",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "cargo.toml",
        "build.rs",
        "package.swift",
        "workspace",
        "workspace.bazel",
        "module.bazel",
        ".bazelrc",
        "build",
        "build.bazel",
        "pubspec.yaml",
        "build.yaml",
        "analysis_options.yaml",
        "conftest.py",
        "pytest.ini",
        ".gitattributes",
        ".gitmodules",
        ".pre-commit-config.yaml",
        ".envrc",
        ".mise.toml",
        ".tool-versions",
    }
)

_RISKY_CONFIG_PREFIXES = (
    ".eslintrc",
    "eslint.config.",
    "tsconfig",
    ".yarnrc",
)

_RISKY_CONFIG_SUFFIXES = (
    ".mk",
    ".gradle",
    ".gradle.kts",
    ".bzl",
)

_RISKY_PATH_SEGMENTS = (
    ".githooks/",
    ".husky/",
    ".github/workflows/",
    ".cargo/config",
)

_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/.+? b/(.+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_LINE_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "process_and_dynamic_execution",
        re.compile(
            r"\b(?:subprocess\b|os\.(?:system|popen|exec\w*|spawn\w*)|pty\.spawn|"
            r"eval\s*\(|exec\s*\(|compile\s*\(|__import__\s*\(|importlib\.import_module|"
            r"child_process|execSync|spawnSync|execFile\w*|new\s+Function\s*\(|"
            r"Process\.(?:run|runSync|start)|Isolate\.spawnUri|Command::new)\b"
        ),
        "Process spawn or dynamic code execution",
    ),
    (
        "network_and_exfiltration",
        re.compile(
            r"(?:\b(?:urllib\.request|requests\.(?:get|post|put|delete|request|Session)|"
            r"httpx\.|aiohttp\.|socket\.socket|fetch\s*\(|XMLHttpRequest|new\s+WebSocket|"
            r"http\.(?:get|post|request)|HttpClient|curl\s+|wget\s+)\b|"
            r"(?:~|\$HOME|/home/|/Users/)[/\w.-]*\.(?:ssh|aws|env|gemini|gnupg|netrc|npmrc|pypirc)\b|"
            r"\.config/gh\b|/etc/(?:passwd|shadow)\b|"
            r"\b(?:os\.environ|process\.env|Platform\.environment)\b)"
        ),
        "Network egress, sensitive path, or environment access",
    ),
    (
        "obfuscation_and_payloads",
        re.compile(
            r"(?:\b(?:base64\.b64decode|codecs\.decode|"
            r"Buffer\.from\s*\([^)]*['\"]base64['\"]|atob\s*\(|bytes\.fromhex)\b|"
            r"(?:\\x[0-9a-fA-F]{2}){8,})"
        ),
        "Encoded payload or obfuscation primitive",
    ),
)


def _check_config_file_risk(file_path: str) -> str | None:
    """Returns a risk reason when file_path is a build, linter plugin, or hook config."""
    norm = file_path.strip().replace("\\", "/")
    if not norm:
        return None
    lower_norm = norm.lower()
    base = lower_norm.rsplit("/", 1)[-1]
    if base in _RISKY_CONFIG_EXACT:
        return f"Modified build, hook, or tool config ({base})"
    if base.startswith(_RISKY_CONFIG_PREFIXES) or base.endswith(_RISKY_CONFIG_SUFFIXES):
        return f"Modified linter, compiler, or build config ({base})"
    for seg in _RISKY_PATH_SEGMENTS:
        if lower_norm.startswith(seg) or f"/{seg}" in lower_norm:
            return f"Modified hook or workflow path ({norm})"
    return None


def _extract_header_path(line: str) -> str | None:
    """Extracts the target file path from diff --git, rename to, or +++ b/ headers."""
    if line.startswith("diff --git "):
        m = _DIFF_GIT_HEADER_RE.match(line)
        return m.group(1).strip() if m else None
    if line.startswith("rename to "):
        return line[len("rename to ") :].strip() or None
    if line.startswith("+++ b/"):
        return line[6:].strip() or None
    return None


def _parse_hunk_new_line(line: str) -> int | None:
    """Extracts the starting new-file line number from a unified diff @@ header."""
    m = _HUNK_HEADER_RE.match(line)
    return int(m.group(1)) if m else None


def _scan_added_line(
    file_path: str, line_num: int, content: str
) -> list[dict[str, Any]]:
    """Matches an added diff line against execution, network, and obfuscation patterns."""
    matches: list[dict[str, Any]] = []
    snippet = content.strip()[:160]
    for category, pattern, reason in _LINE_RISK_PATTERNS:
        if pattern.search(content):
            matches.append(
                {
                    "category": category,
                    "file_path": file_path,
                    "line": line_num,
                    "reason": reason,
                    "snippet": snippet,
                }
            )
    return matches


def _record_signal(
    signal: dict[str, Any],
    signals: list[dict[str, Any]],
    categories: dict[str, int],
    flagged_files: list[str],
) -> None:
    """Appends a risk signal and updates category counts and ordered flagged_files."""
    signals.append(signal)
    cat = signal["category"]
    categories[cat] = categories.get(cat, 0) + 1
    fp = signal["file_path"]
    if fp and fp not in flagged_files:
        flagged_files.append(fp)


def _record_config_if_needed(
    current_file: str,
    raw_line: str,
    seen_config_files: set[str],
    signals: list[dict[str, Any]],
    categories: dict[str, int],
    flagged_files: list[str],
) -> None:
    """Records a build_and_hook_configs signal once per modified config file."""
    if current_file in seen_config_files:
        return
    config_reason = _check_config_file_risk(current_file)
    if not config_reason:
        return
    seen_config_files.add(current_file)
    _record_signal(
        {
            "category": "build_and_hook_configs",
            "file_path": current_file,
            "line": 1,
            "reason": config_reason,
            "snippet": raw_line[:160],
        },
        signals,
        categories,
        flagged_files,
    )


def _step_hunk_line(
    raw_line: str,
    current_file: str,
    new_line: int,
    signals: list[dict[str, Any]],
    categories: dict[str, int],
    flagged_files: list[str],
) -> int:
    """Processes one hunk body line and returns the updated new_line counter."""
    if raw_line.startswith(("+++", "-", "\\")):
        return new_line
    if raw_line.startswith("+"):
        for sig in _scan_added_line(current_file, new_line, raw_line[1:]):
            _record_signal(sig, signals, categories, flagged_files)
    return new_line + 1


_PLUGIN_REGISTRATION_RE = re.compile(r"\bplugins\b", re.IGNORECASE)
_ESLINT_CONFIG_PREFIXES = ("eslint.config.", ".eslintrc")
_MOVED_INTO_PREFIXES = ("rename to ", "copy to ")


def _executable_config_tool(base: str) -> str | None:
    """Returns the tool that runs config file `base` as code, if any."""
    return "eslint" if base.startswith(_ESLINT_CONFIG_PREFIXES) else None


def _plugin_config_tool(base: str) -> str | None:
    """Returns the tool that loads plugins registered in config file `base`."""
    if base.startswith("tsconfig") and base.endswith(".json"):
        return "tsc"
    if base == "analysis_options.yaml":
        return "dart analyze"
    return None


@dataclass
class _PromotionScan:
    """Position and results of the config promotion pass over one diff."""

    promotions: list[dict[str, Any]] = field(default_factory=list)
    promoted: set[tuple[str, str]] = field(default_factory=set)
    current_file: str = ""
    plugin_tool: str | None = None
    new_line: int | None = None
    plugins_in_view: bool = False

    def promote(self, tool: str, line: int, reason: str, snippet: str) -> None:
        """Records `tool` as promoted by the current file, once per pair."""
        key = (tool, self.current_file)
        if key in self.promoted:
            return
        self.promoted.add(key)
        self.promotions.append(
            {
                "tool": tool,
                "file_path": self.current_file,
                "line": line,
                "reason": reason,
                "snippet": snippet.strip()[:160],
            }
        )


def _promotion_header_path(raw_line: str) -> str | None:
    """Extracts the file path that a diff header line names, if it names one."""
    if raw_line.startswith("copy to "):
        return raw_line[len("copy to ") :].strip() or None
    return _extract_header_path(raw_line)


def _enter_promotion_file(scan: _PromotionScan, file_path: str, raw_line: str) -> None:
    """Moves the pass to `file_path` and records what its header alone decides.

    An ESLint config is code, so any change to it promotes `eslint`. A file
    renamed or copied into a plugin config name brings content that the diff
    does not show, so it promotes the tool without waiting for a `plugins` line.
    """
    base = file_path.strip().replace("\\", "/").lower().rsplit("/", 1)[-1]
    scan.current_file = file_path
    scan.plugin_tool = _plugin_config_tool(base)
    if raw_line.startswith("diff --git "):
        scan.new_line = None
    tool = _executable_config_tool(base)
    if tool:
        scan.promote(tool, 1, f"Executable {tool} config changed ({base})", raw_line)
    elif scan.plugin_tool and raw_line.startswith(_MOVED_INTO_PREFIXES):
        reason = (
            f"File renamed or copied into a {scan.plugin_tool} config ({base}); "
            "the diff does not show its plugin registration"
        )
        scan.promote(scan.plugin_tool, 1, reason, raw_line)


def _step_promotion_hunk_line(
    scan: _PromotionScan, tool: str, new_line: int, raw_line: str
) -> int:
    """Processes one hunk body line of a plugin config and returns the new line counter.

    A hunk touches the plugin registration once any of its lines, or its
    header, names `plugins`. The first line added at or after that point
    promotes `tool`, because it may be an entry of the plugin list.
    """
    if raw_line.startswith("\\"):
        return new_line
    if _PLUGIN_REGISTRATION_RE.search(raw_line):
        scan.plugins_in_view = True
    if raw_line.startswith("-"):
        return new_line
    if raw_line.startswith("+") and scan.plugins_in_view:
        base = scan.current_file.replace("\\", "/").rsplit("/", 1)[-1]
        reason = f"Plugin registration for {tool} changed ({base})"
        scan.promote(tool, new_line, reason, raw_line[1:])
    return new_line + 1


def _scan_config_promotions(diff_text: str) -> list[dict[str, Any]]:
    """Lists the linters and analyzers whose config the diff makes untrusted."""
    scan = _PromotionScan()
    for raw_line in diff_text.splitlines():
        header_path = _promotion_header_path(raw_line)
        if header_path is not None:
            _enter_promotion_file(scan, header_path, raw_line)
            continue
        hunk_start = _parse_hunk_new_line(raw_line)
        if hunk_start is not None:
            scan.new_line = hunk_start
            scan.plugins_in_view = bool(_PLUGIN_REGISTRATION_RE.search(raw_line))
        elif scan.new_line is not None and scan.plugin_tool:
            scan.new_line = _step_promotion_hunk_line(
                scan, scan.plugin_tool, scan.new_line, raw_line
            )
    return scan.promotions


def scan_diff_execution_risks(diff_text: str) -> dict[str, Any]:
    """Scans a unified diff for local code execution hazards before running local tools.

    The scan reads only added lines and file headers, so a hazard on a deleted
    line is ignored. A clean result means no pattern matched. It does not prove
    the change is safe to run, and the scan has known blind spots:

    - It needs git-style headers with the default `a/` and `b/` path prefixes.
      A diff made with `diff.noprefix` or `diff.mnemonicPrefix` reads as clean.
    - It does not read a file header whose path git wrapped in double quotes
      (a path with a quote, a backslash, a control character, or, unless
      `core.quotePath=false` is set, a non-ASCII byte). Added lines of such a
      file count toward the file before it, or are skipped when it comes first.
    - It skips an added line whose text starts with `++`.

    Args:
        diff_text: The unified diff to scan. An empty diff is clean.

    Returns:
        A JSON-serializable dict with these keys:

        - `status`: `"flagged"` when `signals` is not empty, otherwise `"clean"`.
        - `risk_count`: The number of signals.
        - `flagged_files`: Paths that raised a signal, in first-seen order.
        - `categories`: A map from signal category to its count. The
          categories are `build_and_hook_configs`,
          `process_and_dynamic_execution`, `network_and_exfiltration`, and
          `obfuscation_and_payloads`.
        - `signals`: One dict per match with `category`, `file_path`, `line`
          (in the new file, or 1 for a whole-file signal), `reason`, and
          `snippet`.
        - `config_promotions`: One dict per linter or analyzer that must wait
          for the security gate, with `tool` (`"eslint"`, `"tsc"`, or
          `"dart analyze"`), `file_path`, `line`, `reason`, and `snippet`. A
          tool is listed when the diff changes its executable config
          (`eslint.config.*`, `.eslintrc*`) or its plugin registration
          (`plugins` in `tsconfig*.json` or `analysis_options.yaml`). A plugin
          entry added far from the `plugins` key, with no `plugins` line in
          its hunk, is not detected. The file still raises a
          `build_and_hook_configs` signal.
    """
    signals: list[dict[str, Any]] = []
    categories: dict[str, int] = {}
    flagged_files: list[str] = []
    seen_config_files: set[str] = set()
    current_file = ""
    new_line: int | None = None

    for raw_line in (diff_text or "").splitlines():
        header_path = _extract_header_path(raw_line)
        if header_path is not None:
            current_file = header_path
            if raw_line.startswith("diff --git "):
                new_line = None
            _record_config_if_needed(
                current_file,
                raw_line,
                seen_config_files,
                signals,
                categories,
                flagged_files,
            )
            continue

        hunk_start = _parse_hunk_new_line(raw_line)
        if hunk_start is not None:
            new_line = hunk_start
        elif new_line is not None and current_file:
            new_line = _step_hunk_line(
                raw_line, current_file, new_line, signals, categories, flagged_files
            )

    return {
        "status": "flagged" if signals else "clean",
        "risk_count": len(signals),
        "flagged_files": flagged_files,
        "categories": categories,
        "signals": signals,
        "config_promotions": _scan_config_promotions(diff_text or ""),
    }


def _read_diff(source: str) -> str:
    """Reads the diff from a file path, or from stdin when `source` is `-`.

    Bytes that are not valid UTF-8 are replaced instead of rejected, so one
    stray byte in a change cannot stop the scan.
    """
    if source == "-":
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    return Path(source).read_text(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the command-line scan and returns the process exit code.

    Args:
        argv: Command-line arguments without the program name. Defaults to
            `sys.argv[1:]`.

    Returns:
        0 after printing the scan result, or 1 when the diff cannot be read
        or the `--output` file cannot be written.
    """
    parser = argparse.ArgumentParser(
        prog="security_prescan.py",
        description="Scan a unified diff for local code execution hazards.",
    )
    parser.add_argument(
        "diff", help="Path to a unified diff file, or '-' to read it from stdin."
    )
    parser.add_argument(
        "--output", help="Also write the scan result JSON to this path."
    )
    args = parser.parse_args(argv)

    try:
        diff_text = _read_diff(args.diff)
    except OSError as exc:
        print(f"Error reading diff: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(scan_diff_execution_risks(diff_text), indent=2)
    if args.output:
        output_path = Path(args.output)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            print(f"Error writing scan result: {exc}", file=sys.stderr)
            return 1
    print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
