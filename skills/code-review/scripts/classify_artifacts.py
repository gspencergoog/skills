#!/usr/bin/env python3
"""Classifies changed files as code or as a document type for code-review.

The code-review skill uses this to decide which files get the code review
mandate and which get the document review mandate, and which rubric applies.

Only prose files (.md, .rst, .adoc, .txt) and JSON Schema files (*.schema.json,
or .json files whose "$schema" names a json-schema.org meta-schema) can be
documents. Everything else is "code". For eligible files, ranked rules are
checked in order and the first match wins. Each result names the rule that
matched so the caller can override a wrong call.

Usage:
  classify_artifacts.py [--root DIR] [--no-content] PATH...
  classify_artifacts.py [--root DIR] [--ring | --combined] [--diff-file DIFF] [PATH...]
  git diff --name-only main...HEAD | classify_artifacts.py [--root DIR] -

Prints a JSON list of {"path", "type", "rule", "rubric"} objects (or a JSON
object under --ring or --combined).
Exits 0 on success and 2 on a usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

_CONTEXT_INDEXER_SCRIPTS = str(
    Path(__file__).resolve().parents[2] / "context-indexer" / "scripts"
)
if _CONTEXT_INDEXER_SCRIPTS not in sys.path:
    sys.path.insert(0, _CONTEXT_INDEXER_SCRIPTS)

import diff_weight
from stage_diffs import _extract_file_name, _split_into_file_diffs

DOC_TYPES = (
    "normative-spec",
    "design",
    "blueprint",
    "agent-instructions",
    "reference-doc",
)
PROSE_EXTENSIONS = (".md", ".rst", ".adoc", ".txt")
SNIFF_BYTES = 8192

RUBRICS = {
    "normative-spec": "references/rubrics/normative_specs.md",
    "design": "references/rubrics/design_docs.md",
    "blueprint": "references/rubrics/blueprints.md",
    "agent-instructions": "references/rubrics/agent_instructions.md",
    "reference-doc": "references/rubrics/reference_docs.md",
}

_AGENT_INSTRUCTION_FILES = frozenset(
    {
        "skill.md",
        "agents.md",
        "claude.md",
        "gemini.md",
        ".cursorrules",
        ".clinerules",
        ".windsurfrules",
    }
)
_AGENT_INSTRUCTION_DIRS = frozenset({"rules", "policies", "prompts", "agents"})

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_BLUEPRINT_TYPE = re.compile(
    r"^type:\s*['\"]?(module|feature|codebase)['\"]?\s*$", re.MULTILINE
)
# A JSON Schema document declares a json-schema.org meta-schema. Config files
# that only point at a validation schema (tsconfig, schemastore) do not.
_JSON_SCHEMA_META = re.compile(r'"\$schema"\s*:\s*"https?://json-schema\.org/')
_FENCED_BLOCK = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.DOTALL | re.MULTILINE)
_DESIGN_HEADING = re.compile(
    r"^(?:#{1,6}\s*(?:alternatives?(?:\s+considered)?|non-goals|decision"
    r"|drawbacks|unresolved questions|consequences)\b"
    r"|\**status\**:)",
    re.IGNORECASE | re.MULTILINE,
)
# RFC 2119 / RFC 8174: only the uppercase forms are normative.
_RFC2119 = re.compile(r"\b(?:MUST|SHALL|SHOULD|REQUIRED|RECOMMENDED|MAY|OPTIONAL)\b")

_DESIGN_DIRS = frozenset({"rfcs", "rfc", "adr", "adrs", "proposals"})
_SPEC_DIRS = frozenset({"spec", "specs", "specification", "specifications"})


class Rule(NamedTuple):
    """One ranked classifier rule."""

    name: str
    doc_type: str
    matches: Callable[[PurePosixPath, str], bool]


def _frontmatter(content: str) -> str:
    match = _FRONTMATTER.match(content)
    return match.group(1) if match else ""


def _is_blueprint(path: PurePosixPath, content: str) -> bool:
    if path.name.endswith(".blueprint.md"):
        return True
    return bool(_BLUEPRINT_TYPE.search(_frontmatter(content)))


def _prose_only(content: str) -> str:
    """Removes fenced code blocks so quoted code and prompts don't count."""
    return _FENCED_BLOCK.sub("", content)


