"""Out-of-process background jobs for deep research.

Long-running research (many sequential LLM "trips") must not live inside a web
request: it would die with the connection and stall the API worker. So a job
runs in its **own OS process** — a child of the web worker that spawned it —
and communicates progress through PostgreSQL (`jobs` + `job_events` tables),
so any API worker can serve a poll and progress survives an API restart.

Design (ported from the FUP project's job runner):
- **Registry.** Each job ``kind`` registers a handler ``fn(params, ctx)`` via
  :func:`register`. Handlers take JSON-serializable ``params`` and build their
  own DB client from env.
- **Spawn.** :func:`spawn_job` (web side) writes a ``jobs`` row, spawns
  ``python -m backend.job_runner``, and hands it
  ``{job_id, kind, params, secrets}`` as JSON on the child's stdin pipe.
  Returns ``{job_id}`` immediately. Only ``params`` is persisted; ``secrets``
  (board key, content-bearing inputs, a user's provider override) exists in the
  request, the pipe and the child's memory. The child's argv is just
  ``python -m backend.job_runner``, so nothing leaks via ``/proc/<pid>/cmdline``
  either, and :func:`_redactor` keeps secrets out of persisted error text.
- **Progress via Postgres.** The child's :class:`JobContext` appends to
  ``job_events`` and heartbeats the ``jobs`` row (30s daemon thread), so a
  slow-but-healthy step (one long LLM call) isn't mistaken for a dead child.
- **Reaper.** :func:`reap_stale_jobs` marks abandoned ``running`` rows failed
  (child pid gone, or heartbeat older than ``RESEARCH_STALE_HEARTBEAT_SECS``)
  and finalizes the linked research board so the UI never spins forever.
- **Cancellation** is cooperative: :func:`request_cancel` sets a flag the
  handler checks between trips via ``ctx.is_cancelled()``.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from postgres_client import pg

log = logging.getLogger("riksdagen.research.jobs")

# kind -> handler(params, ctx). Populated by register() at import time of each
# handler module (backend/job_runner.py imports them in the child).
JobHandler = Callable[[dict, "JobContext"], None]
_HANDLERS: dict[str, JobHandler] = {}
_RUNTIME_CAPS: dict[str, Optional[int]] = {}

# Popen handles of children spawned by *this* web worker, reaped lazily so
# finished children don't linger as zombies while the worker is alive.
_children: list[subprocess.Popen] = []

# Persist the jobs-row progress at most this often during a burst of events.
_PERSIST_MIN_SECS = 3.0
HEARTBEAT_INTERVAL_SECS = int(os.getenv("RESEARCH_HEARTBEAT_SECS", "30"))
STALE_HEARTBEAT_SECS = int(os.getenv("RESEARCH_STALE_HEARTBEAT_SECS", "150"))
MAX_JOB_RUNTIME_SECS = int(os.getenv("RESEARCH_MAX_JOB_RUNTIME_SECS", "3600"))

_DEFAULT_CAP = object()


def register(
    kind: str, *, max_runtime_secs: object = _DEFAULT_CAP
) -> Callable[[JobHandler], JobHandler]:
    """Decorator: register ``fn`` as the handler for job ``kind``.

    ``max_runtime_secs``: omit for the global cap, ``None`` for unbounded,
    or a positive int for a kind-specific ceiling.
    """

    def deco(fn: JobHandler) -> JobHandler:
        _HANDLERS[kind] = fn
        _RUNTIME_CAPS[kind] = (
            MAX_JOB_RUNTIME_SECS if max_runtime_secs is _DEFAULT_CAP else max_runtime_secs  # type: ignore[assignment]
        )
        return fn

    return deco


def get_handler(kind: str) -> Optional[JobHandler]:
    return _HANDLERS.get(kind)


def runtime_cap_for(kind: str) -> Optional[int]:
    return _RUNTIME_CAPS.get(kind, MAX_JOB_RUNTIME_SECS)


def _new_job_id(kind: str) -> str:
    """Short, sortable, greppable id, e.g. ``20260713T120102-research_build-a1b2c3``."""
    ts = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{kind}-{uuid.uuid4().hex[:6]}"


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return ""


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. Signal 0 doesn't kill."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _reap_children() -> None:
    """Poll finished children so they don't linger as zombies."""
    for p in list(_children):
        if p.poll() is not None:
            _children.remove(p)


# ---------------------------------------------------------------------------
# JobContext — passed to the handler (runs inside the child process)
# ---------------------------------------------------------------------------


