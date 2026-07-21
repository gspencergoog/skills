---
name: proposal-writer
description: Helps write technical proposals, system designs, RFCs, or architecture documents for software products. Use this skill when: (1) the user asks to write a technical proposal, RFC, or system design document, (2) you need to propose a new software architecture or major feature, or (3) you want to document a software design before implementation.
---

# Writing technical proposals

Use this skill to write structured technical proposals for software products.

## Choose the proposal depth

Select the appropriate template based on the size and complexity of the project:

* **Lightweight**: Use
  [lightweight_template.md](file:///Users/gspencer/code/cheats/agents/skills/proposal-writer/references/lightweight_template.md)
  for small features, minor changes, or simple refactors.
* **Standard**: Use
  [standard_template.md](file:///Users/gspencer/code/cheats/agents/skills/proposal-writer/references/standard_template.md)
  for typical features, new services, or moderate refactors.
* **Detailed**: Use
  [detailed_template.md](file:///Users/gspencer/code/cheats/agents/skills/proposal-writer/references/detailed_template.md)
  for major features, new products, or large-scale system migrations.

Use the `/grill-me` slash command to request the proposal depth or any
information needed to complete the proposal.

## Gather information

Before writing, gather the necessary context:
1. Identify the target audience (e.g., engineers, product managers, security
   teams).
2. Define the problem being solved.
3. Understand the constraints (e.g., time, resources, existing systems).

If the requirements are underspecified or if there are open design decisions,
recommend that the user runs the `/grill-me` slash command to resolve them
through an interactive interview.

## Write the proposal

Follow the selected template. Apply these practices while writing:

### Define explicit non-goals
Specify what is out of scope. List at least three non-obvious items that this
proposal will not address. This prevents scope creep.

### Include architecture diagrams
Create a Mermaid diagram in the "Proposed architecture" section to show how
components interact. Verify that the diagram matches the text description.

### Review APIs
If the proposal introduces new APIs or modifies existing ones, use the
[api-review](file:///Users/gspencer/.gemini/config/skills/api-review/SKILL.md)
skill to verify the API design.

### Apply natural writing style
Follow the rules in the
[write-prose](file:///usr/local/google/home/gspencer/.gemini/config/skills/write-prose/SKILL.md)
skill to ensure the proposal is written in a clear, neutral, and natural tone.
Avoid AI-specific phrasing, puffery, and promotional language.

## Run a self-critique

Before presenting the proposal to the user, review the draft from the
perspective of a senior reviewer.

1. Analyze the design for:
   * Single points of failure.
   * Scalability bottlenecks.
   * Security vulnerabilities.
   * Data migration difficulties.
2. Update the "Alternatives considered" or "Risks and mitigations" sections to
   address these findings.
3. Document the critique and how it was resolved in your final response to the
   user.

If the self-critique reveals major unresolved design trade-offs, recommend that
the user runs the `/grill-me` slash command to align on the best path forward.