def _is_schema(path: PurePosixPath, content: str) -> bool:
    return path.suffix.lower() == ".json"


def _in_dirs(path: PurePosixPath, dirs: frozenset[str]) -> bool:
    return any(part.lower() in dirs for part in path.parts[:-1])


def _is_design(path: PurePosixPath, content: str) -> bool:
    if _in_dirs(path, _DESIGN_DIRS):
        return True
    return len(_DESIGN_HEADING.findall(_prose_only(content))) >= 2


def _is_normative_prose(path: PurePosixPath, content: str) -> bool:
    if _in_dirs(path, _SPEC_DIRS):
        return True
    lines = _prose_only(content).splitlines()
    keyword_lines = sum(1 for line in lines if _RFC2119.search(line))
    return keyword_lines >= 3


def _has_dir_sequence(parts_lower: list[str], sequence: tuple[str, ...]) -> bool:
    """Returns whether `sequence` appears as contiguous directory segments."""
    span = len(sequence)
    return any(
        tuple(parts_lower[i : i + span]) == sequence
        for i in range(len(parts_lower) - span + 1)
    )


def _is_agent_instructions(path: PurePosixPath, content: str) -> bool:
    """Returns whether `path` is an executable agent-instruction document."""
    del content  # Path-only rule.
    name_lower = path.name.lower()
    if name_lower in _AGENT_INSTRUCTION_FILES:
        return True
    if path.suffix.lower() not in (".md", ".mdx"):
        return False
    dir_parts = [part.lower() for part in path.parts[:-1]]
    if any(part in _AGENT_INSTRUCTION_DIRS for part in dir_parts):
        return True
    return _has_dir_sequence(dir_parts, (".github", "instructions"))


def _is_changelog(path: PurePosixPath, content: str) -> bool:
    del content  # Path-only rule.
    return path.name.upper().startswith("CHANGELOG")


RULES: tuple[Rule, ...] = (
    Rule("blueprint-frontmatter-or-name", "blueprint", _is_blueprint),
    Rule("agent-instructions-path", "agent-instructions", _is_agent_instructions),
    Rule("json-schema", "normative-spec", _is_schema),
    Rule("design-headings-or-dir", "design", _is_design),
    Rule("rfc2119-keywords-or-spec-dir", "normative-spec", _is_normative_prose),
    Rule("changelog", "reference-doc", _is_changelog),
    Rule("prose-default", "reference-doc", lambda _path, _content: True),
)


def is_document_eligible(path: str, content: str | None = None) -> bool:
    """Returns whether `path` can be classified as a document.

    Prose files (`.md`, `.rst`, `.adoc`, `.txt`), paths matching the
    agent-instructions rule (including `.cursorrules`, `.clinerules`,
    `.windsurfrules`, and `.mdx` under agent-instruction directories), and
    JSON Schema documents (`*.schema.json` or `.json` files declaring a
    json-schema.org meta-schema) are eligible. Other `.mdx` files stay `code`.
    """
    pure = PurePosixPath(path.replace("\\", "/"))
    name = pure.name.lower()
    if (
        _is_agent_instructions(pure, "")
        or name.endswith(PROSE_EXTENSIONS)
        or name.endswith(".schema.json")
    ):
        return True
    return name.endswith(".json") and bool(_JSON_SCHEMA_META.search(content or ""))


# ---------------------------------------------------------------------------
# Generalized Reuse Ring Classification (Blast Radius & Review Depth)
# ---------------------------------------------------------------------------

RING_LABELS: dict[int, str] = {
    0: "Ring 0 (Private / Local)",
    1: "Ring 1 (Internal Tooling & Repo Plumbing)",
    2: "Ring 2 (Application / Service Logic)",
    3: "Ring 3 (Shared Libraries & Public Contracts)",
    4: "Ring 4 (Foundational Runtimes & Core Engines)",
}

RING_LEVELS: dict[int, tuple[str, list[str]]] = {
    0: ("fast", ["--fast"]),
    1: ("standard", []),
    2: ("standard", []),
    3: ("panel", ["--panel"]),
    4: ("max", ["--max"]),
}

