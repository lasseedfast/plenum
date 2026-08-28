Look up one debate by its id and return its speeches with per-speech summaries. Every
returned speech is registered as a citable source.

Typical flow: `vector_search_debates(query)` to discover debate ids, then
`fetch_debate(debate_id, query=query)` on the best match.

**Pass the same `query`.** Long debates are trimmed by semantic relevance to it. Without a
query you get a chronological slice instead, and a `note` field telling you how many
speeches were left out.

You get the debate summary plus a compact list of speeches: id, speaker_name, party,
person_id and a per-speech summary. Cite the individual speeches, never the debate itself.
