"""Offline local 384-dim vector embedders for packrat.

Supports two offline CPU backends producing 384-dimensional L2-normalized
FLOAT32 vectors:
1. OnnxArcticEmbedder: local Snowflake/snowflake-arctic-embed-xs via onnxruntime + tokenizers.
2. DeterministicHashEmbedder: zero-network 384-dim semantic cluster + feature-hash
   embedder ported from inflight's src/arcs/embedder.ts.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from db_store import DEFAULT_DATA_DIR, PackratStore
from models import (
    EMBEDDING_DIM,
    StoredEmbeddingRecord,
    TextChunk,
    l2_normalize,
    utc_now_iso,
)
from progress import NULL_REPORTER, ProgressReporter

DEFAULT_MODEL_DIR = DEFAULT_DATA_DIR / "models" / "snowflake-arctic-embed-xs"
ARCTIC_MODEL_ID = "snowflake-arctic-embed-xs"
DETERMINISTIC_MODEL_ID = "deterministic-384-v2"
RETRIEVAL_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

CONCEPT_DIM = 30
HASH_DIM = EMBEDDING_DIM - CONCEPT_DIM

STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "down",
        "during",
        "each",
        "either",
        "else",
        "ever",
        "every",
        "few",
        "for",
        "from",
        "further",
        "get",
        "gets",
        "got",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "however",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "keep",
        "let",
        "like",
        "made",
        "make",
        "makes",
        "many",
        "may",
        "me",
        "might",
        "more",
        "most",
        "much",
        "must",
        "my",
        "myself",
        "need",
        "needs",
        "neither",
        "never",
        "new",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "often",
        "on",
        "once",
        "one",
        "only",
        "onto",
        "or",
        "other",
        "others",
        "ought",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "per",
        "perhaps",
        "please",
        "put",
        "quite",
        "rather",
        "really",
        "same",
        "say",
        "says",
        "see",
        "seem",
        "seemed",
        "seems",
        "seen",
        "several",
        "shall",
        "she",
        "should",
        "since",
        "so",
        "some",
        "somebody",
        "someone",
        "something",
        "sometimes",
        "somewhere",
        "still",
        "such",
        "take",
        "taken",
        "taking",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "therefore",
        "these",
        "they",
        "thing",
        "things",
        "think",
        "this",
        "those",
        "though",
        "through",
        "thus",
        "to",
        "together",
        "too",
        "toward",
        "towards",
        "under",
        "unless",
        "until",
        "up",
        "upon",
        "us",
        "use",
        "used",
        "uses",
        "using",
        "very",
        "via",
        "want",
        "wants",
        "was",
        "way",
        "we",
        "well",
        "were",
        "what",
        "whatever",
        "when",
        "whenever",
        "where",
        "wherever",
        "whether",
        "which",
        "while",
        "who",
        "whoever",
        "whole",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "within",
        "without",
        "would",
        "yes",
        "yet",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        "pr",
        "prs",
        "cl",
        "cls",
        "issue",
        "issues",
        "bug",
        "bugs",
        "commit",
        "commits",
        "branch",
        "branches",
        "file",
        "files",
        "line",
        "lines",
        "code",
        "change",
        "changes",
        "update",
        "updates",
        "updated",
        "add",
        "added",
        "adding",
        "remove",
        "removed",
        "removing",
        "fix",
        "fixed",
        "fixes",
        "fixing",
        "feat",
        "chore",
        "refactor",
        "merge",
        "merged",
        "pull",
        "request",
        "review",
        "reviewed",
        "lgtm",
        "nit",
        "todo",
        "wip",
        "work",
        "working",
        "task",
        "tasks",
    }
)

CONCEPT_CLUSTERS: tuple[tuple[str, ...], ...] = (
    (
        "auth",
        "oauth",
        "token",
        "jwt",
        "credential",
        "session",
        "login",
        "sso",
        "passwd",
        "secret",
        "key",
        "bearer",
        "oid",
        "saml",
        "authn",
        "authz",
        "permission",
        "role",
        "acl",
        "rbac",
    ),
    (
        "db",
        "database",
        "sql",
        "sqlite",
        "postgres",
        "mysql",
        "spanner",
        "query",
        "schema",
        "migration",
        "table",
        "index",
        "column",
        "row",
        "transaction",
        "orm",
        "prisma",
        "drizzle",
    ),
    (
        "cache",
        "redis",
        "memcached",
        "lru",
        "ttl",
        "invalidate",
        "invalidation",
        "stale",
        "evict",
        "memoize",
        "buffer",
    ),
    (
        "ui",
        "frontend",
        "react",
        "component",
        "css",
        "style",
        "layout",
        "theme",
        "modal",
        "dialog",
        "button",
        "input",
        "form",
        "render",
        "dom",
        "html",
        "tailwind",
        "view",
        "pane",
        "panel",
        "card",
        "badge",
        "icon",
        "widget",
        "surface",
    ),
    (
        "api",
        "rest",
        "graphql",
        "grpc",
        "proto",
        "protobuf",
        "endpoint",
        "route",
        "handler",
        "controller",
        "request",
        "response",
        "payload",
        "json",
        "http",
        "https",
        "webhook",
        "fetch",
        "client",
        "server",
        "rpc",
        "mcp",
    ),
    (
        "test",
        "testing",
        "unittest",
        "pytest",
        "vitest",
        "jest",
        "mocha",
        "mock",
        "stub",
        "fixture",
        "assert",
        "expect",
        "coverage",
        "e2e",
        "integration",
        "spec",
        "benchmark",
    ),
    (
        "ci",
        "cd",
        "pipeline",
        "workflow",
        "github",
        "action",
        "actions",
        "build",
        "compile",
        "bundle",
        "webpack",
        "vite",
        "rollup",
        "esbuild",
        "tsc",
        "lint",
        "eslint",
        "prettier",
        "format",
        "presubmit",
        "tap",
    ),
    (
        "deploy",
        "release",
        "publish",
        "version",
        "semver",
        "changelog",
        "docker",
        "container",
        "k8s",
        "kubernetes",
        "helm",
        "cloud",
        "aws",
        "gcp",
        "azure",
        "prod",
        "production",
        "staging",
        "rollout",
        "canary",
    ),
    (
        "async",
        "concurrent",
        "concurrency",
        "parallel",
        "thread",
        "worker",
        "queue",
        "job",
        "cron",
        "schedule",
        "timer",
        "interval",
        "poll",
        "polling",
        "webhook",
        "stream",
        "event",
        "pubsub",
        "kafka",
        "promise",
        "future",
        "isolate",
    ),
    (
        "perf",
        "performance",
        "latency",
        "throughput",
        "memory",
        "cpu",
        "leak",
        "profile",
        "optimize",
        "optimization",
        "fast",
        "slow",
        "bottleneck",
        "batch",
        "chunk",
        "pagination",
        "limit",
        "quota",
        "rate",
        "throttle",
    ),
    (
        "error",
        "exception",
        "crash",
        "panic",
        "fail",
        "failure",
        "retry",
        "backoff",
        "timeout",
        "fallback",
        "recover",
        "recovery",
        "graceful",
        "handle",
        "handling",
        "catch",
        "throw",
        "reject",
        "abort",
    ),
    (
        "log",
        "logging",
        "logger",
        "metric",
        "metrics",
        "trace",
        "tracing",
        "telemetry",
        "opentelemetry",
        "monitor",
        "monitoring",
        "alert",
        "health",
        "status",
        "audit",
        "debug",
        "diagnostic",
        "inspect",
    ),
    (
        "config",
        "configuration",
        "setting",
        "settings",
        "env",
        "environment",
        "flag",
        "flags",
        "option",
        "options",
        "param",
        "parameter",
        "arg",
        "argument",
        "cli",
        "yaml",
        "toml",
        "ini",
        "dotenv",
    ),
    (
        "security",
        "vuln",
        "vulnerability",
        "xss",
        "csrf",
        "injection",
        "sanitize",
        "escape",
        "encrypt",
        "encryption",
        "decrypt",
        "hash",
        "sha",
        "tls",
        "ssl",
        "cert",
        "certificate",
        "cors",
        "csp",
        "sandbox",
        "untrusted",
    ),
    (
        "git",
        "repo",
        "repository",
        "worktree",
        "rebase",
        "cherry",
        "pick",
        "stash",
        "diff",
        "patch",
        "conflict",
        "upstream",
        "origin",
        "remote",
        "clone",
        "fetch",
        "push",
        "tag",
        "head",
        "sha",
    ),
    (
        "agent",
        "llm",
        "model",
        "prompt",
        "token",
        "context",
        "embedding",
        "vector",
        "cosine",
        "similarity",
        "cluster",
        "clustering",
        "semantic",
        "search",
        "rag",
        "inference",
        "gemini",
        "claude",
        "openai",
        "anthropic",
        "jetski",
    ),
    (
        "type",
        "types",
        "typing",
        "interface",
        "struct",
        "class",
        "enum",
        "union",
        "generic",
        "schema",
        "zod",
        "validation",
        "validate",
        "parse",
        "parser",
        "serialize",
        "deserialize",
        "cast",
        "convert",
    ),
    (
        "file",
        "filesystem",
        "fs",
        "path",
        "dir",
        "directory",
        "folder",
        "read",
        "write",
        "stream",
        "blob",
        "upload",
        "download",
        "export",
        "import",
        "archive",
        "zip",
        "tar",
        "disk",
        "storage",
    ),
    (
        "state",
        "store",
        "redux",
        "zustand",
        "signal",
        "signals",
        "reactive",
        "observable",
        "subscribe",
        "dispatch",
        "action",
        "reducer",
        "context",
        "hook",
        "effect",
        "lifecycle",
        "mount",
        "unmount",
    ),
    (
        "doc",
        "docs",
        "documentation",
        "readme",
        "guide",
        "tutorial",
        "example",
        "sample",
        "comment",
        "docstring",
        "jsdoc",
        "markdown",
        "md",
        "diagram",
        "mermaid",
        "spec",
        "rfc",
        "adr",
        "proposal",
    ),
    (
        "plugin",
        "extension",
        "addon",
        "module",
        "package",
        "library",
        "sdk",
        "framework",
        "dependency",
        "dependencies",
        "npm",
        "pnpm",
        "yarn",
        "pip",
        "cargo",
        "go",
        "pub",
        "gem",
        "install",
        "upgrade",
    ),
    (
        "user",
        "account",
        "profile",
        "member",
        "team",
        "org",
        "organization",
        "admin",
        "owner",
        "author",
        "reviewer",
        "assignee",
        "collaborator",
        "group",
        "tenant",
        "customer",
        "client",
    ),
    (
        "notify",
        "notification",
        "email",
        "mail",
        "slack",
        "chat",
        "message",
        "messaging",
        "toast",
        "banner",
        "badge",
        "ping",
        "mention",
        "inbox",
        "unread",
        "read",
        "snooze",
        "dismiss",
        "mute",
    ),
    (
        "nav",
        "navigation",
        "router",
        "routing",
        "link",
        "url",
        "uri",
        "redirect",
        "history",
        "tab",
        "tabs",
        "menu",
        "sidebar",
        "header",
        "footer",
        "breadcrumb",
        "page",
        "screen",
    ),
    (
        "table",
        "grid",
        "list",
        "tree",
        "chart",
        "graph",
        "plot",
        "sort",
        "filter",
        "search",
        "find",
        "group",
        "aggregate",
        "count",
        "sum",
        "avg",
        "rank",
        "score",
        "weight",
    ),
    (
        "mobile",
        "android",
        "ios",
        "flutter",
        "dart",
        "swift",
        "kotlin",
        "native",
        "web",
        "browser",
        "chrome",
        "safari",
        "firefox",
        "electron",
        "desktop",
        "linux",
        "macos",
        "windows",
    ),
    (
        "math",
        "calc",
        "calculate",
        "compute",
        "formula",
        "matrix",
        "vector",
        "float",
        "int",
        "number",
        "decimal",
        "round",
        "floor",
        "ceil",
        "random",
        "prob",
        "probability",
        "stat",
        "statistics",
    ),
    (
        "time",
        "date",
        "datetime",
        "timestamp",
        "timezone",
        "utc",
        "iso",
        "duration",
        "elapsed",
        "age",
        "stale",
        "expire",
        "expiration",
        "deadline",
        "calendar",
        "day",
        "week",
        "month",
        "year",
    ),
    (
        "net",
        "network",
        "socket",
        "tcp",
        "udp",
        "ip",
        "dns",
        "proxy",
        "tunnel",
        "ssh",
        "port",
        "host",
        "hostname",
        "localhost",
        "connect",
        "connection",
        "disconnect",
        "online",
        "offline",
    ),
    (
        "lock",
        "mutex",
        "semaphore",
        "atomic",
        "sync",
        "synchronize",
        "race",
        "deadlock",
        "starvation",
        "block",
        "blocking",
        "nonblocking",
        "wait",
        "signal",
        "barrier",
        "latch",
    ),
)

CANONICAL_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bpull\s+requests?\b", re.IGNORECASE), "pull_request"),
    (re.compile(r"\bmerge\s+conflicts?\b", re.IGNORECASE), "merge_conflict"),
    (re.compile(r"\brate\s+limits?(?:ing)?\b", re.IGNORECASE), "rate_limit"),
    (re.compile(r"\bfeature\s+flags?\b", re.IGNORECASE), "feature_flag"),
    (re.compile(r"\btype\s+check(?:s|ing)?\b", re.IGNORECASE), "typecheck"),
    (re.compile(r"\bunit\s+tests?\b", re.IGNORECASE), "unittest"),
    (re.compile(r"\bintegration\s+tests?\b", re.IGNORECASE), "integration_test"),
    (re.compile(r"\bend\s+to\s+end\b", re.IGNORECASE), "e2e"),
    (re.compile(r"\bcommand\s+line\b", re.IGNORECASE), "cli"),
    (re.compile(r"\buser\s+interface\b", re.IGNORECASE), "ui"),
    (re.compile(r"\bdata\s+base\b", re.IGNORECASE), "database"),
    (re.compile(r"\bsingle\s+sign\s+on\b", re.IGNORECASE), "sso"),
    (re.compile(r"\baccess\s+control\b", re.IGNORECASE), "acl"),
    (re.compile(r"\bmemory\s+leaks?\b", re.IGNORECASE), "memory_leak"),
    (re.compile(r"\brace\s+conditions?\b", re.IGNORECASE), "race_condition"),
    (re.compile(r"\bstack\s+traces?\b", re.IGNORECASE), "stack_trace"),
    (re.compile(r"\bside\s+pane\b", re.IGNORECASE), "side_pane"),
    (re.compile(r"\bwork\s+trees?\b", re.IGNORECASE), "worktree"),
)


class Embedder(Protocol):
    """Protocol for offline 384-dim chunk and query embedders."""

    model_id: str

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Computes L2-normalized 384-dim vectors for a sequence of document strings."""
        ...

    def embed_chunks(self, chunks: Sequence[TextChunk]) -> list[list[float]]:
        """Computes L2-normalized 384-dim vectors for a sequence of structured chunks."""
        ...

    def embed_query(self, query: str) -> list[float]:
        """Computes an L2-normalized 384-dim vector for a search query string."""
        ...