PANEL_MIN_LINES = 225
RISK_ESCALATION_MIN_LINES = 350
FOCUS_MIN_LINES = 400

_CI_DIRS = frozenset({".github", ".gitlab", ".circleci", ".buildkite", "jenkins"})
_CI_FILES = frozenset(
    {
        "jenkinsfile",
        ".gitlab-ci.yml",
        ".travis.yml",
        ".cirrus.yml",
    }
)

_TOOL_CONFIG_FILES = frozenset(
    {
        "makefile",
        "cmakelists.txt",
        "dockerfile",
        "containerfile",
        "taskfile.yml",
        "taskfile.yaml",
        "justfile",
        "vagrantfile",
    }
)

_TOOL_CONFIG_PREFIXES = (
    ".eslintrc",
    "eslint.config.",
    "tsconfig",
    ".prettierrc",
    "analysis_options",
    "ruff.",
    ".flake8",
    ".rubocop",
    ".golangci",
)

_TOOL_SCRIPT_DIRS = frozenset({"scripts", "tools", "tool", "tasks", "dev"})

_TEST_DIRS = frozenset(
    {
        "test",
        "tests",
        "__tests__",
        "testing",
        "fixtures",
        "mocks",
        "testdata",
    }
)

_REPO_META_FILES = frozenset(
    {
        "readme.md",
        "contributing.md",
        "license",
        "license.md",
        "license.txt",
        "code_of_conduct.md",
        "agents.md",
        "gemini.md",
    }
)

_SCRATCH_DIRS = frozenset(
    {
        "scratch",
        "tmp",
        "temp",
        "sandbox",
        "experiments",
        "playground",
        "drafts",
        "notes",
        "personal",
    }
)

_DOTFILE_DIRS = frozenset({".config", ".local", "dotfiles", "cheats"})

_DOTFILE_NAMES = frozenset(
    {
        "bashrc",
        ".bashrc",
        "bash_profile",
        ".bash_profile",
        "zshrc",
        ".zshrc",
        "cheats",
        ".cheats",
    }
)

_ENGINE_DIRS = frozenset(
    {
        "engine",
        "runtime",
        "vm",
        "compiler",
        "kernel",
        "allocator",
        "gc",
        "skwasm",
        "wimp",
    }
)

_NATIVE_DIRS = frozenset({"native", "ffi", "cbits", "jni"})

_SCHEMA_DIRS = frozenset({"protos", "proto", "schemas", "schema"})

_PUBLIC_EXPORT_DIRS = frozenset({"api", "public", "exports", "include"})

_PACKAGE_MANIFESTS = frozenset(
    {
        "package.json",
        "pubspec.yaml",
        "cargo.toml",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pom.xml",
        "go.mod",
        "api.txt",
    }
)


_NATIVE_CORE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".rs")
_NATIVE_CORE_DIRS = frozenset({"engine", "native", "runtime", "vm", "core"})
_PACKAGE_ROOT_ENTRYPOINTS = frozenset(
    {"index.ts", "index.js", "public-api.ts", "mod.ts"}
)
_TEST_FILE_SUFFIXES = (
    "_test.dart",
    "_test.py",
    "_test.go",
    ".test.ts",
    ".test.js",
    ".spec.ts",
    ".spec.js",
)

_RISK_SIGNAL_ORDER: tuple[str, ...] = (
    "concurrency-or-locks",
    "auth-or-trust-boundary",
    "schema-or-sql",
    "subprocess-or-ci-permissions",
    "retry-or-state-machine",
)
_AUTH_PATH_SEGMENTS = frozenset(
    {"auth", "authn", "authz", "oauth", "oauth2", "rbac", "acl", "jwt", "security"}
)
_SCHEMA_PATH_SEGMENTS = frozenset({"migrations", "migration", "alembic"})
_SCHEMA_FILE_SUFFIXES = (".sql", ".prisma", ".graphql")
_PROSE_RISK_SKIP_SUFFIXES = (".md", ".mdx", ".rst", ".adoc", ".txt")

