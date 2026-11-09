"""Unit tests for prepare_design_context.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import prepare_design_context as pdc


class PrepareDesignContextTests(unittest.TestCase):
    """Tests mode classification, door signals, link resolution, and code metrics."""

    def test_classify_target_mode_variants(self) -> None:
        """Classifies design-doc, code-architecture, and conformance modes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / "spec.md"
            spec.write_text("# Spec\n", encoding="utf-8")
            schema = root / "item.schema.json"
            schema.write_text(
                '{"$schema": "https://json-schema.org/draft/2020-12/schema"}\n',
                encoding="utf-8",
            )
            code_dir = root / "pkg"
            code_dir.mkdir()
            (code_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")

            self.assertEqual(
                pdc.classify_target_mode([spec, schema], None), "design-doc"
            )
            self.assertEqual(
                pdc.classify_target_mode([code_dir], None), "code-architecture"
            )
            self.assertEqual(pdc.classify_target_mode([code_dir], spec), "conformance")

    def test_door_classification_two_way_vs_one_way(self) -> None:
        """Distinguishes reversible internal helpers from one-way door changes."""
        two_way = pdc.classify_door_reversibility(
            "Refactor internal helper loop.", 80, False
        )
        self.assertEqual(two_way.overall, "two-way")
        self.assertEqual(two_way.recommended_flags, [])

        one_way = pdc.classify_door_reversibility(
            "CREATE TABLE orders_v2 and update protobuf wire format.",
            450,
            True,
        )
        self.assertEqual(one_way.overall, "one-way")
        self.assertIn("persisted-schema", one_way.signals)
        self.assertIn("wire-protocol", one_way.signals)
        self.assertIn("public-contract", one_way.signals)
        self.assertIn("large-spec", one_way.signals)
        self.assertEqual(one_way.recommended_flags, ["--panel"])

    def test_external_artifact_file_uri_and_repo_relative_links(self) -> None:
        """Resolves valid file://#L... and repo-relative links in external artifacts."""
        with (
            tempfile.TemporaryDirectory() as repo_tmp,
            tempfile.TemporaryDirectory() as ext_tmp,
        ):
            repo_root = Path(repo_tmp)
            (repo_root / "docs").mkdir()
            target_doc = repo_root / "docs" / "guide.md"
            target_doc.write_text("# Guide\nLine 2\n", encoding="utf-8")

            ext_dir = Path(ext_tmp)
            artifact = ext_dir / "converged_design.md"
            artifact.write_text(
                "# Design\n\n"
                "- **D1 (2026-11-09)**: Chose SQLite WAL.\n\n"
                f"See [guide](file://{target_doc}#L1-L2), "
                "[rel](docs/guide.md), [badrel](docs/no_such_doc.md), "
                "[badanchor](#missing-section), "
                "and [dead](file:///nonexistent/missing.md#L5).\n\n"
                "```mermaid\nflowchart LR\n    A --> B\n```\n",
                encoding="utf-8",
            )
            out_dir = ext_dir / "out"
            ctx = pdc.build_design_context([str(artifact)], repo_root, out_dir)
            self.assertEqual(ctx["mode"], "design-doc")
            self.assertEqual(ctx["markdown"]["mermaid_count"], 1)
            self.assertEqual(len(ctx["markdown"]["settled_decisions"]), 1)
            broken = ctx["markdown"]["broken_links"]
            self.assertEqual(len(broken), 3)
            raw_targets = {b["raw_target"] for b in broken}
            self.assertIn("docs/no_such_doc.md", raw_targets)
            self.assertIn("#missing-section", raw_targets)
            self.assertIn("file:///nonexistent/missing.md#L5", raw_targets)

    def test_large_markdown_splits_chunks(self) -> None:
        """Passes --split-chunks when Markdown input exceeds 500 lines."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            big = root / "big_rfc.md"
            lines = ["# Big RFC\n"] + [
                f"## Section {i}\nBody line {i}.\n" for i in range(260)
            ]
            big.write_text("".join(lines), encoding="utf-8")
            out_dir = root / "scratch"
            ctx = pdc.build_design_context(["big_rfc.md"], root, out_dir)
            self.assertGreaterEqual(len(ctx["markdown"]["chunks"]), 1)
            self.assertTrue((out_dir / "design_context.json").is_file())

    def test_code_architecture_detects_python_import_cycles_and_mocked_tools(
        self,
    ) -> None:
        """Detects Python import cycles and handles missing optional tools gracefully."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "modpkg"
            pkg.mkdir()
            (pkg / "alpha.py").write_text(
                "import modpkg.beta\n\ndef f():\n    return 1\n", encoding="utf-8"
            )
            (pkg / "beta.py").write_text(
                "from modpkg import alpha\n\ndef g():\n    return 2\n", encoding="utf-8"
            )
            (pkg / "test_alpha.py").write_text(
                "def test_x():\n    assert True\n", encoding="utf-8"
            )
            (pkg / "package.json").write_text('{"name": "demo"}\n', encoding="utf-8")

            fake_cc = {
                "files": [
                    {
                        "file": "modpkg/alpha.py",
                        "functions": [{"name": "f", "score": 19, "line": 3}],
                    }
                ]
            }
            with (
                mock.patch("shutil.which", return_value=None),
                mock.patch.object(
                    pdc, "_probe_packrat_hotspots", return_value=([], "", False)
                ),
                mock.patch("subprocess.run") as run_mock,
            ):
                run_mock.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=json.dumps(fake_cc), stderr=""
                )
                ctx = pdc.build_design_context(["modpkg"], root, root / "out")

            self.assertEqual(ctx["mode"], "code-architecture")
            self.assertEqual(ctx["door_classification"]["overall"], "one-way")
            self.assertEqual(len(ctx["code_metrics"]["dependency_cycles"]), 1)
            self.assertEqual(
                ctx["code_metrics"]["high_complexity_functions"][0]["score"], 19
            )
            self.assertIn("sem", ctx["code_metrics"]["skipped_probes"])

    def test_conformance_mode_and_cli_main_exit_codes(self) -> None:
        """Runs CLI main() in conformance mode and exits 2 on missing paths."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / "spec.md"
            spec.write_text(
                "# Governing Spec\nMust hold invariant.\n", encoding="utf-8"
            )
            src = root / "impl.py"
            src.write_text("def run():\n    return True\n", encoding="utf-8")
            out_dir = root / "ctx"

            rc_ok = pdc.main(
                [
                    "--root",
                    str(root),
                    "--output-dir",
                    str(out_dir),
                    "--against",
                    "spec.md",
                    "impl.py",
                ]
            )
            self.assertEqual(rc_ok, 0)
            saved = json.loads(
                (out_dir / "design_context.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["mode"], "conformance")

            rc_missing_target = pdc.main(
                ["--root", str(root), "--output-dir", str(out_dir), "nope.py"]
            )
            self.assertEqual(rc_missing_target, 2)
            rc_missing_spec = pdc.main(
                [
                    "--root",
                    str(root),
                    "--output-dir",
                    str(out_dir),
                    "--against",
                    "nope.md",
                    "impl.py",
                ]
            )
            self.assertEqual(rc_missing_spec, 2)
            rc_bad_args = pdc.main([])
            self.assertEqual(rc_bad_args, 2)


if __name__ == "__main__":
    unittest.main()
