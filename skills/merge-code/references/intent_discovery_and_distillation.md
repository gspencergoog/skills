# Intent Discovery & Multi-Language AST Distillation

This guide explains how to reconstruct the base state ($I_{\text{base}}$), extract the change intents from both branches ($\Delta I_{\text{ours}}$ and $\Delta I_{\text{theirs}}$), and detect silent semantic conflicts across programming languages.

## Table of Contents

1. [The 3-Way Intent Model](#1-the-3-way-intent-model)
2. [Multi-Language AST & Semantic Inspection Toolbox](#2-multi-language-ast--semantic-inspection-toolbox)
3. [Tracing Symbol & File History Across Directory Moves](#3-tracing-symbol--file-history-across-directory-moves)
4. [Catching Silent Constructor & Signature Drift](#4-catching-silent-constructor--signature-drift)
5. [Schema & Enum Registry Parity Checklist](#5-schema--enum-registry-parity-checklist)

______________________________________________________________________

## 1. The 3-Way Intent Model

Line-based diff tools compare `OURS` directly against `THEIRS`. That comparison hides which branch changed the original behavior and why. Always evaluate three points:

- **Base Invariant ($I_{\text{base}}$)**: What contract, input types, or state ownership did the code enforce at the merge base?
- **Ours Intent ($\Delta I_{\text{ours}} = \text{Intent}(BASE \to OURS)$)**: What problem did the target branch solve since `BASE`?
- **Theirs Intent ($\Delta I_{\text{theirs}} = \text{Intent}(BASE \to THEIRS)$)**: What problem did the incoming branch solve since `BASE`?

Run the pre-flight analyzer first to harvest commit histories and stage OIDs without touching the working tree:

```bash
python3 scripts/merge_assistant.py analyze --ours HEAD --theirs <source-branch> --json
```

______________________________________________________________________

## 2. Multi-Language AST & Semantic Inspection Toolbox

Repositories often contain multiple languages (TypeScript, Dart, Python, Swift, Kotlin, Rust, Go, C++, JSON/YAML schemas). Use the most accurate AST or semantic tool available for each language in the workspace:

| Tool / Engine | Best Use Case | Example Command |
| :--- | :--- | :--- |
| **`sem` CLI** ([sem-semantic-info](../../sem-semantic-info/SKILL.md)) | Tree-sitter structural diffs and cross-file call-graph impact across languages. | `sem diff <base> <theirs> --format json` |
| **Language LSPs & Analyzers** | Type-checking merged trees and finding broken call sites or missing overrides. | Dart MCP `analyze_files`, `tsc --noEmit`, `mypy .`, `swift build` |
| **Bundled AST Analyzers** ([cognitive-complexity](../../cognitive-complexity/SKILL.md)) | Listing parsed functions, classes, and line ranges across `.py`, `.ts`, `.dart`, `.swift`, `.kt`. | `python3 ../cognitive-complexity/scripts/cognitive_complexity.py -f json <file>` |
| **`git diff` `xfuncname` Drivers** | Viewing function-scoped diffs using Git's built-in language chunk headers. | `git diff -W <base>..<theirs> -- <file> \| cat` |
| **`rg` and `fd`** ([unix-cli-best-practices](../../unix-cli-best-practices/SKILL.md)) | Locating moved files (`fd`) and finding all call sites or imports of a symbol (`rg`). | `rg -w "<SymbolName>"` / `fd "<filename>"` |

______________________________________________________________________

## 3. Tracing Symbol & File History Across Directory Moves

When a directory restructure (for example, moving `renderers/web_core/src/v0_9/` to `typescript/web_core/src/`) hides a file's history from path-based `git log` checks:

1. **Locate the relocated file with `fd`**:
   ```bash
   fd "generic-binder.ts"
   ```
2. **Search across the whole tree by symbol name with `rg`**:
   ```bash
   rg -n "class SurfaceModel|function resolveDynamicValue"
   ```
3. **Trace function evolution across renames with `git log -L`**:
   ```bash
   git log --follow -M25% -L :resolveDynamicValue:typescript/web_core/src/rendering/data-context.ts | cat
   ```
4. **Find the exact commit that added, renamed, or removed a symbol with `-S`**:
   ```bash
   git log -n 5 --oneline -S "A2uiCompileError" <base>..<theirs> | cat
   ```

______________________________________________________________________

## 4. Catching Silent Constructor & Signature Drift

A merge can finish with zero textual conflicts and still fail at compile time or runtime. This happens when Branch A changes a symbol's contract in `file_a` while Branch B adds a new call site in `file_b`.

Check these four silent hazard categories on every merge:

1. **Removed Optional Parameters**:
   - *Scenario*: `OURS` simplified `DirectJsonParser.__init__(self, catalog)` by removing `validator=None`, while `THEIRS` added tests calling `DirectJsonParser(catalog, validator=v)`.
   - *Fix*: Restore the optional parameter with a backward-compatible default (`validator=None`) or update the new callers in `THEIRS` to use the new validation entry point.
2. **Changed Input or Return Shapes**:
   - *Scenario*: `OURS` changed `PayloadValidator.validate()` to accept a flat component list, while `THEIRS` passed a message envelope (`{"updateComponents": ...}`).
   - *Fix*: Inspect both callers; either normalize envelopes at the start of `validate()` or adapt the incoming caller.
3. **Deprecated Getters Masking Mock Literals**:
   - *Scenario*: `OURS` renamed property `catalog` to `defaultCatalog` and kept a getter for `catalog`. Plain object mocks in `THEIRS` written as `{catalog: mockCatalog}` pass type-checking in some languages but leave `defaultCatalog` undefined at runtime.
   - *Fix*: Run `rg "catalog:"` across test files added or modified in `BASE..THEIRS`.
4. **Deleted Exceptions or Helper Types**:
   - *Scenario*: `THEIRS` deleted `A2uiCompileError` from core packages in favor of `A2uiCompilationError`, while `OURS` added new `catch` blocks or exports for `A2uiCompileError`.
   - *Fix*: Run `rg -w "A2uiCompileError"` across the entire merged workspace after resolving conflicts.

______________________________________________________________________

## 5. Schema & Enum Registry Parity Checklist

When either branch modifies shared specification files (JSON Schema, Protobuf, OpenAPI, GraphQL, or shared YAML test definitions):

1. Extract added `enum` values, `$defs` types, or action names from the schema diff:
   ```bash
   git diff <base>..<theirs> -- "conformance/**.json" "specification/**.json" | cat
   ```
2. If `OURS` refactored inline schema objects into `$defs` (using `$ref`), port `THEIRS`'s new properties and enum values into the `$defs` target rather than re-inlining the block.
3. Use `rg` to locate every language-specific action dispatcher, exhaustive `switch`/`when`/`match`, or `UNIMPLEMENTED_ACTIONS` allowlist across the repository:
   ```bash
   rg "UNIMPLEMENTED_ACTIONS|parse_chunk|create_processor"
   ```
4. Register any newly introduced schema actions in the corresponding language test runners or skip-lists so conformance suites stay green across all SDKs.
