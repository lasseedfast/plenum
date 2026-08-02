# SearchService using PostgreSQL full-text search (tsvector/tsquery) with Swedish dictionary.
#
# Replaces the ArangoSearch-based implementation.
# - BM25 → ts_rank_cd() (similar ranking, different formula)
# - ArangoSearch view → GIN index on search_vector (tsvector column)
# - OFFSET_INFO highlighting → ts_headline()
# - PHRASE() → phraseto_tsquery()
# - TOKENS() → plainto_tsquery()
# - STARTS_WITH() prefix → to_tsquery with :* suffix
#
# psycopg2 uses %s for parameters (not $N). The tsquery expression is placed
# in a CTE so it's computed once and reused for WHERE, ORDER BY, and ts_headline.

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from postgres_client import pg
from parliament import PARLIAMENT

# Postgres text-search configuration. Validated against ^[a-z_][a-z0-9_]*$ when
# parliament.yaml loads, so interpolating it into SQL is safe — a config name is
# an identifier and cannot be passed as a bind parameter.
_FTS = PARLIAMENT.language.fts_config


@dataclass
class ParsedQuery:
    must_terms: list[str] = field(default_factory=list)
    should_groups: list[list[str]] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    years: tuple[int, int] | None = None


class SearchService:
    """
    Full-text and filtered search over the talks table using PostgreSQL.

    Query syntax (same as before):
      - Single word:   "klimat"
      - Exact phrase:  "klimat förändring"   (quoted in query string)
      - OR:            klimat OR miljö
      - NOT:           klimat -riksdag
      - Prefix:        klima*
      - Year range:    år:2020-2023
    """

    def parse_query(self, query: str) -> ParsedQuery:
        """Parse a raw query string into must/should/exclude buckets and optional year span."""
        parsed = ParsedQuery()
        if not query:
            return parsed
        parts = re.findall(r'"[^"]+"|\S+', query.replace("'", '"'))
        tokens = [token.strip('"') for token in parts]
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if not token:
                idx += 1
                continue
            if token.lower().startswith("år:") and len(token) >= 8:
                try:
                    start, end = token[3:].split("-", 1)
                    parsed.years = (int(start), int(end))
                except ValueError:
                    pass
                idx += 1
                continue
            is_negative = token.startswith("-")
            clean = token[1:] if is_negative else token
            group: list[str] = [clean]
            j = idx + 1
            while j + 1 < len(tokens) and tokens[j].upper() == "OR":
                group.append(tokens[j + 1])
                j += 2
            if len(group) > 1:
                if is_negative:
                    parsed.exclude_terms.extend(group)
                else:
                    parsed.should_groups.append(group)
                idx = j
                continue
            if is_negative:
                parsed.exclude_terms.append(clean)
            else:
                parsed.must_terms.append(clean)
            idx += 1
        return parsed

    # ──────────────────────────────────────────────────────────────────────────
    # tsquery building
    # ──────────────────────────────────────────────────────────────────────────

    def _build_tsquery(
        self, parsed: ParsedQuery
    ) -> tuple[str, list, list[str]]:
        """
        Build a tsquery SQL expression with %s placeholders for psycopg2.

        Returns:
            tsq_sql   - SQL fragment, e.g. "plainto_tsquery(<fts_config>, %s) && ..."
            tsq_params - list of values matching each %s in tsq_sql
            snippet_terms - raw terms used for snippet highlighting (informational)

        The expression is designed to be placed in a CTE so it is evaluated once
        and referenced multiple times (WHERE, ORDER BY, ts_headline).
        """
        parts: list[str] = []
        tsq_params: list = []
        snippet_terms: list[str] = []
        seen: set[str] = set()

        def _add_term(term: str) -> str | None:
            is_prefix = term.endswith(("*", "%"))
            clean = term.rstrip("*% ").strip()
            if not clean:
                return None
            is_phrase = " " in clean
            if clean not in seen:
                snippet_terms.append(clean)
                seen.add(clean)
            tsq_params.append(clean)
            if is_prefix:
                return f"to_tsquery('{_FTS}', %s || ':*')"
            elif is_phrase:
                return f"phraseto_tsquery('{_FTS}', %s)"
            else:
                return f"plainto_tsquery('{_FTS}', %s)"

        # MUST: all must match
        must_parts = [_add_term(t) for t in parsed.must_terms]
        must_parts = [p for p in must_parts if p]
        if must_parts:
            parts.append("(" + " && ".join(must_parts) + ")")

        # SHOULD groups: each group is OR-combined; groups are ANDed
        for group in parsed.should_groups:
            group_parts = [_add_term(t) for t in group]
            group_parts = [p for p in group_parts if p]
            if group_parts:
                parts.append("(" + " || ".join(group_parts) + ")")

        # EXCLUDE: must NOT match
        for term in parsed.exclude_terms:
            frag = _add_term(term)
            if frag:
                parts.append(f"!!({frag})")

        if not parts:
            return "", [], []

        tsq_sql = " && ".join(parts)
        return tsq_sql, tsq_params, snippet_terms

    # ──────────────────────────────────────────────────────────────────────────
    # Main search
    # ──────────────────────────────────────────────────────────────────────────

    def search(
        self,
        payload,
        include_snippets: bool = True,
        return_snippets: bool = False,
        focus_ids: Sequence[str] | None = None,
        return_fields: Iterable[str] = (),
    ):
        """
        Run a full-text + filter search against PostgreSQL.

        Returns (results, stats, limit_reached).
        If return_snippets is True, returns (snippets, stats, limit_reached).
        """
        parsed = self.parse_query(payload.q)

        tsq_sql, tsq_params, snippet_terms = self._build_tsquery(parsed)

        # ── Filter WHERE clauses ───────────────────────────────────────────────
        filter_clauses: list[str] = []
        filter_params: list = []

        focus_ids = list(focus_ids or []) or getattr(payload, "focus_ids", None) or []

        if payload.parties:
            filter_params.append(list(payload.parties))
            filter_clauses.append("t.parti = ANY(%s::text[])")

        if getattr(payload, "speaker_ids", None):
            ids = payload.speaker_ids
            if isinstance(ids, str):
                ids = [ids]
            filter_params.append(list(ids))
            filter_clauses.append("t.intressent_id = ANY(%s::text[])")
        elif getattr(payload, "speaker", None):
            filter_params.append(payload.speaker)
            filter_clauses.append("t.talare = %s")

        if getattr(payload, "people", None):
            sub = []
            for name in payload.people:
                filter_params.append(f"%{name.lower()}%")
                sub.append("LOWER(t.talare) LIKE %s")
            filter_clauses.append("(" + " OR ".join(sub) + ")")

        if getattr(payload, "debates", None):
            filter_params.append(list(payload.debates))
            filter_clauses.append("t.kammaraktivitet = ANY(%s::text[])")

        year_start = parsed.years[0] if parsed.years else getattr(payload, "from_year", None)
        year_end   = parsed.years[1] if parsed.years else getattr(payload, "to_year", None)
        if year_start is not None:
            filter_params.append(year_start)
            filter_clauses.append("t.year >= %s")
        if year_end is not None:
            filter_params.append(year_end)
            filter_clauses.append("t.year <= %s")

        if focus_ids:
            clean_focus = [fid.removeprefix("talks/") for fid in focus_ids]
            filter_params.append(clean_focus)
            filter_clauses.append("t.id = ANY(%s::text[])")

        # ── Build SQL ─────────────────────────────────────────────────────────
        # Put the tsquery in a CTE so it's evaluated once and reused.
        if tsq_sql:
            cte = f"WITH q AS (SELECT {tsq_sql} AS tsq)"
            cte_params = tsq_params
            fts_where = "t.search_vector @@ q.tsq"
            order_by = "ts_rank_cd(t.search_vector, q.tsq) DESC, t.datum ASC, t.anforande_nummer ASC"
            if include_snippets:
                headline_col = (
                    f", ts_headline('{_FTS}', t.anforandetext, q.tsq, "
                    "'MaxWords=15, MinWords=8, MaxFragments=1') AS _headline"
                    f", ts_headline('{_FTS}', t.anforandetext, q.tsq, "
                    "'MaxWords=60, MinWords=30, MaxFragments=4') AS _headline_long"
                )
            else:
                headline_col = ""
            from_clause = "FROM talks t, q"
            all_where = ([fts_where] + filter_clauses) if filter_clauses else [fts_where]
        else:
            cte = ""
            cte_params = []
            headline_col = ""
            order_by = "t.datum ASC, t.anforande_nummer ASC"
            from_clause = "FROM talks t"
            all_where = filter_clauses

        where_sql = ("WHERE " + " AND ".join(all_where)) if all_where else ""

        limit = getattr(payload, "limit", None)
        limit_reached = False
        if limit:
            filter_params.append(limit + 1)
            limit_sql = "LIMIT %s"
        else:
            limit_sql = ""

        sql = f"""
        {cte}
        SELECT
            t.id,
            t.anforandetext,
            t.anforande_nummer,
            t.kammaraktivitet,
            t.talare,
            t.datum::text AS datum,
            t.year,
            COALESCE(t.debateurl, t.url_session) AS debateurl,
            t.parti,
            t.intressent_id,
            t.titel,
            t.rel_dok_id
            {headline_col}
        {from_clause}
        {where_sql}
        ORDER BY {order_by}
        {limit_sql}
        """

        all_params = tuple(cte_params + filter_params)
        rows = pg.execute(sql, all_params if all_params else None)
        print(f"{len(rows)} rows returned from PostgreSQL")

        if limit:
            limit_reached = len(rows) > limit
            if limit_reached:
                rows = rows[:limit]

        # ── Build result objects ───────────────────────────────────────────────
        results = []
        for doc in rows:
            text = doc.get("anforandetext") or ""
            headline = doc.get("_headline") or ""
            headline_long = doc.get("_headline_long") or ""

            # ts_headline uses <b>…</b> – convert to **bold** for frontend
            if headline:
                snippet = re.sub(r"<b>(.*?)</b>", r"**\1**", headline)
                snippet_long = re.sub(r"<b>(.*?)</b>", r"**\1**", headline_long or headline)
            else:
                snippet = text[:200]
                snippet_long = text[:800]

            kammaraktivitet = doc.get("kammaraktivitet")
            debate_info = PARLIAMENT.activity_types.get(kammaraktivitet, {})
            debate_type_title = (
                debate_info.get("title", kammaraktivitet)
                if isinstance(debate_info, dict)
                else debate_info
            )

            talk_id = doc.get("id") or ""
            results.append(
                {
                    "_id": f"talks/{talk_id}",
                    "text": text,
                    "snippet": snippet,
                    "snippet_long": snippet_long,
                    "number": doc.get("anforande_nummer"),
                    "debate_type": debate_type_title,
                    "kammaraktivitet": kammaraktivitet,
                    "speaker": doc.get("talare"),
                    "date": str(doc.get("datum") or ""),
                    "year": doc.get("year"),
                    "url_session": doc.get("debateurl"),
                    "party": doc.get("parti"),
                    "intressent_id": doc.get("intressent_id"),
                    "titel": doc.get("titel"),
                    "rel_dok_id": doc.get("rel_dok_id"),
                    "bm25": None,
                }
            )

        per_party = Counter(hit["party"] for hit in results if hit["party"])
        per_year = Counter(hit["year"] for hit in results if hit["year"])
        stats = {
            "per_party": dict(per_party),
            "per_year": {int(k): v for k, v in per_year.items()},
            "total": len(results),
        }

        if return_snippets:
            return (
                [
                    {
                        "_id": r["_id"],
                        "snippet_long": r["snippet_long"],
                        "speaker": r["speaker"],
                        "date": r["date"],
                        "party": r["party"],
                        "debate_type": r["debate_type"],
                    }
                    for r in results
                ],
                stats,
                limit_reached,
            )

        print(f"Search returning {len(results)} results, limit reached: {limit_reached}")
        return results, stats, limit_reached


