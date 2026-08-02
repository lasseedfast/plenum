"""
MP Chat Service — role-plays as a specific Riksdag member, grounded in their actual speeches.
"""
from __future__ import annotations

import os
import json
import queue
import re as _re
import threading
from typing import Any, Callable, Dict, Generator, List, Optional, Sequence, Tuple

from packages.llm import LLM, get_tools, ChatCompletionMessage
from packages.colorprinter import *

from backend.services.chat import (
    FAST_MODEL,
    SMART_MODEL,
    WORKER_SYSTEM,
    SUMMARIZE_THRESHOLD,
    FinalAnswer,
    ChatService,
)
from backend.services.llm_tools import (
    SearchHitsResult,
    HitsResponse,
    _tool_structured_result,
)
from backend.services.provenance import (
    ProvenanceRegistry,
    parse_and_renumber_citations,
)

ChatMessage = Dict[str, Any]
ChatSource = Dict[str, Any]
ChatResponse = Dict[str, Any]


def _build_persona_system(person: Dict[str, Any], initial_talk: Optional[Dict[str, Any]] = None) -> str:
    tilltalsnamn = person.get("tilltalsnamn") or ""
    efternamn = person.get("efternamn") or ""
    namn = person.get("namn") or f"{tilltalsnamn} {efternamn}".strip()
    parti = person.get("parti") or "okänt parti"
    valkrets = person.get("valkrets") or "okänd valkrets"
    fodd_ar = person.get("fodd_ar") or ""
    intressent_id = person.get("intressent_id") or ""

    # Summarise current roles from personuppdrag (JSONB list of assignments)
    roles_text = ""
    uppdrag = person.get("personuppdrag")
    if isinstance(uppdrag, list):
        active = [
            u for u in uppdrag
            if isinstance(u, dict) and (not u.get("tom") or u.get("tom", "") == "")
        ]
        if active:
            role_lines = []
            for u in active[:5]:
                organ = u.get("organ_kod") or u.get("typ") or ""
                roll = u.get("roll_kod") or ""
                if organ or roll:
                    role_lines.append(f"- {roll} i {organ}".strip(" i"))
            if role_lines:
                roles_text = "\nDina nuvarande uppdrag:\n" + "\n".join(role_lines)

    birth_text = f"\nFödd: {fodd_ar}" if fodd_ar else ""

    initial_talk_text = ""
    if initial_talk:
        talk_date = initial_talk.get("datum") or ""
        talk_topic = initial_talk.get("avsnittsrubrik") or initial_talk.get("titel") or "ett anförande"
        talk_text = (initial_talk.get("anforandetext") or "")[:3000]
        initial_talk_text = f"""

## Startkontext: Samtalet startades från ett specifikt anförande

Anförande ({talk_date}, ämne: {talk_topic}):
\"\"\"{talk_text}\"\"\"

Om användaren ställer en allmän fråga kan du utgå från detta anförande.
"""

    return f"""Du är {tilltalsnamn} {efternamn}, riksdagsledamot för {parti} från {valkrets}.{birth_text}{roles_text}

**VIKTIGT: Du är en digital assistent, inte den riktiga {tilltalsnamn} {efternamn}.**
Detta är ett rollspel baserat på faktiska anföranden i riksdagen.

## KRITISK REGEL — Gäller alltid utan undantag

**Varje svar du ger måste antingen (a) anropa ett eller flera verktyg, eller (b) vara ditt fullständiga slutsvar.**

Du får ALDRIG:
- Beskriva vad du ska göra utan att göra det: "Jag kan söka...", "Låt mig undersöka...", "Vill du att jag..."
- Fråga användaren om du får söka mer — bara sök.
- Ge ett svar och sedan fråga om du ska fortsätta — antingen är svaret klart, eller söker du mer.

Om du inte har tillräckligt med material: anropa nästa verktyg direkt.

## Sökstrategi — kör alltid hela kedjan automatiskt

**Steg 1 — Sök {tilltalsnamn}s egna anföranden (ALLTID första steget):**
```
arango_search(query="<ämne>", intressent_ids=["{intressent_id}"], return_snippets=True, limit=10)
```
`return_snippets=True` ger bara korta utdrag — bra för att se om det finns träffar.
**Om du hittar relevanta träffar MÅSTE du sedan hämta full text med fetch_documents.**

**Steg 2 — Hämta full text för de mest relevanta träffarna:**
```
fetch_documents(_ids=["<_id från steg 1>", ...])
```
Utan full text kan du inte citera korrekt. Hoppa inte över detta steg.

**Steg 3 — Om < 3 relevanta träffar på {tilltalsnamn}:** sök automatiskt partiets linje (ingen fråga till användaren):
```
arango_search(query="<ämne>", parties=["{parti}"], limit=5)
```
Notera: utan `return_snippets=True` får du full text direkt — du behöver inte hämta separat.

**Steg 4 — Semantisk sökning som komplement vid behov:**
```
vector_search(query="<ämne>")
```

Du kör steg 2–4 på eget initiativ utan att fråga användaren.

## När du formulerar slutsvaret

- Svara i första person som {tilltalsnamn}.
- Referera naturligt till egna uttalanden: "Som jag sa i debatten om X (2019)..."
- Om du hänvisar till partikollegor: "...min kollega [namn] betonade att..." — väv in organiskt,
  och var tydlig att det är partiets linje, inte ditt eget direkta uttalande.
- Om du inte hittat egna anföranden om ämnet, säg det kort och gå direkt till partiets linje:
  "Jag har inte talat om detta i riksdagen, men {parti}s hållning är tydlig — [partiresultat]."
- Citera ALDRIG något du inte hittat via sökning.
- Svara alltid på svenska.
- Håll svaret konversationellt, inte som ett politikertal.
- Starta INTE med fraser som "Som en AI..." — det är redan klargjort.

## Källhänvisningar i svaret

Inkludera inline-källhänvisningar i formatet [src:ID] direkt efter påståenden som bygger på ett specifikt anförande. ID:t hittar du i verktygsresultaten (t.ex. [src:H40911]). Avsluta INTE med en separat "Källor"-sektion. Citera ALDRIG med [1], [2] numrering — använd ALLTID [src:ID].
{initial_talk_text}

## Dina tekniska identifierare
- Namn: {namn}
- Parti: {parti}
- intressent_id: {intressent_id}  ← använd detta i intressent_ids-parametern
"""


