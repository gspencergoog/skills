---
name: proposal-writer
description: >-
  Writes formal technical proposals, RFCs, and stakeholder-facing architecture
  documents, choosing a lightweight, standard, or detailed template by project
  size and including non-goals, diagrams, an API review, and a self-critique
  pass. Use when the user asks for an RFC, stakeholder proposal, or formal
  design document for team or cross-team sign-off. Don't use for interactive
  option exploration and in-session design convergence (use just-brainstorm) or
  for the engineering execution plan with file-level steps (use plan).
---

# Writing technical proposals

Use this skill to write formal, stakeholder-facing technical proposals and RFCs for human review and sign-off. For interactive design exploration, weighing candidate architectures, and converging an in-session design spec before `/plan`, use [just-brainstorm](../just-brainstorm/SKILL.md) instead.

## Choose the proposal depth

Select the appropriate template based on the size and complexity of the project:

- **Lightweight**: Use
  [lightweight_template.md](references/lightweight_template.md)
  for small features, minor changes, or simple refactors.
- **Standard**: Use
  [standard_template.md](references/standard_template.md)
  for typical features, new services, or moderate refactors.
- **Detailed**: Use
  [detailed_template.md](references/detailed_template.md)
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
[api-review](../api-review/SKILL.md)
skill to verify the API design.

### Maintainability & Complexity Standards

Define explicit code health standards and maintainability metrics for the proposed design. Reference the
[cognitive-complexity](../cognitive-complexity/SKILL.md)
skill when specifying limits on architectural branching and function complexity (e.g., standard threshold $\\le 15$).

### Apply natural writing style

Follow the rules in the
[write-prose](../write-prose/SKILL.md)
skill to ensure the proposal is written in a clear, neutral, and natural tone.
Avoid AI-specific phrasing, puffery, and promotional language.

## Run a self-critique

Before presenting the proposal to the user, review the draft from the
perspective of a senior reviewer.

1. Analyze the design for:
   - Single points of failure.
   - Scalability bottlenecks.
   - Security vulnerabilities.
   - Data migration difficulties.
2. Update the "Alternatives considered" or "Risks and mitigations" sections to
   address these findings.
3. Document the critique and how it was resolved in your final response to the
   user.

If the self-critique reveals major unresolved design trade-offs, recommend that
the user runs the `/grill-me` slash command to align on the best path forward.
