from __future__ import annotations

from collections.abc import Iterable

from parliament import PARLIAMENT


def datestring_to_iso(date_str: str) -> str:
    day, month_name, year = date_str.split(" ")
    return f"{year}-{PARLIAMENT.language.months[month_name]}-{day.zfill(2)}"


def _cleanup(text: str) -> str:
    return text.replace("Fru talman! ", "").replace("Herr talman! ", "")


def _tokenize(text: str) -> list[str]:
    return text.split()


def make_snippet(text: str, search_terms: Iterable[str], long: bool = False) -> str:
    text = _cleanup(text or "")
    lowered = text.lower()
    terms = [term.strip().lower() for term in search_terms if term.strip()]
    if not terms:
        return text[:400] + ("..." if len(text) > 400 else "")
    snippet_segments = []
    words = _tokenize(text)
    lowered_words = _tokenize(lowered)
    window = 8 if not long else 32
    for term in terms:
        stripped = term.replace("*", "")
        if not stripped:
            continue
        if stripped in lowered_words:
            idx = lowered_words.index(stripped)
            start = max(idx - window, 0)
            end = min(idx + window, len(words))
            snippet_segments.append(" ".join(words[start:end]))
        elif stripped in lowered:
            pos = lowered.find(stripped)
            start = max(pos - window * 5, 0)
            end = min(pos + len(stripped) + window * 5, len(text))
            snippet_segments.append(text[start:end])
    if not snippet_segments:
        snippet_segments.append(" ".join(words[: window * 2]))
    glue = " ... "
    snippet = glue.join(snippet_segments)
    return f"...{snippet.strip()}..." if snippet else ""


def assign_party_highlight(party: str) -> str:
    return PARLIAMENT.party_highlight_color(party)