def _collect_sources_from_payload(payload: Dict[str, Any], collected_sources: List[ChatSource]) -> None:
    """Extract sources from an arango_search payload dict and append to collected_sources."""
    results = payload.get("results", [])
    for item in results:
        if not isinstance(item, dict):
            continue
        item_id = item.get("_id")
        if not item_id:
            continue
        collected_sources.append({
            "_id": item_id,
            "chunk_index": item.get("chunk_index", -1),
            "heading": item.get("titel") or item.get("heading"),
            "debateurl": item.get("debateurl") or item.get("url_session"),
            "snippet": item.get("snippet") or item.get("snippet_long") or "",
            "speaker": item.get("speaker") or item.get("talare"),
            "party": item.get("party") or item.get("parti"),
            "intressent_id": item.get("intressent_id"),
            "date": item.get("date") or item.get("datum"),
        })


def _collect_persons_from_results(results: List[Any], collected_persons: Dict[str, Dict]) -> None:
    """Extract intressent_id / speaker / party from search result items."""
    for item in results:
        if not isinstance(item, dict):
            continue
        iid = item.get("intressent_id")
        name = item.get("speaker") or item.get("talare")
        party = item.get("party") or item.get("parti") or ""
        if iid and name and iid not in collected_persons:
            collected_persons[iid] = {"name": name, "party": party}


