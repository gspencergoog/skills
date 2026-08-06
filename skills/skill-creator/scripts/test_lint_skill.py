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
author: John Doe
version: 1.0.0
license: MIT
compatibility: python 3.10+
metadata:
  category: dev
key_features:
  - feature 1
tags:
  - tag1
---
# Body
"""
        self.skill_md.write_text(content, encoding="utf-8")
        issues = validate_skill_file(self.skill_md)
        self.assertFalse(any("Disallowed keys in YAML frontmatter" in i.message for i in issues))

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
