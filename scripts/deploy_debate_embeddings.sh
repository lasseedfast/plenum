#!/bin/bash
# Deploy debate summary embeddings feature.
#
# Steps:
#   1. Add summary_embedding column + HNSW index to debates table
#   2. Fix broken talk_ids in old ArangoDB-migrated debate rows
#   3. Generate debate summaries for debates whose talks are all summarized
#      (runs debates.py once, then exits — use screen for continuous mode)
#   4. Backfill summary_embedding for all debates with a summary
#
# Usage:
#   bash scripts/deploy_debate_embeddings.sh

set -e
cd "$(dirname "$0")/.."

# Load PG credentials from .env (skip lines that aren't simple KEY=VALUE)
while IFS='=' read -r key value; do
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    [[ "$key" =~ ^PG_ ]] && export "$key=$value"
done < .env
export PGPASSWORD="${PG_PASSWORD}"

PSQL="psql -h ${PG_HOST:-localhost} -U ${PG_USER:-riksdagen} -d ${PG_DB:-riksdagen}"

echo "=== Step 1: Add summary_embedding column to debates ==="
$PSQL < _postgres/migrations/add_debate_summary_embedding.sql
echo "Migration applied."

echo ""
echo "=== Step 2: Fix broken talk_ids in existing debate rows ==="
$PSQL < _postgres/migrations/fix_debate_talk_ids.sql
echo "talk_ids fixed."

echo ""
echo "=== Step 3: Generate missing debate summaries (one pass) ==="
# Run debates.py in a subprocess that exits after one full pass.
# For continuous summarization, run: screen -S debates python scripts/debates.py
python - <<'PYEOF'
import os, sys
os.chdir("/home/lasse/riksdagen")
sys.path.append("/home/lasse/riksdagen")

from concurrent.futures import ProcessPoolExecutor, as_completed
from scripts.debates import process_ready_debate, process_debate_date
from postgres_client import pg

system_message = """Din uppgift är att sammanfatta debatter i Sveriges riksdag.
Du kommer först att få enskilda tal som du ska sammanfatta var för sig, efter det ska du sammanfatta hela debatten.
Sammanfattningarna ska vara på svenska och vara koncisa och informativa.
Det är viktigt att du förstår vad som är kärnan i varje tal och debatt, fokusera därför på de argument och sakförhållanden som framförs.
"""

ready = pg.execute("""
    SELECT t.debate
    FROM talks t
    LEFT JOIN debates d ON t.debate = d.debate
    WHERE t.debate IS NOT NULL AND d.debate IS NULL
    GROUP BY t.debate
    HAVING COUNT(t.id) = COUNT(t.summary)
      AND COUNT(t.id) > 1
    ORDER BY t.debate
""")
ready_ids = [row["debate"] for row in ready]
print(f"Found {len(ready_ids)} ready debates to summarize.")

with ProcessPoolExecutor(max_workers=4) as executor:
    futures = {
        executor.submit(process_ready_debate, did, system_message): did
        for did in ready_ids
    }
    done = 0
    for future in as_completed(futures):
        did = futures[future]
        try:
            future.result()
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(ready_ids)} done")
        except Exception as exc:
            print(f"Error on {did}: {exc}")
print(f"Done. Processed {done} debates.")
PYEOF

echo ""
echo "=== Step 4: Backfill debate summary embeddings ==="
python scripts/embed_debate_summaries.py

echo ""
echo "=== Done! ==="
echo "Verify with:"
echo "  psql ... -c \"SELECT COUNT(*) FROM debates WHERE summary_embedding IS NOT NULL\""
echo "  psql ... -c \"SELECT COUNT(*) FROM debates\""