_ONNX_DEPS_CACHE: bool | None = None
_ONNX_DEPS_PROBED: bool = False
_ONNX_DEPS_AVAILABLE: bool = False


def reset_onnx_deps_cache() -> None:
    """Resets the memoized `ensure_onnx_deps()` probe result for testing."""
    global _ONNX_DEPS_CACHE, _ONNX_DEPS_PROBED, _ONNX_DEPS_AVAILABLE
    _ONNX_DEPS_CACHE = None
    _ONNX_DEPS_PROBED = False
    _ONNX_DEPS_AVAILABLE = False


def _set_onnx_deps_cache(available: bool) -> bool:
    """Records and returns the memoized `ensure_onnx_deps()` result."""
    global _ONNX_DEPS_CACHE, _ONNX_DEPS_PROBED, _ONNX_DEPS_AVAILABLE
    _ONNX_DEPS_CACHE = available
    _ONNX_DEPS_PROBED = True
    _ONNX_DEPS_AVAILABLE = available
    return available


def _can_import_onnx_deps() -> bool:
    """Returns True when numpy, onnxruntime, and tokenizers are importable."""
    try:
        return all(
            importlib.util.find_spec(mod) is not None
            for mod in ("numpy", "onnxruntime", "tokenizers")
        )
    except (ImportError, ValueError):
        return False


