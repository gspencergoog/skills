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
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}

# Description quality thresholds. The description is injected into every
# system prompt, so it must be long enough to route on and short enough to
# fit the 1024-character platform limit with room to spare.
DESCRIPTION_MAX_CHARS = 1024
DESCRIPTION_SOFT_MIN = 200
DESCRIPTION_SOFT_MAX = 800

TRIGGER_RE = re.compile(r"\buse when\b", re.IGNORECASE)
SECOND_PERSON_RE = re.compile(r"\b(you|your|yours|I|we|us|our)\b")
QUOTED_SPAN_RE = re.compile(r'"[^"]*"')
FLAG_GLOSS_RE = re.compile(r"--[A-Za-z][\w-]*[^()\n]{0,20}\(")
BODY_WHEN_TO_USE_RE = re.compile(r"^#{1,6}\s+when to use\b", re.IGNORECASE | re.MULTILINE)
NON_VERB_OPENERS = {
    "a", "an", "the", "this", "guide", "guidelines", "best", "framework",
    "comprehensive", "unified", "safe", "master",
}
PUFFERY_RE = re.compile(
    r"\b(?:comprehensive|robust|seamless(?:ly)?|leverage[sd]?|delve[sd]?|powerful|"
    r"cutting-edge|master|effective|high-quality|high-signal|professional|"
    r"state-of-the-art|best-in-class)\b|\bultra-",
    re.IGNORECASE,
)
EM_DASH = "\u2014"

UNQUOTED_COLON_HINT = (
    "unquoted string contains ': '. "
    "Strings containing colons must be enclosed in quotes ('...' or \"...\") "
    "or formatted as a block scalar ('>-' or '|-')."
)


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


def parse_yaml_frontmatter(content: str) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
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

    # 1. Use PyYAML if available
    try:
        import yaml
        try:
            parsed = yaml.safe_load(raw_yaml)
            if parsed is None:
                return {}, body, None
            if not isinstance(parsed, dict):
                return None, body, "Frontmatter must be a YAML dictionary mapping."
            result: Dict[str, Any] = {}
            for k, v in parsed.items():
                if isinstance(v, (str, int, float, bool)):
                    result[str(k)] = str(v)
                else:
                    result[str(k)] = v
            return result, body, None
        except yaml.YAMLError as e:
            if "mapping values are not allowed" in str(e):
                # Match the fallback parser's hint so both branches report the
                # same actionable message for an unquoted ': ' in a value.
                return None, body, f"Invalid YAML syntax in frontmatter: {UNQUOTED_COLON_HINT}"
            return None, body, f"Invalid YAML in frontmatter: {e}"
    except ImportError:
        pass

    # 2. Strict built-in zero-dependency YAML parser
    metadata: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_val_lines: List[str] = []
    is_block_scalar = False
    is_quoted = False
    is_nested = False

    def finish_key() -> Optional[str]:
        nonlocal current_key, current_val_lines, is_block_scalar, is_quoted, is_nested
        if not current_key:
            return None
        val = "\n".join(current_val_lines).strip()
        if not is_block_scalar and not is_quoted and not is_nested:
            if re.search(r":\s", val):
                return f"Invalid YAML syntax in field '{current_key}': {UNQUOTED_COLON_HINT}"
        metadata[current_key] = val
        return None

    for line in raw_yaml.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Check for new key at root level (not indented)
        key_match = re.match(r"^([a-zA-Z0-9_-]+)\s*:\s*(.*)$", line)
        if key_match and not line.startswith(" ") and not line.startswith("\t"):
            err = finish_key()
            if err:
                return None, body, err

            current_key = key_match.group(1)
            val_part = key_match.group(2).strip()
            is_block_scalar = val_part in (">-", ">", "|-", "|")
            is_quoted = (val_part.startswith('"') and val_part.endswith('"') and len(val_part) >= 2) or (
                val_part.startswith("'") and val_part.endswith("'") and len(val_part) >= 2
            )
            is_nested = (val_part == "")

            if not is_block_scalar and not is_quoted and val_part != "":
                if re.search(r":\s", val_part):
                    return (
                        None,
                        body,
                        f"Invalid YAML syntax in field '{current_key}': {UNQUOTED_COLON_HINT}",
                    )

            current_val_lines = [val_part] if (val_part and not is_block_scalar) else []
        elif current_key is not None:
            if line.startswith(" ") or line.startswith("\t"):
                if stripped.startswith("- ") or re.match(r"^[a-zA-Z0-9_-]+\s*:", stripped):
                    if is_nested:
                        pass
                    elif not is_block_scalar and not is_quoted:
                        if re.search(r":\s", stripped):
                            return (
                                None,
                                body,
                                f"Invalid YAML syntax in field '{current_key}': unquoted multiline string contains ': '. "
                                "Use block scalar ('>-' or '|-') or quotes.",
                            )
            current_val_lines.append(line.strip())

    err = finish_key()
    if err:
        return None, body, err

    # Clean quotes from values if present
    for k, v in list(metadata.items()):
        if isinstance(v, str):
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                metadata[k] = v[1:-1].strip()

    return metadata, body, None


