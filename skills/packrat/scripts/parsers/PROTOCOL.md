# Packrat Symbol Parser Protocol (v1)

Normative wire contract, routing, kind taxonomy, naming rules, and conformance contract shared by `symbol_parsers.py` and all language workers.

## NDJSON Wire Framing

Framing is UTF-8 newline-delimited JSON (`\n` terminated), flushed per message. Maximum line size is **32 MiB** (`33,554,432` bytes). `stderr` is drained continuously into a 4 KiB ring buffer and never used for protocol messages.

### 1. Startup Banner (`worker -> orchestrator`, within 10 s)

The first line written to `stdout` must be a single JSON object:

```json
{
  "ready": true,
  "parser": "dart-analyzer",
  "protocol": 1,
  "parser_ids": {".dart": "dart-analyzer@1"},
  "library": "analyzer 14.5.0"
}
```

### 2. Parse Request (`orchestrator -> worker`)

```json
{
  "id": 17,
  "path": "lib/src/widget.dart",
  "content": "<utf-8 source>",
  "deadline_ms": 5000
}
```

Workers may process requests concurrently (typically capped at `min(4, cores)`) and write responses in any order.

### 3. Parse Response (`worker -> orchestrator`)

Success response (even when syntax recovery occurred):

```json
{
  "id": 17,
  "symbols": [
    {
      "kind": "method",
      "name": "build",
      "qualified_name": "MyWidget.build",
      "parent": "MyWidget",
      "signature": "Widget build(BuildContext context)",
      "docstring": "Describes the part of the user interface...",
      "line_start": 42,
      "line_end": 58,
      "visibility": "public"
    }
  ],
  "syntax_error_count": 0
}
```

Per-file error response (e.g., deadline exceeded):

```json
{"id": 18, "error": "timeout"}
```

### 4. Shutdown

Closing `stdin` (EOF) signals the worker to finish any accepted in-flight requests and exit `0` within 5 s (else `SIGTERM`, then `SIGKILL` after 2 s).

## Orchestrator Rule Table

| Situation | Orchestrator behavior |
| :--- | :--- |
| First `stdout` line is not a `ready` object, or arrives after 10 s | Parser `Unavailable` with reason `bad-banner` or `spawn-timeout` |
| Banner `protocol` is not `1` | `Unavailable`, reason `protocol-skew` |
| Banner `parser_ids` differs from `stamp.json` | `Unavailable`, reason `stamp-mismatch` |
| Unknown keys in any JSON object | Ignored |
| Symbol with unknown `kind`, `line_end < line_start`, `line_start < 1`, or empty `qualified_name` | Dropped and counted in `dropped_symbol_count` |
| Missing `visibility`, `parent`, `docstring`, or `signature` | Defaults `""`; `signature` defaults to `name` |
| `signature` (> 500 chars) or `docstring` (> 4000 chars) | Truncated by `decode_symbol()` |
| `{"id", "error"}` or per-file `deadline_ms` exceeded | That file falls back to `regex@1`; the batch continues |
| Malformed line, worker exit, line > 32 MiB, or batch timeout `max(30, 0.2 * n)` s | Worker treated as crashed; bisects unanswered batch (up to 3 crashes, 4th marks `Unavailable`) |

## Routing Table

| Extensions | Parser | `parser_id` |
| :--- | :--- | :--- |
| `.dart` | Dart analyzer worker (`dart-analyzer`) | `dart-analyzer@1` |
| `.swift` | SwiftSyntax worker (`swift-syntax`) | `swift-syntax@1` |
| `.py` | Python stdlib `ast`, in-process (`py-ast`) | `py-ast@1` |
| `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs` | tree-sitter worker (`treesitter`) | `treesitter@1` |
| `.kt` | tree-sitter worker (`treesitter`) | `treesitter@1` |
| `.c`, `.cc`, `.cpp`, `.h`, `.hpp` | tree-sitter worker (`treesitter`) | `treesitter@1` |
| `.go`, `.rs`, `.java` | Regex scanner (`regex`) | `regex@1` |
| `.md`, `.mdx` | Markdown section parser (`md-sections`) | `md-sections@1` |

## Canonical `SYMBOL_KINDS`

`class`, `mixin`, `enum`, `enum_value`, `extension`, `extension_type`, `interface`, `protocol`, `struct`, `object`, `namespace`, `typedef`, `function`, `method`, `constructor`, `getter`, `setter`, `field`, `variable`

## Naming, Span, Line, and Docstring Rules

- `qualified_name` segments are joined with `.`; C++ `::` maps to `.`; anonymous C++ namespaces contribute `(anonymous)`.
- Unnamed or primary constructors use the language token: Dart `new` (`Foo.new`), Swift `init`, Kotlin `init`, TypeScript `constructor`, C++ the class name; named Dart constructors use their identifier (`Foo.bar`).
- Unnamed extensions and other anonymous declarations use `<kind>@L<line_start>`.
- `parent` is the immediately enclosing type-like declaration's `qualified_name`, or `""` at top level.
- Extension members declared outside the receiver use `qualified_name = <Receiver>.<name>`; `parent` follows the enclosing declaration rule only (`""` for Kotlin top-level extensions, `extension@L<n>` or extension name for Swift).
- Only module/file-level declarations and members of type-like declarations are emitted. Local functions, closures, and lambdas inside function/method bodies are omitted.
- `line_start` is 1-based, starting at the first token of the declaration including modifiers/decorators/annotations, excluding leading doc comments. `line_end` is 1-based inclusive, `>= line_start`.
- Line splitting uses LF (`\n`) and CRLF (`\r\n`) only (`text.split("\n")` with trailing `\r` stripped), never Python `str.splitlines()` (so `\f` / `\x0c` does not increment line numbers).
- `signature` is single-line declaration header text up to body opener (`{`, `=>`, `;`, or Python `:`), whitespace collapsed, max 500 characters.
- `docstring` is the attached doc comment with markers (`///`, `/** */`, leading `*`, `"""`) stripped and trimmed, max 4000 characters.
- `visibility` is `""` when unknown, else one of `public`, `private`, `protected`, `internal`, `package`.

## Generated and Minified Files

A file is classified as generated when any single line exceeds 4,000 characters OR UTF-8 byte length exceeds `line_count * 500`. For generated files, `decode_symbol` / batch post-processing keeps only top-level declarations (`parent == ""`) and caps output at 200 symbols in line order. All other files cap at 1,000 symbols in line order. Over-cap and filtered symbols increment `dropped_symbol_count`.

## Stamp Schema (`stamp.json`)

```json
{
  "parser": "dart-analyzer",
  "protocol": 1,
  "parser_ids": {".dart": "dart-analyzer@1"},
  "platforms": {
    "linux-x86_64": {
      "source_sha256": "<sha256>",
      "built_at": "2026-11-13T18:00:00Z",
      "built_on": "darwin-arm64"
    }
  }
}
```

## Conformance Fixture Contract

Each worker parser directory provides `fixtures/conformance/` containing per-language:

- `deep_nesting.<ext>`: Deeply nested constructs; tested at `deadline_ms=50`, must yield `{"error": "timeout"}` (orchestrator falls back cleanly to `regex@1` without marking parser `Unavailable`).
- `syntax_error.<ext>`: Intentionally malformed syntax; must parse with `syntax_error_count >= 1` without crashing the worker.
