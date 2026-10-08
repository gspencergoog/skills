---
name: write-prose
description: Master prose writing and orchestration skill for creating clear, plain, accessible, and natural human writing. Synthesizes ISO 24495-1:2023, W3C Cognitive Accessibility Guidance, Plain Writing Act, and Simplified Technical English (STE) standards. Use when writing, drafting, editing, or reviewing reports, PR descriptions, commit messages, API docstrings, READMEs, architecture documents, user guides, prompts, or general technical prose.
---

# Master prose writing & orchestration (`write-prose`)

> [!CAUTION]
> **MANDATORY LINGUISTIC AUDIT**
> Language models naturally default to pre-trained "AI-isms" (*delve, leverage, seamless, robust, pivotal, testament, not only... but also*), em-dash (`—`) punctuation, and formulaic inline-header lists (`- **Title**: body`). You MUST actively execute the two-pass drafting protocol below to audit and replace machine-generated fluff before producing final output.

> [!IMPORTANT]
> **WORKSPACE HYGIENE & FILE LOCATION RULES**
>
> - Never create temporary draft files or scratch markdown in the project repository root or source directories.
> - Store intermediate drafts, multi-pass review files, or scratch notes in the conversation scratch directory: `<appDataDir>/brain/<conversation-id>/scratch/`.
> - Write persistent, user-facing markdown reports or documents to the conversation artifacts directory: `<appDataDir>/brain/<conversation-id>/`.

This skill is the central source of truth for clear, plain, accessible, and natural human writing across technical and non-technical documents.

## Procedural workflow

### Step 1: Context & audience inference

Determine the target audience and document format:

1. **Pull requests / commits**: Engineering peers $\rightarrow$ Focus on "Why" over "How", factual tone, no fluff.
2. **API documentation / docstrings**: API consumers $\rightarrow$ Third-person singular verbs, concise summaries, clear parameter prose.
3. **User guides / tutorials**: End users $\rightarrow$ Direct second-person ("you"), short critical paths, explicit step-by-step instructions.
4. **Architecture / design RFCs**: Team leads & stakeholders $\rightarrow$ Clear tradeoffs, decision-first layout, plain language.
5. **Prompts & system instructions**: AI agents / LLMs $\rightarrow$ Imperative tone, XML tags, positive directives, assume high baseline knowledge (name concepts without explaining them).

Evaluate reader knowledge and conceptual boundaries:

- Strip external meta-task framing (such as plan phase numbers, task milestone labels, execution option names, or prompt scoping structures) unless the concept is explicitly defined *within* the document itself.
- Ensure the document stands alone without assuming the reader has access to prompt history, conversation transcripts, or external planning documents.

> [!NOTE]
> If the target audience or document context is ambiguous, use `ask_question` to clarify before drafting.

For detailed audience templates and tone matrices, see [references/audiences.md](references/audiences.md).

______________________________________________________________________

### Mode A: Writing & drafting new prose

#### Step 2: Sub-skill coordination

For specialized document types, delegate content gathering to domain skills while enforcing `write-prose` quality standards:

