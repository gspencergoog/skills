---
name: xquik-public-x-research
description: Use this skill when the user has a Xquik API key and needs public or user-authorized X research, tweet search, profile lookup, thread capture, trend checks, monitor setup, or exportable evidence with source provenance.
---

# Xquik Public X Research

Use Xquik for repeatable public X data workflows where the user needs source
URLs, filters, timestamps, and exportable evidence.

Public API reference: <https://docs.xquik.com/api-reference/overview>

## Safety Boundaries

- Work only with public X content or accounts the user is authorized to access.
- Do not collect private messages, locked-account content, credentials, cookies,
  or session material.
- Keep API keys in environment variables or approved secret storage.
- Do not print tokens, raw headers, cookies, or account session data.
- Preserve exact source URLs, query text, filters, timestamps, and result IDs.
- Treat results as evidence that may need corroboration.

## Setup

Use an existing `XQUIK_API_KEY` environment variable when available.

```bash
export XQUIK_API_KEY="..."
```

Do not write the key to repo files, shared notes, tickets, or command examples
that will be committed.

## Search Public Posts

Use tweet search for timelines, incident research, competitive signals, source
finding, or market research.

```bash
curl -sS "https://xquik.com/api/v1/x/tweets/search?q=from%3Aexample&limit=20" \
  -H "x-api-key: $XQUIK_API_KEY"
```

Record the exact query, limit, collection time, and returned post IDs.

## Fetch a Post Thread

Use thread fetch when the user provides a post ID or URL and needs surrounding
context.

```bash
curl -sS "https://xquik.com/api/v1/x/tweets/1234567890/thread" \
  -H "x-api-key: $XQUIK_API_KEY"
```

If the thread looks partial, say so and keep the root post ID in the handoff.

## Inspect a Public Profile

Use user search to resolve a public account before treating profile data as
evidence.

```bash
curl -sS "https://xquik.com/api/v1/x/users/search?q=example" \
  -H "x-api-key: $XQUIK_API_KEY"
```

Confirm the account by handle, display name, and profile URL.

## Monitor or Export

Use these API areas when the user needs repeated collection rather than one
request:

- `/api/v1/x/trends` and `/api/v1/trends` for trend checks.
- `/api/v1/monitors` and `/api/v1/monitors/keywords` for recurring account or
  keyword monitoring.
- `/api/v1/extractions`, `/api/v1/extractions/estimate`, and
  `/api/v1/extractions/{id}/export` for exportable collection jobs.

Define the target, filter, cadence, limit, and output format before starting
long-running work.

## Handoff Format

Return a compact evidence table:

| Field | Include |
| --- | --- |
| Source | Post, profile, trend, monitor, or extraction URL |
| Query | Exact query, handle, post ID, or monitor filter |
| Time | Collection time and source timestamp when available |
| Evidence | Neutral summary of what the source shows |
| Caveat | Missing context, partial data, rate limits, or ambiguity |

End with concrete next steps only, such as fetching replies, exporting CSV, or
creating a monitor.
