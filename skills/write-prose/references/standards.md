# Foundational writing standards (`standards.md`)

This document synthesizes international writing standards (ISO 24495-1:2023, W3C Cognitive Accessibility, Plain Writing Act, Simplified Technical English) and natural writing controls to eliminate "AI-isms" and structural fluff.

______________________________________________________________________

## 1. Quantitative targets & measurement limits

When writing or auditing text, aim for these quantitative thresholds:

| Metric                            | Target / Limit                                    | Rule                                                                                                                              |
| :-------------------------------- | :------------------------------------------------ | :-------------------------------------------------------------------------------------------------------------------------------- |
| **Sentence Length (Procedural)**  | $\le \mathbf{20\text{ words}}$                    | Procedural steps must be short and direct (1 action per step).                                                                    |
| **Sentence Length (Descriptive)** | $\le \mathbf{25\text{ words}}$ (15–20 average)    | Flag and split any sentence over 25 words.                                                                                        |
| **Paragraph Length**              | $\le \mathbf{4\text{ sentences}}$ (40–80 words)   | Keep paragraphs focused on a single concept.                                                                                      |
| **Word Length**                   | $\le \mathbf{5\text{ characters}}$ median         | Prefer simple words (*use* vs *utilize*, *check* vs *investigate*).                                                               |
| **Noun Clusters**                 | $\le \mathbf{3\text{ consecutive nouns}}$         | Avoid heavy noun stacks (e.g., write *"system for monitoring engine temperature"*, not *"engine temperature monitoring system"*). |

Run `python3 scripts/analyze_prose.py <path-to-file>` to measure these stats automatically.

______________________________________________________________________

## 2. Technical markup & Markdown link rules

To ensure technical clarity and prevent UI rendering breakage:

- Enclose variable names, function names, CLI flags, and file paths in backticks: `` `my_function()` ``, `` `--verbose` ``, `` `src/main.ts` ``.

- Always specify a language specifier for code blocks (`python`, `bash`, `json`, `markdown`).

- **NEVER** wrap Markdown link text in backticks. Wrapping link text in backticks breaks clickable formatting in IDEs and web views:

  ❌ **Incorrect**:

  ```markdown
  [`main.ts`](src/main.ts)
  [`MyClass`](src/main.ts#L10)
  ```

  ✅ **Correct**:

  ```markdown
  [main.ts](src/main.ts)
  [MyClass](src/main.ts#L10)
  ```

______________________________________________________________________

## 3. ISO 24495-1:2023 (Plain language principles)

Content must satisfy four core principles:

1. **Relevant**: Include only information the reader needs to achieve their goal. Eliminate filler and background tangents.
2. **Findable**: Structure documents with descriptive, sentence-case headings (`## Header`), logical ordering, and lists where appropriate so information is scannable.
3. **Understandable**: Use familiar words, short sentences (15–20 words on average), and simple sentence structures.
4. **Usable**: Provide clear, actionable instructions that enable the reader to perform their task without re-reading.

______________________________________________________________________

## 4. W3C Cognitive Accessibility Guidance (COGA)

To support cognitive accessibility and reduce mental load:

- Avoid metaphors, idioms, hyperbole, and abstract expressions; use clear, literal language.
- Limit paragraphs to 2–4 sentences focusing on a single idea.
- Present multi-step instructions as numbered lists (`1.`, `2.`, `3.`) with one action per step.
- Put the most important action or decision first to keep critical paths short.
- Do not expect the reader to remember details from earlier sections; repeat critical parameters or link directly.

______________________________________________________________________

## 5. Plain Writing Act & STE standards

- Make the actor the subject of the sentence using active voice (*"The server rejected the request"* instead of *"The request was rejected by the server"*).
- Use second-person pronouns ("you", "your") for user-facing instructions and guides.
- Keep subjects and their main verbs close together.
- Do not turn verbs into heavy nominalizations (use *"decide"* instead of *"make a decision"*).
- State prerequisites, warnings, or conditions *before* the action verb.