def description_scalar_style(raw_yaml: str) -> str:
    """Returns how the raw `description:` value is written: '>-', '>', '|', '|-', 'quoted', 'plain', or 'missing'.

    The parsed dict loses this, and only `>-` both folds safely and survives
    an unquoted ': ' inside the text, so the linter wants the raw form.
    """
    for line in raw_yaml.splitlines():
        match = re.match(r"^description\s*:\s*(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value in (">-", ">", "|", "|-", ">+", "|+"):
            return value
        if value.startswith(('"', "'")):
            return "quoted"
        return "plain"
    return "missing"


def _opener_is_third_person(description: str) -> bool:
    """Loose voice heuristic: a third-person present-tense verb ends in 's' and is not a known noun opener."""
    first = re.match(r"[A-Za-z][\w-]*", description.strip())
    if not first:
        return False
    word = first.group(0)
    if word.lower() in NON_VERB_OPENERS:
        return False
    return word.endswith("s")


def check_description_quality(
    description: str, raw_yaml: str, body: str, skill_md_path: Path
) -> List[ValidationIssue]:
    """Checks the description against the house trigger-contract template.

    Everything except the hard 1024-character limit is a warning: the
    description is prose, and these heuristics cannot parse grammar.
    """
    issues: List[ValidationIssue] = []

    def warn(message: str) -> None:
        issues.append(ValidationIssue(False, skill_md_path, message))

    length = len(description)
    if length >= DESCRIPTION_MAX_CHARS:
        issues.append(
            ValidationIssue(
                True,
                skill_md_path,
                f"Frontmatter 'description' is {length} characters; the platform limit is {DESCRIPTION_MAX_CHARS}.",
            )
        )
    elif length > DESCRIPTION_SOFT_MAX:
        warn(f"Frontmatter 'description' is {length} characters; aim for {DESCRIPTION_SOFT_MIN} to {DESCRIPTION_SOFT_MAX}.")
    elif length < DESCRIPTION_SOFT_MIN:
        warn(
            f"Frontmatter 'description' is {length} characters; aim for {DESCRIPTION_SOFT_MIN} to {DESCRIPTION_SOFT_MAX} "
            "so the router has triggers to match on."
        )

    style = description_scalar_style(raw_yaml)
    if style != ">-":
        warn(f"Frontmatter 'description' uses a {style} scalar; write it as 'description: >-' (folded block) so it wraps and tolerates ': '.")

    if "\n" in description:
        warn("Frontmatter 'description' contains literal newlines (a '|' scalar); use '>-' so the text folds into one paragraph.")

    if not TRIGGER_RE.search(description):
        warn("Frontmatter 'description' has no 'Use when ...' clause; the router only sees the description, so list the user intents here.")

    if not _opener_is_third_person(description):
        warn("Frontmatter 'description' should open with a third-person capability statement (e.g. 'Reviews ...', 'Merges ...'), not an imperative or a noun phrase.")

    unquoted = QUOTED_SPAN_RE.sub("", description)
    person = SECOND_PERSON_RE.search(unquoted)
    if person:
        warn(f"Frontmatter 'description' uses first or second person ('{person.group(0)}'); describe the capability in third person.")

    if FLAG_GLOSS_RE.search(description):
        warn("Frontmatter 'description' glosses a flag with a parenthetical; list bare flag names only and explain them in the body.")

    puffery = PUFFERY_RE.search(description)
    if puffery:
        warn(f"Frontmatter 'description' contains the puffery word '{puffery.group(0)}'; state what the skill does instead.")

    if EM_DASH in description:
        warn("Frontmatter 'description' contains an em-dash; use a comma, colon, or separate sentence.")

    if BODY_WHEN_TO_USE_RE.search(body):
        warn("SKILL.md body has a 'When to use' section; move its triggers into the frontmatter description, which is the only part the router reads.")

    return issues


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
                    f"Disallowed keys in YAML frontmatter: {', '.join(sorted(extra_keys))}. "
                    f"Allowed keys are: {', '.join(sorted(ALLOWED_FRONTMATTER_KEYS))}.",
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
        else:
            raw_yaml = content.split("---", 2)[1] if content.startswith("---") else ""
            issues.extend(check_description_quality(description, raw_yaml, body or "", skill_md_path))

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
    """Finds all SKILL.md files given a file or directory target.

    Directory scans skip skills whose directory is a symlink: those point at
    skills maintained elsewhere (for example a google3 checkout), and this
    linter's house rules do not apply to them. A symlinked skill passed
    directly as the target is still linted.
    """
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
        return sorted(p for p in found if not p.parent.is_symlink())

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
