Full-text and metadata search over speeches ($speech_plural) held in the chamber, using
PostgreSQL full-text search. Hits are ranked by relevance (ts_rank_cd).

Supports quoted "phrases", AND/OR/NOT, and year ranges written as `år:2018-2022`, plus
filtering by party, speaker, person id and year range.

Use `return_snippets=True` to get highlighted excerpts instead of full texts — good for
scanning whether results are relevant before reading them properly. Always pass a `limit`.

If a search returns fewer results than your limit, or reports `limit reached: False`, you
have already retrieved everything available. Do not repeat the same search with a higher
limit.

Prefer `person_ids` over `people` when you have the ids from an earlier result: it is an
exact match rather than a name match.

When NOT to use:
- Fuzzy or semantic similarity → `vector_search`
- Counts, aggregations or joins → `database_query`
