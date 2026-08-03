"""Evaluation harness for ChatService.

Generates novel Swedish questions at runtime, runs them through ChatService,
and uses a judge LLM to verdict each paragraph's citations against its sources.

Usage:
    python scripts/eval_harness.py --label "gpt-oss baseline" --iterations 500
    python scripts/eval_harness.py --label smoke --iterations 3
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import traceback

import requests
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv()

import types as _types
# Stub the backend package before importing submodules so that
# backend/__init__.py (which imports the full FastAPI app) never runs.
# __path__ must be set so Python treats the stubs as packages.
def _stub_pkg(name: str, path: str) -> None:
    if name not in sys.modules:
        m = _types.ModuleType(name)
        m.__path__ = [path]
        m.__package__ = name
        sys.modules[name] = m

_stub_pkg("backend", str(_ROOT / "backend"))
_stub_pkg("backend.services", str(_ROOT / "backend/services"))

from postgres_client import pg
from packages.llm import LLM
from backend.services.chat import ChatService, SMART_MODEL, FAST_MODEL


GENERATOR_SYSTEM = """Du genererar realistiska frågor som en svensk journalist eller medborgare kan ställa till ett chatgränssnitt över riksdagens anföranden (~450 000 anföranden från 1990 till idag, med speaker_name, party, date, debatt och fulltext).

Variera teman brett: sakpolitik (skola, vård, försvar, klimat, migration, skatt, EU, kultur, arbetsmarknad), personfrågor, historiska skeenden, specifika händelser, citat.

Du får ett komplexitetsmål (1–3) som styr hur många delfrågor frågan ska innehålla:
- Nivå 1: En enkel, direkt fråga. T.ex. "Vad säger Miljöpartiet om kärnkraft?"
- Nivå 2: Två relaterade vinklar i samma fråga. T.ex. "Vad säger partierna om kärnkraft, och vilka politiker driver frågan mest aktivt?"
- Nivå 3: Tre vinklar eller en bredare analytisk fråga. T.ex. "Vad säger partierna om kärnkraft, vilka politiker är mest aktiva, och hur har debatten förändrats sedan 2010?"

Returnera ENDAST frågan, på svenska, utan förklaring eller inledning. Ingen markdown, ingen numrering."""

JUDGE_SYSTEM = """Du är en noggrann faktakontrollant för ett system som söker i svenska riksdagsanföranden.

Du får:
1. Det fullständiga svaret som AI-assistenten gav.
2. Ett specifikt stycke ur svaret som du ska bedöma.
3. De fullständiga texterna till de tal som stycket citerar.

Din uppgift: avgör om påståendena i stycket stöds av de citerade talens faktiska innehåll.

Var särskilt uppmärksam på om rätt speaker_name och party tillskrivs rätt tal. Ett känt fel är t.ex. att svaret skriver "Jan Björklund (M)" men det citerade talet hölls av Helena Bargholtz (L).

Returnera ENDAST ett JSON-objekt:
{"verdict": "...", "cited_indices": [N, ...], "rationale": "kort motivering på svenska"}

Du MÅSTE alltid skriva en rationale på minst en mening som förklarar ditt beslut, oavsett verdict.

Verdict (välj EXAKT ett):
- "supported": påståendet stöds av taltexterna.
- "partial": delvis korrekt men något är överdrivet eller ej verifierbart mot taltexterna.
- "unsupported": påståendet motsägs eller saknar stöd i taltexterna — använd detta även när
  rätt speaker_name citeras men innehållet som tillskrivs dem inte finns i det angivna talet.
- "wrong_speaker": ENBART om namnet eller partiförkortningen i stycket INTE stämmer med vem
  som faktiskt höll det citerade talet enligt taltextens metadata och innehåll. Ange rätt
  speaker_name/party i rationale. Använd INTE detta verdict enbart för att innehållet är felaktigt
  — det hör till "unsupported".
