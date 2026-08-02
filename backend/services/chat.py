from __future__ import annotations

import os
import queue
import threading

from backend.services.event_logger import log_event, log_error
from backend.services.eval_conversation_logger import (
    ConversationRecorder,
    detect_and_strip_test_prefix,
    sanitize_provider,
)
from typing import Any, Callable, Dict, Generator, List, Sequence, Optional, Tuple
from pydantic import BaseModel, Field
import backend.services.llm_tools
from backend.services.llm_tools import (
    SearchHitsResult,
    HitsResponse,
    _tool_structured_result,
    _insight_callback,
    _provenance_registry,
    _fast_llm_var,
    share_insight,
)
from packages.llm import LLM, get_tools, ChatCompletionMessage
from backend.services.provenance import (
    ProvenanceRegistry,
    SourceRecord,
    normalize_talk_id,
    parse_and_renumber_citations,
    _SRC_PATTERN,
)
from backend.services.research_models import (
    ResearchRequest,
    ResearchReport,
    SubFinding,
    SubQuestion,
)
from packages.colorprinter import *
import json
import re
from datetime import date

ChatResponse = Dict[str, Any]
ChatSource = Dict[str, Any]
ChatMessage = Dict[str, Any]


def _is_duplicate_insight(candidate: str, sent: List[str], threshold: float = 0.6) -> bool:
    """Return True if candidate overlaps too heavily with any previously sent insight.

    Uses word-level Jaccard similarity so model-instruction dedup failures don't
    reach the user. threshold=0.6 means 60% word overlap triggers suppression.
    """
    if not sent:
        return False
    words_c = set(candidate.lower().split())
    if not words_c:
        return False
    for s in sent:
        words_s = set(s.lower().split())
        if not words_s:
            continue
        overlap = len(words_c & words_s) / len(words_c | words_s)
        if overlap >= threshold:
            return True
    return False

# Per-tool nudges appended to the cached-result message when the model repeats
# an identical call, steering it toward a genuinely different next step.
_DEDUP_HINTS = {
    "arango_search": "Vary the keywords/filters, or try vector_search for semantic matching, or database_query for counts.",
    "vector_search": "Rephrase the query as a content statement, or use arango_search with metadata filters.",
    "vector_search_debates": "Pick a debate from the previous result and call fetch_debate, or vary the query.",
    "fetch_debate": "You already have this debate's talks — read specific talks with read_documents_for instead.",
    "fetch_documents": "You already have these documents. Use read_documents_for with a focused question if you need their substance.",
    "read_documents_for": "Ask a DIFFERENT question or read different documents.",
    "database_query": "The result will not change — use the rows you already received.",
    "lookup_source": "You already recalled these sources; the text is in your history.",
    "search_motions": "Vary the keywords/filters, or try vector_search_motions for semantic matching, or database_query for counts.",
    "vector_search_motions": "Rephrase the query as a content statement, or use search_motions with metadata filters.",
    "fetch_motion": "You already have this motion — use read_documents_for with a focused question if you need its substance.",
}


def _dedup_hint(tool_name: str) -> str:
    return _DEDUP_HINTS.get(tool_name, "Try a different tool or different arguments.")


FAST_MODEL = os.getenv("LLM_MODEL_FAST", "smart")
SMART_MODEL = os.getenv("LLM_MODEL_SMART", "smart")
print_blue(f"Using SMART_MODEL={SMART_MODEL} and FAST_MODEL={FAST_MODEL} for ChatService.")
print_blue(f"Using LLM_MODEL_EMBEDDING={os.getenv('LLM_MODEL_EMBEDDING')} for embeddings.")

date_today = date.today().strftime("%Y-%m-%d")

ORCHESTRATOR_SYSTEM = """
You help users find information in speeches (anföranden) and motions (motioner) from the Swedish Riksdag. You have several tools available to search the database; use these tools whenever you need data not present in earlier messages.
The data in the database is correct, including party affiliations, dates, and speaker names. If you find something in the data, you can trust that it's accurate and use it in your answer. Trust the data, not your prior assumptions or general world knowledge.

*Important operational rules:*
- Always read each tool's description and arguments carefully before calling it; follow examples.
- When presenting results, cite sources by mentioning the talk titles and dates when available.
- You may call multiple tools in one conversation; if one tool doesn't return what you need, call another.
- Summarize and analyze findings continuously so you know what you have and what you still need. By including things like _id:s and other valuable information in your reasoning, this will be stored to your memory.
- If you find something concrete that you'll rely on (a speaker, a count, a pattern), surface it with `share_insight` so the user can follow your progress — keep the message to one sentence.
- When you need more data, call a tool. When you want to share a finding, call `share_insight`. When you are done, give your final answer. Do not describe what you are about to do in plain text without taking an action.

**Decision / tool-selection map (follow this strictly):**

1. `arango_search(query, people, parties, from_year, to_year, limit, return_snippets, intressent_ids)`
   - Use for: finding speeches by keyword, phrase, person, party, or year.
   - Supports: `intressent_ids=["012345678"]` and `people=["Helena Gellermann"]` to filter by speaker, `parties=["S","M"]` to filter by party.
   - Use intressent_ids if you have them from earlier searches to find speeches by specific individuals, better  than filtering by the `people` parameter.
   - Use `return_snippets=True` for a quick overview.
   - If a search returns fewer results than your requested limit, or if `limit reached: False`, it means you have retrieved all available documents. Do not repeat the same search with a higher limit.

2. `vector_search(query, limit)` — semantic/conceptual search.
   - Use when keywords alone won't work (vague topics, synonyms, thematic clusters).
   - Under the hood this blends chunk-level passages (quote-ready) with summary-level gists (thematic) and merges them by talk, so you get a mix in a single call. Each hit carries `source_type` in metadata: `"chunk"`, `"summary"`, or `"both"`.
   - You do NOT need to choose between snippet- and summary-level searching; this tool does both. Use as a complement to `arango_search`, not a replacement.

3. `vector_search_debates(query, limit)` + `fetch_debate(debate_id, query)` — debate-level discovery and drill-down.
   - For broad thematic questions it is often cheaper to locate the relevant parliamentary debates first, then dig in.
   - `vector_search_debates` returns ~5 debates with their summaries. The ids look like `"2021-06-17:42"` (bare date:index form). **Do not cite debates directly** — they are a navigation aid.
   - Pick the best debate and call `fetch_debate(debate_id, query=<same query>)`. You get the debate summary plus a compact list of talks (id, talare, parti, intressent_id, per-talk summary). **Pass the same query** — long debates are trimmed by semantic relevance to it; without a query, a chronological slice is returned and a `note` field tells you how many talks were omitted. Cite the individual talks with `[src:TALK_ID]` as usual.
   - Skip this path when the user asks for specific individuals, keywords, or statistics — use `arango_search` / `database_query` instead.

4. `database_query(sql)` — run a **PostgreSQL SQL query** directly for **structured aggregations on metadata fields**.
   - Use for: count/rank by party, year, speaker, debate type — e.g. "how many speeches per party?" or "top 10 most active speakers in S?"
   - **Exact column names**
    — `talks`: id, talare, parti, year, datum (DATE), intressent_id, kammaraktivitet, replik, anforande_nummer, debate, summary, tags, anforandetext.
    - `people`: intressent_id, namn, parti, fodd_ar, kon, aktiv, valkrets.
    - `debates`: debate (PK), datum (DATE), summary, num_talks, talk_ids (TEXT[]).
    - `motions`: dok_id (PK), rm, year, datum (DATE), titel, subtyp, organ, status, parties (TEXT[]), author_names (TEXT[]), num_yrkanden, text.
    - `motion_authors`: dok_id, intressent_id, namn, partibet, ordinal (0 = first author).
    - `motion_yrkanden`: id (PK), dok_id, nummer, lydelse (the condensed proposal text), utskottet, kammaren (chamber decision e.g. 'Avslag'/'Bifall'), behandlas_i.
    -> **Use only these — never invent columns.**
   - Motions FTS: `WHERE search_vector @@ websearch_to_tsquery('swedish', '...')` works on `motions` too (it covers titel + yrkanden + full text). Party filter on motions: `parties && ARRAY['S']` (any co-author) or `unnest(parties)` to group per party.
   - To analyse concrete proposals or their outcomes, use `motion_yrkanden` (join to motions on dok_id); e.g. count yrkanden per chamber decision: `SELECT kammaren, COUNT(*) FROM motion_yrkanden GROUP BY kammaren`.
   - Cast dates to text when selecting: `datum::text`.
   - It's a good idea to include `intressent_id` in your SELECT clause when querying the talks table, as it allows you to link back to specific speakers and their profiles.
   - For **content-based counts** ("how many speeches per party about AI?") use FTS: `WHERE search_vector @@ websearch_to_tsquery('swedish', 'AI OR artificiell intelligens')` — uses the GIN index, supports Swedish stemming, phrases, OR, exclusion.
   - ⚠️ **NEVER** use `anforandetext @@` — it bypasses the index and causes a full table scan. Always use `search_vector @@` for content search.
   - ⚠️ **NEVER** use LIKE/ILIKE on `anforandetext` — slow full table scan, wrong results ('ai' matches 'Thai', 'Ukraine'). Use `search_vector @@` + `websearch_to_tsquery` instead.
   - Keep letters åäö as they are, if substituting with a a o there will be no hits for those words (this and other tools).

5. `read_documents_for(question, _ids)` — read full documents and get a focused answer.
   - Use after `arango_search`, `vector_search`, or `fetch_debate` when you need to know what specific speeches actually SAY (positions, arguments, exact statements) — this is the default way to go deeper than snippets.
   - A reading assistant reads the full texts (up to 6 ids) and returns a short grounded answer with `[src:ID]` tags and verbatim quotes. Ask ONE concrete question per call.
   - Prefer this over `fetch_documents`: you get the substance without flooding your context with raw text.

6. `fetch_documents(_ids)` — fetch full raw document text by ID.
   - Use ONLY when you truly need the complete verbatim text (e.g. the user explicitly asks to see a whole speech). For "what does the speech say about X?" use `read_documents_for` instead.
   - Pass `fields=["anforandetext", "talare", "intressent_id", "datum"]` to keep the response compact.

7. `lookup_source(source_ids)` — recall the stored grounding text for sources you've already seen.
   - Search results in your message history are compacted to one-line `[src:ID] Speaker (Party) date — heading — preview` rows once registered. The full snippet/text is kept server-side.
   - Call `lookup_source(["H40911", "GH09100"])` ONLY when you actually need the underlying text to quote verbatim or verify a specific claim. For most claims the eviction stub + your own notes are enough.
   - **Maximum 5 source IDs per call.** Pick the few you really need; bodies are truncated to keep your context lean.

8. `search_motions(query, people, parties, from_year, to_year, limit, return_snippets, intressent_ids)` + `vector_search_motions(query, limit)` + `fetch_motion(dok_id)` — MOTIONER (written proposals from MPs).
   - **Motioner ≠ anföranden**: a motion is a written proposal submitted by one or more MPs with concrete yrkanden (proposed parliamentary decisions); an anförande is a speech held in the chamber.
   - **Anföranden are your PRIMARY source — search speeches first.** Motion tools are a SECONDARY, complementary source. Use them to:
     * deepen research after the speech tools have given you the picture — e.g. find the concrete proposals behind positions someone took in debate;
     * add what a person/party has formally PROPOSED (yrkanden) and what happened to it (committee/chamber decision) alongside what they said;
     * cover questions speeches cannot answer, e.g. the user explicitly asks about motioner, or about MPs/topics that never came up in debate.
   - Do NOT lead with motion tools for general questions ("vad tycker X om Y?") — start with `arango_search`/`vector_search`, then complement with motions when proposals matter for the answer.
   - `search_motions` = keyword/FTS search (like `arango_search` but over motions; `parties`/`people` match any co-author). `vector_search_motions` = semantic search (like `vector_search`). Same query syntax and filters.
   - `fetch_motion(dok_id)` returns the motion's metadata, all authors, all yrkanden with committee proposal (`utskottet`) and chamber decision (`kammaren` — e.g. "Avslag"/"Bifall"), and the full text. Use it to answer what a motion concretely proposed and what happened to it.
   - Motion hits are cited like speeches: `[src:HD02846]`. `read_documents_for` accepts motion ids too. In your answer, make clear which claims come from speeches and which from motions.
   - Note: motions from before ~1995 may only exist as scanned PDFs (metadata present, `note` says fulltext saknas).

**Notes:**
- You may call **multiple tools in a single turn** — this is encouraged.
- `arango_search` with `return_snippets=True`: gives highlighted excerpts — use to quickly scan what topics appear before fetching full texts.
- `focus_ids`: pass `focus_ids=focus_ids` to narrow the next search to previously found documents.

Once you have gathered enough information to fully answer the user's prompt, DO NOT call any more tools. Immediately output your final answer to the user.

**When giving your final answer:**
- Respond concisely, the user is not here for small talk.
- **IMPORTANT: Always format your answer using Markdown.** The frontend will convert it to HTML automatically.
- **IMPORTANT: Cite sources using `[src:...]` tags.** Each tool result begins with an enriched tag like `[src:H40911 | Ulla Hoffmann (V) | 2005-12-07]`. The part after `src:` up to the next `|` is the canonical ID; the speaker and date that follow are the ground truth for who said what. **Copy the whole tag verbatim** after the claim it supports — do not restate the speaker or party from memory or world knowledge, and do not mix up which tag goes with which claim. Example: `ROT-avdraget infördes 2009[src:H40911 | Anders Borg (M) | 2008-12-03] och syftade till att minska svartarbete[src:GH09100 | Stefan Löfven (S) | 2009-04-22].`
- If a claim is general and based on very many sources (>8), don't use citations for that particular
- If you base an important part of your answer on specific speeches, make sure to have read them in full and cite them properly — don't just rely on snippets.
- **Do NOT write a "Källor" (Sources) section** — it is generated automatically by the system.
- **Do NOT use `[1]`, `[2]` numbering** — use only `[src:ID]` tags from tool results.
- **Do NOT cite `database_query` results with `[src:...]`** — statistics and counts don't have individual source IDs. Just state the numbers.
- If refering to a politician in text, do it like Name Lastname (PARTY CODE). Example:  "Jan Riise (MP)".
- Don't ever make up quotes or facts; if you don't have enough information, say that you don't know, or call another tool to find more information.
- Answer in Swedish.

Today is {date_today}, so any references to "current year" or "recently" should be interpreted in that context.
"""

WORKER_SYSTEM = """You read speeches made in the Swedish parliament and write concise, structured summaries.
You will get the full text of a speech, along with the speakers name. You will also get instructions on what to look for in the speech, based on the user's question and the research assistant's current findings.
Your task is to *extract the most important statements relevant to the question*, and write a concise summary.
Include specific names, dates and numbers when relevant to the question. If the speech contains a particularly interesting or relevant quote, include that too.
Note: A single speech might not be able to answer the user's question on its own, rather use the question as a lens to identify and extract the most relevant information from the speech.
"""

EDITOR_SYSTEM = """Du är korrekturläsare på en nyhetsdesk som bevakar svenska riksdagen. En reporter har lämnat ett utkast och du ska göra EN MINIMAL faktagranskning — inte skriva om, inte sammanfatta, inte korta ned.

Du får:
1. Användarens ursprungliga fråga.
2. Utkastet (markdown med [src:ID | Talare (Parti) | datum]-taggar inbäddade).
3. De citerade taltexterna.

Din uppgift är BEGRÄNSAD till:

1. **Rätta felaktiga namn/parti** bredvid en [src:…]-tagg om källans metadata visar en annan talare eller ett annat parti. Ändra bara det felaktiga namnet/partiet — rör inte resten av meningen.
2. **Rätta fabricerade direktcitat** (text i "…") som inte finns ordagrant i källtexten — omformulera som indirekt referens eller ta bort citattecknen.
3. **Minimala språkliga justeringar** — bara om något är uppenbart fel. Ändra inte stil, struktur eller innehåll.

**KRITISKA REGLER:**
- Det reviderade svaret ska vara UNGEFÄR LIKA LÅNGT som utkastet. Kortare svar betyder att du tagit bort innehåll — det är FÖRBJUDET.
- Bevara ALL text, ALL struktur, ALLA rubriker, ALLA punktlistor från utkastet.
- Bevara [src:…]-taggarna EXAKT. Flytta dem bara om du omformulerar den mening de tillhör.
- Lägg INTE till ny text, nya påståenden eller nya källor.
- Om utkastet är korrekt: returnera det i princip oförändrat.

**Format:** returnera ENDAST det reviderade markdown-svaret. Ingen inledning, ingen förklaring.
"""

