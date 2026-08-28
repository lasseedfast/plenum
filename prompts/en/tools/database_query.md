Execute a read-only PostgreSQL query against the $parliament_name_en database.

Use this for structured questions about metadata: party breakdowns, aggregations, speaker
statistics, comparisons over time, and content-based counts via full-text search.

✅ WHEN TO USE
- Counting or ranking: "how many speeches per party?", "the 10 most active speakers in 2020?"
- Aggregations and joins: activity per party over time, speeches joined with member demographics
- Full-text aggregations: "how many speeches per party mentioned AI?"
- Outcomes of proposals ($proposal_plural): what a party proposed and how the chamber decided

❌ WHEN NOT TO USE
- Semantic or conceptual search → `vector_search`
- Exact word or phrase search → `search_speeches`
- Fetching whole documents → `fetch_speeches` or `fetch_document`

{{include:_shared/schema}}