- "wrong_attribution": rätt speaker_name är angiven, men det specifika påståendet är hämtat från
  ett annat tal eller ett annat källindex än det som faktiskt citeras — t.ex. att innehållet
  finns i källa [7] men stycket citerar [3] av samma speaker_name."""


# ---------------------------------------------------------------------------
# Question generator
# ---------------------------------------------------------------------------

def _fetch_random_talk_snippet() -> Optional[Dict[str, Any]]:
    """Pick a random talk and return its first ~500 chars plus metadata."""
    rows = pg.execute(
        """SELECT id, speaker_name, party, date::text AS date,
                  LEFT(text, 500) AS snippet
           FROM speeches
           WHERE text IS NOT NULL AND LENGTH(text) > 300
           ORDER BY RANDOM()
           LIMIT 1"""
    )
    return rows[0] if rows else None


class QuestionGenerator:
    """Mixes three strategies so the harness exercises different input shapes.

    - talk_seed: sample a random talk snippet and ask the LLM to turn it into a
      question (grounds the run in real database content).
    - free: open-ended question from the system prompt's theme list.
    """

    STRATEGIES = ["talk_seed", "talk_seed", "free"]  # weighted toward talk_seed

    def __init__(self, llm: LLM, history_size: int = 20) -> None:
        self.llm = llm
        self.recent: List[str] = []
        self.history_size = history_size

    def _prompt_free(self, complexity: int) -> str:
        avoid = "\n".join(f"- {q}" for q in self.recent[-self.history_size:]) or "(inga tidigare)"
        return (
            f"Komplexitetsmål: {complexity}\n\n"
            f"Undvik att upprepa något av följande frågor/teman:\n{avoid}\n\n"
            "Skriv en ny fråga."
        )

    def _prompt_talk_seed(self, talk: Dict[str, Any], complexity: int) -> str:
        avoid = "\n".join(f"- {q}" for q in self.recent[-self.history_size:]) or "(inga tidigare)"
        return (
            f"Komplexitetsmål: {complexity}\n\n"
            "Här är ett utdrag ur ett riksdagsanförande:\n"
            f"Talare: {talk.get('speaker_name')} ({talk.get('party')}) — {talk.get('date')}\n"
            f"Utdrag:\n\"\"\"\n{talk.get('snippet')}\n\"\"\"\n\n"
            "Formulera en naturlig fråga på svenska som en journalist eller medborgare "
            "kunde ställa, och som skulle leda till att man hittar detta eller liknande "
            "anföranden. Fråga gärna bredare än utdraget — t.ex. vad olika partier eller "
            "politiker sagt i samma sakfråga, hur diskussionen utvecklats, eller kombinera "
            "vinklar i enlighet med komplexitetsmålet. Nämn INTE talaren eller datumet direkt.\n\n"
            f"Undvik att upprepa något av följande teman:\n{avoid}"
        )

    def generate(self) -> tuple[str, int]:
        """Returns (question, complexity) where complexity is 1–3."""
        complexity = random.randint(1, 3)
        strategy = random.choice(self.STRATEGIES)
        prompt: Optional[str] = None
        if strategy == "talk_seed":
            talk = _fetch_random_talk_snippet()
            if talk:
                prompt = self._prompt_talk_seed(talk, complexity)
        if prompt is None:
            strategy = "free"
            prompt = self._prompt_free(complexity)

        resp = self.llm.generate(
            messages=[
                {"role": "system", "content": GENERATOR_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            think=False,
        )
        text = getattr(resp, "content", str(resp)).strip()
        text = text.strip('"').strip("'").strip()
        if "\n" in text:
            text = text.split("\n", 1)[0].strip()
        self.recent.append(text)
        return text, complexity


# ---------------------------------------------------------------------------
# Trace collector — captures compact event stream
# ---------------------------------------------------------------------------

class TraceCollector:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self._iter = 0

    def callback(self, event: Dict[str, Any]) -> None:
        etype = event.get("type")
        compact: Dict[str, Any] = {"iter": self._iter, "type": etype}
        if etype == "tool_call":
            self._iter += 1
            compact["iter"] = self._iter
            compact["tool"] = event.get("tool")
        elif etype == "status":
            compact["message"] = (event.get("message") or "")[:200]
        elif etype == "search_card":
            compact["hit_count"] = event.get("total", 0)
            compact["hit_ids"] = [
                r.get("_id") for r in (event.get("results") or []) if isinstance(r, dict)
            ]
            compact["speaker_ids"] = event.get("speaker_ids") or []
        elif etype == "stats_card":
            compact["row_count"] = len(event.get("rows") or [])
            compact["speaker_ids"] = event.get("speaker_ids") or []
        elif etype == "insight":
            compact["message"] = (event.get("message") or "")[:200]
        elif etype == "tool_speakers":
            compact["person_ids"] = event.get("person_ids") or []
        else:
            return
        self.events.append(compact)


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

_CITATION_RE = re.compile(r"\[(\d+)\]")
_SPEAKER_RE = re.compile(r"\[([^\]]+)\]\(/mp/[^)]+\)\s*\(([A-ZÅÄÖ]{1,3})\)")


def _check_speaker_metadata(
    paragraph: str, cited_sources: List[Dict[str, Any]]
) -> Optional[str]:
    """Deterministic check: returns a mismatch description if any speaker/party in the
    paragraph doesn't match the cited source metadata, else None.

    Only fires when a [Name](/mp/...) (PARTY) pattern is present so we don't false-positive
    on paragraphs that don't name a speaker explicitly.
    """
    para_speakers = [
        (m.group(1).strip(), m.group(2).strip()) for m in _SPEAKER_RE.finditer(paragraph)
    ]
    if not para_speakers:
        return None
    for para_name, para_party in para_speakers:
        for src in cited_sources:
            src_spk = (src.get("speaker") or "").strip()
            src_party = (src.get("party") or "").strip()
            if not src_spk:
                continue
            name_ok = (
                src_spk.lower() in para_name.lower()
                or para_name.lower() in src_spk.lower()
            )
            if name_ok and src_party and src_party != para_party:
                return (
                    f"Partifel: stycket anger ({para_party}) men källa [{src['n']}] "
                    f"är ({src_party}) — speaker_name: {src_spk}"
                )
            if not name_ok:
                return (
                    f"Namnfel: stycket anger '{para_name}' ({para_party}) men "
                    f"källa [{src['n']}] hölls av '{src_spk}' ({src_party})"
                )
    return None


def _split_paragraphs(answer_md: str) -> List[str]:
    """Split answer into blocks, returning only those that contain [N] citations.

    Splits on double newlines (paragraph breaks). Bullet lists and numbered lists
    that share a double-newline boundary are kept together as one block. Only
    blocks containing at least one [N] reference are returned — headers, intros,
    and transition sentences are silently dropped since the judge has nothing to
    verify without a citation.
    """
    if not answer_md:
        return []
    for marker in ("\n### Källor", "\n## Källor", "\n### Sources", "\n**Källor**"):
        if marker in answer_md:
            answer_md = answer_md.split(marker, 1)[0]
            break
    speech_chunks = [c.strip() for c in answer_md.split("\n\n") if c.strip()]
    return [c for c in speech_chunks if _CITATION_RE.search(c)]


def _parse_judge_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract the first valid JSON object from a judge response.

    Rejects objects with an empty rationale — the judge is required to explain every verdict.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text, i)
                if isinstance(obj, dict) and "verdict" in obj:
                    if not (obj.get("rationale") or "").strip():
                        return None
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def _fetch_full_talks(talk_ids: List[str]) -> Dict[str, str]:
    """Return {speech_id -> talk metadata + full text} for the given ids."""
    if not talk_ids:
        return {}
    bare_ids = [tid.split("/", 1)[-1] for tid in talk_ids]
    rows = pg.execute(
        "SELECT id, speaker_name, party, date::text AS date, text FROM speeches WHERE id = ANY(%s::text[])",
        (bare_ids,),
    )
    return {
        row["id"]: {
            "speaker_name": row["speaker_name"],
            "party": row["party"],
            "date": row["date"],
            "text": row["text"] or "",
        }
        for row in rows
    }


# Combined char budget for all sources fed to the scorer.
# At ~4 chars/token, 28 000 chars ≈ 7 000 tokens, safely below the 8 192-token model limit
# even with a paragraph prepended.
_SCORER_COMBINED_MAX_CHARS = 28_000


_SCORER_ENDPOINT = os.environ.get("SCORER_ENDPOINT", "http://localhost:8005/v1/score")
_SCORER_MODEL = "BAAI/bge-reranker-v2-m3"


class CitationScorer:
    """Calls the vLLM /v1/score endpoint to compute how well each cited source
    supports the paragraph claim.

    Scores each (paragraph, source) pair independently and returns the maximum
    score across all cited sources — answering "is this claim grounded in *any*
    of its citations?".  The raw logit is converted to a 0–1 probability via
    sigmoid so scores are human-readable.
    """

    def __init__(self, endpoint: str = _SCORER_ENDPOINT, timeout: int = 10) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._available: Optional[bool] = None

    def _check_available(self) -> bool:
        if self._available is None:
            try:
                r = requests.get(
                    self.endpoint.replace("/v1/score", "/v1/models"), timeout=3
                )
                self._available = r.status_code == 200
            except Exception:
                self._available = False
            if not self._available:
                print(f"[scorer] endpoint {self.endpoint} not reachable — coverage_score will be NULL")
        return self._available

    def score_all(self, paragraph: str, combined_sources: str) -> Optional[float]:
        """Score the paragraph against all cited sources concatenated into one string.

        The combined_sources string is trimmed to _SCORER_COMBINED_MAX_CHARS before
        sending so the (sources + paragraph) pair fits within the model's token limit.
        Returns a 0–1 support probability (sigmoid of the raw logit).
        """
        if not self._check_available():
            return None
        try:
            payload = {
                "model": _SCORER_MODEL,
                "text_1": combined_sources[:_SCORER_COMBINED_MAX_CHARS],
                "text_2": paragraph,
            }
            r = requests.post(self.endpoint, json=payload, timeout=self.timeout)
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    logit = data[0].get("score", -10.0)
                    return 1.0 / (1.0 + math.exp(-logit))
            else:
                print(f"[scorer] HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[scorer] request failed: {e}")
        return None


class Judge:
    def __init__(self, llm: LLM, model_name: str, scorer: Optional["CitationScorer"] = None) -> None:
        self.llm = llm
        self.model_name = model_name
        self.scorer = scorer

    def _judge_paragraph(
        self,
        answer_md: str,
        paragraph: str,
        para_idx: int,
        cited_sources: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """One LLM call for one paragraph. Returns the verdict dict or None on failure.

        Runs a deterministic speaker-metadata check first; if it fires the result is stored
        in `metadata_mismatch` and passed to the judge as context so it can confirm or
        override. The LLM judge still runs regardless so we capture its independent view.
        """
        # --- Deterministic pre-check ---
        metadata_mismatch = _check_speaker_metadata(paragraph, cited_sources)

        # Fetch full talk texts for the sources cited in this paragraph.
        talk_ids = [s.get("speech_id") or s.get("_id") for s in cited_sources if s.get("speech_id") or s.get("_id")]
        full_texts = _fetch_full_talks(talk_ids)

        source_block = ""
        combined_for_scorer_parts: List[str] = []
        for s in cited_sources:
            n = s.get("n", "?")
            tid = (s.get("speech_id") or s.get("_id") or "").split("/")[-1]
            meta = full_texts.get(tid)
            if meta:
                source_block += (
                    f"\n---\nKälla [{n}]: {meta['speaker_name']} ({meta['party']}) — {meta['date']}\n"
                    f"{meta['text']}\n"
                )
                # Scorer gets the full text (truncation handled by combined budget below).
                combined_for_scorer_parts.append(
                    f"[{n}] {meta['speaker_name']} ({meta['party']}) — {meta['date']}\n{meta['text']}"
                )
            else:
                # Fall back to snippet if full text not found
                snippet = s.get("snippet", "")
                source_block += (
                    f"\n---\nKälla [{n}]: {s.get('speaker')} ({s.get('party')}) — {s.get('date')}\n"
                    f"{snippet}\n"
                )
                if snippet:
                    combined_for_scorer_parts.append(
                        f"[{n}] {s.get('speaker')} ({s.get('party')}) — {s.get('date')}\n{snippet}"
                    )

        # --- Coverage score (cross-encoder) ---
        # All cited sources are concatenated into one string so the model scores
        # the paragraph against the full body of cited evidence.  The combined
        # text is capped at _SCORER_COMBINED_MAX_CHARS to stay within the 8 192-
        # token model limit (paragraph text is passed as text_2, so it doesn't
        # count toward this budget).
        coverage_score: Optional[float] = None
        if self.scorer and combined_for_scorer_parts:
            combined_sources = "\n\n".join(combined_for_scorer_parts)
            coverage_score = self.scorer.score_all(paragraph, combined_sources)

        mismatch_note = (
            f"\nOBS: En automatisk förhandskontroll flaggade följande avvikelse i metadata: "
            f"{metadata_mismatch}\n"
            if metadata_mismatch
            else ""
        )
        prompt = (
            f"Det fullständiga svaret från AI-assistenten:\n\n{answer_md}\n\n"
            f"---\n\nVi fokuserar nu ENBART på detta stycke:\n\n{paragraph}\n\n"
            f"---\n\nStycket refererar till följande tal (fulltext):{source_block}\n"
            f"---\n{mismatch_note}\n"
            "Bedöm om påståendena i stycket stöds av taltexterna ovan.\n"
            "Returnera ENDAST ett JSON-objekt med verdict, cited_indices och rationale."
        )

        resp = self.llm.generate(
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            think=False,
        )
        text = getattr(resp, "content", str(resp))
        item = _parse_judge_json(text)
        if not item:
            return None
        return {
            "paragraph_idx": para_idx,
            "paragraph_text": paragraph,
            "cited_indices": item.get("cited_indices") or [s["n"] for s in cited_sources],
            "verdict": item.get("verdict") or "unsupported",
            "rationale": (item.get("rationale") or "")[:1000],
            "metadata_mismatch": metadata_mismatch,
            "coverage_score": coverage_score,
        }

    def verdict(self, answer_md: str, sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Judge each cited paragraph individually. One LLM call per paragraph."""
        paragraphs = _split_paragraphs(answer_md)
        if not paragraphs:
            return []

        # Build a lookup: citation number → source dict
        source_by_n: Dict[int, Dict[str, Any]] = {s["n"]: s for s in sources if "n" in s}

        out = []
        for para_idx, paragraph in enumerate(paragraphs):
            # Find which [N] indices this paragraph references
            cited_ns = [int(m) for m in _CITATION_RE.findall(paragraph)]
            cited_sources = [source_by_n[n] for n in cited_ns if n in source_by_n]
            if not cited_sources:
                continue  # no resolvable sources — skip

            try:
                result = self._judge_paragraph(answer_md, paragraph, para_idx, cited_sources)
                if result:
                    out.append(result)
            except Exception as e:
                print(f"[judge] paragraph {para_idx} failed: {e}")

        return out


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=_ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def ensure_migration() -> None:
    sql = (_ROOT / "_postgres/migrations/add_eval_tables.sql").read_text()
    pg.execute_void(sql)


