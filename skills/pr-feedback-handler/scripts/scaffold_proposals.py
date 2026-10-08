#!/usr/bin/env python3
"""Generates a proposals.json scaffold from a PR comments analysis report."""

import argparse
import json
import os
import sys


def summarize_text(text, max_len=100):
    """Returns a single-line summary of text truncated to max_len."""
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    first_line = lines[0]
    if len(first_line) > max_len:
        return first_line[: max_len - 3] + "..."
    return first_line


def extract_proposals_scaffold(report_data, include_resolved=False):
    """Extracts unresolved review threads into a proposals dictionary template."""
    threads = report_data.get("threads", [])
    scaffold = {}
    summaries = []

    for thread in threads:
        tid = thread.get("id")
        if not tid:
            continue

        is_resolved = thread.get("isResolved", False)
        is_hidden = thread.get("isHidden", False)

        if not include_resolved and (is_resolved or is_hidden):
            continue

        path = thread.get("path", "")
        line = thread.get("line") or thread.get("originalLine")
        comments = thread.get("comments", [])

        author = "unknown"
        body = ""
        if comments:
            # First comment starts the review thread
            author = comments[0].get("author") or "unknown"
            body = comments[0].get("body") or ""

        summary = summarize_text(body, max_len=120)
        suggestion = thread.get("suggestion")

        scaffold[tid] = {
            "path": path,
            "line": line,
            "author": author,
            "summary": summary,
            "suggestion": suggestion,
            "proposedFix": "",
            "draftReply": "",
            "action": "accept",
            "assessment": "solid",
        }

        summaries.append({
            "id": tid,
            "path": path,
            "line": line,
            "author": author,
            "summary": summary,
            "has_suggestion": bool(suggestion),
        })

    return scaffold, summaries


def print_summary_table(summaries, output_path):
    """Prints a concise human- and agent-readable summary of the scaffolded threads."""
    if not summaries:
        print(f"No unresolved review threads found. Empty scaffold written to: {output_path}")
        return

    print(f"\nScaffolded {len(summaries)} review thread(s) into: {output_path}\n")
    print(f"{'#':<3} {'THREAD ID':<26} {'FILE & LINE':<35} {'AUTHOR':<15}")
    print("-" * 82)

    for i, item in enumerate(summaries, 1):
        loc = f"{item['path']}:{item['line']}" if item["line"] else item["path"]
        if len(loc) > 34:
            loc = "..." + loc[-31:]
        author_str = f"@{item['author']}"
        if len(author_str) > 14:
            author_str = author_str[:13] + "…"

        print(f"{i:<3} {item['id']:<26} {loc:<35} {author_str:<15}")
        if item["summary"]:
            print(f"    Summary: \"{item['summary']}\"")
        if item["has_suggestion"]:
            print("    [Includes code suggestion]")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Scaffold a proposals.json template from pr_comments.json."
    )
    parser.add_argument(
        "--data-dir",
        default=".",
        help="Directory to read pr_comments.json from and write proposals.json to (default: current directory).",
    )
    parser.add_argument(
        "--comments",
        default=None,
        help="Path to pr_comments.json file (defaults to <data-dir>/pr_comments.json).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to output proposals.json file (defaults to <data-dir>/proposals.json).",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite output proposals file if it already exists.",
    )
    parser.add_argument(
        "--include-resolved",
        action="store_true",
        help="Include resolved and hidden threads in the scaffold (default: False).",
    )

    args = parser.parse_args()

    data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    comments_path = (
        os.path.abspath(os.path.expanduser(args.comments))
        if args.comments
        else os.path.join(data_dir, "pr_comments.json")
    )
    output_path = (
        os.path.abspath(os.path.expanduser(args.output))
        if args.output
        else os.path.join(data_dir, "proposals.json")
    )

    if not os.path.exists(comments_path):
        print(f"Error: PR comments file not found at '{comments_path}'.", file=sys.stderr)
        print("Please run analyze_comments.py first to fetch PR comments.", file=sys.stderr)
        sys.exit(1)

    try:
        with open(comments_path, "r", encoding="utf-8") as f:
            report_data = json.load(f)
    except Exception as e:
        print(f"Error: Failed to parse '{comments_path}' as valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if os.path.exists(output_path) and not args.force:
        print(
            f"Error: Output file '{output_path}' already exists. Pass --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    scaffold, summaries = extract_proposals_scaffold(
        report_data, include_resolved=args.include_resolved
    )

    # Ensure output parent directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(scaffold, f, indent=2)
        f.write("\n")

    print_summary_table(summaries, output_path)
    sys.exit(0)


if __name__ == "__main__":
    main()
