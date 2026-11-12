#!/usr/bin/env python3
"""Unit and regression test suite for the audit-memory skill scripts."""

from __future__ import annotations

import errno
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import apply_memory_plan  # noqa: E402
import lint_memory_plan  # noqa: E402
import memory_common  # noqa: E402
import scan_memories  # noqa: E402


META_INSTRUCTION_CORPUS: tuple[str, ...] = (
    "Update line 12 to say python3",
    "Remove this obsolete claim",
    "Delete lines 5-8",
    "Change foo to bar",
    "Replace the bullet with the new path",
    "Fix the claim above",
    "Edit section 2",
    "Drop this entry",
    "Omit the reference to v1",
    "Keep the first sentence only",
    "Retain the pinned rule",
    "Move this bullet to AGENTS.md",
    "Merge the two bullets below",
    "Consolidate these entries",
    "Rewrite the paragraph for clarity",
    "- Update the claim to reflect v2",
    "- Remove line 14",
    "- Delete the bullet above",
    "- Change old_fn to new_fn",
    "- Replace this section with a table",
    "- Fix the text below",
    "- Edit the entry for auth",
    "- Drop the following claim",
    "- Omit this note",
    "- Keep line 3",
    "- Retain this record",
    "- Move the claim to skill docs",
    "- Merge lines 10 and 11",
    "- Consolidate the bullets",
    "- Rewrite this statement",
    "Line 15: use uv run instead",
    "Lines 10-12: remove deprecated flag",
    "1. Update the path to scripts/run.py",
)


class MemoryCommonTests(unittest.TestCase):
    """Tests for `memory_common.py` frontmatter, link, and path helpers."""

    def test_parse_frontmatter_multikey_and_block_scalars(self) -> None:
        lines = [
            "---",
            "# comment line inside frontmatter",
            "",
            "description: >-",
            "  Multi-line folded",
            "  description text.",
            "visibility: PINNED",
            "confidence: 'high''s'",
            'sourced_date: "2026-11-11"',
            "notes: |",
            "  block line 1",
            "  block line 2",
            "---",
            "- Body bullet #direct",
        ]
        fm = memory_common.parse_frontmatter(lines)
        self.assertTrue(fm.valid)
        self.assertEqual(fm.end_line, 13)
        self.assertTrue(fm.is_pinned)
        self.assertEqual(fm.description, "Multi-line folded description text.")
        self.assertEqual(fm.keys["confidence"], "high's")
        self.assertEqual(fm.keys["sourced_date"], "2026-11-11")
        self.assertEqual(fm.keys["notes"], "block line 1 block line 2")

    def test_parse_frontmatter_error_cases(self) -> None:
        no_open = memory_common.parse_frontmatter(["# No frontmatter"])
        self.assertFalse(no_open.valid)
        self.assertIn("Missing opening", no_open.error or "")

        no_close = memory_common.parse_frontmatter(["---", 'description: "ok"'])
        self.assertFalse(no_close.valid)
        self.assertIn("Missing closing", no_close.error or "")

        malformed = memory_common.parse_frontmatter(["---", "not a kv line", "---"])
        self.assertFalse(malformed.valid)
        self.assertIn("Malformed frontmatter line", malformed.error or "")

        unquoted_colon = memory_common.parse_frontmatter(
            ["---", "description: Foo: bar baz", "---"]
        )
        self.assertFalse(unquoted_colon.valid)
        self.assertIn("Unquoted frontmatter value", unquoted_colon.error or "")

        unquoted_hash = memory_common.parse_frontmatter(
            ["---", "description: Foo #bar", "---"]
        )
        self.assertFalse(unquoted_hash.valid)

        empty_desc = memory_common.parse_frontmatter(["---", 'description: ""', "---"])
        self.assertFalse(empty_desc.valid)
        self.assertIn("Missing or empty 'description'", empty_desc.error or "")

        pinned_no_desc = memory_common.parse_frontmatter(
            ["---", "visibility: PINNED", "---"]
        )
        self.assertTrue(pinned_no_desc.valid)
        self.assertTrue(pinned_no_desc.is_pinned)

    def test_format_frontmatter_quotes_and_preserves_keys(self) -> None:
        formatted = memory_common.format_frontmatter({
            "visibility": "PINNED",
            "description": 'Line with "quotes" and: colons',
            "note": "has # comment",
            "empty": "",
        })
        self.assertEqual(formatted[0], "---")
        self.assertEqual(formatted[-1], "---")
        self.assertTrue(formatted[1].startswith('description: "'))
        reparsed = memory_common.parse_frontmatter(formatted)
        self.assertTrue(reparsed.valid)
        self.assertTrue(reparsed.is_pinned)
        self.assertEqual(reparsed.description, 'Line with "quotes" and: colons')

    def test_direct_lines_and_claims_ignore_fences(self) -> None:
        lines = [
            "---",
            'description: "Test file"',
            "---",
            "- Real direct claim #direct",
            "```markdown",
            "- Fenced example #direct",
            "```",
            "~~~",
            "- Tilde fenced #direct",
            "~~~",
            "  * Second claim without tag",
        ]
        direct = memory_common.find_direct_lines(lines, start_after_line=3)
        self.assertEqual(direct, [4])
        self.assertEqual(memory_common.count_claims(lines, start_after_line=3), 2)
        self.assertEqual(memory_common.count_claims(["---", "---"], start_after_line=2), 0)

    def test_extract_relative_links_and_root_containment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            (root / "projects").mkdir(parents=True)
            (root / "topics").mkdir(parents=True)
            target_md = root / "topics" / "tooling.md"
            target_md.write_text('---\ndescription: "Tooling"\n---\n', encoding="utf-8")

            outside = Path(tmp) / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            symlink_escape = root / "projects" / "escape.md"
            symlink_escape.symlink_to(outside)

            lines = [
                "---",
                'description: "Links"',
                "---",
                "See [tooling](../topics/tooling.md#sec) and [missing](missing.md).",
                "Ignore ![img](pic.png), [web](https://example.com/a.md), [anchor](#top),",
                "[/abs](/abs.md), [txt](note.txt), and [escape](../../outside.md).",
            ]
            links = memory_common.extract_relative_links(
                lines, "projects/app.md", root, start_after_line=3
            )
            self.assertEqual(len(links), 2)
            self.assertEqual(links[0]["target"], "topics/tooling.md")
            self.assertTrue(links[0]["exists"])
            self.assertEqual(links[1]["target"], "projects/missing.md")
            self.assertFalse(links[1]["exists"])

            skipped = root / "skills"
            skipped.mkdir()
            skipped_file = skipped / "s.md"
            skipped_file.write_text("x", encoding="utf-8")

            self.assertTrue(memory_common.is_within_root(target_md, root, [skipped]))
            self.assertFalse(memory_common.is_within_root(symlink_escape, root, [skipped]))
            self.assertFalse(memory_common.is_within_root(skipped_file, root, [skipped]))