def create_run(label: str, config: Dict[str, Any]) -> str:
    row = pg.execute(
        "INSERT INTO eval_runs (label, config) VALUES (%s, %s) RETURNING id",
        (label, json.dumps(config, default=str)),
    )
    return str(row[0]["id"])


def finalize_run(run_id: str, num_questions: int) -> None:
    pg.execute_void(
        "UPDATE eval_runs SET finished_at = NOW(), num_questions = %s WHERE id = %s",
        (num_questions, run_id),
    )


def insert_question(
    run_id: str,
    question: str,
    qtype: Optional[str],
    complexity: int,
    answer: Optional[str],
    tool_trace: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    num_iterations: int,
    duration_ms: int,
    error: Optional[str],
) -> str:
    row = pg.execute(
        """INSERT INTO eval_questions
            (run_id, question, question_type, complexity, answer, tool_trace, sources,
             num_iterations, duration_ms, error)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            run_id,
            question,
            qtype,
            complexity,
            answer,
            json.dumps(tool_trace, default=str),
            json.dumps(sources, default=str),
            num_iterations,
            duration_ms,
            error,
        ),
    )
    return str(row[0]["id"])


def insert_judgments(
    question_id: str, judgments: List[Dict[str, Any]], judge_model: str
) -> None:
    for j in judgments:
        pg.execute_void(
            """INSERT INTO eval_judgments
                (question_id, paragraph_idx, paragraph_text, cited_indices,
                 verdict, rationale, judge_model, metadata_mismatch, coverage_score)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                question_id,
                j["paragraph_idx"],
                j["paragraph_text"],
                j["cited_indices"],
                j["verdict"],
                j["rationale"],
                judge_model,
                j.get("metadata_mismatch"),
                j.get("coverage_score"),
            ),
        )


