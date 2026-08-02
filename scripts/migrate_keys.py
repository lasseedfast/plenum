"""
One-time migration: swap talks._key from anforande_id (UUID) to id (dok_id).

After this migration:
  - talks._key       = old "id" field (e.g. "H90982")
  - talks.anforande_id = preserved old UUID for reference
  - chunks.parent_id = "talks/H90982"  (was "talks/<uuid>")
  - debates.talk_ids = ["talks/H90982", ...]

Execution order matters:
  Step 1 — build mapping from talks (while old docs exist)
  Step 2 — update chunks.parent_id (single AQL; DOCUMENT lookup needs old talks)
  Step 3 — update debates.talk_ids  (single AQL; DOCUMENT lookup needs old talks)
  Step 4 — re-key talks              (insert new + delete old)

Usage:
  python scripts/migrate_keys.py             # live run
  python scripts/migrate_keys.py --dry-run   # count only, no changes
"""

import os
import sys
import argparse
import logging
import time

os.chdir("/home/lasse/riksdagen")
sys.path.append("/home/lasse/riksdagen")

# Connect directly to localhost with no read timeout.
# The default 60s client timeout is too short for full-collection scans.
from arango import ArangoClient
from arango.http import DefaultHTTPClient
from dotenv import load_dotenv
load_dotenv()
_client = ArangoClient(hosts="http://localhost:8529", http_client=DefaultHTTPClient(request_timeout=None))
db = _client.db("riksdagen", username="riksdagen", password=os.environ["ARANGO_PWD"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def safe_batch_execute(func, *args, max_retries=5, **kwargs):
    """Executes an ArangoDB batch operation with exponential backoff for lock timeouts."""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # Check if the error is a lock timeout (ERR 1200)
            if "1200" in str(e) or "timeout waiting to lock key" in str(e).lower():
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1.0s, 2.0s, 4.0s
                    continue
            # Raise immediately if it's a different error or we're out of retries
            raise e


def build_mapping() -> dict[str, str]:
    """Return {old_uuid: new_id} for every talk that still has a UUID _key."""
    logger.info("Building old→new key mapping from talks collection...")
    cursor = db.aql.execute(
        """
        FOR t IN talks
            FILTER t.id != null AND t.id != ""
            FILTER t._key != t.id
            RETURN {old: t._key, new: t.id}
        """,
        batch_size=5000,
        ttl=600,
    )
    mapping = {row["old"]: row["new"] for row in cursor}
    logger.info(f"  {len(mapping)} talks need re-keying")
    return mapping


def migrate_chunks(mapping: dict[str, str], dry_run: bool) -> None:
    """
    Update chunks.parent_id from 'talks/{uuid}' → 'talks/{id}'.

    Read phase: stream chunk _key + parent_id in Python (read-only, no locks).
    Write phase: batch UPDATE by _key (point locks, no cross-collection join).
    Must run BEFORE talk re-keying so the mapping is still valid.
    """
    if not db.has_collection("chunks"):
        logger.info("No chunks collection — skipping.")
        return

    logger.info("Step 2: Scanning chunks for stale parent_ids...")
    cursor = db.aql.execute(
        "FOR c IN chunks RETURN {k: c._key, p: c.parent_id}",
        batch_size=10000,
        ttl=3600,
        stream=True,
    )
    updates = []
    for row in cursor:
        parent = row["p"] or ""
        if not parent.startswith("talks/"):
            continue
        old_talk_key = parent[len("talks/"):]
        new_talk_key = mapping.get(old_talk_key)
        if new_talk_key:
            updates.append({"k": row["k"], "p": f"talks/{new_talk_key}"})

    # Deduplicate updates to prevent batch deadlocks on live data
    unique_updates = {u["k"]: u["p"] for u in updates}
    updates = [{"k": k, "p": p} for k, p in unique_updates.items()]

    logger.info(f"  {len(updates)} unique chunks need updating")
    if dry_run:
        logger.info("  [dry-run] skipping writes")
        return

    chunks_col = db.collection("chunks")
    total = 0
    for i in range(0, len(updates), 100):
        batch = [{"_key": u["k"], "parent_id": u["p"]} for u in updates[i : i + 100]]
        safe_batch_execute(chunks_col.update_many, batch, silent=True)
        total += len(batch)
        print(f"  chunks updated: {total}/{len(updates)}", end="\r")
    print()
    logger.info(f"Step 2 done: {total} chunks updated")


def migrate_debates(mapping: dict[str, str], dry_run: bool) -> None:
    """
    Update debates.talk_ids from ["talks/{uuid}", ...] → ["talks/{id}", ...].

    Read phase: stream debate _key + talk_ids in Python (read-only, no locks).
    Write phase: batch UPDATE by _key (point locks, no cross-collection join).
    Must run BEFORE talk re-keying so the mapping is still valid.
    """
    if not db.has_collection("debates"):
        logger.info("No debates collection — skipping.")
        return

    logger.info("Step 3: Scanning debates for stale talk_ids...")
    cursor = db.aql.execute(
        "FOR d IN debates FILTER d.talk_ids != null RETURN {k: d._key, ids: d.talk_ids}",
        batch_size=5000,
        ttl=300,
    )
    updates = []
    for row in cursor:
        new_ids = []
        changed = False
        for tid in row["ids"]:
            if tid.startswith("talks/"):
                old_key = tid[len("talks/"):]
                new_key = mapping.get(old_key)
                if new_key:
                    new_ids.append(f"talks/{new_key}")
                    changed = True
                    continue
            new_ids.append(tid)
        if changed:
            updates.append({"k": row["k"], "ids": new_ids})

    # Deduplicate updates to prevent batch deadlocks on live data
    unique_updates = {u["k"]: u["ids"] for u in updates}
    updates = [{"k": k, "ids": ids} for k, ids in unique_updates.items()]

    logger.info(f"  {len(updates)} unique debates need updating")
    if dry_run:
        logger.info("  [dry-run] skipping writes")
        return

    debates_col = db.collection("debates")
    total = 0
    for i in range(0, len(updates), 100):
        batch = [{"_key": u["k"], "talk_ids": u["ids"]} for u in updates[i : i + 100]]
        safe_batch_execute(debates_col.update_many, batch, silent=True)
        total += len(batch)
        print(f"  debates updated: {total}/{len(updates)}", end="\r")
    print()
    logger.info(f"Step 3 done: {total} debates updated")


def migrate_talks(mapping: dict[str, str], dry_run: bool) -> None:
    """Re-insert talks with new _key, preserve old UUID as anforande_id field."""
    talks_col = db.collection("talks")
    old_keys = list(mapping.keys())
    total = len(old_keys)
    done = 0

    logger.info(f"Step 4: Re-keying {total} talks (batch_size={BATCH_SIZE})...")

    for i in range(0, total, BATCH_SIZE):
        batch_old_keys = old_keys[i : i + BATCH_SIZE]

        docs = list(db.aql.execute(
            "FOR k IN @keys RETURN DOCUMENT(CONCAT('talks/', k))",
            bind_vars={"keys": batch_old_keys},
            batch_size=BATCH_SIZE,
        ))

        new_docs = []
        for doc in docs:
            if doc is None:
                continue
            old_key = doc["_key"]
            new_key = mapping.get(old_key)
            if not new_key:
                continue
            new_doc = {k: v for k, v in doc.items() if k not in ("_key", "_id", "_rev")}
            new_doc["_key"] = new_key
            new_doc["anforande_id"] = old_key  # preserve UUID
            new_docs.append(new_doc)

        done += len(new_docs)
        print(f"  talks {done}/{total}", end="\r")

        if dry_run:
            continue

        if new_docs:
            safe_batch_execute(talks_col.insert_many, new_docs, overwrite=True)
        
        safe_batch_execute(talks_col.delete_many, [{"_key": k} for k in batch_old_keys], silent=True)

    print()
    if dry_run:
        logger.info(f"  [dry-run] {done} talks would be re-keyed")
    else:
        logger.info(f"Step 4 done: {done} talks re-keyed")


def rekey_chunks(dry_run: bool) -> None:
    """
    Re-key chunks from '{uuid}:{idx}' to '{short_id}:{idx}'.

    Talks are already re-keyed; use talks.anforande_id (preserved old UUID) for the mapping.
    Runs server-side via batched AQL INSERT+REMOVE to avoid transferring embedding vectors.
    Requires a persistent index on talks.anforande_id for performance.
    """
    if not db.has_collection("chunks"):
        logger.info("No chunks collection — skipping chunk re-key.")
        return

    # Ensure index exists so the FILTER tt.anforande_id == old_uuid lookup is fast.
    talks_col = db.collection("talks")
    existing_fields = {f for idx in talks_col.indexes() for f in idx.get("fields", [])}
    if "anforande_id" not in existing_fields:
        logger.info("  Creating persistent index on talks.anforande_id...")
        talks_col.add_persistent_index(fields=["anforande_id"], unique=False, sparse=True)

    if dry_run:
        result = list(db.aql.execute(
            'FOR c IN chunks FILTER CONTAINS(c._key, "-") COLLECT WITH COUNT INTO n RETURN n',
            ttl=120,
        ))
        logger.info(f"  [dry-run] {result[0] if result else 0} chunks would be re-keyed")
        return

    logger.info("Step 5: Re-keying chunks from UUID to short-id format...")
    total = 0
    while True:
        result = list(db.aql.execute(
            """
            FOR c IN chunks
              FILTER CONTAINS(c._key, "-")
              LET parts    = SPLIT(c._key, ":")
              LET old_uuid = parts[0]
              LET idx      = parts[1]
              LET t        = FIRST(FOR tt IN talks FILTER tt.anforande_id == old_uuid LIMIT 1 RETURN tt)
              FILTER t != null
              LET new_key  = CONCAT(t._key, ":", idx)
              LET new_doc  = MERGE(UNSET(c, "_key", "_id", "_rev"),
                                   {_key: new_key, parent_id: CONCAT("talks/", t._key)})
              INSERT new_doc INTO chunks OPTIONS {overwriteMode: "ignore"}
              REMOVE c IN chunks
              LIMIT 200
              RETURN 1
            """,
            ttl=300,
        ))
        batch_count = len(result)
        total += batch_count
        print(f"  chunks re-keyed: {total}", end="\r")
        if batch_count == 0:
            break
    print()
    logger.info(f"Step 5 done: {total} chunks re-keyed")


def main():
    parser = argparse.ArgumentParser(description="Migrate talks _key from UUID to id (dok_id)")
    parser.add_argument("--dry-run", action="store_true", help="Print counts only, make no changes")
    parser.add_argument("--rekey-chunks", action="store_true", help="Also re-key chunks to short-id format (step 5)")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN — no data will be modified ===")
    else:
        logger.info("=== LIVE RUN — data will be modified ===")

    mapping = build_mapping()
    if not mapping:
        logger.info("Nothing to migrate — all talks already use short keys.")
        if args.rekey_chunks:
            rekey_chunks(args.dry_run)
        return

    # Steps 2 and 3 must run before step 4 (talk re-keying) — mapping uses old UUIDs as keys.
    migrate_chunks(mapping, args.dry_run)
    migrate_debates(mapping, args.dry_run)
    migrate_talks(mapping, args.dry_run)

    if args.rekey_chunks:
        rekey_chunks(args.dry_run)

    if args.dry_run:
        logger.info("=== Dry run complete. Re-run without --dry-run to apply. ===")
    else:
        logger.info("=== Migration complete. ===")


if __name__ == "__main__":
    main()