class ScanMemoriesTests(unittest.TestCase):
    """Tests for `scan_memories.py` discovery, link graphs, and sharding."""

    def test_probe_mfs_skipped_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem_parent = Path(tmp) / "memory"
            (mem_parent / "skills").mkdir(parents=True)
            fake_out = (
                "default (rw) entries: 42\n"
                "shared_corpus (ro) entries: 15,200\n"
                "unrelated line\n"
            )
            completed = mock.Mock(stdout=fake_out)
            with mock.patch("subprocess.run", return_value=completed):
                skipped = scan_memories._probe_mfs_skipped_mounts(mem_parent)
            names = [p.name for p in skipped]
            self.assertIn("skills", names)
            self.assertIn("shared_corpus", names)

            with mock.patch("subprocess.run", side_effect=OSError("no mfs")):
                fallback = scan_memories._probe_mfs_skipped_mounts(mem_parent)
            self.assertEqual([p.name for p in fallback], ["skills"])

    def test_discover_memory_root_and_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            work = base / "work"
            claude_mem = home / ".claude" / "projects" / "demo" / "memory"
            claude_mem.mkdir(parents=True)
            (work / "agents" / "skills").mkdir(parents=True)
            (work / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
            (work / "agents" / "agents.md").write_text("# Global\n", encoding="utf-8")

            with mock.patch.object(
                scan_memories, "_probe_mfs_skipped_mounts", return_value=[]
            ):
                root, _ = scan_memories.discover_memory_root(
                    home_dir=home, cwd=work
                )
                self.assertEqual(root, claude_mem.resolve())

                explicit, _ = scan_memories.discover_memory_root(
                    explicit_dir=str(claude_mem), home_dir=home, cwd=work
                )
                self.assertEqual(explicit, claude_mem.resolve())

                with self.assertRaises(FileNotFoundError):
                    scan_memories.discover_memory_root(
                        explicit_dir=str(base / "nonexistent"),
                        home_dir=home,
                        cwd=work,
                    )

                with self.assertRaises(FileNotFoundError):
                    scan_memories.discover_memory_root(
                        home_dir=base / "empty_home", cwd=base / "empty_work"
                    )

            targets = scan_memories.discover_promotion_targets(
                workspace=work, home_dir=home, skipped_mounts=[]
            )
            self.assertEqual(
                targets["workspace_instructions"], str((work / "AGENTS.md").resolve())
            )
            self.assertEqual(
                targets["global_instructions"],
                str((work / "agents" / "agents.md").resolve()),
            )
            self.assertIn(
                str((work / "agents" / "skills").resolve()),
                targets["writable_skill_dirs"],
            )

    def test_build_inventory_scopes_and_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mem = base / "memory"
            (mem / "projects").mkdir(parents=True)
            (mem / "topics").mkdir(parents=True)

            (mem / "preferences.md").write_text(
                '---\ndescription: "Prefs"\nvisibility: PINNED\n---\n- Pref 1 #direct\n',
                encoding="utf-8",
            )
            (mem / "projects" / "alpha.md").write_text(
                '---\ndescription: "Alpha"\n---\n- See [style](../topics/style.md)\n',
                encoding="utf-8",
            )
            (mem / "topics" / "style.md").write_text(
                '---\ndescription: "Style"\n---\n- Use ruff #direct\n',
                encoding="utf-8",
            )

            empty_inv = scan_memories.build_inventory(mem, [], {}, scope="areas")
            self.assertEqual(empty_inv["summary"]["total_files"], 0)
            self.assertEqual(empty_inv["shards"], [])

            pref_inv = scan_memories.build_inventory(mem, [], {}, scope="preferences")
            self.assertEqual(list(pref_inv["files"].keys()), ["preferences.md"])
            self.assertTrue(pref_inv["files"]["preferences.md"]["protected_file"])

            glob_inv = scan_memories.build_inventory(mem, [], {}, scope="*alpha*")
            self.assertEqual(list(glob_inv["files"].keys()), ["projects/alpha.md"])

            out_json = base / "out" / "inv.json"
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                rc = scan_memories.main([
                    "--memory-dir",
                    str(mem),
                    "--workspace",
                    str(base),
                    "--scope",
                    "all",
                    "--output",
                    str(out_json),
                ])
            self.assertEqual(rc, 0)
            loaded = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(loaded["summary"]["total_files"], 3)
            self.assertEqual(
                loaded["files"]["topics/style.md"]["incoming_links"][0]["source_file"],
                "projects/alpha.md",
            )

            with mock.patch("sys.stderr", new_callable=io.StringIO):
                bad_rc = scan_memories.main([
                    "--memory-dir",
                    str(base / "missing_dir"),
                    "--output",
                    str(out_json),
                ])
            self.assertEqual(bad_rc, 1)


class LintMemoryPlanTests(unittest.TestCase):
    """Tests for `lint_memory_plan.py` 7 validation rules and CLI."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp_dir.name)
        self.mem = self.base / "memory"
        (self.mem / "projects").mkdir(parents=True)
        (self.mem / "topics").mkdir(parents=True)

        self.agents_md = self.base / "AGENTS.md"
        self.agents_md.write_text(
            "# Workspace Rules\n\n## General rules\n\n- Existing rule.\n",
            encoding="utf-8",
        )
        self.skill_dir = self.base / "skills" / "demo-skill"
        self.skill_dir.mkdir(parents=True)
        self.skill_md = self.skill_dir / "SKILL.md"
        self.skill_md.write_text("# Demo Skill\n\n## Usage\n", encoding="utf-8")

        (self.mem / "preferences.md").write_text(
            '---\ndescription: "User prefs"\nvisibility: PINNED\n---\n- Concise replies #direct\n',
            encoding="utf-8",
        )
        (self.mem / "projects" / "old.md").write_text(
            '---\ndescription: "Old project"\nconfidence: "high"\n---\n'
            "- Stale claim on line 5\n"
            "- Direct rule on line 6 #direct\n",
            encoding="utf-8",
        )
        (self.mem / "projects" / "new.md").write_text(
            '---\ndescription: "New project"\n---\n'
            "- Active link to [old](old.md#top) on line 4\n"
            "  - Indented bullet on line 5\n",
            encoding="utf-8",
        )
        (self.mem / "topics" / "extra.md").write_text(
            '---\ndescription: "Extra topic"\n---\n- Note one\n',
            encoding="utf-8",
        )

        self.promotion_targets = {
            "global_instructions": None,
            "workspace_instructions": str(self.agents_md),
            "writable_skill_dirs": [str(self.base / "skills")],
        }
        self.inventory = scan_memories.build_inventory(
            self.mem, [], self.promotion_targets, scope="all"
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_rule1_root_containment_and_rule3_excerpt(self) -> None:
        findings = [
            {
                "id": "bad-action",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 5,
                "action": "destroy",
                "expected_excerpt": "Stale",
            },
            {
                "id": "bad-file",
                "file": "../outside.md",
                "start_line": 1,
                "end_line": 1,
                "action": "delete",
                "expected_excerpt": "x",
            },
            {
                "id": "bad-mig-target",
                "file": "projects/old.md",
                "start_line": 6,
                "end_line": 6,
                "action": "delete",
                "expected_excerpt": "Direct rule",
                "direct_migration_target": "projects/missing.md",
            },
            {
                "id": "retire-extra",
                "file": "topics/extra.md",
                "start_line": 1,
                "end_line": 4,
                "action": "retire",
                "expected_excerpt": "Extra topic",
            },
            {
                "id": "mig-to-retired",
                "file": "projects/old.md",
                "start_line": 6,
                "end_line": 6,
                "action": "delete",
                "expected_excerpt": "Direct rule",
                "direct_migration_target": "topics/extra.md",
            },
            {
                "id": "bad-excerpt",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 5,
                "action": "delete",
                "expected_excerpt": "Text not on line 5",
            },
            {
                "id": "empty-excerpt",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 5,
                "action": "delete",
                "expected_excerpt": "   ",
            },
        ]
        errors, valid = lint_memory_plan.lint_findings(
            self.inventory, findings, workspace=self.base
        )
        self.assertEqual([f["id"] for f in valid], ["retire-extra"])
        self.assertTrue(any("invalid action" in e for e in errors))
        self.assertTrue(any("outside memory_root" in e for e in errors))
        self.assertTrue(any("cannot target a file marked action='retire'" in e for e in errors))
        self.assertTrue(any("excerpt-match" in e for e in errors))

    def test_rule2_line_ranges_overlaps_and_link_rewrite_collisions(self) -> None:
        findings = [
            {
                "id": "bool-line",
                "file": "projects/old.md",
                "start_line": True,
                "end_line": 5,
                "action": "delete",
                "expected_excerpt": "Stale",
            },
            {
                "id": "oob-line",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 99,
                "action": "delete",
                "expected_excerpt": "Stale",
            },
            {
                "id": "partial-retire",
                "file": "topics/extra.md",
                "start_line": 1,
                "end_line": 2,
                "action": "retire",
                "expected_excerpt": "Extra",
            },
            {
                "id": "overlap-a",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 5,
                "action": "delete",
                "expected_excerpt": "Stale",
            },
            {
                "id": "overlap-b",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 5,
                "action": "delete",
                "expected_excerpt": "Stale",
            },
        ]
        errors, valid = lint_memory_plan.lint_findings(
            self.inventory, findings, workspace=self.base
        )
        self.assertEqual(valid, [])
        self.assertEqual(len(errors), 4)

        collision_findings = [
            {
                "id": "retire-old",
                "file": "projects/old.md",
                "start_line": 1,
                "end_line": 6,
                "action": "retire",
                "expected_excerpt": "Old project",
                "direct_migration_target": "projects/new.md",
                "link_rewrite_target": "topics/extra.md",
            },
            {
                "id": "edit-linking-line",
                "file": "projects/new.md",
                "start_line": 4,
                "end_line": 4,
                "action": "update",
                "expected_excerpt": "Active link",
                "replacement_lines": ["- Updated line 4 without link"],
            },
        ]
        c_errors, c_valid = lint_memory_plan.lint_findings(
            self.inventory, collision_findings, workspace=self.base
        )
        self.assertEqual(c_valid, [])
        self.assertTrue(any("incoming link rewrite" in e for e in c_errors))

        bad_coexist = [
            {
                "id": "retire-extra-1",
                "file": "topics/extra.md",
                "start_line": 1,
                "end_line": 4,
                "action": "retire",
                "expected_excerpt": "Extra topic",
            },
            {
                "id": "update-extra",
                "file": "topics/extra.md",
                "start_line": 4,
                "end_line": 4,
                "action": "update",
                "expected_excerpt": "Note one",
                "replacement_lines": ["- Note updated"],
            },
        ]
        co_errors, co_valid = lint_memory_plan.lint_findings(
            self.inventory, bad_coexist, workspace=self.base
        )
        self.assertEqual(co_valid, [])
        self.assertTrue(any("cannot coexist with whole-file retire" in e for e in co_errors))

    def test_rule4_no_meta_instruction_corpus_and_bullets(self) -> None:
        for idx, phrase in enumerate(META_INSTRUCTION_CORPUS):
            finding = {
                "id": f"meta-{idx}",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 5,
                "action": "update",
                "expected_excerpt": "Stale claim",
                "replacement_lines": [phrase],
            }
            errors, valid = lint_memory_plan.lint_findings(
                self.inventory, [finding], workspace=self.base
            )
            self.assertEqual(valid, [], f"Failed to reject meta phrase: {phrase!r}")
            self.assertTrue(any("no-meta-instruction" in e for e in errors))

        indent_drop = {
            "id": "drop-indent",
            "file": "projects/new.md",
            "start_line": 5,
            "end_line": 5,
            "action": "update",
            "expected_excerpt": "Indented bullet",
            "replacement_lines": ["- Lost leading indentation"],
        }
        errors, _ = lint_memory_plan.lint_findings(
            self.inventory, [indent_drop], workspace=self.base
        )
        self.assertTrue(any("drops indentation" in e for e in errors))

        bullet_drop = {
            "id": "drop-bullet",
            "file": "projects/old.md",
            "start_line": 5,
            "end_line": 5,
            "action": "update",
            "expected_excerpt": "Stale claim",
            "replacement_lines": ["Plain sentence without bullet marker."],
        }
        errors, _ = lint_memory_plan.lint_findings(
            self.inventory, [bullet_drop], workspace=self.base
        )
        self.assertTrue(any("non-bullet prose" in e for e in errors))

    def test_rule5_direct_and_pinned_conservation(self) -> None:
        retire_pinned = {
            "id": "retire-prefs",
            "file": "preferences.md",
            "start_line": 1,
            "end_line": 5,
            "action": "retire",
            "expected_excerpt": "User prefs",
            "allow_drop_direct": True,
        }
        errors, _ = lint_memory_plan.lint_findings(
            self.inventory, [retire_pinned], workspace=self.base
        )
        self.assertTrue(any("cannot retire protected" in e for e in errors))

        drop_direct_line = {
            "id": "drop-direct",
            "file": "projects/old.md",
            "start_line": 6,
            "end_line": 6,
            "action": "delete",
            "expected_excerpt": "Direct rule",
        }
        errors, _ = lint_memory_plan.lint_findings(
            self.inventory, [drop_direct_line], workspace=self.base
        )
        self.assertTrue(any("drops #direct lines" in e for e in errors))

        routed_retire = [
            {
                "id": "route-line-6",
                "file": "projects/old.md",
                "start_line": 6,
                "end_line": 6,
                "action": "delete",
                "expected_excerpt": "Direct rule",
                "direct_migration_target": "projects/new.md",
            },
            {
                "id": "retire-old-ok",
                "file": "projects/old.md",
                "start_line": 1,
                "end_line": 6,
                "action": "retire",
                "expected_excerpt": "Old project",
                "link_rewrite_target": "projects/new.md",
            },
        ]
        errors, valid = lint_memory_plan.lint_findings(
            self.inventory, routed_retire, workspace=self.base
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(valid), 2)

    def test_rule6_promotion_allowlist_and_deduplication(self) -> None:
        findings = [
            {
                "id": "promo-1",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 5,
                "action": "delete",
                "verdict": "promote:agents_md",
                "expected_excerpt": "Stale claim",
                "promote_target": str(self.agents_md),
                "promote_section": "## General rules",
                "promote_lines": ["- Promoted rule from shard 1."],
            },
            {
                "id": "promo-dup",
                "file": "topics/extra.md",
                "start_line": 4,
                "end_line": 4,
                "action": "delete",
                "verdict": "promote:agents_md",
                "expected_excerpt": "Note one",
                "promote_target": str(self.agents_md),
                "promote_section": "## General rules",
                "promote_lines": ["- Promoted rule from shard 1."],
            },
            {
                "id": "promo-skill",
                "file": "projects/new.md",
                "start_line": 5,
                "end_line": 5,
                "action": "delete",
                "verdict": "promote:skill",
                "expected_excerpt": "Indented bullet",
                "promote_target": str(self.skill_md),
                "promote_section": "## Usage",
                "promote_lines": ["- Skill usage note."],
            },
            {
                "id": "promo-script",
                "file": "projects/old.md",
                "start_line": 6,
                "end_line": 6,
                "action": "delete",
                "verdict": "promote:script",
                "expected_excerpt": "Direct rule",
                "promote_target": str(self.base / "scripts" / "check.py"),
            },
        ]
        errors, valid = lint_memory_plan.lint_findings(
            self.inventory, findings, workspace=self.base
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(valid), 4)
        self.assertEqual(valid[0]["promote_lines"], ["- Promoted rule from shard 1."])
        self.assertEqual(valid[1]["promote_lines"], [])
        self.assertTrue(valid[1].get("promotion_deduplicated"))

        bad_promos = [
            {
                "id": "missing-sec",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 5,
                "action": "delete",
                "verdict": "promote:agents_md",
                "expected_excerpt": "Stale claim",
                "promote_target": str(self.agents_md),
                "promote_section": "## Nonexistent Section",
                "promote_lines": ["- Rule."],
            },
            {
                "id": "bad-script-outside",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 5,
                "action": "delete",
                "verdict": "promote:script",
                "expected_excerpt": "Stale claim",
                "promote_target": "/etc/passwd",
            },
        ]
        b_errors, _ = lint_memory_plan.lint_findings(
            self.inventory, bad_promos, workspace=self.base
        )
        self.assertEqual(len(b_errors), 2)

    def test_rule7_frontmatter_safe_and_cli(self) -> None:
        drop_key = {
            "id": "fm-drop-key",
            "file": "projects/old.md",
            "start_line": 2,
            "end_line": 3,
            "action": "update",
            "expected_excerpt": "Old project",
            "replacement_lines": ['description: "Updated only"'],
        }
        unquoted_desc = {
            "id": "fm-unquoted",
            "file": "projects/old.md",
            "start_line": 2,
            "end_line": 2,
            "action": "update",
            "expected_excerpt": "Old project",
            "replacement_lines": ["description: Unquoted description"],
        }
        good_fm = {
            "id": "fm-good",
            "file": "projects/old.md",
            "start_line": 2,
            "end_line": 2,
            "action": "update",
            "expected_excerpt": "Old project",
            "replacement_lines": ['description: "Properly quoted description"'],
        }
        errors, valid = lint_memory_plan.lint_findings(
            self.inventory, [drop_key, unquoted_desc, good_fm], workspace=self.base
        )
        self.assertEqual([f["id"] for f in valid], ["fm-good"])
        self.assertEqual(len(errors), 2)

        inv_path = self.base / "inv.json"
        inv_path.write_text(json.dumps(self.inventory), encoding="utf-8")
        findings_path = self.base / "findings.jsonl"
        findings_path.write_text(
            json.dumps(good_fm) + "\n" + json.dumps(unquoted_desc) + "\n",
            encoding="utf-8",
        )
        out_plan = self.base / "verified_plan.json"

        with mock.patch("sys.stderr", new_callable=io.StringIO):
            strict_rc = lint_memory_plan.main([
                "--inventory",
                str(inv_path),
                "--findings",
                str(findings_path),
                "--workspace",
                str(self.base),
                "--output-plan",
                str(out_plan),
            ])
        self.assertEqual(strict_rc, 1)

        with (
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            partial_rc = lint_memory_plan.main([
                "--inventory",
                str(inv_path),
                "--findings",
                str(findings_path),
                "--workspace",
                str(self.base),
                "--allow-partial",
                "--output-plan",
                str(out_plan),
            ])
        self.assertEqual(partial_rc, 0)
        plan_data = json.loads(out_plan.read_text(encoding="utf-8"))
        self.assertEqual(len(plan_data["findings"]), 1)

    def test_lint_edge_cases_and_json_loaders(self) -> None:
        (self.mem / "projects" / "hub.md").write_text(
            '---\ndescription: "Hub"\n---\n'
            "- Links to [old](old.md) and [extra](../topics/extra.md) on line 4\n",
            encoding="utf-8",
        )
        inv = scan_memories.build_inventory(
            self.mem, [], self.promotion_targets, scope="all"
        )
        edge_findings = [
            {
                "file": "",
                "start_line": 1,
                "end_line": 1,
                "action": "update",
                "expected_excerpt": "x",
            },
            {
                "id": "empty-rep",
                "file": "projects/old.md",
                "start_line": 5,
                "end_line": 5,
                "action": "update",
                "expected_excerpt": "Stale",
                "replacement_lines": [],
            },
            {
                "id": "preserve-direct-update",
                "file": "projects/old.md",
                "start_line": 6,
                "end_line": 6,
                "action": "update",
                "expected_excerpt": "Direct rule",
                "replacement_lines": ["- Updated direct rule #direct"],
            },
            {
                "id": "unrouted-retire",
                "file": "projects/old.md",
                "start_line": 1,
                "end_line": 6,
                "action": "retire",
                "expected_excerpt": "Old project",
            },
            {
                "id": "dup-retire-1",
                "file": "topics/extra.md",
                "start_line": 1,
                "end_line": 4,
                "action": "retire",
                "expected_excerpt": "Extra",
            },
            {
                "id": "dup-retire-2",
                "file": "topics/extra.md",
                "start_line": 1,
                "end_line": 4,
                "action": "retire",
                "expected_excerpt": "Extra",
            },
            {
                "id": "bad-fm-break",
                "file": "projects/new.md",
                "start_line": 1,
                "end_line": 1,
                "action": "update",
                "expected_excerpt": "---",
                "replacement_lines": ["not-delimiter"],
            },
            {
                "id": "bad-promo-empty-target",
                "file": "projects/new.md",
                "start_line": 5,
                "end_line": 5,
                "action": "delete",
                "verdict": "promote:agents_md",
                "expected_excerpt": "Indented",
            },
            {
                "id": "bad-promo-empty-lines",
                "file": "projects/new.md",
                "start_line": 5,
                "end_line": 5,
                "action": "delete",
                "verdict": "promote:agents_md",
                "expected_excerpt": "Indented",
                "promote_target": str(self.agents_md),
                "promote_lines": [],
            },
        ]
        errors, _ = lint_memory_plan.lint_findings(
            inv, edge_findings, workspace=self.base
        )
        self.assertGreaterEqual(len(errors), 7)

        arr_file = self.base / "arr.json"
        arr_file.write_text(json.dumps([edge_findings[2]]), encoding="utf-8")
        bad_jsonl = self.base / "bad.jsonl"
        bad_jsonl.write_text("{bad json\n123\n", encoding="utf-8")
        recs, p_errs = lint_memory_plan._load_findings_files([
            str(arr_file),
            str(bad_jsonl),
            str(self.base / "missing.jsonl"),
        ])
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(p_errs), 3)

        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(
                lint_memory_plan.main([
                    "--inventory",
                    str(self.base / "no_inv.json"),
                    "--findings",
                    str(arr_file),
                    "--output-plan",
                    str(self.base / "out.json"),
                ]),
                1,
            )


class ApplyMemoryPlanTests(unittest.TestCase):
    """Tests for `apply_memory_plan.py` patching, rebasing, backup, and rollback."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp_dir.name)
        self.mem = self.base / "memory"
        self.backups = self.base / "backups"
        (self.mem / "projects").mkdir(parents=True)

        self.agents_md = self.base / "AGENTS.md"
        self.agents_md.write_text(
            "# Rules\n\n## General rules\n\n- Keep tests green.\n\n## Extra\n\n- End.\n",
            encoding="utf-8",
        )
        self.old_md = self.mem / "projects" / "old.md"
        self.old_md.write_text(
            '---\ndescription: "Old"\n---\n'
            "- Stale line 4\n"
            "- Direct rule on line 5 #direct\n",
            encoding="utf-8",
        )
        self.new_md = self.mem / "projects" / "new.md"
        self.new_md.write_text(
            '---\ndescription: "New"\n---\n'
            "- Link to [old](old.md#sec) here\n"
            "- Second bullet on line 5\n",
            encoding="utf-8",
        )

        self.promotion_targets = {
            "global_instructions": None,
            "workspace_instructions": str(self.agents_md),
            "writable_skill_dirs": [],
        }
        self.inventory = scan_memories.build_inventory(
            self.mem, [], self.promotion_targets, scope="all"
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _build_plan(self, findings: list[dict[str, object]]) -> dict[str, object]:
        errors, valid = lint_memory_plan.lint_findings(
            self.inventory, findings, workspace=self.base
        )
        self.assertEqual(errors, [])
        return lint_memory_plan.build_verified_plan(self.inventory, valid)

    def test_apply_retire_link_rewrite_migration_and_restore(self) -> None:
        findings = [
            {
                "id": "f-retire-old",
                "file": "projects/old.md",
                "start_line": 1,
                "end_line": 5,
                "action": "retire",
                "verdict": "promote:agents_md",
                "expected_excerpt": "Old",
                "direct_migration_target": "projects/new.md",
                "link_rewrite_target": "projects/new.md",
                "promote_target": str(self.agents_md),
                "promote_section": "## General rules",
                "promote_lines": ["- Promoted rule from old.md."],
            },
            {
                "id": "f-update-new",
                "file": "projects/new.md",
                "start_line": 5,
                "end_line": 5,
                "action": "update",
                "expected_excerpt": "Second bullet",
                "replacement_lines": ["- Updated second bullet on line 5"],
            },
        ]
        plan = self._build_plan(findings)

        dry_res = apply_memory_plan.apply_verified_plan(
            plan, None, self.backups, dry_run=True
        )
        self.assertTrue(dry_res["ok"])
        self.assertTrue(self.old_md.exists())

        res = apply_memory_plan.apply_verified_plan(
            plan, None, self.backups, max_backups=2, dry_run=False
        )
        self.assertTrue(res["ok"])
        self.assertFalse(self.old_md.exists())
        new_text = self.new_md.read_text(encoding="utf-8")
        self.assertIn("[old](new.md#sec)", new_text)
        self.assertIn("- Direct rule on line 5 #direct", new_text)
        self.assertIn("- Updated second bullet on line 5", new_text)
        agents_text = self.agents_md.read_text(encoding="utf-8")
        self.assertIn("- Promoted rule from old.md.", agents_text)

        plan_path = self.base / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out_buf:
            list_rc = apply_memory_plan.main([
                "--backup-root",
                str(self.backups),
                "--list-backups",
            ])
        self.assertEqual(list_rc, 0)
        listed = json.loads(out_buf.getvalue())
        self.assertEqual(len(listed["backups"]), 1)
        backup_id = listed["backups"][0]["backup_id"]

        with mock.patch("sys.stdout", new_callable=io.StringIO):
            rest_rc = apply_memory_plan.main([
                "--backup-root",
                str(self.backups),
                "--restore",
                backup_id,
            ])
        self.assertEqual(rest_rc, 0)
        self.assertTrue(self.old_md.exists())
        self.assertNotIn("- Promoted rule from old.md.", self.agents_md.read_text(encoding="utf-8"))

    def test_rebase_non_overlapping_and_skip_two_legged_conflict(self) -> None:
        findings = [
            {
                "id": "f-rebase",
                "file": "projects/new.md",
                "start_line": 5,
                "end_line": 5,
                "action": "update",
                "verdict": "promote:agents_md",
                "expected_excerpt": "Second bullet",
                "replacement_lines": ["- Rebased bullet line"],
                "promote_target": str(self.agents_md),
                "promote_section": "## General rules",
                "promote_lines": ["- Rebased promotion line."],
            },
            {
                "id": "f-conflict",
                "file": "projects/old.md",
                "start_line": 4,
                "end_line": 4,
                "action": "delete",
                "verdict": "promote:agents_md",
                "expected_excerpt": "Stale line 4",
                "promote_target": str(self.agents_md),
                "promote_section": "## General rules",
                "promote_lines": ["- Conflicted promotion line MUST NOT appear."],
            },
        ]
        plan = self._build_plan(findings)

        # Non-overlapping insertion above line 5 in new.md:
        self.new_md.write_text(
            '---\ndescription: "New"\n---\n'
            "- Inserted concurrent line\n"
            "- Link to [old](old.md#sec) here\n"
            "- Second bullet on line 5\n",
            encoding="utf-8",
        )
        # Overlapping mutation on line 4 in old.md:
        self.old_md.write_text(
            '---\ndescription: "Old"\n---\n'
            "- Concurrently rewritten line 4\n"
            "- Direct rule on line 5 #direct\n",
            encoding="utf-8",
        )

        res = apply_memory_plan.apply_verified_plan(
            plan, None, self.backups, dry_run=False
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["rebased_ids"], ["f-rebase"])
        self.assertEqual(res["skipped_conflicts"], ["f-conflict"])
        self.assertIn("- Rebased bullet line", self.new_md.read_text(encoding="utf-8"))
        agents_after = self.agents_md.read_text(encoding="utf-8")
        self.assertIn("- Rebased promotion line.", agents_after)
        self.assertNotIn("Conflicted promotion line", agents_after)

    def test_automatic_two_legged_rollback_on_erofs(self) -> None:
        findings = [
            {
                "id": "f-erofs",
                "file": "projects/old.md",
                "start_line": 4,
                "end_line": 4,
                "action": "update",
                "verdict": "promote:agents_md",
                "expected_excerpt": "Stale line 4",
                "replacement_lines": ["- Updated line 4 before EROFS"],
                "promote_target": str(self.agents_md),
                "promote_section": "## General rules",
                "promote_lines": ["- Promoted line before EROFS."],
            }
        ]
        plan = self._build_plan(findings)
        orig_agents = self.agents_md.read_text(encoding="utf-8")
        orig_old = self.old_md.read_text(encoding="utf-8")

        with mock.patch.object(
            apply_memory_plan,
            "_verify_written_frontmatter",
            side_effect=OSError(errno.EROFS, "Read-only file system"),
        ):
            res = apply_memory_plan.apply_verified_plan(
                plan, None, self.backups, dry_run=False
            )

        self.assertFalse(res["ok"])
        self.assertTrue(res["rolled_back"])
        self.assertEqual(res["status"], "rolled_back")
        self.assertIn("EROFS", res["errors"][0])
        self.assertEqual(self.agents_md.read_text(encoding="utf-8"), orig_agents)
        self.assertEqual(self.old_md.read_text(encoding="utf-8"), orig_old)

    def test_backup_pruning_and_cli_error_branches(self) -> None:
        findings = [
            {
                "id": "f-prune",
                "file": "projects/old.md",
                "start_line": 4,
                "end_line": 4,
                "action": "update",
                "expected_excerpt": "Stale line 4",
                "replacement_lines": ["- Prune test line"],
            }
        ]
        plan = self._build_plan(findings)
        b1 = apply_memory_plan.create_backup_snapshot(
            plan, plan["findings"], self.backups, max_backups=1
        )
        b2 = apply_memory_plan.create_backup_snapshot(
            plan, plan["findings"], self.backups, max_backups=1
        )
        self.assertFalse(b1.exists())
        self.assertTrue(b2.exists())

        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(apply_memory_plan.main([]), 2)
            self.assertEqual(
                apply_memory_plan.main(["--plan", str(self.base / "nope.json")]), 1
            )
            self.assertEqual(
                apply_memory_plan.main([
                    "--backup-root",
                    str(self.backups),
                    "--restore",
                    "nonexistent-id",
                ]),
                1,
            )

    def test_apply_containment_rollback_and_cli_modes(self) -> None:
        bad_plan = {
            "schema_version": 1,
            "memory_root": str(self.mem),
            "skipped_mounts": [],
            "promotion_targets": self.promotion_targets,
            "findings": [
                {
                    "id": "bad-escape",
                    "file": "../outside.md",
                    "start_line": 1,
                    "end_line": 1,
                    "action": "delete",
                    "promote_target": "/etc/passwd",
                }
            ],
            "files": {},
            "target_files": {},
        }
        res = apply_memory_plan.apply_verified_plan(
            bad_plan, {"bad-escape"}, self.backups
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "error")
        self.assertEqual(len(res["errors"]), 2)

        # Test frontmatter post-write verification failure triggering rollback:
        findings = [
            {
                "id": "f-cli",
                "file": "projects/new.md",
                "start_line": 5,
                "end_line": 5,
                "action": "update",
                "verdict": "promote:agents_md",
                "expected_excerpt": "Second bullet",
                "replacement_lines": ["- Updated via CLI"],
                "promote_target": str(self.agents_md),
                "promote_section": "## Extra",
                "promote_lines": ["- Appended under last section."],
            }
        ]
        plan = self._build_plan(findings)
        plan_file = self.base / "cli_plan.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")

        with mock.patch("sys.stdout", new_callable=io.StringIO):
            rc = apply_memory_plan.main([
                "--plan",
                str(plan_file),
                "--approve-ids",
                "f-cli",
                "--backup-root",
                str(self.backups),
            ])
        self.assertEqual(rc, 0)
        self.assertIn("- Updated via CLI", self.new_md.read_text(encoding="utf-8"))
        self.assertIn(
            "- Appended under last section.",
            self.agents_md.read_text(encoding="utf-8"),
        )

        # Corrupt frontmatter verification check directly:
        corrupt_md = self.base / "corrupt.md"
        corrupt_md.write_text("no frontmatter here\n", encoding="utf-8")
        with self.assertRaises(apply_memory_plan.PlanVerificationError):
            apply_memory_plan._verify_written_frontmatter(corrupt_md, {"description"})
        missing_key_md = self.base / "missing_key.md"
        missing_key_md.write_text('---\ndescription: "ok"\n---\n', encoding="utf-8")
        with self.assertRaises(apply_memory_plan.PlanVerificationError):
            apply_memory_plan._verify_written_frontmatter(
                missing_key_md, {"description", "confidence"}
            )


if __name__ == "__main__":
    unittest.main()