# ---------------------------------------------------------------------------
# Compact source extraction
# ---------------------------------------------------------------------------

def compact_sources(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "n": i + 1,
            "speech_id": s.get("_id"),
            "speaker": s.get("speaker"),
            "party": s.get("party"),
            "date": s.get("date"),
            "heading": s.get("heading"),
            "person_id": s.get("person_id"),
            "snippet": (s.get("snippet") or "")[:400],
        }
        for i, s in enumerate(sources or [])
    ]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def backfill_scores(scorer: CitationScorer, run_id: Optional[str] = None) -> None:
    """Populate coverage_score for all eval_judgments rows where it is NULL.

    Fetches question sources from the DB, re-downloads full talk texts, and calls
    the scorer.  Optionally scoped to a single run_id; defaults to all rows.
    """
    if not scorer._check_available():
        print("[backfill] scorer not available — aborting")
        return

    where = "WHERE j.coverage_score IS NULL"
    params: tuple = ()
    if run_id:
        where += " AND q.run_id = %s"
        params = (run_id,)

    rows = pg.execute(
        f"""SELECT j.id, j.paragraph_text, j.cited_indices, q.sources
            FROM eval_judgments j
            JOIN eval_questions q ON q.id = j.question_id
            {where}
            ORDER BY j.created_at""",
        params or None,
    )
    print(f"[backfill] {len(rows)} judgments to score")

    for n, row in enumerate(rows, 1):
        sources = row["sources"] or []
        cited = row["cited_indices"] or []
        paragraph = row["paragraph_text"] or ""

        cited_sources = [s for s in sources if s.get("n") in cited]
        talk_ids = [s.get("speech_id") or s.get("_id") for s in cited_sources if s.get("speech_id") or s.get("_id")]
        full_texts = _fetch_full_talks(talk_ids)

        parts: List[str] = []
        for s in cited_sources:
            idx = s.get("n", "?")
            tid = (s.get("speech_id") or s.get("_id") or "").split("/")[-1]
            meta = full_texts.get(tid)
            if meta:
                parts.append(f"[{idx}] {meta['speaker_name']} ({meta['party']}) — {meta['date']}\n{meta['text']}")
            elif s.get("snippet"):
                parts.append(f"[{idx}] {s.get('speaker')} ({s.get('party')})\n{s['snippet']}")

        if not parts:
            continue

        combined = "\n\n".join(parts)
        score = scorer.score_all(paragraph, combined)
        if score is not None:
            pg.execute_void(
                "UPDATE eval_judgments SET coverage_score = %s WHERE id = %s",
                (score, row["id"]),
            )
        if n % 100 == 0:
            print(f"[backfill] {n}/{len(rows)} done")

    print(f"[backfill] complete — {len(rows)} rows processed")


