# Read-Only Codebase Discovery Guide

This guide provides procedures for scouting a target codebase across languages and platforms using strictly read-only inspection. The goal is to ground every implementation plan in verified file paths, existing abstractions, and real test commands.

______________________________________________________________________

## 1. Project Ecosystem & Tooling Detection

Inspect root manifest files to identify the language runtime, dependency manager, test runner, and linters:

| File Pattern                                      | Ecosystem         | Test Runner                                         | Linter / Typechecker                      |
| :------------------------------------------------ | :---------------- | :-------------------------------------------------- | :---------------------------------------- |
| `pubspec.yaml`                                    | Dart / Flutter    | Dart MCP `run_tests` / `flutter test` / `dart test` | Dart MCP `analyze_files` / `dart analyze` |
| `pyproject.toml`, `setup.cfg`, `requirements.txt` | Python            | `pytest`, `python -m unittest`                      | `ruff`, `flake8`, `mypy`, `pylint`        |
| `package.json`, `tsconfig.json`                   | TypeScript / Node | `npm test`, `pnpm test`, `yarn test`, `bun test`    | `eslint`, `tsc --noEmit`, `biome`         |
| `Cargo.toml`                                      | Rust              | `cargo test`                                        | `cargo clippy`, `cargo check`             |
| `go.mod`                                          | Go                | `go test ./...`                                     | `golangci-lint`, `go vet`                 |
| `pom.xml`, `build.gradle`, `build.gradle.kts`     | Java / Kotlin     | `mvn test`, `./gradlew test`                        | `checkstyle`, `ktlint`, `detekt`          |
| `CMakeLists.txt`, `Makefile`                      | C / C++           | `ctest`, `make test`                                | `clang-tidy`, `cppcheck`                  |

### How to Check

- Use `view_file` to read the manifest:
  - In `package.json`: check `"scripts"` block for exact test and build commands (`"test"`, `"lint"`, `"typecheck"`).
  - In `pyproject.toml`: check `[tool.pytest]`, `[tool.ruff]`, `[tool.mypy]` sections.
  - In `pubspec.yaml`: check `dependencies` and `dev_dependencies`.

______________________________________________________________________

## 2. Locating Existing Code & Exemplars

Before writing any new function, class, or module in a plan, verify whether an existing abstraction or pattern can be reused:

1. **Find Exemplars**: Search for similar existing features or tests:
   - Use `find_by_name` to locate files matching the pattern (e.g. `*service*`, `*handler*`, `*validator*`).
   - Read the candidate files using `view_file` to study naming conventions, error handling styles, and directory placement.
2. **Locate Reusable Helpers**:
   - Use `grep_search` with literal strings or regex patterns to see if utility functions or shared base classes exist.
   - For example: when planning validation, search for existing validators rather than designing a new one from scratch.

______________________________________________________________________

## 3. Blast Radius & Impact Mapping

When modifying or deleting existing files/symbols, map their dependencies to prevent unintended breakage:

1. **Import and Call Sites**:
   - Search for the symbol or module name across the repo using `grep_search` or `rg`:

     ```bash
     rg -l "\bSymbolName\b"
     ```

   - Count the number of dependent files.
2. **Semantic Impact**:
   - If the `sem-semantic-info` skill and `sem` CLI are available, run:

     ```bash
     sem impact <path/to/target_file>
     ```

     to see affected entities and dependency relationships.
3. **Associated Tests**:
   - Locate test files covering the target code:
     - Check `tests/`, `test/`, or adjacent `*.test.ts`, `*_test.dart`, `test_*.py` files.
     - Ensure existing tests are listed in the plan to verify no regression occurs.

______________________________________________________________________

## 4. Strict Read-Only Rules

During planning, the following actions are strictly prohibited:

- **Do NOT execute file modification tools**: Never call `replace_file_content`, `write_to_file` on codebase files, or run shell scripts that modify files (`sed -i`, `awk`, `rm`, `touch`).
- **Do NOT execute mutating Git commands**: Never run `git checkout`, `git switch`, `git stash`, `git reset`, `git commit`, `git rm`, or `git pull`. Safe read-only commands include `git status`, `git log -n 10`, `git diff` (read-only), and `git branch`.
- **Do NOT run build or package install commands**: Never run `npm install`, `pub get`, `pip install`, or `cargo build` during planning unless explicitly instructed by the user. Planning is an observational phase.
