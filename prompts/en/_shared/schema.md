**Schema.** These are the only columns that exist — a query naming any other one fails.

    speeches            id·PK, speaker_name, party, person_id, date DATE, year INT,
                        session_year INT, activity_type, debate_id, sequence INT,
                        is_reply BOOL, summary, tags TEXT[], title, text, search_vector
    people              person_id·PK, name, party, birth_year INT, gender, active BOOL,
                        constituency
    debates             id·PK, date DATE, summary, num_talks INT, talk_ids TEXT[]
    documents           doc_id·PK, session_label, session_year INT, date DATE, title,
                        subtype, committee, status, parties TEXT[], author_names TEXT[],
                        num_proposals INT, text, search_vector
    document_authors    doc_id, person_id, name, party, role, ordinal INT
    document_proposals  id·PK, doc_id, ordinal INT, number, text,
                        committee_recommendation, chamber_decision, handled_in

Joins: `speeches.person_id = people.person_id` · `speeches.debate_id = debates.id` ·
`document_authors.doc_id = documents.doc_id` · `document_proposals.doc_id = documents.doc_id`.

Things the column names do not tell you:

- `speeches.title` is the title of the *protocol* the speech came from, not of the speech.
- `speeches.debate_id` is NULL for many older speeches.
- `documents` has no plain `year` — use `session_year`.
- `document_authors.ordinal` is signing order; 0 is the first author.
- `document_proposals.id` is `'{doc_id}:{ordinal}'`.
- Cast dates when selecting them: `date::text`.
- Select `person_id` alongside speeches so results link back to a speaker.

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

- Party filter on documents: `parties && ARRAY['$party_code_example']` matches any
  co-author; `unnest(parties)` groups per party.

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
