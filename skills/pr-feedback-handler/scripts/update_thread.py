#!/usr/bin/env python3
import argparse
import json
import sys

from utils import run_cmd

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
    parser = argparse.ArgumentParser(description="Reply to and/or resolve a PR review thread.")
    parser.add_argument("thread_id", help="The ID of the review thread (e.g. PRRT_xxx).")
    parser.add_argument("--reply", help="The text body of the reply comment.")
    parser.add_argument("--resolve", action="store_true", help="Resolve the thread.")
    args = parser.parse_args()
    
    if not args.reply and not args.resolve:
        print("Error: At least one of --reply or --resolve must be specified.", file=sys.stderr)
        sys.exit(1)
        
    try:
        if args.reply:
            comment = reply_to_thread(args.thread_id, args.reply)
            print(f"Successfully posted reply! Comment ID: {comment['id']}")
        if args.resolve:
            thread = resolve_thread(args.thread_id)
            print(f"Successfully resolved thread! Thread ID: {thread['id']} (isResolved: {thread['isResolved']})")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
