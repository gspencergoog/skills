#!/usr/bin/env python3
"""
test_lint_skill.py - Unit tests for lint_skill.py
Achieves >90% test coverage for lint_skill.py.
"""

import sys
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import lint_skill
from lint_skill import (
    ValidationIssue,
    parse_yaml_frontmatter,
    validate_skill_file,
    find_skill_md_files,
    lint_skills,
)


class TestValidationIssue(unittest.TestCase):
    def test_str_error_with_line(self):
        issue = ValidationIssue(True, Path("skills/test/SKILL.md"), "Missing name", line_num=10)
        self.assertEqual(str(issue), "[ERROR] skills/test/SKILL.md:10: Missing name")

    def test_str_warning_without_line(self):
        issue = ValidationIssue(False, Path("skills/test/SKILL.md"), "Too long")
        self.assertEqual(str(issue), "[WARNING] skills/test/SKILL.md: Too long")


class TestParseYamlFrontmatter(unittest.TestCase):
    def test_missing_start_marker(self):
        metadata, body, err = parse_yaml_frontmatter("name: test\n---")
        self.assertIsNone(metadata)
        self.assertEqual(err, "File does not start with YAML frontmatter marker '---'.")

    def test_unclosed_marker(self):
        metadata, body, err = parse_yaml_frontmatter("---\nname: test\n")
        self.assertIsNone(metadata)
        self.assertEqual(err, "Unclosed YAML frontmatter marker '---'.")

    def test_valid_frontmatter(self):
        content = """---
name: "my-skill"
description: 'A test description for my skill.'
# Comment line
key_extra: value1
  multiline value line
---
# Body Title
Body text.
"""
        metadata, body, err = parse_yaml_frontmatter(content)
        self.assertIsNone(err)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.get("name"), "my-skill")
        self.assertEqual(metadata.get("description"), "A test description for my skill.")
        self.assertIn("key_extra", metadata)
        self.assertIn("Body Title", body)

    def test_unquoted_colon_in_description_fails(self):
        content = """---
name: test-skill
description: Calculate complexity. Use this skill whenever asked to: (1) do something.
---
# Body
"""
        metadata, body, err = parse_yaml_frontmatter(content)
        self.assertIsNone(metadata)
        self.assertIn("unquoted string contains ': '", err)

    def test_quoted_colon_in_description_passes(self):
        content = """---
name: test-skill
description: "Calculate complexity. Use this skill whenever asked to: (1) do something."
---
# Body
"""
        metadata, body, err = parse_yaml_frontmatter(content)
        self.assertIsNone(err)
        self.assertIsNotNone(metadata)
        self.assertEqual(
            metadata.get("description"),
            "Calculate complexity. Use this skill whenever asked to: (1) do something.",
        )

    def test_block_scalar_colon_in_description_passes(self):
        content = """---
name: test-skill
description: >-
  Calculate complexity. Use this skill whenever asked to: (1) do something.
---
# Body
"""
        metadata, body, err = parse_yaml_frontmatter(content)
        self.assertIsNone(err)
        self.assertIsNotNone(metadata)
        self.assertIn("asked to: (1)", metadata.get("description"))

    def test_nested_mapping_metadata_passes(self):
        content = """---
name: test-skill
metadata:
  category: DevOps
description: Valid description.
---
# Body
"""
        metadata, body, err = parse_yaml_frontmatter(content)
        self.assertIsNone(err)
        self.assertIsNotNone(metadata)

    def test_pyyaml_colon_error_is_normalized(self):
        content = """---
name: test-skill
description: Calculate complexity. Use this skill whenever asked to: (1) do something.
---
# Body
"""
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        with patch.object(yaml, "safe_load", side_effect=yaml.YAMLError("mapping values are not allowed here")):
            metadata, body, err = parse_yaml_frontmatter(content)
        self.assertIsNone(metadata)
        self.assertIn("unquoted string contains ': '", err)
        self.assertIn(">-", err)


CLEAN_DESCRIPTION = (
    "Reviews pull requests and local changes for defects and produces a findings "
    "report with an evidence tier on every finding. Use when the user asks for a "
    "review, a critique, or a second opinion on a diff. Don't use for posting the "
    "review to GitHub (use pr-review instead)."
)


def skill_content(description_block: str, body: str = "# Body\n\nInstructions.\n") -> str:
    return f"---\nname: test-skill\n{description_block}\n---\n{body}"


def folded(description: str) -> str:
    return "description: >-\n  " + description


