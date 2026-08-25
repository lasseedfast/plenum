"""
Hanterar debatt-ID:n och sammanfattningar av debatter i PostgreSQL.


Funktioner:
  assign_debate_ids(docs, date) → lägger till 'debate' fält i dokumentlistan
  make_debate_ids()             → tilldelas debatt-ID till alla anföranden utan ett sådant
  process_debate_date(date, ..) → sammanfattar alla debatter för ett date
"""
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from time import sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap  # noqa: E402,F401  — sets cwd and sys.path to the project root
from packages.colorprinter import *
from packages.llm import LLM
from postgres_client import pg

# ─────────────────────────────────────────────────────────────────────────────
# Debate-ID assignment
# ─────────────────────────────────────────────────────────────────────────────

def assign_debate_ids(docs: list[dict], date: str) -> list[dict]:
    """
    Assigns a debate id to each talk in a list of speeches for a given date.
    A new debate starts whenever is_reply == False.
    The debate id is a string: "{date}:{debate_index}".
    """
    debate_index = 0
    current_debate_id = f"{date}:{debate_index}"
    updated = []
    for doc in docs:
        if not doc.get("is_reply", False):
            debate_index += 1
            current_debate_id = f"{date}:{debate_index}"
        updated.append({**doc, "debate": current_debate_id})
    return updated


def make_debate_ids() -> None:
    """
    Find all speeches without a debate field and assign debate IDs.
    """
    dates = pg.execute(
        "SELECT DISTINCT date::text AS date FROM speeches WHERE debate IS NULL ORDER BY date"
    )
    dates = [row["date"] for row in dates if row.get("date")]
    print(f"Found {len(dates)} unique dates with speeches missing debate ids")

    for date in dates:
        speeches = pg.execute(
            """
            SELECT id, is_reply
            FROM speeches
            WHERE date = %s::date
            ORDER BY sequence ASC
            """,
            (date,),
        )
        if not speeches:
            continue

        updated = assign_debate_ids(list(speeches), date)

        # Batch UPDATE
        pg.execute_many(
            "UPDATE speeches SET debate = %s WHERE id = %s",
            [(doc["debate"], doc["id"]) for doc in updated],
        )
        print(f"  {date}: assigned debate IDs to {len(updated)} speeches", end="\r")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Summarization
# ─────────────────────────────────────────────────────────────────────────────

def summarize_talk(talk: dict, llm: LLM) -> str:
    speaker_name = talk["speaker_name"]
    party = talk["party"]
    text = talk["text"]
    if talk.get("is_reply"):
        prompt = f"""
Nedan är ett tal från en debatt i Sveriges riksdag. Sammanfatta talet kort och koncist på svenska, fokusera på de viktigaste argumenten och sakförhållandena som framförs.
Talet är ett svar på ett föregående tal. När du sammanfattar, se till att den går att förstå även utan att ha läst det föregående talet. Inkludera däremot INTE information eller argument från det föregående talet.
Talaren är {speaker_name} från {party}. Börja gärna sammanfattningen med "Namn (Parti) ..." för att tydligt ange vem som talar.
---
{text}
---

Svara _enbart_ med sammanfattningen, inga andra kommentarer eller förklaringar.
"""
    else:
        prompt = f"""
Nedan är ett tal från en debatt i Sveriges riksdag. Sammanfatta talet kort och koncist på svenska, fokusera på de viktigaste argumenten och sakförhållandena som framförs.
Talaren är {speaker_name} från {party}. Börja gärna sammanfattningen med "Namn (Parti) ..." för att tydligt ange vem som talar.
---
{text}
---

Svara **enbart** med sammanfattningen, inga andra kommentarer eller förklaringar.
"""
    return llm.generate(query=prompt).content.strip()


