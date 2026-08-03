"""
Language correction pipeline for riksdag talk arguments.

Reads speeches from 2002 onwards that have arguments (extracted by a small 9b model
with sometimes poor Swedish), sends them to the big-smart LLM for language
correction, and writes corrected arguments back.

Multi-turn strategy (mirrors summarize_and_tag.py):
  - Turn 1: send arguments as a keyed dict {argument_1: ..., argument_2: ...},
            ask for language correction. guided_json guarantees valid JSON output.
            Any argument the LLM can't parse is returned as null.
  - Turn 2 (if any nulls): same conversation, now also includes the full speech
            text so the LLM can re-derive meaning from source.
  - Any key still null after turn 2 falls back to the original text.

Tracks completion via `arguments_corrected` column (resumable).

    nohup python scripts/correct_arguments.py >> logs/correct_arguments.log 2>&1 &
    echo $! > logs/correct_arguments.pid
"""
from pathlib import Path

import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap  # noqa: E402,F401  — sets cwd and sys.path to the project root

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

from packages.llm import LLM
from postgres_client import pg

# ─────────────────────────────────────────────────────────────────────────────
# guided_json schema
# Each key maps to a corrected string, or null if the argument was incomprehensible.
# ─────────────────────────────────────────────────────────────────────────────

