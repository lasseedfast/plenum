"""Child-process entrypoint for background research jobs.

Spawned by ``backend.services.research.jobs.spawn_job`` as
``python -m backend.job_runner`` with a JSON spec
``{job_id, kind, params, secrets}`` on stdin. Imports the handler modules
(which populates the job registry) and runs the spec to completion.

``secrets`` may carry the board key and a user-supplied provider override. They
arrive over the stdin pipe, never as argv and never via the DB, and live only in
this process's memory for as long as the job runs.

Note: importing anything under ``backend.*`` runs ``backend/__init__.py``,
which eagerly imports ``backend.app`` and thus builds the chat singletons.
That is cheap and safe here — the LLM constructors only build lazy OpenAI
client objects (no network until a call is made) and the API startup hook does
not fire on import. The handlers still build exactly the LLMs they need rather
than reusing the chat service.
"""
from __future__ import annotations

import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("riksdagen.job_runner")


def main() -> int:
    raw = sys.stdin.read()
    try:
        spec = json.loads(raw)
    except Exception:
        log.error("job_runner: could not parse spec from stdin: %r", raw[:200])
        return 2

    # Importing handlers populates the registry; importing anything under
    # backend.* also triggers config.py -> env_manager.set_env() for env vars.
    import backend.services.research.handlers  # noqa: F401
    from backend.services.research.jobs import execute_spec

    execute_spec(spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
