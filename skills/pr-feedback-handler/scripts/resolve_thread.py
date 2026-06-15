#!/usr/bin/env python3
import argparse
import subprocess
import json
import sys

def run_cmd(args):
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout.strip()

def resolve_thread(thread_id):
    query = """
    mutation($threadId: ID!) {
      resolveReviewThread(input: { threadId: $threadId }) {
        thread {
          id
          isResolved
        }
      }
    }
    """
    
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={query}",
        "-F", f"threadId={thread_id}"
    ]
    
    output = run_cmd(cmd)
    data = json.loads(output)
    if "errors" in data:
        raise Exception(f"GraphQL errors: {data['errors']}")
    return data["data"]["resolveReviewThread"]["thread"]

def main():
    parser = argparse.ArgumentParser(description="Resolve a PR review thread.")
    parser.add_argument("thread_id", help="The ID of the review thread (e.g. PRRT_xxx).")
    args = parser.parse_args()
    
    try:
        thread = resolve_thread(args.thread_id)
        print(f"Successfully resolved thread! Thread ID: {thread['id']} (isResolved: {thread['isResolved']})")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
