"""
Chunks and embeds all speeches that are not yet represented in the 'speech_chunks' table.


Pipeline:
  1. Find all speeches with no chunk rows in PostgreSQL via LEFT JOIN.
  2. Split each talk's text into speech_chunks (max 500 chars) using TextChunker.
  3. Generate embeddings via Ollama in parallel (3 workers, batches of 20).
  4. Insert chunk rows into the 'speech_chunks' table with id = "{speech_id}:{chunk_index}".

The search_vector column on speeches is kept in sync by a trigger – no manual update needed.

Usage:
  python scripts/make_embeddings.py
"""
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logging.getLogger("httpx").setLevel(logging.WARNING)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap  # noqa: E402,F401  — sets cwd and sys.path to the project root
from parliament import PARLIAMENT  # noqa: E402
from postgres_client import pg  # noqa: E402
from utils import TextChunker  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

EMBED_DIM = PARLIAMENT.embeddings.dimension
EMBED_BATCH = 20
MAX_WORKERS = 3
INSERT_BATCH = 100


def _make_embeddings(texts: list[str]) -> list[list[float]]:
    # OpenAI-compatible vLLM endpoint (VLLM_EMBEDDING_HOST/LLM_MODEL_EMBEDDING),
    # same as query-time embeddings — keeps documents and queries in one space.
    return pg.make_embeddings(texts)


def _embed_batch(chunk_batch: list[dict]) -> list[dict]:
    """Embed a batch of chunk dicts (adds 'embedding' field)."""
    texts = [c["text"] for c in chunk_batch]
    embeddings = _make_embeddings(texts)
    for i, chunk in enumerate(chunk_batch):
        chunk["embedding"] = embeddings[i]
    return chunk_batch


def _chunk_docs(missing: list[dict], text_key: str, parent_key: str) -> list[list[dict]]:
    """Split docs into chunk dicts and group them into embed batches."""
    all_batches: list[list[dict]] = []
    for doc in missing:
        doc_id = doc["id"]
        text = (doc.get(text_key) or "").strip()
        if not text:
            continue
        speech_chunks = TextChunker(chunk_limit=500).chunk(text)
        _chunks = [
            {
                "id": f"{doc_id}:{idx}",
                parent_key: doc_id,
                "chunk_index": idx,
                "text": content,
            }
            for idx, content in enumerate(speech_chunks)
            if content and content.strip()
        ]
        for i in range(0, len(_chunks), EMBED_BATCH):
            batch = _chunks[i : i + EMBED_BATCH]
            if batch:
                all_batches.append(batch)
    return all_batches


def _embed_and_insert(all_batches: list[list[dict]], insert_sql: str, parent_key: str) -> int:
    """Embed all batches in parallel and bulk-insert the chunk rows."""
    if not all_batches:
        logger.info("No speech_chunks to embed.")
        return 0

    logger.info(f"Embedding {len(all_batches)} batches via Ollama …")

    def _rows(speech_chunks: list[dict]) -> list[tuple]:
        return [
            (c["id"], c[parent_key], c["chunk_index"], c["text"], c["embedding"])
            for c in speech_chunks
        ]

    total_inserted = 0
    pending: list[dict] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(_embed_batch, batch) for batch in all_batches]
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            pending.extend(result)
            print(
                f"  batches embedded: {completed}/{len(all_batches)} | "
                f"speech_chunks pending insert: {len(pending)}",
                end="\r",
            )
            if len(pending) >= INSERT_BATCH:
                pg.execute_values(insert_sql, _rows(pending))
                total_inserted += len(pending)
                pending = []

    if pending:
        pg.execute_values(insert_sql, _rows(pending))
        total_inserted += len(pending)

    print()
    logger.info(f"Done. Inserted {total_inserted} speech_chunks into PostgreSQL.")
    return total_inserted


