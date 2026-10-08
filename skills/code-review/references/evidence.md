# Evidence

Optimize for the survival rate of findings, not the count. A false finding costs the author more than a missed one: time spent disproving a wrong claim, or worse, breaking working code to satisfy a review comment. Fabricated citations cost the most because a quoted API doc or spec line reads as fact unless someone opens the file to check. A few findings that all hold up are better than many where only one does. A review that tests its hypotheses and finds zero defects is a successful review; never lower the evidence bar or manufacture minor objections to avoid returning an empty list.

## Tiers

Every finding states the evidence tier that backs it:

| Tier | Meaning | Allowed severities |
| :--- | :--- | :--- |
| `Executed` | You ran a command this session and are showing its real output (a failing test, a repro command, a mutation run, a benchmark, or a dry-run query). | `critical`, `high`, `medium`, `low` |
| `Read` | Literal text you opened from disk this session, cited by `path:line` and quoted. | `critical`, `high`, `medium`, `low` |
| `Fetched` | Literal text you retrieved over the network this session with `read_url_content`, cited by URL and quoted verbatim. | `critical`, `high`, `medium`, `low` |
| `Deduction` | A step-by-step logic, control-flow, or state trace through code read this session. Allowed at `critical` or `high` only when every step cites an exact `path:line`. | `critical`, `high` (with full line citations), `medium`, `low` |
| `Speculation` | Reasoning over incomplete information, an incomplete trace, or an unmeasured claim. | `medium`, `low`, or a Question (`Q1…`) |
| `Recalled` | "I remember this API / spec / tool working this way." | Never acceptable. Drop it, verify it on disk or over the network, or turn it into a Question. |

Two rules follow directly from the tiers and are broken most often:

- **Never quote documentation from memory.** If a finding depends on what an API, library, or spec guarantees, read the declaration or docstring on disk (including installed dependencies under `node_modules/`, `~/.pub-cache/`, `.venv/` / `site-packages/`, or vendor directories). When the source is not on disk, fetch the official reference page with `read_url_content` (at most 3 network fetches per reviewer) and quote what you saw. If you cannot reach the source, file a Question instead of a finding.
- **Measure performance claims.** Claims such as "this will be slow", "this allocates too much", or "this will time out in CI" require an `Executed` measurement or a bound derived from a cited line (`Read` / `Deduction`). An unmeasured performance hunch is `Speculation`: cap it at `medium`, or file a Question asking the author for the number.

## Severity Caps

Severity follows the concrete consequence in `Why`, and is capped by the evidence tier in `Evidence`:

| Severity | Consequence (`Why`) | Minimum evidence tier |
| :--- | :--- | :--- |
| `critical` | Ship this and production, data integrity, security, or interoperability breaks. | `Executed`, `Read`, `Fetched`, or a `Deduction` trace where every step cites `path:line`. |
| `high` | Real defect or regression gap with a concrete failure path and cost. | `Executed`, `Read`, `Fetched`, or a `Deduction` trace where every step cites `path:line`. |
| `medium` | Worth fixing; bounded impact or an unmeasured performance / edge-case concern. | Any tier except `Recalled` (`Speculation` caps here). |
| `low` | Minor, stylistic, or local clarity improvement. | Any tier except `Recalled`. |
| `Question` (`Q1…`) | You suspect a real issue and can name the exact check that settles it, but could not run or reach that check within budget. | `Speculation` or a partial `Deduction` (at most 3 Questions per review; never blocks the verdict). |

Question exists so a reviewer never has to inflate a hunch into a `high` or `critical` finding to make it visible.

## Hypotheses

Before reading the diff line by line, work from explicit, falsifiable hypotheses. The orchestrator seeds a few hypotheses in the prompt; add up to 5 of your own before investigating in detail.

Each hypothesis is a specific claim that a file read or command can confirm or disprove:

1. The removed explicit dependency is not replaced by an implicit one, so the tasks can run out of order.
2. The new test passes against a deliberately broken implementation.
3. The retry path can be entered twice concurrently and double-increments the counter.
4. The deleted test's coverage does not exist anywhere else in the tree.

Not a hypothesis: "the error handling could be better." Nothing falsifies that, so investigating it produces opinion rather than findings.

Report the outcome of every seeded and self-added hypothesis:

- **Confirmed** → a finding in `## Review Comments`.
- **Disproved** → a one-line entry in `## Checked and Found Clean`, naming the tier and the disproving evidence.
- **Unresolved** → a Question in `## Questions` (up to 3 total), naming the check that would settle it.
- If you skip a seeded hypothesis, state that you skipped it and why. Never drop an assigned hypothesis silently.

## Investigate the Tree, Not the Diff

The diff is a pointer to where to look, not the state of the codebase. Before filing any finding that depends on surrounding code, check the tree at `HEAD` (within a read budget of at most 25 files outside the diff chunks and the listed context files):