def rejudge_run(run_id: str, judge: "Judge", judge_model: str, only_missing: bool = True) -> None:
    """Re-run the judge on all answered questions in a run.

    Args:
        only_missing: if True (default), skip questions that already have judgments.
                      Pass False to wipe and redo all.
    """
    filter_sql = (
        "AND NOT EXISTS (SELECT 1 FROM eval_judgments j WHERE j.question_id = q.id)"
        if only_missing else ""
    )
    questions = pg.execute(
        f"""SELECT q.id, q.answer, q.sources
            FROM eval_questions q
            WHERE q.run_id = %s AND q.answer IS NOT NULL {filter_sql}
            ORDER BY q.created_at""",
        (run_id,),
    )
    print(f"[rejudge] {len(questions)} questions to (re)judge in run {run_id}")
    for n, row in enumerate(questions, 1):
        q_id = str(row["id"])
        sources = row["sources"] or []
        if not only_missing:
            pg.execute_void("DELETE FROM eval_judgments WHERE question_id = %s", (q_id,))
        try:
            judgments = judge.verdict(row["answer"], sources)
            insert_judgments(q_id, judgments, judge_model)
            verdicts = [j["verdict"] for j in judgments]
            print(f"[rejudge {n}/{len(questions)}] {q_id[:8]}… → {verdicts}")
        except Exception as e:
            print(f"[rejudge {n}/{len(questions)}] {q_id[:8]}… failed: {e}")