class JobContext:
    """Handed to a job handler so it can emit progress + report results.

    ``progress`` appends a row to ``job_events`` (polled by the frontend) and
    debounced-refreshes the ``jobs`` row. ``set_counts`` / ``add_error``
    accumulate the final summary, flushed when the runner finalizes the job.

    ``board_key`` (encrypted boards only) lives in this process's memory for
    the job's lifetime: every content-bearing string that leaves for Postgres
    (event messages, progress "current", error strings) is encrypted with it;
    numeric progress stays plaintext so progress bars need no key.
    """

    def __init__(self, job_id: str, kind: str, board_id: Optional[str],
                 board_key: Optional[bytes] = None):
        self.job_id = job_id
        self.kind = kind
        self.board_id = board_id
        self.board_key = board_key
        self._seq = 0
        self._last_persist = 0.0
        self._last_cancel_check = 0.0
        self._cancelled_cache = False
        self.counts: dict = {}
        self.errors: list[str] = []
        self.progress_state: dict = {"done": 0, "total": 0, "current": ""}

    # -- emitting -----------------------------------------------------------

    def progress(
        self,
        done: Optional[int] = None,
        total: Optional[int] = None,
        current: str = "",
        message: str = "",
        level: str = "info",
        data: Optional[dict] = None,
    ) -> None:
        if done is not None:
            self.progress_state["done"] = done
        if total is not None:
            self.progress_state["total"] = total
        if current:
            self.progress_state["current"] = current
        event = {
            "done": self.progress_state["done"],
            "total": self.progress_state["total"],
            "current": current or self.progress_state["current"],
            "message": message,
            "level": level,
        }
        # ``data`` lets a job carry extra per-event fields (e.g. a finding
        # preview) — merged last, never clobbering the canonical fields.
        if data:
            for k, v in data.items():
                event.setdefault(k, v)
        if self.board_key is not None:
            from backend.services.crypto_blob import encrypt_str

            content = {k: v for k, v in event.items() if k not in ("done", "total")}
            event = {
                "done": event["done"],
                "total": event["total"],
                "enc": encrypt_str(json.dumps(content, ensure_ascii=False, default=str),
                                   self.board_key),
            }
        self._append_event(event)
        self._maybe_persist()

    def set_counts(self, **kwargs) -> None:
        self.counts.update(kwargs)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    # -- cancellation -------------------------------------------------------

    def is_cancelled(self) -> bool:
        """Read the cancel flag off the jobs row (cached briefly)."""
        now = time.time()
        if (now - self._last_cancel_check) < 2.0:
            return self._cancelled_cache
        self._last_cancel_check = now
        try:
            rows = pg.execute(
                "SELECT cancel_requested FROM jobs WHERE id = %s", (self.job_id,)
            )
            self._cancelled_cache = bool(rows and rows[0]["cancel_requested"])
        except Exception:
            pass
        return self._cancelled_cache

    # -- internal -----------------------------------------------------------

    def _append_event(self, event: dict) -> None:
        try:
            pg.execute_void(
                "INSERT INTO job_events (job_id, seq, event) VALUES (%s, %s, %s::jsonb)",
                (self.job_id, self._seq, json.dumps(event, default=str)),
            )
            self._seq += 1
        except Exception:
            log.warning("could not append job event for %s", self.job_id, exc_info=True)

    def persist(self, status: str, *, finished: bool = False) -> None:
        progress_state = self.progress_state
        errors = self.errors
        if self.board_key is not None:
            # Content-bearing strings (current thread title, handler error
            # text) go to the jobs row encrypted; counters stay plaintext.
            from backend.services.crypto_blob import encrypt_str

            progress_state = dict(self.progress_state)
            if progress_state.get("current"):
                progress_state["current"] = encrypt_str(
                    str(progress_state["current"]), self.board_key
                )
            errors = [encrypt_str(str(e), self.board_key) for e in self.errors]
        try:
            pg.execute_void(
                """
                UPDATE jobs SET
                    status = %s,
                    progress = %s::jsonb,
                    counts = %s::jsonb,
                    errors = %s::jsonb,
                    event_count = %s,
                    last_heartbeat_at = NOW(),
                    finished_at = CASE WHEN %s THEN NOW() ELSE finished_at END
                WHERE id = %s
                """,
                (
                    status,
                    json.dumps(progress_state, default=str),
                    json.dumps(self.counts, default=str),
                    json.dumps(errors, default=str),
                    self._seq,
                    finished,
                    self.job_id,
                ),
            )
        except Exception:
            log.warning("could not persist job %s", self.job_id, exc_info=True)

    def _maybe_persist(self) -> None:
        now = time.time()
        if (now - self._last_persist) < _PERSIST_MIN_SECS:
            return
        self._last_persist = now
        self.persist("running")