class MpChatService:
    """
    Chat service that role-plays as a specific Riksdag member.

    Fetches the MP's profile and speeches, then answers questions in first person
    grounded in their actual parliamentary record.
    """

    def __init__(self, intressent_id: str, initial_talk_id: Optional[str] = None,
                 provider_override=None) -> None:
        from postgres_client import pg

        self.intressent_id = intressent_id

        # Fetch person data
        rows = pg.execute(
            """SELECT intressent_id, namn, tilltalsnamn, efternamn, parti, valkrets,
                      status, fodd_ar, kon, bild_url_192, personuppdrag
               FROM people WHERE intressent_id = %s""",
            (intressent_id,)
        )
        if not rows:
            raise ValueError(f"Person {intressent_id} not found")
        self.person = dict(rows[0])

        # Optionally fetch initial talk for context
        initial_talk = None
        if initial_talk_id:
            talk_id = initial_talk_id.replace("talks/", "")
            talk_rows = pg.execute(
                "SELECT id, datum, avsnittsrubrik, titel, anforandetext FROM talks WHERE id = %s",
                (talk_id,)
            )
            if talk_rows:
                initial_talk = dict(talk_rows[0])

        persona_system = _build_persona_system(self.person, initial_talk)

        # A user-supplied provider takes over both roles; otherwise the
        # server's own models. Same contract as /api/chat — the key is
        # request-scoped and never stored.
        if provider_override is not None:
            from backend.services.llm_override import resolve as _resolve_provider

            provider = _resolve_provider(provider_override)
            llm_url = provider.base_url
            api_key = provider_override.api_key or None
            smart_model = provider_override.smart_model or provider.smart_model
            fast_model = provider_override.fast_model or provider.fast_model or smart_model
        else:
            llm_url = os.getenv("LLM_DIRECT_URL")
            api_key = None
            smart_model, fast_model = SMART_MODEL, FAST_MODEL

        self.smart_llm = LLM(model=smart_model, system_message=persona_system,
                             temperature=0.3, base_url=llm_url, api_key=api_key)
        self.fast_llm = LLM(model=fast_model, system_message=WORKER_SYSTEM,
                            temperature=0.0, base_url=llm_url, api_key=api_key)
        self.tools = get_tools(exclude_tools=["sql_query"])
        self.max_tool_iterations = 14

    def stream_chat_response(
        self,
        messages: Sequence[ChatMessage],
    ) -> Generator[Dict[str, Any], None, None]:
        event_queue: queue.Queue[Dict[str, Any]] = queue.Queue()

        def emit(event: Dict[str, Any]) -> None:
            event_queue.put(event)

        def run() -> None:
            try:
                result = self._get_chat_response(messages, event_callback=emit)
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

    def _get_chat_response(
        self,
        messages: Sequence[ChatMessage],
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> ChatResponse:
        namn = self.person.get("namn") or ""
        tilltalsnamn = self.person.get("tilltalsnamn") or namn.split()[0] if namn else ""

        full_messages = [{"role": "system", "content": self.smart_llm.system_message}] + list(messages)

        # Build the enriched user question
        latest_user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                latest_user = msg.get("content", "").strip()
                break

        # Detect simple greetings that don't need search
        GREETING_WORDS = {"hej", "tjena", "hallå", "hejsan", "tack", "ok", "okej", "bra", "hej!"}
        is_greeting = latest_user.lower().strip().rstrip("!?.") in GREETING_WORDS or len(latest_user.split()) <= 2

        if is_greeting:
            search_reminder = ""
        else:
            search_reminder = (
                f"\n\n**OBLIGATORISKT:** Anropa arango_search med intressent_ids=[\"{self.intressent_id}\"] "
                f"INNAN du svarar. Hämta sedan full text med fetch_documents om du bara fick snippets. "
                f"Alla sakpåståenden MÅSTE grunda sig i faktiska anföranden du hittat via sök."
            )

        enriched_question = (
            f"En medborgare frågar {tilltalsnamn}: *{latest_user}*{search_reminder}"
        )

        collected_sources: List[ChatSource] = []
        collected_persons: Dict[str, Dict] = {}
        registry = ProvenanceRegistry()
        response_message, _ = self._run_tool_loop(
            full_messages,
            collected_sources,
            collected_persons,
            user_question=enriched_question,
            event_callback=event_callback,
            registry=registry,
        )

        answer_text = (
            response_message.final_answer
            if isinstance(response_message, FinalAnswer)
            else str(response_message)
        ).strip()

        # Validate [src:ID] citations against the registry, strip invalid ones,
        # renumber to [1],[2]. MP chat exposes sources via a button, so strip
        # the "Källor" section that parse_and_renumber_citations appends.
        validated_answer, cited_sources, unique_cited_ids, invalid_ids = (
            parse_and_renumber_citations(answer_text, registry)
        )
        validated_answer = _re.split(r"\n+#{1,3}\s*K[äa]ll[ao]r", validated_answer)[0].rstrip()
        if invalid_ids:
            print_yellow(f"[MpChat] Dropped invalid citation IDs: {invalid_ids}")
        fallback_used = not unique_cited_ids and registry.size() > 0
        print_green(
            f"[MpChat] registered: {registry.size()} sources | "
            f"cited: {len(unique_cited_ids)} | "
            f"invalid dropped: {len(invalid_ids)} | "
            f"fallback: {'yes' if fallback_used else 'no'}"
        )

        all_persons = {**collected_persons, **registry.get_persons()}
        unique_persons = self._get_unique_name_persons(all_persons)
        persons, validated_answer = self._inject_person_links(validated_answer, unique_persons)
        print_green(
            f"[MpChat] Completed answer with {len(cited_sources)} cited sources, "
            f"{len(persons)} person links."
        )

        return {"answer": validated_answer, "sources": cited_sources, "persons": persons, "tables": [], "focus_ids": []}

    def _run_tool_loop(
        self,
        messages: Sequence[ChatMessage],
        collected_sources: List[ChatSource],
        collected_persons: Dict[str, Dict],
        user_question: str = "",
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        registry: Optional[ProvenanceRegistry] = None,
    ) -> Tuple[FinalAnswer, List[ChatMessage]]:
        current_messages: List[ChatMessage] = list(messages)

        for i in range(self.max_tool_iterations):
            if i == self.max_tool_iterations - 1:
                current_messages.append({
                    "role": "user",
                    "content": "Du har nått maximalt antal verktygsanrop. Ge ditt svar nu baserat på det du har hittat."
                })

            think_now = (i == 0)
            gen_kwargs = {"messages": current_messages, "think": think_now, "auto_execute_tools": False}
            if getattr(self, "tools", None):
                gen_kwargs["tools"] = self.tools
            response: ChatCompletionMessage = self.smart_llm.generate(**gen_kwargs)

            tool_calls = getattr(response, "tool_calls", None)

            if tool_calls:
                # Emit brief status narration
                narration = getattr(response, "content", None)
                if not narration or not isinstance(narration, str) or not narration.strip():
                    narration = getattr(response, "reasoning_content", None)
                if narration and isinstance(narration, str) and narration.strip():
                    short = narration.strip()
                    if len(short) > 150:
                        short = short[:150].rstrip() + "…"
                    if event_callback:
                        event_callback({"type": "status", "message": short})

                # Append the assistant turn with tool_calls (OpenAI spec requires this
                # before any role:tool messages).
                current_messages.append({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
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
                        for tc in tool_calls
                    ],
                })

                tool_result_messages: List[Dict[str, Any]] = []
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = tool_call.function.arguments
                    if isinstance(tool_args, str):
                        try:
                            tool_args = json.loads(tool_args)
                        except json.JSONDecodeError:
                            tool_args = {}

                    if event_callback:
                        event_callback({"type": "tool_call", "tool": tool_name})

                    print_blue(f"[MpChat] Tool: {tool_name} args: {tool_args}")

                    tool_func = self._get_tool_function(tool_name)
                    if tool_func is None:
                        tool_result_string = f"ERROR: Tool '{tool_name}' not found."
                        tool_result = None
                    else:
                        _tool_structured_result.set(None)
                        try:
                            tool_result = tool_func(**tool_args)
                        except Exception as e:
                            print_red(f"[MpChat] Exception in tool '{tool_name}': {e}")
                            import traceback; traceback.print_exc()
                            tool_result = f"ERROR: {e}"

                        structured = _tool_structured_result.get()

                        # Handle structured results (SearchHitsResult / HitsResponse)
                        # produced by new-style tool functions via ContextVar.
                        if structured is not None and isinstance(structured, (SearchHitsResult, HitsResponse)):
                            hits_response = (
                                structured.response
                                if isinstance(structured, SearchHitsResult)
                                else structured
                            )
                            # Register in provenance registry
                            if registry is not None:
                                ChatService._register_hits_in_registry(hits_response, registry, tool_name)
                            # Collect sources and persons
                            for hit in hits_response.hits:
                                meta = hit.metadata or {}
                                iid = meta.get("intressent_id")
                                collected_sources.append({
                                    "_id": hit.id or "",
                                    "chunk_index": meta.get("chunk_index", -1),
                                    "heading": meta.get("titel"),
                                    "debateurl": meta.get("debateurl"),
                                    "snippet": hit.snippet or "",
                                    "speaker": hit.speaker,
                                    "party": hit.party,
                                    "intressent_id": iid,
                                    "date": hit.date,
                                })
                                if iid and hit.speaker and iid not in collected_persons:
                                    collected_persons[iid] = {"name": hit.speaker, "party": hit.party or ""}
                            # to_string() embeds [src:ID] tags per document
                            tool_result_string = hits_response.to_string()
                        else:
                            tool_result_string = self._handle_tool_result(
                                tool_name, tool_args, tool_result, collected_sources, collected_persons
                            )

                    # ── Guard: arango_search without person/party filter ────────
                    if tool_name == "arango_search" and isinstance(tool_args, dict):
                        has_person = bool(
                            tool_args.get("intressent_ids") or tool_args.get("people")
                        )
                        has_party = bool(tool_args.get("parties"))
                        if not has_person and not has_party:
                            tool_result_string += (
                                f"\n\n[SYSTEMVARNING: Sökningen ovan filtrerade INTE på person "
                                f"eller parti. Resultaten kan komma från vem som helst. "
                                f"Sök igen med intressent_ids=[\"{self.intressent_id}\"] för "
                                f"{self.person.get('namn','personen')}s egna anföranden, eller "
                                f"parties=[\"{self.person.get('parti','')}\"] för partiets linje.]"
                            )

                    # ── Guard: snippets only → must fetch full text before citing ──
                    if tool_name == "arango_search" and isinstance(tool_args, dict) and tool_args.get("return_snippets"):
                        result_ids = []
                        if isinstance(tool_result, dict):
                            results_list = tool_result.get("results") or tool_result.get("payload", {}).get("results", [])
                            result_ids = [
                                r["_id"] for r in results_list
                                if isinstance(r, dict) and r.get("_id")
                            ]
                        if result_ids:
                            tool_result_string += (
                                f"\n\n[SYSTEMINFO: Du sökte med return_snippets=True och fick bara utdrag. "
                                f"Om du vill basera svaret på dessa anföranden MÅSTE du hämta full text först: "
                                f"fetch_documents(_ids={result_ids[:5]})]"
                            )

                    if len(tool_result_string) > SUMMARIZE_THRESHOLD:
                        tool_result_string = self._summarize(tool_name, tool_result_string, user_question)
                    elif len(tool_result_string) > 12000:
                        tool_result_string = f"{tool_result_string[:12000]} (...) [truncated]"

                    tool_result_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": f"Resultat från {tool_name}:\n{tool_result_string}.",
                    })

                # Add citation/search reminder to the last tool message only.
                if tool_result_messages and user_question:
                    reminder = (
                        "\n\n**KRITISKT:** Antingen anropar du nästa verktyg NU, eller ger du ditt slutsvar. "
                        "Du får INTE fråga användaren om du ska söka mer — bara sök. "
                        "Du får INTE beskriva vad du planerar att göra. "
                        "Om materialet är otillräckligt: anropa arango_search med parties eller vector_search direkt. "
                        "I slutsvaret: svara som {namn} i första person och inkludera [src:ID]-citat direkt efter varje påstående. "
                        "ID:n hittar du i verktygsresultaten ovan (t.ex. [src:H40911]). "
                        "Avsluta INTE med en separat 'Källor'-sektion."
                    ).format(namn=self.person.get("tilltalsnamn") or self.person.get("namn", ""))
                    tool_result_messages[-1]["content"] += reminder

                current_messages.extend(tool_result_messages)
                continue

            elif response.content:
                final_content = getattr(response, "content", "")
                return FinalAnswer(
                    final_answer=final_content,
                    explanation="Direct answer from MP persona."
                ), current_messages

            else:
                # Empty response — append a forcing message so the next iteration
                # has new context to act on.
                last_tool_msg = next(
                    (m for m in reversed(current_messages) if m.get("role") == "tool"),
                    None,
                )
                last_had_error = last_tool_msg and "ERROR" in last_tool_msg.get("content", "")
                print_red(f"[MpChat] Iteration {i}: model returned empty response.")
                if last_had_error:
                    current_messages.append({
                        "role": "user",
                        "content": "Det senaste verktygsanropet returnerade ett fel. Rätta felet och försök igen.",
                    })
                else:
                    current_messages.append({
                        "role": "user",
                        "content": "Anropa ett verktyg om du behöver mer information, eller ge ditt slutsvar nu.",
                    })

        return FinalAnswer(
            final_answer="Förlåt, jag kunde inte hitta tillräckligt med information för att svara på det.",
            explanation="Max iterations reached."
        ), current_messages

    def _handle_tool_result(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: Any,
        collected_sources: List[ChatSource],
        collected_persons: Dict[str, Dict],
    ) -> str:
        """
        Normalise the return value of any tool into a string for the LLM,
        and side-effect: append sources to collected_sources and persons to
        collected_persons when applicable.

        arango_search has three possible return shapes:
          1. Normal call (no flags):  returns payload dict directly
             { "results": [...], "stats": {...}, "limit_reached": bool, ... }
          2. surface_results=True:    {"type": "search_results", "payload": {...}, "surface_only": True}
          3. results_to_user=True:    {"type": "search_results", "payload": {...}}
        """
        if not isinstance(tool_result, dict):
            # str from vector_search, list from fetch_documents, etc.
            return str(tool_result)

        t = tool_result.get("type")

        # ── arango_search: wrapped return (surface_results / results_to_user) ──
        if t == "search_results":
            payload = tool_result.get("payload", {})
            _collect_sources_from_payload(payload, collected_sources)
            _collect_persons_from_results(payload.get("results", []), collected_persons)
            return json.dumps(payload, ensure_ascii=False)

        # ── arango_search: direct payload (normal call, no special flags) ──
        if "results" in tool_result and isinstance(tool_result.get("results"), list):
            _collect_sources_from_payload(tool_result, collected_sources)
            _collect_persons_from_results(tool_result.get("results", []), collected_persons)
            return json.dumps(tool_result, ensure_ascii=False)

        # ── database_query: stats surface ──
        if t == "stats_results":
            return json.dumps(tool_result.get("rows", []), ensure_ascii=False)

        # ── share_insight ──
        if t == "insight":
            return "Insikt noterad."

        # Fallback
        return json.dumps(tool_result, ensure_ascii=False)

    def _get_unique_name_persons(self, persons: Dict[str, Dict]) -> Dict[str, Dict]:
        """
        Look up each collected person by intressent_id to get the canonical DB name,
        then keep only those whose name is unique in the people table.
        """
        if not persons:
            return {}
        from postgres_client import pg
        iids = list(persons.keys())
        print_yellow(f"[MpChat] Person lookup: {len(iids)} intressent_ids: {iids}")
        try:
            id_rows = pg.execute(
                "SELECT intressent_id, namn, parti FROM people WHERE intressent_id = ANY(%s)",
                (iids,)
            )
            if not id_rows:
                return {}
            names = [r["namn"] for r in id_rows]
            unique_rows = pg.execute(
                "SELECT namn FROM people WHERE LOWER(namn) = ANY(%s) GROUP BY namn HAVING COUNT(*) = 1",
                ([n.lower() for n in names],)
            )
            unique_names_lower = {r["namn"].lower() for r in unique_rows}
            result: Dict[str, Dict] = {}
            for row in id_rows:
                if row["namn"].lower() in unique_names_lower:
                    result[row["intressent_id"]] = {
                        "name": row["namn"],
                        "party": row["parti"] or ""
                    }
            print_green(f"[MpChat] {len(result)} persons will be linked: {[v['name'] for v in result.values()]}")
            return result
        except Exception as e:
            print_red(f"[MpChat] Person uniqueness check failed: {e}")
            return {}

    def _inject_person_links(self, answer_text: str, unique_persons: Dict[str, Dict]) -> Tuple[List[Dict], str]:
        """
        Inject markdown person links for persons with unique names.
        First occurrence: [Name (Party)](/mp/id), subsequent: [Name](/mp/id).
        Skips the "Källor" section so citation lines are not modified.
        Returns (persons_list, validated_answer).
        """
        if not unique_persons:
            return [], answer_text

        parts = _re.split(r'(\n#+\s*K[äa]llor)', answer_text, maxsplit=1, flags=_re.IGNORECASE)
        body = parts[0]
        tail = "".join(parts[1:])

        used_ids: set = set()

        for iid, info in unique_persons.items():
            name = info["name"]
            pattern = _re.compile(
                r'(?<!\[)(?<!\(/)' + _re.escape(name) + r'(?!\])',
                _re.UNICODE
            )

            def make_replace(iid=iid, name=name):
                def replace(m):
                    used_ids.add(iid)
                    return f"[{name}](/mp/{iid})"
                return replace

            body = pattern.sub(make_replace(), body)

        persons_list = [
            {"intressent_id": iid, **unique_persons[iid]}
            for iid in used_ids
        ]
        return persons_list, body + tail

    def _summarize(self, tool_name: str, result: str, question: str) -> str:
        MAX_INPUT = 40_000
        truncated = len(result) > MAX_INPUT
        input_text = result[:MAX_INPUT]
        truncation_note = f"\n[...truncated at {MAX_INPUT} chars]" if truncated else ""
        prompt = (
            f"The tool '{tool_name}' returned the following. Question: '{question}'\n\n"
            f"RAW RESULT:\n{input_text}{truncation_note}\n\n"
            "Write a concise summary of the key findings. Include names, dates, quotes, _id values, numbers."
        )
        response = self.fast_llm.generate(
            messages=[
                {"role": "system", "content": WORKER_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            think=False,
        )
        summary = getattr(response, "content", str(response))
        return f"[Summary of {tool_name} — original {len(result)} chars]\n{summary}"

    def _get_tool_function(self, tool_name: str):
        for tool in self.tools:
            if hasattr(tool, "name") and tool.name == tool_name:
                return getattr(tool, "function", None)
        try:
            import backend.services.llm_tools as llm_tools
            return getattr(llm_tools, tool_name, None)
        except Exception:
            return None
