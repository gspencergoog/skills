---
name: cost-optimization
description: Framework for optimizing LLM inference costs and API spend per completed task across LLM platforms (Anthropic, OpenAI, Google Gemini/Vertex AI, AWS Bedrock, Azure, etc.). Use when asked to optimize AI spend, reduce token consumption, profile API usage, configure prompt caching, prune context windows, or tune model selection and reasoning effort.
---

# Cost Optimization - Cutting LLM Spend per Completed Task

API spend is optimized in units of **cost per completed task, not cost per token**. A model or reasoning setting with a higher sticker price per token can be the cheaper option if it finishes the job in fewer turns, and a cheaper model that fails still bills its tokens, then the retry, then whatever the failure costs downstream. Every judgment below evaluates cost and quality together.

The levers divide into two categories, and the order of execution is load-bearing:

- **Free wins** — prompt caching, input-token hygiene, loop hygiene, output-token hygiene, batch processing — lower what you pay without lowering output quality. They go first, and prompt caching should stay on permanently where available.
- **Tradeoffs** — reasoning effort, task budgets, model choice, multi-model architectures — exchange cost for intelligence. They go last, because each one alters model capability, and overshooting costs quality that free wins cannot recover.

---

## Step 0: Establish Scope, Quality Bar, and Baseline

Establish three items from the request, the codebase, and the user before making changes. Work interactively with the user when context or evaluation checks are missing.

1. **Scope and Target Platform**
   - Identify every location calling LLM APIs (request builders, agent loops, batch jobs).
   - Distinguish traffic classes (e.g., interactive real-time user calls vs. scheduled background jobs). Cost per task must be measured per traffic class.
   - Identify the target provider and platform (e.g., Anthropic API, OpenAI API, Google Gemini / Vertex AI, AWS Bedrock, Azure OpenAI, or self-hosted endpoints like vLLM). Feature availability (prompt caching, batch APIs, reasoning effort controls, usage endpoints) varies across providers.

2. **Quality Bar**
   - Locate the project's evaluation suite, test harness, or output validation checks.
   - If no eval exists, highlight this prominently: without one, cost savings cannot be distinguished from quality regressions.
   - Free wins remain safe to propose without an eval, but mark every tradeoff lever as "needs an eval before applying". The minimal eval recipe (§ Step 3) provides a path to unblock tradeoffs.

3. **Baseline Cost per Task**
   - Calculate baseline spend using actual logged token counts across all active token categories:
     - **Regular input tokens** (base rate)
     - **Cache creation / write tokens** (often billed at a premium rate, e.g., 1.25x–2x input)
     - **Cache read / hit tokens** (billed at a fraction of base input, e.g., 0.1x–0.5x input)
     - **Reasoning / thinking tokens** (billed at output rate)
     - **Output tokens** (base output rate)
   - Fetch live pricing rates from provider documentation before sizing expected savings.
   - If historical API metrics are unavailable, run a representative sample of requests through the project's test suite to measure baseline token consumption. Request user approval before running paid test passes.

---

## Step 1: Profile Where the Tokens Go

The token profile can be measured directly via provider telemetry or estimated from codebase inspection.

### A. Measure Telemetry (Preferred)

Use provider usage APIs or application usage logs to gather actual token distributions:
- **Provider Admin / Usage APIs**: Query usage reporting endpoints (e.g., Anthropic Usage API, OpenAI Usage API, GCP Billing/Metrics, AWS Cost Explorer) grouped by model, key, or workspace.
- **Application Logs**: If the application logs `response.usage` objects per request, sum tokens across calls per completed task.
- Key metrics to measure:
  - Cache hit rate (`cached_input_tokens` vs `uncached_input_tokens`)
  - Reasoning token overhead (`reasoning_tokens` / `thinking_tokens` vs total output)
  - Input/Output ratio per task
  - Spend concentration by model and endpoint

### B. Estimate from Codebase Inspection

