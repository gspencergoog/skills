#!/usr/bin/env python3
"""
lint_skill.py - Skill File Linter and Validator

Validates Agent Skill files (SKILL.md) and directory structures against
the Agent Skills specification.
"""

import sys
import os
import re
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any, Set

DISALLOWED_CLUTTER_FILES = {
    "installation_guide.md",
    "quick_reference.md",
    "changelog.md",
    "setup.md",
    ".ds_store",
}

ALLOWED_SUBDIRECTORIES = {
    "scripts",
    "references",
    "assets",
    "evals",
    "tests",
    "agents",
    "examples",
    "sidecar",
    "eval-viewer",
    "__pycache__",
}

ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "compatibility",
    "metadata",
    "version",
    "license",
    "key_features",
    "author",
    "tags",
}


class ValidationIssue:
    def __init__(self, is_error: bool, path: Path, message: str, line_num: Optional[int] = None):
        self.is_error = is_error
        self.path = path
        self.message = message
        self.line_num = line_num

    def __str__(self) -> str:
        prefix = "ERROR" if self.is_error else "WARNING"
        location = f"{self.path}"
        if self.line_num is not None:
            location += f":{self.line_num}"
        return f"[{prefix}] {location}: {self.message}"


def parse_yaml_frontmatter(content: str) -> Tuple[Optional[Dict[str, str]], str, Optional[str]]:
    """
    Parses YAML frontmatter enclosed by '---' markers.
    Returns (frontmatter_dict, markdown_body, parse_error_message).
    """
    if not content.startswith("---"):
        return None, content, "File does not start with YAML frontmatter marker '---'."

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content, "Unclosed YAML frontmatter marker '---'."

    raw_yaml = parts[1]
    body = parts[2]

    metadata: Dict[str, str] = {}
    current_key: Optional[str] = None
    current_val_lines: List[str] = []

    for line in raw_yaml.splitlines():
        # Skip empty lines or comments in YAML block
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Check for new key at root level (not indented)
        key_match = re.match(r"^([a-zA-Z0-9_-]+)\s*:\s*(.*)$", line)
        if key_match and not line.startswith(" ") and not line.startswith("\t"):
            if current_key:
                metadata[current_key] = "\n".join(current_val_lines).strip()
            current_key = key_match.group(1)
            val_part = key_match.group(2).strip()
            current_val_lines = [val_part] if val_part else []
        elif current_key is not None:
            # Continuation of multi-line key value
            current_val_lines.append(line.strip())

    if current_key:
        metadata[current_key] = "\n".join(current_val_lines).strip()

    # Clean quotes from values if present
    for k, v in metadata.items():
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            metadata[k] = v[1:-1].strip()

    return metadata, body, None


def check_exact_case_path_exists(base_dir: Path, rel_target: str) -> bool:
    """Verifies that rel_target exists relative to base_dir with exact case matching across all platforms."""
    target_path = (base_dir / rel_target).resolve()
    if not target_path.exists():
        return False
    try:
        anchor = target_path.anchor
        rel_parts = target_path.relative_to(anchor).parts
        current = Path(anchor)
        for part in rel_parts:
            entries = [child.name for child in current.iterdir()]
            if part not in entries:
                return False
            current = current / part
        return True
    except Exception:
        return False


