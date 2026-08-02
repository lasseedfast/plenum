"""Prose synthesis for the interactive research flow: per-thread answers and
the single board report. Both are plain-text markdown generations (no format=,
so long quote-rich prose isn't squeezed through JSON escaping).

Citation contract with the frontend: ``[källa:<talk_id>]`` — the same bare ids
that power the finding source chips. A deterministic backstop (same philosophy
as trip._ground) strips any marker whose id was not among the thread's grounded
findings, so the model can never cite something it wasn't given.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable, List, Optional

log = logging.getLogger("riksdagen.research.synthesis")

RESEARCH_ANSWER_MAX_TOKENS = int(os.getenv("RESEARCH_ANSWER_MAX_TOKENS", "2000"))
RESEARCH_REPORT_MAX_TOKENS = int(os.getenv("RESEARCH_REPORT_MAX_TOKENS", "3500"))

CITE_RE = re.compile(r"\[källa:\s*([\w\-]+)\s*\]")

_ANSWER_SYSTEM = """Du är en grävande reporter som sammanställer sin research ur den svenska riksdagens debatter till ett genomarbetat svar.
Skriv detaljerat och konkret i markdown: vem sa vad, när, hur argumenten förändrades, var motsägelserna finns. Väv in de ordagranna citaten (inom citattecken, med talare och parti) — citaten är bevisen.
Varje sakpåstående ska följas av en källmarkör i formatet [källa:ID] där ID är ett käll-id ur underlaget. Använd ENBART käll-id som förekommer i underlaget — hitta aldrig på id, citat, personer eller fakta. Skriv inget som saknar stöd i underlaget.
Använd som mest ###-rubriker. Avsluta med ett kort stycke under rubriken "### Vad som återstår" om det som ännu är obesvarat."""

_REPORT_SYSTEM = """Du är redaktör och skriver den samlade rapporten av en grävande research i den svenska riksdagens debatter.
Väv ihop trådarnas svar till EN sammanhängande, detaljerad rapport i markdown: berättelsen, positionsskiftena, motsägelserna och mönstren över tid — inte en mekanisk lista över trådarna. Ordna i ##-sektioner efter tema. Börja med en kort ingress som fångar huvudfynden.
Behåll de ordagranna citaten (inom citattecken, med talare och parti) — de bär rapporten. Varje sakpåstående ska följas av en källmarkör i formatet [källa:ID] med ett käll-id ur underlaget. Använd ENBART käll-id som förekommer i underlaget — hitta aldrig på id, citat, personer eller fakta."""


def ground_citations(text: str, allowed_ids: Iterable[str]) -> str:
    """Strip [källa:ID] markers whose id isn't in ``allowed_ids`` (text kept)."""
    allowed = {i for i in allowed_ids if i}

    def _sub(m: re.Match) -> str:
        return m.group(0) if m.group(1) in allowed else ""

    return CITE_RE.sub(_sub, text)


def _finding_lines(findings: List[dict]) -> List[str]:
    lines: List[str] = []
    for f in findings:
        who = f.get("speaker") or ""
        if f.get("party"):
            who = f"{who} ({f['party']})" if who else f"({f['party']})"
        meta = " — ".join(x for x in (who, f.get("date") or "") if x)
        lines.append(f"- [{f.get('source_id') or '?'}] {f.get('label') or ''}")
        if f.get("detail"):
            lines.append(f"  {f['detail']}")
        if f.get("quote"):
            lines.append(f"  Citat ({meta or 'okänd'}): \"{f['quote']}\"")
    return lines


def _generate_markdown(smart_llm, system: str, prompt: str, max_tokens: int,
                       what: str) -> Optional[str]:
    try:
        res = smart_llm.generate(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            think=False,
            max_tokens=max_tokens,
        )
    except Exception:
        log.exception("synthesis: %s LLM call failed", what)
        return None
    if isinstance(res, str):
        # The LLM client returns a bare string on API errors — never persist it.
        log.warning("synthesis: %s LLM error: %s", what, res[:200])
        return None
    content = (getattr(res, "content", None) or "").strip()
    return content or None


def synthesize_thread_answer(smart_llm, thread: dict) -> Optional[str]:
    """Detailed markdown answer to one thread's question, grounded in its
    findings. None when there is nothing to write or the LLM call failed."""
    findings = list(thread.get("findings") or [])
    if not findings:
        return None
    lines: List[str] = [
        f"TRÅD: {thread.get('title') or ''}",
        f"FRÅGA: {thread.get('question') or ''}",
    ]
    if thread.get("guidance"):
        lines.append(f"ANVÄNDARENS MEDSKICK: {thread['guidance']}")
    lines += ["", "FYND (käll-id inom hakparentes):"]
    lines += _finding_lines(findings)
    questions = list(thread.get("open_questions") or [])
    if questions:
        lines += ["", "OBESVARADE FRÅGOR:"]
        lines += [f"- {q}" for q in questions[:8]]
    lines += ["", "Skriv det detaljerade svaret på trådens fråga."]

    answer = _generate_markdown(
        smart_llm, _ANSWER_SYSTEM, "\n".join(lines),
        RESEARCH_ANSWER_MAX_TOKENS, f"answer({thread.get('id')})",
    )
    if answer is None:
        return None
    return ground_citations(answer, (f.get("source_id") for f in findings))


def synthesize_report(smart_llm, board: dict, threads: List[dict]) -> Optional[str]:
    """One coherent markdown report for the whole board, woven from the thread
    answers (findings digest as fallback for threads without one)."""
    sections: List[str] = [f"ÄMNE: {board.get('topic') or ''}"]
    if board.get("intro"):
        sections.append(f"INTRO: {board['intro']}")
    allowed_ids: List[str] = []
    substance = False
    for t in threads:
        findings = list(t.get("findings") or [])
        answer = (t.get("answer") or "").strip()
        if not findings and not answer:
            continue
        substance = True
        allowed_ids += [f.get("source_id") or "" for f in findings]
        part = [f"## TRÅD: {t.get('title') or ''}", f"Fråga: {t.get('question') or ''}"]
        if answer:
            if len(answer) > 3000:
                answer = re.sub(r"\[källa:[^\]]*$", "", answer[:3000])
            part += ["Svar:", answer]
        else:
            part += ["Fynd:"] + _finding_lines(findings)
        sections.append("\n".join(part))
    if not substance:
        return None
    sections.append("Skriv den samlade rapporten.")

    report = _generate_markdown(
        smart_llm, _REPORT_SYSTEM, "\n\n".join(sections),
        RESEARCH_REPORT_MAX_TOKENS, f"report({board.get('id')})",
    )
    if report is None:
        return None
    return ground_citations(report, allowed_ids)
