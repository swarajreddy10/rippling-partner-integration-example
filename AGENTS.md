# AGENTS.md

Instructions for coding agents (Claude Code, Codex, Cursor) working in this repository.

## Source of truth for Rippling behaviour

Read Rippling's agent index first: <https://developer.rippling.com/llms.txt>. Every page has a Markdown copy (append `.md` to its URL). Do not rely on memory for Rippling's API: fetch the page, cite its URL and the date in a code comment or `docs/decisions.md`, and label anything not stated there as inference.

## How to change code

1. Write the failing test first and watch it fail for the right reason.
2. Extend `tests/fake_rippling.py` only with behaviour Rippling documents, citing the page.
3. Make the test pass, then run every check:

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

Database tests need `DATABASE_URL` pointing at a real Postgres; they fail rather than skip without it.

## Rules

- Never send a bearer token to a host other than `rest.ripplingapis.com` or `api.rippling.com`.
- Retry only the statuses Rippling documents as retryable.
- Never reconcile (mark workers removed) after a partial or empty listing.
- Pin every GitHub Action to a commit SHA; `tests/test_workflows.py` enforces it.
- No real customer data, tokens, or credentials in the repository.
