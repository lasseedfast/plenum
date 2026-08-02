#!/bin/bash
# Progress of the motion embedding backfills. Run anytime:
#   bash scripts/motions_progress.sh
set -e
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PGPASSWORD=$(grep '^PG_PASSWORD=' .env | cut -d= -f2)
PGDB=$(grep '^PG_DB=' .env | cut -d= -f2)
PGUSER=$(grep '^PG_USER=' .env | cut -d= -f2)
PGHOST=$(grep '^PG_HOST=' .env | cut -d= -f2)

echo "=== Motion embedding coverage ($(date '+%H:%M:%S')) ==="
psql -h "$PGHOST" -U "$PGUSER" -d "$PGDB" -tA -F $'\t' <<'SQL' | column -t -s $'\t'
SELECT 'full-text chunks (motions)' AS what,
       count(*) FILTER (WHERE c.id IS NOT NULL) AS done,
       count(*) AS total,
       round(100.0*count(*) FILTER (WHERE c.id IS NOT NULL)/nullif(count(*),0),1)||'%' AS pct
FROM (SELECT dok_id FROM motions WHERE has_text) m
LEFT JOIN LATERAL (SELECT 1 AS id FROM motion_chunks c WHERE c.motion_id = m.dok_id LIMIT 1) c ON true
UNION ALL
SELECT 'yrkande embeddings',
       count(*) FILTER (WHERE embedding IS NOT NULL),
       count(*),
       round(100.0*count(*) FILTER (WHERE embedding IS NOT NULL)/nullif(count(*),0),1)||'%'
FROM motion_yrkanden;
SQL

echo
echo "=== Backfill processes ==="
pgrep -af "make_embeddings.py|backfill_motions.sh" | grep -v pgrep || echo "  (none running — backfills finished)"
