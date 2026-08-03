"""Fetch, adapt, and load a parliament's open data.

    python -m ingest.cli fetch --source documents --range 2022-2025
    python -m ingest.cli load  --source documents
    python -m ingest.cli sync  --source all

Sources are declared under `sources:` in parliament.yaml. The adapter named there
is the only module that knows the source's own field names.
"""
