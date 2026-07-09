---
name: nochange
description: |
  Provide commentary, explanations, code analysis, or answers without making any modifications to the codebase or executing commands. Use when the user explicitly requests "/nochange", "nochange", or asks to explain/review code without editing.
---

# Nochange Skill

This skill instructs the agent to strictly provide commentary, explanations, and advice without making any changes to files or running commands that modify the project.

## Instructions

1. **NO FILE EDITS OR CREATION**: Do NOT modify, create, delete, or rename any files under any circumstances.
2. **NO ACTIONS/COMMANDS**: Do NOT execute any terminal commands that would modify the state of the workspace (e.g., git commits, package installations, tests that modify files).
3. **PROVIDE COMMENTS/ADVICE**: Focus entirely on explaining, analyzing, answering questions, or reviewing code.
4. **USE ONLY READ-ONLY TOOLS**: You may view files using `view_file`, perform searches using `grep_search`, or use `rg` (ripgrep) or `fd` (find files) or other read-only tools to understand the code, but you must not edit them.
5. **EXPLICIT CONFIRMATION**: If the user later decides they want to make changes, wait for their explicit, unambiguous instruction before using any file modification tools or running modification commands.
