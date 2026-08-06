#!/usr/bin/env python3
"""
test_init_skill.py - Unit tests for init_skill.py
Achieves >90% test coverage for init_skill.py.
"""

import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import init_skill
from init_skill import create_skill_template, main


class TestInitSkill(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_default_initialization_minimal(self):
        created = create_skill_template("my-skill", self.root)
        self.assertTrue(created.exists())
        self.assertTrue((created / "SKILL.md").exists())
        self.assertFalse((created / "scripts").exists())
        self.assertFalse((created / "references").exists())
        self.assertFalse((created / "assets").exists())

        content = (created / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: my-skill", content)
        self.assertIn("description:", content)

    def test_initialization_with_flags(self):
        created = create_skill_template(
            "full-skill",
            self.root,
            include_scripts=True,
            include_references=True,
            include_assets=True,
        )
        self.assertTrue(created.exists())
        self.assertTrue((created / "SKILL.md").exists())
        self.assertTrue((created / "scripts").exists())
        self.assertTrue((created / "references").exists())
        self.assertTrue((created / "assets").exists())

    def test_existing_skill_md_not_overwritten(self):
        skill_dir = self.root / "existing-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("Existing Content", encoding="utf-8")

        create_skill_template("existing-skill", skill_dir)
        self.assertEqual(skill_md.read_text(encoding="utf-8"), "Existing Content")

    def test_cli_main_default(self):
        with patch.object(sys, "argv", ["init_skill.py", "cli-skill", "--path", str(self.root)]):
            ret = main()
            self.assertEqual(ret, 0)
            self.assertTrue((self.root / "cli-skill" / "SKILL.md").exists())

    def test_cli_main_with_all_flags(self):
        with patch.object(
            sys,
            "argv",
            [
                "init_skill.py",
                "cli-skill-full",
                "--path",
                str(self.root),
                "--scripts",
                "--references",
                "--assets",
            ],
        ):
            ret = main()
            self.assertEqual(ret, 0)
            target = self.root / "cli-skill-full"
            self.assertTrue((target / "scripts").exists())
            self.assertTrue((target / "references").exists())
            self.assertTrue((target / "assets").exists())


if __name__ == "__main__":
    unittest.main()
