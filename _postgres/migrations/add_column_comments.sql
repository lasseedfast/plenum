-- What the model may read, and what each column means.
--
-- The comment on a column decides whether it reaches the prompt, and carries the
-- meaning that introspection cannot derive. scripts/generate_schema_prompt.py reads
-- these and writes prompts/en/_shared/schema_tables.md.
--
--   '-'             exposed; the name and type already say it
--   '[hide] reason' not shown to the model, with the reason recorded
--   any other text  exposed, and the text becomes the note in the prompt
--   no comment      undecided — tests/test_schema_comments.py fails until one exists
--
-- Keep notes to one line of 80 characters or less. Anything longer belongs in the
-- hand-written prose in prompts/en/_shared/schema.md, not here.
--
-- Adapting this for another parliament: keep the structure, rewrite the values that
-- are specific to this data (id formats, committee codes, coverage percentages).
-- See docs/SCHEMA.md.

-- ── speeches ────────────────────────────────────────────────────────────────────
COMMENT ON COLUMN speeches.id                  IS 'Speech key: {source_doc_id}-{sequence}, e.g. GH09116-16.';
COMMENT ON COLUMN speeches.text                IS '-';
COMMENT ON COLUMN speeches.section_title       IS 'What the debate was about. The topic to search or group by.';
COMMENT ON COLUMN speeches.title               IS 'Title of the protocol, not of the speech. Use section_title.';
COMMENT ON COLUMN speeches.sequence            IS 'Position within the source protocol, from 0.';
COMMENT ON COLUMN speeches.activity_type       IS 'Debate form. 66% filled, and "-" and "" occur as values.';
COMMENT ON COLUMN speeches.speaker_name        IS '-';
COMMENT ON COLUMN speeches.party               IS '-';
COMMENT ON COLUMN speeches.person_id           IS 'Joins people.person_id.';
COMMENT ON COLUMN speeches.date                IS '-';
COMMENT ON COLUMN speeches.year                IS '-';
COMMENT ON COLUMN speeches.session_year        IS 'Session start year: 2016 means 2016/17.';
COMMENT ON COLUMN speeches.debate_id           IS 'Joins debates.id.';
COMMENT ON COLUMN speeches.is_reply            IS 'A reply in an exchange rather than a main speech.';
COMMENT ON COLUMN speeches.summary             IS 'Model-written summary of the speech.';
COMMENT ON COLUMN speeches.tags                IS 'Model-assigned topic tags.';
COMMENT ON COLUMN speeches.arguments           IS 'Model-extracted claims the speech argues for.';
COMMENT ON COLUMN speeches.search_vector       IS 'Full-text index over the speech text. Use with @@.';
COMMENT ON COLUMN speeches.related_doc_id      IS '[hide] Looks like a documents join but matches 29 of 428k rows.';
COMMENT ON COLUMN speeches.url_video           IS '[hide] Not populated; media feature not shipped.';
COMMENT ON COLUMN speeches.url_session         IS '[hide] Not populated; media feature not shipped.';
COMMENT ON COLUMN speeches.url_audio           IS '[hide] Not populated; media feature not shipped.';
COMMENT ON COLUMN speeches.url_audio_file      IS '[hide] Not populated; media feature not shipped.';
COMMENT ON COLUMN speeches.audio_start_seconds IS '[hide] Not populated; media feature not shipped.';
COMMENT ON COLUMN speeches.source_speech_id    IS '[hide] Ingest bookkeeping.';
COMMENT ON COLUMN speeches.source_datetime     IS '[hide] Ingest bookkeeping; use date.';
COMMENT ON COLUMN speeches.source_doc_number   IS '[hide] Ingest bookkeeping.';
COMMENT ON COLUMN speeches.source_record_id    IS '[hide] Ingest bookkeeping.';
COMMENT ON COLUMN speeches.source_doc_id       IS '[hide] Ingest bookkeeping; it is the first half of id.';
COMMENT ON COLUMN speeches.tagging_failed      IS '[hide] Pipeline state.';
COMMENT ON COLUMN speeches.arguments_corrected IS '[hide] Pipeline state.';
COMMENT ON COLUMN speeches.summary_embedding   IS '[hide] Vector; reached through vector_search_debates.';

-- ── people ──────────────────────────────────────────────────────────────────────
COMMENT ON COLUMN people.person_id         IS '-';
COMMENT ON COLUMN people.name              IS '-';
COMMENT ON COLUMN people.party             IS '-';
COMMENT ON COLUMN people.birth_year        IS 'Stored as text, not a number. Cast before comparing.';
COMMENT ON COLUMN people.gender            IS 'Values: man, kvinna, Okänt.';
COMMENT ON COLUMN people.constituency      IS 'Electoral district. 65% filled.';
COMMENT ON COLUMN people.active            IS 'Currently sitting.';
COMMENT ON COLUMN people.status            IS '[hide] 242 free-text values; use active.';
COMMENT ON COLUMN people.home_town         IS '[hide] 0.8% filled.';
COMMENT ON COLUMN people.first_name        IS '[hide] Use name.';
COMMENT ON COLUMN people.last_name         IS '[hide] Use name.';
COMMENT ON COLUMN people.sort_name         IS '[hide] Use name.';
COMMENT ON COLUMN people.source_record_id  IS '[hide] Ingest bookkeeping.';
COMMENT ON COLUMN people.source_record_guid IS '[hide] Ingest bookkeeping.';
COMMENT ON COLUMN people.source_id         IS '[hide] Ingest bookkeeping.';
COMMENT ON COLUMN people.source_url        IS '[hide] Presentation only.';
COMMENT ON COLUMN people.image_url_small   IS '[hide] Presentation only.';
COMMENT ON COLUMN people.image_url_medium  IS '[hide] Presentation only.';
COMMENT ON COLUMN people.image_url_large   IS '[hide] Presentation only.';
COMMENT ON COLUMN people.assignments       IS '[hide] jsonb; no useful SQL shape.';
COMMENT ON COLUMN people.contact_details   IS '[hide] jsonb; no useful SQL shape.';

