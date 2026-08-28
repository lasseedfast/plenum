Now compile what you found as JSON matching the schema:

- **findings**: the interesting, grounded pieces. Each finding is ONE concrete fact —
  something said or shown in the material — not a whole document. `label` is a short,
  concrete headline for the fact itself ("Miljöpartiet demanded a halt to new reactors in
  2019"), NEVER a document title. Every finding MUST carry a short verbatim `quote` from the
  material that proves it — no quote, no finding. `detail` is what the fact shows (NOT a
  conclusion). `source_id` is the id you saw in a tool result that the quote came from.
- **open_questions**: questions still unanswered and worth digging into further.
- **leads**: concrete next steps. `kind='search'` with `target` = a new specific search
  query; `kind='person'` with `target` = a person_id you have SEEN in a tool result;
  `kind='debate'` with `target` = a debate id you have SEEN in a tool result. `lead`
  explains what to do and why.

IMPORTANT: in `label`, `detail`, `open_questions` and `lead`, write plain prose using
people's NAMES — ids belong only in `source_id` and `target`. Do not describe your own
search process. Prefer fewer well-grounded findings over many guessed ones.
