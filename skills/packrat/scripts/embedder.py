"""Offline local 384-dim vector embedders for packrat.

Supports two offline CPU backends producing 384-dimensional L2-normalized
FLOAT32 vectors:
1. OnnxBgeEmbedder: local BAAI/bge-small-en-v1.5 via onnxruntime + tokenizers.
2. DeterministicHashEmbedder: zero-network 384-dim semantic cluster + feature-hash
   embedder ported from agents/plugins/inflight/src/arcs/embedder.ts.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Sequence
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

DEFAULT_MODEL_DIR = DEFAULT_DATA_DIR / "models" / "bge-small-en-v1.5"
BGE_MODEL_ID = "bge-small-en-v1.5"
DETERMINISTIC_MODEL_ID = "deterministic-384-v2"
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

CONCEPT_DIM = 30
HASH_DIM = EMBEDDING_DIM - CONCEPT_DIM

STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "about", "above", "after", "again", "against", "all", "also", "am", "an", "and",
        "any", "are", "as", "at", "be", "because", "been", "before", "being", "below",
        "between", "both", "but", "by", "can", "could", "did", "do", "does", "doing", "done",
        "down", "during", "each", "either", "else", "ever", "every", "few", "for", "from",
        "further", "get", "gets", "got", "had", "has", "have", "having", "he", "her", "here",
        "hers", "herself", "him", "himself", "his", "how", "however", "i", "if", "in", "into",
        "is", "it", "its", "itself", "just", "keep", "let", "like", "made", "make", "makes",
        "many", "may", "me", "might", "more", "most", "much", "must", "my", "myself", "need",
        "needs", "neither", "never", "new", "no", "nor", "not", "now", "of", "off", "often",
        "on", "once", "one", "only", "onto", "or", "other", "others", "ought", "our", "ours",
        "ourselves", "out", "over", "own", "per", "perhaps", "please", "put", "quite",
        "rather", "really", "same", "say", "says", "see", "seem", "seemed", "seems", "seen",
        "several", "shall", "she", "should", "since", "so", "some", "somebody", "someone",
        "something", "sometimes", "somewhere", "still", "such", "take", "taken", "taking",
        "than", "that", "the", "their", "theirs", "them", "themselves", "then", "there",
        "therefore", "these", "they", "thing", "things", "think", "this", "those", "though",
        "through", "thus", "to", "together", "too", "toward", "towards", "under", "unless",
        "until", "up", "upon", "us", "use", "used", "uses", "using", "very", "via", "want",
        "wants", "was", "way", "we", "well", "were", "what", "whatever", "when", "whenever",
        "where", "wherever", "whether", "which", "while", "who", "whoever", "whole", "whom",
        "whose", "why", "will", "with", "within", "without", "would", "yes", "yet", "you",
        "your", "yours", "yourself", "yourselves",
        "pr", "prs", "cl", "cls", "issue", "issues", "bug", "bugs", "commit", "commits",
        "branch", "branches", "file", "files", "line", "lines", "code", "change", "changes",
        "update", "updates", "updated", "add", "added", "adding", "remove", "removed",
        "removing", "fix", "fixed", "fixes", "fixing", "feat", "chore", "refactor", "merge",
        "merged", "pull", "request", "review", "reviewed", "lgtm", "nit", "todo", "wip",
        "work", "working", "task", "tasks",
    }
)

CONCEPT_CLUSTERS: tuple[tuple[str, ...], ...] = (
    ("auth", "oauth", "token", "jwt", "credential", "session", "login", "sso", "passwd", "secret", "key", "bearer", "oid", "saml", "authn", "authz", "permission", "role", "acl", "rbac"),
    ("db", "database", "sql", "sqlite", "postgres", "mysql", "spanner", "query", "schema", "migration", "table", "index", "column", "row", "transaction", "orm", "prisma", "drizzle"),
    ("cache", "redis", "memcached", "lru", "ttl", "invalidate", "invalidation", "stale", "evict", "memoize", "buffer"),
    ("ui", "frontend", "react", "component", "css", "style", "layout", "theme", "modal", "dialog", "button", "input", "form", "render", "dom", "html", "tailwind", "view", "pane", "panel", "card", "badge", "icon", "widget", "surface"),
    ("api", "rest", "graphql", "grpc", "proto", "protobuf", "endpoint", "route", "handler", "controller", "request", "response", "payload", "json", "http", "https", "webhook", "fetch", "client", "server", "rpc", "mcp"),
    ("test", "testing", "unittest", "pytest", "vitest", "jest", "mocha", "mock", "stub", "fixture", "assert", "expect", "coverage", "e2e", "integration", "spec", "benchmark"),
    ("ci", "cd", "pipeline", "workflow", "github", "action", "actions", "build", "compile", "bundle", "webpack", "vite", "rollup", "esbuild", "tsc", "lint", "eslint", "prettier", "format", "presubmit", "tap"),
    ("deploy", "release", "publish", "version", "semver", "changelog", "docker", "container", "k8s", "kubernetes", "helm", "cloud", "aws", "gcp", "azure", "prod", "production", "staging", "rollout", "canary"),
    ("async", "concurrent", "concurrency", "parallel", "thread", "worker", "queue", "job", "cron", "schedule", "timer", "interval", "poll", "polling", "webhook", "stream", "event", "pubsub", "kafka", "promise", "future", "isolate"),
    ("perf", "performance", "latency", "throughput", "memory", "cpu", "leak", "profile", "optimize", "optimization", "fast", "slow", "bottleneck", "batch", "chunk", "pagination", "limit", "quota", "rate", "throttle"),
    ("error", "exception", "crash", "panic", "fail", "failure", "retry", "backoff", "timeout", "fallback", "recover", "recovery", "graceful", "handle", "handling", "catch", "throw", "reject", "abort"),
    ("log", "logging", "logger", "metric", "metrics", "trace", "tracing", "telemetry", "opentelemetry", "monitor", "monitoring", "alert", "health", "status", "audit", "debug", "diagnostic", "inspect"),
    ("config", "configuration", "setting", "settings", "env", "environment", "flag", "flags", "option", "options", "param", "parameter", "arg", "argument", "cli", "yaml", "toml", "ini", "dotenv"),
    ("security", "vuln", "vulnerability", "xss", "csrf", "injection", "sanitize", "escape", "encrypt", "encryption", "decrypt", "hash", "sha", "tls", "ssl", "cert", "certificate", "cors", "csp", "sandbox", "untrusted"),
    ("git", "repo", "repository", "worktree", "rebase", "cherry", "pick", "stash", "diff", "patch", "conflict", "upstream", "origin", "remote", "clone", "fetch", "push", "tag", "head", "sha"),
    ("agent", "llm", "model", "prompt", "token", "context", "embedding", "vector", "cosine", "similarity", "cluster", "clustering", "semantic", "search", "rag", "inference", "gemini", "claude", "openai", "anthropic", "jetski"),
    ("type", "types", "typing", "interface", "struct", "class", "enum", "union", "generic", "schema", "zod", "validation", "validate", "parse", "parser", "serialize", "deserialize", "cast", "convert"),
    ("file", "filesystem", "fs", "path", "dir", "directory", "folder", "read", "write", "stream", "blob", "upload", "download", "export", "import", "archive", "zip", "tar", "disk", "storage"),
    ("state", "store", "redux", "zustand", "signal", "signals", "reactive", "observable", "subscribe", "dispatch", "action", "reducer", "context", "hook", "effect", "lifecycle", "mount", "unmount"),
    ("doc", "docs", "documentation", "readme", "guide", "tutorial", "example", "sample", "comment", "docstring", "jsdoc", "markdown", "md", "diagram", "mermaid", "spec", "rfc", "adr", "proposal"),
    ("plugin", "extension", "addon", "module", "package", "library", "sdk", "framework", "dependency", "dependencies", "npm", "pnpm", "yarn", "pip", "cargo", "go", "pub", "gem", "install", "upgrade"),
    ("user", "account", "profile", "member", "team", "org", "organization", "admin", "owner", "author", "reviewer", "assignee", "collaborator", "group", "tenant", "customer", "client"),
    ("notify", "notification", "email", "mail", "slack", "chat", "message", "messaging", "toast", "banner", "badge", "ping", "mention", "inbox", "unread", "read", "snooze", "dismiss", "mute"),
    ("nav", "navigation", "router", "routing", "link", "url", "uri", "redirect", "history", "tab", "tabs", "menu", "sidebar", "header", "footer", "breadcrumb", "page", "screen"),
    ("table", "grid", "list", "tree", "chart", "graph", "plot", "sort", "filter", "search", "find", "group", "aggregate", "count", "sum", "avg", "rank", "score", "weight"),
    ("mobile", "android", "ios", "flutter", "dart", "swift", "kotlin", "native", "web", "browser", "chrome", "safari", "firefox", "electron", "desktop", "linux", "macos", "windows"),
    ("math", "calc", "calculate", "compute", "formula", "matrix", "vector", "float", "int", "number", "decimal", "round", "floor", "ceil", "random", "prob", "probability", "stat", "statistics"),
    ("time", "date", "datetime", "timestamp", "timezone", "utc", "iso", "duration", "elapsed", "age", "stale", "expire", "expiration", "deadline", "calendar", "day", "week", "month", "year"),
    ("net", "network", "socket", "tcp", "udp", "ip", "dns", "proxy", "tunnel", "ssh", "port", "host", "hostname", "localhost", "connect", "connection", "disconnect", "online", "offline"),
    ("lock", "mutex", "semaphore", "atomic", "sync", "synchronize", "race", "deadlock", "starvation", "block", "blocking", "nonblocking", "wait", "signal", "barrier", "latch"),
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

    def embed_query(self, query: str) -> list[float]:
        """Computes an L2-normalized 384-dim vector for a search query string."""
        ...


def _ensure_onnx_deps() -> bool:
    """Ensures onnxruntime, tokenizers, and numpy are importable via uv cache if needed."""
    try:
        import numpy
        import onnxruntime
        import tokenizers

        return True
    except ImportError:
        pass

    uv_bin = str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv_bin).exists():
        found = shutil.which("uv")
        if not found:
            return False
        uv_bin = found

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
        return False

    if proc.returncode != 0 or not proc.stdout.strip():
        return False

    try:
        paths = json.loads(proc.stdout.strip().splitlines()[-1])
        for p in reversed(paths):
            if p not in sys.path:
                sys.path.insert(0, p)
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401

        return True
    except (ImportError, OSError, ValueError):
        return False


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


def _strip_markdown(text: str) -> str:
    """Strips Markdown formatting while retaining code identifiers."""
    cleaned = text
    for pattern, replacement in CANONICAL_PHRASES:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"```[\s\S]*?```", " ", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r" \1 ", cleaned)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", cleaned)
    cleaned = re.sub(r"[/\\.\-:]+", " ", cleaned)
    cleaned = re.sub(r"[^a-zA-Z0-9_\s]", " ", cleaned)
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
    for c_idx, cluster in enumerate(CONCEPT_CLUSTERS):
        matches = sum(1 for kw in cluster if kw in token_set or _stem_token(kw) in token_set)
        if matches > 0:
            vec[c_idx] += 2.8 + min(matches - 1, 3) * 0.6


def _apply_hashed_features(tokens: list[str], vec: list[float]) -> None:
    """Populates hashed unigram, bigram, and char n-gram features into indices 30..383."""
    for i, tok in enumerate(tokens):
        weight = 2.2 if re.search(r"[_0-9]", tok) else 1.6
        idx = CONCEPT_DIM + (_fnv1a(f"u:{tok}") % HASH_DIM)
        vec[idx] += _hash_sign(f"u:{tok}") * weight
        if i + 1 < len(tokens):
            bigram = f"b:{tok}_{tokens[i + 1]}"
            b_idx = CONCEPT_DIM + (_fnv1a(bigram) % HASH_DIM)
            vec[b_idx] += _hash_sign(bigram) * 1.2
        if len(tok) >= 4:
            for j in range(len(tok) - 2):
                trigram = f"c3:{tok[j : j + 3]}"
                c_idx = CONCEPT_DIM + (_fnv1a(trigram) % HASH_DIM)
                vec[c_idx] += _hash_sign(trigram) * 0.25
            for j in range(len(tok) - 3):
                quadgram = f"c4:{tok[j : j + 4]}"
                c4_idx = CONCEPT_DIM + (_fnv1a(quadgram) % HASH_DIM)
                vec[c4_idx] += _hash_sign(quadgram) * 0.25


def embed_text_deterministic(text: str) -> list[float]:
    """Computes a 384-dim L2-normalized vector using concept clusters and feature hashing."""
    vec = [0.0] * EMBEDDING_DIM
    tokens = _tokenize(text)
    if not tokens:
        return vec
    _apply_concept_clusters(set(tokens), vec)
    _apply_hashed_features(tokens, vec)
    return list(l2_normalize(vec))


def embed_structured_deterministic(title: str, body: str, extra: str = "") -> list[float]:
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
        results: list[list[float]] = []
        for text in texts:
            if "\n\n" in text:
                title, body = text.split("\n\n", 1)
                results.append(embed_structured_deterministic(title, body))
            else:
                results.append(embed_text_deterministic(text))
        return results

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


def ensure_bge_model(
    model_dir: Path = DEFAULT_MODEL_DIR,
    allow_download: bool = False,
    *,
    reporter: ProgressReporter = NULL_REPORTER,
) -> bool:
    """Checks if BAAI/bge-small-en-v1.5 ONNX files exist, optionally downloading them."""
    if _find_onnx_model_files(model_dir) is not None:
        return True
    if not allow_download:
        return False

    onnx_dir = model_dir / "onnx"
    onnx_dir.mkdir(parents=True, exist_ok=True)
    base_url = "https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main"
    files = {
        model_dir / "tokenizer.json": f"{base_url}/tokenizer.json",
        onnx_dir / "model_quantized.onnx": f"{base_url}/onnx/model_quantized.onnx",
    }
    reporter.start_phase("packrat", "download BGE model", total=len(files))
    try:
        for idx, (dest, url) in enumerate(files.items(), start=1):
            if not dest.exists():
                urllib.request.urlretrieve(url, dest)
            reporter.advance(idx, len(files), detail=dest.name)
        reporter.finish_phase()
        return _find_onnx_model_files(model_dir) is not None
    except (OSError, ValueError) as exc:
        reporter.finish_phase()
        reporter.warn("packrat", f"BGE ONNX download failed ({exc}); falling back to {DETERMINISTIC_MODEL_ID}")
        return False


class OnnxBgeEmbedder:
    """Offline local CPU ONNX embedder for BAAI/bge-small-en-v1.5 (384-dim)."""

    model_id: str = BGE_MODEL_ID

    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR) -> None:
        if not _ensure_onnx_deps():
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
        normalized = cls_vecs / norms
        return [row.astype(float).tolist() for row in normalized]

    def embed_query(self, query: str) -> list[float]:
        """Computes a 384-dim BGE query embedding with retrieval instruction prefix."""
        vecs = self.embed_texts([f"{BGE_QUERY_PREFIX}{query}"])
        return vecs[0] if vecs else [0.0] * EMBEDDING_DIM


def get_default_embedder(
    model_dir: Path = DEFAULT_MODEL_DIR,
    prefer_onnx: bool = True,
    *,
    reporter: ProgressReporter = NULL_REPORTER,
) -> Embedder:
    """Returns OnnxBgeEmbedder when model files and runtime exist, else DeterministicHashEmbedder."""
    if prefer_onnx and _find_onnx_model_files(model_dir) is not None and _ensure_onnx_deps():
        try:
            return OnnxBgeEmbedder(model_dir=model_dir)
        except Exception as exc:  # noqa: BLE001
            reporter.warn("packrat", f"ONNX runtime init failed ({exc}); using {DETERMINISTIC_MODEL_ID}")
            return DeterministicHashEmbedder()
    return DeterministicHashEmbedder()


def _format_chunk_for_embedding(chunk: TextChunk) -> str:
    """Formats a chunk's metadata and text for embedding."""
    extra_parts: list[str] = [chunk.source_kind]
    if chunk.file_path:
        extra_parts.append(chunk.file_path)
    header = f"{chunk.title} ({' '.join(extra_parts)})".strip()
    return f"{header}\n\n{chunk.text}"