_GUIDED_NULLABLE_DICT = {
    "type": "object",
    "additionalProperties": {"type": ["string", "null"]},
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_keyed(arguments: list[str]) -> dict[str, str]:
    return {f"argument_{i+1}": arg for i, arg in enumerate(arguments)}


def _from_keyed(keyed: dict, arguments: list[str]) -> list[str | None]:
    """Map keyed dict back to a list, preserving original order. Missing keys → None."""
    return [keyed.get(f"argument_{i+1}") for i in range(len(arguments))]


# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
Du är ett språkgranskningsverktyg för svenska riksdagsanföranden.

Du får en dict med extraherade politiska argument skrivna av en liten AI-modell \
som ibland producerar bristfällig svenska (grammatikfel, knaggliga meningar, konstiga ordval).

Regler:
- Ändra ENBART språket: grammatik, ordval, meningsbyggnad, stavning.
- Ändra INTE innebörden, ståndpunkten eller det politiska innehållet.
- Behåll ungefär samma längd och form på varje argument.
- Om ett argument är knaggligt men begripligt: rätta språket och behåll innebörden exakt.
- Om ett argument är så korrumperat att du inte kan avgöra vad som menas: sätt värdet till null.
- Svara BARA med ett JSON-objekt med exakt samma nycklar som indata.
"""


def _turn1_user(arguments: list[str]) -> str:
    keyed = _to_keyed(arguments)
    return (
        "Rätta språket i dessa argument:\n\n"
        + json.dumps(keyed, ensure_ascii=False, indent=2)
        + "\n\nSvara BARA med ett JSON-objekt med samma nycklar."
    )


def _turn2_user(unclear_keyed: dict[str, str], talk: dict) -> str:
    speaker_name = (talk.get("speaker_name") or "Okänd").strip()
    party = (talk.get("party") or "").strip()
    text = talk.get("text", "")
    return (
        f"Dessa argument var obegripliga. Här är det fullständiga anförandet som referens:\n\n"
        f"Talare: {speaker_name} ({party})\n\n"
        f"Anförande:\n{text}\n\n"
        "---\n"
        "Rätta nu språket i dessa argument med hjälp av anförandet. "
        "Om ett argument är alltför korrumperat, re-extrahera det korrekt från anförandet. "
        "Sätt inget värde till null — anförandet ger dig tillräcklig kontext.\n\n"
        + json.dumps(unclear_keyed, ensure_ascii=False, indent=2)
        + "\n\nSvara BARA med ett JSON-objekt med samma nycklar."
    )


# ─────────────────────────────────────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────────────────────────────────────

_thread_local = threading.local()


def get_worker_llm() -> LLM:
    if not hasattr(_thread_local, "llm"):
        _thread_local.llm = LLM(
            model="big-smart",
            think=False,
            temperature=0.1,
            chat=False,
            silent=True,
        )
    return _thread_local.llm


def _parse_dict(content: str) -> dict | None:
    """Extract a JSON object from model output as robustly as possible."""
    text = content.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        text = m.group(1).strip()
    try:
        val, _ = json.JSONDecoder().raw_decode(text)
        if isinstance(val, dict):
            return val
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{[\s\S]*?\}", text)
    if m:
        try:
            val = json.loads(m.group(0))
            if isinstance(val, dict):
                return val
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _call_structured(llm: LLM, messages: list[dict]) -> tuple[dict | None, str]:
    """Call LLM with guided_json dict schema. Returns (parsed_dict, raw_content)."""
    result = llm.generate(
        messages=messages,
        temperature=0.1,
        think=False,
        model="big-smart",
        extra_body={"guided_json": _GUIDED_NULLABLE_DICT},
    )
    if isinstance(result, str):
        raise RuntimeError(f"LLM API error: {result}")
    content = getattr(result, "content", "") or ""
    return _parse_dict(content), content


# ─────────────────────────────────────────────────────────────────────────────
# Core correction logic
# ─────────────────────────────────────────────────────────────────────────────

def correct_arguments(llm: LLM, talk: dict) -> tuple[list[str] | None, bool]:
    """
    Multi-turn language correction for one talk's arguments.
    Returns (corrected_list, used_full_text).
    corrected_list is None when there are no arguments to process.
    used_full_text is True when turn 2 (full speech context) was needed.
    """
    arguments = talk.get("arguments")
    if not arguments:
        return None, False

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Turn 1: correct arguments without full speech text
    messages.append({"role": "user", "content": _turn1_user(arguments)})
    result1, content1 = _call_structured(llm, messages)

    if result1 is None:
        raise RuntimeError(f"Unparseable turn-1 response: {content1[:300]}")

    messages.append({"role": "assistant", "content": content1})

    corrected = _from_keyed(result1, arguments)

    # Turn 2: retry unclear ones with full speech text
    unclear_indices = [i for i, v in enumerate(corrected) if v is None]
    used_full_text = bool(unclear_indices)
    if unclear_indices:
        logger.info(f"Talk {talk['id']}: {len(unclear_indices)} unclear argument(s), fetching full text")
        unclear_keyed = {f"argument_{i+1}": arguments[i] for i in unclear_indices}

        messages.append({"role": "user", "content": _turn2_user(unclear_keyed, talk)})
        result2, content2 = _call_structured(llm, messages)

        if result2 is not None:
            for i in unclear_indices:
                key = f"argument_{i+1}"
                val = result2.get(key)
                corrected[i] = val if isinstance(val, str) else arguments[i]
        else:
            logger.warning(f"Talk {talk['id']}: turn-2 response unusable — keeping originals")
            for i in unclear_indices:
                corrected[i] = arguments[i]

    # Final safety: replace any remaining nulls with originals
    corrected = [v if isinstance(v, str) else arguments[i] for i, v in enumerate(corrected)]

    return corrected, used_full_text


# ─────────────────────────────────────────────────────────────────────────────
# Worker
# ─────────────────────────────────────────────────────────────────────────────

def process_talk(talk: dict) -> tuple[bool, bool]:
    """Returns (success, used_full_text)."""
    try:
        llm = get_worker_llm()
        corrected, used_full_text = correct_arguments(llm, talk)

        if corrected is None:
            pg.execute_void(
                "UPDATE speeches SET arguments_corrected = TRUE WHERE id = %s",
                (talk["id"],),
            )
            return True, False

        pg.execute_void(
            "UPDATE speeches SET arguments = %s, arguments_corrected = TRUE WHERE id = %s",
            (corrected, talk["id"]),
        )
        return True, used_full_text
    except Exception as e:
        logger.error(f"Error processing talk {talk.get('id')}: {e}")
        return False, False


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

WORKERS = 5
BATCH_SIZE = 100


def fetch_batch() -> list[dict]:
    return pg.execute(
        """
        SELECT id, text, speaker_name, party, arguments
        FROM speeches
        WHERE array_length(arguments, 1) > 0
          AND date >= '2002-01-01'
          AND arguments_corrected IS NOT TRUE
          AND text IS NOT NULL
        ORDER BY date DESC NULLS LAST
        LIMIT %s
        """,
        (BATCH_SIZE,),
    )


def ensure_schema():
    try:
        pg.execute_void(
            "ALTER TABLE speeches ADD COLUMN IF NOT EXISTS arguments_corrected BOOLEAN DEFAULT FALSE"
        )
    except Exception as e:
        logger.warning(f"Could not apply schema change: {e}")


def backup_arguments():
    """
    Dump all original arguments (2002+) to a JSON file before any corrections.
    Skipped if the backup file already exists.
    """
    path = "logs/arguments_backup.json"
    if os.path.exists(path):
        logger.info(f"Backup already exists at {path}, skipping.")
        return

    logger.info("Creating arguments backup …")
    rows = pg.execute(
        """
        SELECT id, arguments
        FROM speeches
        WHERE array_length(arguments, 1) > 0
          AND date >= '2002-01-01'
          AND text IS NOT NULL
        ORDER BY id
        """
    )
    backup = {str(row["id"]): row["arguments"] for row in rows}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=2)
    logger.info(f"Backed up {len(backup)} speeches → {path}")


def main():
    os.makedirs("logs", exist_ok=True)
    ensure_schema()
    backup_arguments()
    logger.info("Starting argument language correction pipeline …")

    total = 0
    errors = 0
    full_text_lookups = 0
    start = time.time()

    while True:
        batch = fetch_batch()
        if not batch:
            logger.info("No more speeches to correct. All done.")
            break

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {executor.submit(process_talk, talk): talk for talk in batch}
            for future in as_completed(futures):
                ok, used_full_text = future.result()
                total += 1
                if not ok:
                    errors += 1
                if used_full_text:
                    full_text_lookups += 1

                if total % 20 == 0:
                    elapsed = time.time() - start
                    rate = total / (elapsed / 60) if elapsed > 0 else 0
                    pct = full_text_lookups / total * 100
                    print(
                        f"Processed: {total} | Errors: {errors} "
                        f"| Full-text lookups: {full_text_lookups} ({pct:.1f}%) "
                        f"| Rate: {rate:.1f}/min"
                    )

    elapsed = time.time() - start
    rate = total / (elapsed / 60) if elapsed > 0 else 0
    pct = full_text_lookups / total * 100 if total else 0
    logger.info(
        f"=== DONE: {total} processed, {errors} errors, "
        f"{full_text_lookups} full-text lookups ({pct:.1f}%), "
        f"{elapsed/3600:.1f}h total, {rate:.1f}/min ==="
    )


if __name__ == "__main__":
    main()
