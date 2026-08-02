import hashlib
import json
import os
import re
import traceback as _tb

from postgres_client import pg

_DETAIL_MAX = 500


def _inject_eval_ids(detail: dict) -> dict:
    """If running inside the eval harness, stamp run/question ids into detail."""
    run_id = os.environ.get("EVAL_RUN_ID")
    q_id = os.environ.get("EVAL_QUESTION_ID")
    if run_id:
        detail.setdefault("eval_run_id", run_id)
    if q_id:
        detail.setdefault("eval_question_id", q_id)
    return detail


def _truncate_detail(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > _DETAIL_MAX:
            out[k] = v[:_DETAIL_MAX] + "…[truncated]"
        elif isinstance(v, dict):
            out[k] = _truncate_detail(v)
        else:
            out[k] = v
    return out


def _normalize_tb(tb: str) -> str:
    tb = re.sub(r'0x[0-9a-fA-F]+', '0xADDR', tb)
    tb = re.sub(r'/tmp/\S+', '/tmp/TMPFILE', tb)
    tb = re.sub(
        r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
        'UUID',
        tb,
    )
    tb = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?\b', 'IP:PORT', tb)
    return tb


def _fingerprint(tb: str) -> str:
    return hashlib.sha1(_normalize_tb(tb).encode()).hexdigest()


def log_error(
    error_type: str,
    exc: BaseException,
    model: str | None = None,
    **detail,
) -> None:
    """Upsert into error_log with deduplication by traceback fingerprint.

    First occurrence stores full traceback; repeats only increment count.
    Never raises — logging must not break the main flow.
    """
    try:
        tb_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
        fp = _fingerprint(tb_text)
        merged = _inject_eval_ids({"model": model, **detail}) if (model or detail) else _inject_eval_ids({})
        safe_detail = _truncate_detail(merged) if merged else None
        pg.execute_void(
            """
            INSERT INTO error_log (fingerprint, error_type, model, traceback, detail)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO UPDATE SET
                count        = error_log.count + 1,
                last_seen_at = NOW(),
                model        = COALESCE(EXCLUDED.model, error_log.model)
            """,
            (
                fp,
                error_type,
                model,
                tb_text,
                json.dumps(safe_detail, default=str) if safe_detail else None,
            ),
        )
    except Exception:
        pass


def log_event(event_type: str, model: str | None = None, **detail) -> None:
    """Insert one row into llm_events. Never raises — logging must not break the main flow."""
    try:
        if model:
            detail["model"] = model
        _inject_eval_ids(detail)
        pg.execute_void(
            "INSERT INTO llm_events (event_type, detail) VALUES (%s, %s)",
            (event_type, json.dumps(detail, default=str)),
        )
    except Exception:
        pass