class TestDescriptionQuality(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.skill_dir = Path(self.temp_dir) / "test-skill"
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        self.skill_md = self.skill_dir / "SKILL.md"

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def lint(self, content: str):
        self.skill_md.write_text(content, encoding="utf-8")
        return validate_skill_file(self.skill_md)

    def description_issues(self, content: str):
        return [i for i in self.lint(content) if "description" in i.message or "When to use" in i.message]

    def test_clean_description_has_no_issues(self):
        self.assertEqual(self.description_issues(skill_content(folded(CLEAN_DESCRIPTION))), [])

    def test_over_limit_is_error(self):
        text = "Reviews things. Use when asked. " + ("x" * 1000)
        issues = self.description_issues(skill_content(folded(text)))
        self.assertTrue(any(i.is_error and "platform limit" in i.message for i in issues))

    def test_soft_length_bounds_warn(self):
        short = self.description_issues(skill_content(folded("Reviews code. Use when asked to review.")))
        self.assertTrue(any(not i.is_error and "aim for 200 to 800" in i.message for i in short))
        long_text = "Reviews code. Use when asked. " + ("word " * 170)
        long_issues = self.description_issues(skill_content(folded(long_text)))
        self.assertTrue(any(not i.is_error and "aim for 200 to 800" in i.message for i in long_issues))
        self.assertFalse(any(i.is_error for i in long_issues))

    def test_missing_trigger_warns(self):
        text = CLEAN_DESCRIPTION.replace("Use when", "Handy when")
        issues = self.description_issues(skill_content(folded(text)))
        self.assertTrue(any("'Use when ...' clause" in i.message for i in issues))

    def test_second_person_outside_quotes_warns(self):
        text = CLEAN_DESCRIPTION.replace("Use when the user asks", "Use when you need")
        issues = self.description_issues(skill_content(folded(text)))
        self.assertTrue(any("first or second person" in i.message for i in issues))

    def test_second_person_inside_quotes_is_ignored(self):
        text = CLEAN_DESCRIPTION.replace("a second opinion on a diff", 'a second opinion, or says "what if we drop this" or "how do I review"')
        issues = self.description_issues(skill_content(folded(text)))
        self.assertFalse(any("first or second person" in i.message for i in issues))

    def test_flag_gloss_warns_and_bare_flags_do_not(self):
        glossed = CLEAN_DESCRIPTION + " Supports --panel (two reviewers) and --verify (runs tests)."
        issues = self.description_issues(skill_content(folded(glossed)))
        self.assertTrue(any("glosses a flag" in i.message for i in issues))
        bare = CLEAN_DESCRIPTION + " Flags: --panel, --verify, --max."
        issues = self.description_issues(skill_content(folded(bare)))
        self.assertFalse(any("glosses a flag" in i.message for i in issues))

    def test_puffery_word_warns_with_word_boundaries(self):
        issues = self.description_issues(skill_content(folded(CLEAN_DESCRIPTION.replace("a findings report", "a comprehensive findings report"))))
        self.assertTrue(any("puffery word 'comprehensive'" in i.message for i in issues))
        clean = CLEAN_DESCRIPTION.replace("a findings report", "a report that effectively remasters the branch")
        issues = self.description_issues(skill_content(folded(clean)))
        self.assertFalse(any("puffery" in i.message for i in issues))

    def test_em_dash_warns(self):
        issues = self.description_issues(skill_content(folded(CLEAN_DESCRIPTION.replace(" and produces", " \u2014 and produces"))))
        self.assertTrue(any("em-dash" in i.message for i in issues))

    def test_body_when_to_use_section_warns(self):
        body = "# Body\n\n## When To Use This Skill\n\n- always\n"
        issues = self.description_issues(skill_content(folded(CLEAN_DESCRIPTION), body=body))
        self.assertTrue(any("'When to use' section" in i.message for i in issues))

    def test_plain_scalar_style_warns(self):
        issues = self.description_issues(skill_content('description: "' + CLEAN_DESCRIPTION + '"'))
        self.assertTrue(any("uses a quoted scalar" in i.message for i in issues))

    def test_literal_scalar_warns_about_newlines(self):
        block = "description: |-\n  Reviews code and more. Use when asked.\n  Second line that keeps its newline and makes this long enough to pass the length rule, more words here, and more."
        issues = self.description_issues(skill_content(block))
        self.assertTrue(any("literal newlines" in i.message for i in issues))

    def test_imperative_and_noun_openers_warn(self):
        for opener in ("Create components", "Guide for writing", "A specialized workflow for"):
            text = opener + CLEAN_DESCRIPTION[len("Reviews pull requests"):]
            issues = self.description_issues(skill_content(folded(text)))
            self.assertTrue(any("third-person capability statement" in i.message for i in issues), opener)

    def test_symlinked_skill_dirs_are_skipped(self):
        real = Path(self.temp_dir) / "real-skill"
        real.mkdir()
        (real / "SKILL.md").write_text(skill_content(folded(CLEAN_DESCRIPTION)).replace("test-skill", "real-skill"), encoding="utf-8")
        link = Path(self.temp_dir) / "linked-skill"
        link.symlink_to(real, target_is_directory=True)
        found = find_skill_md_files(Path(self.temp_dir))
        self.assertIn(real / "SKILL.md", found)
        self.assertNotIn(link / "SKILL.md", found)
        self.assertEqual(find_skill_md_files(link), [link / "SKILL.md"])


class TestValidateSkillFile(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.skill_dir = Path(self.temp_dir) / "test-skill"
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        self.skill_md = self.skill_dir / "SKILL.md"

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_nonexistent_file(self):
        fake_path = self.skill_dir / "NONEXISTENT.md"
        issues = validate_skill_file(fake_path)
        self.assertTrue(any(i.is_error and "does not exist" in i.message for i in issues))

    def test_unreadable_file(self):
        self.skill_md.write_text("dummy content", encoding="utf-8")
        with patch("builtins.open", side_effect=PermissionError("Denied")):
            issues = validate_skill_file(self.skill_md)
            self.assertTrue(any(i.is_error and "Failed to read file" in i.message for i in issues))

    def test_invalid_frontmatter(self):
        self.skill_md.write_text("No frontmatter content here", encoding="utf-8")
        issues = validate_skill_file(self.skill_md)
        self.assertTrue(any(i.is_error and "YAML frontmatter" in i.message for i in issues))

    def test_disallowed_frontmatter_keys(self):
        content = """---
name: test-skill
description: Valid description for this skill.
unrecognized_custom_key: value
---
# Body
"""
        self.skill_md.write_text(content, encoding="utf-8")
        issues = validate_skill_file(self.skill_md)
        self.assertTrue(any("Disallowed keys in YAML frontmatter: unrecognized_custom_key" in i.message for i in issues))

    def test_allowed_optional_frontmatter_keys(self):
        content = """---
name: test-skill
description: Valid description for this skill.
license: MIT
compatibility: python 3.10+
metadata:
  category: dev
allowed-tools:
  - Bash
---
# Body
"""
        self.skill_md.write_text(content, encoding="utf-8")
        issues = validate_skill_file(self.skill_md)
        self.assertFalse(any("Disallowed keys in YAML frontmatter" in i.message for i in issues))

    def test_legacy_keys_rejected(self):
        content = """---
name: test-skill
description: Valid description for this skill.
author: John Doe
version: 1.0.0
key_features:
  - feature 1
tags:
  - tag1
---
# Body
"""
        self.skill_md.write_text(content, encoding="utf-8")
        issues = validate_skill_file(self.skill_md)
        errors = [i.message for i in issues if i.is_error and "Disallowed keys" in i.message]
        self.assertEqual(len(errors), 1)
        for key in ("author", "key_features", "tags", "version"):
            self.assertIn(key, errors[0])

    def test_missing_name_and_short_description(self):
        content = """---
description: short
---
# Body
"""
        self.skill_md.write_text(content, encoding="utf-8")
        issues = validate_skill_file(self.skill_md)
        self.assertTrue(any("missing required 'name'" in i.message for i in issues))
        self.assertTrue(any("too short" in i.message for i in issues))

    def test_mismatched_name(self):
        content = """---
name: different-name
description: A valid description for this skill.
---
# Body
"""
        self.skill_md.write_text(content, encoding="utf-8")
        issues = validate_skill_file(self.skill_md)
        self.assertTrue(any("does not match skill directory name" in i.message for i in issues))

    def test_clutter_file_and_unrecognized_subdir(self):
        content = """---
name: test-skill
description: A valid description for this skill.
---
# Body
"""
        self.skill_md.write_text(content, encoding="utf-8")
        (self.skill_dir / "INSTALLATION_GUIDE.md").write_text("Clutter", encoding="utf-8")
        (self.skill_dir / "README.md").write_text("Allowed README", encoding="utf-8")
        (self.skill_dir / "LICENSE").write_text("Allowed LICENSE", encoding="utf-8")
        (self.skill_dir / "custom_dir").mkdir()

        issues = validate_skill_file(self.skill_md)
        self.assertTrue(any("Disallowed clutter file 'INSTALLATION_GUIDE.md'" in i.message for i in issues))
        self.assertFalse(any("README.md" in i.message for i in issues))
        self.assertFalse(any("LICENSE" in i.message for i in issues))
        self.assertTrue(any("Unrecognized subdirectory 'custom_dir'" in i.message for i in issues))

    def test_body_length_warning_and_broken_link(self):
        long_body = "\n".join([f"Line {i}" for i in range(550)])
        content = f"""---
name: test-skill
description: A valid description for this skill.
---
# Body
{long_body}

See [nonexistent](references/missing.md) and [http](https://example.com).
"""
        self.skill_md.write_text(content, encoding="utf-8")
        issues = validate_skill_file(self.skill_md)
        self.assertTrue(any("Recommendation is under 500 lines" in i.message for i in issues))
        self.assertTrue(any("Broken relative link 'references/missing.md'" in i.message for i in issues))

    def test_case_mismatched_link(self):
        refs_dir = self.skill_dir / "references"
        refs_dir.mkdir(exist_ok=True)
        (refs_dir / "doc.md").write_text("content", encoding="utf-8")

        content = """---
name: test-skill
description: A valid description for this skill.
---
# Body
See [doc](REFERENCES/doc.md).
"""
        self.skill_md.write_text(content, encoding="utf-8")
        issues = validate_skill_file(self.skill_md)
        self.assertTrue(any("case mismatch" in i.message for i in issues))

    def test_unreferenced_reference_file(self):
        content = """---
name: test-skill
description: A valid description for this skill.
---
# Body
No mention of references.
"""
        self.skill_md.write_text(content, encoding="utf-8")
        refs_dir = self.skill_dir / "references"
        refs_dir.mkdir()
        (refs_dir / "unlinked.md").write_text("# Unlinked", encoding="utf-8")

        issues = validate_skill_file(self.skill_md)
        self.assertTrue(any("Reference file 'references/unlinked.md' is present" in i.message for i in issues))


class TestFindSkillMdFiles(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_single_file_target(self):
        skill_file = self.root / "SKILL.md"
        skill_file.write_text("content", encoding="utf-8")
        self.assertEqual(find_skill_md_files(skill_file), [skill_file])

        other_file = self.root / "OTHER.md"
        other_file.write_text("content", encoding="utf-8")
        self.assertEqual(find_skill_md_files(other_file), [])

    def test_directory_targets(self):
        s1 = self.root / "skill1"
        s1.mkdir()
        f1 = s1 / "SKILL.md"
        f1.write_text("content", encoding="utf-8")

        s2 = self.root / "skill2"
        s2.mkdir()
        f2 = s2 / "SKILL.md"
        f2.write_text("content", encoding="utf-8")

        # Directly passing skill1 dir
        self.assertEqual(find_skill_md_files(s1), [f1])

        # Passing root containing skill1 and skill2
        files = find_skill_md_files(self.root)
        self.assertEqual(files, [f1, f2])

        # Empty dir
        empty_dir = self.root / "empty"
        empty_dir.mkdir()
        self.assertEqual(find_skill_md_files(empty_dir), [])


class TestLintSkillsCLI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_lint_skills_not_found(self):
        res = lint_skills(self.root / "nonexistent")
        self.assertEqual(res, 1)

    def test_lint_skills_clean(self):
        s1 = self.root / "my-skill"
        s1.mkdir()
        skill_md = s1 / "SKILL.md"
        content = """---
name: my-skill
description: A valid description for my skill.
---
# My Skill Body
"""
        skill_md.write_text(content, encoding="utf-8")

        res = lint_skills(self.root)
        self.assertEqual(res, 0)

    def test_lint_skills_with_errors(self):
        s1 = self.root / "my-skill"
        s1.mkdir()
        skill_md = s1 / "SKILL.md"
        content = """---
name: wrong-name
description: short
---
# My Skill Body
"""
        skill_md.write_text(content, encoding="utf-8")

        res = lint_skills(self.root)
        self.assertEqual(res, 1)

    def test_cli_main_invocation(self):
        with patch.object(sys, "argv", ["lint_skill.py", str(self.root)]):
            with patch("sys.exit") as mock_exit:
                # Re-run main logic block
                target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
                res = lint_skills(target)
                self.assertEqual(res, 1)


if __name__ == "__main__":
    unittest.main()