def process_debate_date(date: str, system_message: str) -> None:
    """
    Processes all debates for a given date: summarizes each talk and the debate.
    """
    # Get distinct debates with unsummarized speeches for this date
    debates = pg.execute(
        """
        SELECT DISTINCT debate
        FROM speeches
        WHERE date = %s::date AND summary IS NULL AND debate IS NOT NULL
        ORDER BY debate
        """,
        (date,),
    )
    debates = [row["debate"] for row in debates]

    for debate in debates:
        llm = LLM(model="vllm", temperature=0.2, system_message=system_message)

        speeches = pg.execute(
            """
            SELECT id, text, sequence, date::text AS date,
                   is_reply, speaker_name, party
            FROM speeches
            WHERE debate = %s
            ORDER BY sequence ASC
            """,
            (debate,),
        )
        if not speeches:
            continue

        print(f"Processing debate {debate} with {len(speeches)} speeches")
        summaries = []

        for talk in speeches:
            if talk.get("summary"):
                summaries.append(f"{talk['speaker_name']} ({talk['party']}):\n{talk['summary']}")
                continue
            summary = summarize_talk(talk, llm)
            summaries.append(f"{talk['speaker_name']} ({talk['party']}):\n{summary}")
            print(f"  Talk {talk['sequence']} summary: {summary[:80]}")
            pg.execute_void(
                "UPDATE speeches SET summary = %s WHERE id = %s",
                (summary, talk["id"]),
            )

        if len(speeches) == 1:
            print_yellow(f"Debate {debate} has only one talk, skipping debate summary")
            continue

        summaries_string = "\n---\n".join(summaries)
        prompt = f"""
Tack! Nu ska du sammanfatta hela debatten baserat på de enskilda sammanfattningarna av varje tal nedan.
Fokusera på de viktigaste argumenten och sakförhållandena som framförs i debatten.
Sammanfattningen ska vara koncis och informativ, och skriven på svenska.

Här är sammanfattningarna av de enskilda talen i debatten:
'''
{summaries_string}
'''
Svara så att det framgår vad debatten handlade om och vilka de viktigaste argumenten var, samt vilka ståndpunkter de olika partierna hade.
Svara i löpande text utan någon avancerad formatering.

Svara **enbart** med sammanfattningen, inga andra kommentarer eller förklaringar.
"""
        debate_summary = llm.generate(query=prompt).content.strip()
        print_green(f"Debate summary:\n{debate_summary[:100]}")

        talk_ids = [t["id"] for t in speeches]
        pg.execute_void(
            """
            INSERT INTO debates (debate, date, summary, num_talks, talk_summaries, talk_ids)
            VALUES (%s, %s::date, %s, %s, %s, %s)
            ON CONFLICT (debate) DO UPDATE SET
                summary        = EXCLUDED.summary,
                talk_summaries = EXCLUDED.talk_summaries
            """,
            (
                debate,
                speeches[0]["date"] if speeches else None,
                debate_summary,
                len(speeches),
                summaries,
                talk_ids,
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Process debates where all speeches are already summarized (no debate row yet)
# ─────────────────────────────────────────────────────────────────────────────

def process_ready_debate(debate_id: str, system_message: str) -> None:
    """
    Creates a debates row for a debate whose speeches are all already summarized.
    Skips single-talk debates (no aggregate summary needed).
    """
    llm = LLM(model="vllm", temperature=0.2, system_message=system_message)

    speeches = pg.execute(
        """
        SELECT id, summary, sequence, date::text AS date,
               speaker_name, party
        FROM speeches
        WHERE debate = %s
        ORDER BY sequence ASC
        """,
        (debate_id,),
    )
    if not speeches:
        return

    if len(speeches) == 1:
        print_yellow(f"Debate {debate_id} has only one talk, skipping")
        return

    summaries = [f"{t['speaker_name']} ({t['party']}):\n{t['summary']}" for t in speeches]
    summaries_string = "\n---\n".join(summaries)

    prompt = f"""
Tack! Nu ska du sammanfatta hela debatten baserat på de enskilda sammanfattningarna av varje tal nedan.
Fokusera på de viktigaste argumenten och sakförhållandena som framförs i debatten.
Sammanfattningen ska vara koncis och informativ, och skriven på svenska.

Här är sammanfattningarna av de enskilda talen i debatten:
'''
{summaries_string}
'''
Svara så att det framgår vad debatten handlade om och vilka de viktigaste argumenten var, samt vilka ståndpunkter de olika partierna hade.
Svara i löpande text utan någon avancerad formatering.

Svara **enbart** med sammanfattningen, inga andra kommentarer eller förklaringar.
"""
    debate_summary = llm.generate(query=prompt).content.strip()
    print_green(f"Ready debate {debate_id} summary: {debate_summary[:80]}")

    talk_ids = [t["id"] for t in speeches]
    pg.execute_void(
        """
        INSERT INTO debates (debate, date, summary, num_talks, talk_summaries, talk_ids)
        VALUES (%s, %s::date, %s, %s, %s, %s)
        ON CONFLICT (debate) DO UPDATE SET
            summary        = EXCLUDED.summary,
            talk_summaries = EXCLUDED.talk_summaries,
            talk_ids       = EXCLUDED.talk_ids,
            num_talks      = EXCLUDED.num_talks
        """,
        (
            debate_id,
            speeches[0]["date"] if speeches else None,
            debate_summary,
            len(speeches),
            summaries,
            talk_ids,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    system_message = """Din uppgift är att sammanfatta debatter i Sveriges riksdag.
Du kommer först att få enskilda tal som du ska sammanfatta var för sig, efter det ska du sammanfatta hela debatten.
Sammanfattningarna ska vara på svenska och vara koncisa och informativa.
Det är viktigt att du förstår vad som är kärnan i varje tal och debatt, fokusera därför på de argument och sakförhållanden som framförs.
"""
    while True:
        # Phase 1: summarize speeches and create debate rows for dates with unsummarized speeches
        dates = pg.execute(
            "SELECT DISTINCT date::text AS date FROM speeches WHERE summary IS NULL ORDER BY date"
        )
        dates = [row["date"] for row in dates if row.get("date")]

        if dates:
            print(f"Found {len(dates)} unique dates to process.")
            with ProcessPoolExecutor(max_workers=4) as executor:
                errors = 0
                futures = {
                    executor.submit(process_debate_date, date, system_message): date
                    for date in dates
                }
                for future in as_completed(futures):
                    date = futures[future]
                    if errors > 20:
                        sleep(60 * 10)
                    try:
                        future.result()
                        print_green(f"Finished processing date {date}")
                        errors = 0
                    except Exception as exc:
                        errors += 1
                        print_red(f"Error processing date {date}: {exc}")

        # Phase 2: create debate rows for debates where all speeches are already summarized
        ready = pg.execute(
            """
            SELECT t.debate
            FROM speeches t
            LEFT JOIN debates d ON t.debate = d.debate
            WHERE t.debate IS NOT NULL AND d.debate IS NULL
            GROUP BY t.debate
            HAVING COUNT(t.id) = COUNT(t.summary)
              AND COUNT(t.id) > 1
            ORDER BY t.debate
            """
        )
        ready_ids = [row["debate"] for row in ready]

        if ready_ids:
            print(f"Found {len(ready_ids)} ready debates without a row yet.")
            with ProcessPoolExecutor(max_workers=4) as executor:
                errors = 0
                futures = {
                    executor.submit(process_ready_debate, did, system_message): did
                    for did in ready_ids
                }
                for future in as_completed(futures):
                    did = futures[future]
                    if errors > 20:
                        sleep(60 * 10)
                    try:
                        future.result()
                        errors = 0
                    except Exception as exc:
                        errors += 1
                        print_red(f"Error processing ready debate {did}: {exc}")

        if not dates and not ready_ids:
            print_green("All debates processed, sleeping for a day")
            sleep(60 * 60 * 24)
        else:
            sleep(60 * 15)