______________________________________________________________________

## 6. Natural writing & anti-AI-ism controls

### 6.1 Positive replacement pairs (banned words mappings)

Do NOT use high-probability AI filler words. Replace them using this direct mapping table during **Pass 2** self-correction:

| Banned Word / Phrase                                            | Recommended Direct Alternative(s)                                         |
| :-------------------------------------------------------------- | :------------------------------------------------------------------------ |
| **leverage / utilize / harness**                                | *use*, *apply*, *build on*                                                |
| **delve / delve into**                                          | *examine*, *study*, *check*, *look at*                                    |
| **foster / cultivate / empower / unleash**                      | *support*, *encourage*, *build*, *develop*, *enable*                      |
| **maximize / streamline / elevate**                             | State the specific change: *increase*, *simplify*, *reduce steps*         |
| **democratize**                                                 | *make available to*, *open to*                                            |
| **underscore / highlight (as verb)**                            | *show*, *point out*, *demonstrate*                                        |
| **ensure**                                                      | *verify*, *check*, *make sure*, or state the mechanism (*throws if null*) |
| **align with / resonate with**                                  | *match*, *follow*, *fit*, *agree with*                                    |
| **encompass / bridge**                                          | *include*, *cover*, *connect*                                             |
| **seamless / seamlessly / frictionless**                        | *without extra setup*, *automatically*, *directly*                        |
| **extensively**                                                 | State exact scope: *across 12 modules*, *in all unit tests*               |
| **robust**                                                      | State exact behavior: *handles errors*, *retries up to 3 times*           |
| **intuitive**                                                   | State exact design: *uses standard defaults*, *requires no configuration* |
| **pivotal / crucial / unwavering / indelible**                  | State facts directly: *important*, *key*, or state why it matters         |
| **holistic / comprehensive / overarching / multifaceted**       | *complete*, *full*, or list the specific parts                            |
| **scalable / synergistic / transformative / dynamic**           | State exact mechanism or omit vague praise                                |
| **vibrant / intricate / nuanced / uncharted / breathtaking**    | Use concrete, literal descriptions or omit                                |
| **nestled / boasts / features (as verb) / premier / leading**   | *is in*, *has*, *includes* (maintain neutral, non-promotional tone)       |
| **state-of-the-art / rapidly evolving**                         | State the specific version, date, or capability                           |
| **tapestry / landscape / realm / hub / ecosystem (abstract)**   | *context*, *area*, *environment*, *system*                                |
| **interplay / synergy / cornerstone**                           | *interaction*, *combination*, *foundation*, *core part*                   |
| **testament to / serves as / stands as / acts as**              | *is*, *shows*, *demonstrates*                                             |
| **in order to**                                                 | *to*                                                                      |
| **at this point in time**                                       | *now*, *currently*                                                        |

______________________________________________________________________

### 6.2 Side-by-side contrastive examples

Use these contrastive pairs to calibrate self-correction during drafting:

| ❌ Machine-Generated Fluff (AI-ism)                                                 | ✅ Human Plain Language                                                              |
| :---------------------------------------------------------------------------------- | :----------------------------------------------------------------------------------- |
| *"The library serves as a robust framework for leveraging telemetry data."*         | *"The library collects telemetry data and retries on failure."*                      |
| *"This pull request marks a pivotal moment in streamlining database queries..."*    | *"This pull request reduces database query latency by indexing the user ID column."* |
| *"The team continues to foster innovation while seamlessly navigating challenges."* | *"The team resolved 5 open issues and added unit test coverage."*                    |
| *"Nestled in the architecture, this module acts as a cornerstone..."*               | *"This module handles authentication."*                                              |
| *"It is not only a caching layer, but also a security boundary."*                   | *"It caches data and enforces access controls."*                                     |
| *"Despite initial hurdles, the pipeline continues to thrive..."*                    | *"The build pipeline passed all checks."*                                            |
| *"Certainly! I would be happy to delve into this robust solution for you."*         | *"Here is the design breakdown."*                                                    |