FACT_CHECKER_SYSTEM = """Du är en noggrann faktaredaktör med specialisering på riksdagsdebatter.

Du analyserar ett stycke i ett svar och jämför det mot citerade källor. Din uppgift är att identifiera felaktigheter — INTE att rätta dem.

Returnera din analys som JSON med exakt detta schema:
{
  "issues": [
    {
      "quote": "<den exakta frasen i stycket som är felaktig>",
      "problem": "<vad som är fel — t.ex. fel talare, fel parti, påståendet stöds inte av källan>",
      "source_says": "<vad källan faktiskt säger, kortfattat>"
    }
  ],
  "verdict": "ok"
}
eller
{
  "issues": [...],
  "verdict": "needs_fix"
}

Om stycket är korrekt, returnera issues=[] och verdict="ok".
Returnera ENBART JSON — ingen inledning, ingen förklaring.
"""

LANGUAGE_CHECKER_SYSTEM = """Du är en språkgranskare som förbättrar svenska texter om riksdagsdebatter.

Du får ett svar med inbäddade källhänvisningar i formatet [1], [2] etc. och persontaggar.

Din ENDA uppgift: rätta grammatik, förbättra flöde och klarhet på svenska.

ABSOLUTA REGLER — bryt inte dessa:
- Bevara ALLA [1], [2]-taggar exakt som de är (inklusive plats i texten).
- Bevara ALLA fotnoter och referenser exakt som de är.
- Ändra INTE innehåll, fakta, påståenden eller slutsatser.
- Ändra INTE struktur — samma stycken, rubriker, punktlistor som originalet.
- Förkorta INTE texten — den reviderade versionen ska vara ungefär lika lång.

**Texten du returnerar ska vara densamma som den du får, bara bättre språkligt.**

Returnera ENBART den förbättrade markdown-texten. Ingen inledning, ingen förklaring.
"""

# Tool results longer than this are summarized by the fast model before being
# fed back to the smart orchestrator, keeping its context window lean.
SUMMARIZE_THRESHOLD = 10000

# Soft cap on the running message-history size (in characters; roughly chars/4
# tokens). When current_messages exceeds this, the oldest tool results are
# compacted to a one-line stub that points back to the registry. Sources stay
# citable because the registry holds them; lookup_source can recall the body.
HISTORY_CHAR_BUDGET = 50000

# Instruction appended at the end of the communicator's probe (after the full
# message history + latest tool result).  Kept separate from ORCHESTRATOR_SYSTEM
# so the shared prefix stays identical between orchestrator and communicator —
# this maximises vLLM KV-cache hits.  Edit this string to change what the
# communicator looks for and how it phrases its insights.
_SHADOW_INSTRUCTION = """Du är en kommunikatör som ser till att användaren underhålls och förstår de viktigaste insikterna från researchprocessen i realtid.

I meddelandehistoriken ser du både användarens frågor och de verktygssvar som researchassistenten har fått fram hittills. \
Din ENDA uppgift: avgör om det senaste verktygsresultatet innehåller något konkret och intressant värt att visa för användaren *just nu*.

**Om ja** — anropa `share_insight` med lämpliga argument. Läs beskrivning av verktyget noga! Där finns exempel på hur du kan använda det för att dela olika typer av insikter.

**Om nej** — anropa inget verktyg alls. Skriv ingenting.

Dela INTE om:
- Du redan delat liknande fakta (se listan nedan om sådan finns).
- Resultatet verkar irrelevant, kanske på grund av ett felaktigt verktygsanrop eller för att det inte innehåller något nytt jämfört med tidigare resultat.

Obs! Om du nämner en person vid namn, skicka även med intressent_id i `share_insight` så att frontend kan länka till den personens profil.

Försök tänka som en journalist, utan att överdriva eller spela över. Vad kan vara intressant? Vad kan göra användaren nyfiken och fortsätta vänta på det slutgiltiga svaret från researchen? Vad kan vara kul att lyfta fram (försök dock inte skämta)?
"""

# Per-document summarisation thresholds
DOC_SUMMARIZE_THRESHOLD = 1500  # chars; text below this passes through unchanged
DOC_MAX_INPUT = 20000  # chars fed per document to fast model
DOC_MAX_BATCH = 20  # max documents summarised per run


class FinalAnswer(BaseModel):
    final_answer: str = Field(..., description="Your final answer")
    explanation: str = Field(
        ...,
        description="Your short and non-technical explanation of how you arrived at the answer",
    )


# Hard caps for the planner/researcher pre-pass.
RESEARCH_MAX_SUBQUESTIONS = 3
RESEARCH_ITERATIONS_PER_SUBQ = 5

PLANNER_SYSTEM = """Du planerar research för ett svensk-riksdags chat-system.

Du läser användarens fråga och bryter ner den i 1–{max_sub} specifika delfrågor som var och en kan besvaras med data från riksdagens tal, debatter och statistik.

REGLER:
- Returnera EXAKT strukturen ResearchRequest (Pydantic).
- Om frågan är enkel/atomär — returnera EN delfråga.
- Om frågan har flera tydliga delar — bryt ner i 2–{max_sub} delfrågor.
- ALDRIG fler än {max_sub} delfrågor.
- Varje delfråga ska kunna besvaras självständigt (en delfråga = en search-runda).
- `id` ska vara kort, t.ex. "q1", "q2", "q3".
- `needs_quotes=true` BARA om delfrågan kräver direkta citat (t.ex. "vad sa X exakt?").
- `hints` är valfri lista av personnamn, partier, ämnesnyckelord som forskaren bör fokusera på.
- Skriv delfrågorna på svenska.
"""

RESEARCHER_SYSTEM = """Du är en research-assistent som undersöker EN specifik delfråga i tal från svenska riksdagen.

Du har samma data-verktyg som huvudassistenten: arango_search, vector_search, vector_search_debates, fetch_debate, database_query, read_documents_for, fetch_documents, lookup_source, search_motions, vector_search_motions, fetch_motion.
Behöver du veta vad specifika tal faktiskt SÄGER — använd `read_documents_for(question, _ids)` (en läsassistent läser fulltexterna och svarar fokuserat) i stället för att hämta rå fulltext med fetch_documents.

Arbetssätt:
1. Läs delfrågan noga, planera sökningar.
2. Kör verktygen tills du har tillräckligt med material.
3. När du är klar — anropa INTE fler verktyg, utan returnera en strukturerad SubFinding.

Regler:
- `sub_question_id` MÅSTE vara samma id som delfrågan du undersökte.
- `answer` är 1–3 meningar på svenska som svarar på delfrågan, baserat på källorna.
- `source_ids` är en lista av rena tal-id:n (t.ex. "H40911") från registrerade källor du faktiskt använde — max 8.
- `confidence`: "high" om flera källor konsekvent stödjer svaret, "medium" om delvis stöd, "low" om svagt eller motsägelsefullt.
- `gaps`: kort beskrivning av vad du INTE kunde svara på (om något).
- Hitta INTE på källor — bara id:n du faktiskt sett i tool-resultat.

Sökresultat komprimeras automatiskt till en rad per träff. Anropa `lookup_source([...])` (max 5 id per anrop) bara när du behöver underliggande text för att verifiera ett påstående.
"""


