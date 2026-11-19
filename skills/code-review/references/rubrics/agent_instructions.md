# Rubric: Agent Instructions

Use this rubric for files that a model executes: `SKILL.md`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.cursorrules`, `.clinerules`, `.windsurfrules`, and Markdown files under `rules/`, `policies/`, `prompts/`, `.github/instructions/`, or `agents/`. The main question is: will a model that follows this file step by step do the right thing, and stop safely when a step fails?

Review these files with the rigor you give code. Apply the code criteria in [reviewer_mandate.md](../reviewer_mandate.md) to their control flow and commands, and read [reviewing_documents.md](../reviewing_documents.md) for the lenses, categories, evidence rules, and severity scale. Wording findings stay `low`.

## Control Flow and Ordering

- Steps appear in the order they must run, and each step says when it is done. A step that needs the output of a later step is `inconsistency`.
- A branch covers every case it names. An "if X" step with no path for a likely "not X" is `incompleteness`.
- Instructions do not contradict each other, or another skill or rule file that the agent loads at the same time (sibling skills, `AGENTS.md`, global rules). Quote both sides. A contradiction on a path the agent always takes is `high`.
- Every loop and retry has an exit: a cap, a timeout, or a condition the agent can check.

## Failure and Fallback States

- Each command, tool call, or network step says what to do when it fails, times out, or returns nothing. A missing fallback on a step that often fails (network, authentication, quota, an optional tool that may be absent) is `incompleteness`, usually `medium` or `high`.
- The file says what the agent tells the user when it cannot finish.

## Shell Commands

Check every fenced `bash` or `sh` block, and every inline command, as code:

- An unquoted expansion (`$var`, `$(cmd)`, or a glob) in a path or argument splits on spaces or expands to the wrong files. The fix is quoting (`"$var"`).
- Text from outside the checkout (PR titles, branch names, issue text, user input) that reaches a command line without quoting or validation is a command injection hazard: `high`, or `critical` when the command runs with credentials.
- A destructive command (`rm -rf`, `git reset --hard`, `git push --force`) checks its target first or asks the user.
- The command works in the shell the file names, and on every platform the file claims (for example, GNU and BSD `sed -i` differ).

## Paths, Flags, and Tools

Verify each reference on disk before you trust it:

- Each relative or absolute script path resolves from the directory the instructions run in. Check with `fd`, `ls`, or a file read. A dead path on a required step is `high` `drift`.
- Each command-line flag exists in the script's argument parser or in the tool's `--help` output. Quote the parser line as evidence.
- Each tool, MCP server, and environment variable the file relies on is standard for the host, or the file says what to do when it is missing. An unstated dependency is `incompleteness`.

## Frontmatter and Loader Rules

- The YAML frontmatter parses, and its fields match what the loader expects. For a skill, `name` matches the directory name, and a `description` that contains `: ` or ` #` is quoted or a block scalar.
- The description says when to use the file and when not to, so the loader can route to it.
- Every file the instructions tell the agent to read exists, and every link resolves.