______________________________________________________________________

### 6.3 Converting vague adjectives into concrete technical behaviors

Never use vague praise adjectives (*robust, seamless, intuitive, frictionless, scalable*). Replace them with the actual technical mechanism:

- ❌ *"Implements robust error handling."*
  $\rightarrow$ ✅ *"Catches `NetworkException` and retries failed requests up to 3 times before timing out."*
- ❌ *"Provides a seamless user experience."*
  $\rightarrow$ ✅ *"Saves user settings automatically without requiring a manual save button."*
- ❌ *"Highly scalable architecture."*
  $\rightarrow$ ✅ *"Distributes requests across worker isolates using a round-robin pool."*

______________________________________________________________________

### 6.4 Sentence structure & tone anti-AI-isms

#### Avoid copula substitutions & promotional language

Do not replace simple `is`, `are`, or `has` verbs with flowery or promotional equivalents such as *serves as, stands as, acts as, boasts, features* (as a verb), or *offers*. Avoid advertisement words like *premier, leading, state-of-the-art, committed to,* and *dedicated to*.

- ❌ *"The service stands as a premier solution and boasts sub-millisecond reads."*
  $\rightarrow$ ✅ *"The service is a key-value store with sub-millisecond reads."*

#### No superficial analysis (dangling `-ing` participle clauses)

Avoid attaching trailing present-participle clauses starting with *highlighting, emphasizing, reflecting, showcasing, ensuring,* or *demonstrating* that merely restate the obvious or add vague commentary.

- ❌ *"The client caches tokens locally, reflecting a modular design and ensuring optimal performance."*
  $\rightarrow$ ✅ *"The client caches tokens locally to reduce network requests."*

#### No puffery or forced significance

Do not inflate the importance of a change or topic with phrases like *"serves as a testament to," "marking a pivotal moment," "underscoring the importance of," "leaving an indelible mark,"* or *"shaping the landscape."* If something matters, state the concrete facts that show why.

#### No negative parallelism

Avoid sentences that construct an artificial contrast:

- ❌ *"It is not only a parser, but also a validator."*
- ❌ *"It is not just about speed; it is about reliability."*
- ✅ *"It parses and validates input payloads."*

#### No "rule of three" padding

Avoid listing three adjectives or three noun phrases simply to sound thorough (e.g., *"fast, modular, and extensible"* or *"marketers, engineers, and designers"* unless those exact three groups are the only ones).

#### No false ranges (`from X to Y`)

Do not use *"from X to Y"* unless X and Y are endpoints of a logical, measurable scale such as time, size, or numeric range.

- ❌ *"The guide covers everything from authentication to database sharding."*
- ✅ *"The guide covers topics including authentication and database sharding."*

#### Eliminate "elegant variation" & weasel attribution

Do not cycle through coined synonyms just to avoid repeating a subject's name; repeat the exact noun or use a pronoun naturally. Avoid vague attribution (*"Experts argue," "Observers note," "Several sources indicate"*) unless you cite specific sources immediately.

#### No "challenges and future outlook" conclusions

Do not end a document or section with a formulaic summary paragraph starting with *"Despite [X], [Subject] continues to..."*, *"Looking ahead..."*, or *"Overall, this change ensures..."*. End with the last factual point.

______________________________________________________________________

### 6.5 Formatting, punctuation & integrity anti-AI-isms

#### Avoid em dashes (`—`)

Do not use em-dash characters (`—`) or double-hyphen stand-ins (`--`) to splice clauses, insert parenthetical remarks, or add dramatic emphasis. Language models heavily overuse em dashes as an all-purpose punctuation crutch. Use commas, parentheses, colons, or split the thought into separate sentences instead.