_RISK_CODE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "concurrency-or-locks",
        re.compile(
            r"\b(?:threading\.(?:Lock|RLock|Semaphore|Condition|Event|Barrier|Thread)"
            r"|multiprocessing\.(?:Lock|RLock|Queue|Pool|Process|Manager)"
            r"|asyncio\.(?:Lock|Semaphore|Event|Condition|Barrier|gather|TaskGroup|create_task)"
            r"|fcntl\.flock|msvcrt\.locking|filelock"
            r"|Mutex|RwLock|ReentrantLock|synchronized|CountDownLatch"
            r"|Atomic(?:Integer|Long|Bool|Reference)"
            r"|std::(?:mutex|lock_guard|unique_lock|shared_mutex|atomic)"
            r"|sync\.(?:Mutex|RWMutex|WaitGroup)|pthread_mutex"
            r"|Isolate\.spawn|ReceivePort"
            r"|acquire_lock|release_lock|with_lock"
            r"|BEGIN\s+(?:IMMEDIATE|EXCLUSIVE))\b"
        ),
    ),
    (
        "auth-or-trust-boundary",
        re.compile(
            r"\b(?:jwt\.(?:encode|decode|verify)|PyJWT|jsonwebtoken|OAuth2"
            r"|verify_token|validate_token|check_permission|has_permission"
            r"|require_auth|authenticate_user|authorize_request"
            r"|hmac\.compare_digest|secrets\.compare_digest"
            r"|bcrypt|scrypt|argon2|pbkdf2|hashlib\.pbkdf2_hmac"
            r"|ssl\.create_default_context|CERT_NONE|check_hostname\s*=\s*False"
            r"|Access-Control-Allow-Origin|Content-Security-Policy"
            r"|Set-Cookie|HttpOnly|SameSite|csrf_token|xsrf)\b"
        ),
    ),
    (
        "schema-or-sql",
        re.compile(
            r"\b(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|INDEX|VIEW|TRIGGER|MACRO)"
            r"|ALTER\s+TABLE|DROP\s+(?:TABLE|INDEX|COLUMN)"
            r"|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM"
            r"|PRAGMA\s+\w+"
            r"|sqlite3\.connect|duckdb\.connect"
            r"|op\.(?:add_column|drop_column|create_table|alter_column)"
            r"|SCHEMA_VERSION|MIGRATION_VERSION|migrate_schema)\b"
        ),
    ),
    (
        "subprocess-or-ci-permissions",
        re.compile(
            r"(?:\b(?:subprocess\.(?:run|Popen|call|check_call|check_output)"
            r"|os\.(?:system|popen|exec\w*|spawn\w*)|pty\.spawn"
            r"|asyncio\.create_subprocess_(?:exec|shell)"
            r"|child_process|execSync|spawnSync|execFile(?:Sync)?"
            r"|Process\.(?:run|runSync|start)|Command::new|exec\.Command)\b"
            r"|shell\s*=\s*True)"
        ),
    ),
    (
        "retry-or-state-machine",
        re.compile(
            r"\b(?:exponential_backoff|backoff_seconds|backoff_delay"
            r"|retry_after|max_retries|retry_count|retry_delay|with_retry"
            r"|RetryPolicy|BackoffPolicy|tenacity\.retry"
            r"|state_machine|StateMachine|VALID_TRANSITIONS|STATE_TRANSITIONS"
            r"|allowed_transitions|transition_to|circuit_breaker|CircuitBreaker"
            r"|idempotency_key|dead_letter)\b"
        ),
    ),
)
_CI_PERMISSION_LINE_RE = re.compile(
    r"^\s*(?:permissions:|pull_request_target\b"
    r"|id-token:\s*write|contents:\s*write|packages:\s*write"
    r"|security-events:\s*write|write-all\b)"
)


def _is_ring0_private(parts_lower: list[str], name_lower: str) -> str | None:
    """Returns a Ring 0 rule name if the path is private, scratch, or a dotfile."""
    dir_parts = parts_lower[:-1]
    if any(part in _SCRATCH_DIRS for part in dir_parts):
        return "scratch-or-sandbox"
    if any(tag in name_lower for tag in (".scratch.", ".local.", ".private.")):
        return "scratch-or-private-file"
    if any(part in _DOTFILE_DIRS for part in dir_parts) or name_lower in _DOTFILE_NAMES:
        return "personal-dotfiles"
    if (
        name_lower.startswith(".")
        and not any(part in _CI_DIRS for part in parts_lower)
        and not name_lower.startswith(_TOOL_CONFIG_PREFIXES)
    ):
        return "dotfile"
    return None


