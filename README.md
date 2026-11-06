# Greg's skills repo

<a href="https://agentskills.io"><img src="https://img.shields.io/badge/Compliance-Standard%20Agent%20Skills-brightgreen.svg" alt="Standard Agent Skills"></a>

A repo for agent skills I want to share with others.

These are mainly skills I use in my daily coding.  They should be *fairly* general, but some will include things that only I care about.

I mostly created this repo to make it easier to share skills with other people, so don't expect it to be maintained with a high level of responsiveness.

I'll happily take PRs, but it might take me a long time to review them, if I get to them at all.

## Skills Inventory

<!-- SKILLS_LIST_START -->

To install any skill individually:

```bash
dart run skills@ add gspencergoog/skills --skill <skill-name>
# or
npx skills add gspencergoog/skills --skill <skill-name>
```

| Skill | Description |
| :--- | :--- |
| **[agy-debugger](skills/agy-debugger/SKILL.md)** | Tool call auditor and real-time debugger web UI for agent sessions. |
| **[analyze-github-pr](skills/analyze-github-pr/SKILL.md)** | Fetches and summarizes GitHub PR descriptions, reviews, comments, and CI diagnostics. |
| **[api-review](skills/api-review/SKILL.md)** | Reviews code against canonical API design principles with concrete rewrites. |
| **[branch-cleanup](skills/branch-cleanup/SKILL.md)** | Identifies and prunes merged local and remote git branches and worktrees. |
| **[code-documentation](skills/code-documentation/SKILL.md)** | Writes and audits docstrings and comments across Dart, Python, and TypeScript. |
| **[code-review](skills/code-review/SKILL.md)** | Multi-pass code review with evidence tiers, severity caps, and focus playbooks. |
| **[cognitive-complexity](skills/cognitive-complexity/SKILL.md)** | Calculates Sonar Cognitive Complexity across Python, TypeScript, Dart, Swift, Kotlin, and C++. |
| **[commit-changes](skills/commit-changes/SKILL.md)** | Formats, lints, and commits changes with conventional commit messages. |
| **[context-indexer](skills/context-indexer/SKILL.md)** | Stages diffs and Markdown into size-bounded chunks and relational tables for DuckDB SQL. |
| **[cost-optimization](skills/cost-optimization/SKILL.md)** | Profiles and reduces LLM inference cost, context size, and token usage. |
| **[dart-cli-app-best-practices](skills/dart-cli-app-best-practices/SKILL.md)** | Guidance and conventions for structuring command-line applications in Dart. |
| **[dart-concurrency](skills/dart-concurrency/SKILL.md)** | Diagnoses and resolves asynchronous concurrency bugs, race conditions, and test timeouts in Dart. |
| **[dart-modern-features](skills/dart-modern-features/SKILL.md)** | Applies modern Dart 3 features including patterns, records, and class modifiers. |
| **[design](skills/design/SKILL.md)** | Interactive architecture design, trade-off evaluation, and spec convergence. |
| **[duckdb](skills/duckdb/SKILL.md)** | Queries local structured data, diffs, and Markdown files using DuckDB SQL. |
| **[flutter-architecture](skills/flutter-architecture/SKILL.md)** | Guides Flutter app architecture following MVVM, repositories, and reactive state. |
| **[flutter-concurrency](skills/flutter-concurrency/SKILL.md)** | Offloads CPU-bound computation and JSON parsing to Flutter background isolates. |
| **[flutter-testing](skills/flutter-testing/SKILL.md)** | Comprehensive guide for unit, widget, and integration testing in Flutter. |
| **[gh-cli](skills/gh-cli/SKILL.md)** | GitHub CLI recipes for pull requests, issues, actions, and API scripting. |
| **[git-stack](skills/git-stack/SKILL.md)** | Manages and cascade-rebases stacked GitHub pull requests across worktrees. |
| **[grill-me](skills/grill-me/SKILL.md)** | Interactive interview session to stress-test designs and resolve assumptions before planning. |
| **[matcher-to-checks](skills/matcher-to-checks/SKILL.md)** | Migrates Dart and Flutter tests from package:matcher to package:checks. |
| **[merge-code](skills/merge-code/SKILL.md)** | Three-way semantic merge resolver reconstructing developer intent across branches and forks. |
| **[mermaid-diagrams](skills/mermaid-diagrams/SKILL.md)** | Creates software architecture, sequence, and workflow diagrams using Mermaid syntax. |
| **[monitor](skills/monitor/SKILL.md)** | Watches GitHub pull requests and review queues with automated CI and merge conflict remediation. |
| **[nochange](skills/nochange/SKILL.md)** | Explains, analyzes, and reviews code in a strictly read-only mode without file mutations. |
| **[packrat](skills/packrat/SKILL.md)** | Local SQLite WAL and DuckDB mirror of GitHub repositories, PRs, code symbols, and vector search. |
| **[plan](skills/plan/SKILL.md)** | Phased implementation planning with parallel execution waves and plan validation. |
| **[pr-feedback-handler](skills/pr-feedback-handler/SKILL.md)** | Addresses review feedback on your PRs via interactive dashboard or automated fixes. |
| **[pr-review](skills/pr-review/SKILL.md)** | End-to-end pull request code reviewer with blind-first intent matching and review dashboard staging. |
| **[pr-reviewer-recommendation](skills/pr-reviewer-recommendation/SKILL.md)** | Recommends PR reviewers using git blame ownership and historical review participation. |
| **[proposal-writer](skills/proposal-writer/SKILL.md)** | Drafts formal technical proposals, RFCs, and architecture decision records. |
| **[sem-semantic-info](skills/sem-semantic-info/SKILL.md)** | Semantic code search and structural diff tool tracking AST functions, classes, and types. |
| **[ship-it](skills/ship-it/SKILL.md)** | Pre-review release pipeline covering PR slicing, coverage audits, and review loops. |
| **[skill-creator](skills/skill-creator/SKILL.md)** | Creates, structures, and lints agent skills against specification standards. |
| **[unix-cli-best-practices](skills/unix-cli-best-practices/SKILL.md)** | Modern command-line patterns using ripgrep, fd, jq, and POSIX utilities. |
| **[write-pr-description](skills/write-pr-description/SKILL.md)** | Drafts clear, structured pull request descriptions directly from git diffs. |
| **[write-prose](skills/write-prose/SKILL.md)** | Enforces natural human writing free of AI-isms, em-dashes, and syntactic fluff. |

