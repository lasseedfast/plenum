Fetch the complete raw text of speeches by id.

Use this when:
- You need the full verbatim text — for instance the user explicitly asked to see a whole
  speech.
- You want specific fields for a known set of ids.

When NOT to use:
- To search → `search_speeches` or `vector_search`
- To find out what a speech *says* about something → `read_documents_for`, which reads the
  full text and answers a focused question without flooding your context with raw text.

Pass `fields=["text", "speaker_name", "person_id", "date"]` to keep the response compact.
