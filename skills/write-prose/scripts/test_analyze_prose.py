"""Unit tests for analyze_prose.py and prose_lexicon.py."""

import io
import pathlib
import tempfile
import unittest
from typing import Any
from unittest import mock

import prose_lexicon
from analyze_prose import (
    INSTALL_HINT_MESSAGE,
    _load_spacy_model,
    _parse_cli_args,
    _probe_nlp_backends,
    _try_import_optional,
    analyze_prose,
    score_sentence_complexity,
)


class _FakeSpacyToken:
    """Minimal stand-in for a spaCy Token in unit tests."""

    def __init__(
        self,
        text: str,
        i: int,
        pos_: str = "NOUN",
        dep_: str = "dep",
        head_i: int = 0,
        left_indices: list[int] | None = None,
        ancestor_indices: list[int] | None = None,
    ) -> None:
        self.text = text
        self.i = i
        self.pos_ = pos_
        self.dep_ = dep_
        self._head_i = head_i
        self._left_indices = left_indices or []
        self._ancestor_indices = ancestor_indices or []
        self.is_alpha = text.isalpha()
        self.is_punct = not self.is_alpha
        self.is_space = False
        self._doc: list[_FakeSpacyToken] = []

    @property
    def head(self) -> "_FakeSpacyToken":
        return self._doc[self._head_i]

    @property
    def lefts(self) -> list["_FakeSpacyToken"]:
        return [self._doc[idx] for idx in self._left_indices]

    @property
    def ancestors(self) -> list["_FakeSpacyToken"]:
        return [self._doc[idx] for idx in self._ancestor_indices]