# ---------------------------------------------------------------------------
# Web side — spawn + poll
# ---------------------------------------------------------------------------


def spawn_job(*, kind: str, board_id: Optional[str] = None, params: Optional[dict] = None,
              secrets: Optional[dict] = None) -> dict:
    """Start job ``kind`` in a child OS process and return its id immediately.

    The spec passed on the child's stdin is ``{job_id, kind, params, secrets}``.
    The child builds its own DB and LLM clients from server-side env.

    ``secrets`` is the side channel for values that must NEVER touch the DB:
    the raw per-board encryption key of an encrypted board, plus any
    content-bearing job inputs (e.g. a lead's text). Only ``params`` is
    written to the ``jobs`` row; ``secrets`` exists in the request, this pipe,
    and the child's memory — nowhere else.
    """
    job_id = _new_job_id(kind)

    # The jobs row is written by the web side first so a poll right after
    # spawn sees a `running` row even before the child has booted.
    try:
        pg.execute_void(
            """
            INSERT INTO jobs (id, kind, board_id, status, params)
            VALUES (%s, %s, %s, 'running', %s::jsonb)
            """,
            (job_id, kind, board_id, json.dumps(params or {}, default=str)),
        )
    except Exception as exc:
        log.exception("could not create jobs row for %s (%s)", job_id, kind)
        return {"job_id": job_id, "status": "failed", "error": str(exc)}

    spec = {"job_id": job_id, "kind": kind, "board_id": board_id,
            "params": params or {}, "secrets": secrets or {}}

    _reap_children()
    repo_root = Path(__file__).resolve().parents[3]
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "backend.job_runner"],
            stdin=subprocess.PIPE,
            start_new_session=True,  # survive an API-worker restart
            cwd=str(repo_root),
        )
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(spec).encode("utf-8"))
        proc.stdin.close()
        _children.append(proc)
    except Exception as exc:
        log.exception("could not spawn job process %s (%s)", job_id, kind)
        pg.execute_void(
            """
            UPDATE jobs SET status = 'failed', finished_at = NOW(),
                errors = errors || %s::jsonb
            WHERE id = %s
            """,
            (json.dumps([f"could not start job process: {exc}"]), job_id),
        )
        return {"job_id": job_id, "status": "failed"}

    # Record pid + host so the reaper can verify liveness on its own host.
    try:
        pg.execute_void(
            "UPDATE jobs SET pid = %s, host = %s WHERE id = %s",
            (proc.pid, _hostname(), job_id),
        )
    except Exception:
        pass

    log.info("spawned job %s (%s) pid=%s board=%s", job_id, kind, proc.pid, board_id)
    return {"job_id": job_id, "status": "started"}


def get_events(job_id: str, offset: int = 0) -> dict:
    """Poll for new events. ``is_done`` is only True once all events are delivered."""
    try:
        rows = pg.execute(
            "SELECT event FROM job_events WHERE job_id = %s AND seq >= %s ORDER BY seq",
            (job_id, offset),
        )
        events = [r["event"] for r in rows]
    except Exception:
        events = []
    try:
        jrows = pg.execute(
            "SELECT status, event_count, progress, counts, errors FROM jobs WHERE id = %s",
            (job_id,),
        )
    except Exception:
        jrows = []
    job = jrows[0] if jrows else {}
    status = job.get("status", "running")
    delivered_all = (offset + len(events)) >= int(job.get("event_count") or 0)
    is_done = status in ("done", "failed", "cancelled") and delivered_all
    return {
        "events": events,
        "is_done": is_done,
        "offset": offset + len(events),
        "status": status,
        "progress": job.get("progress") or {"done": 0, "total": 0, "current": ""},
        "counts": job.get("counts") or {},
        "errors": job.get("errors") or [],
    }


def running_job_for_board(board_id: str) -> Optional[dict]:
    """The board's currently-running job row (dict), or None."""
    try:
        rows = pg.execute(
            """
            SELECT id AS job_id, kind, status, progress, started_at::text AS started_at
            FROM jobs WHERE board_id = %s AND status = 'running'
            ORDER BY started_at DESC LIMIT 1
            """,
            (board_id,),
        )
    except Exception:
        return None
    return dict(rows[0]) if rows else None


