{{include:_shared/schema_tables}}
Joins: `speeches.person_id = people.person_id` · `speeches.debate_id = debates.id` ·
`document_authors.doc_id = documents.doc_id` · `document_proposals.doc_id = documents.doc_id`.

Also worth knowing:

- `documents` has no plain `year` — use `session_year`.
- Cast dates when selecting them: `date::text`.
- Select `person_id` alongside speeches so results link back to a speaker.
- `summary`, `tags` and `arguments` are written by a model from the source text.
  Use them to find things; quote only `text`, which is what was actually said.

**Full-text search, never LIKE.** Use
`WHERE search_vector @@ websearch_to_tsquery('$fts_config', '...')` — it uses the GIN index
and handles quoted phrases, `OR`, `-` to exclude, and stemming. It covers the text on
`speeches`, and title + proposals + text on `documents`.

- **Never** `text @@ ...` or `LIKE`/`ILIKE` on `text`: no index, whole-table scan, and wrong
  matches ('ai' hits 'Thai').
- **Never** put `search_vector @@ ...` inside `SELECT`, `SUM` or `CASE` — it then runs per
  row without the index and takes 30–60 seconds. For two counts, filter in two CTEs:

      WITH a AS (SELECT id, party FROM speeches
                 WHERE search_vector @@ websearch_to_tsquery('$fts_config', 'q1')),
           b AS (SELECT id FROM speeches
                 WHERE search_vector @@ websearch_to_tsquery('$fts_config', 'q2'))
      SELECT a.party, COUNT(*) total, COUNT(b.id) matches
        FROM a LEFT JOIN b USING (id) GROUP BY 1

**Party values.** The real codes are $party_codes. `speeches.party` also holds NULL and
$non_party_values — the presiding chair, not a party. Exclude those from per-party counts.

**`chamber_decision` values.** Filtering on the plain "approved" value alone is the commonest
way to get a badly wrong answer; here it covers barely one percent of rows.

$decision_values

To count what a party actually got through, take the approved and partly-approved values
*and* the rows deferring to the committee, resolved via `committee_recommendation`. If you
give one number, say which of these it covers.

**Examples**

    -- speeches on a topic per party, using the index (query words in $answer_language_native)
    SELECT party, COUNT(*) c FROM speeches
      WHERE party IN ($party_codes_sql)
        AND search_vector @@ websearch_to_tsquery('$fts_config', '<topic OR synonym>')
      GROUP BY party ORDER BY c DESC

    -- how one party's proposals fared
    SELECT p.chamber_decision, p.committee_recommendation, COUNT(*) c
      FROM document_proposals p JOIN documents d USING (doc_id)
      WHERE d.parties && ARRAY['$party_code_example']
      GROUP BY 1, 2 ORDER BY c DESC
