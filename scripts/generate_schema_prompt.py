#!/usr/bin/env python3
"""Write the schema block the model reads, from the database itself.

The column list used to be maintained by hand, which is how `debates.debate`,
`document_proposals.nummer` and `behandlas_i` ended up in it — all three named
columns that did not exist, so every query touching them failed.

Here the database is the source of truth. A column reaches the prompt only if it
carries a comment, and the comment supplies the meaning that introspection cannot:

    '-'             exposed; the name and type already say it
    '[hide] reason' not shown to the model
    any other text  exposed, and the text becomes the note
    no comment      undecided — the build fails rather than guessing

Run after any migration that adds or renames a column:

    python scripts/generate_schema_prompt.py

A rename carries its comment along, so the prompt follows automatically; a dropped
column takes its comment with it and disappears. Only a genuinely new column needs
a decision, and tests/test_schema_comments.py insists on one.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from postgres_client import pg  # noqa: E402

# The tables the model may query. Kept in step with the guard in
# backend/services/llm_tools.py by tests/test_schema_comments.py.
TABLES = [
    "speeches", "people", "debates",
    "documents", "document_authors", "document_proposals",
]

OUT = ROOT / "prompts" / "en" / "_shared" / "schema_tables.md"
OUT_COMPACT = ROOT / "prompts" / "en" / "_shared" / "schema_columns.md"

HIDE = "[hide]"
PLAIN = "-"

# `text` is the default and would be noise on most columns; only the types that
# change how a column must be used are worth the characters.
TYPE_LABELS = {
    "integer": "INT", "bigint": "INT", "smallint": "INT",
    "boolean": "BOOL", "date": "DATE", "tsvector": "tsvector",
    "timestamp with time zone": "TIMESTAMPTZ", "jsonb": "jsonb",
    "double precision": "FLOAT", "numeric": "NUMERIC",
}

_QUERY = """
SELECT c.relname                                   AS table_name,
       a.attname                                   AS column_name,
       format_type(a.atttypid, a.atttypmod)        AS full_type,
       t.typname                                   AS type_name,
       d.description                               AS comment,
       COALESCE(i.indisprimary, false)             AS is_pk
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
  JOIN pg_type t ON t.oid = a.atttypid
  LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = a.attnum
  LEFT JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary
                      AND a.attnum = ANY (i.indkey)
 WHERE c.relname = ANY (%s)
 ORDER BY array_position(%s, c.relname), a.attnum
"""


def _type_label(full_type: str, type_name: str) -> str:
    """A short type suffix, or '' when the type is unremarkable text."""
    if type_name.startswith("_"):  # arrays come back as _text, _int4, ...
        inner = TYPE_LABELS.get(full_type[:-2], "TEXT" if full_type == "text[]" else None)
        return f"{inner or 'TEXT'}[]"
    if full_type.startswith("character varying") or full_type == "text":
        return ""
    if full_type.startswith("vector"):
        return "vector"
    return TYPE_LABELS.get(full_type, full_type.upper())


def build(notes: bool = True) -> str:
    rows = pg.execute(_QUERY, (TABLES, TABLES))

    undecided = [f"{r['table_name']}.{r['column_name']}" for r in rows if r["comment"] is None]
    if undecided:
        raise SystemExit(
            "Undecided columns — every column needs a comment before the prompt can be\n"
            "generated. Use '-' if the name says it, '[hide] reason' to keep it out, or a\n"
            "one-line description. Add them to _postgres/migrations/add_column_comments.sql:\n"
            + "".join(f"  {name}\n" for name in undecided)
        )

    listing: list[str] = []
    width = max(len(t) for t in TABLES) + 2
    pad = " " * (4 + width)

    for table in TABLES:
        cols = [r for r in rows if r["table_name"] == table
                and not r["comment"].startswith(HIDE)]
        rendered = []
        table_notes = []
        for col in cols:
            label = _type_label(col["full_type"], col["type_name"])
            name = col["column_name"] + ("\u00b7PK" if col["is_pk"] else "")
            rendered.append(f"{name} {label}".strip())
            if notes and col["comment"] != PLAIN:
                table_notes.append(f"{col['column_name']}: {col['comment']}")
        # Wrap the column list under a hanging indent so it stays scannable.
        line, lines = "", []
        for item in rendered:
            candidate = f"{line}, {item}" if line else item
            if len(candidate) > 62:
                lines.append(line)
                line = item
            else:
                line = candidate
        lines.append(line)
        listing.append(f"    {table:<{width}}{lines[0]}")
        listing += [f"{pad}{rest}" for rest in lines[1:]]
        # Notes sit under their own table, so the table name is not repeated.
        listing += [f"{pad}\u00b7 {note}" for note in table_notes]

    header = "**Schema.** The only columns that exist; a query naming another fails.\n"
    if notes:
        header += "Lines marked \u00b7 are what the column name alone would not tell you.\n"
    return (
        "<!-- Generated by scripts/generate_schema_prompt.py; edit the column "
        "comments, not this. -->\n\n"
        + header
        + "\n"
        + "\n".join(listing)
        + "\n"
    )


if __name__ == "__main__":
    check = "--check" in sys.argv
    outputs = [(OUT, build(notes=True)), (OUT_COMPACT, build(notes=False))]
    stale = []
    for path, text in outputs:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if check:
            if current != text:
                stale.append(path.relative_to(ROOT))
        else:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)} — {len(text):,} chars, "
                  f"~{len(text) / 3.54:.0f} tokens")
    if check:
        if stale:
            raise SystemExit(
                "Out of date: " + ", ".join(str(s) for s in stale)
                + "\nRun: python scripts/generate_schema_prompt.py"
            )
        print("schema blocks are current.")
