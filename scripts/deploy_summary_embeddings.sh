#!/bin/bash
# Deploy summary embeddings feature.
#
# Steps:
#   1. Apply DB migration (adds summary_embedding column + HNSW index)
#   2. Restart summarize_and_tag.py in the 'recovery' screen session
#   3. Run the backfill script (embeds existing summaries)
#
# Usage:
#   bash scripts/deploy_summary_embeddings.sh

set -e
cd "$(dirname "$0")/.."

# Load PG credentials from .env (skip lines that aren't simple KEY=VALUE)
while IFS='=' read -r key value; do
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    [[ "$key" =~ ^PG_ ]] && export "$key=$value"
done < .env
export PGPASSWORD="${PG_PASSWORD}"

echo "=== Step 1: Apply DB migration ==="
PSQL="psql -h ${PG_HOST:-localhost} -U ${PG_USER:-riksdagen} -d ${PG_DB:-riksdagen}"
$PSQL < _postgres/migrations/add_summary_embedding.sql
echo "Migration applied."

echo ""
echo "=== Step 2: Restart summarize_and_tag in screen 'recovery' ==="
screen -S recovery -X stuff $'\009'   # send Ctrl+C to stop the running script
sleep 3
screen -S recovery -X stuff "python scripts/summarize_and_tag.py\n"
echo "summarize_and_tag.py restarted in screen 'recovery'."

echo ""
echo "=== Step 3: Backfill existing summaries ==="
python scripts/embed_summaries.py

echo ""
echo "=== Done! ==="
echo "Verify with:"
echo "  psql -h \${PG_HOST:-localhost} -U \${PG_USER:-riksdagen} -d \${PG_DB:-riksdagen} \\"
echo "    -c \"SELECT COUNT(*) FROM talks WHERE summary_embedding IS NOT NULL\""