def validate_skill_file(skill_md_path: Path) -> List[ValidationIssue]:
    """Validates a single SKILL.md file and its containing directory."""
    issues: List[ValidationIssue] = []

    if not skill_md_path.exists() or not skill_md_path.is_file():
        issues.append(ValidationIssue(True, skill_md_path, "File does not exist or is not a regular file."))
        return issues

    skill_dir = skill_md_path.parent
    folder_name = skill_dir.name

    try:
        with open(skill_md_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        issues.append(ValidationIssue(True, skill_md_path, f"Failed to read file: {e}"))
        return issues

    # 1. Frontmatter Validation
    metadata, body, parse_err = parse_yaml_frontmatter(content)
    if parse_err:
        issues.append(ValidationIssue(True, skill_md_path, parse_err, line_num=1))
    elif metadata is not None:
        # Check unrecognized keys
        extra_keys = set(metadata.keys()) - ALLOWED_FRONTMATTER_KEYS
        if extra_keys:
            issues.append(
                ValidationIssue(
                    True,
                    skill_md_path,
                    f"Disallowed keys in YAML frontmatter: {', '.join(sorted(extra_keys))}. Only 'name' and 'description' are allowed.",
                )
            )

        # Validate 'name'
        name = metadata.get("name")
        if not name:
            issues.append(ValidationIssue(True, skill_md_path, "Frontmatter is missing required 'name' field."))
        else:
            if name != folder_name:
                issues.append(
                    ValidationIssue(
                        True,
                        skill_md_path,
                        f"Frontmatter name '{name}' does not match skill directory name '{folder_name}'.",
                    )
                )

        # Validate 'description'
        description = metadata.get("description")
        if not description:
            issues.append(ValidationIssue(True, skill_md_path, "Frontmatter is missing required 'description' field."))
        elif len(description.strip()) < 10:
            issues.append(ValidationIssue(True, skill_md_path, "Frontmatter 'description' is too short (must be >= 10 characters)."))

    # 2. Directory Clutter & Structure Validation
    if skill_dir.exists() and skill_dir.is_dir():
        for child in skill_dir.iterdir():
            child_name_lower = child.name.lower()
            if child.is_file():
                if child_name_lower in DISALLOWED_CLUTTER_FILES:
                    issues.append(
                        ValidationIssue(
                            True,
                            child,
                            f"Disallowed clutter file '{child.name}' in skill directory. Skills should not contain auxiliary documentation files.",
                        )
                    )
            elif child.is_dir():
                if child.name.lower() not in ALLOWED_SUBDIRECTORIES and not child.name.startswith("."):
                    issues.append(
                        ValidationIssue(
                            False,
                            child,
                            f"Unrecognized subdirectory '{child.name}'. Standard skill subdirectories are: scripts/, references/, assets/.",
                        )
                    )

    # 3. Markdown Content & Links Validation
    if body:
        line_count = len(body.splitlines())
        if line_count > 500:
            issues.append(
                ValidationIssue(
                    False,
                    skill_md_path,
                    f"SKILL.md body is {line_count} lines. Recommendation is under 500 lines to avoid context window bloat.",
                )
            )

        # Find relative markdown links ](target)
        link_pattern = re.compile(r'\]\(([^)]+)\)')
        in_code_block = False
        start_line_offset = content.count('\n', 0, content.find(body) if body in content else 0) + 1

        for idx, line in enumerate(body.splitlines()):
            line_num = start_line_offset + idx
            stripped_line = line.strip()
            if stripped_line.startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue

            line_no_inline_code = re.sub(r'`[^`]+`', '', line)
            for match in link_pattern.finditer(line_no_inline_code):
                link_target = match.group(1).split('#')[0].strip()
                if not link_target or link_target.startswith(('http://', 'https://', 'mailto:', 'file://')):
                    continue
                target_path = (skill_dir / link_target).resolve()
                if not check_exact_case_path_exists(skill_dir, link_target):
                    issues.append(
                        ValidationIssue(
                            True,
                            skill_md_path,
                            f"Broken relative link '{link_target}' target not found or case mismatch at '{target_path}'.",
                            line_num=line_num,
                        )
                    )

    # 4. Check references/ discovery
    refs_dir = skill_dir / "references"
    if refs_dir.exists() and refs_dir.is_dir():
        for ref_file in refs_dir.glob("*.md"):
            rel_path_str = f"references/{ref_file.name}"
            if rel_path_str not in content and ref_file.name not in content:
                issues.append(
                    ValidationIssue(
                        False,
                        skill_md_path,
                        f"Reference file '{rel_path_str}' is present in references/ but not referenced or mentioned in SKILL.md.",
                    )
                )

    return issues


def find_skill_md_files(target: Path) -> List[Path]:
    """Finds all SKILL.md files given a file or directory target."""
    if target.is_file():
        if target.name == "SKILL.md":
            return [target]
        return []

    if target.is_dir():
        if (target / "SKILL.md").exists():
            return [target / "SKILL.md"]

        # Search subdirectories for SKILL.md
        found = list(target.glob("*/SKILL.md"))
        if not found:
            found = list(target.glob("**/SKILL.md"))
        return sorted(found)

    return []


def lint_skills(target_path: Path) -> int:
    """Main linting function for CLI entrypoint."""
    skill_files = find_skill_md_files(target_path)
    if not skill_files:
        print(f"No SKILL.md files found at target: {target_path}")
        return 1

    total_errors = 0
    total_warnings = 0

    print(f"Linting {len(skill_files)} skill file(s)...\n")

    for skill_file in skill_files:
        issues = validate_skill_file(skill_file)
        errors = [i for i in issues if i.is_error]
        warnings = [i for i in issues if not i.is_error]

        total_errors += len(errors)
        total_warnings += len(warnings)

        if issues:
            for issue in issues:
                print(issue)

    print("\n--- Linting Summary ---")
    print(f"Skills Checked: {len(skill_files)}")
    print(f"Errors: {total_errors}")
    print(f"Warnings: {total_warnings}")

    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    sys.exit(lint_skills(target))
