import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import classify_artifacts
from classify_artifacts import classify, main

BLUEPRINT_FRONTMATTER = "---\nname: core\ntype: module\ndescription: x\n---\n\n# Core\n"
MUST_LINES = "".join(f"- The parser MUST handle case {i}.\n" for i in range(17))


class TestClassify(unittest.TestCase):
    def assert_type(self, path, content, expected_type, expected_rule=None):
        result = classify(path, content)
        self.assertEqual(result["type"], expected_type, result)
        if expected_rule:
            self.assertEqual(result["rule"], expected_rule, result)
        return result

    # One case per rank.
    def test_rank1_blueprint_by_frontmatter(self):
        self.assert_type(
            "docs/core.md", BLUEPRINT_FRONTMATTER, "blueprint", "blueprint-frontmatter-or-name"
        )

    def test_rank1_blueprint_by_filename(self):
        self.assert_type("blueprints/features/x.blueprint.md", "", "blueprint")

    def test_rank1_frontmatter_type_must_be_blueprint_kind(self):
        self.assert_type("docs/x.md", "---\ntype: tutorial\n---\n", "reference-doc")

    def test_rank1_type_outside_frontmatter_is_ignored(self):
        self.assert_type("docs/x.md", "# Title\n\ntype: module\n", "reference-doc")

    def test_rank2_schema_by_filename(self):
        result = self.assert_type("specification/json/common.schema.json", "{}", "normative-spec")
        self.assertEqual(result["rule"], "json-schema")

    def test_rank2_json_with_json_schema_meta_is_spec(self):
        content = '{\n  "$schema": "https://json-schema.org/draft/2020-12/schema",\n  "$defs": {}\n}'
        self.assert_type("specification/v1_0/json/common_types.json", content, "normative-spec")

    def test_rank2_config_json_pointing_at_schemastore_is_code(self):
        content = '{"$schema": "https://json.schemastore.org/tsconfig", "compilerOptions": {}}'
        self.assert_type("tsconfig.json", content, "code", "not-prose")

    def test_rank2_plain_json_is_code(self):
        self.assert_type("data/fixture.json", '{"a": 1}', "code")

    def test_markdown_with_embedded_schema_is_not_spec(self):
        content = '# Guide\n\n```json\n{"$schema": "https://json-schema.org/draft/2020-12/schema"}\n```\n'
        self.assert_type("docs/guide.md", content, "reference-doc")

    def test_keywords_in_fenced_code_do_not_count(self):
        content = "# Guide\n\n```python\nPROMPT = '''\nYou MUST x.\nYou MUST y.\nYou MUST z.\n'''\n```\n"
        self.assert_type("docs/guide.md", content, "reference-doc")

    def test_design_headings_in_fenced_code_do_not_count(self):
        content = "# Template\n\n~~~markdown\n## Non-goals\n## Alternatives considered\n~~~\n"
        self.assert_type("docs/template.md", content, "reference-doc")

    def test_rank3_design_by_headings(self):
        content = "# Plan\n\n## Non-goals\n\nx\n\n## Alternatives considered\n\ny\n"
        self.assert_type("docs/plan.md", content, "design", "design-headings-or-dir")

    def test_rank3_design_by_status_line(self):
        content = "# ADR 3\n\nStatus: Accepted\n\n## Decision\n\nUse X.\n"
        self.assert_type("notes/adr3.md", content, "design")

    def test_rank3_one_design_heading_is_not_enough(self):
        self.assert_type("docs/plan.md", "# Plan\n\n## Non-goals\n", "reference-doc")

    def test_rank3_design_by_dir(self):
        for path in ("rfcs/0001-thing.md", "docs/adr/0002.md", "proposals/x.md"):
            with self.subTest(path=path):
                self.assert_type(path, "# Title\n", "design")

    def test_rank4_normative_by_keywords(self):
        content = "A MUST do x.\nB SHOULD do y.\nC MAY do z.\n"
        self.assert_type("docs/protocol.md", content, "normative-spec", "rfc2119-keywords-or-spec-dir")

    def test_rank4_lowercase_keywords_are_not_normative(self):
        content = "You must do x.\nYou should do y.\nYou may do z.\n"
        self.assert_type("docs/guide.md", content, "reference-doc")

    def test_rank4_two_keyword_lines_are_not_enough(self):
        self.assert_type("docs/guide.md", "A MUST x.\nB SHOULD y.\n", "reference-doc")

    def test_rank4_normative_by_dir(self):
        self.assert_type("specification/v0_9_1/docs/sdks_spec.md", "# SDKs\n", "normative-spec")

    def test_rank5_changelog(self):
        self.assert_type("CHANGELOG.md", "## 1.0.0\n", "reference-doc", "changelog")

    def test_rank6_default(self):
        result = self.assert_type("README.md", "# Hi\n", "reference-doc", "prose-default")
        self.assertEqual(result["rubric"], "references/rubrics/reference_docs.md")

    # a2ui-shaped precedence cases.
    def test_blueprint_with_many_must_lines_is_blueprint(self):
        self.assert_type(
            "blueprints/modules/x.blueprint.md", BLUEPRINT_FRONTMATTER + MUST_LINES, "blueprint"
        )

    def test_blueprints_readme_is_reference_doc(self):
        self.assert_type("blueprints/README.md", "# Blueprints\n\nHow to use.\n", "reference-doc")

    def test_blueprint_skill_is_reference_doc(self):
        content = "---\nname: foo\ndescription: bar\n---\n\n# Foo\n"
        self.assert_type("blueprints/skills/foo/SKILL.md", content, "reference-doc")

    # Code false-positive regressions.
    def test_code_files_stay_code(self):
        cases = {
            "spec/models/user_spec.rb": "it 'MUST' do\nend\n",
            "src/foo.spec.ts": "describe('x', () => {});\n",
            "design/tokens.ts": "export const x = 1;\n",
            "lib/parser.py": "# MUST do x\n# MUST do y\n# SHOULD do z\n",
            "package-lock.json": "{}",
        }
        for path, content in cases.items():
            with self.subTest(path=path):
                result = self.assert_type(path, content, "code", "not-prose")
                self.assertIsNone(result["rubric"])

    def test_missing_content_classifies_by_path(self):
        self.assert_type("specs/deleted.md", None, "normative-spec")
        self.assert_type("docs/deleted.md", None, "reference-doc")

    def test_extension_check_is_case_insensitive(self):
        self.assert_type("docs/NOTES.MD", "# x\n", "reference-doc")

    def test_every_doc_type_has_rubric(self):
        for doc_type in classify_artifacts.DOC_TYPES:
            self.assertIn(doc_type, classify_artifacts.RUBRICS)