-- ── debates ─────────────────────────────────────────────────────────────────────
COMMENT ON COLUMN debates.id                IS 'Debate key, e.g. 1995-02-08:1.';
COMMENT ON COLUMN debates.date              IS '-';
COMMENT ON COLUMN debates.summary           IS 'Model-written summary of the whole debate.';
COMMENT ON COLUMN debates.num_talks         IS 'How many speeches the debate contains.';
COMMENT ON COLUMN debates.talk_ids          IS 'The speech ids, in order.';
COMMENT ON COLUMN debates.talk_summaries    IS '[hide] Bulk duplicate of speeches.summary.';
COMMENT ON COLUMN debates.summary_embedding IS '[hide] Vector; reached through vector_search_debates.';

-- ── documents ───────────────────────────────────────────────────────────────────
COMMENT ON COLUMN documents.doc_id           IS 'Document key, e.g. GE02A1.';
COMMENT ON COLUMN documents.title            IS '-';
COMMENT ON COLUMN documents.text             IS '-';
COMMENT ON COLUMN documents.date             IS '-';
COMMENT ON COLUMN documents.author_names     IS '-';
COMMENT ON COLUMN documents.session_label    IS 'Session as printed, e.g. 2011/12.';
COMMENT ON COLUMN documents.session_year     IS 'Session start year: 2011 means 2011/12.';
COMMENT ON COLUMN documents.subtype          IS 'Motion kind: Enskild, Kommitté, Parti, Flerparti. "-" occurs.';
COMMENT ON COLUMN documents.committee        IS 'Committee code, e.g. SoU, TU, UbU. 27 values.';
COMMENT ON COLUMN documents.parties          IS 'Author party codes. Case varies; fold with upper().';
COMMENT ON COLUMN documents.num_proposals    IS 'How many document_proposals rows this document has.';
COMMENT ON COLUMN documents.search_vector    IS 'Full-text index over title, text and proposals. Use with @@.';
COMMENT ON COLUMN documents.status           IS '[hide] Publishing workflow state, not the political outcome.';
COMMENT ON COLUMN documents.doc_type         IS '[hide] Always "motion"; this table holds motions only.';
COMMENT ON COLUMN documents.designation      IS '[hide] Short in-session label; doc_id identifies a document.';
COMMENT ON COLUMN documents.subtitle         IS '[hide] Authorship line; author_names is the structured form.';
COMMENT ON COLUMN documents.summary          IS '[hide] Not populated.';
COMMENT ON COLUMN documents.proposals_text   IS '[hide] Concatenation; use document_proposals.';
COMMENT ON COLUMN documents.proposals_raw    IS '[hide] jsonb; use document_proposals.';
COMMENT ON COLUMN documents.attachments      IS '[hide] jsonb; no useful SQL shape.';
COMMENT ON COLUMN documents.has_text         IS '[hide] Nearly always true; not a useful filter.';
COMMENT ON COLUMN documents.url_text         IS '[hide] Presentation only.';
COMMENT ON COLUMN documents.url_html         IS '[hide] Presentation only.';
COMMENT ON COLUMN documents.url_pdf          IS '[hide] Presentation only.';
COMMENT ON COLUMN documents.source_record_id IS '[hide] Ingest bookkeeping.';
COMMENT ON COLUMN documents.source_updated_at IS '[hide] Ingest bookkeeping.';
COMMENT ON COLUMN documents.published_at     IS '[hide] Ingest bookkeeping; use date.';

-- ── document_authors ────────────────────────────────────────────────────────────
COMMENT ON COLUMN document_authors.doc_id    IS 'Joins documents.doc_id.';
COMMENT ON COLUMN document_authors.person_id IS 'Joins people.person_id.';
COMMENT ON COLUMN document_authors.name      IS '-';
COMMENT ON COLUMN document_authors.party     IS 'Party code. Case varies in older rows; fold with upper().';
COMMENT ON COLUMN document_authors.ordinal   IS 'Signing order; 0 is the first author.';
COMMENT ON COLUMN document_authors.role      IS '[hide] Always "undertecknare".';

-- ── document_proposals ──────────────────────────────────────────────────────────
COMMENT ON COLUMN document_proposals.id       IS 'Proposal key: {doc_id}:{ordinal}.';
COMMENT ON COLUMN document_proposals.doc_id   IS 'Joins documents.doc_id.';
COMMENT ON COLUMN document_proposals.ordinal  IS 'Position within the document, from 0.';
COMMENT ON COLUMN document_proposals.number   IS 'Proposal number as printed. Text, not a number.';
COMMENT ON COLUMN document_proposals.text     IS '-';
COMMENT ON COLUMN document_proposals.committee_recommendation IS 'What the committee recommended. 76% filled.';
COMMENT ON COLUMN document_proposals.chamber_decision IS 'Outcome. 77% filled; see the decision values below.';
COMMENT ON COLUMN document_proposals.handled_in IS 'Committee report that handled it, e.g. 2001/02:TU2.';
COMMENT ON COLUMN document_proposals.embedding IS '[hide] Vector; reached through vector_search_documents.';