- **Pull request descriptions**: Refer to [write-pr-description](../write-pr-description/SKILL.md) for diff structure and testing steps.
- **Commit messages**: Refer to [commit-changes](../commit-changes/SKILL.md) for conventional commit formatting.
- **Code & API documentation**: Refer to [code-documentation](../code-documentation/SKILL.md) for docstring and tag conventions.
- **AI prompts & skill instructions**: Refer to Section 7 in [references/standards.md](references/standards.md#7-prompt-design-standards-for-llm-audiences) for XML tagging, primacy placement, and token conservation.

#### Step 3: Apply plain writing & accessibility standards

Before writing, consult [references/standards.md](references/standards.md) to apply core principles from:

- **ISO 24495-1:2023**: Ensure content is relevant, findable, understandable, and usable.
- **W3C Cognitive Accessibility (COGA)**: Use clear words, literal language, short text, separate steps, short critical paths, and no reliance on memory.
- **Plain Writing Act**: Ensure immediate first-reading clarity using active voice and short sentences (15–20 words max).
- **Simplified Technical English (STE / ASD-STE100)**: Use controlled vocabulary, explicit sequential steps, max 3 nouns per cluster, and warnings before actions.

#### Step 4: Two-pass drafting & anti-AI-ism self-correction

Execute this mandatory two-pass procedure before finalizing output:

1. **Pass 1 (Content draft)**: Draft the response focusing on technical accuracy, structure, and domain content.
2. **Pass 2 (Linguistic inspection & rewrite)**:
   - Scan the draft line-by-line against the positive replacement pairs in [references/standards.md](references/standards.md#61-positive-replacement-pairs-banned-words-mappings).
   - Flag and replace banned verbs (*delve, leverage, foster, cultivate, maximize, democratize, resonate, encompass, bridge, underscore, highlight, ensure, align with*) and adverbs (*seamlessly, extensively*).
   - Replace vague or promotional adjectives (*robust, seamless, pivotal, crucial, holistic, intuitive, comprehensive, frictionless, scalable, synergistic, premier, state-of-the-art*) with **specific physical or technical behaviors** (e.g. replace *"robust error handling"* with *"retries failed HTTP requests up to 3 times"*).
   - Eliminate copula substitutions (*"serves as"* / *"stands as"* $\rightarrow$ *"is"*), dangling `-ing` commentary clauses (*", highlighting..."*, *", ensuring..."*), negative parallelism (*"not only... but also"*, *"not just about X; it is about Y"*), false ranges (*"from X to Y"*), rule-of-three padding, and formulaic *"Despite challenges..."* conclusions ([references/standards.md](references/standards.md#64-sentence-structure--tone-anti-ai-isms)).
   - Remove all em-dash (`—`) characters ([references/standards.md](references/standards.md#65-formatting-punctuation--integrity-anti-ai-isms)). Replace them with commas, parentheses, colons, or separate sentences.
   - Eliminate overuse of inline-header lists (`- **Title**: body`) ([references/standards.md](references/standards.md#65-formatting-punctuation--integrity-anti-ai-isms)). Not every bullet item has to begin with a bold statement. Use plain prose paragraphs or unbolded bullet items unless defining a glossary or parameter list.
   - Run the **Fresh Reader Test** ([references/standards.md](references/standards.md#8-standalone-readability--meta-context-isolation)) to verify conceptual and frame isolation. Remove undefined meta-task labels (plan tiers, phase numbers), ephemeral subagent IDs, conversation turn references, and absolute local system paths.
3. **Output**: Present only the polished, post-audit prose.

______________________________________________________________________

### Mode B: Reviewing & auditing existing prose

When the user asks to review, audit, or critique an existing document, PR description, or prompt:

1. **Run the statistical analyzer**:
   Execute the analyzer script on the target file:

   ```bash
   python3 ./scripts/analyze_prose.py <path-to-file>
   ```

2. **Evaluate output metrics**:

   - Check total word count, sentence count, median sentence length, and paragraph stats.
   - Note any sentences exceeding **25 words**, paragraphs exceeding **4 sentences**, **banned AI words or phrases**, **em-dash (`—`) characters**, **smart quotes**, or **overused inline-header lists** returned by the script.

3. **Generate audit report table**:
   Output a clear feedback report detailing findings and concrete fixes:

   | Line / Location | Issue / Violation                 | Standard Violated         | Suggested Plain Language Fix                          |
   | :-------------- | :-------------------------------- | :------------------------ | :---------------------------------------------------- |
   | Line 12         | *"serves as a robust framework"*  | Anti-AI-ism / Copula Sub  | *"is a framework that retries HTTP requests"*         |
   | Line 19         | Em-dash (`—`) clause splice       | Anti-AI-ism / Punctuation | Replace `—` with a comma or split into two sentences. |
   | Lines 22–28     | Inline-header list on all bullets | Anti-AI-ism / List Style  | Remove bold lead-in titles or rewrite as prose.       |
   | Line 34         | Sentence length (42 words)        | STE / Plain Language      | Split into two sentences ($\le 20$ words each).       |