def sync_repo_embeddings(
    store: PackratStore,
    repo_id: int | None,
    embedder: Embedder | None = None,
    batch_size: int = 64,
    *,
    reporter: ProgressReporter = NULL_REPORTER,
    repo_label: str = "packrat",
) -> int:
    """Incremental embedding sync: embeds only chunks whose content_sha256 has changed."""
    active_embedder = embedder or get_default_embedder(reporter=reporter)
    pending = store.get_unembedded_chunks(repo_id, active_embedder.model_id)
    if not pending:
        return 0

    now = utc_now_iso()
    total_pending = len(pending)
    total_embedded = 0
    reporter.start_phase(repo_label, f"embeddings ({active_embedder.model_id})", total=total_pending)
    for start in range(0, total_pending, batch_size):
        batch = pending[start : start + batch_size]
        texts = [_format_chunk_for_embedding(ch) for ch in batch]
        vectors = active_embedder.embed_texts(texts)
        records = [
            StoredEmbeddingRecord(
                chunk_id=ch.chunk_id,
                model_id=active_embedder.model_id,
                content_sha256=ch.content_sha256,
                vector=tuple(vec),
                updated_at=now,
            )
            for ch, vec in zip(batch, vectors)
        ]
        store.upsert_embeddings(records)
        total_embedded += len(records)
        reporter.advance(total_embedded, total_pending)
    reporter.finish_phase()
    return total_embedded
