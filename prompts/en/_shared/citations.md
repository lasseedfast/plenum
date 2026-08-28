**Citations**

- Cite with `[src:ID]` tags placed immediately after the claim they support.
- Each search hit opens with its `[src:ID]` tag, followed by `SPEAKER`, `PARTY` and `DATE`
  blocks. Longer results are compacted into an enriched tag,
  `[src:ID | Speaker (Party) | date]` — there, the text between `src:` and the first `|` is
  the id, and the speaker and date that follow are the ground truth for who said what.
  Copy the whole tag verbatim; do not restructure, rename or split it.
- Only cite ids you have actually seen in a tool result. Never restate a speaker or party
  from memory or general knowledge, and never attach a tag to a claim it does not support.
- Example: `The ROT deduction was extended in 2015[src:$speech_id_example].`
- If a claim is general and rests on more than about eight sources, state it without a
  citation rather than picking an arbitrary few.
- Do **not** write a sources section (`### Källor`) — the system generates it.
- Do **not** use `[1]`, `[2]` numbering — only `[src:ID]` tags from tool results.
- Do **not** put `[src:...]` on `database_query` results. Counts and aggregates have no
  individual source id; just state the numbers.
