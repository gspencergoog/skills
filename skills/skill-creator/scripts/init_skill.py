#!/usr/bin/env python3
"""
init_skill.py - Skill Directory Initializer

Generates a new Agent Skill directory template with SKILL.md.
Optionally creates resource subdirectories (scripts/, references/, assets/)
only when requested via command-line flags.
"""

import sys
import argparse
from pathlib import Path
from typing import List, Optional


def create_skill_template(
    skill_name: str,
    output_dir: Path,
    include_scripts: bool = False,
    include_references: bool = False,
    include_assets: bool = False,
) -> Path:
    """Creates a skill directory and SKILL.md template with optional resource subdirectories."""
    # Ensure skill_name is clean slug
    skill_name = skill_name.strip()
    if output_dir.name == skill_name:
        target_dir = output_dir
    else:
        target_dir = output_dir / skill_name
    target_dir.mkdir(parents=True, exist_ok=True)

    skill_md_path = target_dir / "SKILL.md"

    # Only write SKILL.md if it doesn't already exist
    if not skill_md_path.exists():
        content = f"""---
name: {skill_name}
description: Write a clear description of what this skill does and specific triggers/contexts for when an agent should use it.
---

# {skill_name.replace('-', ' ').title()}

Provide clear, procedural instructions for an agent executing this skill.

## Workflow

1. Step 1: Initialize process.
2. Step 2: Perform core operations.
"""
        skill_md_path.write_text(content, encoding="utf-8")

    # Handle optional subdirectories
    if include_scripts:
        (target_dir / "scripts").mkdir(exist_ok=True)
    if include_references:
        (target_dir / "references").mkdir(exist_ok=True)
    if include_assets:
        (target_dir / "assets").mkdir(exist_ok=True)

    return target_dir


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize a new Agent Skill directory template.")
    parser.add_argument("name", help="Name of the skill (e.g. my-skill)")
    parser.add_argument(
        "--path",
        default=".",
        help="Target output directory (defaults to current directory)",
    )
    parser.add_argument(
        "--scripts",
        action="store_true",
        help="Create optional scripts/ subdirectory",
    )
    parser.add_argument(
        "--references",
        action="store_true",
        help="Create optional references/ subdirectory",
    )
    parser.add_argument(
        "--assets",
        action="store_true",
        help="Create optional assets/ subdirectory",
    )

    args = parser.parse_args(argv)

    output_path = Path(args.path).resolve()
    created_dir = create_skill_template(
        skill_name=args.name,
        output_dir=output_path,
        include_scripts=args.scripts,
        include_references=args.references,
        include_assets=args.assets,
    )

    print(f"Successfully initialized skill '{args.name}' at: {created_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
