# Contributing

## The one rule

Answers must be traceable to sources. This is a tool for journalists and
researchers, so a claim the user cannot verify is a defect even when it happens to
be true. Changes to prompts, retrieval or citation handling are judged on whether
they preserve that.

## Keeping it country-agnostic

Before hardcoding anything Swedish, check whether it belongs in configuration:

- A party, colour, committee, activity code, identifier shape or source URL →
  `parliament.yaml`
- Text the model reads → `prompts/<lang>/`
- Text the user reads → `content/<lang>/` or, for now, the component
- Knowledge of one portal's JSON → `ingest/adapters/`

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

`ruff check .` and `pytest -q` are what CI runs. Neither needs a model endpoint;
tests that would call one should be skipped rather than mocked into passing.

## Prompts

Edit the files under `prompts/`, not Python. Run with `PROMPTS_RELOAD=1` to skip
restarting between edits.

`tests/test_prompts_golden.py` asserts prompts match their snapshots. If you change a
prompt deliberately, regenerate with `python tests/test_prompts_golden.py --update`
and include the diff in your pull request — a silent prompt change shows up later as
subtly worse answers, which is hard to trace back.

## Database changes

Add a migration under `_postgres/migrations/` **and** update `_postgres/schema.sql`.
CI applies `schema.sql` to a clean database; it drifted from production once and the
result was a schema newcomers could not run the code against.

Guard migrations so they are no-ops on a database that already has the change. The
same file then works for a fresh install and an existing deployment.

## Commits

Explain why, not what — the diff shows what. If you worked something out the hard
way (a provider quirk, a Postgres behaviour, a data oddity), write it down; that is
usually the most valuable part of the change.
