"""
Combined summarization + tagging pipeline for all riksdag speeches.

Reads speeches that are missing a summary OR tags from PostgreSQL, calls the
vLLM server for each one using a multi-turn chat (summary → arguments → tags),
then writes the results back.  Designed to run in the background for days:

    nohup python scripts/summarize_and_tag.py >> logs/summarize_and_tag.log 2>&1 &
    echo $! > logs/summarize_and_tag.pid

Resumable: only processes speeches where summary IS NULL OR tags IS NULL.
Concurrency: 4 worker threads, each with its own LLM instance.

Multi-turn strategy: The speech is sent once and cached by vLLM. Three
subsequent turns ask for summary, arguments, and tags separately, reusing
the KV cache of all previous turns.
"""
from pathlib import Path

import difflib
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List
from packages.colorprinter import print_red, print_green

def log(msg):
    print(msg, flush=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap  # noqa: E402,F401  — sets cwd and sys.path to the project root

from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# Suppress verbose HTTP and library logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

from packages.llm import LLM
from postgres_client import pg

# ─────────────────────────────────────────────────────────────────────────────
# Tags
# ─────────────────────────────────────────────────────────────────────────────

with open("political_subjecs.json") as f:
    political_topics: dict = json.load(f)

VALID_TAGS = set(t.upper() for t in political_topics.keys())

TAGS_LIST = "\n".join(
    f"  {key.upper()}: {desc}" for key, desc in political_topics.items()
)

# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""Du är ett analysverktyg för svenska riksdagsanföranden.

Du kommer att analysera ett anförande i tre steg: sammanfattning, argument och ämnestaggar.
Vänta på instruktioner för varje steg.

Tillgängliga ämnestaggar (används i sista steget):
{TAGS_LIST}
"""

SUMMARY_INSTRUCTION = (
    "Skriv en kortfattad sammanfattning av anförandet på 2–4 meningar på korrekt, saklig svenska. "
    "Återge talarens huvudståndpunkt och viktigaste budskap. "
    "Börja gärna med \"Talare (Parti) ...\". "
    "Utgå bara från det som faktiskt sägs eller tydligt framgår av anförandet. "
    "Svara med bara sammanfattningstexten, ingen annan text."
)

ARGUMENTS_INSTRUCTION = (
    "Lista nu de viktigaste argumenten som framförs i anförandet.\n\n"
    "Regler:\n"
    "- Ett argument ska vara en kort, självständig mening på svenska.\n"
    "- Ett argument ska uttrycka ett tydligt politiskt krav, förslag, ställningstagande, "
    "prioritering, försvar av en linje eller kritik med tydlig alternativ linje.\n"
    "- Varje argument ska uttrycka exakt en sak.\n"
    "- Prioritera de argument som är mest centrala i anförandet.\n"
    "- Slå ihop upprepningar och närliggande omformuleringar.\n\n"
    "Ta INTE med:\n"
    "- bakgrundsbeskrivningar\n"
    "- allmänt vedertagna sakpåståenden\n"
    "- rena konsekvensbeskrivningar utan tydlig politisk linje\n"
    "- exempel, motiveringar eller förklaringar som bara stöder ett annat argument\n"
    "- retoriska frågor, artighetsfraser eller allmän kritik utan tydlig ståndpunkt\n\n"
    "Om anförandet inte innehåller några tydliga politiska argument, svara med ett tomt JSON-array.\n\n"
    'Svara BARA med ett JSON-array: ["Argument 1", "Argument 2"]'
)

VALID_TAGS_STR = ", ".join(sorted(VALID_TAGS))

TAGS_INSTRUCTION = (
    "Välj nu 1–3 ämnestaggar som bäst beskriver anförandets huvudämne(n).\n\n"
    f"Giltiga taggar (ENDAST dessa får användas, exakt som de skrivs):\n{VALID_TAGS_STR}\n\n"
    "Regler:\n"
    "- Använd ENDAST taggar ur listan ovan — inga egna påhittade taggar.\n"
    "- Om anförandet är ett kort artighetsyttrande, procedurfråga eller saknar politiskt innehåll: svara med ett tomt array [].\n"
    "- Om flera verkar möjliga, välj de mest konkreta och textnära.\n\n"
    'Svara BARA med ett JSON-array: ["TAGG1"] eller [] om inget passar.'
)

# ─────────────────────────────────────────────────────────────────────────────
# Context fetching (for replies)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_reply_context(debate: str, sequence: int) -> list[dict]:
    """
    For a reply, fetch up to two context speeches (summaries only):
    1. The debate opener (sequence == 1)
    2. The immediately preceding talk (sequence - 1)
    """
    context = []

    openers = pg.execute(
        """
        SELECT speaker_name, party, sequence, summary
        FROM speeches
        WHERE debate = %s AND sequence = 1
        LIMIT 1
        """,
        (debate,),
    )
    opener = openers[0] if openers else None

    prevs = pg.execute(
        """
        SELECT speaker_name, party, sequence, summary
        FROM speeches
        WHERE debate = %s AND sequence = %s
        LIMIT 1
        """,
        (debate, sequence - 1),
    )
    prev = prevs[0] if prevs else None

    if opener:
        context.append(opener)
    if prev and (not opener or prev.get("sequence") != opener.get("sequence")):
        context.append(prev)

    return context


def get_context_text(doc: dict) -> str:
    summary = (doc.get("summary") or "").strip()
    if summary:
        return summary
    return "(ingen sammanfattning tillgänglig)"


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────

def build_speech_message(talk: dict) -> str:
    """Build the user message that presents the speech to analyse."""
    lines = []

    section_title = (talk.get("section_title") or "").strip()
    if section_title:
        lines.append(f"Avsnittsrubrik: {section_title}")

    speaker_name = (talk.get("speaker_name") or "Okänd").strip()
    party = (talk.get("party") or "").strip()
    lines.append(f"Talare: {speaker_name} ({party})")

    if talk.get("is_reply"):
        debate = talk.get("debate")
        nr = talk.get("sequence")
        if debate and nr:
            ctx_talks = fetch_reply_context(debate, nr)
            if ctx_talks:
                lines.append(
                    "\nDetta är en is_reply. Nedan följer korta sammanfattningar av de "
                    "föregående anförandena som repliken sannolikt syftar på:"
                )
                for ctx in ctx_talks:
                    ctx_talare = ctx.get("speaker_name", "Okänd")
                    ctx_parti = ctx.get("party", "")
                    text = get_context_text(ctx)
                    lines.append(f"- {ctx_talare} ({ctx_parti}): {text}")

    lines.append("\nAnförande:")
    lines.append(talk["text"])

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Tag fuzzy correction
# ─────────────────────────────────────────────────────────────────────────────

def fuzzy_correct_tag(tag: str) -> str | None:
    """
    If `tag` is not a valid tag but is close enough to exactly one valid tag
    (difflib ratio >= 0.82), return the corrected tag. Otherwise return None.
    """
    matches = difflib.get_close_matches(tag, VALID_TAGS, n=1, cutoff=0.82)
    if matches:
        corrected = matches[0]
        return corrected
    return None


def resolve_tags(raw_tags: list[str]) -> tuple[list[str], list[str]]:
    """
    Split raw_tags into (valid, still_invalid) after attempting fuzzy correction.
    """
    valid, invalid = [], []
    for tag in raw_tags:
        if tag in VALID_TAGS:
            valid.append(tag)
        else:
            corrected = fuzzy_correct_tag(tag)
            if corrected:
                valid.append(corrected)
            else:
                invalid.append(tag)
    return valid, invalid


# ─────────────────────────────────────────────────────────────────────────────
# Multi-turn LLM call
# ─────────────────────────────────────────────────────────────────────────────

_GUIDED_STRING_LIST = {"type": "array", "items": {"type": "string"}}


def _parse_string_list(content: str) -> list[str]:
    """
    Extract a list[str] from model output as robustly as possible.
    Tries in order: raw_decode (handles trailing text), then regex extraction of
    the first [...] block. Returns [] if nothing parseable is found.
    """
    text = content.strip()
    # Strip markdown code fences
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        text = m.group(1).strip()
    # raw_decode stops at the first complete JSON value, ignoring trailing garbage
    try:
        val, _ = json.JSONDecoder().raw_decode(text)
        if isinstance(val, list):
            return [s for s in val if isinstance(s, str)]
    except (json.JSONDecodeError, ValueError):
        pass
    # Find the first [...] block (handles leading prose before the array)
    m = re.search(r"\[[\s\S]*?\]", text)
    if m:
        try:
            val = json.loads(m.group(0))
            if isinstance(val, list):
                return [s for s in val if isinstance(s, str)]
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _call(llm: LLM, messages: list[dict]) -> str:
    result = llm.generate(messages=messages, temperature=0.2, think=False, model="smart")
    if isinstance(result, str):
        # generate() returns a plain string on API failure
        if "Remote API failed" in result or "An error occurred" in result:
            raise RuntimeError(f"LLM API error: {result}")
        return result
    content = getattr(result, "content", None)
    if content is None:
        return str(result)
    return content if isinstance(content, str) else str(content)


def _call_structured(llm: LLM, messages: list[dict]) -> tuple[list[str], str]:
    """
    Call LLM using vLLM guided_json decoding to guarantee a list[str] response.
    Returns (parsed_list, raw_content) where raw_content is used for conversation history.
    Raises RuntimeError on API failure.
    """
    result = llm.generate(
        messages=messages,
        temperature=0.2,
        think=False,
        model="smart",
        extra_body={"guided_json": _GUIDED_STRING_LIST, "repetition_penalty": 1.2},
    )
    if isinstance(result, str):
        raise RuntimeError(f"LLM API error: {result}")
    content = getattr(result, "content", "") or ""
    return _parse_string_list(content), content


def get_summary(llm: LLM, messages: list[dict], speech_msg: str, max_retries: int = 2) -> tuple[str, list[dict]]:
    """
    Turn 1: speech + summary instruction in one user message (avoids consecutive user msgs).
    Returns (summary_text, updated_messages).
    """
    first_user = speech_msg + "\n\n" + SUMMARY_INSTRUCTION
    msgs = messages + [{"role": "user", "content": first_user}]

    for attempt in range(1 + max_retries):
        content = _call(llm, msgs)
        summary = content.strip()
        if summary:
            msgs.append({"role": "assistant", "content": summary})
            return summary, msgs
        if attempt < max_retries:
            msgs.append({"role": "assistant", "content": content})
            msgs.append({"role": "user", "content": "Svara med sammanfattningstexten."})

    return "", msgs


def get_arguments(llm: LLM, messages: list[dict]) -> tuple[list[str], list[dict]]:
    """
    Turn 2: ask for arguments. Returns (arguments_list, updated_messages).
    Structured output guarantees a valid list[str] — no JSON parsing retry needed.
    """
    msgs = messages + [{"role": "user", "content": ARGUMENTS_INSTRUCTION}]
    args, content = _call_structured(llm, msgs)
    msgs.append({"role": "assistant", "content": content})
    return args, msgs


def get_tags(llm: LLM, messages: list[dict], max_retries: int = 2) -> tuple[list[str], bool]:
    """
    Turn 3: ask for tags. Returns (tags_list, tagging_failed).
    Structured output guarantees valid JSON; retries are only needed when the model
    picks strings outside the allowed tag vocabulary.
    tagging_failed=True means retries were exhausted with persistent invalid tags.
    An intentional empty [] is tagging_failed=False.
    """
    msgs = messages + [{"role": "user", "content": TAGS_INSTRUCTION}]

    last_valid_tags: list[str] = []
    had_invalid = False

    for attempt in range(1 + max_retries):
        raw_tags, content = _call_structured(llm, msgs)
        raw_tags = [t.upper() for t in raw_tags if isinstance(t, str)]

        valid_tags, invalid = resolve_tags(raw_tags)
        if not invalid:
            return valid_tags, False

        had_invalid = True
        last_valid_tags = valid_tags

        if attempt < max_retries:
            correction = (
                f"Fel: taggarna {invalid} finns INTE i den godkända listan. "
                f"Du får ENDAST använda exakt dessa nycklar:\n"
                f"{VALID_TAGS_STR}\n"
                "Om inget passar: svara med []. "
                'Svara BARA med ett JSON-array: ["TAGG1"] eller []'
            )
            msgs.append({"role": "assistant", "content": content})
            msgs.append({"role": "user", "content": correction})

    # Retries exhausted with invalid tags — mark as failed so we retry later
    return last_valid_tags, had_invalid and len(last_valid_tags) == 0


def call_llm_multiturn(llm: LLM, talk: dict) -> tuple[str, list[str], list[str], bool]:
    """
    Run only the turns needed for fields that are NULL in the talk dict.
    Existing non-NULL values are reused verbatim to reconstruct conversation history
    so downstream turns can still benefit from KV caching.

    Returns (summary, tags, arguments, tagging_failed).
    """
    # Treat empty arrays ({}) the same as NULL — both mean "not yet generated"
    existing_summary = talk.get("summary") or None
    existing_arguments = talk.get("arguments") or None
    existing_tags = talk.get("tags") or None

    speech_msg = build_speech_message(talk)
    base_messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Turn 1: summary
    if existing_summary is None:
        summary, messages = get_summary(llm, base_messages, speech_msg)
    else:
        summary = existing_summary
        first_user = speech_msg + "\n\n" + SUMMARY_INSTRUCTION
        messages = base_messages + [
            {"role": "user", "content": first_user},
            {"role": "assistant", "content": existing_summary},
        ]

    # Turn 2: arguments
    if existing_arguments is None:
        arguments, messages = get_arguments(llm, messages)
    else:
        arguments = existing_arguments
        messages = messages + [
            {"role": "user", "content": ARGUMENTS_INSTRUCTION},
            {"role": "assistant", "content": json.dumps(existing_arguments, ensure_ascii=False)},
        ]

    # Turn 3: tags
    if existing_tags is None:
        tags, tagging_failed = get_tags(llm, messages)
    else:
        tags = existing_tags
        tagging_failed = False

    return summary, tags, arguments, tagging_failed


# ─────────────────────────────────────────────────────────────────────────────
# Worker
# ─────────────────────────────────────────────────────────────────────────────

_thread_local = threading.local()


def make_llm() -> LLM:
    return LLM(
        model="smart",
        think=False,
        temperature=0.2,
        chat=False,
        silent=True,
        extra_body={"repetition_penalty": 1.2},
    )


def get_worker_llm() -> LLM:
    """Return a per-thread LLM instance (created on first use per thread)."""
    if not hasattr(_thread_local, "llm"):
        _thread_local.llm = make_llm()
    return _thread_local.llm


def process_talk(talk: dict) -> bool:
    """Process a single talk. Returns True on success, False on error."""
    try:
        llm = get_worker_llm()
        summary, tags, arguments, tagging_failed = call_llm_multiturn(llm, talk)

        tagging_failed = tagging_failed or not summary
        # Only compute embedding if summary was newly generated
        embedding = pg.make_embeddings([summary])[0] if (summary and talk.get("summary") is None) else None
        pg.execute_void(
            """
            UPDATE speeches
            SET summary           = CASE WHEN summary IS NOT NULL THEN summary ELSE %s END,
                tags              = CASE WHEN array_length(tags, 1) > 0 THEN tags ELSE %s END,
                arguments         = CASE WHEN array_length(arguments, 1) > 0 THEN arguments ELSE %s END,
                tagging_failed    = %s,
                summary_embedding = COALESCE(summary_embedding, %s)
            WHERE id = %s
            """,
            (summary or None, tags, arguments, tagging_failed, embedding, talk["id"]),
        )
        return True
    except Exception as e:
        logger.error(f"Error processing talk {talk.get('id')}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

WORKERS = 4
BATCH_SIZE = 200  # fetch this many speeches at a time before re-querying


def fetch_batch() -> list[dict]:
    return pg.execute(
        """
        SELECT id, text, is_reply, debate, sequence,
               section_title, speaker_name, party,
               summary, tags, arguments
        FROM speeches
        WHERE (
            summary IS NULL
            OR tags IS NULL
            OR arguments IS NULL
        )
          AND tagging_failed IS NOT TRUE
          AND text IS NOT NULL
          AND LENGTH(text) >= 200
        ORDER BY date DESC NULLS LAST
        LIMIT %s
        """,
        (BATCH_SIZE,),
    )


def ensure_schema():
    """Add pipeline columns to speeches if they don't already exist."""
    for ddl in [
        "ALTER TABLE speeches ADD COLUMN IF NOT EXISTS tagging_failed BOOLEAN DEFAULT FALSE",
        "ALTER TABLE speeches ADD COLUMN IF NOT EXISTS arguments TEXT[]",
    ]:
        try:
            pg.execute_void(ddl)
        except Exception as e:
            logger.warning(f"Could not apply schema change: {e}")


def main():
    os.makedirs("logs", exist_ok=True)
    ensure_schema()
    logger.info("Starting summarize_and_tag pipeline …")

    total_processed = 0
    total_errors = 0
    start_time = time.time()

    while True:
        batch = fetch_batch()
        if not batch:
            logger.info("No more speeches to process. All done.")
            break

        batch_ok = 0
        batch_err = 0

        # get_worker_llm() creates one LLM per thread via threading.local()
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {executor.submit(process_talk, talk): talk for talk in batch}
            for future in as_completed(futures):
                ok = future.result()
                if ok:
                    batch_ok += 1
                else:
                    batch_err += 1

                total_processed += 1
                total_errors += 0 if ok else 1

        elapsed = time.time() - start_time
        rate = total_processed / (elapsed / 60) if elapsed > 0 else 0
        print(
            f"Processed: {total_processed} | Errors: {total_errors} | Rate: {rate:.1f}/min"
        )

    elapsed = time.time() - start_time
    rate = total_processed / (elapsed / 60) if elapsed > 0 else 0
    logger.info(
        f"=== DONE: {total_processed} processed, {total_errors} errors, {elapsed/3600:.1f}h total, {rate:.1f}/min ==="
    )


if __name__ == "__main__":
    main()
