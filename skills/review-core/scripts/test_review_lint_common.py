#!/usr/bin/env python3
"""Unit tests for review_lint_common.py."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_WRITE_PROSE_SCRIPTS = _SCRIPTS_DIR.parents[1] / "write-prose" / "scripts"
for _p in (str(_SCRIPTS_DIR), str(_WRITE_PROSE_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from analyze_prose import analyze_prose
import review_lint_common as rlc

CODE_PROFILE = rlc.ReportProfile(
    name="code",
    sections=(),
    findings_section="Review Comments (Ordered by Severity)",
    questions_section="Questions",
    required_fields=("File", "Line", "Severity", "Evidence", "Body", "Why"),
    optional_fields=("Category", "Focus", "Suggestion", "Details", "Repro test"),
    validators={"Focus": re.compile(r"^`[a-z0-9][a-z0-9-]*(?::[a-z0-9-]+)?`$")},
)

DESIGN_PROFILE = rlc.ReportProfile(
    name="design",
    sections=(
        "Summary",
        "Principle Scorecard",
        "View & Boundary Assessment",
        "Findings (Ordered by Severity)",
        "Open Design Questions",
        "Checked and Found Clean",
        "Recommended Next Steps",
    ),
    findings_section="Findings (Ordered by Severity)",
    questions_section="Open Design Questions",
    required_fields=("Target", "Door", "Severity", "Principle", "Evidence", "Body", "Why", "Suggestion"),
    optional_fields=("Focus", "Details"),
    validators={
        "Door": re.compile(r"^`?(?:one-way|two-way)`?$", re.IGNORECASE),
        "Principle": re.compile(
            r"^`?(?:problem-scope|module-depth|boundaries-state|tradeoffs-simplicity|"
            r"resilience-ops|evolution-fitness|ergonomics-coherence)`?$"
        ),
    },
)

CODE_FIXTURE = """\
## Summary
Checked two files.
Verdict: Minor Revisions Needed

## Review Comments (Ordered by Severity)

### H1. Missing null guard on lookup
- **File**: `pkg/store.py`
- **Line**: `42`
- **Severity**: `high`
- **Evidence**: `Read` — `pkg/store.py:42` dereferences `row[0]`
- **Body**: `get_item` indexes the query tuple without checking for `None`.
- **Why**: Missing keys raise `TypeError` instead of returning `None`.
- **Suggestion**:
```python
return row[0] if row is not None else None
```

## Questions

### Q1. Can callers pass an empty tenant id?
- **File**: `pkg/store.py`
- **Line**: `18`
- **Evidence**: `Speculation` — Checked local callers only.
- **Why**: Empty tenant ids share cache keys (`high` if reachable).
- **To settle**: Inspect `routes.py` input validation.

## Checked and Found Clean
- Cache eviction race: `Read` — `pkg/store.py:12` holds `self._lock`.
"""

DESIGN_FIXTURE = """\
## Summary
Reviewed the store spec.
Verdict: Ready

## Principle Scorecard
| Principle | Score | Assessment |
| :--- | :---: | :--- |

## View & Boundary Assessment
- Module view is acyclic.

## Findings (Ordered by Severity)

### M1. Shared cache key format
- **Target**: `pkg/keys.py:10`
- **Door**: `two-way`
- **Severity**: `medium`
- **Principle**: `module-depth`
- **Evidence**: `Read`: `pkg/keys.py:10` formats keys directly
- **Body**: Two modules format cache keys by hand.
- **Why**: Format drift causes cache misses.
- **Suggestion**: Move formatting into `format_key(tenant)`.

## Open Design Questions
None.

## Checked and Found Clean
- Import cycles: `Executed` — `prepare_design_context.py` found 0 cycles.

