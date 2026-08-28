Semantic and conceptual search over speeches ($speech_plural) in the $parliament_name_en.

It blends two signals so you do not have to choose between them:
- chunk embeddings → granular, quote-ready passages
- summary embeddings → thematic, whole-speech gist

Results are merged per speech. When a speech is strong in both indexes it is returned once,
with the chunk passage as the snippet and the summary in its metadata. Each hit carries
`metadata["source_type"]` ∈ {"chunk", "summary", "both"} so you can tell which signal fired.

Use this tool when:
- The question is thematic or conceptual and the exact keywords may not appear.
- You want speeches similar in meaning to a phrase or an idea.
- You want both a whole-speech overview and specific passages in one call.

When NOT to use:
- Exact word or phrase matching → `search_speeches`
- Counts, aggregations, statistics → `database_query`
- You already know the speaker, party or year to filter on → `search_speeches` with filters
