# Porting plenum to another parliament

Four things are country-specific: a configuration file, an ingest adapter, a set of
prompts, and the UI language. Nothing else in the application should need editing —
if it does, that is a bug worth reporting.

Expect the adapter to be most of the work. Everything else is filling in values.

## 1. `parliament.yaml`

Copy it and change the values. The fields that matter most:

**`language.fts_config`** — a PostgreSQL text-search configuration. Run
`SELECT cfgname FROM pg_ts_config;` to see what your server has. Postgres ships
stemmers for around two dozen languages. If yours is not among them, `simple` works
but gives no stemming, so `kärnkraften` will not match a search for `kärnkraft`.
Set it on the database too, because the search-vector triggers read it:

```sql
ALTER DATABASE plenum SET app.fts_config = 'bulgarian';
```

The application refuses to start if the two disagree. That check exists because a
mismatch does not raise anything — it just makes almost every search return nothing.

**`vocabulary`** — your parliament's own words for a speech, a document, a proposal,
a committee. These are injected into prompts, so the model reasons in your domain's
language rather than a translation of Swedish. Words like *yrkande* and *riksmöte*
have no stable English equivalent, which is exactly why they live here rather than in
column names.

**`parties`** — code, display name, colour, and whether the party is current. The
frontend reads these at runtime; there is no per-party CSS to edit.

**`ids`** — the shape of your source's identifiers, with a real example. These are
templated into prompts so the model can recognise and construct them.

**`activity_types`** — your chamber's debate/activity codes and what they mean. Used
as filters in the UI and as context in prompts.

## 2. An ingest adapter

Write `ingest/adapters/<yourparliament>.py` and point `sources.adapter` at it. The
adapter's job is to turn your source's records into the shape the loader expects; it
is the only place that should know your open-data portal's field names.

Read `ingest/adapters/riksdagen.py` as the worked example. It handles a quirk worth
anticipating: the Riksdag's XML-to-JSON conversion emits a single child as an object
and multiple children as an array, so every repeated field needs normalising before
use. Most portals that started as XML do something similar.

If your parliament publishes bulk archives, `kind: zip-dataset` with a
`url_template` and a list of ranges is likely enough. If it only has a per-record
API, you will need a fetch loop in the adapter.

Data quality is usually the real difficulty: missing speaker identifiers, documents
that exist only as scanned PDFs, and party codes that change when parties merge or
rename. Decide early whether to correct these on ingest or preserve them as-is and
handle them in queries. plenum preserves the source values and explains them via
`decisions` and `document_subtypes`, on the principle that a research tool should not
silently rewrite the record.

## 3. Prompts

Copy `prompts/sv/` to `prompts/<your-lang>/` and translate. The loader resolves
`prompts/<prompt_language>/<name>.md` first, then falls back to the shared directory
and then to `prompts/en/`, so you can port incrementally.

Placeholders use `$name`, not `{name}` — several prompts contain literal JSON braces.
Available in every prompt: `$parliament_name`, `$fts_config`, `$answer_language`,
`$preserve_characters`, `$party_codes`, `$date_today`, the `$..._id_example` values,
and every key from `vocabulary`.

`$preserve_characters` matters more than it looks. It tells the model not to
transliterate characters your language depends on; searching for `karnkraft` instead
of `kärnkraft` returns nothing at all.

Set `PROMPTS_RELOAD=1` while you iterate so you are not restarting the server between
edits.

## 4. UI language

The frontend has no i18n layer yet — Swedish strings are written directly into about
twenty components. Porting the UI currently means translating those in place. Site
copy is easier: `content/<lang>/explainer.md` and `limit_warning.md` are served
through `/api/meta` and need no code change.

## What you do not need to change

The search engine, chat orchestration, tool-calling, citation and attribution
checking, the deep-research agent, authentication and the encrypted chat store are
all country-neutral. So is the database schema — although its column names are still
Swedish (`anforandetext`, `intressent_id`, `yrkanden`). They are just names; nothing
reads meaning from them. [docs/SCHEMA.md](SCHEMA.md) translates them.

## A caution about scope

plenum assumes a parliament that publishes, as open data, both a transcript of what
was said in the chamber and the documents members submit, with stable identifiers
linking them to a register of members. Where that holds — Sweden, the UK, the
European Parliament, most Nordic and Baltic states — porting is mostly configuration.
Where it does not, the data model itself may not fit, and you should check that
before investing in an adapter.