class ChatService:
    """
    Handles retrieval-augmented replies by letting the LLM pick tools dynamically.

    The smart model orchestrates: it decides which tools to call and synthesizes
    the final answer. The fast model is used to compress long tool results before
    they are fed back to the smart model, keeping its context window lean.
    """

    def __init__(self) -> None:
        # Serialises shadow-communicator threads so each sees the fully-updated
        # sent_insights list before deciding what to share. Prevents duplicate
        # insights when multiple tool results arrive in quick succession.
        self._shadow_lock = threading.Lock()
        llm_url = os.getenv("LLM_DIRECT_URL")
        self.smart_llm = LLM(
            model=SMART_MODEL,
            system_message=ORCHESTRATOR_SYSTEM,
            temperature=0.2,
            base_url=llm_url,
        )
        self.fast_llm = LLM(
            model=FAST_MODEL,
            system_message=WORKER_SYSTEM,
            temperature=0.05,
            base_url=llm_url,
        )
        self.communicator_llm = LLM(
            model=FAST_MODEL,
            system_message=ORCHESTRATOR_SYSTEM,
            temperature=0.3,
            base_url=llm_url,
        )
        # Editor reuses the smart model by default. Callers that pass a
        # provider_override with editor_model set will get a per-request LLM.
        self.editor_llm = LLM(
            model=SMART_MODEL,
            system_message=EDITOR_SYSTEM,
            temperature=0.1,
            base_url=llm_url,
        )
        # Language-polish fallback chain: Gemini Flash → Berget → vLLM.
        # Built at startup; _language_pass iterates until one model returns a valid response.
        _gemini_key = os.getenv("GOOGLE_GEMINI_KEY")
        _berget_key = os.getenv("BERGET_API_KEY")
        _berget_url = os.getenv("BERGET_BASE_URL", "https://api.berget.ai/v1")
        _berget_model = os.getenv("BERGET_MODEL") or SMART_MODEL
        self.language_llm_chain: list = []
        if _gemini_key:
            self.language_llm_chain.append(LLM(
                model="gemini-3-flash-preview",
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key=_gemini_key,
                system_message=LANGUAGE_CHECKER_SYSTEM,
                temperature=0.1,
            ))
        if _berget_key:
            self.language_llm_chain.append(LLM(
                model=_berget_model,
                base_url=_berget_url,
                api_key=_berget_key,
                system_message=LANGUAGE_CHECKER_SYSTEM,
                temperature=0.1,
            ))
        self.language_llm_chain.append(LLM(
            model=SMART_MODEL,
            system_message=LANGUAGE_CHECKER_SYSTEM,
            temperature=0.1,
            base_url=llm_url,
        ))
        # Main orchestrator never calls share_insight — the shadow communicator does.
        self.tools = get_tools(exclude_tools=["sql_query", "share_insight"])
        # Shadow communicator gets only share_insight as its available tool so it
        # can decide IF and HOW to call it (plain insight, search_card, stats_card).
        _all_tools = get_tools(exclude_tools=["sql_query"])
        self.communicator_tools = [
            t
            for t in _all_tools
            if isinstance(t, dict)
            and t.get("function", {}).get("name") == "share_insight"
        ]
        self.max_tool_iterations = 20

    def _build_llm_instances(self, provider_override=None):
        """Return (smart_llm, fast_llm, communicator_llm, editor_llm, language_llm_chain, supports_thinking).

        When provider_override is None the singleton instances on self are reused.
        When an override is present, new per-request instances are created using the
        user-supplied API key so no key ever bleeds between sessions.
        The editor model defaults to the smart model when not explicitly chosen.
        language_llm_chain always uses the server-side keys regardless of provider override.
        """
        if provider_override is None:
            return (
                self.smart_llm,
                self.fast_llm,
                self.communicator_llm,
                self.editor_llm,
                self.language_llm_chain,
                True,
            )

        from backend.services.provider_registry import get_provider
        provider = get_provider(provider_override.provider_id)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_override.provider_id!r}")

        key = provider_override.api_key
        # User-chosen models take precedence over providers.yaml defaults.
        smart_model = provider_override.smart_model or provider.smart_model
        fast_model = provider_override.fast_model or provider.fast_model or smart_model
        editor_model = (
            getattr(provider_override, "editor_model", "") or smart_model
        )
        smart = LLM(
            model=smart_model,
            base_url=provider.base_url,
            api_key=key,
            system_message=ORCHESTRATOR_SYSTEM,
            temperature=0.2,
        )
        fast = LLM(
            model=fast_model,
            base_url=provider.base_url,
            api_key=key,
            system_message=WORKER_SYSTEM,
            temperature=0.05,
        )
        communicator = LLM(
            model=smart_model,
            base_url=provider.base_url,
            api_key=key,
            system_message=ORCHESTRATOR_SYSTEM,
            temperature=0.3,
        )
        editor = LLM(
            model=editor_model,
            base_url=provider.base_url,
            api_key=key,
            system_message=EDITOR_SYSTEM,
            temperature=0.1,
        )
        return smart, fast, communicator, editor, self.language_llm_chain, provider.supports_thinking

    def stream_chat_response(
        self,
        messages: Sequence[ChatMessage],
        top_k: int = 30,
        focus_ids: Optional[Sequence[str]] = None,
        provider_override=None,
        use_editor: bool = False,
        quick: bool = False,
        session_id: Optional[str] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Run get_chat_response in a background thread and yield SSE-compatible
        event dicts as the tool loop progresses.

        Yields dicts with a "type" key:
          {"type": "tool_call", "tool": "<name>"}   – a tool is about to run
          {"type": "status",    "message": "<text>"} – generic progress note
          {"type": "answer",    "answer": "...", "sources": [...], ...} – final answer
          {"type": "error",     "message": "<text>"} – unhandled exception
        """
        event_queue: queue.Queue[Dict[str, Any]] = queue.Queue()

        def emit(event: Dict[str, Any]) -> None:
            if event.get("_eval_only"):
                return  # eval-log-only events must never reach the SSE stream
            event_queue.put(event)

        def run() -> None:
            try:
                result = self.get_chat_response(
                    messages, top_k=top_k, focus_ids=focus_ids, event_callback=emit,
                    provider_override=provider_override, use_editor=use_editor,
                    quick=quick, session_id=session_id,
                )
                event_queue.put({"type": "answer", **result})
            except Exception as exc:
                import traceback

                traceback.print_exc()
                event_queue.put({"type": "error", "message": str(exc)})

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        while True:
            event = event_queue.get()
            yield event
            if event.get("type") in ("answer", "error"):
                break

        thread.join(timeout=5)

    def get_chat_response(
        self,
        messages: Sequence[ChatMessage],
        top_k: int = 30,
        focus_ids: Optional[Sequence[str]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        provider_override=None,
        use_editor: bool = False,
        quick: bool = False,
        session_id: Optional[str] = None,
    ) -> ChatResponse:
        """
        Public entry point. If the first user message starts with "TEST ",
        the prefix is stripped before the LLM sees it and the whole turn
        (messages, events, tool calls/results, final answer, timings, errors)
        is recorded to the Postgres eval_conversations table. Normal
        conversations are never stored.
        """
        messages, is_eval = detect_and_strip_test_prefix(list(messages))
        if not is_eval:
            return self._get_chat_response_impl(
                messages, top_k=top_k, focus_ids=focus_ids,
                event_callback=event_callback, provider_override=provider_override,
                use_editor=use_editor, quick=quick,
            )
        recorder = ConversationRecorder(
            session_id=session_id,
            messages=messages,
            request_meta={
                "top_k": top_k,
                "focus_ids": list(focus_ids or []),
                "use_editor": use_editor,
                "quick": quick,
                "provider": sanitize_provider(provider_override)
                or {"provider_id": "default"},
            },
            stream=event_callback is not None,
        )
        try:
            result = self._get_chat_response_impl(
                messages, top_k=top_k, focus_ids=focus_ids,
                event_callback=recorder.wrap(event_callback),
                provider_override=provider_override, use_editor=use_editor,
                quick=quick,
            )
        except Exception as exc:
            recorder.finish(error=exc)
            raise
        recorder.finish(result=result)
        return result

    def _get_chat_response_impl(
        self,
        messages: Sequence[ChatMessage],
        top_k: int = 30,
        focus_ids: Optional[Sequence[str]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        provider_override=None,
        use_editor: bool = False,
        quick: bool = False,
    ) -> ChatResponse:
        """
        Generate a reply while allowing the assistant to call registered tools.

        Args:
            messages: Ordered chat history including the latest user prompt.
            top_k: Maximum number of unique sources to expose to the client.
            focus_ids: Optional list of document ids shared with the user in earlier turns.
        Returns:
            Dict containing the assistant answer and harvested sources.
        """
        (
            smart_llm,
            fast_llm,
            communicator_llm,
            editor_llm,
            language_llm_chain,
            supports_thinking,
        ) = self._build_llm_instances(provider_override)
        print_yellow(f"Messages in chat:")
        for msg in messages:
            print_yellow(msg)
        full_messages = [
            {"role": "system", "content": smart_llm.system_message}
        ] + list(messages)

        question = self._latest_user_message(messages)
        ids_part = None
        if "INTRESSENT_IDS" in question:
            question_parts = question.split("INTRESSENT_IDS")
            question = question_parts[0].strip()
            ids_part = question_parts[1].strip()
        question = f"""A user has asked:
            *{question}*\n
            Make sure to understand the question and plan your research accordingly.
            If it is in Swedish, make sure to understand it correctly.
            If you need to clarify the question, ask the user to clarify."""
        if ids_part:
            question += f"""\nAs the user is interested in a certain person or persons, you can use the following list of intressent_id:s to find relevant speeches:\n{ids_part}."""
        if not question:
            raise ValueError("Conversation must contain at least one user message.")
        print_yellow(
            f"[ChatService] Generating answer for {len(messages)} messages (top_k={top_k})."
        )
        collected_sources: List[ChatSource] = []
        collected_tables: List[Dict[str, Any]] = []
        collected_persons: Dict[str, Dict] = {}
        registry = ProvenanceRegistry()
        # Shared dedup list — passed through the researcher and into the
        # orchestrator's tool loop so the shadow communicator does not repeat
        # the same insight across both phases.
        sent_insights: List[str] = []

        # --- Optional research pre-pass ----------------------------------------
        # Plan the question into sub-questions. If 2+ are produced, dispatch a
        # Researcher first; its compact ResearchReport is injected into the
        # orchestrator's history so the orchestrator never sees raw tool bodies.
        # If planning fails or returns ≤1 sub-question, fall through to the
        # orchestrator's normal tool loop.
        # quick=True bypasses planning + Researcher entirely — single-shot
        # orchestrator loop for users who want a fast answer.
        if quick:
            print_blue("[ChatService] quick=True — skipping planner/researcher.")
            log_event("research_skipped_quick")
            plan = None
        else:
            plan = self._plan_research(question, smart_llm)
        if plan and len(plan.sub_questions) >= 2:
            print_purple(
                f"[ChatService] Planner produced {len(plan.sub_questions)} sub-question(s); "
                "dispatching Researcher."
            )
            log_event(
                "research_dispatch",
                num_sub_questions=len(plan.sub_questions),
            )
            try:
                report = self._run_researcher(
                    plan,
                    registry,
                    collected_sources,
                    collected_persons,
                    smart_llm=smart_llm,
                    fast_llm=fast_llm,
                    communicator_llm=communicator_llm,
                    sent_insights=sent_insights,
                    event_callback=event_callback,
                )
                report_text = self._format_research_report(report, plan)
                full_messages.append({"role": "user", "content": report_text})
            except Exception as exc:
                print_red(f"[Researcher] failed; falling back to direct loop: {exc}")
                log_error("researcher_run_failure", exc)
        elif plan:
            print_blue(
                f"[ChatService] Planner returned {len(plan.sub_questions)} sub-question(s); "
                "running orchestrator directly."
            )

        response_message, tables, updated_focus_ids = self._run_tool_loop(
            full_messages,
            collected_sources,
            collected_tables,
            collected_persons,
            list(focus_ids or []),
            user_question=question,
            event_callback=event_callback,
            registry=registry,
            smart_llm=smart_llm,
            fast_llm=fast_llm,
            communicator_llm=communicator_llm,
            supports_thinking=supports_thinking,
            sent_insights=sent_insights,
        )
        answer_text = (
            response_message.final_answer
            if isinstance(response_message, FinalAnswer)
            else str(response_message)
        ).strip()

        # Full-answer editor pass has been removed — it over-edited.
        # Attribution fixes are now done per-paragraph via _fix_with_fact_check_feedback().

        # --- Provenance-based citation parsing and renumbering ---
        validated_answer, cited_sources, unique_cited_ids, invalid_ids = (
            parse_and_renumber_citations(answer_text, registry)
        )
        if invalid_ids:
            print_yellow(f"[Provenance] Dropped invalid citation IDs: {invalid_ids}")
            log_event("dropped_citations", count=len(invalid_ids))
        fallback_used = not unique_cited_ids and registry.size() > 0
        print_green(
            f"[Provenance] registered: {registry.size()} sources | "
            f"cited: {len(unique_cited_ids)} | "
            f"invalid dropped: {len(invalid_ids)} | "
            f"fallback: {'yes' if fallback_used else 'no'}"
        )

        # Person link injection — use registry persons merged with collected_persons.
        # Only wrap names whose paragraph has a citation supporting them (via
        # backend.services.attribution.paragraph_supports_name).
        all_persons = {**collected_persons, **registry.get_persons()}
        unique_persons = self._get_unique_name_persons(all_persons)
        persons, validated_answer = self._inject_person_links(
            validated_answer, unique_persons, cited_sources
        )

        # Attribution detector: scan for Name (PARTY) tokens that no cited source
        # in the paragraph supports. Signal only — no answer mutation. Feeds the
        # editor pass when enabled and surfaces as a warning field on the response.
        from backend.services.attribution import detect_attribution_warnings
        attribution_warnings = detect_attribution_warnings(validated_answer, cited_sources)
        if attribution_warnings:
            log_event(
                "attribution_mismatch_detected",
                count=len(attribution_warnings),
                reasons=list({w["reason"] for w in attribution_warnings}),
            )
            print_yellow(
                f"[Attribution] {len(attribution_warnings)} warning(s): "
                + ", ".join(f"{w['name']}({w['party']})" for w in attribution_warnings[:5])
            )
            if use_editor:
                try:
                    validated_answer = self._fix_with_fact_check_feedback(
                        answer_body=validated_answer,
                        warnings=attribution_warnings,
                        cited_sources=cited_sources,
                        editor_llm=editor_llm,
                        smart_llm=smart_llm,
                        event_callback=event_callback,
                    )
                    attribution_warnings = detect_attribution_warnings(validated_answer, cited_sources)
                except Exception as exc:
                    print_red(f"[Attribution fix] pass failed: {exc}")
                    log_error("attribution_fix_failure", exc)

        # --- Language pass (always runs when language_llm_chain is available) ---
        # Split off the "Källor" section first — it's a list of citation lines,
        # not prose, and an LLM asked to "improve flow" on it tends to collapse
        # the blank lines between entries onto a single line. Only the answer
        # body goes through the polish pass; the tail is reattached unchanged.
        if language_llm_chain and validated_answer:
            body_part, kallor_sep, kallor_tail = validated_answer.partition("\n\n### Källor\n\n")
            try:
                body_part = self._language_pass(
                    text=body_part,
                    language_llm_chain=language_llm_chain,
                    event_callback=event_callback,
                )
            except Exception as exc:
                print_red(f"[Language pass] failed, keeping answer: {exc}")
                log_error("language_pass_failure", exc)
            validated_answer = body_part + kallor_sep + kallor_tail

        print_green(
            f"[ChatService] Completed answer with {len(cited_sources)} cited sources, "
            f"{len(persons)} person links, {len(attribution_warnings)} attribution warnings."
        )
        return {
            "answer": validated_answer,
            "sources": cited_sources,
            "persons": persons,
            "tables": tables,
            "focus_ids": updated_focus_ids,
            "attribution_warnings": attribution_warnings,
        }

    def _plan_research(
        self,
        user_question: str,
        smart_llm,
    ) -> Optional[ResearchRequest]:
        """Break the user's question into 1-3 sub-questions for research.

        Returns None on planner failure — caller should fall back to running the
        orchestrator's tool loop directly.
        """
        system = PLANNER_SYSTEM.format(max_sub=RESEARCH_MAX_SUBQUESTIONS)
        prompt = (
            f"Användarens fråga:\n{user_question}\n\n"
            "Bryt ner i delfrågor enligt schemat ResearchRequest. "
            f"Sätt user_message till användarens fråga ovan. Max {RESEARCH_MAX_SUBQUESTIONS} delfrågor."
        )
        try:
            response = smart_llm.generate(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                format=ResearchRequest,
                think=False,
            )
        except Exception as exc:
            print_red(f"[Planner] failed: {exc}")
            log_error("research_planner_failure", exc)
            return None
        if isinstance(response, str):
            print_red(f"[Planner] returned error string: {response}")
            log_event("research_planner_error_string")
            return None
        parsed = getattr(response, "parsed", None) or getattr(response, "content", None)
        if isinstance(parsed, ResearchRequest):
            # Hard cap: never let the planner return more than the limit.
            if len(parsed.sub_questions) > RESEARCH_MAX_SUBQUESTIONS:
                parsed.sub_questions = parsed.sub_questions[:RESEARCH_MAX_SUBQUESTIONS]
            return parsed
        print_red(f"[Planner] unexpected response shape: {type(parsed)}")
        return None

    def _investigate_subquestion(
        self,
        sub_q: SubQuestion,
        request: ResearchRequest,
        registry: ProvenanceRegistry,
        collected_sources: List[ChatSource],
        collected_persons: Dict[str, Dict],
        smart_llm,
        fast_llm,
        communicator_llm=None,
        sent_insights: Optional[List[str]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> SubFinding:
        """Run a small tool loop for one sub-question. Returns a structured SubFinding.

        Reuses the same tools, registry, and stub-building as the main orchestrator.
        Skips share_insight, focus_ids, and citation retries — the orchestrator will
        validate citations on the final answer.
        """
        hints_block = ""
        if sub_q.hints:
            hints_block = "\nFokus-tips: " + ", ".join(sub_q.hints) + "."
        user_msg = (
            f"Övergripande användarfråga: {request.user_message}\n\n"
            f"DIN delfråga ({sub_q.id}): {sub_q.question}"
            f"{hints_block}\n\n"
            "Sök i databasen tills du har tillräckligt, sedan returnera SubFinding."
        )
        messages: List[ChatMessage] = [
            {"role": "system", "content": RESEARCHER_SYSTEM},
            {"role": "user", "content": user_msg},
        ]
        # Identical repeats are blocked and answered from cache (see _run_tool_loop).
        _executed_calls: Dict[Tuple[str, str], str] = {}

        for i in range(RESEARCH_ITERATIONS_PER_SUBQ):
            # Compact old tool messages if history gets large.
            self._compact_old_tool_messages(messages, HISTORY_CHAR_BUDGET)

            gen_kwargs = {"messages": messages, "think": False, "auto_execute_tools": False}
            if getattr(self, "tools", None):
                gen_kwargs["tools"] = self.tools
            response = smart_llm.generate(**gen_kwargs)
            if isinstance(response, str):
                print_red(f"[Researcher/{sub_q.id}] LLM error: {response}")
                log_error(
                    "researcher_api_failure",
                    RuntimeError(response),
                    sub_question_id=sub_q.id,
                )
                break

            tool_calls = getattr(response, "tool_calls", None)
            if not tool_calls:
                # Model decided it's done — break out and force structured output.
                if response.content:
                    messages.append({"role": "assistant", "content": response.content})
                break

            # Append assistant turn with tool_calls (OpenAI spec).
            def _build_tc_dict(tc):
                d = {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": (
                            json.dumps(tc.function.arguments)
                            if isinstance(tc.function.arguments, dict)
                            else tc.function.arguments
                        ),
                    },
                }
                extra = getattr(tc, "extra_content", None) or (
                    getattr(tc, "model_extra", None) or {}
                ).get("extra_content")
                if extra:
                    d["extra_content"] = extra
                return d

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [_build_tc_dict(tc) for tc in tool_calls],
                }
            )

            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                tool_args = tool_call.function.arguments
                if isinstance(tool_args, str):
                    try:
                        tool_args = json.loads(tool_args)
                    except json.JSONDecodeError:
                        tool_args = {}
                # Researcher does not handle focus_ids — drop if model passed one.
                if isinstance(tool_args, dict):
                    tool_args.pop("focus_ids", None)

                _call_key = (tool_name, json.dumps(tool_args, sort_keys=True, default=str))
                if _call_key in _executed_calls:
                    print_yellow(
                        f"[Researcher/{sub_q.id}] Blocked repeated identical call: {tool_name}"
                    )
                    log_event(
                        "repeated_tool_call_blocked",
                        model=getattr(smart_llm, "model", None),
                        tool=tool_name,
                        args=tool_args,
                        sub_question_id=sub_q.id,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": (
                                "You already made this exact call. Cached result:\n"
                                + _executed_calls[_call_key][:1500]
                                + "\n\nDo not repeat identical calls. "
                                + _dedup_hint(tool_name)
                            ),
                        }
                    )
                    continue

                if event_callback:
                    event_callback({"type": "tool_call", "tool": tool_name})
                print_blue(
                    f"[Researcher/{sub_q.id}] {tool_name} args={tool_args}"
                )

                tool_func = self._get_tool_function(tool_name)
                tool_result_string: str
                if tool_func is None:
                    tool_result_string = f"ERROR: Tool '{tool_name}' not found."
                else:
                    _tool_structured_result.set(None)
                    try:
                        tool_result = tool_func(**tool_args)
                    except Exception as e:
                        print_red(f"[Researcher/{sub_q.id}] tool exception: {e}")
                        log_error(
                            "researcher_tool_exception", e,
                            tool=tool_name, sub_question_id=sub_q.id,
                        )
                        tool_result = f"ERROR: {e}"

                    structured = _tool_structured_result.get()
                    if structured is not None and isinstance(structured, SearchHitsResult):
                        self._register_hits_in_registry(
                            structured.response, registry, tool_name
                        )
                        self._collect_sources_from_hits_response(
                            structured.response, collected_sources, collected_persons
                        )
                        tool_result_string = self._build_eviction_stub(
                            structured.response, tool_name
                        )
                    elif structured is not None and isinstance(structured, HitsResponse):
                        self._register_hits_in_registry(
                            structured, registry, tool_name
                        )
                        self._collect_sources_from_hits_response(
                            structured, collected_sources, collected_persons
                        )
                        if tool_name == "read_documents_for":
                            # The return value IS the focused answer — keep it.
                            tool_result_string = (
                                tool_result if isinstance(tool_result, str) else str(tool_result)
                            )
                        else:
                            tool_result_string = self._build_eviction_stub(
                                structured, tool_name
                            )
                    elif (
                        tool_name == "fetch_debate"
                        and structured is not None
                        and isinstance(structured, HitsResponse)
                    ):
                        # already handled above
                        tool_result_string = self._build_eviction_stub(structured, tool_name)
                    else:
                        tool_result_string = (
                            tool_result if isinstance(tool_result, str) else str(tool_result)
                        )

                if len(tool_result_string) > 12000:
                    tool_result_string = tool_result_string[:12000] + " (...)[truncated]"
                _executed_calls[_call_key] = tool_result_string

                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": tool_result_string,
                }
                messages.append(tool_message)

                # Shadow communicator: keep the user informed during long
                # research sessions. Fires only for data tools that returned
                # something useful, mirroring the orchestrator-loop behaviour.
                _DATA_TOOLS = {
                    "arango_search",
                    "vector_search",
                    "vector_search_debates",
                    "fetch_debate",
                    "database_query",
                    "fetch_documents",
                    "read_documents_for",
                    "search_motions",
                    "vector_search_motions",
                    "fetch_motion",
                }
                result_is_useful = (
                    tool_name in _DATA_TOOLS
                    and "ERROR" not in tool_result_string
                    and tool_result_string.strip() not in ("", "...")
                )
                if (
                    result_is_useful
                    and event_callback
                    and communicator_llm is not None
                    and sent_insights is not None
                ):
                    shadow_msgs = list(messages)
                    cb = event_callback
                    threading.Thread(
                        target=self._shadow_communicate,
                        args=(
                            shadow_msgs,
                            cb,
                            sent_insights,
                            dict(collected_persons),
                            {s["_id"] for s in collected_sources},
                            communicator_llm,
                        ),
                        daemon=True,
                    ).start()

        # Finalisation: ask the model to emit a structured SubFinding.
        # Re-prompt budget: one retry if the first attempt is malformed or
        # mismatches the sub_question_id.
        finalise_prompt = (
            f"Du har samlat tillräckligt material för delfråga {sub_q.id}. "
            f"Returnera nu en SubFinding (Pydantic) där sub_question_id='{sub_q.id}'. "
            "answer = 1–3 meningar på svenska, source_ids = de tal-id:n du faktiskt använde "
            "(max 8), confidence = high/medium/low, gaps = kort beskrivning av vad du saknar."
        )
        attempts = 0
        finding: Optional[SubFinding] = None
        while attempts < 2 and finding is None:
            attempts += 1
            try:
                finalise_response = smart_llm.generate(
                    messages=messages + [{"role": "user", "content": finalise_prompt}],
                    format=SubFinding,
                    think=False,
                )
            except Exception as exc:
                print_red(f"[Researcher/{sub_q.id}] finalise failed: {exc}")
                log_error(
                    "researcher_finalise_failure", exc, sub_question_id=sub_q.id,
                )
                break
            if isinstance(finalise_response, str):
                print_red(f"[Researcher/{sub_q.id}] finalise error string: {finalise_response}")
                break
            parsed = getattr(finalise_response, "parsed", None) or getattr(
                finalise_response, "content", None
            )
            if isinstance(parsed, SubFinding):
                if parsed.sub_question_id != sub_q.id:
                    # Force-correct the id mismatch on first attempt; on the
                    # second, accept it (better partial than nothing).
                    if attempts == 1:
                        finalise_prompt += (
                            f"\n\nObs: sub_question_id måste vara EXAKT '{sub_q.id}'."
                        )
                        continue
                    parsed.sub_question_id = sub_q.id
                # Filter source_ids to those actually in the registry.
                clean_ids = [
                    sid for sid in parsed.source_ids
                    if registry.get(normalize_talk_id(sid) or sid)
                ]
                parsed.source_ids = clean_ids[:8]
                finding = parsed

        if finding is None:
            finding = SubFinding(
                sub_question_id=sub_q.id,
                answer="(Forskaren kunde inte returnera ett strukturerat svar.)",
                source_ids=[],
                confidence="low",
                gaps="Forskaren misslyckades med strukturerad output.",
            )
            log_event("researcher_finalise_fallback", sub_question_id=sub_q.id)
        return finding

    def _run_researcher(
        self,
        request: ResearchRequest,
        registry: ProvenanceRegistry,
        collected_sources: List[ChatSource],
        collected_persons: Dict[str, Dict],
        smart_llm,
        fast_llm,
        communicator_llm=None,
        sent_insights: Optional[List[str]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> ResearchReport:
        """Run the researcher across all sub-questions and assemble a ResearchReport."""
        if registry is not None:
            _provenance_registry.set(registry)
        # Reader sub-agent (read_documents_for) uses the request's fast model.
        _fast_llm_var.set(fast_llm)
        if event_callback:
            event_callback(
                {
                    "type": "status",
                    "message": f"Forskar i {len(request.sub_questions)} delfråga(or)…",
                }
            )
        if sent_insights is None:
            sent_insights = []
        findings: List[SubFinding] = []
        for idx, sub_q in enumerate(request.sub_questions, start=1):
            print_purple(
                f"[Researcher] investigating {sub_q.id}: {sub_q.question[:80]}…"
            )
            if event_callback:
                event_callback(
                    {
                        "type": "status",
                        "message": (
                            f"Delfråga {idx}/{len(request.sub_questions)}: {sub_q.question}"
                        ),
                    }
                )
            f = self._investigate_subquestion(
                sub_q,
                request,
                registry,
                collected_sources,
                collected_persons,
                smart_llm,
                fast_llm,
                communicator_llm=communicator_llm,
                sent_insights=sent_insights,
                event_callback=event_callback,
            )
            findings.append(f)
        return ResearchReport(findings=findings, dead_ends=[], overall_notes="")

    @staticmethod
    def _format_research_report(report: ResearchReport, request: ResearchRequest) -> str:
        """Compact prose-form of a ResearchReport, ready to inject into orchestrator history."""
        if not report.findings:
            return "Forskningsrundan returnerade inga resultat."
        # Map sub_question_id -> SubQuestion text for context.
        q_map = {q.id: q.question for q in request.sub_questions}
        lines = [
            "Forskningsrunda klar. En specialiserad forskare har undersökt följande delfrågor:",
            "",
        ]
        for f in report.findings:
            q_text = q_map.get(f.sub_question_id, "(okänd delfråga)")
            ids_str = ", ".join(f"[src:{sid}]" for sid in f.source_ids) or "(inga källor)"
            lines.append(f"**{f.sub_question_id} — {q_text}**")
            lines.append(f"Svar (confidence={f.confidence}): {f.answer}")
            lines.append(f"Källor: {ids_str}")
            if f.gaps:
                lines.append(f"Luckor: {f.gaps}")
            lines.append("")
        if report.overall_notes:
            lines.append(f"Övriga noter: {report.overall_notes}")
        lines.append(
            "Använd dessa fynd som grund. Anropa fler verktyg om du behöver komplettera, "
            "och anropa lookup_source([src:ID]) för att hämta ordagrann text när du citerar."
        )
        return "\n".join(lines)

    def _summarize_tool_result(
        self, tool_name: str, tool_result_string: str, user_question: str, fast_llm=None
    ) -> str:
        """
        Use the fast model to compress a long tool result into a concise summary.

        Called when a tool returns more than SUMMARIZE_THRESHOLD characters, so the
        smart orchestrator never has to wade through massive raw outputs.
        The input is capped at 40 000 chars before being sent to the fast model.
        """
        # Cap what we feed to the fast model — 40k chars is already a lot of context
        MAX_INPUT = 40000
        truncated = len(tool_result_string) > MAX_INPUT
        input_text = tool_result_string[:MAX_INPUT]
        truncation_note = (
            f"\n[...input truncated at {MAX_INPUT} chars — original was {len(tool_result_string)} chars]"
            if truncated
            else ""
        )

        prompt = (
            f"The tool '{tool_name}' returned the following result. "
            f"The user's question is: '{user_question}'\n\n"
            f"RAW RESULT:\n{input_text}{truncation_note}\n\n"
            "Write a concise summary of the key findings relevant to the question. "
            "Include specific names, dates, quotes, and numbers. Be brief but complete.\n\n"
            "IMPORTANT: The raw result contains citation tags of the form "
            "[src:ID | Speaker (Party) | date] (e.g. [src:GY0992-90 | Peter Rådberg (MP) | 2009-11-12]) "
            "placed before or inside each document. You MUST copy these tags VERBATIM into your "
            "summary immediately after the fact or quote they support — keep the speaker and date "
            "inside the brackets, do not drop, rename, or restructure them. "
            "Example: 'Peter Rådberg (MP) criticized the fleet reduction[src:GY0992-90 | Peter Rådberg (MP) | 2009-11-12].'"
        )
        _fast = fast_llm or self.fast_llm
        response = _fast.generate(
            messages=[
                {"role": "system", "content": WORKER_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            think=False,  # Summarization is mechanical — no reasoning chain needed
        )
        # _llm.generate swallows API exceptions and returns a plain string on failure.
        # Detect that and fall back to a truncated excerpt of the original result so
        # the smart model still has useful content rather than an error message.
        if isinstance(response, str):
            print_red(f"[Fast] Summarization failed: {response}. Falling back to truncated excerpt.")
            FALLBACK_CHARS = 8000
            excerpt = tool_result_string[:FALLBACK_CHARS]
            tail = f"\n[...truncated at {FALLBACK_CHARS} chars — original was {len(tool_result_string)} chars]"
            return f"[Partial {tool_name} result (fast-model unavailable)]\n{excerpt}{tail}"
        summary = getattr(response, "content", str(response))
        if not isinstance(summary, str):
            summary = str(summary)
        print_blue(
            f"[Fast] Summarized {tool_name} result: {len(tool_result_string)} → {len(summary)} chars"
        )
        return f"[Summary of {tool_name} result — full result was {len(tool_result_string)} chars]\n{summary}"

    def _collect_sources_from_hits_response(
        self,
        hits_response: HitsResponse,
        collected_sources: List[ChatSource],
        collected_persons: Dict[str, Dict],
    ) -> List[str]:
        """Collect sources and persons from a HitsResponse. Returns list of intressent_ids.

        Debate-level hits (from `vector_search_debates`) are skipped — they are
        navigation aids, not citable sources.
        """
        intressent_ids: List[str] = []
        for hit in hits_response.hits:
            meta = hit.metadata or {}
            if meta.get("kind") == "debate" or (hit.id or "").startswith("debates/"):
                continue
            iid = meta.get("intressent_id")
            collected_sources.append(
                {
                    "_id": hit.id or "",
                    "chunk_index": meta.get("chunk_index", -1),
                    "heading": meta.get("titel"),
                    "debateurl": meta.get("debateurl"),
                    "snippet": hit.snippet or "",
                    "score": hit.score or 0.0,
                    "speaker": hit.speaker,
                    "party": hit.party,
                    "intressent_id": iid,
                    "date": hit.date,
                }
            )
            if iid and hit.speaker and iid not in collected_persons:
                collected_persons[iid] = {"name": hit.speaker, "party": hit.party or ""}
            if iid:
                intressent_ids.append(iid)
        return intressent_ids

    @staticmethod
    def _compact_old_tool_messages(messages: List[ChatMessage], budget: int) -> int:
        """If the running history exceeds `budget` chars, replace older tool
        message contents with a tiny placeholder. Preserves tool_call_id linkage
        so the OpenAI tool-call spec stays valid; the orchestrator can still
        recall the underlying sources via lookup_source.

        Returns the number of tool messages compacted.
        """
        def total_chars() -> int:
            return sum(len(m.get("content") or "") for m in messages)

        if total_chars() <= budget:
            return 0

        # Walk from the oldest tool message forward, leave the last few intact.
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(tool_indices) <= 3:
            return 0
        compacted = 0
        for idx in tool_indices[:-3]:  # keep last 3 tool messages intact
            msg = messages[idx]
            content = msg.get("content") or ""
            if content.startswith("[compacted:"):
                continue
            tool_name = msg.get("name") or "tool"
            messages[idx] = {
                **msg,
                "content": (
                    f"[compacted: earlier {tool_name} result evicted to save context. "
                    "Sources stay in the registry; call lookup_source([src:...]) if you need them.]"
                ),
            }
            compacted += 1
            if total_chars() <= budget:
                break
        return compacted

    @staticmethod
    def _build_eviction_stub(hits_response: HitsResponse, tool_name: str) -> str:
        """Compact one-line-per-hit summary used in place of the full tool body.

        Once hits are registered in the provenance registry, the orchestrator
        only needs source IDs + a short headline to know what was found. The
        full text remains available via `lookup_source([src:ID])`.
        """
        lines: List[str] = []
        for hit in hits_response.hits:
            meta = hit.metadata or {}
            if meta.get("kind") == "debate" or (hit.id or "").startswith("debates/"):
                continue
            bare = hit.key or (hit.id.split("/", 1)[1] if hit.id and "/" in hit.id else hit.id)
            if not bare:
                continue
            speaker = hit.speaker or "Okänd"
            party = f" ({hit.party})" if hit.party else ""
            date = hit.date or ""
            heading = (meta.get("titel") or "").strip()
            if len(heading) > 60:
                heading = heading[:60].rstrip() + "…"
            preview = (hit.snippet or "").replace("\n", " ").strip()[:90]
            if len(hit.snippet or "") > 90:
                preview = preview.rstrip() + "…"
            parts = [f"[src:{bare}] {speaker}{party} {date}".rstrip()]
            if heading:
                parts.append(heading)
            if preview:
                parts.append(preview)
            lines.append("  - " + " — ".join(parts))
        if not lines:
            return f"{tool_name} returned no citable hits."
        header = (
            f"{tool_name} returned {len(lines)} hit(s); registered as sources. "
            "Cite with [src:ID] verbatim from the list. "
            "Call lookup_source([src:ID,...]) only if you need the underlying text to quote or verify."
        )
        return header + "\n" + "\n".join(lines)

    @staticmethod
    def _register_hits_in_registry(
        hits_response: HitsResponse,
        registry: ProvenanceRegistry,
        tool_name: str = "unknown",
    ) -> None:
        """Register all hits from a HitsResponse into the provenance registry.

        Skips debate-level hits: `vector_search_debates` emits bare ids like
        "2021-06-17:42" with metadata["kind"] == "debate". Debates are a
        navigation tool and are not citable on their own — talks inside a
        debate become citable via `fetch_debate`.
        """
        for hit in hits_response.hits:
            meta = hit.metadata or {}
            if meta.get("kind") == "debate" or (hit.id or "").startswith("debates/"):
                continue
            talk_id = normalize_talk_id(hit.id) or hit.key
            if not talk_id:
                continue
            # Body = grounding text the LLM should be able to recall via
            # lookup_source. Prefer full text (fetch_documents), fall back to
            # snippet (vector_search neighbours, summary, etc.). The registry
            # caps it at BODY_CAP_CHARS.
            body_text = hit.text or hit.snippet or ""
            registry.register(
                SourceRecord(
                    source_id=talk_id,
                    tool=tool_name,
                    speaker=hit.speaker,
                    party=hit.party,
                    date=hit.date,
                    heading=meta.get("titel"),
                    debateurl=meta.get("debateurl"),
                    snippet=hit.snippet or hit.text or "",
                    intressent_id=meta.get("intressent_id"),
                    score=hit.score or 0.0,
                    body=body_text,
                )
            )

    def _summarize_hits_response(
        self,
        hits_response: HitsResponse,
        user_question: str,
        fast_llm=None,
    ) -> HitsResponse:
        """
        Per-document summarisation using a growing conversation so vLLM prefix
        caching can reuse the shared system + question prefix across all docs.
        Only documents whose text exceeds DOC_SUMMARIZE_THRESHOLD are summarised.
        """
        needs_summary = [
            h for h in hits_response.hits if len(h.text or "") > DOC_SUMMARIZE_THRESHOLD
        ][:DOC_MAX_BATCH]

        if not needs_summary:
            return hits_response

        _fast = fast_llm or self.fast_llm
        conversation: List[Dict[str, Any]] = [
            {"role": "system", "content": WORKER_SYSTEM}
        ]
        summaries: Dict[str, str] = {}

        for hit in needs_summary:
            doc_prompt = (
                f"Talare: {hit.speaker}, Parti: {hit.party}\n\n"
                f"{(hit.text or '')[:DOC_MAX_INPUT]}\n\n"
                f"\n\nAnvändarfråga: {user_question}"
            )
            conversation.append({"role": "user", "content": doc_prompt})
            try:
                response = _fast.generate(messages=conversation, think=False)
                summary_text = getattr(response, "content", str(response))
            except Exception as e:
                summary_text = (hit.text or "")[:500] + "(...)\n\n[Summary failed]"
                print_red(f"[ChatService] Per-doc summary failed for {hit.id}: {e}")
            if not isinstance(summary_text, str):
                summary_text = str(summary_text)
            conversation.append({"role": "assistant", "content": summary_text})
            summaries[hit.id] = summary_text
            print_blue(
                f"[Fast] Summarized doc {hit.id}: {len(hit.text or '')} → {len(summary_text)} chars"
            )

        new_hits = []
        for hit in hits_response.hits:
            if hit.id in summaries:
                hit = hit.model_copy(
                    update={
                        "text": f"[Sammanfattning — hämta hela dokumentet {hit.id} vid behov]\n{summaries[hit.id]}"
                    }
                )
            new_hits.append(hit)
        # Print hits for debugging — be careful with large outputs here!
        print_blue(f"\n[ChatService] Returning {len(new_hits)} hits (summarized {len(summaries)} docs):")
        for i, hit in enumerate(new_hits, 1):
            for k, v in hit.dict().items():
                if isinstance(v, str) and len(v) > 200:
                    print_blue(f"Hit {i} {k}: {v[:200]}... ({len(v)} chars)")
                else:
                    print_blue(f"Hit {i} {k}: {v}")
        print_blue('---\n')
        return HitsResponse(hits=new_hits)

    def _run_tool_loop(
        self,
        messages: Sequence[ChatMessage],
        collected_sources: List[ChatSource],
        collected_tables: List[Dict[str, Any]],
        collected_persons: Dict[str, Dict],
        initial_focus_ids: List[str],
        user_question: str = "",
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        registry: Optional[ProvenanceRegistry] = None,
        smart_llm=None,
        fast_llm=None,
        communicator_llm=None,
        supports_thinking: bool = True,
        sent_insights: Optional[List[str]] = None,
    ) -> Tuple[FinalAnswer, List[Dict[str, Any]], List[str]]:
        """
        Repeatedly call the smart LLM, executing tool calls as needed, until a
        final answer is produced.

        Long tool results are compressed by the fast model before being appended
        to the message history, keeping the smart model's context lean.
        """
        _smart = smart_llm or self.smart_llm
        _fast = fast_llm or self.fast_llm
        _communicator = communicator_llm or self.communicator_llm
        print_purple("[ChatService] Starting tool interaction loop.")
        # Make the registry visible to tools (e.g. lookup_source) via ContextVar.
        if registry is not None:
            _provenance_registry.set(registry)
        # Reader sub-agent (read_documents_for) uses the request's fast model.
        _fast_llm_var.set(_fast)
        current_messages: List[ChatMessage] = list(messages)
        active_focus_ids: List[str] = list(dict.fromkeys(initial_focus_ids))
        # Tracks what the shadow communicator has already told the user this session,
        # so it can avoid outputting the same insight twice. Shared across the
        # researcher pre-pass and the orchestrator loop when caller passes one in.
        if sent_insights is None:
            sent_insights = []
        if active_focus_ids:
            current_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Du har tidigare delat sökresultat med användaren. "
                        "Listan `focus_ids` innehåller deras dokument-id:n:\n"
                        f"{active_focus_ids}\n"
                        "Om du vill begränsa en ny arango_search till samma träffar anger du argumentet "
                        "`focus_ids=focus_ids`."
                    ),
                }
            )

        _last_tool_call: tuple[str, bool, str] | None = None  # (name, result_was_empty, args_json)
        # (tool_name, sorted-args-json) -> result string for every call already
        # executed this turn. Identical repeats are blocked and answered from
        # this cache with a nudge, so a confused model can't burn iterations.
        _executed_calls: Dict[Tuple[str, str], str] = {}
        _DEDUP_EXEMPT = {"share_insight"}
        _citation_retries = 0
        _MAX_CITATION_RETRIES = 2

        for i in range(self.max_tool_iterations):

            if i == self.max_tool_iterations - 1:
                print_red(
                    f"[ChatService] Reached max iterations ({self.max_tool_iterations}). Forcing final answer."
                )
                current_messages.append(
                    {
                        "role": "user",
                        "content": "**IMPORTANT** You have reached the maximum number of tool calls. Please provide your final answer based on the information you have gathered so far.",
                    }
                )

            # Soft history-budget guard: if running context grows past
            # HISTORY_CHAR_BUDGET, compact the oldest tool-result bodies to
            # a placeholder. Sources stay in the registry → lookup_source
            # can recall them if needed.
            compacted = self._compact_old_tool_messages(
                current_messages, HISTORY_CHAR_BUDGET
            )
            if compacted:
                print_yellow(
                    f"[ChatService] History over budget; compacted {compacted} old tool message(s)."
                )

            # Smart model orchestrates: decides which tool to call (or gives final answer).
            # think=True only on the first iteration so the model carefully reads the
            # question and plans; subsequent iterations are routine tool-selection calls
            # that don't need long reasoning chains (and think=True is expensive/slow).
            # supports_thinking is False for non-vLLM providers (Berget, OpenAI).
            think_now = (i == 0) and supports_thinking
            # think_now = True  # Always use think=True to encourage careful reading and planning at every step, even if it adds latency.
            gen_kwargs = {"messages": current_messages, "think": think_now, "auto_execute_tools": False}
            if getattr(self, "tools", None):
                gen_kwargs["tools"] = self.tools
            response: ChatCompletionMessage = _smart.generate(**gen_kwargs)

            if isinstance(response, str):
                # _llm swallows API exceptions and returns a plain string error message.
                # Surface it as a proper exception so the SSE handler can emit an error event.
                _exc = RuntimeError(f"LLM API error: {response}")
                log_error("llm_api_failure", _exc, model=getattr(_smart, "model", None), iteration=i)
                raise _exc

            thinking = getattr(response, "reasoning_content", None)
            if thinking:
                print_blue("Thinking:", thinking)
            try:
                print_purple("[Smart] Content:", response.content)
            except Exception as e:
                print_red(f"[ChatService] Error printing content response: {e}")

            tool_calls = getattr(response, "tool_calls", None)
            if tool_calls:
                # Emit the model's own narration of what it's about to do as a status hint.
                # This is the text content the LLM writes before calling a tool, e.g.
                # "Jag söker nu efter tal från Jan Riise om AI...". Trim to ~200 chars
                # so it fits neatly in the UI without overwhelming it.
                # When think=True the model puts narration in reasoning_content, not content.
                # Fall back to reasoning_content if content is empty.
                narration = getattr(response, "content", None)
                if (
                    not narration
                    or not isinstance(narration, str)
                    or not narration.strip()
                ):
                    narration = getattr(response, "reasoning_content", None)
                if narration and isinstance(narration, str) and narration.strip():
                    short = narration.strip()
                    print_green(f"[SSE] Emitting status: {short[:80]}…")
                    if event_callback:
                        event_callback({"type": "status", "message": short})
                else:
                    print_yellow(f"[SSE] No narration on iteration {i}")

                # Append the assistant turn with tool_calls so the LLM can read its own
                # decisions on the next iteration (OpenAI spec requires this).
                # Gemini 3 requires thought_signature to be echoed back on the first
                # tool_call in each step; it lives in extra_content on the tc object.
                def _build_tc_dict(tc):
                    d = {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": (
                                json.dumps(tc.function.arguments)
                                if isinstance(tc.function.arguments, dict)
                                else tc.function.arguments
                            ),
                        },
                    }
                    extra = getattr(tc, "extra_content", None) or (
                        getattr(tc, "model_extra", None) or {}
                    ).get("extra_content")
                    if extra:
                        d["extra_content"] = extra
                    return d

                current_messages.append(
                    {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": [_build_tc_dict(tc) for tc in tool_calls],
                    }
                )
                print_blue(
                    f"[ChatService] Smart model requested {len(tool_calls)} tool call(s)."
                )
                tool_result_messages: List[Dict[str, Any]] = []
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = tool_call.function.arguments
                    # _llm previously parsed this as a side-effect of auto-execution.
                    # With auto_execute_tools=False we must parse it ourselves.
                    if isinstance(tool_args, str):
                        try:
                            tool_args = json.loads(tool_args)
                        except json.JSONDecodeError:
                            log_event("malformed_json", model=getattr(_smart, "model", None), tool=tool_name, raw=tool_call.function.arguments[:300])
                            tool_args = {}
                    if isinstance(tool_args, dict) and "focus_ids" in tool_args:
                        requested_focus = tool_args["focus_ids"]
                        if requested_focus is True or (
                            isinstance(requested_focus, str)
                            and requested_focus.strip().lower() == "focus_ids"
                        ):
                            tool_args["focus_ids"] = list(active_focus_ids)
                        elif requested_focus in (False, None) and not active_focus_ids:
                            tool_args.pop("focus_ids")
                    # Hard-block identical repeats: answer from cache with a nudge
                    # instead of re-executing. Every tool_call_id still gets a
                    # tool message (OpenAI spec), so we append and move on.
                    _call_key = (tool_name, json.dumps(tool_args, sort_keys=True, default=str))
                    if tool_name not in _DEDUP_EXEMPT and _call_key in _executed_calls:
                        print_yellow(
                            f"[ChatService] Blocked repeated identical call: {tool_name}"
                        )
                        log_event(
                            "repeated_tool_call_blocked",
                            model=getattr(_smart, "model", None),
                            tool=tool_name,
                            args=tool_args,
                        )
                        tool_result_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_name,
                                "content": (
                                    "You already made this exact call earlier in this conversation. "
                                    "Cached result:\n"
                                    + _executed_calls[_call_key][:1500]
                                    + "\n\nDo not repeat identical calls. "
                                    + _dedup_hint(tool_name)
                                ),
                            }
                        )
                        continue

                    print_blue(
                        f"[ChatService] Executing tool: {tool_name} with args: {tool_args}"
                    )
                    if event_callback:
                        event_callback({"type": "tool_call", "tool": tool_name, "args": tool_args})

                    # For share_insight: strip any speaker_ids or hit_ids the LLM
                    # invented — only allow IDs that actually appeared in prior tool results.
                    if tool_name == "share_insight":
                        valid_speaker_ids = set(collected_persons.keys())
                        valid_hit_ids = {s["_id"] for s in collected_sources}
                        if "speaker_ids" in tool_args and tool_args["speaker_ids"]:
                            original = tool_args["speaker_ids"]
                            tool_args["speaker_ids"] = [
                                sid for sid in original if sid in valid_speaker_ids
                            ]
                            stripped = set(original) - set(tool_args["speaker_ids"])
                            if stripped:
                                print_yellow(
                                    f"[ChatService] share_insight: stripped hallucinated speaker_ids: {stripped}"
                                )
                                log_event("hallucinated_speaker_ids", model=getattr(_smart, "model", None), stripped=list(stripped))
                        if "hit_ids" in tool_args and tool_args["hit_ids"]:
                            original = tool_args["hit_ids"]
                            tool_args["hit_ids"] = [
                                hid
                                for hid in original
                                if hid in valid_hit_ids
                                or f"talks/{hid}" in valid_hit_ids
                                or f"motions/{hid}" in valid_hit_ids
                            ]
                            stripped = set(original) - set(tool_args["hit_ids"])
                            if stripped:
                                print_yellow(
                                    f"[ChatService] share_insight: stripped hallucinated hit_ids: {stripped}"
                                )
                                log_event("hallucinated_hit_ids", model=getattr(_smart, "model", None), stripped=list(stripped))

                    structured = None
                    tool_func = self._get_tool_function(tool_name)
                    if tool_func is None:
                        print_blue(
                            f"[ChatService] Tool function '{tool_name}' not found!"
                        )
                        log_event("tool_not_found", model=getattr(_smart, "model", None), tool=tool_name)
                        tool_result = f"ERROR: Tool '{tool_name}' not found."
                    else:
                        _tool_structured_result.set(None)  # clear before call
                        try:
                            tool_result = tool_func(**tool_args)
                        except Exception as e:
                            print_red(
                                f"[ChatService] Exception in tool '{tool_name}': {e}"
                            )
                            import traceback

                            traceback.print_exc()
                            log_error("tool_exception", e, model=getattr(_smart, "model", None), tool=tool_name)
                            tool_result = f"ERROR: {e}"
                        # If the tool stored a structured result via the ContextVar
                        # (to avoid JSON-serialisation issues in the _llm wrapper),
                        # use that instead of the plain string return value.
                        structured = _tool_structured_result.get()
                        # For database_query: extract speaker intressent_ids from enriched rows
                        # so the shadow communicator can attach portraits to stats insights.
                        if (
                            tool_name == "database_query"
                            and isinstance(structured, dict)
                            and structured.get("type") == "db_rows"
                        ):
                            for row in structured.get("rows") or []:
                                if not isinstance(row, dict):
                                    continue
                                iid = row.get("intressent_id")
                                name = row.get("talare")
                                party = row.get("parti", "")
                                if iid and name and iid not in collected_persons:
                                    collected_persons[iid] = {"name": name, "party": party}
                            structured = None  # Don't let this dict affect tool_result downstream
                        # Register provenance from structured data regardless of tool
                        if structured is not None and registry is not None:
                            if isinstance(structured, SearchHitsResult):
                                self._register_hits_in_registry(
                                    structured.response, registry, tool_name
                                )
                            elif isinstance(structured, HitsResponse):
                                self._register_hits_in_registry(
                                    structured, registry, tool_name
                                )
                        # Replace tool_result with structured data for search tools.
                        # fetch_documents returns a plain list and fetch_debate returns
                        # a dict with debate-level metadata; for both we keep the
                        # original return value so the eviction step (else branch)
                        # can read it alongside the structured HitsResponse.
                        # read_documents_for returns the distilled answer itself —
                        # replacing it with the structured hits would destroy it.
                        if structured is not None and tool_name not in (
                            "fetch_documents", "fetch_debate", "read_documents_for"
                        ):
                            tool_result = structured

                    if (
                        isinstance(tool_result, dict)
                        and tool_result.get("type") == "insight"
                    ):
                        if event_callback:
                            hits_payload = tool_result.get("hits")
                            rows_payload = tool_result.get("rows")
                            speaker_ids = tool_result.get("speaker_ids", [])
                            speaker_ids_context = tool_result.get(
                                "speaker_ids_context", ""
                            )
                            if hits_payload:
                                event_callback(
                                    {
                                        "type": "search_card",
                                        "query": tool_result.get("message", ""),
                                        "results": hits_payload[:8],
                                        "total": len(hits_payload),
                                        "limit_reached": False,
                                        "stats": {},
                                        "speaker_ids": speaker_ids,
                                        "speaker_ids_context": speaker_ids_context,
                                    }
                                )
                            elif rows_payload:
                                event_callback(
                                    {
                                        "type": "stats_card",
                                        "rows": rows_payload[:20],
                                        "speaker_ids": speaker_ids,
                                        "speaker_ids_context": speaker_ids_context,
                                    }
                                )
                            else:
                                event_callback(
                                    {
                                        "type": "insight",
                                        "message": tool_result.get("message", ""),
                                        "sources": tool_result.get("sources", {}),
                                        "speaker_ids": speaker_ids,
                                        "speaker_ids_context": speaker_ids_context,
                                    }
                                )
                        tool_result_string = "ok"

                    elif isinstance(tool_result, SearchHitsResult):
                        # Provenance already registered above from structured data.
                        active_focus_ids = tool_result.focus_ids or active_focus_ids
                        iids = self._collect_sources_from_hits_response(
                            tool_result.response, collected_sources, collected_persons
                        )
                        if event_callback and iids:
                            event_callback(
                                {
                                    "type": "tool_speakers",
                                    "intressent_ids": list(dict.fromkeys(iids)),
                                }
                            )
                        # Evict raw bodies — full text is in the registry, accessible via lookup_source.
                        tool_result_string = self._build_eviction_stub(
                            tool_result.response, tool_name
                        )

                    elif isinstance(tool_result, HitsResponse):
                        # Provenance already registered above from structured data.
                        iids = self._collect_sources_from_hits_response(
                            tool_result, collected_sources, collected_persons
                        )
                        if event_callback and iids:
                            event_callback(
                                {
                                    "type": "tool_speakers",
                                    "intressent_ids": list(dict.fromkeys(iids)),
                                }
                            )
                        tool_result_string = self._build_eviction_stub(
                            tool_result, tool_name
                        )

                    else:
                        # fetch_documents and fetch_debate both produce a structured
                        # HitsResponse on the side; we evict the bodies and keep a
                        # one-line stub. fetch_debate also has debate-level metadata
                        # (summary, note, num_talks) worth preserving.
                        if structured is not None and isinstance(structured, HitsResponse):
                            self._collect_sources_from_hits_response(
                                structured, collected_sources, collected_persons
                            )
                            stub = self._build_eviction_stub(structured, tool_name)
                            if tool_name == "read_documents_for":
                                # The return value IS the focused answer — keep it.
                                # Bodies live in the registry (lookup_source).
                                tool_result_string = str(tool_result)
                            elif tool_name == "fetch_debate" and isinstance(tool_result, dict):
                                debate_summary = (tool_result.get("summary") or "").strip()
                                note = tool_result.get("note") or ""
                                num_talks = tool_result.get("num_talks") or 0
                                datum = tool_result.get("datum") or ""
                                debate_id = tool_result.get("debate_id") or ""
                                header_lines = [
                                    f"fetch_debate({debate_id}) — {datum}, {num_talks} talks"
                                ]
                                if debate_summary:
                                    header_lines.append(f"Debate summary: {debate_summary}")
                                if note:
                                    header_lines.append(f"Note: {note}")
                                tool_result_string = "\n".join(header_lines) + "\n\n" + stub
                            else:
                                tool_result_string = stub
                        else:
                            tool_result_string = str(tool_result)

                    # Route long results through the fast model for summarization.
                    # HitsResponse results are already per-document summarised above.
                    # Plain strings (database_query, fetch_documents) may still be large.
                    if len(tool_result_string) > SUMMARIZE_THRESHOLD:
                        tool_result_string = self._summarize_tool_result(
                            tool_name, tool_result_string, user_question, fast_llm=_fast
                        )
                        # Re-inject [src:...] tags after summarization — the fast model
                        # rewrites the text and may destroy them. Use the enriched
                        # format so the orchestrator still sees speaker+party inline.
                        if structured is not None:
                            hits_list = None
                            if isinstance(structured, SearchHitsResult):
                                hits_list = structured.response.hits
                            elif isinstance(structured, HitsResponse):
                                hits_list = structured.hits
                            if hits_list:
                                from backend.services.llm_tools import _format_src_tag
                                tags = []
                                for h in hits_list:
                                    bare = h.key or (h.id.split("/", 1)[1] if h.id and "/" in h.id else h.id)
                                    if bare:
                                        tags.append(_format_src_tag(bare, h.speaker, h.party, h.date))
                                if tags:
                                    tag_line = "Sources: " + " ".join(tags)
                                    tool_result_string = f"{tag_line}\n\n{tool_result_string}"
                    elif len(tool_result_string) > 12000:
                        # Fallback hard truncation (shouldn't normally be reached)
                        print_red(
                            f"[ChatService] Tool result still too long ({len(tool_result_string)} chars), truncating."
                        )
                        tool_result_string = (
                            f"{tool_result_string[:12000]} (...) [truncated]"
                        )

                    if event_callback:
                        # _eval_only: recorded by the eval ConversationRecorder,
                        # never forwarded to the SSE stream.
                        event_callback({
                            "type": "tool_result",
                            "tool": tool_name,
                            "content": tool_result_string[:8000],
                            "chars": len(tool_result_string),
                            "_eval_only": True,
                        })

                    # ── LLM misbehaviour detection ────────────────────────────────────
                    _SEARCH_TOOLS = {"arango_search", "vector_search", "vector_search_debates", "search_motions", "vector_search_motions"}
                    _args_json = json.dumps(tool_args, sort_keys=True, default=str)
                    _result_empty = not tool_result_string.strip() or tool_result_string.startswith("ERROR")
                    _model_name = getattr(_smart, "model", None)
                    if _last_tool_call is not None:
                        _prev_name, _prev_empty, _prev_args_json = _last_tool_call
                        if tool_name in _SEARCH_TOOLS and _prev_name == tool_name and _prev_empty:
                            log_event(
                                "zero_result_retry",
                                model=_model_name,
                                tool=tool_name,
                                first_args=json.loads(_prev_args_json),
                                retry_args=tool_args,
                            )
                        elif tool_name == _prev_name and _args_json == _prev_args_json:
                            log_event("repeated_tool_call", model=_model_name, tool=tool_name, args=tool_args)
                    _last_tool_call = (tool_name, _result_empty, _args_json)
                    _executed_calls[(tool_name, _args_json)] = tool_result_string
                    # ── end misbehaviour detection ────────────────────────────────────

                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": f"Result from calling {tool_name}:\n{tool_result_string}.",
                    }
                    if "ERROR" in tool_result_string:
                        print_red(
                            f"[ChatService] Tool result for '{tool_name.upper()}': {tool_message['content']}..."
                        )
                    else:
                        print_green(
                            f"[ChatService] Tool result for '{tool_name.upper()}': {tool_message['content'][:200]}..."
                        )
                    # After data-returning tools, check if the shadow communicator should share an insight.
                    data_tools = {
                        "arango_search",
                        "vector_search",
                        "vector_search_debates",
                        "fetch_debate",
                        "database_query",
                        "fetch_documents",
                        "read_documents_for",
                        "search_motions",
                        "vector_search_motions",
                        "fetch_motion",
                    }
                    result_is_useful = (
                        tool_name in data_tools
                        and "ERROR" not in tool_result_string
                        and tool_result_string.strip() not in ("", "...")
                    )
                    tool_result_messages.append(tool_message)
                    # Shadow communicator: fire-and-forget insight check.
                    if result_is_useful and event_callback:
                        shadow_msgs = list(current_messages) + [tool_message]
                        cb = event_callback
                        threading.Thread(
                            target=self._shadow_communicate,
                            args=(
                                shadow_msgs,
                                cb,
                                sent_insights,
                                dict(collected_persons),
                                # Snapshot of valid hit IDs so we can reject
                                # hallucinated ones from the communicator.
                                {s["_id"] for s in collected_sources},
                                _communicator,
                            ),
                            daemon=True,
                        ).start()

                # Append all tool results. Add the citation/question reminder on the last one only.
                # Do NOT append a separate user message — it reads as the user nagging
                # and pushes the model to rush to the final answer.
                if tool_result_messages and user_question:
                    question_note = (
                        f"\n\n[Reminder: cite sources using [src:ID] tags from tool results. "
                        f'Do NOT use [1],[2]. Do NOT write a "Källor" section. '
                        f"Answer in Swedish. Original question: {user_question}]"
                    )
                    tool_result_messages[-1]["content"] += question_note
                current_messages.extend(tool_result_messages)
                continue
            elif response.content:
                final_content = getattr(response, "content", "")

                # Reject answers that cite only hallucinated IDs.
                # If the model used [src:...] tags but none match the registry,
                # it invented the citations — push back and force a real search.
                if registry is not None and _citation_retries < _MAX_CITATION_RETRIES:
                    cited_in_answer = _SRC_PATTERN.findall(final_content)
                    if cited_in_answer:
                        valid = [cid for cid in cited_in_answer if registry.get(cid)]
                        if not valid:
                            _citation_retries += 1
                            invalid_shown = ", ".join(
                                f"[src:{cid}]" for cid in list(dict.fromkeys(cited_in_answer))[:5]
                            )
                            print_red(
                                f"[ChatService] Citation retry {_citation_retries}: "
                                f"all cited IDs are invalid ({invalid_shown})"
                            )
                            log_event(
                                "citation_retry",
                                attempt=_citation_retries,
                                invalid_ids=list(dict.fromkeys(cited_in_answer)),
                            )
                            current_messages.append(
                                {"role": "assistant", "content": final_content}
                            )
                            current_messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        f"Ditt svar innehåller källhänvisningar ({invalid_shown}) "
                                        "som inte finns bland de tal du har hämtat — du har hittat på tal-ID:n. "
                                        "Du får BARA citera tal vars [src:ID] du faktiskt sett i ett verktygsresultat. "
                                        "Sök igen med rätt filter (t.ex. intressent_ids) och bygg om svaret med riktiga källhänvisningar."
                                    ),
                                }
                            )
                            continue

                final_message = FinalAnswer(
                    final_answer=final_content,
                    explanation="Model provided a direct answer without requiring additional tools.",
                )
                return final_message, collected_tables, active_focus_ids
            else:
                # Model returned neither tool calls nor content (empty response).
                # Append a forcing message so the next iteration has new context to act on;
                # without this the model sees identical messages and keeps returning None.
                last_tool_msg = next(
                    (m for m in reversed(current_messages) if m.get("role") == "tool"),
                    None,
                )
                last_had_error = last_tool_msg and "ERROR" in last_tool_msg.get(
                    "content", ""
                )
                print_red(
                    f"[ChatService] Iteration {i}: model returned empty response (last_had_error={last_had_error})."
                )
                log_event("empty_response", model=getattr(_smart, "model", None), iteration=i, last_had_error=last_had_error)
                if last_had_error:
                    current_messages.append(
                        {
                            "role": "user",
                            "content": "Det senaste verktygsanropet returnerade ett fel. Rätta felet och försök igen, eller anropa ett annat verktyg.",
                        }
                    )
                else:
                    current_messages.append(
                        {
                            "role": "user",
                            "content": "Anropa ett verktyg om du behöver mer information, eller ge ditt slutsvar nu.",
                        }
                    )

    def _shadow_communicate(
        self,
        messages_snapshot: List[ChatMessage],
        event_callback: Callable[[Dict[str, Any]], None],
        sent_insights: List[str],
        known_persons: Dict[str, Dict],
        known_hit_ids: set,
        communicator_llm=None,
    ) -> None:
        """Background thread: let the communicator LLM decide IF and HOW to call share_insight.

        The communicator receives the full message history + latest tool result and has
        share_insight as its ONLY available tool. It decides:
          - Whether the result is interesting enough to surface.
          - Which card type to use: plain insight, search_card (hit_ids), stats_card (sql).
          - Which speaker portraits to attach (speaker_ids).

        The main message history (current_messages) is NEVER modified here — the communicator
        runs as a pure side-effect so the orchestrator's context stays clean.

        Args:
            messages_snapshot: Full message history including the latest tool result.
            event_callback: SSE emitter for the frontend.
            sent_insights: Shared list of messages already emitted this session.
                Updated in-place when a new insight is sent so future calls can avoid repeats.
            known_persons: Snapshot of {intressent_id → {name, party}} from actual results.
                Used to reject hallucinated speaker_ids.
            known_hit_ids: Snapshot of talk IDs seen in actual search results.
                Used to reject hallucinated hit_ids.
        """
        # Serialise shadow threads: acquire the lock before reading sent_insights
        # and hold it until we've appended any new insight. This prevents concurrent
        # threads from each seeing an empty/stale list and all deciding to share.
        with self._shadow_lock:
            # Rebuild dedup block under the lock so it reflects all prior insights.
            if sent_insights:
                already_shared = "\n".join(f"- {s}" for s in sent_insights)
                dedup_block = (
                    f"\n\nInsikter redan delade den här sessionen:\n{already_shared}\n\n"
                    "Om det senaste resultatet bara bekräftar samma fakta som ovan (samma person, samma siffra, samma ämne): anropa INTE share_insight. "
                    "Om du redan delat en insikt om en enskild person, dela inte en ny om samma person!"
                )
            else:
                dedup_block = ""

            instruction = _SHADOW_INSTRUCTION + dedup_block
            probe = list(messages_snapshot) + [{"role": "user", "content": instruction}]

            # Set the SSE callback in the ContextVar so share_insight (called below)
            # can publish the event without needing a return value.
            # ContextVar is thread-local: this set() only affects this thread.
            _insight_callback.set(event_callback)

            _comm = communicator_llm or self.communicator_llm
            gen_kwargs = {"messages": probe, "think": False, "auto_execute_tools": False}
            if getattr(self, "communicator_tools", None):
                gen_kwargs["tools"] = self.communicator_tools
            response = _comm.generate(**gen_kwargs)

            tool_calls = getattr(response, "tool_calls", None)
            if not tool_calls:
                print_yellow("[ShadowCommunicator] No tool call — nothing to share")
                return

            for tc in tool_calls:
                if tc.function.name != "share_insight":
                    continue
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                message = (args.get("message") or "").strip()
                if not message:
                    return

                # Server-side deduplication: skip if this message overlaps heavily
                # with something already sent. This catches cases where the model
                # ignores the instruction-based dedup (e.g. less instruction-following
                # models like Llama on Berget).
                if _is_duplicate_insight(message, sent_insights):
                    print_yellow(f"[ShadowCommunicator] Dedup skip (overlap): {message[:80]}")
                    return

                # Reject hallucinated speaker_ids — only allow IDs seen in actual results.
                if args.get("speaker_ids"):
                    args["speaker_ids"] = [
                        s for s in args["speaker_ids"] if s in known_persons
                    ]

                # Reject hallucinated hit_ids — only allow IDs from actual search results.
                if args.get("hit_ids"):
                    args["hit_ids"] = [
                        h
                        for h in args["hit_ids"]
                        if h in known_hit_ids
                        or f"talks/{h}" in known_hit_ids
                        or f"motions/{h}" in known_hit_ids
                    ]

                print_yellow(f"[ShadowCommunicator] share_insight: {message[:120]}")
                # Record before releasing the lock so the next thread sees it.
                sent_insights.append(message)
                # Call share_insight — it publishes via _insight_callback set above.
                share_insight(**args)

    def _editor_pass(
        self,
        draft: str,
        user_question: str,
        registry: ProvenanceRegistry,
        editor_llm,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> str:
        """Run one fact-check + language-polish pass over the draft answer.

        Fetches the full anforandetext for each cited source (capped so the editor
        call stays within model context), hands them to the editor along with the
        draft, and returns the rewritten draft. On any error the original draft is
        returned unchanged so the user never gets worse output because of the pass.
        """
        # Collect only source IDs actually cited in the draft — no point paying for
        # sources the orchestrator didn't use.
        cited_ids_raw = _SRC_PATTERN.findall(draft)
        seen: set[str] = set()
        cited_ids: List[str] = []
        for cid in cited_ids_raw:
            if cid not in seen and registry.get(cid):
                cited_ids.append(cid)
                seen.add(cid)
        if not cited_ids:
            print_yellow("[Editor] no valid citations in draft; skipping editor pass")
            return draft

        # Fetch full talk texts for the cited sources.
        from postgres_client import pg as _pg
        try:
            rows = _pg.execute(
                "SELECT id, talare, parti, datum::text AS datum, anforandetext "
                "FROM talks WHERE id = ANY(%s::text[])",
                (cited_ids,),
            )
        except Exception as exc:
            print_red(f"[Editor] failed to fetch cited talks: {exc}")
            return draft
        talks_by_id: Dict[str, Dict[str, Any]] = {r["id"]: r for r in rows}

        # Budget source text so the full editor prompt stays bounded. 24 000 chars
        # across N cited sources ≈ 6 000 tokens, leaving room for the draft, system
        # prompt, and the editor's rewrite. Per-source cap scales with count.
        TOTAL_BUDGET = 24_000
        if cited_ids:
            per_source = max(1_500, TOTAL_BUDGET // max(1, len(cited_ids)))
        else:
            per_source = 0

        source_blocks: List[str] = []
        for sid in cited_ids:
            row = talks_by_id.get(sid)
            if not row:
                src = registry.get(sid)
                if src:
                    source_blocks.append(
                        f"[src:{sid} | {src.speaker or '?'} ({src.party or '?'}) | {src.date or '?'}]\n"
                        f"(Full talktext ej tillgänglig — använd snippet nedan)\n{src.snippet[:per_source]}"
                    )
                continue
            text = (row.get("anforandetext") or "")[:per_source]
            source_blocks.append(
                f"[src:{sid} | {row['talare']} ({row['parti']}) | {row['datum']}]\n{text}"
            )

        sources_text = "\n\n---\n\n".join(source_blocks)
        user_prompt = (
            f"Ursprunglig fråga:\n{user_question}\n\n"
            f"### Utkast att granska\n\n{draft}\n\n"
            f"### Citerade källor (fulltext, tryngd till budget)\n\n{sources_text}\n\n"
            "Returnera ENDAST den reviderade markdown-texten."
        )

        if event_callback:
            event_callback({"type": "status", "message": "Redaktör läser igenom svaret..."})

        import time as _time
        t0 = _time.time()
        try:
            response = editor_llm.generate(
                messages=[
                    {"role": "system", "content": EDITOR_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                think=False,
            )
        except Exception as exc:
            print_red(f"[Editor] generate() raised: {exc}")
            return draft
        elapsed_ms = int((_time.time() - t0) * 1000)

        if isinstance(response, str):
            # LLM wrapper swallowed an API error and returned a plain string.
            print_red(f"[Editor] wrapper returned error string: {response[:200]}")
            log_event("editor_pass_failure", detail=response[:200])
            return draft

        revised = getattr(response, "content", None) or ""
        revised = revised.strip()
        if not revised:
            print_red("[Editor] empty response — keeping draft")
            log_event("editor_pass_empty")
            return draft

        # Reject if the editor compressed the answer significantly — that means it
        # summarised or rewrote rather than making targeted fixes.
        if len(revised) < len(draft) * 0.85:
            print_red(
                f"[Editor] response too short ({len(revised)} vs {len(draft)} chars, "
                f"{len(revised)/len(draft):.0%}) — editor likely rewrote instead of patching, keeping draft"
            )
            log_event("editor_pass_rejected", reason="too_short",
                      draft_chars=len(draft), revised_chars=len(revised))
            return draft

        delta_chars = len(revised) - len(draft)
        log_event(
            "editor_pass_ran",
            draft_chars=len(draft),
            revised_chars=len(revised),
            delta_chars=delta_chars,
            duration_ms=elapsed_ms,
            cited_sources=len(cited_ids),
            model=getattr(editor_llm, "model", None),
        )
        print_green(
            f"[Editor] rewrote draft: {len(draft)} → {len(revised)} chars "
            f"(Δ {delta_chars:+d}) in {elapsed_ms} ms"
        )
        return revised

    def _fix_with_fact_check_feedback(
        self,
        answer_body: str,
        warnings: List[Dict[str, Any]],
        cited_sources: List[Dict[str, Any]],
        editor_llm,
        smart_llm,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> str:
        """Two-phase attribution fix: fact-checker produces JSON feedback, smart LLM rewrites.

        Phase 1: editor_llm analyzes each flagged paragraph and returns structured JSON
        describing specific issues (wrong speaker, unsupported claim, etc.).
        Phase 2: smart_llm rewrites only that paragraph using the feedback plus full answer context.

        Called only when use_editor=True and at least one warning was found.
        """
        if not warnings:
            return answer_body

        import json as _json
        import time as _time
        _cite_n_re = re.compile(r"\[\d+\]")
        _editor_model = getattr(editor_llm, "model", None)
        _smart_model = getattr(smart_llm, "model", None)

        all_paras = answer_body.split("\n\n")
        cited_to_actual: Dict[int, int] = {}
        cited_idx = 0
        for actual_idx, para in enumerate(all_paras):
            if _cite_n_re.search(para):
                cited_to_actual[cited_idx] = actual_idx
                cited_idx += 1

        by_para: Dict[int, List[Dict[str, Any]]] = {}
        for w in warnings:
            by_para.setdefault(w["paragraph_idx"], []).append(w)

        flagged_summary = ", ".join(
            f"{w['name']}({w['party']})" for ws in by_para.values() for w in ws
        )
        print_yellow(
            f"[Fact-check fix] {len(by_para)} paragraph(s) to check — "
            f"flagged: {flagged_summary}"
        )
        log_event(
            "fact_check_fix_started",
            paragraphs=len(by_para),
            total_warnings=len(warnings),
            flagged=[{"name": w["name"], "party": w["party"], "reason": w["reason"]} for w in warnings],
        )

        # Pre-fetch full talk texts for all cited sources in flagged paragraphs.
        all_ns: set = {n for ws in by_para.values() for w in ws for n in w["cited_ns"]}
        talk_ids: List[str] = []
        for n in all_ns:
            idx = n - 1
            if 0 <= idx < len(cited_sources):
                src = cited_sources[idx]
                tid = (src.get("talk_id") or src.get("_id") or "").split("/")[-1]
                if tid:
                    talk_ids.append(tid)

        full_texts: Dict[str, Dict[str, Any]] = {}
        if talk_ids:
            try:
                from postgres_client import pg as _pg
                rows = _pg.execute(
                    "SELECT id, talare, parti, datum::text AS datum, anforandetext "
                    "FROM talks WHERE id = ANY(%s::text[])",
                    (list(set(talk_ids)),),
                )
                full_texts = {r["id"]: r for r in rows}
            except Exception as exc:
                print_red(f"[Fact-check fix] DB fetch failed: {exc}")
                log_error("attribution_fix_db_failure", exc)

        _PER_SOURCE_CHARS = 3_000
        modified_paras = list(all_paras)
        fixed_count = 0

        if event_callback:
            event_callback({"type": "status", "message": "Kontrollerar källhänvisningar..."})

        for cited_para_idx, para_warnings in sorted(by_para.items()):
            actual_idx = cited_to_actual.get(cited_para_idx)
            if actual_idx is None:
                print_yellow(f"[Fact-check fix] para {cited_para_idx}: no mapping, skipping")
                continue

            para_text = all_paras[actual_idx]
            above_text = all_paras[actual_idx - 1] if actual_idx > 0 else ""
            para_ns = sorted({n for w in para_warnings for n in w["cited_ns"]})

            # Build source blocks with full talk text where available.
            source_blocks: List[str] = []
            for n in para_ns:
                idx = n - 1
                if not (0 <= idx < len(cited_sources)):
                    continue
                src = cited_sources[idx]
                tid = (src.get("talk_id") or src.get("_id") or "").split("/")[-1]
                meta = full_texts.get(tid)
                if meta:
                    text = (meta.get("anforandetext") or "")[:_PER_SOURCE_CHARS]
                    source_blocks.append(
                        f"[{n}] {meta['talare']} ({meta['parti']}) — {meta['datum']}\n{text}"
                    )
                else:
                    source_blocks.append(
                        f"[{n}] {src.get('speaker')} ({src.get('party')}) — {src.get('date')}\n"
                        f"{(src.get('snippet') or '')[:_PER_SOURCE_CHARS]}"
                    )

            mismatch_lines: List[str] = []
            for w in para_warnings:
                src_labels = []
                for n in w["cited_ns"]:
                    idx = n - 1
                    if 0 <= idx < len(cited_sources):
                        s = cited_sources[idx]
                        src_labels.append(f"[{n}] {s.get('speaker')} ({s.get('party')})")
                mismatch_lines.append(
                    f"- Stycket namnger '{w['name']} ({w['party']})' men de citerade källorna "
                    f"({', '.join(src_labels) or '?'}) matchar inte detta namn/parti."
                )
                print_yellow(
                    f"[Fact-check fix] para {cited_para_idx}: "
                    f"'{w['name']} ({w['party']})' ≠ {', '.join(src_labels) or '?'} "
                    f"[{w['reason']}]"
                )

            context_block = f"Stycket ovanför (kontext, ändra ej):\n{above_text}\n\n" if above_text else ""
            fc_prompt = (
                f"{context_block}"
                f"Stycket att granska:\n{para_text}\n\n"
                f"Identifierade avvikelser att verifiera:\n" + "\n".join(mismatch_lines) + "\n\n"
                + "Citerade källors fulltext:\n\n"
                + "\n\n---\n\n".join(source_blocks)
                + "\n\nReturnera JSON enligt schemat i din systeminstruktion."
            )

            # --- Phase 1: fact-checker produces structured feedback ---
            t0 = _time.time()
            try:
                fc_response = editor_llm.generate(
                    messages=[
                        {"role": "system", "content": FACT_CHECKER_SYSTEM},
                        {"role": "user", "content": fc_prompt},
                    ],
                    think=False,
                )
            except Exception as exc:
                print_red(f"[Fact-check fix] para {cited_para_idx}: fact-check call failed — {exc}")
                log_error("fact_check_call_failure", exc, cited_para_idx=cited_para_idx)
                continue
            fc_ms = int((_time.time() - t0) * 1000)

            if isinstance(fc_response, str):
                print_red(f"[Fact-check fix] para {cited_para_idx}: LLM error — {fc_response[:120]}")
                continue

            fc_text = (getattr(fc_response, "content", None) or "").strip()
            # Strip markdown code fences if the model wrapped the JSON.
            fc_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", fc_text, flags=re.DOTALL).strip()
            try:
                fc_data = _json.loads(fc_text)
            except Exception as exc:
                print_red(f"[Fact-check fix] para {cited_para_idx}: JSON parse failed ({exc}) — raw: {fc_text[:200]}")
                log_event("fact_check_parse_failure", cited_para_idx=cited_para_idx, raw=fc_text[:200])
                continue

            if fc_data.get("verdict") == "ok" or not fc_data.get("issues"):
                print_green(f"[Fact-check fix] para {cited_para_idx}: fact-checker found no issues — keeping original")
                log_event("fact_check_ok", cited_para_idx=cited_para_idx, duration_ms=fc_ms)
                continue

            issues_text = "\n".join(
                f"- Fras: \"{iss.get('quote', '')}\"\n"
                f"  Problem: {iss.get('problem', '')}\n"
                f"  Källan säger: {iss.get('source_says', '')}"
                for iss in fc_data["issues"]
            )
            log_event(
                "fact_check_issues_found",
                cited_para_idx=cited_para_idx,
                issue_count=len(fc_data["issues"]),
                duration_ms=fc_ms,
                model=_editor_model,
            )
            print_yellow(
                f"[Fact-check fix] para {cited_para_idx}: {len(fc_data['issues'])} issue(s) found "
                f"in {fc_ms} ms — proceeding to rewrite"
            )

            # --- Phase 2: smart_llm rewrites paragraph using the feedback ---
            rewrite_prompt = (
                f"Du skriver om ETT stycke i ett svar. Hela svaret ges nedan som kontext "
                f"— rör INGENTING annat än stycket markerat med >>START>> och >>SLUT>>.\n\n"
                f"### Hela svaret (kontext, ändra ej)\n\n{answer_body}\n\n"
                f"### Stycket att rätta\n\n>>START>>\n{para_text}\n>>SLUT>>\n\n"
                f"### Faktaredaktörens återkoppling\n\n{issues_text}\n\n"
                f"Rätta stycket enligt återkopplingen. Bevara [N]-taggarna exakt. "
                f"Returnera ENBART det rättade stycket — ingen inledning, ingen förklaring."
            )

            t1 = _time.time()
            try:
                rw_response = smart_llm.generate(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Du är en skicklig svensk textredaktör. Du gör minimala rättelser "
                                "baserat på faktaredaktörens återkoppling. Du rör inte stycken du "
                                "inte har fått instruktioner om att rätta."
                            ),
                        },
                        {"role": "user", "content": rewrite_prompt},
                    ],
                    think=False,
                )
            except Exception as exc:
                print_red(f"[Fact-check fix] para {cited_para_idx}: rewrite call failed — {exc}")
                log_error("fact_check_rewrite_failure", exc, cited_para_idx=cited_para_idx)
                continue
            rw_ms = int((_time.time() - t1) * 1000)

            if isinstance(rw_response, str):
                print_red(f"[Fact-check fix] para {cited_para_idx}: rewrite LLM error — {rw_response[:120]}")
                continue

            revised = (getattr(rw_response, "content", None) or "").strip()
            if not revised:
                print_red(f"[Fact-check fix] para {cited_para_idx}: empty rewrite, keeping original")
                continue

            if len(revised) > len(para_text) * 3 + 500:
                print_yellow(
                    f"[Fact-check fix] para {cited_para_idx}: rewrite too long "
                    f"({len(revised)} chars vs {len(para_text)} original), skipping"
                )
                log_event("fact_check_rewrite_too_long", cited_para_idx=cited_para_idx,
                          original_chars=len(para_text), revised_chars=len(revised))
                continue

            modified_paras[actual_idx] = revised
            fixed_count += 1
            delta = len(revised) - len(para_text)
            log_event(
                "fact_check_fix_applied",
                cited_para_idx=cited_para_idx,
                original_chars=len(para_text),
                revised_chars=len(revised),
                delta_chars=delta,
                fc_ms=fc_ms,
                rw_ms=rw_ms,
                fc_model=_editor_model,
                rw_model=_smart_model,
                issues=len(fc_data["issues"]),
            )
            print_green(
                f"[Fact-check fix] para {cited_para_idx}: rewritten "
                f"({len(para_text)} → {len(revised)} chars, Δ{delta:+d}) "
                f"fc={fc_ms}ms rw={rw_ms}ms"
            )

        print_green(
            f"[Fact-check fix] done — {fixed_count}/{len(by_para)} paragraph(s) rewritten"
        )
        log_event("fact_check_fix_done", fixed=fixed_count, attempted=len(by_para))
        return "\n\n".join(modified_paras)

    def _language_pass(
        self,
        text: str,
        language_llm_chain: list,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> str:
        """Polish Swedish language using the first available LLM in the fallback chain.

        Tries Gemini Flash → Berget → vLLM in order. Each candidate is validated:
        - must not be shorter than 85 % of the original (guards against summarisation)
        - must contain the exact same number of [N] citation tags (guards against dropped refs)
        Returns original text if all models fail or produce invalid output.
        """
        if not language_llm_chain or not text:
            return text

        if event_callback:
            event_callback({"type": "status", "message": "Språkgranskar svaret..."})

        original_citation_count = len(re.findall(r"\[\d+\]", text))
        import time as _time

        for llm in language_llm_chain:
            model_name = getattr(llm, "model", "unknown")
            t0 = _time.time()
            try:
                response = llm.generate(
                    messages=[
                        {"role": "system", "content": LANGUAGE_CHECKER_SYSTEM},
                        {"role": "user", "content": text},
                    ],
                    think=False,
                )
            except Exception as exc:
                print_red(f"[Language pass] {model_name}: generate() raised — {exc}; trying next")
                log_error("language_pass_failure", exc, model=model_name)
                continue
            elapsed_ms = int((_time.time() - t0) * 1000)

            if isinstance(response, str):
                print_red(f"[Language pass] {model_name}: LLM error — {response[:120]}; trying next")
                log_event("language_pass_llm_error", model=model_name, error=response[:200])
                continue

            revised = (getattr(response, "content", None) or "").strip()
            if not revised:
                print_red(f"[Language pass] {model_name}: empty response; trying next")
                continue

            if len(revised) < len(text) * 0.85:
                print_red(
                    f"[Language pass] {model_name}: too short "
                    f"({len(revised)} vs {len(text)} chars, {len(revised)/len(text):.0%}); trying next"
                )
                log_event("language_pass_rejected", reason="too_short", model=model_name,
                          original_chars=len(text), revised_chars=len(revised))
                continue

            revised_citation_count = len(re.findall(r"\[\d+\]", revised))
            if revised_citation_count != original_citation_count:
                print_red(
                    f"[Language pass] {model_name}: citation count changed "
                    f"({original_citation_count} → {revised_citation_count}); trying next"
                )
                log_event("language_pass_rejected", reason="citation_count_mismatch", model=model_name,
                          original=original_citation_count, revised=revised_citation_count)
                continue

            delta = len(revised) - len(text)
            log_event(
                "language_pass_ran",
                model=model_name,
                original_chars=len(text),
                revised_chars=len(revised),
                delta_chars=delta,
                duration_ms=elapsed_ms,
            )
            print_green(
                f"[Language pass] {model_name}: polished {len(text)} → {len(revised)} chars "
                f"(Δ{delta:+d}) in {elapsed_ms} ms"
            )
            return revised

        print_red("[Language pass] all models failed — keeping original answer")
        log_event("language_pass_all_failed")
        return text

    def _targeted_attribution_fix(
        self,
        answer_body: str,
        warnings: List[Dict[str, Any]],
        cited_sources: List[Dict[str, Any]],
        editor_llm,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> str:
        """Fix paragraphs flagged by detect_attribution_warnings.

        For each flagged paragraph, sends that paragraph plus the one above (for
        context) along with the full source texts and an explicit description of
        the detected mismatch to the editor. Only the flagged paragraph is
        replaced; the paragraph above is read-only context.

        Called only when use_editor=True and at least one warning was found.
        """
        if not warnings:
            return answer_body

        import time as _time
        _cite_n_re = re.compile(r"\[\d+\]")
        _editor_model = getattr(editor_llm, "model", None)

        # Split the answer into paragraphs and build a map:
        #   cited_para_idx (index among paragraphs that have [N]) -> actual list index
        all_paras = answer_body.split("\n\n")
        cited_to_actual: Dict[int, int] = {}
        cited_idx = 0
        for actual_idx, para in enumerate(all_paras):
            if _cite_n_re.search(para):
                cited_to_actual[cited_idx] = actual_idx
                cited_idx += 1

        # Group warnings by paragraph_idx so we do one editor call per paragraph.
        by_para: Dict[int, List[Dict[str, Any]]] = {}
        for w in warnings:
            by_para.setdefault(w["paragraph_idx"], []).append(w)

        flagged_summary = ", ".join(
            f"{w['name']}({w['party']})" for ws in by_para.values() for w in ws
        )
        print_yellow(
            f"[Attribution fix] {len(by_para)} paragraph(s) to fix — "
            f"flagged: {flagged_summary}"
        )
        log_event(
            "attribution_fix_started",
            model=_editor_model,
            paragraphs=len(by_para),
            total_warnings=len(warnings),
            flagged=[{"name": w["name"], "party": w["party"], "reason": w["reason"]} for w in warnings],
        )

        # Pre-fetch full talk texts for all cited sources that appear in flagged paragraphs.
        all_ns: set = {n for ws in by_para.values() for w in ws for n in w["cited_ns"]}
        talk_ids: List[str] = []
        for n in all_ns:
            idx = n - 1
            if 0 <= idx < len(cited_sources):
                src = cited_sources[idx]
                tid = (src.get("talk_id") or src.get("_id") or "").split("/")[-1]
                if tid:
                    talk_ids.append(tid)

        full_texts: Dict[str, Dict[str, Any]] = {}
        if talk_ids:
            try:
                from postgres_client import pg as _pg
                rows = _pg.execute(
                    "SELECT id, talare, parti, datum::text AS datum, anforandetext "
                    "FROM talks WHERE id = ANY(%s::text[])",
                    (list(set(talk_ids)),),
                )
                full_texts = {r["id"]: r for r in rows}
            except Exception as exc:
                print_red(f"[Attribution fix] DB fetch failed: {exc}")
                log_error("attribution_fix_db_failure", exc)

        _PER_SOURCE_CHARS = 3_000
        modified_paras = list(all_paras)
        fixed_count = 0

        for cited_para_idx, para_warnings in sorted(by_para.items()):
            actual_idx = cited_to_actual.get(cited_para_idx)
            if actual_idx is None:
                print_yellow(f"[Attribution fix] para {cited_para_idx}: no mapping to actual paragraph, skipping")
                continue

            para_text = all_paras[actual_idx]
            above_text = all_paras[actual_idx - 1] if actual_idx > 0 else ""
            para_ns = sorted({n for w in para_warnings for n in w["cited_ns"]})

            # Build source blocks with full talk text where available.
            source_blocks: List[str] = []
            for n in para_ns:
                idx = n - 1
                if not (0 <= idx < len(cited_sources)):
                    continue
                src = cited_sources[idx]
                tid = (src.get("talk_id") or src.get("_id") or "").split("/")[-1]
                meta = full_texts.get(tid)
                if meta:
                    text = (meta.get("anforandetext") or "")[:_PER_SOURCE_CHARS]
                    source_blocks.append(
                        f"[{n}] {meta['talare']} ({meta['parti']}) — {meta['datum']}\n{text}"
                    )
                else:
                    source_blocks.append(
                        f"[{n}] {src.get('speaker')} ({src.get('party')}) — {src.get('date')}\n"
                        f"{(src.get('snippet') or '')[:_PER_SOURCE_CHARS]}"
                    )

            # Describe each detected mismatch explicitly, including what the source actually says.
            mismatch_lines: List[str] = []
            for w in para_warnings:
                src_labels = []
                for n in w["cited_ns"]:
                    idx = n - 1
                    if 0 <= idx < len(cited_sources):
                        s = cited_sources[idx]
                        src_labels.append(f"[{n}] {s.get('speaker')} ({s.get('party')})")
                mismatch_lines.append(
                    f"- Stycket namnger '{w['name']} ({w['party']})' men de citerade källorna "
                    f"({', '.join(src_labels) or '?'}) matchar inte detta namn/parti."
                )
                print_yellow(
                    f"[Attribution fix] para {cited_para_idx}: "
                    f"'{w['name']} ({w['party']})' ≠ {', '.join(src_labels) or '?'} "
                    f"[{w['reason']}]"
                )

            context_block = f"Stycket ovanför (kontext, ändra ej):\n{above_text}\n\n" if above_text else ""
            user_prompt = (
                f"{context_block}"
                f"Stycket att granska:\n{para_text}\n\n"
                f"Identifierade avvikelser:\n" + "\n".join(mismatch_lines) + "\n\n"
                + "Citerade källors fulltext:\n\n"
                + "\n\n---\n\n".join(source_blocks)
                + "\n\n"
                "Rätta stycket enligt följande prioritering:\n"
                "1. Om källan stöder påståendet men talaren är fel — rätta till rätt namn/parti.\n"
                "2. Om påståendet handlar om ett partis ståndpunkt (inte en namngiven ledamot) — ta bort personnamnet.\n"
                "3. Om källan inte stöder påståendet alls — ta bort eller omformulera utan namn.\n"
                "Bevara [N]-taggarna exakt som de är. Returnera ENBART det rättade stycket, ingen förklaring."
            )

            if event_callback:
                event_callback({"type": "status", "message": "Kontrollerar källhänvisningar..."})

            t0 = _time.time()
            try:
                response = editor_llm.generate(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Du är en noggrann faktaredaktör. Du rättar felaktiga personattribueringar "
                                "i texter om riksdagsdebatter. Du returnerar ENBART det korrigerade stycket."
                            ),
                        },
                        {"role": "user", "content": user_prompt},
                    ],
                    think=False,
                )
            except Exception as exc:
                print_red(f"[Attribution fix] para {cited_para_idx}: editor call failed — {exc}")
                log_error("attribution_fix_call_failure", exc, model=_editor_model, cited_para_idx=cited_para_idx)
                continue
            duration_ms = int((_time.time() - t0) * 1000)

            if isinstance(response, str):
                print_red(f"[Attribution fix] para {cited_para_idx}: LLM error — {response[:120]}")
                log_event("attribution_fix_llm_error", model=_editor_model, cited_para_idx=cited_para_idx, error=response[:200])
                continue

            revised = (getattr(response, "content", None) or "").strip()
            if not revised:
                print_red(f"[Attribution fix] para {cited_para_idx}: empty response, keeping original")
                log_event("attribution_fix_empty", model=_editor_model, cited_para_idx=cited_para_idx)
                continue

            if len(revised) > len(para_text) * 3 + 500:
                print_yellow(
                    f"[Attribution fix] para {cited_para_idx}: response too long "
                    f"({len(revised)} chars vs {len(para_text)} original), skipping"
                )
                log_event("attribution_fix_skipped_too_long", model=_editor_model, cited_para_idx=cited_para_idx,
                          original_chars=len(para_text), revised_chars=len(revised))
                continue

            modified_paras[actual_idx] = revised
            fixed_count += 1
            delta = len(revised) - len(para_text)
            log_event(
                "attribution_fix_applied",
                model=_editor_model,
                cited_para_idx=cited_para_idx,
                original_chars=len(para_text),
                revised_chars=len(revised),
                delta_chars=delta,
                duration_ms=duration_ms,
                warnings=[{"name": w["name"], "party": w["party"], "reason": w["reason"]} for w in para_warnings],
            )
            print_green(
                f"[Attribution fix] para {cited_para_idx}: fixed "
                f"({len(para_text)} → {len(revised)} chars, Δ{delta:+d}) in {duration_ms} ms"
            )

        print_green(
            f"[Attribution fix] done — {fixed_count}/{len(by_para)} paragraph(s) rewritten"
        )
        log_event(
            "attribution_fix_done",
            model=_editor_model,
            fixed=fixed_count,
            attempted=len(by_para),
        )
        return "\n\n".join(modified_paras)

    def _get_tool_function(self, tool_name: str):
        for tool in self.tools:
            if hasattr(tool, "name") and tool.name == tool_name:
                return getattr(tool, "function", None)
        try:
            import backend.services.llm_tools as llm_tools

            return getattr(llm_tools, tool_name, None)
        except Exception:
            print_red(f"[ChatService] Could not import tool '{tool_name}'.")
            return None

    def _latest_user_message(self, messages: Sequence[ChatMessage]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return message.get("content", "").strip()
        return ""

    def _normalize_chunk_index(self, value: Any, default: int = -1) -> int:
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("+"):
                stripped = stripped[1:]
            if stripped.lstrip("-").isdigit():
                return int(stripped)
        return default

    def _deduplicate_sources(
        self, sources: List[ChatSource], limit: int
    ) -> List[ChatSource]:
        unique: Dict[tuple[Any, Any], ChatSource] = {}
        for source in sources:
            source_id = source.get("_id") or source.get("_id")
            chunk_index = self._normalize_chunk_index(source.get("chunk_index"))
            key = (source_id, chunk_index)
            if key in unique:
                continue
            snippet_value = source.get("snippet", "")
            snippet_text = self._trim_snippet(str(snippet_value))
            unique[key] = {
                "_id": source_id,
                "heading": source.get("heading"),
                "snippet": snippet_text,
                "chunk_index": chunk_index,
                "debateurl": source.get("debateurl") or source.get("debate_url"),
                "speaker": source.get("speaker"),
                "party": source.get("party"),
                "intressent_id": source.get("intressent_id"),
                "date": source.get("date"),
            }
        max_items = max(1, limit)
        return list(unique.values())[:max_items]

    def _trim_snippet(self, text: str, length: int = 4000) -> str:
        cleaned = text.strip()
        if len(cleaned) <= length:
            return cleaned
        return f"{cleaned[:length].rstrip()}…"

    def _get_unique_name_persons(self, persons: Dict[str, Dict]) -> Dict[str, Dict]:
        """
        Look up each collected person by intressent_id to get the canonical DB name,
        then keep only those whose name is unique in the people table.
        Two simple queries — same pattern as names_autocomplete.py.
        """
        if not persons:
            return {}
        from postgres_client import pg

        iids = list(persons.keys())
        print_yellow(f"[ChatService] Person lookup: {len(iids)} intressent_ids: {iids}")
        try:
            # Step 1: get canonical name + party for each collected intressent_id.
            id_rows = pg.execute(
                "SELECT intressent_id, namn, parti FROM people WHERE intressent_id = ANY(%s)",
                (iids,),
            )
            print_yellow(f"[ChatService] DB returned {len(id_rows)} people rows")
            if not id_rows:
                return {}

            # Step 2: check which of those names are unique (case-insensitive).
            names = [r["namn"] for r in id_rows]
            unique_rows = pg.execute(
                "SELECT namn FROM people WHERE LOWER(namn) = ANY(%s) GROUP BY namn HAVING COUNT(*) = 1",
                ([n.lower() for n in names],),
            )
            unique_names_lower = {r["namn"].lower() for r in unique_rows}
            print_yellow(
                f"[ChatService] Unique names: {[r['namn'] for r in unique_rows]}"
            )

            result = {}
            for row in id_rows:
                if row["namn"].lower() in unique_names_lower:
                    result[row["intressent_id"]] = {
                        "name": row["namn"],
                        "party": row["parti"] or "",
                    }
            print_green(
                f"[ChatService] {len(result)} persons will be linked: {[v['name'] for v in result.values()]}"
            )
            return result
        except Exception as e:
            print_red(f"[ChatService] Person uniqueness check failed: {e}")
            import traceback

            traceback.print_exc()
            return {}

    def _inject_person_links(
        self,
        answer_text: str,
        unique_persons: Dict[str, Dict],
        cited_sources: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict], str]:
        """
        Inject markdown person links into the answer body for persons with unique names.
        First occurrence: [Name (Party)](/mp/intressent_id), subsequent: [Name](/mp/id).
        Skips the "Källor" section so citation lines are not modified.

        When `cited_sources` is provided, a name is only wrapped if the paragraph
        containing it has at least one `[N]` citation pointing to a source whose
        speaker matches that name. This prevents us from turning a wrong attribution
        (e.g. LLM wrote "Lena Hallengren (M)" next to a citation that's actually by
        Hillevi Larsson) into a misleadingly authoritative portrait link.
        """
        import re as _re

        if not unique_persons:
            return [], answer_text

        # Split off the Sources section to avoid wrapping names inside citation lines.
        parts = _re.split(
            r"(\n#+\s*K[äa]llor)", answer_text, maxsplit=1, flags=_re.IGNORECASE
        )
        body = parts[0]
        tail = "".join(parts[1:])  # "## Källor\n..." or empty

        used_ids: set = set()
        print_yellow(
            f"[ChatService] Injecting links for {len(unique_persons)} persons in answer body ({len(body)} chars)"
        )

        # Paragraph-aware wrap: for each paragraph, only inject links for names
        # that a citation in that paragraph actually supports.
        from backend.services.attribution import paragraph_supports_name

        paragraphs = body.split("\n\n")
        rebuilt: List[str] = []
        for para in paragraphs:
            new_para = para
            for iid, info in unique_persons.items():
                name = info["name"]
                if cited_sources is not None and not paragraph_supports_name(
                    new_para, name, cited_sources
                ):
                    continue
                pattern = _re.compile(
                    r"(?<!\[)(?<!\(/)" + _re.escape(name) + r"(?!\])", _re.UNICODE
                )

                def make_replace(iid=iid, name=name):
                    def replace(m):
                        used_ids.add(iid)
                        return f"[{name}](/mp/{iid})"

                    return replace

                new_para = pattern.sub(make_replace(), new_para)
            rebuilt.append(new_para)
        body = "\n\n".join(rebuilt)

        persons_list = [
            {"intressent_id": iid, **unique_persons[iid]} for iid in used_ids
        ]
        return persons_list, body + tail


# ---- Test code ----
if __name__ == "__main__":
    service = ChatService()
    print("Registered tools:")
    for tool in service.tools:
        print(
            f" - {tool['function']['name']} - {tool['function']['description'][:100]}..."
        )
    test_messages = [
        {"role": "user", "content": "Hur många gånger har kärnkraft nämnts?"},
    ]