<!-- SKILLS_LIST_END -->

## Prerequisites and Support Programs

Many skills in this repository rely on external command-line tools, runtimes, and utilities. You do not need to install every tool to get started; install the programs relevant to the skills you plan to use.

### Core Command-Line Tools

These general-purpose utilities power search, file discovery, data queries, and version control across many skills.

| Program | Command | Description | Primary Skills |
| :--- | :--- | :--- | :--- |
| [ripgrep](https://github.com/BurntSushi/ripgrep) | `rg` | Fast regex code and text search across workspaces. | [unix-cli-best-practices](skills/unix-cli-best-practices/SKILL.md), [context-indexer](skills/context-indexer/SKILL.md), [design](skills/design/SKILL.md), [plan](skills/plan/SKILL.md), [code-review](skills/code-review/SKILL.md) |
| [fd](https://github.com/sharkdp/fd) | `fd` (or `fdfind`) | Fast, user-friendly file and directory finder. | [unix-cli-best-practices](skills/unix-cli-best-practices/SKILL.md), [context-indexer](skills/context-indexer/SKILL.md), [plan](skills/plan/SKILL.md), [branch-cleanup](skills/branch-cleanup/SKILL.md) |
| [DuckDB](https://duckdb.org/) | `duckdb` | Fast in-process analytical SQL database (version 2.0 or newer). Queries JSON, JSONL, Parquet, SQLite, code diffs, and Markdown. | [duckdb](skills/duckdb/SKILL.md), [context-indexer](skills/context-indexer/SKILL.md), [code-review](skills/code-review/SKILL.md), [pr-review](skills/pr-review/SKILL.md) |
| [sem](skills/sem-semantic-info/SKILL.md) | `sem` | Semantic code navigation and diff tool that tracks functions, classes, and types instead of plain text lines. | [sem-semantic-info](skills/sem-semantic-info/SKILL.md), [merge-code](skills/merge-code/SKILL.md), [context-indexer](skills/context-indexer/SKILL.md), [plan](skills/plan/SKILL.md) |
| [Git](https://git-scm.com/) | `git` | Distributed version control system for branches, diffs, and commits. | [commit-changes](skills/commit-changes/SKILL.md), [git-stack](skills/git-stack/SKILL.md), [branch-cleanup](skills/branch-cleanup/SKILL.md), [merge-code](skills/merge-code/SKILL.md), [ship-it](skills/ship-it/SKILL.md) |
| [GitHub CLI](https://cli.github.com/) | `gh` | Official CLI for pull requests, issues, reviews, and CI checks. | [gh-cli](skills/gh-cli/SKILL.md), [pr-review](skills/pr-review/SKILL.md), [pr-feedback-handler](skills/pr-feedback-handler/SKILL.md), [monitor](skills/monitor/SKILL.md), [analyze-github-pr](skills/analyze-github-pr/SKILL.md) |
| [jq](https://jqlang.github.io/jq/) | `jq` | Command-line JSON processor for piping and filtering structured tool output. | [gh-cli](skills/gh-cli/SKILL.md), [unix-cli-best-practices](skills/unix-cli-best-practices/SKILL.md), [duckdb](skills/duckdb/SKILL.md) |
| [Python 3](https://www.python.org/) | `python3` | Runtime for skill helper scripts and test suites (Python 3.10 or newer). | [context-indexer](skills/context-indexer/SKILL.md), [pr-review](skills/pr-review/SKILL.md), [skill-creator](skills/skill-creator/SKILL.md), [write-prose](skills/write-prose/SKILL.md) |
| [uv](https://docs.astral.sh/uv/) | `uv` | Fast Python package and script runner. Executes helper scripts with inline PEP 723 dependencies without manual environment management. | [cognitive-complexity](skills/cognitive-complexity/SKILL.md) |

Standard POSIX utilities (`sed`, `awk`, `xargs`, `curl`) are assumed to be available on Unix-like environments.

Skills like `code-review`, `monitor`, and `plan` can optionally integrate with [packrat](skills/packrat/SKILL.md) for local repository mirroring. Packrat is included directly in this repository rather than installed as an external tool.

### Language Runtimes and SDKs

Install these tools when working in specific programming languages or using language-specific analysis engines.

| Program | Command | Description | Relevant Skills |
| :--- | :--- | :--- | :--- |
| [Node.js](https://nodejs.org/) | `node`, `npm`, `npx` | JavaScript runtime used for local review dashboard servers and TypeScript cognitive complexity analysis. | [pr-review](skills/pr-review/SKILL.md), [pr-feedback-handler](skills/pr-feedback-handler/SKILL.md), [cognitive-complexity](skills/cognitive-complexity/SKILL.md) |
| [Dart SDK](https://dart.dev/) | `dart` | Dart language SDK for testing, analysis, and refactoring. | [dart-cli-app-best-practices](skills/dart-cli-app-best-practices/SKILL.md), [dart-concurrency](skills/dart-concurrency/SKILL.md), [dart-modern-features](skills/dart-modern-features/SKILL.md), [matcher-to-checks](skills/matcher-to-checks/SKILL.md), [cognitive-complexity](skills/cognitive-complexity/SKILL.md) |
| [Flutter SDK](https://flutter.dev/) | `flutter` | Flutter framework toolchain for UI apps and widget testing. | [flutter-architecture](skills/flutter-architecture/SKILL.md), [flutter-concurrency](skills/flutter-concurrency/SKILL.md), [flutter-testing](skills/flutter-testing/SKILL.md), [ship-it](skills/ship-it/SKILL.md) |
| [Java Runtime](https://www.java.com/) | `java` | Java runtime (JRE or JDK) required to run the Kotlin cognitive complexity analyzer JAR. | [cognitive-complexity](skills/cognitive-complexity/SKILL.md) |
| [Swift](https://www.swift.org/) | `swift` | Swift compiler and runtime for Swift cognitive complexity analysis. | [cognitive-complexity](skills/cognitive-complexity/SKILL.md) |
| [Clang-Tidy](https://clang.llvm.org/extra/clang-tidy/) | `clang-tidy` | Optional linter for C++ cognitive complexity analysis. | [cognitive-complexity](skills/cognitive-complexity/SKILL.md) |
| [Mermaid CLI](https://github.com/mermaid-js/mermaid-cli) | `mmdc` | Optional CLI (`npm install -g @mermaid-js/mermaid-cli`) to compile Mermaid diagrams to image formats. | [mermaid-diagrams](skills/mermaid-diagrams/SKILL.md) |
