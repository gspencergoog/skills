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

def process_decisions(decisions):
    success_count = 0
    failures = []
    
    for i, item in enumerate(decisions):
        if not isinstance(item, dict):
            failures.append({
                "index": i,
                "error": f"Item is not a dictionary: {item}"
            })
            continue
            
        thread_id = item.get("threadId") or item.get("thread_id")
        if not thread_id:
            failures.append({
                "index": i,
                "error": f"Missing threadId/thread_id in item: {item}"
            })
            continue
            
        approved = item.get("approved", True)
        if not approved:
            print(f"Skipping thread {thread_id} because it is not approved.")
            continue
            
        reply = item.get("reply") or item.get("body")
        resolve = item.get("resolve", False)
        
        if not reply and not resolve:
            print(f"Skipping thread {thread_id} because no reply or resolve action was specified.")
            continue
            
        print(f"Processing thread {thread_id}...")
        thread_failed = False
        
        if reply:
            try:
                comment = reply_to_thread(thread_id, reply)
                print(f"  Successfully posted reply! Comment ID: {comment['id']}")
            except Exception as e:
                failures.append({
                    "thread_id": thread_id,
                    "action": "reply",
                    "error": str(e)
                })
                thread_failed = True
                
        if resolve and not thread_failed:
            try:
                thread = resolve_thread(thread_id)
                print(f"  Successfully resolved thread! Thread ID: {thread['id']} (isResolved: {thread['isResolved']})")
            except Exception as e:
                failures.append({
                    "thread_id": thread_id,
                    "action": "resolve",
                    "error": str(e)
                })
                thread_failed = True
                
        if not thread_failed:
            success_count += 1
            
    return success_count, failures

def main():
    parser = argparse.ArgumentParser(description="Reply to and/or resolve PR review threads.")
    parser.add_argument("thread_id", nargs="?", help="The ID of the review thread (e.g. PRRT_xxx).")
    parser.add_argument("--reply", help="The text body of the reply comment.")
    parser.add_argument("--resolve", action="store_true", help="Resolve the thread.")
    parser.add_argument("--file", help="Path to a JSON file containing thread decisions.")
    args = parser.parse_args()
    
    if args.file:
        if args.thread_id or args.reply or args.resolve:
            print("Error: If --file is specified, thread_id, --reply, and --resolve must not be specified.", file=sys.stderr)
            sys.exit(1)
            return
            
        try:
            with open(args.file, "r") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error reading JSON file {args.file}: {e}", file=sys.stderr)
            sys.exit(1)
            return
            
        if isinstance(data, dict) and "decisions" in data:
            decisions = data["decisions"]
        elif isinstance(data, list):
            decisions = data
        else:
            print("Error: JSON file must contain a list of decisions or an object with a 'decisions' key.", file=sys.stderr)
            sys.exit(1)
            return
            
        if not isinstance(decisions, list):
            print("Error: 'decisions' must be a list.", file=sys.stderr)
            sys.exit(1)
            return
            
        success_count, failures = process_decisions(decisions)
        print(f"\nBulk processing completed: {success_count} succeeded, {len(failures)} failed.")
        
        if failures:
            print("\n--- Failure Report ---", file=sys.stderr)
            for f in failures:
                if "thread_id" in f:
                    print(f"Thread {f['thread_id']} failed during '{f['action']}': {f['error']}", file=sys.stderr)
                else:
                    print(f"Item at index {f['index']} failed: {f['error']}", file=sys.stderr)
            print("----------------------", file=sys.stderr)
            sys.exit(1)
            return
            
    else:
        if not args.thread_id:
            print("Error: Must specify thread_id or --file.", file=sys.stderr)
            sys.exit(1)
            return
        if not args.reply and not args.resolve:
            print("Error: At least one of --reply or --resolve must be specified.", file=sys.stderr)
            sys.exit(1)
            return
            
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
            return

if __name__ == "__main__":
    main()

