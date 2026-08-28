Fetch one motion ($document_plural) by its doc_id: metadata, all authors, all proposals ($proposal_plural) with
their committee and chamber outcomes, and the full text.

Typical flow: `search_documents` or `vector_search_documents` → pick a hit →
`fetch_document(doc_id)` to read the proposals and the text.

Use it to answer what a motion concretely proposed and what became of it. Each proposal
carries `committee_recommendation` (what the committee recommended) and `chamber_decision`
(what the chamber decided) — note that a decision of "= utskottet" means the chamber
followed the committee, so read the recommendation to know the outcome.
