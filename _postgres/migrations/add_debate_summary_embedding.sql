ALTER TABLE debates ADD COLUMN IF NOT EXISTS summary_embedding vector(384);

CREATE INDEX IF NOT EXISTS debates_summary_embedding_idx
    ON debates USING hnsw (summary_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
