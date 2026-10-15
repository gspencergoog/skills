---
name: write-prose
description: Master prose writing and orchestration skill for creating clear, plain, accessible, and natural human writing. Synthesizes ISO 24495-1:2023, W3C Cognitive Accessibility Guidance, Plain Writing Act, and Simplified Technical English (STE) standards. Use when writing, drafting, editing, or reviewing reports, PR descriptions, inline code review comments, commit messages, API docstrings, READMEs, architecture documents, user guides, prompts, or general technical prose.
---

# Master prose writing & orchestration (`write-prose`)

> [!CAUTION]
> **MANDATORY LINGUISTIC AUDIT**
> Language models naturally default to pre-trained "AI-isms" (`delve`, `leverage`, `seamless`, `robust`, `pivotal`, `testament`, `not only... but also`), em-dash (`—`) punctuation, and formulaic inline-header lists (`- **Title**: body` or `1. **Title**: body`). Execute the two-pass drafting protocol below to audit and replace machine-generated fluff before producing final output.

> [!IMPORTANT]
> **WORKSPACE HYGIENE & FILE LOCATION RULES**
>
> - Never create temporary draft files or scratch markdown in the project repository root or source directories.
> - Store intermediate drafts, multi-pass review files, or scratch notes in the conversation scratch directory: `<appDataDir>/brain/<conversation-id>/scratch/`.
> - Write persistent, user-facing markdown reports or documents to the conversation artifacts directory: `<appDataDir>/brain/<conversation-id>/`.

This skill is the central source of truth for clear, plain, accessible, and natural human writing across technical and non-technical documents.

> [!NOTE]
> Use American English instead of British (or other languages) unless otherwise instructed (e.g. avoid things like `flavour` and `behaviour`).

## Procedural workflow

### Step 1: Context & audience inference

Determine the target audience and document format:

1. **Pull requests / commits**: Engineering peers $\rightarrow$ Focus on "Why" over "How", factual tone, no fluff.
2. **Inline code review comments**: PR authors $\rightarrow$ State the defect and consequence in 1–3 short sentences ($\le 80$ words) without restating the diff.
3. **API documentation / docstrings**: API consumers $\rightarrow$ Third-person singular verbs, concise summaries, clear parameter prose.
4. **User guides / tutorials**: End users $\rightarrow$ Direct second-person ("you"), short critical paths, explicit step-by-step instructions.
5. **Architecture / design RFCs**: Team leads & stakeholders $\rightarrow$ Clear tradeoffs, decision-first layout, plain language.
6. **Prompts & system instructions**: AI agents / LLMs $\rightarrow$ Imperative tone, XML tags, positive directives, assume high baseline knowledge (name concepts without explaining them).

Evaluate reader knowledge and conceptual boundaries:

- Strip external meta-task framing such as plan phase numbers, milestone labels, or option names unless the document defines them.
- Make sure the document stands alone without assuming access to prompt history, conversation transcripts, or external planning documents.

> [!NOTE]
> If the target audience or document context is ambiguous, prompt the user (using your interactive question tool if available, or by asking in chat and waiting for their reply) to clarify before drafting.

For detailed audience templates and tone matrices, see [references/audiences.md](references/audiences.md).

______________________________________________________________________

### Mode A: Writing & drafting new prose

#### Step 2: Sub-skill coordination

For specialized document types, delegate content gathering to domain skills while enforcing `write-prose` quality standards:

- **Pull request descriptions**: Refer to [write-pr-description](../write-pr-description/SKILL.md) for diff structure and testing steps.
- **Inline code review comments**: Refer to [code-review](../code-review/SKILL.md) and [pr-review](../pr-review/SKILL.md) for evidence tiers, comment prose budgets, and suggestion block rules.
- **Commit messages**: Refer to [commit-changes](../commit-changes/SKILL.md) for conventional commit formatting.
- **Code & API documentation**: Refer to [code-documentation](../code-documentation/SKILL.md) for docstring and tag conventions.
- **AI prompts & skill instructions**: Refer to Section 7 in [references/standards.md](references/standards.md#7-prompt-design-standards-for-llm-audiences) for XML tagging, primacy placement, and token conservation.

#### Step 3: Apply plain writing & accessibility standards

Before writing, consult [references/standards.md](references/standards.md) to apply core principles from:

- **ISO 24495-1:2023**: Make content relevant, findable, understandable, and usable.
- **W3C Cognitive Accessibility (COGA)**: Use clear words, literal language, short text, separate steps, short critical paths, and no reliance on memory.
- **Plain Writing Act**: Achieve immediate first-reading clarity using active voice and short sentences (15–20 words max).
- **Simplified Technical English (STE / ASD-STE100)**: Use one word per meaning, explicit sequential steps, max 3 nouns per cluster, and warnings before actions.

#### Step 4: Two-pass drafting & anti-AI-ism self-correction

Execute this mandatory two-pass procedure before finalizing output:

1. Draft the response in Pass 1 focusing on technical accuracy, structure, and domain content.
2. Run `analyze_prose.py` on the draft in Pass 2 and rewrite any flagged violations before producing final output:
   - Pipe inline or chat drafts to stdin (`python3 ./scripts/analyze_prose.py - <<'EOF' ... EOF`), or pass the file path when writing a file (`python3 ./scripts/analyze_prose.py <file>`).
   - Replace banned AI filler verbs/adverbs (`delve`, `leverage`, `seamlessly`) and vague praise adjectives (`robust`, `seamless`, `pivotal`) with concrete technical behaviors.
   - Replace archaic or formal words (`lieu`, `ergo`, `obviate`), light-verb nominalizations (`make a decision` $\rightarrow$ `decide`), and colloquial phrasal verbs (`spin up` $\rightarrow$ `start`) with plain verbs.
   - Break up 4+ consecutive noun clusters, and use one consistent action verb per concept across the document.
   - Keep sentence cognitive complexity low ($\text{PCC} \le 5$): avoid center-embedded clauses between subject and verb, long subject-verb distances ($\text{DD}_{\text{SV}} > 6$), pre-verbal delays ($> 8$ words), and stacked negations.
   - Preserve epistemic modality hedges (`may`, `might`, `could`) when stating engineering uncertainty or root-cause hypotheses.
   - Eliminate copula substitutions (`serves as`), dangling `-ing` commentary (`, highlighting...`), negative parallelism (`not only... but also`), and false ranges ([references/standards.md](references/standards.md#64-sentence-structure--tone-anti-ai-isms)).
   - Remove all em-dash (`—`) characters ([references/standards.md](references/standards.md#65-formatting-punctuation--integrity-anti-ai-isms)). Replace them with commas, parentheses, colons, or separate sentences.
   - Eliminate overuse of inline-header lists in both unordered (`- **Title**: body`) and numbered (`1. **Title**: body`) lists ([references/standards.md](references/standards.md#65-formatting-punctuation--integrity-anti-ai-isms)). Use plain prose paragraphs or unbolded list items unless defining a glossary or parameter list.
   - Run the **Fresh Reader Test** ([references/standards.md](references/standards.md#8-standalone-readability--meta-context-isolation)) to remove undefined meta-task labels, subagent IDs, turn references, and local paths.
3. Present only the polished, post-audit prose.

______________________________________________________________________

### Mode B: Reviewing & auditing existing prose

When the user asks to review, audit, or critique an existing document, PR description, or prompt:

1. Run the statistical and cognitive complexity analyzer on the target file or stdin (`-`):

   ```bash
   python3 ./scripts/analyze_prose.py <path-to-file | -> [--threshold 5] [--engine auto|stdlib|spacy]
   ```

2. Inspect runtime capabilities and output metrics:

   - Check `summary.capabilities` to see which NLP backend ran (`stdlib` vs `spacy`) and whether optional packages (`wordfreq`, `spacy`, `en_core_web_sm`) were missing.
   - When optional packages are missing, the script runs all first-party lexicon and stdlib syntactic checks and prints an `install_hint` command in `summary.capabilities`.
   - Review word count, sentence count, median sentence length, paragraph stats, `median_sentence_complexity`, and `max_sentence_complexity`.
   - Inspect all violation arrays under `violations`: `banned_ai_words_found`, `banned_ai_phrases_found`, `obscure_words_found`, `rare_domain_nouns_advisory`, `nominalizations_found`, `noun_clusters_found`, `phrasal_verbs_found`, `synonym_rotations_found`, `complex_sentences_found`, `em_dashes_found`, `smart_quotes_found`, `inline_header_list_overuse`, `sentences_exceeding_25_words`, and `paragraphs_exceeding_4_sentences`.

3. Output a clear feedback report table detailing findings and concrete fixes:

   | Line / Location | Issue / Violation                                     | Standard Violated         | Suggested Plain Language Fix                                   |
   | :-------------- | :---------------------------------------------------- | :------------------------ | :------------------------------------------------------------- |
   | Line 12         | *"serves as a robust framework"*                      | Anti-AI-ism / Copula Sub  | *"is a framework that retries HTTP requests"*                  |
   | Line 15         | *"in lieu of"* / *"obviate"*                          | Lexical Familiarity (STE) | Replace with *"instead of"* and *"prevent"*.                   |
   | Line 19         | Em-dash (`—`) clause splice                           | Anti-AI-ism / Punctuation | Replace `—` with a comma or split into two sentences.          |
   | Lines 22–28     | Inline-header list on all bullets                     | Anti-AI-ism / List Style  | Remove bold lead-in titles or rewrite as prose.                |
   | Line 34         | Sentence complexity $\text{PCC} = 8$ (center-embedded) | Syntactic Load (PCC > 5)  | Move the relative clause after the verb or split the sentence. |
