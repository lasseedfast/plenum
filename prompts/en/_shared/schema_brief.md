{{include:_shared/schema_columns}}
Joins: `speeches.person_id = people.person_id` · `speeches.debate_id = debates.id` ·
`document_authors.doc_id = documents.doc_id` · `document_proposals.doc_id = documents.doc_id`.

**Searching text.** Never `LIKE`/`ILIKE` — use the index:
`WHERE search_vector @@ websearch_to_tsquery('$fts_config', '...')`, and never put that
expression inside `SELECT`, `SUM` or `CASE`.

**Call `database_schema` before writing anything beyond a simple count.** It returns what
these column names do not tell you: which are only partly filled, what the decision values
mean (filtering on the obvious "approved" value alone is the commonest way to get a badly
wrong answer here), the party-code casing trap, and worked examples. It costs one call and
saves a wrong answer.