def _is_ring4_engine(parts_lower: list[str], name_lower: str) -> str | None:
    """Returns a Ring 4 rule name if the path belongs to a core engine or runtime."""
    dir_parts = parts_lower[:-1]
    if any(part in _ENGINE_DIRS for part in dir_parts):
        return "core-engine-or-runtime"
    if any(part in _NATIVE_DIRS for part in dir_parts):
        return "native-or-ffi-plumbing"
    if name_lower.endswith(_NATIVE_CORE_SUFFIXES) and any(
        part in _NATIVE_CORE_DIRS for part in parts_lower
    ):
        return "native-core-code"
    return None


def _is_ring3_contract(
    pure: PurePosixPath, parts_lower: list[str], name_lower: str
) -> str | None:
    """Returns a Ring 3 rule name if the path is a public contract or library surface."""
    dir_parts = parts_lower[:-1]
    if any(part in _SCHEMA_DIRS for part in dir_parts) or name_lower.endswith(
        (".proto", ".schema.json")
    ):
        return "schema-or-protocol-contract"
    if name_lower in _PACKAGE_MANIFESTS:
        return "package-manifest-or-api-surface"
    if any(part in _PUBLIC_EXPORT_DIRS for part in dir_parts):
        return "public-export-directory"
    if (
        len(pure.parts) == 2
        and pure.parts[0].lower() == "lib"
        and name_lower.endswith(".dart")
    ):
        return "dart-library-entrypoint"
    if name_lower in _PACKAGE_ROOT_ENTRYPOINTS and len(pure.parts) <= 2:
        return "package-root-entrypoint"
    return None


def _is_ring1_tooling(parts_lower: list[str], name_lower: str) -> str | None:
    """Returns a Ring 1 rule name if the path is CI, tooling, test, or repo metadata."""
    dir_parts = parts_lower[:-1]
    if any(part in _CI_DIRS for part in dir_parts) or name_lower in _CI_FILES:
        return "ci-workflow-or-pipeline"
    if name_lower in _TOOL_CONFIG_FILES or name_lower.startswith(_TOOL_CONFIG_PREFIXES):
        return "build-or-linter-config"
    if any(part in _TOOL_SCRIPT_DIRS for part in dir_parts):
        return "repo-tooling-or-scripts"
    if (
        any(part in _TEST_DIRS for part in dir_parts)
        or name_lower.startswith("test_")
        or name_lower.endswith(_TEST_FILE_SUFFIXES)
    ):
        return "test-suite-or-fixture"
    if name_lower in _REPO_META_FILES:
        return "repo-documentation"
    return None


def classify_file_ring(path: str) -> tuple[int, str]:
    """Classifies a single file into a reuse ring (0 to 4) and reason rule."""
    pure = PurePosixPath(path.replace("\\", "/"))
    parts_lower = [p.lower() for p in pure.parts]
    name_lower = pure.name.lower()

    if rule := _is_ring0_private(parts_lower, name_lower):
        return 0, rule
    if rule := _is_ring4_engine(parts_lower, name_lower):
        return 4, rule
    if rule := _is_ring3_contract(pure, parts_lower, name_lower):
        return 3, rule
    if rule := _is_ring1_tooling(parts_lower, name_lower):
        return 1, rule
    return 2, "application-logic"


def _is_ci_workflow_path(parts_lower: list[str], name_lower: str) -> bool:
    """Returns whether the path is a CI workflow definition file."""
    return (
        _has_dir_sequence(parts_lower[:-1], (".github", "workflows"))
        or name_lower in _CI_FILES
    )


def _scan_block_path_risks(file_path: str, found: set[str]) -> bool:
    """Records path-based risk signals and returns whether the file is a CI workflow."""
    pure = PurePosixPath(file_path.replace("\\", "/"))
    parts_lower = [part.lower() for part in pure.parts]
    dir_parts = parts_lower[:-1]
    name_lower = pure.name.lower()
    if any(part in _AUTH_PATH_SEGMENTS for part in dir_parts):
        found.add("auth-or-trust-boundary")
    if any(part in _SCHEMA_PATH_SEGMENTS for part in dir_parts) or name_lower.endswith(
        _SCHEMA_FILE_SUFFIXES
    ):
        found.add("schema-or-sql")
    return _is_ci_workflow_path(parts_lower, name_lower)


