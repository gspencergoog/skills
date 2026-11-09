# Agent Skill and Prompt Design

Playbook id: `agent-skill-and-prompt-design`. Primary principles: `module-depth`, `ergonomics-coherence`, `boundaries-state`. Lane for merges: B.

## Cues (for triage)

- New or modified agent skills (`SKILL.md`), reference docs (`references/*.md`), or skill helper scripts (`scripts/*.py`).
- Multi-agent orchestration (`/swarm`, subagents, clean-room reviewers, or workflow pipelines).
- Prompt templates, tool declarations, or context-indexing pipelines.

## Failure modes

1. A `SKILL.md` file exceeds the single-read byte budget (`43,800` raw bytes or `46,080` line-numbered bytes), causing `read_file` truncation so the agent never sees the trailing workflow rules or safety gates.
   Look at: projected or actual byte size of `SKILL.md` and inlined reference tables or templates. Settles it: run `packrat_context.py --check-skill-budget` or measure byte length (`wc -c`); `Executed`.
2. Core workflow instructions inline bulky domain catalogs, long code templates, or rare branch rules directly in `SKILL.md` instead of pushing them to on-demand `references/*.md` files (broken progressive disclosure).
   Look at: sections in `SKILL.md` exceeding 60 lines that apply only to specific modes or languages. Settles it: check whether conditional tables and rubrics are factored into linked `references/*.md` files; `Read`.
3. Prompts ask the LLM to perform deterministic arithmetic, AST parsing, regex link resolution, or JSON schema validation in prose instead of delegating those checks to a tested script in `scripts/`.
   Look at: prompt instructions asking the agent to count lines/words, verify math, or validate strict report schemas manually. Settles it: check whether a deterministic script with `unittest` coverage handles the mechanical step; `Read`.
4. Subagent or swarm reviewer briefs forward raw conversation history, full transcripts, or author rationale instead of a clean-room Briefing Packet (`< 10 KB` total, `< 4 KB` for `--post-design`), anchoring the critic on the author's framing.
   Look at: reviewer prompt templates, `fork` settings, and context fields passed to `swarm` or `invoke_subagent`. Settles it: verify that reviewers start cold (`fork` omitted) and receive only paths, constraints, and settled decisions; `Read`.
5. Skill Markdown hardcodes user-specific workstation hostnames, absolute home paths, internal shortlinks, or unguarded destructive shell commands, breaking portability across agent harnesses or violating read-only mode.
   Look at: file path examples, URLs, frontmatter `(use X)` pointers, and tool guardrails. Settles it: check that placeholders (`<conversation-artifact-dir>`, `<conversation-scratch-dir>`) replace local paths and `public_skills.json` governs public `(use X)` references; `Read` or `Executed`.
6. Frontmatter `description` violates the trigger contract (`< 200` or `> 800` chars, missing third-person verb opener, missing `Use when` or `Don't use for`, or unreferenced `references/*.md` files).
   Look at: YAML frontmatter and markdown links in `SKILL.md`. Settles it: run `python3 agents/skills/skill-creator/scripts/lint_skill.py <skill-dir>`; `Executed`.

## Hypothesis templates

- "`<skill>/SKILL.md` measures `<N>` bytes (or inlines `<large_section>`), risking single-read truncation instead of loading `<reference_file>` on demand."
- "Step `<step>` relies on LLM prose judgment to validate `<deterministic_rule>` instead of enforcing it in `<script_name>.py`."
- "The reviewer brief passes `<author_context_or_history>` to `<worker>`, biasing the clean-room critique or exceeding the `< 10 KB` brief cap."
- "`<file>` embeds non-portable path or link `<literal>` instead of `<conversation-artifact-dir>` or `<conversation-scratch-dir>`."
- "Skill `<skill>` omits an explicit read-only or cleanup guardrail for `<temp_or_target_path>` on early exit."
