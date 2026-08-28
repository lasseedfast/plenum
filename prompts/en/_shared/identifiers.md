**Identifiers — copy them, never build them.**

Every id you use must have appeared verbatim in a tool result. Do not construct, guess,
shorten or "correct" an id, and do not carry one over from world knowledge.

- **Speech id** — like `$speech_id_example`: the source document id, a hyphen, then the
  speech's sequence number inside that document. A bare document id with no `-N` suffix is
  **not** a speech id and will not resolve.
- **Motion (document) id** — like `$doc_id_example`.
- **Debate id** — like `$debate_id_example`: date, colon, index.
- **Person id** — like `$person_id_example`: a numeric string. If you do not have one from a
  tool result, filter by name instead; never invent a placeholder.