def _resolve_uv_binary() -> str | None:
    """Locates the `uv` binary in `~/.local/bin/uv` or on `PATH`."""
    uv_bin = Path.home() / ".local" / "bin" / "uv"
    if uv_bin.exists():
        return str(uv_bin)
    return shutil.which("uv")


def _probe_uv_onnx_paths(uv_bin: str) -> list[str]:
    """Probes the offline `uv` cache for site-packages containing ONNX runtime dependencies."""
    probe_code = (
        "import json, sys, numpy, onnxruntime, tokenizers; "
        "print(json.dumps([p for p in sys.path if 'uv/archive-v0' in p or 'site-packages' in p]))"
    )
    try:
        proc = subprocess.run(
            [
                uv_bin,
                "run",
                "--offline",
                "--python",
                sys.executable,
                "--with",
                "onnxruntime",
                "--with",
                "tokenizers",
                "--with",
                "numpy",
                "python3",
                "-c",
                probe_code,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        parsed = json.loads(proc.stdout.strip().splitlines()[-1])
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(p) for p in parsed if isinstance(p, str)]


def ensure_onnx_deps() -> bool:
    """Ensures onnxruntime, tokenizers, and numpy are importable via uv cache if needed."""
    if _ONNX_DEPS_CACHE is not None:
        return _ONNX_DEPS_CACHE
    if _ONNX_DEPS_PROBED:
        return _ONNX_DEPS_AVAILABLE
    if _can_import_onnx_deps():
        return _set_onnx_deps_cache(True)

    uv_bin = _resolve_uv_binary()
    if not uv_bin:
        return _set_onnx_deps_cache(False)

    paths = _probe_uv_onnx_paths(uv_bin)
    if not paths:
        return _set_onnx_deps_cache(False)

    for p in reversed(paths):
        if p not in sys.path:
            sys.path.insert(0, p)
    return _set_onnx_deps_cache(_can_import_onnx_deps())


_ensure_onnx_deps = ensure_onnx_deps


def _fnv1a(text: str) -> int:
    """Computes unsigned 32-bit FNV-1a hash of a string."""
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _hash_sign(text: str) -> int:
    """Computes deterministic +1 or -1 sign from FNV-1a with salt."""
    h = 2166136261 ^ 0x9E3779B9
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return 1 if (h & 1) == 0 else -1


def _collapse_double_trailing(base: str) -> str:
    """Collapses a trailing doubled character on stems of length >= 3."""
    if len(base) >= 3 and base[-1] == base[-2]:
        return base[:-1]
    return base


_SUFFIX_RULES: tuple[tuple[str, int, str, bool], ...] = (
    ("ies", 4, "y", False),
    ("ing", 5, "", True),
    ("tion", 5, "e", False),
    ("ed", 4, "", True),
    ("es", 4, "", False),
)


@functools.lru_cache(maxsize=16384)
def _stem_token(word: str) -> str:
    """Applies lightweight morphological suffix stripping matching inflight's embedder."""
    if len(word) <= 3:
        return word
    for suffix, min_len, replacement, collapse in _SUFFIX_RULES:
        if len(word) > min_len and word.endswith(suffix):
            base = word[: -len(suffix)] + replacement
            return _collapse_double_trailing(base) if collapse else base
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


_CLUSTER_STEM_PAIRS: tuple[tuple[tuple[str, str], ...], ...] = tuple(
    tuple((kw, _stem_token(kw)) for kw in cluster) for cluster in CONCEPT_CLUSTERS
)

_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_URL_RE = re.compile(r"https?://\S+")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_PATH_PUNCT_RE = re.compile(r"[/\\.\-:]+")
_NON_IDENT_RE = re.compile(r"[^a-zA-Z0-9_\s]")
_CODE_TOKEN_RE = re.compile(r"[_0-9]")


def _strip_markdown(text: str) -> str:
    """Strips Markdown formatting while retaining code identifiers."""
    cleaned = text
    for pattern, replacement in CANONICAL_PHRASES:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = _FENCED_CODE_RE.sub(" ", cleaned)
    cleaned = _INLINE_CODE_RE.sub(r" \1 ", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", cleaned)
    cleaned = _PATH_PUNCT_RE.sub(" ", cleaned)
    cleaned = _NON_IDENT_RE.sub(" ", cleaned)
    return cleaned.lower()


def _tokenize(text: str) -> list[str]:
    """Tokenizes and stems cleaned text, dropping stop words and pure numbers."""
    raw = _strip_markdown(text).split()
    tokens: list[str] = []
    for tok in raw:
        if len(tok) < 2 or tok in STOP_WORDS or tok.isdigit():
            continue
        stemmed = _stem_token(tok)
        if len(stemmed) >= 2 and stemmed not in STOP_WORDS:
            tokens.append(stemmed)
    return tokens


def _apply_concept_clusters(token_set: set[str], vec: list[float]) -> None:
    """Populates the first 30 semantic concept dimensions."""
    for c_idx, pairs in enumerate(_CLUSTER_STEM_PAIRS):
        matches = sum(1 for kw, stem in pairs if kw in token_set or stem in token_set)
        if matches > 0:
            vec[c_idx] += 2.8 + min(matches - 1, 3) * 0.6


@functools.lru_cache(maxsize=16384)
def _unigram_delta(tok: str) -> tuple[int, float]:
    """Computes the hashed feature index and signed weight delta for a unigram token."""
    weight = 2.2 if _CODE_TOKEN_RE.search(tok) else 1.6
    key = f"u:{tok}"
    idx = CONCEPT_DIM + (_fnv1a(key) % HASH_DIM)
    return idx, _hash_sign(key) * weight


@functools.lru_cache(maxsize=16384)
def _bigram_delta(tok: str, next_tok: str) -> tuple[int, float]:
    """Computes the hashed feature index and signed weight delta for an adjacent token pair."""
    bigram = f"b:{tok}_{next_tok}"
    b_idx = CONCEPT_DIM + (_fnv1a(bigram) % HASH_DIM)
    return b_idx, _hash_sign(bigram) * 1.2


@functools.lru_cache(maxsize=16384)
def _char_ngram_deltas(tok: str) -> tuple[tuple[int, float], ...]:
    """Computes ordered `(index, signed_weight)` deltas for a token's 3-grams and 4-grams."""
    deltas: list[tuple[int, float]] = []
    for j in range(len(tok) - 2):
        trigram = f"c3:{tok[j : j + 3]}"
        c_idx = CONCEPT_DIM + (_fnv1a(trigram) % HASH_DIM)
        deltas.append((c_idx, _hash_sign(trigram) * 0.25))
    for j in range(len(tok) - 3):
        quadgram = f"c4:{tok[j : j + 4]}"
        c4_idx = CONCEPT_DIM + (_fnv1a(quadgram) % HASH_DIM)
        deltas.append((c4_idx, _hash_sign(quadgram) * 0.25))
    return tuple(deltas)


def _apply_hashed_features(tokens: list[str], vec: list[float]) -> None:
    """Populates hashed unigram, bigram, and char n-gram features into indices 30..383."""
    for i, tok in enumerate(tokens):
        idx, delta = _unigram_delta(tok)
        vec[idx] += delta
        if i + 1 < len(tokens):
            b_idx, b_delta = _bigram_delta(tok, tokens[i + 1])
            vec[b_idx] += b_delta
        if len(tok) >= 4:
            for c_idx, c_delta in _char_ngram_deltas(tok):
                vec[c_idx] += c_delta


def embed_text_deterministic(text: str) -> list[float]:
    """Computes a 384-dim L2-normalized vector using concept clusters and feature hashing."""
    vec = [0.0] * EMBEDDING_DIM
    tokens = _tokenize(text)
    if not tokens:
        return vec
    _apply_concept_clusters(set(tokens), vec)
    _apply_hashed_features(tokens, vec)
    return list(l2_normalize(vec))


def embed_structured_deterministic(
    title: str, body: str, extra: str = ""
) -> list[float]:
    """Blends title + extra (weight 1.0) with body (weight 0.65) and L2-normalizes."""
    head_text = f"{title} {title} {extra}".strip()
    head_vec = embed_text_deterministic(head_text)
    if not body.strip():
        return head_vec
    body_vec = embed_text_deterministic(body[:2500])
    combined = [head_vec[i] + 0.65 * body_vec[i] for i in range(EMBEDDING_DIM)]
    return list(l2_normalize(combined))


class DeterministicHashEmbedder:
    """Zero-network 384-dim semantic cluster and feature-hashing embedder."""

    model_id: str = DETERMINISTIC_MODEL_ID

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Computes 384-dim vectors for a sequence of document strings."""
        return [embed_text_deterministic(text) for text in texts]

    def embed_chunks(self, chunks: Sequence[TextChunk]) -> list[list[float]]:
        """Computes 384-dim vectors for structured chunks, blending title, metadata, and body."""
        return [
            embed_structured_deterministic(
                ch.title,
                ch.text,
                extra=f"{ch.source_kind} {ch.file_path or ''}".strip(),
            )
            for ch in chunks
        ]

    def embed_query(self, query: str) -> list[float]:
        """Computes a 384-dim vector for a search query."""
        return embed_text_deterministic(query)


def _find_onnx_model_files(model_dir: Path) -> tuple[Path, Path] | None:
    """Returns (onnx_path, tokenizer_path) if present under model_dir."""
    tokenizer_path = model_dir / "tokenizer.json"
    if not tokenizer_path.exists():
        return None
    candidates = [
        model_dir / "onnx" / "model_quantized.onnx",
        model_dir / "onnx" / "model.onnx",
        model_dir / "model_quantized.onnx",
        model_dir / "model.onnx",
    ]
    for cand in candidates:
        if cand.exists():
            return cand, tokenizer_path
    return None


def _download_url_to_path(url: str, dest: Path, timeout: float = 30.0) -> None:
    """Downloads `url` to `dest` with an explicit socket timeout."""
    import urllib.request

    with (
        urllib.request.urlopen(url, timeout=timeout) as resp,
        dest.open("wb") as out_f,
    ):
        shutil.copyfileobj(resp, out_f)


def ensure_onnx_model(
    model_dir: Path = DEFAULT_MODEL_DIR,
    allow_download: bool = False,
    *,
    reporter: ProgressReporter = NULL_REPORTER,
) -> bool:
    """Checks if Snowflake/snowflake-arctic-embed-xs ONNX files exist, optionally downloading them."""
    if _find_onnx_model_files(model_dir) is not None:
        return True
    if not allow_download or not ensure_onnx_deps():
        return False

    onnx_dir = model_dir / "onnx"
    onnx_dir.mkdir(parents=True, exist_ok=True)
    base_url = "https://huggingface.co/Snowflake/snowflake-arctic-embed-xs/resolve/main"
    files = {
        model_dir / "tokenizer.json": f"{base_url}/tokenizer.json",
        onnx_dir / "model_quantized.onnx": f"{base_url}/onnx/model_quantized.onnx",
    }
    reporter.start_phase("packrat", f"download {ARCTIC_MODEL_ID}", total=len(files))
    pid = os.getpid()
    tmp_files: list[Path] = []
    try:
        for idx, (dest, url) in enumerate(files.items(), start=1):
            if not dest.exists():
                tmp_dest = dest.with_name(f"{dest.name}.tmp.{pid}")
                tmp_files.append(tmp_dest)
                _download_url_to_path(url, tmp_dest)
                os.replace(tmp_dest, dest)
            reporter.advance(idx, len(files), detail=dest.name)
        reporter.finish_phase()
        return _find_onnx_model_files(model_dir) is not None
    except (OSError, ValueError) as exc:
        for tmp_dest in tmp_files:
            tmp_dest.unlink(missing_ok=True)
        reporter.finish_phase()
        reporter.warn(
            "packrat",
            f"ONNX download failed ({exc}); falling back to {DETERMINISTIC_MODEL_ID}",
        )
        return False


class OnnxArcticEmbedder:
    """Offline local CPU ONNX embedder for Snowflake/snowflake-arctic-embed-xs (384-dim)."""

    model_id: str = ARCTIC_MODEL_ID

    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR) -> None:
        if not ensure_onnx_deps():
            raise RuntimeError("onnxruntime, tokenizers, and numpy are not available.")
        found = _find_onnx_model_files(model_dir)
        if found is None:
            raise FileNotFoundError(f"ONNX model files not found in {model_dir}")
        onnx_path, tokenizer_path = found

        import onnxruntime as ort
        from tokenizers import Tokenizer

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = 4
        self._session = ort.InferenceSession(
            str(onnx_path),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {inp.name for inp in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=512)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Computes 384-dim CLS-pooled L2-normalized embeddings via ONNX Runtime."""
        if not texts:
            return []
        import numpy as np

        encodings = self._tokenizer.encode_batch(list(texts))
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feeds: dict[str, object] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids, dtype=np.int64)

        outputs = self._session.run(None, feeds)
        hidden = outputs[0]
        cls_vecs = hidden[:, 0, :]
        norms = np.linalg.norm(cls_vecs, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        normalized = (cls_vecs / norms).astype(np.float32, copy=False)
        return normalized.tolist()

    def embed_chunks(self, chunks: Sequence[TextChunk]) -> list[list[float]]:
        """Computes 384-dim embeddings for structured chunks."""
        return self.embed_texts([_format_chunk_for_embedding(ch) for ch in chunks])

    def embed_query(self, query: str) -> list[float]:
        """Computes a 384-dim query embedding with retrieval instruction prefix."""
        vecs = self.embed_texts([f"{RETRIEVAL_QUERY_PREFIX}{query}"])
        return vecs[0] if vecs else [0.0] * EMBEDDING_DIM


def get_default_embedder_model_id(
    model_dir: Path = DEFAULT_MODEL_DIR,
    prefer_onnx: bool = True,
    allow_download: bool = False,
    *,
    reporter: ProgressReporter = NULL_REPORTER,
) -> str:
    """Resolves the active embedder model_id without constructing an ONNX InferenceSession."""
    if prefer_onnx and ensure_onnx_deps():
        if _find_onnx_model_files(model_dir) is None and allow_download:
            ensure_onnx_model(
                model_dir=model_dir, allow_download=True, reporter=reporter
            )
        if _find_onnx_model_files(model_dir) is not None:
            return ARCTIC_MODEL_ID
    return DETERMINISTIC_MODEL_ID


def get_default_embedder(
    model_dir: Path = DEFAULT_MODEL_DIR,
    prefer_onnx: bool = True,
    allow_download: bool = False,
    *,
    reporter: ProgressReporter = NULL_REPORTER,
) -> Embedder:
    """Returns OnnxArcticEmbedder when model files and runtime exist, else DeterministicHashEmbedder."""
    model_id = get_default_embedder_model_id(
        model_dir=model_dir,
        prefer_onnx=prefer_onnx,
        allow_download=allow_download,
        reporter=reporter,
    )
    if model_id == ARCTIC_MODEL_ID:
        try:
            return OnnxArcticEmbedder(model_dir=model_dir)
        except Exception as exc:  # noqa: BLE001
            reporter.warn(
                "packrat",
                f"ONNX runtime init failed ({exc}); using {DETERMINISTIC_MODEL_ID}",
            )
    return DeterministicHashEmbedder()


def _format_chunk_for_embedding(chunk: TextChunk) -> str:
    """Formats a chunk's metadata and text for embedding."""
    extra_parts: list[str] = [chunk.source_kind]
    if chunk.file_path:
        extra_parts.append(chunk.file_path)
    header = f"{chunk.title} ({' '.join(extra_parts)})".strip()
    return f"{header}\n\n{chunk.text}"


EMBEDDING_PAGE_SIZE = 2048


def _fetch_unembedded_page(
    store: PackratStore,
    repo_id: int | None,
    model_id: str,
    *,
    limit: int,
    after_chunk_id: str | None,
) -> tuple[list[TextChunk], bool]:
    """Fetches one keyset page of unembedded chunks, falling back if `store` lacks paging args."""
    try:
        return (
            store.get_unembedded_chunks(
                repo_id,
                model_id,
                limit=limit,
                after_chunk_id=after_chunk_id,
            ),
            True,
        )
    except TypeError:
        if after_chunk_id is not None:
            return [], False
        return store.get_unembedded_chunks(repo_id, model_id), False


def _next_unembedded_page_if_needed(
    store: PackratStore,
    repo_id: int | None,
    model_id: str,
    *,
    current_page_len: int,
    page_size: int,
    supports_paging: bool,
    last_chunk_id: str,
) -> tuple[list[TextChunk], bool]:
    """Fetches the next keyset page when the current page filled `page_size`."""
    if not supports_paging or current_page_len < page_size:
        return [], supports_paging
    return _fetch_unembedded_page(
        store,
        repo_id,
        model_id,
        limit=page_size,
        after_chunk_id=last_chunk_id,
    )


def _embed_chunk_batch(
    embedder: Embedder, batch: Sequence[TextChunk]
) -> list[list[float]]:
    """Embeds a batch of chunks via `embed_chunks` (or `embed_documents` on test doubles)."""
    embed_chunks_fn = getattr(embedder, "embed_chunks", None)
    if callable(embed_chunks_fn):
        return embed_chunks_fn(batch)
    embed_docs_fn = getattr(embedder, "embed_documents", None)
    if callable(embed_docs_fn):
        return embed_docs_fn(batch)
    raise AttributeError("Embedder must provide embed_chunks or embed_documents")


def _build_embedding_records(
    batch: Sequence[TextChunk],
    vectors: Sequence[Sequence[float]],
    model_id: str,
    now_iso: str,
) -> list[StoredEmbeddingRecord]:
    """Builds `StoredEmbeddingRecord` rows for an embedded chunk batch."""
    return [
        StoredEmbeddingRecord(
            chunk_id=ch.chunk_id,
            model_id=model_id,
            content_sha256=ch.content_sha256,
            vector=tuple(vec),
            updated_at=now_iso,
        )
        for ch, vec in zip(batch, vectors)
    ]


def _upsert_embedding_batch(
    store: PackratStore,
    records: Sequence[StoredEmbeddingRecord],
    *,
    allow_seq_bump: bool,
) -> None:
    """Persists a batch of embeddings, passing `allow_seq_bump` when supported by `store`."""
    try:
        store.upsert_embeddings(records, allow_seq_bump=allow_seq_bump)
    except TypeError:
        store.upsert_embeddings(records)


def _resolve_active_embedder_and_first_page(
    store: PackratStore,
    repo_id: int | None,
    embedder: Embedder | None,
    *,
    page_size: int,
    model_dir: Path,
    allow_download: bool,
    reporter: ProgressReporter,
) -> tuple[Embedder | None, list[TextChunk], bool]:
    """Resolves the target model ID, fetches page 1, and constructs the embedder lazily if needed."""
    target_model_id = (
        embedder.model_id
        if embedder is not None
        else get_default_embedder_model_id(
            model_dir=model_dir,
            allow_download=allow_download,
            reporter=reporter,
        )
    )
    first_page, supports_paging = _fetch_unembedded_page(
        store,
        repo_id,
        target_model_id,
        limit=page_size,
        after_chunk_id=None,
    )
    if not first_page:
        return None, [], supports_paging
    active_embedder = embedder or get_default_embedder(
        model_dir=model_dir,
        allow_download=allow_download,
        reporter=reporter,
    )
    if active_embedder.model_id != target_model_id:
        first_page, supports_paging = _fetch_unembedded_page(
            store,
            repo_id,
            active_embedder.model_id,
            limit=page_size,
            after_chunk_id=None,
        )
        if not first_page:
            return None, [], supports_paging
    return active_embedder, first_page, supports_paging


@dataclass(frozen=True)
class EmbeddingSyncResult:
    """Result from `sync_repo_embeddings` including whether `max_chunks` capped the run."""

    embedded_count: int
    capped: bool = False


def _slice_batch_for_cap(
    batch: list[TextChunk],
    total_embedded: int,
    max_chunks: int | None,
) -> tuple[list[TextChunk], bool]:
    """Slices `batch` to `max_chunks` budget and returns `(sliced_batch, hit_cap)`."""
    if max_chunks is None:
        return batch, False
    remaining = max_chunks - total_embedded
    if remaining <= 0:
        return [], True
    if len(batch) > remaining:
        return batch[:remaining], True
    return batch, False


def _probe_more_after_cap(
    store: PackratStore,
    repo_id: int | None,
    model_id: str,
    *,
    batch_end: int,
    page_len: int,
    effective_page_size: int,
    supports_paging: bool,
    last_chunk_id: str,
) -> bool:
    """Returns True if more unembedded chunks remain past the current batch."""
    if batch_end < page_len:
        return True
    probe, _ = _next_unembedded_page_if_needed(
        store,
        repo_id,
        model_id,
        current_page_len=page_len,
        page_size=effective_page_size,
        supports_paging=supports_paging,
        last_chunk_id=last_chunk_id,
    )
    return bool(probe)


def _resolve_total_pending(
    store: PackratStore,
    repo_id: int | None,
    model_id: str,
    *,
    first_page_len: int,
    effective_page_size: int,
    supports_paging: bool,
    max_chunks: int | None,
) -> int | None:
    """Computes the upfront pending chunk denominator for progress reporting."""
    if not supports_paging or first_page_len < effective_page_size:
        total_pending: int | None = first_page_len
    else:
        count_fn = getattr(store, "count_unembedded_chunks", None)
        total_pending = (
            int(count_fn(repo_id, model_id)) if callable(count_fn) else None
        )
    if total_pending is not None and max_chunks is not None:
        total_pending = min(total_pending, max_chunks)
    return total_pending


def _sync_embedding_page(
    store: PackratStore,
    repo_id: int | None,
    active_embedder: Embedder,
    current_page: list[TextChunk],
    *,
    effective_batch_size: int,
    effective_page_size: int,
    supports_paging: bool,
    max_chunks: int | None,
    total_embedded: int,
    total_pending: int | None,
    now: str,
    reporter: ProgressReporter,
) -> tuple[list[TextChunk], bool, int, int | None, bool]:
    """Embeds and persists all slices of one page, returning updated loop state."""
    page_len = len(current_page)
    last_chunk_id = max(ch.chunk_id for ch in current_page)
    current_page.sort(key=lambda ch: len(ch.title) + len(ch.text))
    next_page: list[TextChunk] = []
    capped = False
    for start in range(0, page_len, effective_batch_size):
        raw_batch = current_page[start : start + effective_batch_size]
        batch, capped = _slice_batch_for_cap(raw_batch, total_embedded, max_chunks)
        if not batch:
            return [], supports_paging, total_embedded, total_pending, True
        batch_end = start + len(batch)
        hit_page_end = batch_end >= page_len
        if hit_page_end and not capped:
            next_page, supports_paging = _next_unembedded_page_if_needed(
                store,
                repo_id,
                active_embedder.model_id,
                current_page_len=page_len,
                page_size=effective_page_size,
                supports_paging=supports_paging,
                last_chunk_id=last_chunk_id,
            )
        elif max_chunks is not None and total_embedded + len(batch) >= max_chunks:
            capped = _probe_more_after_cap(
                store,
                repo_id,
                active_embedder.model_id,
                batch_end=batch_end,
                page_len=page_len,
                effective_page_size=effective_page_size,
                supports_paging=supports_paging,
                last_chunk_id=last_chunk_id,
            )
        vectors = _embed_chunk_batch(active_embedder, batch)
        records = _build_embedding_records(batch, vectors, active_embedder.model_id, now)
        is_final_batch = capped or (hit_page_end and not next_page)
        _upsert_embedding_batch(store, records, allow_seq_bump=is_final_batch)
        total_embedded += len(records)
        reporter.advance(total_embedded, total_pending)
        if capped:
            return [], supports_paging, total_embedded, total_pending, True
    return next_page, supports_paging, total_embedded, total_pending, capped


def sync_repo_embeddings(
    store: PackratStore,
    repo_id: int | None,
    embedder: Embedder | None = None,
    batch_size: int = 64,
    *,
    page_size: int = EMBEDDING_PAGE_SIZE,
    max_chunks: int | None = None,
    model_dir: Path = DEFAULT_MODEL_DIR,
    allow_download: bool = False,
    reporter: ProgressReporter = NULL_REPORTER,
    repo_label: str = "packrat",
) -> EmbeddingSyncResult:
    """Incremental embedding sync: embeds only chunks whose content_sha256 has changed."""
    if max_chunks is not None and max_chunks <= 0:
        return EmbeddingSyncResult(embedded_count=0, capped=True)
    effective_batch_size = max(1, int(batch_size))
    effective_page_size = max(1, int(page_size))
    active_embedder, current_page, supports_paging = (
        _resolve_active_embedder_and_first_page(
            store,
            repo_id,
            embedder,
            page_size=effective_page_size,
            model_dir=model_dir,
            allow_download=allow_download,
            reporter=reporter,
        )
    )
    if active_embedder is None or not current_page:
        return EmbeddingSyncResult(embedded_count=0, capped=False)

    now = utc_now_iso()
    total_pending = _resolve_total_pending(
        store,
        repo_id,
        active_embedder.model_id,
        first_page_len=len(current_page),
        effective_page_size=effective_page_size,
        supports_paging=supports_paging,
        max_chunks=max_chunks,
    )
    total_embedded = 0
    capped = False
    reporter.start_phase(
        repo_label, f"embeddings ({active_embedder.model_id})", total=total_pending
    )
    while current_page and not capped:
        current_page, supports_paging, total_embedded, total_pending, capped = (
            _sync_embedding_page(
                store,
                repo_id,
                active_embedder,
                current_page,
                effective_batch_size=effective_batch_size,
                effective_page_size=effective_page_size,
                supports_paging=supports_paging,
                max_chunks=max_chunks,
                total_embedded=total_embedded,
                total_pending=total_pending,
                now=now,
                reporter=reporter,
            )
        )
    reporter.finish_phase()
    return EmbeddingSyncResult(embedded_count=total_embedded, capped=capped)
