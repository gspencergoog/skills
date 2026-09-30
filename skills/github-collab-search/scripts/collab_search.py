#!/usr/bin/env python3
"""collab_search.py - CLI tool to find and summarize GitHub collaboration history between users.

Usage:
  collab-search --user-a <login> [--user-b <login>] [--repo <owner/repo>] [--org <org>]
                [--since <YYYY-MM-DD>] [--until <YYYY-MM-DD>] [--format markdown|json|summary]
                [--out <path>] [--split-chunks <KB>]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add script directory to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import collab_search_lib as lib


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="github-collab-search",
        description="Discover, extract, and summarize collaborative GitHub interactions between users.",
    )
    parser.add_argument(
        "--user-a",
        required=True,
        help="Primary GitHub user handle (required).",
    )
    parser.add_argument(
        "--user-b",
        help="Collaborator GitHub user handle (optional; if omitted, digests User A's activity).",
    )
    parser.add_argument(
        "--repo",
        help="Target repository slug (e.g. 'owner/repo'). Defaults to active git checkout remote.",
    )
    parser.add_argument(
        "--org",
        help="Target GitHub organization slug (e.g. 'orgname').",
    )
    parser.add_argument(
        "--since",
        help="Start date filter (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--until",
        help="End date filter (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "summary"],
        default="markdown",
        help="Output format (default: markdown).",
    )
    parser.add_argument(
        "--out",
        help="Output file or directory path. Defaults to stdout.",
    )
    parser.add_argument(
        "--split-chunks",
        type=int,
        metavar="KB",
        help="Split output into chunk files of maximum size in KB (e.g. 120). Requires --out directory.",
    )

    args = parser.parse_args(argv)

    # Determine search scope (repo or org)
    target = ""
    if args.repo:
        target = f"repo:{args.repo}"
    elif args.org:
        target = f"org:{args.org}"
    else:
        owner, repo_name = lib.detect_repo_slug()
        if owner and repo_name:
            target = f"repo:{owner}/{repo_name}"
        else:
            print("Error: Could not detect repository from current directory. Please pass --repo or --org.", file=sys.stderr)
            return 2

    # Date defaults: if neither specified, query without date restriction or default to past 90 days
    start_date = args.since or ""
    end_date = args.until or ""

    print(f"Searching GitHub interactions for {args.user_a}" + (f" & {args.user_b}" if args.user_b else "") + f" in {target}...", file=sys.stderr)

    if start_date and end_date:
        raw_nodes = lib.fetch_all_nodes_in_window(target, args.user_a, start_date, end_date)
    else:
        query_str = lib.build_search_query(target, args.user_a, start_date, end_date)
        _, raw_nodes, has_next, cursor = lib.fetch_graphql_page(query_str)
        while has_next and cursor:
            _, next_nodes, has_next, cursor = lib.fetch_graphql_page(query_str, cursor=cursor)
            raw_nodes.extend(next_nodes)

    print(f"Fetched {len(raw_nodes)} candidate threads involving @{args.user_a}.", file=sys.stderr)

    threads = lib.extract_threads(raw_nodes, user_a=args.user_a, user_b=args.user_b)
    print(f"Retained {len(threads)} collaborative threads.", file=sys.stderr)

    if args.format == "json":
        # Format as JSON
        serializable_threads = []
        for t in threads:
            d = dataclasses.asdict(t)
            d["participants"] = list(t.participants)
            serializable_threads.append(d)
        output_content = json.dumps(serializable_threads, indent=2)
    elif args.format == "summary":
        total_prs = sum(1 for t in threads if t.type == "PR")
        merged_prs = sum(1 for t in threads if t.type == "PR" and t.state == "MERGED")
        total_issues = sum(1 for t in threads if t.type == "Issue")
        output_content = (
            f"Target: {target}\n"
            f"Participants: @{args.user_a}" + (f" and @{args.user_b}" if args.user_b else "") + "\n"
            f"Total Collaborative Threads: {len(threads)}\n"
            f"Pull Requests: {total_prs} (Merged: {merged_prs})\n"
            f"Issues: {total_issues}\n"
        )
    else:
        output_content = lib.generate_markdown_report(
            threads=threads,
            target=target,
            user_a=args.user_a,
            user_b=args.user_b,
            since=start_date,
            until=end_date,
        )

    # Chunking or direct output
    if args.split_chunks and args.split_chunks > 0:
        chunks = lib.partition_into_chunks(output_content, max_chunk_kb=args.split_chunks)
        out_dir = Path(args.out) if args.out else Path.cwd() / "collab_chunks"
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"Splitting report into {len(chunks)} chunk(s) in {out_dir}...", file=sys.stderr)
        for i, chunk in enumerate(chunks, start=1):
            chunk_file = out_dir / f"chunk_{i:02d}.md"
            chunk_file.write_text(chunk, encoding="utf-8")
            print(f"  Wrote {chunk_file} ({len(chunk.encode('utf-8'))} bytes)", file=sys.stderr)
    elif args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_content, encoding="utf-8")
        print(f"Wrote report to {out_path} ({len(output_content.encode('utf-8'))} bytes)", file=sys.stderr)
    else:
        print(output_content)

    return 0


if __name__ == "__main__":
    sys.exit(main())