If telemetry is unavailable, analyze request builders and loop structures:
- **Prefix**: Size of system prompts, static instruction headers, and interpolated dynamic variables (timestamps, UUIDs).
- **Reference Material**: Inlined documentation, guidelines, or reference manuals attached to every prompt.
- **Tool Schemas**: Total token cost of tool definitions and whether all schemas are sent on every turn.
- **Agent Loop Depth**: Average turns per task and whether bulky tool responses accumulate over time.
- **Media Assets**: Resolution and patch tokenization of images, PDFs, or audio inputs.
- **Output Controls**: Configured `max_tokens` / `max_completion_tokens` limits and actual output length.
- **Model and Reasoning Effort**: Active model string, reasoning effort level (`low`, `medium`, `high`, or token budget), and default behavior when omitted.
- **Caching Setup**: Existing prompt cache markers or context caching parameters.
- **Latency Tolerance**: Real-time user blocking vs. asynchronous batch job candidates.

### C. Rank the Levers

Size each applicable lever based on spend profile and express savings appropriately:
- **Provider API Data**: Quote savings in actual dollar ranges (labeled `measured`).
- **Application Usage Logs**: Quote savings as `% of bill` with estimated dollar savings.
- **Code Inspection Only**: Quote savings in relative bands (`high`, `medium`, `low` impact).

---

## Step 2: Work the Levers in Order

Apply free wins first (Sections 2.1–2.5). Apply tradeoff levers (Sections 2.6–2.7) only with an evaluation check and explicit approval.

### 2.1 Prompt Caching — First and Permanent

In multi-turn agent loops, resending growing conversation context causes input token count to grow quadratically ($O(N^2)$ with turn count). Prompt caching reprices cached prefix tokens to a fraction of the base input rate.

- **Prefix Matching Invariant**: Caching requires byte-identical prefix matching. Keep static system prompts, guidelines, and tool definitions at the very beginning of the prompt.
- **Explicit vs. Automatic Breakpoints**: Place explicit cache breakpoints after large static prefixes (system instructions, tool definitions, reference material). Use automatic caching where platforms support top-level message caching.
- **Cache TTL Selection**: Use standard TTLs (e.g., 5-minute) for fast sequential turns. Use extended TTLs (e.g., 1-hour or explicit context caching) when loops wait on human input or batch queues.
- **Eliminate Mid-Task Cache Breakers**:
  - Avoid placing dynamic timestamps, random IDs, or changing session state above cache breakpoints.
  - Avoid altering reasoning effort, tool definitions, or system prompts mid-conversation.
  - Avoid switching model strings within a single multi-turn session.
- **Preserved Reasoning & History Integrity**: On models with strict message validation or reasoning trace verification, modifying historical `system` prompts, `tools`, or prior assistant messages breaks both prompt caching and internal reasoning state. Keep system prompts and tool schemas fixed for the session, and treat message histories as append-only.
- **Verification**: Inspect response usage metadata to confirm `cached_input_tokens` / `cache_read_tokens` dominate input token spend on turn 2 and beyond.

### 2.2 Input Tokens — Progressive Disclosure

Send only the context required for the immediate step; retrieve the rest on demand.

- **Large Reference Documents**: Replace full inlined documentation with RAG or on-demand retrieval tools/skills so the model fetches specific sections as needed.
- **Tool Description Hygiene**: Remove redundant tool usage descriptions from system prompts if tool schemas already describe parameters cleanly.
- **Tool Schema Pruning & Lazy Loading**: Use tool search or lazy schema loading (`defer_loading`) when tool schemas exceed ~10K tokens (e.g., extensive MCP toolsets).
- **Vision Asset Downscaling**: Pre-downscale images and PDFs before sending. Image tokenization scales with pixel resolution (e.g., patch-based tokenization); cap standard visual assets (e.g., ~1280x720) unless fine details are required.
- **Sandboxed Execution for Large Files/Tables**: Mount raw CSVs, large JSON files, or PDFs into a code execution environment or Files API. Let the model write code to extract answers rather than inlining large raw data into context.
- **Filtered Web Content**: Strip HTML boilerplate, navigation menus, and scripts from web fetch tools before context insertion.
- **Narrow Tool Accessors**: Prefer specific parameter queries (`get_item(id)`) over bulk dumps (`get_all_items()`), and add `limit`, `fields`, and `date_range` bounds.
- **Ingestion Gates**: Count tokens on user-supplied inputs prior to API requests; truncate or route oversized payloads to summarization pipelines.
- **Prompt Auditing**: Audit prompt strings for dated, redundant, or verbose instructions.