- ❌ *"The cache stores tokens in memory—reducing database load during peak traffic."*
  $\rightarrow$ ✅ *"The cache stores tokens in memory, which reduces database load during peak traffic."*
- ❌ *"Three components—the parser, the validator, and the serializer—handle the payload."*
  $\rightarrow$ ✅ *"Three components (the parser, the validator, and the serializer) handle the payload."*

#### Avoid overusing inline-header lists

Do not reflexively format bulleted lists as inline-header lists (`- **Title**: body`, `- **Title** — body`, or `- **Statement.** Explanation`). Not every bullet item has to begin with a bold statement. Starting every bullet with bold text is one of the most recognizable structural habits of LLM output; when every item is bolded, nothing stands out.

- Prefer regular prose paragraphs when sentences build on one another.
- When a bulleted list is appropriate, write plain, unbolded bullet items that start directly with the verb or subject.
- Reserve inline-header lists strictly for genuine key-value reference material (such as named API parameters, CLI flags, configuration keys, or glossary terms) where readers scan for a specific identifier.

❌ **Overused inline-header list**:

```markdown
- **Connection pooling**: Reuses open sockets to avoid TCP handshake overhead.
- **Automatic retries**: Retries failed HTTP 503 requests up to three times.
- **Timeout enforcement**: Cancels requests that take longer than 5 seconds.
```

✅ **Natural unbolded list**:

```markdown
- Reuses open sockets to avoid TCP handshake overhead.
- Retries failed HTTP 503 requests up to three times.
- Cancels requests that take longer than 5 seconds.
```

#### Headings, bolding, tables & quotes

- Use sentence case for headings (e.g., `## Configuring automatic retries`, not `## Configuring Automatic Retries`). Do not use Title Case in headings.
- Avoid excessive bolding of keywords or "key takeaways" inside body paragraphs.
- Avoid creating tables for simple information that fits in a single sentence, and do not add decorative emojis (`🚀`, `🧠`) in prose or lists.
- Use straight quotes (`"`, `'`) and straight apostrophes (`'`). Do not use curly or smart quotes (`“`, `’`).
- Never invent citations, URLs, or DOIs. In chat or review responses, omit collaborative opening filler (*"Certainly!"*, *"Here is the information,"* *"I hope this helps"*), AI self-apologies, or `Subject:` lines.

______________________________________________________________________

## 7. Prompt design standards for LLM audiences

When writing system prompts, agent skills, tool guidelines, or automated prompts:

1. **Token conservation principles**:

   - Every token consumed in a prompt reduces attention efficiency and limits remaining history/code space.
   - Omit polite and conversational filler like *"Please make sure to..."*, *"It would be appreciated if you..."*, or *"As an AI assistant, you should..."*. Use direct imperative verbs (*"Extract parameters..."*, *"Return JSON format..."*).
   - Use brief XML tags (`<instructions>`, `<constraints>`) or Markdown bullet points instead of prose paragraphs to delineate sections.
   - Where supported (such as agent skills), use progressive disclosure by separating core instructions from deep reference files (`references/`). For standalone prompts where multi-file loading is not feasible, keep the primary directive concise at the top and relegate large reference data or schemas to clearly delimited sections at the end of the prompt.

2. **Assume high baseline knowledge**:

   - Models already have pre-trained knowledge about ASTs, REST APIs, Conventional Commits, or ISO standards. Name the concept directly without writing explanatory paragraphs.
   - Elaborate only when defining a non-standard rule, custom domain schema, or project-specific edge case.

3. **Semantic delimiters**:

   - Use XML tags (`<instructions>`, `<constraints>`, `<context>`, `<examples>`) or Markdown headers (`#`) to bound prompt components so the model distinguishes instructions from user input.

4. **Primacy & recency placement**:

   - Place role definition, primary objective, and core callouts at the **very top** (`> [!CAUTION]`).
   - Place critical output formatting constraints at the **very bottom** (immediately before the response threshold).