## Recommended Next Steps
1. Extract `format_key`.
"""


class ReviewLintCommonTests(unittest.TestCase):
    """Covers parsing, sequence, evidence, verdict, and prose boundary checks."""

    def test_strip_ticks_and_extract_tier(self) -> None:
        self.assertEqual(rlc.strip_ticks(" `high` "), "high")
        self.assertEqual(rlc.extract_tier("`Read` — foo.py:1"), "Read")
        self.assertEqual(rlc.extract_tier("Executed: pytest passed"), "Executed")
        self.assertEqual(rlc.extract_tier("Guesswork: none"), "")
        self.assertEqual(rlc.extract_evidence_detail("`Read`: line 10"), "line 10")

    def test_code_and_design_fixtures_pass(self) -> None:
        for text, profile in ((CODE_FIXTURE, CODE_PROFILE), (DESIGN_FIXTURE, DESIGN_PROFILE)):
            sections = rlc.parse_sections(text)
            findings = rlc.parse_items(sections[profile.findings_section], ("C", "H", "M", "L"))
            questions = rlc.parse_items(sections[profile.questions_section], ("Q",))
            clean = [(ln, s[2:].strip()) for ln, s in sections["Checked and Found Clean"] if s.strip().startswith("- ")]
            report = rlc.ParsedReport(sections=sections, findings=findings, questions=questions, clean_lines=clean)

            self.assertEqual(rlc.check_sections(report, profile), [])
            self.assertEqual(rlc.check_id_sequence(findings), [])
            self.assertEqual(rlc.check_id_sequence(questions, expected_prefix="Q"), [])
            self.assertEqual(rlc.check_severity_matches_id(findings), [])
            self.assertEqual(rlc.check_evidence_caps(findings, is_findings=True, profile=profile), [])
            self.assertEqual(rlc.check_required_fields(findings, profile), [])
            self.assertEqual(rlc.check_questions(questions), [])
            self.assertEqual(rlc.check_clean_lines(clean), [])
            self.assertEqual(rlc.check_single_fence_per_field(findings), [])
            self.assertEqual(rlc.check_verdict([s for _, s in sections["Summary"]], findings), [])
            self.assertEqual(rlc.check_prose(findings, profile, analyze_prose), [])

    def test_verdict_strings_and_contradiction(self) -> None:
        high = [rlc.Item("H1", "Blocker", {"Severity": "high"}, 10)]
        for verdict in rlc.VALID_VERDICTS:
            res = rlc.check_verdict([f"Verdict: {verdict}"], [] if verdict == "Ready" else high)
            self.assertEqual(res, [])
        bad = rlc.check_verdict(["Verdict: Ready"], high)
        self.assertEqual([v.rule for v in bad], ["verdict-contradicts-findings"])
        missing = rlc.check_verdict(["No verdict here."], [])
        self.assertEqual([v.rule for v in missing], ["missing-verdict"])

    def test_recalled_and_speculation_caps(self) -> None:
        items = [
            rlc.Item("H1", "One", {"Severity": "high", "Evidence": "`Speculation`: guess"}, 5),
            rlc.Item("M1", "Two", {"Severity": "medium", "Evidence": "`Recalled`: old"}, 12),
        ]
        rules = {v.rule for v in rlc.check_evidence_caps(items, is_findings=True, require_detail=True)}
        self.assertEqual(rules, {"severity-cap", "evidence-recalled"})

    def test_prose_120_words_6_sentences_boundary(self) -> None:
        sent = (
            "The cache returns stale rows after the second retry, and the eviction "
            "timer never resets on new writes from the worker."
        )  # 21 words
        five_sents = " ".join([sent] * 5)  # 105 words, 5 sentences -> passes 120/6
        six_sents = " ".join([sent] * 6)   # 126 words, 6 sentences -> fails 120 words
        item_ok = rlc.Item("M1", "Ok", {"Body": five_sents, "Why": "Breaks."}, 10)
        item_long = rlc.Item("M2", "Long", {"Body": six_sents, "Why": "Breaks."}, 20)
        self.assertEqual(rlc.check_prose([item_ok], CODE_PROFILE, analyze_prose), [])
        hits = rlc.check_prose([item_long], CODE_PROFILE, analyze_prose)
        self.assertIn("comment-prose-length", {v.rule for v in hits})

    def test_violation_aliases(self) -> None:
        v = rlc.Violation("some-rule", "Bad", line=14, fix_hint="Do X")
        self.assertEqual(v.lineno, 14)
        self.assertEqual(v.fix, "Do X")


if __name__ == "__main__":
    unittest.main()