class AnalyzeProseTests(unittest.TestCase):
    def _run_on_text(
        self,
        content: str,
        complexity_threshold: int = 5,
        engine: str = "stdlib",
    ) -> dict:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(content)
            tmp_path = pathlib.Path(tmp.name)
        try:
            return analyze_prose(
                str(tmp_path),
                complexity_threshold=complexity_threshold,
                engine=engine,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_clean_prose_has_no_violations(self):
        report = self._run_on_text(
            "## Summary\n\n"
            "The client retries failed HTTP 503 requests up to three times.\n\n"
            "- Reuses open sockets across requests.\n"
            "- Cancels requests that take longer than five seconds.\n"
            "- Logs timeout errors with the request ID.\n"
        )
        v = report["violations"]
        self.assertEqual(v["banned_ai_words_found"], [])
        self.assertEqual(v["banned_ai_phrases_found"], [])
        self.assertEqual(v["obscure_words_found"], [])
        self.assertEqual(v["rare_domain_nouns_advisory"], [])
        self.assertEqual(v["nominalizations_found"], [])
        self.assertEqual(v["noun_clusters_found"], [])
        self.assertEqual(v["phrasal_verbs_found"], [])
        self.assertEqual(v["synonym_rotations_found"], [])
        self.assertEqual(v["complex_sentences_found"], [])
        self.assertEqual(v["em_dashes_found"], [])
        self.assertEqual(v["smart_quotes_found"], [])
        self.assertFalse(v["inline_header_list_overuse"]["flagged"])
        self.assertEqual(v["sentences_exceeding_25_words"], [])
        self.assertEqual(v["paragraphs_exceeding_4_sentences"], [])

    def test_graceful_degradation_reports_missing_packages_and_hint(self):
        report = self._run_on_text(
            "Use this flag in lieu of a configuration file.\n",
            engine="stdlib",
        )
        caps = report["summary"]["capabilities"]
        self.assertEqual(caps["engine_used"], "stdlib")
        self.assertFalse(caps["wordfreq_available"])
        self.assertFalse(caps["spacy_available"])
        self.assertIsNone(caps["spacy_model"])
        self.assertEqual(
            caps["missing_packages"],
            ["wordfreq", "spacy", "en_core_web_sm"],
        )
        self.assertEqual(caps["install_hint"], INSTALL_HINT_MESSAGE)
        obscure = report["violations"]["obscure_words_found"]
        self.assertEqual(len(obscure), 1)
        self.assertEqual(obscure[0]["word"], "lieu")

    def test_detects_short_obscure_words_and_exempts_code_and_cs_terms(self):
        report = self._run_on_text(
            "---\n"
            "name: sample-doc\n"
            "description: Sample frontmatter.\n"
            "---\n"
            "Use a flag in lieu of a file, or by dint of a retry loop.\n"
            "The check failed; ergo, the question is moot and does not bode well.\n"
            "The worker will cede the lock and obviate the pith of the issue.\n"
            "Inline code `lieu` and CS terms like mutex, idempotent, shard, and inode are fine.\n"
            "```python\n"
            "lieu = 'obviate ergo moot'\n"
            "```\n"
        )
        found = {m["word"]: m for m in report["violations"]["obscure_words_found"]}
        for expected in (
            "lieu",
            "dint",
            "ergo",
            "moot",
            "bode",
            "cede",
            "obviate",
            "pith",
        ):
            self.assertIn(expected, found)
        for allowed in ("mutex", "idempotent", "shard", "inode", "sample"):
            self.assertNotIn(allowed, found)

    def test_detects_rare_domain_nouns_advisory_and_wordfreq_hooks(self):
        report = self._run_on_text(
            "The paper discusses epistemology and malfeasance in the module.\n"
        )
        advisory = {
            m["word"] for m in report["violations"]["rare_domain_nouns_advisory"]
        }
        self.assertIn("epistemology", advisory)
        self.assertIn("malfeasance", advisory)

        fake_wordfreq = mock.MagicMock()
        fake_wordfreq.zipf_frequency.return_value = 2.4
        obscure_entry = prose_lexicon.lookup_obscure_word(
            "obfuscate", pos_hint="VERB", wordfreq_mod=fake_wordfreq
        )
        self.assertIsNotNone(obscure_entry)
        assert obscure_entry is not None
        self.assertEqual(obscure_entry.pos, "verb")
        self.assertEqual(obscure_entry.zipf, 2.4)

        is_rare, zipf_val = prose_lexicon.is_rare_domain_noun(
            "eigenvalue", is_noun_hint=True, wordfreq_mod=fake_wordfreq
        )
        self.assertTrue(is_rare)
        self.assertEqual(zipf_val, 2.4)

    def test_detects_nominalizations_phrasal_verbs_noun_clusters_and_synonyms(self):
        report = self._run_on_text(
            "We perform an investigation and make a decision on the request.\n"
            "Next we spin up a worker and dive into the container cluster configuration file parser.\n"
            "First check the input, then verify the output.\n"
        )
        v = report["violations"]
        noms = {m["nominalization"] for m in v["nominalizations_found"]}
        self.assertIn("investigation", noms)
        self.assertIn("decision", noms)

        phrasals = {m["phrasal_verb"] for m in v["phrasal_verbs_found"]}
        self.assertIn("spin up", phrasals)
        self.assertIn("dive into", phrasals)

        clusters = v["noun_clusters_found"]
        self.assertEqual(len(clusters), 1)
        self.assertIn(
            "container cluster configuration file parser",
            clusters[0]["cluster"],
        )
        self.assertGreaterEqual(clusters[0]["length"], 4)

        rotations = v["synonym_rotations_found"]
        self.assertEqual(len(rotations), 1)
        rotated_words = {w["word"] for w in rotations[0]["words_used"]}
        self.assertEqual(rotated_words, {"check", "verify"})

    def test_epistemic_modality_hedges_are_not_penalized_as_passive(self):
        report = self._run_on_text(
            "The network request may have failed during the outage.\n"
            "The stale lock might be held by another worker process.\n"
            "Deprecated fields could be removed in the next release.\n"
        )
        self.assertEqual(report["violations"]["complex_sentences_found"], [])
        self.assertEqual(report["summary"]["max_sentence_complexity"], 0)

    def test_syntactic_cognitive_complexity_scores_and_breakdowns(self):
        simple = score_sentence_complexity(
            "If the request fails, the client retries once.",
            line_num=1,
            sentence_idx=1,
        )
        self.assertLessEqual(simple.cognitive_complexity, 2)
        self.assertEqual(simple.classification, "simple")

        convoluted_text = (
            "If the configuration payload that the primary cluster controller "
            "in the staging environment receives is not validated by the "
            "schema verifier unless the override flag is set, the deployment fails."
        )
        complex_res = score_sentence_complexity(
            convoluted_text, line_num=5, sentence_idx=2
        )
        self.assertGreater(complex_res.cognitive_complexity, 5)
        self.assertEqual(complex_res.classification, "high")
        joined_breakdown = " | ".join(complex_res.breakdown)
        self.assertIn("subordinate clause", joined_breakdown)
        self.assertIn("nested clause", joined_breakdown)
        self.assertIn("center-embedded clause", joined_breakdown)
        self.assertIn("front-loaded pre-verbal delay", joined_breakdown)
        self.assertIn("passive voice with explicit agent", joined_breakdown)
        self.assertIn("stacked negation", joined_breakdown)

        abstract_res = score_sentence_complexity(
            "The conceptualization of methodological operationalization requires epistemological alignment.",
            line_num=8,
            sentence_idx=3,
        )
        self.assertTrue(
            any("abstractness cluster" in b for b in abstract_res.breakdown)
        )

    def test_spacy_tree_walker_scoring_with_mock_nlp(self):
        tokens = [
            _FakeSpacyToken("The", 0, "DET", "det", 1),
            _FakeSpacyToken("payload", 1, "NOUN", "nsubj", 8),
            _FakeSpacyToken("that", 2, "PRON", "mark", 4),
            _FakeSpacyToken("the", 3, "DET", "det", 4),
            _FakeSpacyToken("server", 4, "NOUN", "nsubj", 5),
            _FakeSpacyToken("sent", 5, "VERB", "relcl", 1, left_indices=[2, 4]),
            _FakeSpacyToken("yesterday", 6, "NOUN", "npadvmod", 5),
            _FakeSpacyToken("morning", 7, "NOUN", "npadvmod", 5),
            _FakeSpacyToken("fails", 8, "VERB", "ROOT", 8, left_indices=[1]),
        ]
        for t in tokens:
            t._doc = tokens

        def fake_nlp(_text: str) -> Any:
            return tokens

        res = score_sentence_complexity(
            "The payload that the server sent yesterday morning fails.",
            line_num=1,
            sentence_idx=1,
            nlp=fake_nlp,
        )
        joined = " | ".join(res.breakdown)
        self.assertIn("center-embedded clause", joined)
        self.assertGreaterEqual(res.cognitive_complexity, 3)

    def test_detects_em_dashes_outside_code_spans(self):
        report = self._run_on_text(
            "---\n"
            "The cache stores tokens in memory\u2014reducing database load.\n"
            "It also handles retries--without blocking the caller.\n"
            "Spaced stand-in -- is also flagged.\n"
            "Literal code span `\u2014` or `--` and <!-- html comment --> are ignored.\n"
            "```python\n"
            "x = '\u2014 --'\n"
            "```\n"
        )
        em_dashes = report["violations"]["em_dashes_found"]
        self.assertEqual([m["line"] for m in em_dashes], [2, 3, 4])

    def test_detects_inline_header_list_overuse(self):
        report = self._run_on_text(
            "- **Pooling**: Reuses open sockets.\n"
            "- **Retries**: Retries failed HTTP 503 requests.\n"
            "- **Timeouts**: Cancels slow requests.\n"
            "- Plain item without bold header.\n"
        )
        overuse = report["violations"]["inline_header_list_overuse"]
        self.assertTrue(overuse["flagged"])
        self.assertEqual(overuse["inline_header_count"], 3)
        self.assertEqual(overuse["total_bullets"], 4)
        self.assertEqual(overuse["ratio"], 0.75)

        numbered_report = analyze_prose(
            "-",
            engine="stdlib",
            text=(
                "1. **First step**: Open the socket.\n"
                "2. **Second step**: Send the payload.\n"
                "3. **Third step**: Close the socket.\n"
            ),
        )
        self.assertEqual(numbered_report["file_path"], "<stdin>")
        num_overuse = numbered_report["violations"]["inline_header_list_overuse"]
        self.assertTrue(num_overuse["flagged"])
        self.assertEqual(num_overuse["inline_header_count"], 3)
        self.assertEqual(num_overuse["total_bullets"], 3)

    def test_detects_banned_words_phrases_and_smart_quotes(self):
        report = self._run_on_text(
            "This module serves as a robust, seamless hub, highlighting the \u201cnew\u201d design.\n"
        )
        v = report["violations"]
        words = {m["banned_word"] for m in v["banned_ai_words_found"]}
        phrases = {m["banned_phrase"] for m in v["banned_ai_phrases_found"]}
        self.assertIn("robust", words)
        self.assertIn("seamless", words)
        self.assertIn("serves as", phrases)
        self.assertIn("dangling -ing participle commentary", phrases)
        self.assertEqual(len(v["smart_quotes_found"]), 1)

    def test_auto_probe_spacy_clusters_and_error_paths(self):
        self.assertIsNone(_try_import_optional("nonexistent_pkg_xyz_123"))
        self.assertEqual(_load_spacy_model(None), (None, None))

        bad_spacy = mock.MagicMock()
        bad_spacy.load.side_effect = OSError("model not found")
        self.assertEqual(_load_spacy_model(bad_spacy), (None, None))

        good_spacy = mock.MagicMock()
        fake_nlp_obj = mock.MagicMock()
        good_spacy.load.return_value = fake_nlp_obj
        self.assertEqual(
            _load_spacy_model(good_spacy, "en_core_web_sm"),
            (fake_nlp_obj, "en_core_web_sm"),
        )

        with mock.patch("analyze_prose._try_import_optional", return_value=None):
            wf, nlp_model, caps = _probe_nlp_backends("auto")
            self.assertIsNone(wf)
            self.assertIsNone(nlp_model)
            self.assertEqual(caps.engine_used, "stdlib")
            self.assertIn("wordfreq", caps.missing_packages)

        def fake_doc_nlp(text: str) -> list[_FakeSpacyToken]:
            words = [w for w in text.split() if w.isalpha()]
            return [
                _FakeSpacyToken(w, idx, "NOUN", "compound", 0)
                for idx, w in enumerate(words)
            ]

        fake_wf = mock.MagicMock()
        fake_wf.zipf_frequency.return_value = 2.5
        with mock.patch(
            "analyze_prose._probe_nlp_backends",
            return_value=(
                fake_wf,
                fake_doc_nlp,
                mock.MagicMock(to_dict=lambda: {"engine_used": "spacy"}),
            ),
        ):
            report = self._run_on_text(
                "The container cluster configuration file parser runs.\n",
                engine="auto",
            )
            self.assertGreaterEqual(len(report["violations"]["noun_clusters_found"]), 1)

    def test_dd_sv_bands_pre_verbal_delay_and_spacy_branches(self):
        dd7_res = score_sentence_complexity(
            "The configuration in the primary production cluster on host fails.",
            line_num=1,
            sentence_idx=1,
        )
        self.assertTrue(
            any("long subject-verb distance" in b for b in dd7_res.breakdown)
        )

        dd12_res = score_sentence_complexity(
            "The configuration in the primary production cluster on the remote staging host machine fails.",
            line_num=2,
            sentence_idx=2,
        )
        self.assertTrue(
            any("+2: long subject-verb distance" in b for b in dd12_res.breakdown)
        )

        delay9_res = score_sentence_complexity(
            "When the primary cluster controller in the staging region restarts, the client retries.",
            line_num=3,
            sentence_idx=3,
        )
        self.assertTrue(
            any("+1: front-loaded pre-verbal delay" in b for b in delay9_res.breakdown)
        )

        tokens = [
            _FakeSpacyToken("When", 0, "SCONJ", "mark", 2),
            _FakeSpacyToken("it", 1, "PRON", "nsubj", 2),
            _FakeSpacyToken("starts", 2, "VERB", "advcl", 16, left_indices=[0, 1]),
            _FakeSpacyToken("because", 3, "SCONJ", "mark", 5),
            _FakeSpacyToken("cache", 4, "NOUN", "nsubj", 5),
            _FakeSpacyToken(
                "expired",
                5,
                "VERB",
                "advcl",
                2,
                left_indices=[3, 4],
                ancestor_indices=[2],
            ),
            _FakeSpacyToken("in", 6, "ADP", "prep", 5),
            _FakeSpacyToken("the", 7, "DET", "det", 8),
            _FakeSpacyToken("morning", 8, "NOUN", "pobj", 6),
            _FakeSpacyToken("the", 9, "DET", "det", 10),
            _FakeSpacyToken("client", 10, "NOUN", "nsubj", 18),
            _FakeSpacyToken("on", 11, "ADP", "prep", 10),
            _FakeSpacyToken("the", 12, "DET", "det", 15),
            _FakeSpacyToken("remote", 13, "ADJ", "amod", 15),
            _FakeSpacyToken("staging", 14, "NOUN", "compound", 15),
            _FakeSpacyToken("host", 15, "NOUN", "pobj", 11),
            _FakeSpacyToken("quickly", 16, "ADV", "advmod", 18),
            _FakeSpacyToken("and", 17, "CCONJ", "cc", 16),
            _FakeSpacyToken("fails", 18, "VERB", "ROOT", 18, left_indices=[10]),
        ]
        for t in tokens:
            t._doc = tokens

        spacy_res = score_sentence_complexity(
            "Short text not matching stdlib.",
            line_num=4,
            sentence_idx=4,
            nlp=lambda _: tokens,
        )
        joined = " | ".join(spacy_res.breakdown)
        self.assertIn("nested clause", joined)
        self.assertIn("long subject-verb distance", joined)
        self.assertIn("front-loaded pre-verbal delay", joined)

    def test_document_thresholds_missing_file_and_cli_args(self):
        missing_res = analyze_prose("/nonexistent/path/to/missing_prose_doc.md")
        self.assertIn("error", missing_res)

        report = self._run_on_text(
            "First short sentence. Second short sentence. Third short sentence. "
            "Fourth short sentence. Fifth short sentence in the same paragraph.\n\n"
            "If the configuration payload that the primary cluster controller "
            "in the staging environment receives is not validated by the "
            "schema verifier unless the override flag is set, the deployment fails.\n",
            complexity_threshold=5,
        )
        v = report["violations"]
        self.assertEqual(len(v["paragraphs_exceeding_4_sentences"]), 1)
        self.assertEqual(len(v["sentences_exceeding_25_words"]), 1)
        self.assertEqual(len(v["complex_sentences_found"]), 1)
        self.assertGreater(v["complex_sentences_found"][0]["cognitive_complexity"], 5)

        parsed = _parse_cli_args(["doc.md", "--threshold", "4", "--engine", "stdlib"])
        self.assertEqual(parsed.file_path, "doc.md")
        self.assertEqual(parsed.threshold, 4)
        self.assertEqual(parsed.engine, "stdlib")
        default_parsed = _parse_cli_args([])
        self.assertIsNone(default_parsed.file_path)
        self.assertEqual(_parse_cli_args(["-"]).file_path, "-")
        with mock.patch("sys.stdin", io.StringIO("Clean prose sentence.\n")):
            stdin_res = analyze_prose("-", engine="stdlib")
        self.assertEqual(stdin_res["file_path"], "<stdin>")
        self.assertEqual(stdin_res["summary"]["total_sentences"], 1)

    def test_lexicon_suffix_inference_and_wordfreq_fallbacks(self):
        self.assertEqual(prose_lexicon.normalize_lemma("cities"), "city")
        bad_wf = mock.MagicMock()
        bad_wf.zipf_frequency.side_effect = RuntimeError("broken")
        self.assertIsNone(
            prose_lexicon.lookup_obscure_word("lieu", wordfreq_mod=bad_wf).zipf
        )

        low_wf = mock.MagicMock()
        low_wf.zipf_frequency.return_value = 2.1
        for word, expected_pos in (
            ("lugubriously", "adv"),
            ("oleaginous", "adj"),
            ("vituperate", "verb"),
        ):
            entry = prose_lexicon.lookup_obscure_word(
                word, pos_hint=None, wordfreq_mod=low_wf
            )
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.pos, expected_pos)

        is_rare, zipf_score = prose_lexicon.is_rare_domain_noun(
            "eschatology", is_noun_hint=False, wordfreq_mod=low_wf
        )
        self.assertTrue(is_rare)
        self.assertEqual(zipf_score, 2.1)


if __name__ == "__main__":
    unittest.main()
