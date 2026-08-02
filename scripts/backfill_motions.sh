#!/bin/bash
# One-time backfill of motioner 1990→idag.
#
# Kör i tre steg (alla resumérbara — avbrott är ofarligt, kör bara om):
#   1. Ladda ned alla mot-arkiv som saknas (hoppar över ifyllda mappar)
#   2. Parsa alla mappar i motioner/ till Postgres (ON CONFLICT DO NOTHING)
#   3. Bygg embeddings år för år, nyast först
#
# Starta frikopplat:  nohup scripts/backfill_motions.sh > backfill_motions.log 2>&1 &
set -e
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=.venv/bin/python

echo "=== Step 1: download all ranges ==="
$PY scripts/download_motions.py

echo "=== Step 2: parse all folders into Postgres ==="
$PY scripts/motions_to_postgres.py

echo "=== Step 3: full-text embeddings, newest year first ==="
for year in $(seq 2025 -1 1990); do
    echo "--- embeddings for year $year ---"
    $PY scripts/make_embeddings.py motions "$year"
done

echo "=== Step 4: yrkande (condensed proposal) embeddings ==="
$PY scripts/make_embeddings.py yrkanden

echo "=== Backfill complete ==="
