# Tree-sitter Symbol Parser (`treesitter`)

`symbol_parser.py` runs as a long-lived NDJSON worker covering C, C++, Kotlin, TypeScript, TSX, and JavaScript under `parser_id = "treesitter@1"`. It follows the wire protocol and symbol rules in [../PROTOCOL.md](../PROTOCOL.md).

## Pinned Dependencies and Warmup

`symbol_parser.py` uses PEP 723 inline script metadata managed by `uv` so `packrat.service` needs no global virtualenv or Node runtime:

- `tree-sitter>=0.25,<0.26`
- `tree-sitter-cpp==0.23.4`
- `tree-sitter-kotlin==1.1.0`
- `tree-sitter-typescript==0.23.2`
- `tree-sitter-javascript==0.25.0`

During `packrat sync`, [../../symbol_parsers.py](../../symbol_parsers.py) starts the worker with `uv run --offline --python <sys.executable> --script parsers/treesitter/symbol_parser.py --worker`. Warm the local `uv` wheel cache once before offline runs:

```bash
uv run --script parsers/treesitter/symbol_parser.py --selftest
```

## Architecture and Deadline Handling

`run_worker` prints the startup banner matching [stamp.json](stamp.json), reads NDJSON parse requests from `stdin`, and dispatches files across a `ProcessPoolExecutor` capped at `min(4, os.cpu_count())`. Each worker process caches one `tree_sitter.Parser` instance per grammar (`cpp`, `kotlin`, `typescript`, `tsx`, `javascript`).

Per-file `deadline_ms` budgets are enforced in `_parse_bytes_with_deadline` using a chunked `read_callback` that checks `time.monotonic()` every 256 bytes. `Parser.timeout_micros` also stays active while present on `0.25.x`.

In `py-tree-sitter` 0.25.0 through 0.25.2, passing `progress_callback` with raw `bytes` is ignored with a `UserWarning`. Passing `progress_callback` with `read_callback` triggers a C segmentation fault on Python 3.13. Aborting inside `read_callback` avoids that C callback path while honoring 50 ms conformance deadlines cleanly.

## Visitor Modules

- [common.py](common.py) provides LF-only line indexing, `/**` / `/*!` / `///` docstring stripping, header signatures, syntax error counts, and generated-file caps.
- [typescript.py](typescript.py) and [javascript.py](javascript.py) extract classes, interfaces, enums, namespaces, type aliases, functions, overloads, constructors, accessors, fields (`#name` is `private`), and variables.
- [kotlin.py](kotlin.py) extracts classes, interfaces, enums, objects, companion objects, type aliases, `init` constructors, property accessors, and receiver extensions (`<Receiver>.<name>`).
- [cpp.py](cpp.py) extracts namespaces (`::` mapped to `.` and `(anonymous)`), templates, classes, structs, enums, aliases, constructors, methods, access-tracked fields, and globals.

## Generated Bundle Measurement

[fixtures/minified_bundle.js](fixtures/minified_bundle.js) models a single-line minified JavaScript bundle over the 4,000-character generated-file threshold. It contains 240 top-level declarations plus 30 nested class methods, yielding 270 raw symbols and 0 local IIFE helpers. `finalize_symbols` drops all 30 nested members (`parent != ""`) and caps top-level output at 200 symbols (`dropped_symbol_count = 70`).