def _scan_added_risk_line(content: str, is_ci_workflow: bool, found: set[str]) -> None:
    """Matches one added logic line against high-precision risk-escalation patterns."""
    if is_ci_workflow and _CI_PERMISSION_LINE_RE.search(content):
        found.add("subprocess-or-ci-permissions")
    if not diff_weight.is_logic_line(content):
        return
    for signal_name, pattern in _RISK_CODE_PATTERNS:
        if signal_name not in found and pattern.search(content):
            found.add(signal_name)


def _iter_added_lines(file_block: str) -> list[str]:
    """Returns the text of added (`+`) hunk lines inside one per-file diff block."""
    added: list[str] = []
    in_hunk = False
    for raw_line in file_block.splitlines():
        if raw_line.startswith("@@ "):
            in_hunk = True
            continue
        if in_hunk and raw_line.startswith("+") and not raw_line.startswith("+++ "):
            added.append(raw_line[1:])
    return added


def _is_risk_scan_candidate(file_path: str) -> bool:
    """Returns whether `file_path` is a production code or CI config file to scan."""
    if not file_path:
        return False
    if diff_weight.classify_low_content_path(file_path) is not None:
        return False
    if diff_weight.is_test_path(file_path):
        return False
    suffix = PurePosixPath(file_path.replace("\\", "/")).suffix.lower()
    return suffix not in _PROSE_RISK_SKIP_SUFFIXES


def _scan_file_block_risks(block: str, idx: int, found: set[str]) -> None:
    """Extracts added lines from one file diff block and records matched risk signals."""
    file_path = _extract_file_name(block, idx)
    if not _is_risk_scan_candidate(file_path):
        return
    added_lines = _iter_added_lines(block)
    if not added_lines:
        return
    is_ci_workflow = _scan_block_path_risks(file_path, found)
    for line in added_lines:
        _scan_added_risk_line(line, is_ci_workflow, found)


def scan_ring_risk_signals(diff_text: str) -> list[str]:
    """Scans a unified diff for high-risk signals that justify Ring 1/2 escalation.

    Inspects only added (`+`) logic lines and changed production paths, skipping
    low-content files, test files, and prose documents. Returns matched signal
    names in canonical order from `concurrency-or-locks`,
    `auth-or-trust-boundary`, `schema-or-sql`, `subprocess-or-ci-permissions`,
    and `retry-or-state-machine`.
    """
    if not diff_text:
        return []
    found: set[str] = set()
    for idx, block in enumerate(_split_into_file_diffs(diff_text)):
        _scan_file_block_risks(block, idx, found)
    return [sig for sig in _RISK_SIGNAL_ORDER if sig in found]


def _score_ring_files(
    paths: list[str],
) -> tuple[list[dict], int, str | None, str | None]:
    scored_files = []
    max_ring = -1
    highest_file = None
    highest_rule = None
    for path in paths:
        ring, rule = classify_file_ring(path)
        scored_files.append({"path": path, "ring": ring, "rule": rule})
        if ring > max_ring:
            max_ring = ring
            highest_file = path
            highest_rule = rule
    return scored_files, max(0, max_ring), highest_file, highest_rule


def _normalize_risk_signals(risk_signals: Sequence[str] | None) -> list[str]:
    """Deduplicates non-empty risk signal labels while preserving order."""
    if not risk_signals:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in risk_signals:
        sig = str(raw).strip()
        if sig and sig not in seen:
            seen.add(sig)
            ordered.append(sig)
    return ordered


