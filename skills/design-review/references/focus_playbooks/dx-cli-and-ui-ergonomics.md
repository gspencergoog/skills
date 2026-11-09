# DX, CLI, and UI Ergonomics

Playbook id: `dx-cli-and-ui-ergonomics`. Primary principles: `ergonomics-coherence`, `tradeoffs-simplicity`. Lane for merges: B.

## Cues (for triage)

- New or modified CLI commands, flags, exit codes, configuration files, or environment variables.
- Developer SDK entry points, interactive UI components, `genui` / A2UI surfaces, or workflow wizards.
- Human-facing or agent-facing error diagnostics, progress output, and structured JSON/JSONL streams.

## Failure modes

1. Common user intents require editing multiple files, passing long flag combinations, or repeating boilerplate (high viscosity under Green and Petre's cognitive dimensions) instead of working with sensible defaults.
   Look at: CLI synopsis, quick-start examples, and configuration schemas. Settles it: count the flags or config edits required for the primary 80% use case; `Read`.
2. Command behavior changes silently based on undocumented environment variables, working-directory state, or global config files (hidden dependencies), making runs non-reproducible across machines or agent turns.
   Look at: `os.environ` reads, relative path resolution, and implicit state lookups. Settles it: trace all inputs that alter execution without appearing in CLI flags or explicit config; `Read`.
3. Commands take multiple positional arguments of the same type or ambiguous boolean flags, inviting ordering mistakes or accidental destructive actions (error-proneness).
   Look at: argument parser definitions, positional parameter ordering, and flag names. Settles it: inspect `argparse` or CLI specs for named flags on multi-parameter or destructive operations; `Read`.
4. Destructive, mutating, or batch operations lack a `--dry-run` or preview mode (lack of provisionality), forcing users or agents to execute irreversible mutations to see what would change.
   Look at: mutation commands, deletion scripts, and batch migration flags. Settles it: check whether a read-only preview flag or confirmation gate exists before irreversible writes; `Read`.
5. Progress logs, warnings, or human banners print to `stdout` instead of `stderr`, corrupting machine-parsed `--json` or `--csv` pipelines, or failure paths return exit code `0`.
   Look at: `print` calls, logging handlers, and `main()` return codes on validation or runtime errors. Settles it: trace `stdout` vs. `stderr` streams and exit codes (`0`, `1`, `2`) across success and error paths; `Read`.
6. Interactive UI surfaces omit keyboard navigation, focus management, ARIA labels, dark/light theme adaptability, or fallback handling when an auxiliary UI pane is unavailable.
   Look at: UI component hierarchies, theme variable usage, and Tier 2/Tier 3 fallback branches. Settles it: check the UI spec or component code for accessibility attributes and headless fallback rules; `Read`.

## Hypothesis templates

- "Running the default `<command>` workflow requires `<N>` manual flags or config edits because `<parameter>` lacks a sensible default."
- "Command `<command>` reads ambient state from `<env_or_cwd>` without logging or flag override, causing non-deterministic behavior across environments."
- "Passing `--json` to `<command>` emits human status lines on `stdout` at `<path:line>`, breaking downstream JSON parsers."
- "Destructive operation `<subcommand>` modifies `<target>` immediately without a `--dry-run` preview or confirmation gate."
- "UI surface `<component>` fails or traps input when `<pane_or_feature>` is absent because no keyboard or Tier 2/3 fallback is defined."
