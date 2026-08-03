"""Check whether this machine can run plenum, and report what is missing.

    python scripts/doctor.py

Every check prints OK, WARN or FAIL with the actual value it found, so the output can
be pasted somewhere or read by an assistant deciding what to do next. Nothing is
changed — this only looks.

Exit code is 1 if any check FAILed, so it can gate a setup script.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
_results: list[str] = []


def report(status: str, check: str, detail: str = "") -> None:
    _results.append(status)
    print(f"  [{status}] {check}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ok(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        import requests

        r = requests.get(url, timeout=timeout)
        return r.status_code < 500, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, type(exc).__name__


# ── environment ───────────────────────────────────────────────────────────────


def check_python() -> None:
    section("Python")
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        report(OK, "version", f"{v.major}.{v.minor}.{v.micro}")
    else:
        report(FAIL, "version", f"{v.major}.{v.minor} — plenum needs 3.10 or newer")

    missing = [m for m in ("fastapi", "psycopg2", "openai", "yaml", "pgvector")
               if not _importable(m)]
    if missing:
        report(FAIL, "dependencies", f"missing {missing} — run: pip install -e '.[dev]'")
    else:
        report(OK, "dependencies", "installed")


def _importable(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def check_config() -> None:
    section("Configuration")
    env = ROOT / ".env"
    if env.exists():
        report(OK, ".env", str(env))
    else:
        report(FAIL, ".env", "missing — copy .env.example to .env")
        return

    try:
        from dotenv import load_dotenv

        load_dotenv(env)
    except Exception as exc:
        report(WARN, ".env", f"could not load: {exc}")

    try:
        from parliament import PARLIAMENT

        report(OK, "parliament config",
               f"{PARLIAMENT.meta.get('name')} ({PARLIAMENT.meta.get('country')}), "
               f"fts={PARLIAMENT.language.fts_config}, "
               f"embeddings={PARLIAMENT.embeddings.dimension}d")
    except Exception as exc:
        report(FAIL, "parliament config", str(exc)[:120])


# ── database ──────────────────────────────────────────────────────────────────


def check_database() -> None:
    section("PostgreSQL")
    host = os.getenv("PG_HOST", "localhost")
    port = int(os.getenv("PG_PORT", 5432))
    if not _port_open(host, port):
        report(FAIL, "reachable", f"nothing listening on {host}:{port}")
        return
    report(OK, "reachable", f"{host}:{port}")

    try:
        from postgres_client import pg

        ver = pg.execute("SELECT version() AS v")[0]["v"].split(",")[0]
        report(OK, "connected", ver)
    except Exception as exc:
        report(FAIL, "connected", f"{type(exc).__name__}: {str(exc).strip()[:90]}")
        return

    exts = {r["extname"] for r in pg.execute("SELECT extname FROM pg_extension")}
    for ext in ("vector", "pg_trgm"):
        if ext in exts:
            report(OK, f"extension {ext}", "installed")
        else:
            report(FAIL, f"extension {ext}", f'missing — run: CREATE EXTENSION {ext};')

    try:
        cfg = pg.execute("SELECT current_setting('app.fts_config') AS c")[0]["c"]
        from parliament import PARLIAMENT

        if cfg == PARLIAMENT.language.fts_config:
            report(OK, "app.fts_config", cfg)
        else:
            report(FAIL, "app.fts_config",
                   f"database says {cfg!r}, config says {PARLIAMENT.language.fts_config!r}")
    except Exception:
        report(FAIL, "app.fts_config",
               "not set — ALTER DATABASE <db> SET app.fts_config = '<language>';")

    tables = pg.execute(
        "SELECT count(*) AS n FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'"
    )[0]["n"]
    if tables >= 22:
        report(OK, "schema", f"{tables} tables")
    elif tables == 0:
        report(FAIL, "schema", "empty — psql -f _postgres/schema.sql")
    else:
        report(WARN, "schema", f"only {tables} tables; expected 22")

    try:
        n = pg.execute("SELECT count(*) AS n FROM speeches")[0]["n"]
        report(OK if n else WARN, "data",
               f"{n:,} speeches" if n else "no data yet — run ingest.cli")
    except Exception:
        report(WARN, "data", "tables not queryable yet")


# ── models ────────────────────────────────────────────────────────────────────


def check_chat_model() -> None:
    section("Chat model")
    url = os.getenv("LLM_DIRECT_URL")
    if not url:
        report(FAIL, "LLM_DIRECT_URL", "not set — chat and research will not work")
        return
    report(OK, "LLM_DIRECT_URL", url)

    ok, detail = _http_ok(url.rstrip("/") + "/models")
    if not ok:
        report(FAIL, "reachable", f"{detail} — is the server running?")
        return
    report(OK, "reachable", detail)

    model = os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL")
    if not model:
        report(WARN, "LLM_MODEL_SMART", "not set")
        return

    try:
        from packages.llm import LLM

        llm = LLM(base_url=url, model=model, api_key=os.getenv("LLM_BEARER") or None,
                  silent=True)
        r = llm.generate(messages=[{"role": "user", "content": "Reply with OK."}],
                         think=False, max_tokens=16)
        if isinstance(r, str):
            report(FAIL, f"model {model}", r[:100])
        else:
            report(OK, f"model {model}", "responds")
    except Exception as exc:
        report(FAIL, f"model {model}", str(exc)[:100])


def check_tool_calling() -> None:
    """A model can converse fine and still never call a tool — which here means
    confident answers with no sources."""
    section("Tool calling (required for grounded answers)")
    url, model = os.getenv("LLM_DIRECT_URL"), os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL")
    if not (url and model):
        report(WARN, "skipped", "no model configured")
        return
    try:
        from packages.llm import LLM, get_tools, register_tool

        @register_tool
        def _doctor_probe(topic: str) -> str:
            """Look up a topic.

            Args:
                topic: What to look up.
            """
            return f"result for {topic}"

        llm = LLM(base_url=url, model=model, api_key=os.getenv("LLM_BEARER") or None,
                  tools=get_tools(["_doctor_probe"]), silent=True)
        llm.generate(messages=[{"role": "user",
                                "content": "Use the tool to look up 'energy'."}], think=False)
        if [m for m in llm.messages if m.get("role") == "tool"]:
            report(OK, "tool calling", "works")
        else:
            report(FAIL, "tool calling",
                   f"{model} did not call the tool — chat will answer without sources")
    except Exception as exc:
        report(WARN, "tool calling", str(exc)[:100])


def check_embeddings() -> None:
    section("Embeddings")
    url = os.getenv("EMBEDDING_BASE_URL")
    if not url:
        report(WARN, "EMBEDDING_BASE_URL", "not set — semantic search unavailable")
        return
    report(OK, "EMBEDDING_BASE_URL", url)
    try:
        from parliament import PARLIAMENT
        from postgres_client import pg

        vec = pg.make_embeddings(["doctor test"])[0]
        want = PARLIAMENT.embeddings.dimension
        if len(vec) == want:
            report(OK, "dimension", f"{len(vec)} matches parliament.yaml")
        else:
            report(FAIL, "dimension",
                   f"model returns {len(vec)}, config expects {want} — fix before ingesting")
    except Exception as exc:
        report(FAIL, "embedding call", str(exc)[:100])


# ── host ──────────────────────────────────────────────────────────────────────


def check_host() -> None:
    section("Host")
    for tool, why in (("psql", "applying the schema"), ("node", "building the frontend"),
                      ("npm", "building the frontend")):
        path = shutil.which(tool)
        report(OK if path else WARN, tool, path or f"not found — needed for {why}")

    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                              "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=8)
        if gpu.returncode == 0 and gpu.stdout.strip():
            report(OK, "GPU", gpu.stdout.strip().replace("\n", "; "))
        else:
            report(WARN, "GPU", "none detected — use Ollama on CPU, or a hosted provider")
    except Exception:
        report(WARN, "GPU", "nvidia-smi not available")

    if shutil.which("systemctl"):
        can_sudo = subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0
        report(OK if can_sudo else WARN, "sudo",
               "passwordless" if can_sudo else "will prompt — needed to install services")

    section("Ports")
    for port, what in ((8000, "API / vLLM"), (5432, "PostgreSQL"), (11434, "Ollama"),
                       (8003, "embeddings"), (8001, "MCP server"), (8005, "eval scorer")):
        report(OK if _port_open("127.0.0.1", port, 0.4) else WARN, f"{port}",
               f"{what}: " + ("in use" if _port_open("127.0.0.1", port, 0.4) else "free"))


def main() -> int:
    print("plenum doctor — checking this machine. Nothing will be changed.")
    for check in (check_python, check_config, check_database, check_chat_model,
                  check_tool_calling, check_embeddings, check_host):
        try:
            check()
        except Exception as exc:  # a broken check must not hide the others
            report(WARN, check.__name__, f"check itself failed: {exc}")

    failed = _results.count(FAIL)
    warned = _results.count(WARN)
    print(f"\n{_results.count(OK)} ok, {warned} warnings, {failed} failures")
    if failed:
        print("Fix the FAIL lines before continuing; see docs/SETUP.md.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
