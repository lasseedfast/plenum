Read the FULL text of up to 6 speeches or documents and get a focused answer to ONE specific
question about them.

A reading assistant reads the complete texts and returns a short grounded answer with
`[src:ID]` tags and verbatim quotes. Ask one concrete question per call.

Use this tool when:
- You need to know what specific documents actually SAY — positions, arguments, exact
  statements — not just their metadata.
- A snippet or summary is too thin and you would otherwise fetch the full text.

This is the default way to go deeper than snippets. Prefer it over `fetch_speeches`: you get
the substance without the raw text filling your context.

Motion ids work here too — the full motion text is read.

When NOT to use:
- To search → `search_speeches` or `vector_search`
- When the user explicitly wants the complete raw text → `fetch_speeches`