class TestMain(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "blueprints"))
        with open(os.path.join(self.root, "blueprints", "core.md"), "w") as handle:
            handle.write(BLUEPRINT_FRONTMATTER)

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_main(self, argv, stdin_text=""):
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stdin", io.StringIO(stdin_text)), patch(
            "sys.stderr", io.StringIO()
        ):
            code = main(argv)
        return code, out.getvalue()

    def test_reads_content_from_root(self):
        code, out = self.run_main(["--root", self.root, "blueprints/core.md"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["type"], "blueprint")

    def test_no_content_flag(self):
        code, out = self.run_main(["--root", self.root, "--no-content", "blueprints/core.md"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["type"], "reference-doc")

    def test_stdin_paths(self):
        code, out = self.run_main(
            ["--root", self.root, "-"], "blueprints/core.md\n\nsrc/a.ts\n"
        )
        self.assertEqual(code, 0)
        self.assertEqual([r["type"] for r in json.loads(out)], ["blueprint", "code"])

    def test_missing_file_uses_path_only(self):
        code, out = self.run_main(["--root", self.root, "specs/gone.md"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["type"], "normative-spec")

    def test_empty_stdin_prints_empty_list(self):
        code, out = self.run_main(["-"], "")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_usage_error_exits_2(self):
        code, _ = self.run_main([])
        self.assertEqual(code, 2)
        code, _ = self.run_main(["--bogus", "x.md"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
