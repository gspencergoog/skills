# Rubric: Reference Docs

Use this rubric for READMEs, user guides, API reference pages, `SKILL.md` and `AGENTS.md` files, changelogs, and other docs that describe existing behavior. The main question is: is the doc accurate, and does it do one job well?

Read [reviewing_documents.md](../reviewing_documents.md) first for the lenses, categories, evidence rules, and severity scale. Reference docs are where most `wording` findings come from. Keep them `low`.

## Accuracy Against the Code

This is the highest-value check for reference docs. For each command, flag, option, symbol, file path, environment variable, and default value the changed text names:

- Confirm it exists, with `rg`, `fd`, or by reading the source. Quote what you found as evidence.
- Confirm the described behavior matches the code. A doc that tells users to run something that doesn't exist or does something else is `high` `drift`.
- Confirm examples would run: correct syntax, imports, argument order, and output.

## One Job Per Page

Following the Diátaxis framework, each page does one of four jobs:

| Job | Reader need | Signs it is mixed with another job |
| :--- | :--- | :--- |
| Tutorial | Learn by doing | Long reference tables in the middle of steps |
| How-to guide | Finish a specific task | Background theory before the first step |
| Reference | Look up facts | Opinions, step-by-step instructions |
| Explanation | Understand why | Command listings |

A change that mixes jobs on one page is `structure`, usually `low` or `medium`. Raise it only when it makes the page harder to use for its main reader.

## Kept in Step With Behavior

- When the diff changes behavior (in the same PR, or as described in the PR), the docs that describe that behavior change too. A doc left stale is `high` `drift`. Use the downstream impact pass to find those docs.
- Removed features, flags, or files are removed from the docs, including tables of contents and index pages.

## Links and Anchors

- Relative links resolve to files that exist. Heading anchors (`#section-name`) match a real heading.
- Renamed or moved files have their inbound links updated. List broken inbound links in the Structural Assessment.
- A broken link is `medium` `structure`. A broken link in installation or setup steps is `high`.

## Agent Instruction Files

For `SKILL.md`, `AGENTS.md`, rules, and prompt files, the reader is a model:

- Instructions that conflict with each other, or with another instruction file the agent also loads, are `inconsistency`.
- Steps are in the order they must run, and each step says when it is done.
- Referenced scripts, files, and tools exist at the paths given.
- Frontmatter fields match the format the loader expects.

## Changelogs

- The entry describes the change the diff makes, in the user's terms, under the right version heading.
- Breaking changes are labeled as breaking and say what users must do.
- The entry doesn't claim changes the diff doesn't contain.
