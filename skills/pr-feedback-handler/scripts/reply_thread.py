#!/usr/bin/env python3
import argparse
import subprocess
import json
import sys

def run_cmd(args):
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout.strip()

def reply_to_thread(thread_id, body):
    query = """
    mutation($threadId: ID!, $body: String!) {
      addPullRequestReviewThreadReply(input: { pullRequestReviewThreadId: $threadId, body: $body }) {
        comment {
          id
          body
        }
      }
    }
    """
    
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={query}",
        "-F", f"threadId={thread_id}",
        "-F", f"body={body}"
    ]
    
    output = run_cmd(cmd)
    data = json.loads(output)
    if "errors" in data:
        raise Exception(f"GraphQL errors: {data['errors']}")
    return data["data"]["addPullRequestReviewThreadReply"]["comment"]

def main():
    parser = argparse.ArgumentParser(description="Reply to a PR review thread.")
    parser.add_argument("thread_id", help="The ID of the review thread (e.g. PRRT_xxx).")
    parser.add_argument("body", help="The text body of the reply comment.")
    args = parser.parse_args()
    
    try:
        comment = reply_to_thread(args.thread_id, args.body)
        print(f"Successfully posted reply! Comment ID: {comment['id']}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