def count_running_jobs(server_key_only: bool = False) -> int:
    """Running jobs. With ``server_key_only``, count only jobs spending the
    server's own tokens — jobs marked ``byo`` run on a user-supplied key and
    are budgeted separately (see count_running_byo_jobs)."""
    sql = "SELECT COUNT(*) AS n FROM jobs WHERE status = 'running'"
    if server_key_only:
        sql += " AND NOT COALESCE((params->>'byo')::boolean, FALSE)"
    try:
        rows = pg.execute(sql)
        return int(rows[0]["n"]) if rows else 0
    except Exception:
        return 0


def count_running_byo_jobs(owner_session: Optional[str] = None,
                           user_id: Optional[str] = None) -> int:
    """Running bring-your-own-key jobs belonging to one browser or one account.

    With neither identifier the answer is 0 — an unattributable job must never
    consume someone else's allowance.
    """
    if not owner_session and not user_id:
        return 0
    clauses = ["j.status = 'running'",
               "COALESCE((j.params->>'byo')::boolean, FALSE)"]
    args: list = []
    if user_id:
        clauses.append("b.user_id = %s")
        args.append(user_id)
    else:
        clauses.append("b.owner_session = %s")
        args.append(owner_session)
    try:
        rows = pg.execute(
            "SELECT COUNT(*) AS n FROM jobs j "
            "JOIN research_boards b ON b.id = j.board_id "
            f"WHERE {' AND '.join(clauses)}",
            tuple(args),
        )
        return int(rows[0]["n"]) if rows else 0
    except Exception:
        return 0


def request_cancel(job_id: str) -> None:
    """Set the cancel flag on the jobs row; the child checks it between trips."""
    try:
        pg.execute_void(
            "UPDATE jobs SET cancel_requested = TRUE WHERE id = %s", (job_id,)
        )
    except Exception:
        log.warning("could not request cancel for job %s", job_id, exc_info=True)


def reap_stale_jobs() -> int:
    """Mark abandoned `running` jobs failed; kill same-host orphan pids.

    A job is abandoned when its child pid is gone (crash / OOM-kill) or its
    heartbeat is older than ``STALE_HEARTBEAT_SECS``. Also finalizes the
    linked research board (digging → failed) so the UI never spins forever.
    Runs on API startup and opportunistically from the research GET routes.
    """
    try:
        rows = pg.execute(
            """
            SELECT id, pid, host, board_id, errors,
                   (last_heartbeat_at < NOW() - make_interval(secs => %s)) AS hb_stale
            FROM jobs WHERE status = 'running'
            """,
            (STALE_HEARTBEAT_SECS,),
        )
    except Exception:
        return 0
    host = _hostname()
    reaped = 0
    for row in rows:
        job_id = row["id"]
        pid = row.get("pid")
        same_host = (row.get("host") or "") == host
        pid_dead = same_host and isinstance(pid, int) and not _pid_alive(pid)
        hb_stale = bool(row.get("hb_stale"))
        if not (pid_dead or hb_stale):
            continue
        # Pid alive but heartbeat stale: the child is wedged — kill it.
        if same_host and isinstance(pid, int) and _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                log.warning("reaper: could not kill pid %s for job %s", pid, job_id)
        reason = "child process died" if pid_dead else "heartbeat went stale"
        try:
            pg.execute_void(
                """
                UPDATE jobs SET status = 'failed', finished_at = NOW(),
                    errors = errors || %s::jsonb
                WHERE id = %s
                """,
                (json.dumps([f"job abandoned: {reason}"]), job_id),
            )
            if row.get("board_id"):
                pg.execute_void(
                    """
                    UPDATE research_boards SET status = 'failed', updated_at = NOW()
                    WHERE id = %s AND status IN ('scouting', 'digging', 'reporting')
                    """,
                    (row["board_id"],),
                )
        except Exception:
            log.exception("reaper: could not finalize job %s", job_id)
            continue
        reaped += 1
        log.warning("reaper: marked job %s failed (%s)", job_id, reason)
    return reaped


# ---------------------------------------------------------------------------
# Child side — execute one spec (called by backend/job_runner.py)
# ---------------------------------------------------------------------------


