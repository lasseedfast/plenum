#!/usr/bin/env bash
# =============================================================================
# recovery.sh — full data recovery pipeline
#
# Run inside a screen session:
#   screen -S recovery
#   bash recovery.sh 2>&1 | tee logs/recovery.log
#
# Steps (ordered, each depends on the previous):
#   1. Load ~450k raw talks from disk JSON files  (documents_to_postgres.py)
#   2. Enrich with summaries+tags from talks_training in ArangoDB
#   3. Assign debate IDs to talks that don't have one
#   4. Migrate debates from ArangoDB
#   5. Migrate chunks (2.3M rows with embeddings) from ArangoDB  ← hours
#   6. Launch summarize_and_tag.py in the background             ← days
# =============================================================================

set -euo pipefail

REPO="/home/lasse/riksdagen"
LOG_DIR="$REPO/logs"
PYTHON="python"

cd "$REPO"
mkdir -p "$LOG_DIR"

# ── colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓] $*${NC}"; }
warn() { echo -e "${YELLOW}[!] $*${NC}"; }
fail() { echo -e "${RED}[✗] $*${NC}"; exit 1; }
step() { echo; echo -e "${YELLOW}══════════════════════════════════════════${NC}"; \
         echo -e "${YELLOW} $*${NC}"; \
         echo -e "${YELLOW}══════════════════════════════════════════${NC}"; }

# ── sanity checks ─────────────────────────────────────────────────────────────
step "Pre-flight checks"

[[ -f "$REPO/.env" ]] || warn ".env not found — relying on shell environment"
[[ -d "$REPO/talks"  ]] || fail "talks/ directory not found"

$PYTHON -c "import psycopg2, pgvector" 2>/dev/null || fail "psycopg2 / pgvector not installed"
$PYTHON -c "from postgres_client import pg; rows = pg.execute('SELECT 1'); print('PostgreSQL: OK')" \
    || fail "Cannot connect to PostgreSQL"

ok "Pre-flight passed"

# =============================================================================
# STEP 1 — Load all talks from disk JSON files
# =============================================================================
step "Step 1 — Load talks from disk (documents_to_postgres.py)"

$PYTHON scripts/documents_to_postgres.py \
    2>&1 | tee "$LOG_DIR/step1_documents_to_postgres.log"

ok "Step 1 done"

# =============================================================================
# STEP 2 — Enrich with summaries + tags from ArangoDB talks_training
# =============================================================================
step "Step 2 — Import talks_training from ArangoDB"

$PYTHON scripts/migrate_arango_to_postgres.py --collection talks_training \
    2>&1 | tee "$LOG_DIR/step2_talks_training.log"

ok "Step 2 done"

# =============================================================================
# STEP 3 — Assign debate IDs
# =============================================================================
step "Step 3 — Assign debate IDs"

$PYTHON - <<'PYEOF' 2>&1 | tee "$LOG_DIR/step3_debate_ids.log"
import os, sys
os.chdir("/home/lasse/riksdagen")
sys.path.insert(0, "/home/lasse/riksdagen")
from scripts.debates import make_debate_ids
make_debate_ids()
print("Debate ID assignment complete.")
PYEOF

ok "Step 3 done"

# =============================================================================
# STEP 4 — Migrate debates from ArangoDB
# =============================================================================
step "Step 4 — Migrate debates from ArangoDB"

$PYTHON scripts/migrate_arango_to_postgres.py --collection debates \
    2>&1 | tee "$LOG_DIR/step4_debates.log"

ok "Step 4 done"

# =============================================================================
# STEP 5 — Migrate chunks from ArangoDB (slow — embeddings are large)
# =============================================================================
step "Step 5 — Migrate chunks from ArangoDB  (this will take several hours)"
echo "You can detach the screen session (Ctrl-A D) and come back later."
echo "Progress is printed to this terminal and logged to $LOG_DIR/step5_chunks.log"

$PYTHON scripts/migrate_arango_to_postgres.py --collection chunks \
    2>&1 | tee "$LOG_DIR/step5_chunks.log"

ok "Step 5 done"

# =============================================================================
# STEP 6 — Launch summarize_and_tag.py in the background
# =============================================================================
step "Step 6 — Launch summarize_and_tag.py (background, may take days)"

SATLOG="$LOG_DIR/summarize_and_tag.log"
SATPID="$LOG_DIR/summarize_and_tag.pid"

# Kill any previous instance
if [[ -f "$SATPID" ]]; then
    OLD_PID=$(cat "$SATPID")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        warn "Killing previous summarize_and_tag.py (PID $OLD_PID)"
        kill "$OLD_PID"
        sleep 2
    fi
fi

nohup $PYTHON scripts/summarize_and_tag.py >> "$SATLOG" 2>&1 &
SAT_PID=$!
echo "$SAT_PID" > "$SATPID"

ok "summarize_and_tag.py launched (PID $SAT_PID)"
echo "  Log:  $SATLOG"
echo "  PID file: $SATPID"
echo "  Monitor: tail -f $SATLOG"

# =============================================================================
# DONE
# =============================================================================
step "Recovery pipeline complete"
echo ""
echo "Verification queries to run in psql:"
echo "  SELECT COUNT(*) FROM talks;                           -- should be ~450k"
echo "  SELECT COUNT(*) FROM talks WHERE summary IS NOT NULL; -- should be ~11k+"
echo "  SELECT COUNT(*) FROM chunks;                          -- should be ~2.3M"
echo "  SELECT COUNT(*) FROM debates;                         -- should be ~17k"
echo ""
echo "FK orphan check:"
echo "  SELECT COUNT(*) FROM chunks c"
echo "    WHERE NOT EXISTS (SELECT 1 FROM talks t WHERE t.id = c.talk_id);"
echo ""
echo "Summarize progress (run any time):"
echo "  SELECT COUNT(*) FROM talks WHERE summary IS NOT NULL AND tags IS NOT NULL;"
