Surface a concrete finding to the user while research continues.

Use it for something specific and grounded that you have actually seen in a tool result — a
speaker, a count, a pattern. Keep `message` to one or two sentences in $answer_language, be
concrete, and do not overstate. The message is the card's header and should explain what the
attached data shows.

There are three kinds of card, chosen by which argument you pass:

- **Plain insight** — `message` alone.
- **Search card** — `message` + `hit_ids`: the speech or motion ids you saw in an earlier
  search result. The backend looks up speaker, party, date and summary for each id itself;
  do not copy that data in by hand.
- **Stats card** — `message` + `sql`: re-pass the query you gave `database_query` (or a
  simplified variant). The backend re-runs it and builds the table; do not write rows yourself.

Add `speaker_ids` (person_id values) to highlight members' portraits, always paired with
`speaker_ids_context` explaining in one or two sentences why those people matter here. If
you name a person in `message`, include their person_id so the frontend can link to them.

Examples (write `message` in $answer_language; the shapes matter, not these words):

    share_insight(
        message="<one concrete finding naming the people involved>",
        speaker_ids=["$person_id_example"],
        speaker_ids_context="<why these people matter to the finding>",
    )

    share_insight(
        message="<what the numbers show>",
        sql="SELECT year, COUNT(*) FROM speeches WHERE search_vector @@ "
            "websearch_to_tsquery('$fts_config', '<topic>') GROUP BY year ORDER BY year",
    )