# Keys inside a secret whose values are identifiers, not secrets. Redacting a
# provider id or model name would mangle otherwise-useful error text (and tell
# an attacker nothing), so they are left alone.
_PUBLIC_SECRET_KEYS = frozenset({
    "provider_id", "smart_model", "fast_model", "editor_model", "kind",
})


def _secret_strings(value, out: list) -> None:
    """Collect every string worth redacting out of a (possibly nested) secret."""
    if isinstance(value, str):
        if len(value.strip()) >= 8:  # short values would redact half the message
            out.append(value.strip())
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in _PUBLIC_SECRET_KEYS:
                _secret_strings(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _secret_strings(v, out)


def _redactor(secrets: dict):
    """A str -> str function that masks any secret value appearing in the text.

    Job errors and progress messages are persisted to the DB; secrets by
    definition must not be. Longest-first so a value that contains another is
    masked whole.
    """
    values: list = []
    _secret_strings(secrets, values)
    values.sort(key=len, reverse=True)
    if not values:
        return lambda text: text

    def redact(text: str) -> str:
        for v in values:
            if v in text:
                text = text.replace(v, "[redacted]")
        return text

    return redact


def execute_spec(spec: dict) -> None:
    """Run one job spec to completion inside the current (child) process.

    Dispatches to the registered handler and finalizes the ``jobs`` row.
    Handler modules must already be imported so the registry is populated
    (``backend/job_runner.py`` does this).
    """
    job_id = spec["job_id"]
    kind = spec["kind"]
    board_id = spec.get("board_id")
    params = spec.get("params") or {}
    secrets = spec.get("secrets") or {}

    # Built before anything is popped, so every secret is covered.
    redact = _redactor(secrets)

    # The board key (encrypted boards) stays in this process's memory only.
    board_key: Optional[bytes] = None
    raw_key = secrets.pop("board_key", None)
    if raw_key:
        import base64

        board_key = base64.b64decode(raw_key)
    # Remaining secrets are content-bearing job inputs (e.g. a lead, or the
    # user's provider override) that were kept out of the persisted params;
    # hand them to the handler in-memory.
    if secrets:
        params = {**params, **secrets}

    ctx = JobContext(job_id, kind, board_id, board_key=board_key)

    handler = get_handler(kind)
    if handler is None:
        ctx.add_error(f"no handler registered for job kind '{kind}'")
        ctx.persist("failed", finished=True)
        log.error("job %s: no handler for kind '%s'", job_id, kind)
        return

    # Heartbeat thread: refresh last_heartbeat_at on a fixed cadence regardless
    # of whether the handler emits progress, so a long but healthy step (one
    # slow LLM call) is never mistaken for a dead child by the reaper.
    stop_heartbeat = threading.Event()

    def _heartbeat() -> None:
        while not stop_heartbeat.wait(HEARTBEAT_INTERVAL_SECS):
            try:
                pg.execute_void(
                    "UPDATE jobs SET last_heartbeat_at = NOW() WHERE id = %s",
                    (job_id,),
                )
            except Exception:
                log.debug("heartbeat update failed for %s", job_id, exc_info=True)

    hb = threading.Thread(target=_heartbeat, name=f"hb-{job_id}", daemon=True)
    hb.start()

    # Wall-clock watchdog: a runaway loop keeps the daemon heartbeat ticking,
    # so heartbeat staleness alone can't catch it. SIGALRM interrupts the
    # handler past the cap. POSIX-only; the reaper is the backstop elsewhere.
    cap = runtime_cap_for(kind)

    def _on_timeout(signum, frame):  # noqa: ANN001
        raise TimeoutError(f"job exceeded max runtime of {cap}s")

    _have_alarm = hasattr(signal, "SIGALRM") and cap is not None and cap > 0
    if _have_alarm:
        signal.signal(signal.SIGALRM, _on_timeout)
        signal.alarm(cap)

    status = "done"
    try:
        handler(params, ctx)
        if ctx.is_cancelled():
            status = "cancelled"
    except Exception as exc:  # noqa: BLE001 — surface any handler failure
        status = "failed"
        log.exception("job %s (%s) crashed", job_id, kind)
        # Providers routinely echo the failing request — auth header included —
        # back in their error bodies, and both of these calls persist the text.
        message = redact(str(exc))
        ctx.add_error(message)
        ctx.progress(message=message, level="error")
    finally:
        if _have_alarm:
            signal.alarm(0)
        stop_heartbeat.set()
        ctx.persist(status, finished=True)
        log.info("job %s (%s) finished: %s", job_id, kind, status)