class MotionSearchService(SearchService):
    """
    Full-text and filtered search over the motions table.

    Reuses parse_query()/_build_tsquery() from SearchService; only the SQL and
    result shaping differ (motions have multiple authors and no debate fields).
    """

    def search(
        self,
        payload,
        include_snippets: bool = True,
        return_snippets: bool = False,
        focus_ids: Sequence[str] | None = None,
        return_fields: Iterable[str] = (),
    ):
        parsed = self.parse_query(payload.q)
        tsq_sql, tsq_params, _ = self._build_tsquery(parsed)

        # ── Filter WHERE clauses ───────────────────────────────────────────────
        filter_clauses: list[str] = []
        filter_params: list = []

        focus_ids = list(focus_ids or []) or getattr(payload, "focus_ids", None) or []

        if payload.parties:
            filter_params.append(list(payload.parties))
            filter_clauses.append("m.parties && %s::text[]")

        if getattr(payload, "speaker_ids", None):
            ids = payload.speaker_ids
            if isinstance(ids, str):
                ids = [ids]
            filter_params.append(list(ids))
            filter_clauses.append(
                "EXISTS (SELECT 1 FROM motion_authors a"
                " WHERE a.dok_id = m.dok_id AND a.intressent_id = ANY(%s::text[]))"
            )

        if getattr(payload, "people", None):
            sub = []
            for name in payload.people:
                filter_params.append(f"%{name.lower()}%")
                sub.append(
                    "EXISTS (SELECT 1 FROM motion_authors a"
                    " WHERE a.dok_id = m.dok_id AND LOWER(a.namn) LIKE %s)"
                )
            filter_clauses.append("(" + " OR ".join(sub) + ")")

        year_start = parsed.years[0] if parsed.years else getattr(payload, "from_year", None)
        year_end   = parsed.years[1] if parsed.years else getattr(payload, "to_year", None)
        if year_start is not None:
            filter_params.append(year_start)
            filter_clauses.append("m.year >= %s")
        if year_end is not None:
            filter_params.append(year_end)
            filter_clauses.append("m.year <= %s")

        if focus_ids:
            clean_focus = [fid.removeprefix("motions/") for fid in focus_ids]
            filter_params.append(clean_focus)
            filter_clauses.append("m.dok_id = ANY(%s::text[])")

        # ── Build SQL ─────────────────────────────────────────────────────────
        if tsq_sql:
            cte = f"WITH q AS (SELECT {tsq_sql} AS tsq)"
            cte_params = tsq_params
            fts_where = "m.search_vector @@ q.tsq"
            order_by = "ts_rank_cd(m.search_vector, q.tsq) DESC, m.datum DESC"
            if include_snippets:
                headline_col = (
                    f", ts_headline('{_FTS}', m.text, q.tsq, "
                    "'MaxWords=15, MinWords=8, MaxFragments=1') AS _headline"
                    f", ts_headline('{_FTS}', m.text, q.tsq, "
                    "'MaxWords=60, MinWords=30, MaxFragments=4') AS _headline_long"
                )
            else:
                headline_col = ""
            from_clause = "FROM motions m, q"
            all_where = ([fts_where] + filter_clauses) if filter_clauses else [fts_where]
        else:
            cte = ""
            cte_params = []
            headline_col = ""
            order_by = "m.datum DESC"
            from_clause = "FROM motions m"
            all_where = filter_clauses

        where_sql = ("WHERE " + " AND ".join(all_where)) if all_where else ""

        limit = getattr(payload, "limit", None)
        limit_reached = False
        if limit:
            filter_params.append(limit + 1)
            limit_sql = "LIMIT %s"
        else:
            limit_sql = ""

        sql = f"""
        {cte}
        SELECT
            m.dok_id,
            m.text,
            m.titel,
            m.undertitel,
            m.rm,
            m.beteckning,
            m.subtyp,
            m.organ,
            m.status,
            m.datum::text AS datum,
            m.year,
            m.parties,
            m.author_names,
            m.num_yrkanden,
            m.has_text,
            m.dokument_url_html
            {headline_col}
        {from_clause}
        {where_sql}
        ORDER BY {order_by}
        {limit_sql}
        """

        all_params = tuple(cte_params + filter_params)
        rows = pg.execute(sql, all_params if all_params else None)

        if limit:
            limit_reached = len(rows) > limit
            if limit_reached:
                rows = rows[:limit]

        # ── Build result objects ───────────────────────────────────────────────
        results = []
        for doc in rows:
            text = doc.get("text") or ""
            headline = doc.get("_headline") or ""
            headline_long = doc.get("_headline_long") or ""

            if headline:
                snippet = re.sub(r"<b>(.*?)</b>", r"**\1**", headline)
                snippet_long = re.sub(r"<b>(.*?)</b>", r"**\1**", headline_long or headline)
            else:
                snippet = text[:200]
                snippet_long = text[:800]

            author_names = doc.get("author_names") or []
            speaker = ", ".join(author_names[:3])
            if len(author_names) > 3:
                speaker += " m.fl."

            dok_id = doc.get("dok_id") or ""
            results.append(
                {
                    "_id": f"motions/{dok_id}",
                    "text": text,
                    "snippet": snippet,
                    "snippet_long": snippet_long,
                    "debate_type": "Motion",
                    "speaker": speaker,
                    "author_names": author_names,
                    "date": str(doc.get("datum") or ""),
                    "year": doc.get("year"),
                    "party": "/".join(doc.get("parties") or []),
                    "titel": doc.get("titel"),
                    "undertitel": doc.get("undertitel"),
                    "rm": doc.get("rm"),
                    "beteckning": doc.get("beteckning"),
                    "subtyp": doc.get("subtyp"),
                    "organ": doc.get("organ"),
                    "status": doc.get("status"),
                    "num_yrkanden": doc.get("num_yrkanden"),
                    "has_text": doc.get("has_text"),
                    "url_session": doc.get("dokument_url_html"),
                    "bm25": None,
                }
            )

        per_party = Counter(p for hit in results for p in (hit["party"].split("/") if hit["party"] else []))
        per_year = Counter(hit["year"] for hit in results if hit["year"])
        stats = {
            "per_party": dict(per_party),
            "per_year": {int(k): v for k, v in per_year.items()},
            "total": len(results),
        }

        if return_snippets:
            return (
                [
                    {
                        "_id": r["_id"],
                        "snippet_long": r["snippet_long"],
                        "speaker": r["speaker"],
                        "date": r["date"],
                        "party": r["party"],
                        "debate_type": r["debate_type"],
                    }
                    for r in results
                ],
                stats,
                limit_reached,
            )

        return results, stats, limit_reached


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from dataclasses import dataclass

    @dataclass
    class Payload:
        q: str = "klimat"
        parties: list[str] | None = None
        people: list[str] | None = None
        debates: list[str] | None = None
        from_year: int | None = 2018
        to_year: int | None = 2023
        speaker: str | None = None
        limit: int = 10
        speaker_ids: list[str] | None = None

    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "motions":
        svc = MotionSearchService()
        results, stats, limited = svc.search(Payload(q="kärnkraft", from_year=2022, to_year=None))
    else:
        svc = SearchService()
        results, stats, limited = svc.search(Payload())
    for r in results:
        print(r.get("speaker"), r.get("date"), r.get("snippet", "")[:80])
    print("Stats:", stats, "| limited:", limited)
