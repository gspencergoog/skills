#!/usr/bin/env python3
"""validate_plan.py - Implementation Plan Linter and Validator

Validates implementation plan markdown files for structural completeness,
actionable details, placeholder-free content, parallel wave file disjointness,
and file path integrity against one or more target workspaces.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plan_model import (
    FULL_GLANCE_FIELDS,
    LIGHTWEIGHT_GLANCE_FIELDS,
    PlanDocument,
    TrackInfo,
    WaveInfo,
    find_section,
    iter_non_fenced_lines,
    parse_plan,
    section_text,
    sentence_stats,
    split_shell_segments,
)

BRIEF_MAX_AVG_SENTENCE_WORDS = 20
GLANCE_MAX_WORDS = 180
NEXT_STEP_MESSAGE = (
    "NEXT: present the brief, then run plan_surfaces.py questions <plan> "
    "and render the post-plan worksheet (review checkbox in the footer)."
)

_ENV_ASSIGN_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PLACEHOLDER_CMD_RE = re.compile(r"(?:^|\s)\[(?![\[\s\]])[^\]]*\]")
_PYTHON_BIN_RE = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_SHARE_WORKSPACE_RE = re.compile(
    r'workspace\s*[:=]\s*[`"\']?share[`"\']?|`share`',
    re.IGNORECASE,
)
_NEGATED_SHARE_RE = re.compile(
    r"\b(?:not|rather\s+than|instead\s+of|without)\b[^.\n;|]{0,30}"
    r'(?:workspace\s*[:=]\s*[`"\']?share[`"\']?|`share`)',
    re.IGNORECASE,
)

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_SHELL_BUILTINS = frozenset(
    {
        "cd",
        "export",
        "test",
        "[",
        "[[",
        "true",
        "false",
        "echo",
        "set",
        "unset",
        "source",
        ".",
        "command",
        "exit",
        "type",
        "read",
        ":",
        "for",
        "in",
        "do",
        "done",
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "while",
        "until",
        "case",
        "esac",
        "local",
        "return",
        "shift",
        "trap",
        "umask",
        "exec",
        "eval",
        "builtin",
        "hash",
        "pwd",
        "printf",
        "{",
        "}",
        "!",
    }
)
_WRAPPER_CWD_FLAGS = frozenset(
    {"-C", "--chdir", "--directory", "--project", "--dir", "--cwd"}
)
_WRAPPER_SCOPE_FLAGS = frozenset({"--package", "--filter", "-F"})
_KNOWN_YARN_TOOLS = frozenset(
    {
        "eslint",
        "biome",
        "flake8",
        "mypy",
        "pylint",
        "ruff",
        "pytest",
        "py.test",
        "jest",
        "vitest",
        "tsc",
        "pyright",
    }
)
_ENV_VALUE_FLAGS = frozenset({"-u", "--unset", "-C", "--chdir"})
_UV_RUN_VALUE_FLAGS = frozenset(
    {
        "--with",
        "--with-editable",
        "--with-requirements",
        "--python",
        "-p",
        "--directory",
        "--project",
        "--env-file",
        "--index",
        "--default-index",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--package",
        "--group",
        "--only-group",
        "--extra",
    }
)
_UVX_VALUE_FLAGS = frozenset(
    {
        "--from",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--python",
        "-p",
        "--directory",
        "--index",
        "--default-index",
        "--env-file",
    }
)
_NPX_VALUE_FLAGS = frozenset(
    {"-p", "--package", "-c", "--call", "--cache", "--userconfig", "--registry"}
)
_PNPM_VALUE_FLAGS = frozenset({"-C", "--dir", "--filter", "-F", "--package", "-p"})
_YARN_VALUE_FLAGS = frozenset({"--cwd", "--modules-folder", "--cache-folder"})
_RUFF_VALUE_FLAGS = frozenset(
    {
        "-c",
        "--config",
        "--select",
        "--extend-select",
        "--ignore",
        "--extend-ignore",
        "--per-file-ignores",
        "--extend-per-file-ignores",
        "--fixable",
        "--unfixable",
        "--extend-fixable",
        "--exclude",
        "--extend-exclude",
        "--line-length",
        "--target-version",
        "-o",
        "--output-file",
        "--output-format",
        "--cache-dir",
        "--stdin-filename",
    }
)
_BIOME_VALUE_FLAGS = frozenset(
    {
        "--config-path",
        "--files-max-size",
        "--formatter-indent-style",
        "--indent-style",
        "--indent-width",
        "--line-width",
        "--line-ending",
        "--reporter",
        "--log-level",
        "--log-kind",
        "--diagnostic-level",
        "--stdin-file-path",
    }
)
_LINTER_VALUE_FLAGS = frozenset(
    {
        "-c",
        "--config",
        "--config-file",
        "--rcfile",
        "-f",
        "--format",
        "--output-format",
        "-o",
        "--output-file",
        "--ext",
        "--rule",
        "--parser",
        "--parser-options",
        "--resolve-plugins-relative-to",
        "--ignore-path",
        "--ignore-pattern",
        "--max-warnings",
        "--select",
        "--ignore",
        "--extend-ignore",
        "--extend-select",
        "--exclude",
        "--extend-exclude",
        "--per-file-ignores",
        "--max-line-length",
        "--jobs",
        "-j",
        "-m",
        "--module",
        "-p",
        "--package",
        "--python-version",
        "--python-executable",
        "--platform",
        "--cache-dir",
        "--follow-imports",
        "--disable",
        "-d",
        "--enable",
        "-e",
        "--fail-under",
    }
)
_PYTEST_VALUE_FLAGS = frozenset(
    {
        "-c",
        "--config-file",
        "-o",
        "--override-ini",
        "-m",
        "-k",
        "-W",
        "--pythonwarnings",
        "--rootdir",
        "--confcutdir",
        "--basetemp",
        "--tb",
        "--maxfail",
        "--durations",
        "--durations-min",
        "--color",
        "--capture",
        "--log-level",
        "--log-cli-level",
        "--log-file",
        "--junitxml",
        "--junit-xml",
        "--import-mode",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "-p",
        "--cov",
        "--cov-report",
        "--cov-config",
        "-n",
        "--numprocesses",
        "--dist",
    }
)
_PYTHON_VALUE_FLAGS = frozenset({"-W", "-X", "--check-hash-based-pycs"})


def _skip_leading_flags(
    tokens: Sequence[str], value_flags: frozenset[str]
) -> tuple[str, ...]:
    """Skips leading CLI flags (and arguments to `value_flags`) before a subcommand."""
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--":
            return tuple(tokens[idx + 1 :])
        if tok in value_flags:
            idx += 2
        elif tok.startswith("-"):
            idx += 1
        else:
            break
    return tuple(tokens[idx:])


def _strip_env_assignments(tokens: Sequence[str]) -> tuple[str, ...]:
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGN_TOKEN_RE.match(tokens[idx]):
        idx += 1
    return tuple(tokens[idx:])


def _strip_single_env(tokens: Sequence[str]) -> tuple[str, ...]:
    idx = 1
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--":
            idx += 1
            break
        if tok in ("-S", "--split-string") and idx + 1 < len(tokens):
            try:
                extra = shlex.split(tokens[idx + 1])
            except ValueError:
                extra = [tokens[idx + 1]]
            return _strip_env_assignments([*extra, *tokens[idx + 2 :]])
        if tok in _ENV_VALUE_FLAGS:
            idx += 2
        elif tok.startswith("-"):
            idx += 1
        else:
            break
    return _strip_env_assignments(tokens[idx:])


def _strip_env_wrappers(argv: Sequence[str]) -> tuple[str, ...]:
    tokens = _strip_env_assignments(argv)
    while tokens and Path(os.path.expanduser(tokens[0])).name == "env":
        rest = _strip_single_env(tokens)
        if not rest:
            return ("env",)
        tokens = rest
    return tokens


def _unwrap_uv(rest: Sequence[str]) -> tuple[str, ...] | None:
    after_top = _skip_leading_flags(rest, _UV_RUN_VALUE_FLAGS)
    if not after_top:
        return None
    if after_top[0] == "run":
        return _skip_leading_flags(after_top[1:], _UV_RUN_VALUE_FLAGS) or None
    if len(after_top) >= 2 and after_top[0] == "tool" and after_top[1] == "run":
        return _skip_leading_flags(after_top[2:], _UVX_VALUE_FLAGS) or None
    return None


def _unwrap_pnpm(rest: Sequence[str]) -> tuple[str, ...] | None:
    after_top = _skip_leading_flags(rest, _PNPM_VALUE_FLAGS)
    if not after_top:
        return None
    if after_top[0] in ("exec", "dlx"):
        return _skip_leading_flags(after_top[1:], _PNPM_VALUE_FLAGS) or None
    if Path(after_top[0]).name in _KNOWN_YARN_TOOLS:
        return after_top
    return None


def _unwrap_yarn(rest: Sequence[str]) -> tuple[str, ...] | None:
    after_top = _skip_leading_flags(rest, _YARN_VALUE_FLAGS)
    if not after_top:
        return None
    if after_top[0] == "exec":
        return _skip_leading_flags(after_top[1:], frozenset()) or None
    if after_top[0] == "run" and len(after_top) >= 2:
        candidate = after_top[1:]
        return candidate if Path(candidate[0]).name in _KNOWN_YARN_TOOLS else None
    if Path(after_top[0]).name in _KNOWN_YARN_TOOLS:
        return after_top
    return None


def _unwrap_command(argv: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """Unwraps env/uv/uvx/npx/pnpm/yarn, returning `(outer_exe, inner_argv)`."""
    tokens = _strip_env_wrappers(argv)
    if not tokens:
        return "", ()
    outer_exe = os.path.expanduser(tokens[0])
    exe_name = Path(outer_exe).name
    rest = tokens[1:]

    unwrapped: tuple[str, ...] | None = None
    if exe_name == "uv":
        unwrapped = _unwrap_uv(rest)
    elif exe_name == "uvx":
        unwrapped = _skip_leading_flags(rest, _UVX_VALUE_FLAGS) or None
    elif exe_name == "npx":
        unwrapped = _skip_leading_flags(rest, _NPX_VALUE_FLAGS) or None
    elif exe_name == "pnpm":
        unwrapped = _unwrap_pnpm(rest)
    elif exe_name == "yarn":
        unwrapped = _unwrap_yarn(rest)

    inner = unwrapped if unwrapped else (outer_exe, *rest)
    expanded_inner = (os.path.expanduser(inner[0]), *inner[1:])
    return outer_exe, expanded_inner


def _extract_positional_args(
    args: Sequence[str], value_flags: frozenset[str]
) -> list[str]:
    """Extracts positional arguments after skipping flags and `value_flags` arguments."""
    positionals: list[str] = []
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == "--":
            positionals.extend(args[idx + 1 :])
            break
        if tok in value_flags:
            idx += 2
        elif tok.startswith("-"):
            idx += 1
        else:
            positionals.append(tok)
            idx += 1
    return positionals


def _is_dot_or_empty_target(positionals: Sequence[str]) -> bool:
    return not positionals or any(p in (".", "./") for p in positionals)


def _args_before_double_dash(args: Sequence[str]) -> tuple[str, ...]:
    if "--" in args:
        return tuple(args[: args.index("--")])
    return tuple(args)


def _normalize_python_module_tool(inner_argv: tuple[str, ...]) -> tuple[str, ...]:
    if not inner_argv or not _PYTHON_BIN_RE.match(Path(inner_argv[0]).name):
        return inner_argv
    if (
        len(inner_argv) >= 3
        and inner_argv[1] == "-m"
        and inner_argv[2] in {"ruff", "pytest", "flake8", "mypy", "pylint"}
    ):
        return inner_argv[2:]
    return inner_argv


def _is_unscoped_cargo_clippy(args: Sequence[str]) -> bool:
    if not args or args[0] != "clippy":
        return False
    cargo_opts = _args_before_double_dash(args[1:])
    return not any(
        tok in ("-p", "--package", "--manifest-path")
        or tok.startswith(("--package=", "--manifest-path="))
        or (tok.startswith("-p") and not tok.startswith("--"))
        for tok in cargo_opts
    )


def _is_unscoped_pytest(args: Sequence[str]) -> bool:
    opts = _args_before_double_dash(args)
    has_k_selector = any(
        tok == "-k" or (tok.startswith("-k") and not tok.startswith("--"))
        for tok in opts
    )
    if has_k_selector:
        return False
    return _is_dot_or_empty_target(_extract_positional_args(args, _PYTEST_VALUE_FLAGS))


def _is_unscoped_mypy(args: Sequence[str]) -> bool:
    opts = _args_before_double_dash(args)
    has_pkg_or_mod = any(
        tok in ("-m", "--module", "-p", "--package")
        or tok.startswith(("--module=", "--package="))
        for tok in opts
    )
    if has_pkg_or_mod:
        return False
    return _is_dot_or_empty_target(_extract_positional_args(args, _LINTER_VALUE_FLAGS))


def _is_unscoped_subcommand_linter(
    args: Sequence[str],
    subcommands: frozenset[str],
    value_flags: frozenset[str],
) -> bool:
    sub = _skip_leading_flags(args, value_flags)
    if not sub or sub[0] not in subcommands:
        return False
    return _is_dot_or_empty_target(_extract_positional_args(sub[1:], value_flags))


def _is_unscoped_track_command(inner_argv: tuple[str, ...]) -> bool:
    """Returns True when an unwrapped track command runs a directory-wide linter or bare pytest."""
    effective = _normalize_python_module_tool(inner_argv)
    if not effective:
        return False
    tool = Path(effective[0]).name
    args = effective[1:]

    if tool == "ruff":
        return _is_unscoped_subcommand_linter(
            args, frozenset({"check", "format"}), _RUFF_VALUE_FLAGS
        )
    if tool == "biome":
        return _is_unscoped_subcommand_linter(
            args, frozenset({"check", "lint", "format", "ci"}), _BIOME_VALUE_FLAGS
        )
    if tool == "mypy":
        return _is_unscoped_mypy(args)
    if tool in ("eslint", "flake8", "pylint"):
        return _is_dot_or_empty_target(
            _extract_positional_args(args, _LINTER_VALUE_FLAGS)
        )
    if tool == "cargo":
        return _is_unscoped_cargo_clippy(args)
    if tool in ("pytest", "py.test"):
        return _is_unscoped_pytest(args)
    return False


def _split_flag_value(
    tokens: Sequence[str], idx: int, flags: frozenset[str]
) -> tuple[str | None, int]:
    tok = tokens[idx]
    if tok in flags and idx + 1 < len(tokens):
        return tokens[idx + 1], idx + 2
    name, eq, val = tok.partition("=")
    if eq and name in flags and val:
        return val, idx + 1
    return None, idx


def _extract_wrapper_scope(
    argv: Sequence[str], inner_argv: Sequence[str], outer_exe: str = ""
) -> tuple[str | None, bool]:
    """Extracts `(wrapper_cwd, has_pkg_scope)` from wrapper prefix flags."""
    prefix_len = max(0, len(argv) - len(inner_argv))
    prefix = argv[:prefix_len]
    allow_pkg_scope = Path(outer_exe).name != "npx"
    wrapper_cwd: str | None = None
    has_pkg_scope = False
    idx = 0
    while idx < len(prefix):
        cwd_val, next_idx = _split_flag_value(prefix, idx, _WRAPPER_CWD_FLAGS)
        if cwd_val is not None:
            wrapper_cwd = cwd_val
            idx = next_idx
            continue
        scope_val, next_idx = _split_flag_value(prefix, idx, _WRAPPER_SCOPE_FLAGS)
        if scope_val is not None:
            has_pkg_scope = has_pkg_scope or allow_pkg_scope
            idx = next_idx
            continue
        idx += 1
    return wrapper_cwd, has_pkg_scope


def _combine_cwd(base_cwd: str | None, extra_cwd: str | None) -> str | None:
    if not extra_cwd:
        return base_cwd
    if os.path.isabs(extra_cwd) or extra_cwd.startswith("~"):
        return extra_cwd
    if base_cwd and (
        not os.path.isabs(base_cwd) or Path(os.path.expanduser(base_cwd)).is_dir()
    ):
        return os.path.normpath(os.path.join(base_cwd, extra_cwd))
    return extra_cwd


def _is_subdir_scope(cwd: str | None) -> bool:
    if not cwd or os.path.isabs(cwd) or cwd.startswith("~"):
        return False
    norm = os.path.normpath(cwd)
    return norm not in (".", "") and not norm.startswith("..")


def _clean_script_token(tok: str) -> str | None:
    if not tok or tok == "-" or tok.startswith("$"):
        return None
    return tok


def _is_python_non_script_flag(tok: str) -> bool:
    if tok in ("-m", "-c", "-", "-V", "--version", "-h", "--help"):
        return True
    return len(tok) > 2 and tok[:2] in ("-m", "-c") and not tok.startswith("--")


def _extract_python_script_arg(inner_argv: tuple[str, ...]) -> str | None:
    """Returns the positional script path passed to python/python3, or None for -m/-c/stdin."""
    if not inner_argv or not _PYTHON_BIN_RE.match(Path(inner_argv[0]).name):
        return None
    args = inner_argv[1:]
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if _is_python_non_script_flag(tok):
            return None
        if tok == "--":
            return _clean_script_token(next(iter(args[idx + 1 :]), ""))
        if tok in _PYTHON_VALUE_FLAGS:
            idx += 2
            continue
        if not tok.startswith("-"):
            return _clean_script_token(tok)
        idx += 1
    return None


def _uses_share_workspace(exec_text: str) -> bool:
    cleaned = _HTML_COMMENT_RE.sub("", exec_text)
    cleaned = _NEGATED_SHARE_RE.sub("", cleaned)
    return bool(_SHARE_WORKSPACE_RE.search(cleaned))


@dataclass
class ValidationIssue:
    is_error: bool
    message: str
    line_num: int | None = None
    file_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": "ERROR" if self.is_error else "WARNING",
            "message": self.message,
            "line_num": self.line_num,
            "file_path": self.file_path,
        }

    def __str__(self) -> str:
        level = "ERROR" if self.is_error else "WARNING"
        location = f":{self.line_num}" if self.line_num is not None else ""
        return f"[{level}]{location} {self.message}"


class PlanValidator:
    BRIEF_MAX_AVG_SENTENCE_WORDS = BRIEF_MAX_AVG_SENTENCE_WORDS
    GLANCE_MAX_WORDS = GLANCE_MAX_WORDS

    BRIEF_SECTION_PATTERNS = (
        r"^at a glance\b",
        r"^user review required\b",
        r"^open questions\b",
        r"^decisions\b",
    )

    # Required top-level or secondary section patterns (case-insensitive regex)
    REQUIRED_SECTION_PATTERNS = (
        (r"^#\s+.+", "Top-level title (e.g. '# Feature Name')"),
        (
            r"^##\s+.*(user review|open questions|review required|context)",
            "User Review / Questions / Context section",
        ),
        (
            r"^##\s+.*(proposed changes|phased implementation|implementation steps|phases|waves)",
            "Proposed Changes / Phased Implementation section",
        ),
        (
            r"^##\s+.*(verification plan|verification|testing)",
            "Verification Plan section",
        ),
    )

    # Task list items with inline heading hashes (e.g. "- [ ] #### [MODIFY] ...")
    CHECKBOX_HEADING_RE = re.compile(
        r"^\s*(?:(?:[-*+]|\d+\.)\s+)?\[[ xX]\]\s+#{1,6}\s+"
    )

    # Placeholders that indicate incomplete plans
    SUSPICIOUS_PLACEHOLDERS = (
        re.compile(r"\bTODO\b", re.IGNORECASE),
        re.compile(r"\bTBD\b", re.IGNORECASE),
        re.compile(r"\bimplement (?:rest|here|later)\b", re.IGNORECASE),
        re.compile(r"\badd code here\b", re.IGNORECASE),
        re.compile(r"\binsert code here\b", re.IGNORECASE),
    )

    INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)")
    DEFAULT_OPTION_PREFIX_RE = re.compile(
        r"^\s*[-*+]\s+(?:\*\*|`)?(?:\(\s*default\s*\)|[A-Z](?:\*\*|`)?\s*\(\s*default\s*\)\s*:)",
        re.IGNORECASE,
    )
    LEADING_NONE_OR_ZERO_RE = re.compile(r"^(?:none|0)\b", re.IGNORECASE)
    GLANCE_QUESTION_REF_RE = re.compile(r"^[1-9]\d*\b|\bQ\d+\b", re.IGNORECASE)
    DECISIONS_BULLET_RE = re.compile(
        r"^\s*[-*+]\s+\*\*decisions needed from you\s*:?\s*\*\*",
        re.IGNORECASE,
    )

    def __init__(
        self,
        content: str,
        workspace_dir: Path | None = None,
        strict: bool = False,
        extra_workspaces: Sequence[Path] = (),
        check_tools: bool = False,
    ) -> None:
        self.content = content
        self.lines = content.splitlines()
        self.workspace_dir = workspace_dir
        self.extra_workspaces: tuple[Path, ...] = tuple(extra_workspaces)
        self.strict = strict
        self.check_tools = check_tools
        self.issues: list[ValidationIssue] = []
        self._parsed_content = content
        self.doc: PlanDocument = parse_plan(content)

    def validate(self) -> list[ValidationIssue]:
        self.issues.clear()
        if self.content != self._parsed_content:
            self.lines = self.content.splitlines()
            self.doc = parse_plan(self.content)
            self._parsed_content = self.content

        self._check_sections()
        self._check_at_a_glance()
        self._check_glance_question_consistency()
        self._check_brief_readability()
        self._check_questions()
        self._check_pr_table()
        self._check_code_blocks()
        self._check_task_list_headings()
        self._check_placeholders()
        self._check_prose_rules()
        self._check_file_actions()
        waves = self._parallel_waves_map()
        self._check_parallel_waves(waves)
        self._check_model_tiers(waves)
        self._check_parallel_orchestration(self.doc)
        self._check_track_verification_scope()
        self._check_verification_tools()
        self._check_parallel_handoff_gates(waves)
        self._check_track_interfaces(waves)
        return self.issues

    def _all_workspaces(self) -> tuple[Path, ...]:
        if self.workspace_dir is None:
            return self.extra_workspaces
        return (self.workspace_dir, *self.extra_workspaces)

    def _check_sections(self) -> None:
        """Verifies that all required structural sections exist."""
        for pattern, description in self.REQUIRED_SECTION_PATTERNS:
            regex = re.compile(pattern, re.IGNORECASE)
            found = any(
                regex.search(line) for _, line in iter_non_fenced_lines(self.lines)
            )
            if not found:
                self.issues.append(
                    ValidationIssue(
                        is_error=True,
                        message=f"Missing required section: {description}",
                    )
                )

    def _check_at_a_glance(self) -> None:
        """Verifies that '## At a glance' is first, has required fields, and is <= 180 words."""
        glance_section = find_section(self.doc.sections, r"^at a glance\b", level=2)
        if self.doc.at_a_glance is None or glance_section is None:
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message="Missing required '## At a glance' section.",
                )
            )
            return

        first_h2 = next((s for s in self.doc.sections if s.level == 2), None)
        if first_h2 is not None and first_h2.start_line != glance_section.start_line:
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message=(
                        f"'## At a glance' must be the first '##' section "
                        f"(found '## {first_h2.heading}' first)."
                    ),
                    line_num=first_h2.start_line,
                )
            )

        required_fields = (
            LIGHTWEIGHT_GLANCE_FIELDS if self.doc.is_lightweight else FULL_GLANCE_FIELDS
        )
        missing = [f for f in required_fields if not self.doc.at_a_glance.get(f)]
        if missing:
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message=(
                        f"'## At a glance' is missing required field(s): "
                        f"{', '.join(missing)}."
                    ),
                    line_num=glance_section.start_line,
                )
            )

        word_count = len(self.doc.glance_bullet_text.split())
        if word_count > self.GLANCE_MAX_WORDS:
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message=(
                        f"'## At a glance' field bullets total {word_count} words "
                        f"(max {self.GLANCE_MAX_WORDS})."
                    ),
                    line_num=glance_section.start_line,
                )
            )

    def _glance_decisions_line(self) -> int | None:
        section = find_section(self.doc.sections, r"^at a glance\b", level=2)
        if section is None:
            return None
        for i, line in iter_non_fenced_lines(self.lines):
            if (
                section.start_line < i <= section.end_line
                and self.DECISIONS_BULLET_RE.match(line)
            ):
                return i
        return section.start_line

    def _check_glance_question_consistency(self) -> None:
        """Checks 'Decisions needed from you' in At a glance against Open Questions."""
        if self.doc.at_a_glance is None:
            return
        raw_val = (self.doc.at_a_glance.get("decisions needed from you") or "").strip()
        if not raw_val:
            return

        cleaned = raw_val.lstrip("`* ").strip()
        starts_none = bool(self.LEADING_NONE_OR_ZERO_RE.match(cleaned))
        q_count = len(self.doc.questions)

        if q_count > 0 and starts_none:
            self.issues.append(
                ValidationIssue(
                    is_error=self.strict,
                    message=(
                        f"'## At a glance' says 'Decisions needed from you: {raw_val}' "
                        f"but '## Open Questions' has {q_count} open question(s)."
                    ),
                    line_num=self._glance_decisions_line(),
                )
            )
        elif (
            q_count == 0
            and not starts_none
            and self.GLANCE_QUESTION_REF_RE.search(cleaned)
        ):
            self.issues.append(
                ValidationIssue(
                    is_error=self.strict,
                    message=(
                        f"'## At a glance' says 'Decisions needed from you: {raw_val}' "
                        "but '## Open Questions' has 0 open questions."
                    ),
                    line_num=self._glance_decisions_line(),
                )
            )

    def _check_brief_readability(self) -> None:
        """Verifies that the brief sections average 20 words per sentence or fewer."""
        brief_text = "\n\n".join(
            section_text(self.content, self.doc, pattern)
            for pattern in self.BRIEF_SECTION_PATTERNS
        )
        mean_words, _ = sentence_stats(brief_text)
        if mean_words > self.BRIEF_MAX_AVG_SENTENCE_WORDS:
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message=(
                        f"Brief sections average {mean_words:.1f} words per sentence "
                        f"(max {self.BRIEF_MAX_AVG_SENTENCE_WORDS})."
                    ),
                )
            )

    def _check_questions(self) -> None:
        """Verifies structured '### Q<n>' open questions and rejects free-form bullets."""
        if self.doc.free_question_bullets and not self.doc.no_questions_marker:
            section = find_section(self.doc.sections, r"^open questions\b", level=2)
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message=(
                        "Open Questions contains free-form bullets; use '### Q<n>: <title>' "
                        "with option bullets or 'None currently blocking.'."
                    ),
                    line_num=section.start_line if section else None,
                )
            )

        for q in self.doc.questions:
            if len(q.options) < 2:
                self.issues.append(
                    ValidationIssue(
                        is_error=True,
                        message=(
                            f"Open question {q.qid} ('{q.title}') has {len(q.options)} "
                            "option(s); at least 2 options are required."
                        ),
                        line_num=q.line,
                    )
                )
            rec_count = sum(1 for opt in q.options if opt.recommended)
            if rec_count != 1:
                self.issues.append(
                    ValidationIssue(
                        is_error=True,
                        message=(
                            f"Open question {q.qid} ('{q.title}') must have exactly 1 "
                            f"'(Recommended)' option (found {rec_count})."
                        ),
                        line_num=q.line,
                    )
                )

    def _check_pr_table(self) -> None:
        """Requires a PR or Commits table when the plan has 2 or more waves."""
        if self.doc.is_lightweight:
            return
        if len(self.doc.waves) >= 2 and not self.doc.pr_rows:
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message=(
                        f"Plan has {len(self.doc.waves)} waves but no PR or Commits table "
                        "(expected a pipe table under a 'PRs', 'Commits', or 'PR plan' heading)."
                    ),
                )
            )

    def _check_code_blocks(self) -> None:
        """Verifies that markdown code blocks are properly opened and closed."""
        in_code_block = False
        fence_char = ""
        fence_len = 0
        block_start_line = 0

        for i, line in enumerate(self.lines, start=1):
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                current_char = stripped[0]
                current_len = len(stripped) - len(stripped.lstrip(current_char))
                if not in_code_block:
                    in_code_block = True
                    fence_char = current_char
                    fence_len = current_len
                    block_start_line = i
                elif current_char == fence_char and current_len >= fence_len:
                    in_code_block = False

        if in_code_block:
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message=f"Unclosed code fence starting at line {block_start_line}",
                    line_num=block_start_line,
                )
            )

    def _check_task_list_headings(self) -> None:
        """Flags task list items that contain inline heading hashes (e.g. '- [ ] #### Item')."""
        for i, line in iter_non_fenced_lines(self.lines):
            if self.CHECKBOX_HEADING_RE.search(line):
                self.issues.append(
                    ValidationIssue(
                        is_error=True,
                        message=(
                            f"Invalid Markdown: task list item contains heading hashes: '{line.strip()}'"
                        ),
                        line_num=i,
                    )
                )

    def _check_placeholders(self) -> None:
        """Flags unresolved placeholders (TODO, TBD, implement rest)."""
        for i, line in iter_non_fenced_lines(self.lines):
            for pattern in self.SUSPICIOUS_PLACEHOLDERS:
                if pattern.search(line):
                    self.issues.append(
                        ValidationIssue(
                            is_error=self.strict,
                            message=f"Unresolved placeholder found: '{line.strip()}'",
                            line_num=i,
                        )
                    )

    def _check_prose_rules(self) -> None:
        """Flags em-dashes outside inline/fenced code and '(default)' option prefixes."""
        open_q = find_section(self.doc.sections, r"^open questions\b", level=2)
        for i, line in iter_non_fenced_lines(self.lines):
            stripped_code = self.INLINE_CODE_RE.sub("", line)
            if "\u2014" in stripped_code:
                self.issues.append(
                    ValidationIssue(
                        is_error=self.strict,
                        message=(
                            "Em-dash character found outside code fences; rewrite with a "
                            "colon, comma, parentheses, or separate sentence."
                        ),
                        line_num=i,
                    )
                )
            if (
                open_q is not None
                and open_q.start_line < i <= open_q.end_line
                and self.DEFAULT_OPTION_PREFIX_RE.match(line)
            ):
                self.issues.append(
                    ValidationIssue(
                        is_error=self.strict,
                        message=(
                            "Option in '## Open Questions' uses '(default)' prefix; "
                            "use '(Recommended)' instead."
                        ),
                        line_num=i,
                    )
                )

    def _resolve_action_file(self, path_str: str) -> Path:
        """Resolves a planned file path against the primary or extra workspaces."""
        assert self.workspace_dir is not None
        if not os.path.isabs(path_str):
            for ws in self._all_workspaces():
                ws_candidate = ws / path_str
                if ws_candidate.exists():
                    return ws_candidate
            return self.workspace_dir / path_str

        abs_candidate = Path(path_str)
        if abs_candidate.exists():
            return abs_candidate
        abs_resolved = abs_candidate.resolve()
        for ws in self._all_workspaces():
            if abs_resolved.is_relative_to(ws.resolve()):
                return abs_candidate
        rel_subpath = path_str.lstrip("/")
        for ws in self._all_workspaces():
            ws_candidate = ws / rel_subpath
            if ws_candidate.exists():
                return ws_candidate
        return abs_candidate

    def _normalize_action_path(self, path_str: str) -> str:
        """Normalizes a file path so relative and file:// links compare equal."""
        if self.workspace_dir:
            ws_resolved = self.workspace_dir.resolve()
            candidate = self._resolve_action_file(path_str).resolve()
            try:
                return candidate.relative_to(ws_resolved).as_posix()
            except ValueError:
                return candidate.as_posix().lstrip("/")

        cleaned = Path(path_str).as_posix()
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned.lstrip("/")

    def _wave_order_map(self) -> dict[str, int]:
        """Maps each wave_id to its 0-based order of first appearance."""
        order: dict[str, int] = {}
        for wave in self.doc.waves:
            order.setdefault(wave.wave_id, len(order))
        for track in self.doc.tracks:
            order.setdefault(track.wave_id, len(order))
        for fa in self.doc.file_actions:
            if fa.wave_id is not None:
                order.setdefault(fa.wave_id, len(order))
        return order

    def _validate_new_workspace_file(
        self,
        path_str: str,
        resolved_file: Path,
        line_num: int,
        planned_new_paths: set[Path],
    ) -> None:
        if resolved_file.exists():
            self.issues.append(
                ValidationIssue(
                    is_error=False,
                    message=(
                        f"File marked [NEW] already exists on disk: '{path_str}'. "
                        "Should this be [MODIFY] instead?"
                    ),
                    line_num=line_num,
                    file_path=path_str,
                )
            )
        has_planned_parent = any(p in resolved_file.parents for p in planned_new_paths)
        if not resolved_file.parent.exists() and not has_planned_parent:
            self.issues.append(
                ValidationIssue(
                    is_error=False,
                    message=(
                        f"Parent directory for new file does not exist yet: "
                        f"'{resolved_file.parent}'"
                    ),
                    line_num=line_num,
                    file_path=path_str,
                )
            )

    def _validate_workspace_action(
        self,
        action: str,
        path_str: str,
        line_num: int,
        planned_new_paths: set[Path],
        planned_in_prior_wave: bool = False,
    ) -> Path | None:
        """Validates a single file action against disk when workspace_dir is configured."""
        if not self.workspace_dir:
            return None

        resolved_file = self._resolve_action_file(path_str)
        if action in ("MODIFY", "DELETE", "RENAME"):
            if not resolved_file.exists() and not planned_in_prior_wave:
                self.issues.append(
                    ValidationIssue(
                        is_error=True,
                        message=(
                            f"File marked [{action}] does not exist on disk: '{path_str}' "
                            f"(resolved to {resolved_file})"
                        ),
                        line_num=line_num,
                        file_path=path_str,
                    )
                )
        elif action == "NEW":
            self._validate_new_workspace_file(
                path_str, resolved_file, line_num, planned_new_paths
            )
        return resolved_file

    @staticmethod
    def _is_prior_wave(cur_wave_idx: int | None, prior_idx: int | None) -> bool:
        return (
            cur_wave_idx is not None
            and prior_idx is not None
            and prior_idx < cur_wave_idx
        )

    def _check_file_actions(self) -> None:
        """Verifies [NEW], [MODIFY], [DELETE], [RENAME] entries against disk when workspace is set."""
        if not self.workspace_dir:
            return

        wave_order = self._wave_order_map()
        seen_actions: set[tuple[str, str]] = set()
        earliest_new_wave: dict[str, int] = {}
        planned_new_paths: set[Path] = set()

        for fa in self.doc.file_actions:
            norm_path = self._normalize_action_path(fa.raw_path)
            cur_wave_idx = wave_order.get(fa.wave_id or "")
            in_prior_wave = self._is_prior_wave(
                cur_wave_idx, earliest_new_wave.get(norm_path)
            )

            if (fa.action, norm_path) not in seen_actions:
                seen_actions.add((fa.action, norm_path))
                resolved = self._validate_workspace_action(
                    fa.action, fa.raw_path, fa.line, planned_new_paths, in_prior_wave
                )
                if fa.action == "NEW" and resolved is not None:
                    planned_new_paths.add(resolved)

            if fa.action == "NEW" and cur_wave_idx is not None:
                earliest_new_wave[norm_path] = min(
                    earliest_new_wave.get(norm_path, cur_wave_idx), cur_wave_idx
                )

    def _parallel_waves_map(self) -> dict[str, list[TrackInfo]]:
        """Groups non-sequential tracks from plan_model by wave_id."""
        waves: dict[str, list[TrackInfo]] = {}
        for track in self.doc.parallel_tracks:
            waves.setdefault(track.wave_id, []).append(track)
        return waves

    def _check_wave_collisions(self, wave_id: str, tracks: Sequence[TrackInfo]) -> None:
        """Flags file paths modified by multiple parallel tracks in the same wave."""
        first_seen: dict[str, tuple[str, int, str]] = {}

        for track in tracks:
            seen_in_track: set[str] = set()
            for fa in track.file_actions:
                norm_path = self._normalize_action_path(fa.raw_path)
                if norm_path in seen_in_track:
                    continue
                seen_in_track.add(norm_path)

                if norm_path in first_seen:
                    prev_track, prev_line, _ = first_seen[norm_path]
                    self.issues.append(
                        ValidationIssue(
                            is_error=True,
                            message=(
                                f"Parallel file collision in Wave {wave_id}: '{fa.raw_path}' "
                                f"is touched by both {prev_track} (line {prev_line}) and "
                                f"{track.track_id} (line {fa.line}). Isolate shared "
                                "chokepoint files into a sequential wave."
                            ),
                            line_num=fa.line,
                            file_path=fa.raw_path,
                        )
                    )
                else:
                    first_seen[norm_path] = (track.track_id, fa.line, fa.raw_path)

    def _check_track_verifications(self, tracks: Sequence[TrackInfo]) -> None:
        """Warns (or errors in strict mode) when a parallel track lacks verification."""
        for track in tracks:
            if track.file_actions and not track.has_verification:
                self.issues.append(
                    ValidationIssue(
                        is_error=self.strict,
                        message=(
                            f"Parallel track '{track.track_id}' in Wave {track.wave_id} "
                            f"is missing a track verification command (e.g. '**{track.track_id} Verification**:')."
                        ),
                        line_num=track.line,
                    )
                )

    def _check_out_of_workspace_parallel_edits(
        self, wave_id: str, tracks: Sequence[TrackInfo]
    ) -> None:
        """Warns when a multi-track parallel wave edits a path outside primary workspace_dir."""
        if not self.workspace_dir:
            return

        ws_resolved = self.workspace_dir.resolve()
        for track in tracks:
            seen_in_track: set[str] = set()
            for fa in track.file_actions:
                norm_path = self._normalize_action_path(fa.raw_path)
                if norm_path in seen_in_track:
                    continue
                seen_in_track.add(norm_path)

                resolved = self._resolve_action_file(fa.raw_path).resolve()
                if not resolved.is_relative_to(ws_resolved):
                    self.issues.append(
                        ValidationIssue(
                            is_error=self.strict,
                            message=(
                                f"Parallel track '{track.track_id}' in Wave {wave_id} modifies "
                                f"'{fa.raw_path}' outside primary workspace '{ws_resolved}' "
                                "(swarm workspace 'share' only isolates the primary repository)."
                            ),
                            line_num=fa.line,
                            file_path=fa.raw_path,
                        )
                    )

    def _check_parallel_waves(
        self, waves: dict[str, list[TrackInfo]] | None = None
    ) -> None:
        """Validates intra-wave file disjointness, verification, and primary-repo scope."""
        if waves is None:
            waves = self._parallel_waves_map()
        for wave_id, tracks in waves.items():
            if len(tracks) < 2:
                continue
            self._check_wave_collisions(wave_id, tracks)
            self._check_track_verifications(tracks)
            self._check_out_of_workspace_parallel_edits(wave_id, tracks)

    def _check_model_tiers(
        self, waves: dict[str, list[TrackInfo]] | None = None
    ) -> None:
        """Errors when a parallel track is missing a '**Model tier**' line."""
        if waves is None:
            waves = self._parallel_waves_map()
        for tracks in waves.values():
            for track in tracks:
                if not track.model_tier:
                    self.issues.append(
                        ValidationIssue(
                            is_error=True,
                            message=(
                                f"Parallel track '{track.track_id}' in Wave {track.wave_id} "
                                "is missing a '**Model tier**:' line."
                            ),
                            line_num=track.line,
                        )
                    )

    def _check_parallel_orchestration(self, doc: PlanDocument) -> None:
        """Warns when a plan has parallel waves without /swarm or /run-workflow in Execution Strategy."""
        if not doc.parallel_waves:
            return
        section = find_section(doc.sections, r"execution.*strategy")
        exec_text = section_text(self.content, doc, r"execution.*strategy")
        if "/swarm" not in exec_text and "/run-workflow" not in exec_text:
            self.issues.append(
                ValidationIssue(
                    is_error=False,
                    message=(
                        "Plan contains parallel waves but Execution Strategy "
                        "does not specify /swarm or /run-workflow orchestration."
                    ),
                    line_num=(
                        section.start_line
                        if section is not None
                        else doc.parallel_waves[0].line
                    ),
                )
            )

    def _check_track_verification_scope(self) -> None:
        """Warns when a parallel track invokes a directory-wide linter or bare pytest."""
        for track in self.doc.parallel_tracks:
            if not track.verification or _PLACEHOLDER_CMD_RE.search(track.verification):
                continue
            for segment in split_shell_segments(track.verification):
                outer_exe, inner_argv = _unwrap_command(segment.argv)
                wrapper_cwd, has_pkg_scope = _extract_wrapper_scope(
                    segment.argv, inner_argv, outer_exe
                )
                if (
                    has_pkg_scope
                    or _is_subdir_scope(segment.cwd)
                    or _is_subdir_scope(wrapper_cwd)
                ):
                    continue
                if not _is_unscoped_track_command(inner_argv):
                    continue
                rendered = " ".join(segment.argv)
                self.issues.append(
                    ValidationIssue(
                        is_error=self.strict,
                        message=(
                            f"Parallel track '{track.track_id}' in Wave {track.wave_id} "
                            f"runs unscoped verification command '{rendered}'; scope "
                            "linters and test runners to track-owned files, a subpackage, "
                            "or a test selector (such as '-k')."
                        ),
                        line_num=track.line,
                    )
                )

    @staticmethod
    def _clean_rel_key(path_str: str) -> str:
        norm = Path(os.path.normpath(path_str)).as_posix()
        while norm.startswith("./"):
            norm = norm[2:]
        return norm.lstrip("/")

    def _planned_new_by_wave(self) -> dict[str, int]:
        """Maps normalized [NEW] paths to their earliest 0-based wave index."""
        wave_order = self._wave_order_map()
        earliest: dict[str, int] = {}
        for fa in self.doc.file_actions:
            if fa.action != "NEW":
                continue
            idx = wave_order.get(fa.wave_id or "", 0)
            for key in (
                self._normalize_action_path(fa.raw_path),
                self._clean_rel_key(fa.raw_path),
            ):
                earliest[key] = min(earliest.get(key, idx), idx)
        return earliest

    def _candidate_search_dirs(self, raw_cwd: str | None) -> tuple[Path, ...]:
        workspaces = self._all_workspaces()
        if not raw_cwd:
            return workspaces or (Path.cwd(),)
        expanded = os.path.expanduser(raw_cwd)
        if os.path.isabs(expanded):
            abs_cwd = Path(expanded)
            if abs_cwd.is_dir():
                return (abs_cwd,)
            return workspaces or (Path.cwd(),)
        if workspaces:
            existing = tuple(
                (ws / expanded).resolve()
                for ws in workspaces
                if (ws / expanded).is_dir()
            )
            return existing or workspaces
        rel_cwd = Path(expanded)
        return (rel_cwd,) if rel_cwd.is_dir() else (Path.cwd(),)

    def _candidate_planned_keys(
        self, expanded: str, segment_cwd: str | None, base_dirs: Sequence[Path]
    ) -> list[str]:
        if os.path.isabs(expanded):
            return [
                self._normalize_action_path(expanded),
                self._clean_rel_key(expanded),
            ]
        if self.workspace_dir is None:
            joined = os.path.join(segment_cwd, expanded) if segment_cwd else expanded
            return [self._clean_rel_key(joined)]
        ws_resolved = self.workspace_dir.resolve()
        keys = [
            (base / expanded).resolve().relative_to(ws_resolved).as_posix()
            for base in base_dirs
            if (base / expanded).resolve().is_relative_to(ws_resolved)
        ]
        return keys or [self._clean_rel_key(expanded)]

    @staticmethod
    def _is_planned_in_or_before_wave(
        candidate_keys: Sequence[str],
        cur_wave_idx: int | None,
        planned_new: dict[str, int],
    ) -> bool:
        for key in candidate_keys:
            planned_idx = planned_new.get(key)
            if planned_idx is not None and (
                cur_wave_idx is None or planned_idx <= cur_wave_idx
            ):
                return True
        return False

    def _path_exists_or_planned(
        self,
        raw_target: str,
        segment_cwd: str | None,
        cur_wave_idx: int | None,
        planned_new: dict[str, int],
    ) -> bool:
        expanded = os.path.expanduser(raw_target)
        base_dirs = self._candidate_search_dirs(segment_cwd)
        if os.path.isabs(expanded):
            if Path(expanded).exists():
                return True
        elif any((base / expanded).exists() for base in base_dirs):
            return True
        keys = self._candidate_planned_keys(expanded, segment_cwd, base_dirs)
        return self._is_planned_in_or_before_wave(keys, cur_wave_idx, planned_new)

    def _verify_outer_executable(
        self,
        outer_exe: str,
        label: str,
        line_num: int,
        segment_cwd: str | None,
        cur_wave_idx: int | None,
        planned_new: dict[str, int],
    ) -> bool:
        if not outer_exe or outer_exe in _SHELL_BUILTINS:
            return True
        if "/" in outer_exe:
            if self._path_exists_or_planned(
                outer_exe, segment_cwd, cur_wave_idx, planned_new
            ):
                return True
            self.issues.append(
                ValidationIssue(
                    is_error=self.strict,
                    message=(
                        f"{label} verification references executable path "
                        f"'{outer_exe}', which does not exist on disk."
                    ),
                    line_num=line_num,
                    file_path=outer_exe,
                )
            )
            return False
        if shutil.which(outer_exe) is not None:
            return True
        self.issues.append(
            ValidationIssue(
                is_error=self.strict,
                message=(
                    f"{label} verification references command '{outer_exe}', "
                    "which was not found on PATH."
                ),
                line_num=line_num,
            )
        )
        return False

    def _collect_verification_commands(self) -> list[tuple[str, int, str, str]]:
        entries: list[tuple[str, int, str, str]] = []
        for wave in self.doc.waves:
            if wave.verification:
                entries.append(
                    (f"Wave {wave.wave_id}", wave.line, wave.wave_id, wave.verification)
                )
        for track in self.doc.tracks:
            if track.verification:
                entries.append(
                    (track.track_id, track.line, track.wave_id, track.verification)
                )
        return entries

    def _check_verification_segment(
        self,
        argv: tuple[str, ...],
        segment_cwd: str | None,
        label: str,
        line_num: int,
        cur_wave_idx: int | None,
        planned_new: dict[str, int],
    ) -> None:
        if not argv or argv[0] in _SHELL_BUILTINS:
            return
        outer_exe, inner_argv = _unwrap_command(argv)
        if not self._verify_outer_executable(
            outer_exe, label, line_num, segment_cwd, cur_wave_idx, planned_new
        ):
            return
        script_arg = _extract_python_script_arg(inner_argv)
        if script_arg is None:
            return
        wrapper_cwd, _ = _extract_wrapper_scope(argv, inner_argv, outer_exe)
        effective_cwd = _combine_cwd(segment_cwd, wrapper_cwd)
        if not self._path_exists_or_planned(
            script_arg, effective_cwd, cur_wave_idx, planned_new
        ):
            self.issues.append(
                ValidationIssue(
                    is_error=self.strict,
                    message=(
                        f"{label} verification runs Python script '{script_arg}', "
                        "which does not exist on disk."
                    ),
                    line_num=line_num,
                    file_path=script_arg,
                )
            )

    def _check_verification_tools(self) -> None:
        """Verifies external tools and local Python scripts when `check_tools` is enabled."""
        if not self.check_tools:
            return
        wave_order = self._wave_order_map()
        planned_new = self._planned_new_by_wave()
        for label, line_num, wave_id, cmd in self._collect_verification_commands():
            if _PLACEHOLDER_CMD_RE.search(cmd):
                continue
            cur_wave_idx = wave_order.get(wave_id)
            for segment in split_shell_segments(cmd):
                self._check_verification_segment(
                    segment.argv,
                    segment.cwd,
                    label,
                    line_num,
                    cur_wave_idx,
                    planned_new,
                )

    @staticmethod
    def _is_parallel_wave(
        wave: WaveInfo, waves_map: dict[str, list[TrackInfo]]
    ) -> bool:
        return wave.mode == "Parallel" or len(waves_map.get(wave.wave_id, ())) >= 2

    def _check_missing_wave_barriers(
        self, waves_map: dict[str, list[TrackInfo]]
    ) -> None:
        for idx, wave in enumerate(self.doc.waves):
            if (
                self._is_parallel_wave(wave, waves_map)
                and idx + 1 < len(self.doc.waves)
                and not wave.has_barrier
            ):
                next_wave = self.doc.waves[idx + 1]
                self.issues.append(
                    ValidationIssue(
                        is_error=self.strict,
                        message=(
                            f"Parallel Wave {wave.wave_id} is followed by Wave "
                            f"{next_wave.wave_id} but is missing a wave-barrier gate "
                            f"(e.g. '**Wave {wave.wave_id} Verifier & Barrier Gate**:')."
                        ),
                        line_num=wave.line,
                    )
                )

    def _check_share_commit_handoffs(
        self, waves_map: dict[str, list[TrackInfo]]
    ) -> None:
        exec_text = section_text(self.content, self.doc, r"execution.*strategy")
        if not _uses_share_workspace(exec_text):
            return
        for idx in range(1, len(self.doc.waves)):
            wave = self.doc.waves[idx]
            prev = self.doc.waves[idx - 1]
            if (
                self._is_parallel_wave(wave, waves_map)
                and not self._is_parallel_wave(prev, waves_map)
                and not prev.mentions_commit_handoff
            ):
                self.issues.append(
                    ValidationIssue(
                        is_error=self.strict,
                        message=(
                            f"Wave {prev.wave_id} precedes parallel Wave {wave.wave_id} "
                            "under swarm workspace 'share', which clones from committed "
                            f"HEAD; note committing or merging Wave {prev.wave_id} "
                            f"before spawning Wave {wave.wave_id} workers."
                        ),
                        line_num=prev.line,
                    )
                )

    def _check_parallel_handoff_gates(
        self, waves: dict[str, list[TrackInfo]] | None = None
    ) -> None:
        """Checks parallel wave barrier gates and `workspace: "share"` commit handoffs."""
        if waves is None:
            waves = self._parallel_waves_map()
        self._check_missing_wave_barriers(waves)
        self._check_share_commit_handoffs(waves)

    def _check_track_interfaces(
        self, waves: dict[str, list[TrackInfo]] | None = None
    ) -> None:
        """Warns when parallel tracks in a multi-wave plan omit both Consumes and Produces."""
        if len(self.doc.waves) < 2:
            return
        if waves is None:
            waves = self._parallel_waves_map()
        for wave_id, tracks in waves.items():
            if len(tracks) < 2:
                continue
            for track in tracks:
                if not track.consumes and not track.produces:
                    self.issues.append(
                        ValidationIssue(
                            is_error=self.strict,
                            message=(
                                f"Parallel track '{track.track_id}' in Wave {wave_id} "
                                "is missing '**Consumes**:' / '**Produces**:' "
                                "interface contract lines."
                            ),
                            line_num=track.line,
                        )
                    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate implementation plan markdown documents."
    )
    parser.add_argument(
        "plan_file",
        nargs="?",
        type=str,
        help="Path to the implementation plan markdown file (reads stdin if omitted).",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        type=str,
        default=None,
        help=(
            "Path to a repository root to verify file paths against disk "
            "(repeatable; the first is the primary workspace)."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors.",
    )
    parser.add_argument(
        "--check-tools",
        action="store_true",
        help="Verify that binaries and local scripts in wave and track verification commands exist.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output validation results in JSON format.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only output on validation errors.",
    )
    return parser.parse_args(argv)


def _read_plan_content(plan_file: str | None) -> str | None:
    """Reads plan markdown from a file path or stdin, returning None on error."""
    if plan_file:
        plan_path = Path(plan_file)
        if not plan_path.exists():
            print(f"Error: Plan file not found: {plan_path}", file=sys.stderr)
            return None
        return plan_path.read_text(encoding="utf-8")

    if sys.stdin.isatty():
        print("Error: No plan file provided and stdin is empty.", file=sys.stderr)
        return None
    return sys.stdin.read()


def _print_human_results(
    issues: list[ValidationIssue],
    errors: list[ValidationIssue],
    warnings: list[ValidationIssue],
    strict: bool,
    quiet: bool,
) -> None:
    """Formats and prints human-readable validation results."""
    if issues and not quiet:
        for issue in issues:
            print(str(issue))

    if not errors and not (strict and warnings):
        if not quiet:
            print(f"✓ Plan passed validation ({len(warnings)} warning(s)).")
            print(NEXT_STEP_MESSAGE)
    else:
        print(
            f"✗ Plan validation failed: {len(errors)} error(s), {len(warnings)} warning(s).",
            file=sys.stderr,
        )


def _resolve_workspace_dir(workspace: str | None) -> tuple[Path | None, bool]:
    """Resolves and validates a single workspace directory path."""
    if not workspace:
        return None, True
    workspace_dir = Path(workspace).resolve()
    if not workspace_dir.is_dir():
        print(
            f"Error: Workspace directory does not exist: {workspace_dir}",
            file=sys.stderr,
        )
        return None, False
    return workspace_dir, True


def _resolve_workspace_dirs(
    workspaces: Sequence[str] | str | None,
) -> tuple[Path | None, tuple[Path, ...], bool]:
    """Resolves primary and extra workspace directories from CLI arguments."""
    if not workspaces:
        return None, (), True
    raw_items = [workspaces] if isinstance(workspaces, str) else list(workspaces)
    resolved: list[Path] = []
    for item in raw_items:
        ws_dir, ok = _resolve_workspace_dir(item)
        if not ok:
            return None, (), False
        if ws_dir is not None:
            resolved.append(ws_dir)
    if not resolved:
        return None, (), True
    return resolved[0], tuple(resolved[1:]), True


def _print_json_results(
    issues: list[ValidationIssue],
    errors: list[ValidationIssue],
    warnings: list[ValidationIssue],
    is_valid: bool,
    quiet: bool = False,
    file_action_count: int = 0,
) -> None:
    """Formats and prints JSON validation results."""
    output_payload: dict[str, Any] = {
        "valid": is_valid,
        "errors": [err.to_dict() for err in errors],
        "warnings": [warn.to_dict() for warn in warnings],
        "total_issues": len(issues),
        "file_action_count": file_action_count,
    }
    if is_valid and not quiet:
        output_payload["next_step"] = NEXT_STEP_MESSAGE
    print(json.dumps(output_payload, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    content = _read_plan_content(args.plan_file)
    if content is None:
        return 2

    workspace_dir, extra_workspaces, valid_ws = _resolve_workspace_dirs(args.workspace)
    if not valid_ws:
        return 2

    validator = PlanValidator(
        content=content,
        workspace_dir=workspace_dir,
        strict=args.strict,
        extra_workspaces=extra_workspaces,
        check_tools=args.check_tools,
    )
    issues = validator.validate()

    errors = [issue for issue in issues if issue.is_error]
    warnings = [issue for issue in issues if not issue.is_error]
    has_fatal_failure = bool(errors) or (args.strict and bool(warnings))

    if args.json:
        _print_json_results(
            issues,
            errors,
            warnings,
            not has_fatal_failure,
            args.quiet,
            validator.doc.file_action_count,
        )
    else:
        _print_human_results(issues, errors, warnings, args.strict, args.quiet)

    return 1 if has_fatal_failure else 0


if __name__ == "__main__":
    sys.exit(main())