### 2.3 Agent-Loop Hygiene — Controlling Context Growth

For deep agentic loops, prevent intermediate tool outputs from accumulating indefinitely.

- **Server-Side Context Editing / Trimming**: Periodically clear or trim old tool outputs and intermediate turns once their phase completes. Perform clears in larger batches rather than every turn to preserve prompt caching benefits.
- **Compaction & Summarization**: Trigger server-side or programmatic conversation summarization when context approaches window limits, replacing long histories with a concise state summary.
- **Client-Side Pruning Safeguards**: On models that enforce strict reasoning trace validation, client-side history edits can invalidate historical reasoning blocks. In such environments, prefer server-side context compaction or append-only summary messages.
- **Subagents for Heavy Sub-tasks**: Delegate self-contained, context-heavy sub-tasks to nested subagent calls. The subagent processes bulky intermediate outputs in its own isolated context window and returns a concise final result to the parent loop.

### 2.4 Output Tokens — Controlling Response Generation

Output tokens carry higher per-token rates than input tokens.

- **`max_tokens` / `max_completion_tokens` is a Safety Backstop**: Treat max token limits as truncation safeguards, not output length controllers. Setting tight limits risks truncation mid-thought (`stop_reason: max_tokens`), leading to failed attempts and retries. Set generous upper bounds for agentic tasks.
- **Prompting Output Format**: Shorten visible output by defining explicit response structures (e.g., concise JSON, bullet summaries, or brief templates) with concrete examples.
- **Reasoning Effort Control**: Control reasoning/thinking output length using provider-supported reasoning effort settings (`effort`, `reasoning_effort`, or explicit `budget_tokens`) rather than toggling reasoning off completely.
- **Early Exit Sentinels**: Define custom stop sequences or early termination tokens (e.g., `<TERMINATE>`) for unrecoverable errors or completed tasks to stop generation immediately.

### 2.5 Batch Processing — Asynchronous Workloads

Use Provider Batch APIs for non-realtime work (evaluation suites, offline analytical jobs, content backfills, scheduled tasks).

- **50% Discount**: Batch APIs typically grant a ~50% price reduction across all token types (input, cached input, and output).
- **Single-Turn vs. Flattened Loops**: Batch requests run asynchronously. Where feasible, pre-fetch tool inputs in code to flatten multi-turn loops into single batchable requests.

### 2.6 Effort and Budgets — Trading Capability for Cost

Adjust reasoning depth and task budgets based on empirical evaluation.

- **Reasoning Effort Sweeps**: Sweep reasoning effort levels (`low`, `medium`, `high`, or token budgets) against your evaluation suite:
  - *Knowledge / QA tasks*: Lower effort settings often maintain accuracy while reducing token spend by 30%–50%.
  - *Complex coding / Multi-step logic*: Higher effort settings buy pass-rate improvements on hard tail cases, but show diminishing returns at maximum levels.
- **Re-run Failures at Higher Effort**: Execute initial runs at a cheap, low-effort setting; re-run only failed or unverified attempts at higher effort.
- **Time Awareness**: Pass wall-clock time or elapsed turn information into the prompt so the model avoids repeating ineffective approaches.
- **Task Token Budgets**: Apply provider-supported task token budgets to steer reasoning pacing without hard-cutting generation.