def _apply_ring_volume_gates(
    effective_ring: int,
    level: str,
    flags: list[str],
    substantive_lines: float | None,
    panel_min_lines: int,
    signals: list[str],
) -> tuple[str, list[str], bool, str | None]:
    """Applies Ring 3 volume downgrade and Ring 1/2 2D risk-by-volume escalation."""
    if substantive_lines is None:
        return level, flags, False, None
    if effective_ring == 3 and substantive_lines <= panel_min_lines:
        reason = (
            f"substantive_lines ({substantive_lines:g}) <= "
            f"panel_min_lines ({panel_min_lines})"
        )
        return "standard", [], True, reason
    if (
        effective_ring in (1, 2)
        and substantive_lines > RISK_ESCALATION_MIN_LINES
        and signals
    ):
        reason = (
            f"substantive_lines ({substantive_lines:g}) > "
            f"risk_escalation_min_lines ({RISK_ESCALATION_MIN_LINES}) "
            f"with risk_signals ({', '.join(signals)})"
        )
        return "panel", ["--panel"], False, reason
    return level, flags, False, None


def classify_ring(
    paths: list[str],
    root: Path | str | None = None,
    substantive_lines: float | None = None,
    panel_min_lines: int = PANEL_MIN_LINES,
    risk_signals: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Classifies a changeset into an effective ring and default review level.

    Applies two volume gates when `substantive_lines` is supplied:

    - Ring 3 with `substantive_lines <= panel_min_lines` downgrades to
      `review_level = "standard"`, `flags = []`, and `api_mandate = True`.
    - Ring 1 or Ring 2 with `substantive_lines > RISK_ESCALATION_MIN_LINES` and
      at least one signal in `risk_signals` escalates to `review_level = "panel"`
      and `flags = ["--panel"]`.
    """
    del root  # Reserved for future manifest inspection.
    signals = _normalize_risk_signals(risk_signals)
    if not paths:
        level, flags = RING_LEVELS[2]
        return {
            "ring": 2,
            "label": RING_LABELS[2],
            "review_level": level,
            "flags": list(flags),
            "api_mandate": False,
            "gate_reason": None,
            "risk_signals": signals,
            "summary": (
                f"{RING_LABELS[2]} -> Review Level: {level.capitalize()} (1 reviewer)"
            ),
            "highest_file": None,
            "highest_rule": "default-fallback",
            "files": [],
        }

    scored_files, effective_ring, highest_file, highest_rule = _score_ring_files(paths)
    default_level, default_flags = RING_LEVELS[effective_ring]
    level, flags, api_mandate, gate_reason = _apply_ring_volume_gates(
        effective_ring,
        default_level,
        list(default_flags),
        substantive_lines,
        panel_min_lines,
        signals,
    )

    summary_extra = f" ({' '.join(flags)})" if flags else " (1 reviewer)"
    summary = (
        f"{RING_LABELS[effective_ring]} -> Review Level:"
        f" {level.capitalize()}{summary_extra}"
    )

    return {
        "ring": effective_ring,
        "label": RING_LABELS[effective_ring],
        "review_level": level,
        "flags": flags,
        "api_mandate": api_mandate,
        "gate_reason": gate_reason,
        "risk_signals": signals,
        "summary": summary,
        "highest_file": highest_file,
        "highest_rule": highest_rule,
        "files": scored_files,
    }


def _extract_paths_from_diff(diff_text: str) -> list[str]:
    """Extracts unique repo-relative file paths from a unified diff in order."""
    seen: set[str] = set()
    paths: list[str] = []
    for i, block in enumerate(_split_into_file_diffs(diff_text)):
        path = _extract_file_name(block, i)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def classify_combined(
    paths: list[str],
    root: Path | str = Path("."),
    diff_text: str | None = None,
    *,
    no_content: bool = False,
) -> dict[str, Any]:
    """Returns combined ring, artifact, and substantive diff classification.

    Computes `substantive` diff stats and static `risk_signals` when `diff_text`
    is supplied, feeding both into `classify_ring`.
    """
    root_path = Path(root)
    risk_signals: list[str] | None = None
    if diff_text is not None:
        stats = diff_weight.compute_substantive_diff_stats(diff_text)
        substantive_lines: float | None = stats.substantive_lines
        risk_signals = scan_ring_risk_signals(diff_text)
        if not paths:
            paths = _extract_paths_from_diff(diff_text)
    else:
        stats = diff_weight.SubstantiveDiffStats()
        substantive_lines = None

    ring_result = classify_ring(
        paths,
        root_path,
        substantive_lines=substantive_lines,
        risk_signals=risk_signals,
    )
    artifacts = [
        classify(path, None if no_content else _read_head(root_path, path))
        for path in paths
    ]
    return {
        "ring": ring_result,
        "artifacts": artifacts,
        "substantive": asdict(stats),
        "focus_dedicated_allowed": (
            substantive_lines is None or substantive_lines > FOCUS_MIN_LINES
        ),
    }


def classify(path: str, content: str | None = None) -> dict:
    """Classifies one file.

    Args:
      path: Repo-relative path of the changed file.
      content: The file's leading text, or None to classify by path only
        (for example, a file the diff deletes).

    Returns:
      {"path", "type", "rule", "rubric"}. "type" is "code" or one of
      DOC_TYPES. "rubric" is the rubric reference path, or None for code.
    """
    if not is_document_eligible(path, content):
        return {"path": path, "type": "code", "rule": "not-prose", "rubric": None}
    pure = PurePosixPath(path.replace("\\", "/"))
    text = content or ""
    for rule in RULES:
        if rule.matches(pure, text):
            return {
                "path": path,
                "type": rule.doc_type,
                "rule": rule.name,
                "rubric": RUBRICS[rule.doc_type],
            }
    raise AssertionError("prose-default rule always matches")


def _read_head(root: Path, path: str) -> str | None:
    """Reads up to `SNIFF_BYTES` from `root / path`, refusing paths outside `root`."""
    raw = Path(path.replace("\\", "/"))
    if raw.is_absolute():
        return None
    try:
        resolved_root = root.resolve()
        target = (resolved_root / raw).resolve()
        if not target.is_relative_to(resolved_root):
            return None
        with open(target, "rb") as handle:
            return handle.read(SNIFF_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def _read_diff_file(diff_file: str) -> str:
    if diff_file == "-":
        return sys.stdin.read()
    with open(diff_file, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify changed files for code-review.",
        exit_on_error=False,
    )
    parser.add_argument("--root", default=".", help="Repo root for reading files.")
    parser.add_argument(
        "--no-content", action="store_true", help="Classify by path only."
    )
    parser.add_argument(
        "--ring",
        action="store_true",
        help="Classify changeset reuse ring (0-4) and default review level.",
    )
    parser.add_argument(
        "--combined",
        action="store_true",
        help="Output combined ring, artifact, and substantive diff classification.",
    )
    parser.add_argument(
        "--diff-file",
        default=None,
        help="Unified diff file for substantive line counting and path extraction.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="File paths, or - to read stdin (optional when --diff-file is set).",
    )
    return parser.parse_args(argv)


def _resolve_paths_and_diff(
    args: argparse.Namespace,
) -> tuple[list[str], str | None]:
    if args.diff_file == "-" and args.paths == ["-"]:
        raise argparse.ArgumentError(
            None,
            "cannot read both --diff-file and paths from stdin (-); omit - so paths come from --diff-file -",
        )
    diff_text = _read_diff_file(args.diff_file) if args.diff_file else None
    paths = list(args.paths)
    if paths == ["-"]:
        paths = [line.strip() for line in sys.stdin if line.strip()]
    elif not paths and diff_text is not None:
        paths = _extract_paths_from_diff(diff_text)
    return paths, diff_text


def _write_json(payload: object) -> int:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str]) -> int:
    """Runs the CLI. Returns the process exit code."""
    try:
        args = _parse_args(argv)
        if not args.paths and not args.diff_file:
            raise argparse.ArgumentError(None, "paths or --diff-file required")
        paths, diff_text = _resolve_paths_and_diff(args)
    except (argparse.ArgumentError, OSError, SystemExit):
        print(__doc__, file=sys.stderr)
        return 2

    root = Path(args.root)
    if args.combined:
        return _write_json(
            classify_combined(paths, root, diff_text, no_content=args.no_content)
        )
    if args.ring:
        substantive_lines = (
            diff_weight.compute_substantive_diff_stats(diff_text).substantive_lines
            if diff_text is not None
            else None
        )
        risk_signals = (
            scan_ring_risk_signals(diff_text) if diff_text is not None else None
        )
        return _write_json(
            classify_ring(
                paths,
                root,
                substantive_lines=substantive_lines,
                risk_signals=risk_signals,
            )
        )
    results = [
        classify(path, None if args.no_content else _read_head(root, path))
        for path in paths
    ]
    return _write_json(results)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
