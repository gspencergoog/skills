"""Unit tests for packrat self-contained in-memory blob parsers."""

from __future__ import annotations

import unittest

from blob_parsers import (
    extract_content_entities,
    parse_markdown_sections,
)


class TestBlobParsers(unittest.TestCase):
    """Tests in-memory Markdown section parsing and AST/regex symbol extraction."""

    def test_markdown_frontmatter_and_atx_headings(self) -> None:
        md = (
            "---\n"
            "title: Sample Document\n"
            "---\n"
            "Leading preamble paragraph.\n"
            "# First Level\n"
            "Body under first level.\n"
            "## Second Level\n"
            "Body under second level.\n"
            "### Third Level\n"
            "Deepest content.\n"
        )
        sections = parse_markdown_sections("sample.md", md.splitlines())

        # Expect preamble, level 1, level 2, and level 3 sections
        self.assertEqual(len(sections), 4)

        preamble = sections[0]
        self.assertEqual(preamble["level"], 0)
        self.assertEqual(preamble["heading"], "(preamble)")
        self.assertEqual(preamble["breadcrumb"], "(preamble)")
        self.assertEqual(preamble["line_start"], 4)
        self.assertEqual(preamble["line_end"], 4)

        sec1 = sections[1]
        self.assertEqual(sec1["level"], 1)
        self.assertEqual(sec1["heading"], "First Level")
        self.assertEqual(sec1["breadcrumb"], "First Level")
        self.assertEqual(sec1["anchor_slug"], "first-level")
        self.assertEqual(sec1["line_start"], 5)
        # Subtree should extend through the end of the file since all subsequent headings are deeper (levels 2 and 3)
        self.assertEqual(sec1["subtree_line_end"], 10)

        sec2 = sections[2]
        self.assertEqual(sec2["level"], 2)
        self.assertEqual(sec2["heading"], "Second Level")
        self.assertEqual(sec2["breadcrumb"], "First Level > Second Level")
        self.assertEqual(sec2["anchor_slug"], "second-level")
        self.assertEqual(sec2["line_start"], 7)
        self.assertEqual(sec2["subtree_line_end"], 10)

        sec3 = sections[3]
        self.assertEqual(sec3["level"], 3)
        self.assertEqual(sec3["heading"], "Third Level")
        self.assertEqual(sec3["breadcrumb"], "First Level > Second Level > Third Level")
        self.assertEqual(sec3["anchor_slug"], "third-level")
        self.assertEqual(sec3["line_start"], 9)
        self.assertEqual(sec3["line_end"], 10)

    def test_markdown_setext_headings(self) -> None:
        md = (
            "Setext Level One\n"
            "================\n"
            "Paragraph one.\n"
            "Setext Level Two\n"
            "----------------\n"
            "Paragraph two.\n"
        )
        sections = parse_markdown_sections("setext.md", md.splitlines())
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["level"], 1)
        self.assertEqual(sections[0]["heading"], "Setext Level One")
        self.assertEqual(sections[1]["level"], 2)
        self.assertEqual(sections[1]["heading"], "Setext Level Two")

    def test_markdown_ignores_headings_inside_fenced_code_blocks(self) -> None:
        md = (
            "# Real Heading\n"
            "```markdown\n"
            "# Fake Heading Inside Code\n"
            "## Another Fake One\n"
            "```\n"
            "## Real Subheading\n"
        )
        sections = parse_markdown_sections("code.md", md.splitlines())
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["heading"], "Real Heading")
        self.assertEqual(sections[1]["heading"], "Real Subheading")

    def test_markdown_empty_or_no_headings(self) -> None:
        self.assertEqual(parse_markdown_sections("empty.md", []), [])
        preamble_only = parse_markdown_sections("text.md", ["Just plain text."])
        self.assertEqual(len(preamble_only), 1)
        self.assertEqual(preamble_only[0]["level"], 0)
        self.assertEqual(preamble_only[0]["heading"], "(preamble)")

    def test_extract_python_entities(self) -> None:
        code = (
            "class EngineManager:\n"
            "    def start(self, timeout: int = 30) -> bool:\n"
            "        return True\n"
            "\n"
            "    async def stop(self) -> None:\n"
            "        pass\n"
            "\n"
            "def calculate_total(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        entities = extract_content_entities("engine.py", "NEW", code)
        self.assertEqual(len(entities), 4)

        cls_ent = entities[0]
        self.assertEqual(cls_ent["kind"], "class")
        self.assertEqual(cls_ent["name"], "EngineManager")
        self.assertEqual(cls_ent["signature"], "class EngineManager")
        self.assertEqual(cls_ent["line_start"], 1)

        m1_ent = entities[1]
        self.assertEqual(m1_ent["kind"], "method")
        self.assertEqual(m1_ent["name"], "start")
        self.assertTrue(m1_ent["signature"].startswith("def start(self, timeout: int"))
        self.assertTrue(m1_ent["signature"].endswith(") -> bool"))

        m2_ent = entities[2]
        self.assertEqual(m2_ent["kind"], "method")
        self.assertEqual(m2_ent["name"], "stop")
        self.assertIn("async def stop(self) -> None", m2_ent["signature"])

        fn_ent = entities[3]
        self.assertEqual(fn_ent["kind"], "function")
        self.assertEqual(fn_ent["name"], "calculate_total")
        self.assertIn("def calculate_total(a: int, b: int) -> int", fn_ent["signature"])
        self.assertEqual(extract_content_entities("engine.py", code), entities)

    def test_extract_python_syntax_error_returns_empty_list(self) -> None:
        code = "def broken(:\n    pass\n"
        entities = extract_content_entities("broken.py", "NEW", code)
        self.assertEqual(entities, [])

    def test_extract_brace_entities_and_ignores_control_keywords(self) -> None:
        code = (
            "export class SurfaceRenderer {\n"
            "  public renderSurface(id: string): void {\n"
            "    if (id.length > 0) {\n"
            "      console.log(id);\n"
            "    }\n"
            "  }\n"
            "}\n"
            "\n"
            "function computeHash(data: string): number {\n"
            "  for (let i = 0; i < data.length; i++) {\n"
            "    // loop\n"
            "  }\n"
            "  return 42;\n"
            "}\n"
        )
        entities = extract_content_entities("renderer.ts", "NEW", code)
        # Should match class SurfaceRenderer, method renderSurface, function computeHash.
        # Should NOT match control keywords 'if' or 'for'.
        kinds_and_names = [(e["kind"], e["name"]) for e in entities]
        self.assertEqual(
            kinds_and_names,
            [
                ("class", "SurfaceRenderer"),
                ("method", "renderSurface"),
                ("function", "computeHash"),
            ],
        )
        self.assertEqual(entities[0]["line_start"], 1)
        self.assertEqual(entities[0]["line_end"], 7)
        self.assertEqual(entities[1]["line_start"], 2)
        self.assertEqual(entities[1]["line_end"], 6)
        self.assertEqual(entities[2]["line_start"], 9)
        self.assertEqual(entities[2]["line_end"], 14)


if __name__ == "__main__":
    unittest.main()