### 2.7 Model Selection — Platform & Tier Optimization

Select models based on **cost per completed task**, not token sticker price.

- **Modern Tier Upgrades**: Upgrading to a newer model generation (even within a lower tier) often yields better quality at lower cost than running an older flagship model.
- **Evaluate Hard Tail Cases**: Benchmark candidates on the hardest 10% of workload tasks. A cheaper model that fails hard tasks incurs retry spend that negates its per-token savings.
- **Model Tier Step-Down Sweeps**: If a task passes evaluations easily at `low` reasoning effort, test stepping down one model tier while resetting effort to default.
- **Multi-Model Architectures**:
  - **Advisor Pattern**: A cheaper driver model executes the primary loop and consults a frontier advisor model on complex decision points.
  - **Orchestrator Pattern**: A frontier planner model decomposes a large project into independent tasks and delegates execution to cheaper worker models.

---

## Step 3: Apply, Measure, Keep or Revert

1. **One Lever Per Diff**: Apply shortlisted levers one at a time. Run the evaluation suite to measure pass rate and cost per completed task against baseline. Revert changes that reduce pass rate.
2. **Approved Measurement Budget**: Establish an explicit evaluation budget for running benchmark passes before executing sweeps.
3. **Warm-Cache Evaluation**: Ensure prompt cache measurements run with a warm cache (run evaluation inputs sequentially or pre-warm static prefixes).
4. **Minimal Eval Recipe**: If no evaluation suite exists, build a minimal check before applying tradeoff levers:
   - **Fixed Input Dataset**: Select ~20–30 representative real-world task prompts.
   - **Evaluation Metric**: Define clear validation criteria (automated test runner, schema validator, golden output diff, or rubric).
   - **Execution Script**: Run the test set through candidate configurations, capturing token usage and pass rates.

---

## Workload Shape to Lever Mapping

| Spend Concentration | Recommended Levers | Watch-Outs / Prerequisites |
|---|---|---|
| Large static system prompt & tool schemas on every turn | **Prompt Caching** (place static content at prompt start) | Dynamic content (timestamps, random IDs) placed above cache breakpoints invalidates the cache. |
| Large reference documentation attached to prompts | **Progressive Disclosure** (RAG / retrieval tools) | Ensure retrieval tool doesn't add more turns than inlining saved. |
| Heavy tool schemas (>10K tokens) | **Tool Search / Lazy Schema Loading** | Overhead of tool search step may exceed savings if schemas are small. |
| High-resolution images or PDF attachments | **Vision Downscaling / Files API Code Execution** | Avoid over-compressing fine text or diagram details required for accuracy. |
| Deep multi-turn agent loops accumulating output | **Context Editing / Compaction / Subagents** | Client-side pruning can invalidate reasoning history on strict models. |
| Verbose or unstructured text output | **Structured Output Templates / Response Formatting** | Avoid overly restrictive caps that truncate necessary responses. |
| High reasoning token spend on routine tasks | **Reasoning Effort Sweeps (`low` / `medium`)** | Validate pass rate on hard tail cases before lowering effort. |
| Non-realtime / scheduled / batch processing | **Batch APIs (~50% discount)** | Asynchronous execution only; single-turn or flattened loop structure. |
| High cost on standard tasks | **Model Tier Step-Down / Multi-Model Patterns** | Always sweep effort on current model before switching model tiers. |

---

## Step 4: Deliverables

1. **Cost Profile & Plan**: Document scope, quality bar, baseline cost per task, token distribution profile, and ranked shortlist of recommended levers.
2. **Execution Diffs**: Provide atomic, one-lever-per-diff changes with validation measurements (baseline vs. new cost per task and pass rate).
3. **Skipped Levers & Justification**: List levers evaluated but skipped (due to low potential impact, platform limitations, or quality tradeoffs).