def make_embeddings() -> int:
    """
    Find speeches with no speech_chunks and generate + insert embeddings.
    Returns the total number of chunk rows inserted.
    """
    # Find speeches that have no rows in the speech_chunks table.
    # SET LOCAL disables parallel hash join for this query — it otherwise spills
    # into the Postgres container's small /dev/shm and fails with DiskFull.
    missing = pg.execute(
        """
        SET LOCAL max_parallel_workers_per_gather = 0;
        SELECT t.id, t.text
        FROM speeches t
        LEFT JOIN speech_chunks c ON c.speech_id = t.id
        WHERE c.id IS NULL
          AND t.text IS NOT NULL
          AND t.text != ''
        """
    )
    logger.info(f"Found {len(missing)} speeches without speech_chunks")

    all_batches = _chunk_docs(missing, text_key="text", parent_key="speech_id")

    INSERT_SQL = """
    INSERT INTO speech_chunks (id, speech_id, chunk_index, text, embedding)
    VALUES %s
    ON CONFLICT (id) DO NOTHING
    """
    return _embed_and_insert(all_batches, INSERT_SQL, parent_key="speech_id")


def make_motion_embeddings(year: int | None = None) -> int:
    """
    Find documents with no speech_chunks and generate + insert embeddings.
    Optional year filter for running the backfill in slices.
    Returns the total number of chunk rows inserted.
    """
    sql = """
        SET LOCAL max_parallel_workers_per_gather = 0;
        SELECT m.doc_id AS id, m.text
        FROM documents m
        LEFT JOIN document_chunks c ON c.doc_id = m.doc_id
        WHERE c.id IS NULL
          AND m.has_text
        """
    params = None
    if year is not None:
        sql += " AND m.session_year = %s"
        params = (year,)
    missing = pg.execute(sql, params)
    logger.info(f"Found {len(missing)} documents without speech_chunks")

    all_batches = _chunk_docs(missing, text_key="text", parent_key="doc_id")

    INSERT_SQL = """
    INSERT INTO document_chunks (id, doc_id, chunk_index, text, embedding)
    VALUES %s
    ON CONFLICT (id) DO NOTHING
    """
    return _embed_and_insert(all_batches, INSERT_SQL, parent_key="doc_id")


def make_yrkande_embeddings(limit: int | None = None) -> int:
    """
    Embed document_proposals rows that have no embedding yet. Each yrkande (text)
    is a short, self-contained proposal, so it is embedded whole (no chunking).
    Rows already exist (populated from the proposals_raw JSONB), so this UPDATEs them.
    Returns the number of yrkanden embedded.
    """
    sql = (
        "SELECT id, text FROM document_proposals "
        "WHERE embedding IS NULL AND text IS NOT NULL AND text != ''"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    missing = pg.execute(sql)
    logger.info(f"Found {len(missing)} yrkanden without embeddings")

    items = [{"id": r["id"], "text": r["text"]} for r in missing]
    batches = [items[i : i + EMBED_BATCH] for i in range(0, len(items), EMBED_BATCH)]
    if not batches:
        logger.info("No yrkanden to embed.")
        return 0

    logger.info(f"Embedding {len(batches)} yrkande batches via vLLM …")
    UPDATE_SQL = "UPDATE document_proposals SET embedding = %s WHERE id = %s"

    total = 0
    pending: list[dict] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(_embed_batch, batch) for batch in batches]
        for future in as_completed(futures):
            pending.extend(future.result())
            completed += 1
            print(f"  batches embedded: {completed}/{len(batches)} | pending update: {len(pending)}", end="\r")
            if len(pending) >= INSERT_BATCH:
                pg.execute_many(UPDATE_SQL, [(c["embedding"], c["id"]) for c in pending])
                total += len(pending)
                pending = []
    if pending:
        pg.execute_many(UPDATE_SQL, [(c["embedding"], c["id"]) for c in pending])
        total += len(pending)

    print()
    logger.info(f"Done. Embedded {total} yrkanden.")
    return total


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode == "documents":
        year = int(sys.argv[2]) if len(sys.argv) > 2 else None
        make_motion_embeddings(year=year)
    elif mode == "yrkanden":
        make_yrkande_embeddings()
    else:
        make_embeddings()
