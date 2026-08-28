Full-text and metadata search over motions ($document_plural) — written proposals submitted
by members — as opposed to `search_speeches`, which searches speeches held in the chamber.

Speeches are the primary source; search them first. Use this tool as a complement: to find
the concrete proposals ($proposal_plural) behind positions taken in debate, to add committee and
chamber outcomes, or when the question is explicitly about motions.

Same query syntax and filters as `search_speeches`: quoted "phrases", AND/OR/NOT, year
ranges as `år:2018-2022`. `parties` and `people` match any co-author, not just the first.

Use `return_snippets=True` to scan before reading in full. Always pass a `limit`.

A small number of documents have no extracted full text; those hits carry a `note` saying so.