5. **Positive action directives**:

   - Frame instructions as direct, positive actions (*"Format the output as a 3-column table with headers X, Y, Z"*) rather than vague negative prohibitions (*"Don't generate bad tables"*).

6. **Few-shot concrete examples**:

   - Provide 1–2 exact `Input -> Output` pairs for complex output formats. Examples communicate format requirements more effectively than token-heavy prose explanations.

______________________________________________________________________

## 8. Standalone readability & meta-context isolation

Prose created by an AI agent must be standalone and free from meta-instruction contamination. Readers do not have access to the agent's context window, prompt history, external task plans, or internal execution logs.

### 8.1 Core rules

1. Do not leak instruction meta-concepts, planning tiers, phase numbers, prompt milestone names, or task division labels into the target prose. State the technical behavior or feature directly without referencing external planning structures (unless those structures are formally defined within the document itself).
2. Supply essential background so the document is understandable on its own. Never refer to "earlier in our chat", "the plan discussed previously", or "as requested in previous turns".
3. Remove all subagent IDs (`task-123`), agent tool names, step numbers, and internal agent orchestration artifacts.
4. Always use clean, relative paths (`src/utils/file.ts`) instead of local absolute system paths (`/Users/.../src/utils/file.ts`).
5. Replace vague pointers (*"this issue"*, *"the bug mentioned earlier"*) with concrete named entities (*"the race condition in `AuthService.login()`"*).

### 8.2 The fresh reader audit checklist

When writing or reviewing prose, execute this 3-question audit:

1. **The Origin Check**: *Did this term, phase label, or classification originate from the user's prompt / execution plan or is it an established concept in the domain / target document?*
   - If from an external prompt or plan $\rightarrow$ **Strip the meta-label or define it in-line.**
2. **The Standalone Check**: *Can a reader with no knowledge of the chat, task prompt, or repository history understand every paragraph without asking "What does X refer to?"*
   - If not $\rightarrow$ **Add explicit context or replace vague pronouns/terms.**
3. **The Hygiene Check**: *Are there any absolute system paths (`/Users/...`), subagent IDs (`task-xyz`), or transcript turn references (`in turn 2`)?*
   - If present $\rightarrow$ **Sanitize to relative paths / domain facts.**

### 8.3 Anti-pattern context leaks vs. standalone plain language fixes

| ❌ Conceptual / Context Leak (AI Artifact)                            | ✅ Standalone Plain Language Fix                                         | Rationale / Failure Mode                                                                    |
| :-------------------------------------------------------------------- | :----------------------------------------------------------------------- | :------------------------------------------------------------------------------------------ |
| *"Under Tier 1 of the spec update plan, we add rate limiting..."*     | *"Adds client-side rate limiting..."*                                    | **Conceptual Frame Leakage**: Plan tier label is external to the spec.                      |
| *"As part of Phase 2, the client now retries on HTTP 503."*           | *"The HTTP client retries failed requests on HTTP 503."*                 | **Meta-Plan Contamination**: Phase identifier is unknown to external readers.               |
| *"Subagent task-402 confirmed that the refactoring works."*           | *"Unit and integration tests confirmed that the refactoring works."*     | **Ephemeral Orchestration Leak**: Remove internal subagent task details.                    |
| *"Fixed lint errors in `/Users/gspencer/code/app/lib/main.dart`."*    | *"Fixed lint errors in `lib/main.dart`."*                                | **Path Leakage**: Sanitize local absolute system paths.                                     |
| *"As we discussed in turn 3 of our conversation, we chose Option B."* | *"We selected Option B because it avoids breaking API changes."*         | **Chat Transcript Reference**: Replace turn references with standalone technical rationale. |
| *"This change addresses the problem."*                                | *"This change resolves the database query timeout during peak traffic."* | **Referential Ambiguity**: Make pronouns explicit for external readers.                     |