- **Removal claims**: A deletion hunk does not mean the code or coverage is gone; it may have moved. Search `HEAD` with `rg` before claiming a symbol, call, or test was removed.
- **API misuse claims**: Read the target function's signature, declaration, and docstring before claiming a caller passes the wrong type, order, or semantics.
- **Unused or missing argument claims**: Search call sites across the tree before recommending an assertion or change on a parameter or overload that callers may never pass.
- **"Test pins incidental behavior" claims**: Read the contract, spec, or docstring before asking to loosen an assertion. Often the behavior is contractual and the test is right.

## Falsification Pass

Before writing up any surviving finding, try to disprove it using the cheapest check available. Resolve these six questions for every candidate finding:

1. What is the cheapest file read or command that would prove me wrong? Run or read it.
2. Am I quoting or paraphrasing anything I did not read in this session? Remove it or read the source first.
3. Is the defect shown by a deterministic code path trace, or is it a hunch?
4. Would existing tests, the compiler, or the type checker already catch this if the code were broken?
5. Does the severity match the evidence tier I can supply?
6. If the author asks for proof, do I have a command output, an exact quote, or a line-by-line trace to show?

Four falsification techniques cover most code findings:

- **Mutation testing** (under `--verify` in your own reserved checkout; mental trace otherwise): when claiming a test is tautological or misses a bug in changed code, apply one small mutation (invert a condition, remove a guard, return a constant, or revert the fix hunk) and run the targeted test; see [verification.md](verification.md#mutation-testing). If the test fails, the behavior is already guarded—revert and drop the finding. If the test still passes, revert and report the gap at `Executed`.
- **Call-site audit**: search all invocations with `rg` across the repository to verify whether callers actually reach the path or pass the construct in practice.
- **Dependency contract inspection**: open the actual source definitions, headers, or type declarations of dependencies on disk (or fetch the official reference page) rather than assuming behavior from memory.
- **Execution-graph query**: for questions about build ordering, task dependencies, or lifecycle hooks, run the toolchain's dry-run or task-graph query (`--dry-run`, `make -n`, `bazel query`, `gradle tasks`) rather than guessing from config files. These read-only queries belong to tier 1 in [verification.md](verification.md#tiers).

Drop any candidate finding that fails the falsification pass, or downgrade it to match the surviving evidence.

## Anti-Patterns

Watch for and eliminate these eight failure modes during self-critique:

- **Fabricated citation**: quoting a docstring, comment, or specification from memory with plausible wording that does not exist in the file.
- **Diff-only reasoning**: concluding code or test coverage was removed from a deletion hunk without searching `HEAD`.
- **Phantom target**: recommending an assertion or check on a parameter, branch, or overload that callers never use.
- **Unmeasured performance**: claiming code is slow, leaky, or will time out in CI without a measurement or a line-cited bound.
- **Category error**: claiming something is "already covered" (or "not covered") by pointing to tests at a different layer than the unit under review.
- **Severity inflation**: filing a `critical` or `high` finding on inference or hunch because a Question or `medium` felt too easy to ignore.
- **Silent skip**: ignoring one of the prompt's seeded hypotheses without reporting it as confirmed, disproved, unresolved, or explicitly skipped.
- **Assertion-flipping**: telling the author "this test pins incidental behavior, loosen it" when the behavior is documented and contractual. When behavior is contractual, keep the strict assertion and suggest quoting the contract in a comment above it so future reviewers do not raise the same objection.

## Formats

Every finding in `## Review Comments` uses a `### <ID>. <short claim>` heading (`C1…`, `H1…`, `M1…`, `L1…` after synthesis; reviewers may use provisional IDs or severity labels before Step 5 numbers them) and these fields:

```markdown
### H1. <short claim>
- **File**: `path/to/file`
- **Line**: `<line number in the new file on a changed line>`
- **Severity**: `critical | high | medium | low`
- **Category**: `<document category>` (document files only)
- **Evidence**: `<Executed | Read | Fetched | Deduction | Speculation>` — <command and trimmed output, quoted text with path:line, or a line-cited trace>
- **Body**: <the defect>
- **Why**: <the concrete consequence if shipped as-is>
- **Suggestion**: <smallest compilable fix, replacement text, or specific question>
```

Every entry in `## Questions` (at most 3 per review) uses `### Q1. <claim phrased as a question>`:

```markdown
### Q1. <claim phrased as a question>
- **File**: `path/to/file`
- **Line**: `<line number in the new file>`
- **Evidence**: `<Speculation | Deduction>` — <what was checked and where the trace stopped>
- **Why**: <If true, what breaks and what severity (`critical | high | medium`) it would carry>
- **To settle**: <the exact file, symbol, or command that answers the question>
```

Every line in `## Checked and Found Clean` (at most 10 lines per review) pairs a disproved hypothesis with its tier and disproving evidence:

```markdown
- <hypothesis>: `<Executed | Read | Fetched | Deduction>` — <command output or path:line quote that disproved it>
```

Lines without an evidence tier and a specific disproof are removed during synthesis so the section stays a record of falsified hypotheses rather than general praise.
