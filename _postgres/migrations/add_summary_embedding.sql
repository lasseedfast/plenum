ALTER TABLE talks ADD COLUMN IF NOT EXISTS summary_embedding vector(384);

CREATE INDEX IF NOT EXISTS talks_summary_embedding_idx
    ON talks USING hnsw (summary_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