def replay_run(
    src_run_id: str,
    label: str,
    judge: "Judge",
    judge_model: str,
    chat: "ChatService",
    use_editor: bool = False,
    sleep_ms: int = 0,
    max_consecutive_errors: int = 10,
    error_backoff_s: int = 30,
) -> None:
    """Re-run ChatService on every question from an existing run and store results as a new run.

    The source run's questions are replayed in order.  Each question gets a fresh
    ChatService call (with the new config, e.g. use_editor), new answer, new sources,
    and new judgments — all stored under a new run_id so the two runs can be compared
    side-by-side.  The source run is never modified.
    """
    src_questions = pg.execute(
        """SELECT id, question, complexity FROM eval_questions
           WHERE run_id = %s AND question IS NOT NULL
           ORDER BY created_at""",
        (src_run_id,),
    )
    if not src_questions:
        print(f"[replay] no questions found in source run {src_run_id}")
        return

    config = {
        "smart_model": SMART_MODEL,
        "fast_model": FAST_MODEL,
        "judge_model": judge_model,
        "git_sha": _git_sha(),
        "use_editor": use_editor,
        "replay_of": src_run_id,
    }
    run_id = create_run(label, config)
    os.environ["EVAL_RUN_ID"] = run_id
    print(f"[replay] new run_id={run_id} label={label!r} replaying {len(src_questions)} questions from {src_run_id[:8]}…")

    completed = 0
    consecutive_errors = 0

    for i, src_q in enumerate(src_questions):
        question = src_q["question"]
        complexity = src_q["complexity"] or 1

        q_uuid = str(uuid.uuid4())
        os.environ["EVAL_QUESTION_ID"] = q_uuid
        print(f"\n[replay {i+1}/{len(src_questions)}] complexity={complexity} q={question!r}")

        trace = TraceCollector()
        t0 = time.time()
        answer = None
        sources_compact: List[Dict[str, Any]] = []
        err_msg: Optional[str] = None
        try:
            result = chat.get_chat_response(
                messages=[{"role": "user", "content": question}],
                event_callback=trace.callback,
                use_editor=use_editor,
            )
            answer = result.get("answer")
            sources_compact = compact_sources(result.get("sources") or [])
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                print(f"[replay] {consecutive_errors} consecutive errors — aborting.")
                insert_question(
                    run_id=run_id, question=question, qtype=None,
                    complexity=complexity, answer=None, tool_trace=trace.events,
                    sources=[], num_iterations=trace._iter,
                    duration_ms=int((time.time() - t0) * 1000), error=err_msg,
                )
                break
            print(f"[replay] backing off {error_backoff_s}s (consecutive={consecutive_errors})")
            time.sleep(error_backoff_s)
        duration_ms = int((time.time() - t0) * 1000)

        question_id = insert_question(
            run_id=run_id,
            question=question,
            qtype=None,
            complexity=complexity,
            answer=answer,
            tool_trace=trace.events,
            sources=sources_compact,
            num_iterations=trace._iter,
            duration_ms=duration_ms,
            error=err_msg,
        )
        os.environ["EVAL_QUESTION_ID"] = question_id

        if answer:
            try:
                judgments = judge.verdict(answer, sources_compact)
                insert_judgments(question_id, judgments, judge_model)
                verdicts = [j["verdict"] for j in judgments]
                print(f"[replay] verdicts={verdicts} ms={duration_ms} iters={trace._iter}")
            except Exception as e:
                print(f"[replay] judge failed (non-fatal): {e}")

        if not err_msg:
            consecutive_errors = 0
        completed += 1

        if sleep_ms:
            time.sleep(sleep_ms / 1000.0)

    os.environ.pop("EVAL_QUESTION_ID", None)
    finalize_run(run_id, completed)
    print(f"\n[replay] done. new run_id={run_id} completed={completed}/{len(src_questions)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default=None, help="Human-readable run label (required unless --rejudge-run is set).")
    ap.add_argument("--iterations", type=int, default=100)
    ap.add_argument("--judge-model", default=None, help="Model for judge + generator (default: LLM_MODEL_SMART).")
    ap.add_argument("--sleep-ms", type=int, default=0, help="Sleep between iterations.")
    ap.add_argument("--max-consecutive-errors", type=int, default=10,
                    help="Abort after this many consecutive hard failures (default: 10).")
    ap.add_argument("--error-backoff-s", type=int, default=30,
                    help="Seconds to wait after a hard failure before retrying (default: 30).")
    ap.add_argument("--rejudge-run", metavar="RUN_ID",
                    help="Re-run the judge on answered questions in this run (skips generation). "
                         "By default only fills in missing judgments; combine with --rejudge-all to redo everything.")
    ap.add_argument("--rejudge-all", action="store_true",
                    help="With --rejudge-run: wipe and redo ALL judgments, not just missing ones.")
    ap.add_argument("--backfill-scores", action="store_true",
                    help="Populate coverage_score for all judgments where it is NULL, then exit. "
                         "Combine with --rejudge-run to scope to a single run.")
    ap.add_argument("--replay-run", metavar="RUN_ID",
                    help="Re-run ChatService on every question from an existing run and store "
                         "results as a new run (requires --label). Use with --use-editor to "
                         "compare editor vs no-editor on identical questions.")
    ap.add_argument("--use-editor", action="store_true",
                    help="Run the editor fact-check + language-polish pass on every answer. "
                         "Uses the smart model by default (or the provider's editor model if set).")
    args = ap.parse_args()

    if not args.rejudge_run and not args.replay_run and not args.label and not args.backfill_scores:
        ap.error("--label is required when not using --rejudge-run, --replay-run, or --backfill-scores")
    if args.replay_run and not args.label:
        ap.error("--replay-run requires --label for the new run")

    ensure_migration()

    judge_model = args.judge_model or SMART_MODEL
    llm_url = os.getenv("LLM_DIRECT_URL")

    generator_llm = LLM(
        model=judge_model,
        system_message=GENERATOR_SYSTEM,
        temperature=0.9,
        base_url=llm_url,
    )
    judge_llm = LLM(
        model=judge_model,
        system_message=JUDGE_SYSTEM,
        temperature=0.1,
        base_url=llm_url,
    )
    scorer = CitationScorer()
    generator = QuestionGenerator(generator_llm)
    judge = Judge(judge_llm, judge_model, scorer=scorer)

    # --backfill-scores: populate coverage_score on existing judgments, then exit.
    if args.backfill_scores:
        backfill_scores(scorer, run_id=args.rejudge_run or None)
        return

    # --replay-run: re-run ChatService on an existing run's questions under a new label.
    if args.replay_run:
        replay_run(
            src_run_id=args.replay_run,
            label=args.label,
            judge=judge,
            judge_model=judge_model,
            chat=ChatService(),
            use_editor=bool(args.use_editor),
            sleep_ms=args.sleep_ms,
            max_consecutive_errors=args.max_consecutive_errors,
            error_backoff_s=args.error_backoff_s,
        )
        return

    # --rejudge-run mode: skip generation, just re-run judge on existing questions.
    if args.rejudge_run:
        rejudge_run(args.rejudge_run, judge, judge_model, only_missing=not args.rejudge_all)
        return

    config = {
        "smart_model": SMART_MODEL,
        "fast_model": FAST_MODEL,
        "judge_model": judge_model,
        "git_sha": _git_sha(),
        "llm_url": llm_url,
        "use_editor": bool(args.use_editor),
    }
    run_id = create_run(args.label, config)
    os.environ["EVAL_RUN_ID"] = run_id
    print(f"[eval] run_id={run_id} label={args.label!r} iterations={args.iterations}")

    chat = ChatService()
    completed = 0
    consecutive_errors = 0

    for i in range(args.iterations):
        try:
            # ----------------------------------------------------------------
            # Generate question
            # ----------------------------------------------------------------
            try:
                question, complexity = generator.generate()
            except Exception as e:
                print(f"[eval] generator failed iter={i}: {e}")
                consecutive_errors += 1
                if consecutive_errors >= args.max_consecutive_errors:
                    print(f"[eval] {consecutive_errors} consecutive errors — aborting.")
                    break
                time.sleep(args.error_backoff_s)
                continue
            if not question:
                continue

            q_uuid = str(uuid.uuid4())
            os.environ["EVAL_QUESTION_ID"] = q_uuid
            print(f"\n[eval {i+1}/{args.iterations}] complexity={complexity} q={question!r}")

            # ----------------------------------------------------------------
            # Run ChatService
            # ----------------------------------------------------------------
            trace = TraceCollector()
            t0 = time.time()
            answer = None
            sources_compact: List[Dict[str, Any]] = []
            err_msg: Optional[str] = None
            try:
                result = chat.get_chat_response(
                    messages=[{"role": "user", "content": question}],
                    event_callback=trace.callback,
                    use_editor=bool(args.use_editor),
                )
                answer = result.get("answer")
                sources_compact = compact_sources(result.get("sources") or [])
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                traceback.print_exc()
                consecutive_errors += 1
                if consecutive_errors >= args.max_consecutive_errors:
                    print(f"[eval] {consecutive_errors} consecutive errors — aborting.")
                    # Still try to persist what we have before exiting.
                    insert_question(
                        run_id=run_id, question=question, qtype=None,
                        complexity=complexity, answer=None, tool_trace=trace.events,
                        sources=[], num_iterations=trace._iter,
                        duration_ms=int((time.time() - t0) * 1000), error=err_msg,
                    )
                    break
                print(f"[eval] backing off {args.error_backoff_s}s (consecutive={consecutive_errors})")
                time.sleep(args.error_backoff_s)
            duration_ms = int((time.time() - t0) * 1000)

            # ----------------------------------------------------------------
            # Persist question
            # ----------------------------------------------------------------
            question_id = insert_question(
                run_id=run_id,
                question=question,
                qtype=None,
                complexity=complexity,
                answer=answer,
                tool_trace=trace.events,
                sources=sources_compact,
                num_iterations=trace._iter,
                duration_ms=duration_ms,
                error=err_msg,
            )
            # Re-sync env with the DB-assigned id so downstream events carry the real id.
            os.environ["EVAL_QUESTION_ID"] = question_id

            # ----------------------------------------------------------------
            # Judge
            # ----------------------------------------------------------------
            if answer:
                try:
                    judgments = judge.verdict(answer, sources_compact)
                    insert_judgments(question_id, judgments, judge_model)
                    verdicts = [j["verdict"] for j in judgments]
                    print(f"[eval] verdicts={verdicts} ms={duration_ms} iters={trace._iter}")
                except Exception as e:
                    print(f"[eval] judge failed (non-fatal): {e}")

            # Reset error streak on any successful iteration.
            if not err_msg:
                consecutive_errors = 0
            completed += 1

        except Exception as e:
            # Outermost safety net — should never be reached, but keeps the loop alive.
            print(f"[eval] unexpected error iter={i}: {e}")
            traceback.print_exc()
            consecutive_errors += 1
            if consecutive_errors >= args.max_consecutive_errors:
                print(f"[eval] {consecutive_errors} consecutive errors — aborting.")
                break
            time.sleep(args.error_backoff_s)

        if args.sleep_ms:
            time.sleep(args.sleep_ms / 1000.0)

    os.environ.pop("EVAL_QUESTION_ID", None)
    finalize_run(run_id, completed)
    print(f"\n[eval] done. run_id={run_id} completed={completed}")
    print(
        "Inspect: "
        f"SELECT verdict, COUNT(*) FROM eval_judgments j "
        f"JOIN eval_questions q ON q.id=j.question_id "
        f"WHERE q.run_id='{run_id}' GROUP BY verdict;"
    )


if __name__ == "__main__":
    main()
