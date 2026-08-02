"""
PostgreSQL client for the Riksdagen project.

Provides a simple interface mirroring _arango/_arango.py, using psycopg2
with a thread-safe connection pool. Includes pgvector support for embeddings.

Environment variables:
  PG_HOST     - PostgreSQL host (default: localhost)
  PG_PORT     - PostgreSQL port (default: 5432)
  PG_DB       - Database name (default: riksdagen)
  PG_USER     - Username (default: riksdagen)
  PG_PASSWORD - Password
"""

import threading
import os
from typing import Any, List, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv
from pgvector.psycopg2 import register_vector
from openai import OpenAI

load_dotenv()


class Postgres:
    """
    Thread-safe PostgreSQL client with a connection pool.

    Use execute() for queries that return rows (SELECT).
    Use execute_void() for queries that don't return rows (INSERT/UPDATE/DELETE).
    Use execute_many() for batch inserts with executemany().
    """

    def __init__(
        self,
        host: str = None,
        port: int = None,
        dbname: str = None,
        user: str = None,
        password: str = None,
        minconn: int = 1,
        maxconn: int = 6,
    ):
        self.host = host or os.environ.get("PG_HOST", "localhost")
        self.port = int(port or os.environ.get("PG_PORT", 5432))
        self.dbname = dbname or os.environ.get("PG_DB", "riksdagen")
        self.user = user or os.environ.get("PG_USER", "riksdagen")
        self.password = password or os.environ.get("PG_PASSWORD", "")

        self.minconn = int(os.environ.get("PG_POOL_MINCONN", minconn))
        self.maxconn = int(os.environ.get("PG_POOL_MAXCONN", maxconn))
        self.application_name = os.environ.get("PG_APPLICATION_NAME", "riksdagen-app")
        self.session_options = self._build_session_options()

        # The pool is opened on first use, not here. Constructing it eagerly made
        # `import backend.app` fail outright without a reachable database, which
        # broke test collection, `--help`, and any tooling that merely imports the app.
        self._pool = None
        self._pool_lock = threading.Lock()

    @property
    def pool(self) -> psycopg2.pool.ThreadedConnectionPool:
        """Open the connection pool on first access."""
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:  # another thread may have won the race
                    self._pool = psycopg2.pool.ThreadedConnectionPool(
                        self.minconn,
                        self.maxconn,
                        host=self.host,
                        port=self.port,
                        dbname=self.dbname,
                        user=self.user,
                        password=self.password,
                        application_name=self.application_name,
                        options=self.session_options,
                    )
        return self._pool

    def _build_session_options(self) -> str:
        """
        Build PostgreSQL session limits for each pooled connection.

        These are application-side guardrails for long or memory-heavy queries.
        They are intentionally configurable through environment variables so the
        backend can stay conservative while batch jobs can opt into looser caps.
        """
        option_parts: list[str] = []

        work_mem = os.environ.get("PG_WORK_MEM", "32MB")
        if work_mem:
            option_parts.append(f"-c work_mem={work_mem}")

        temp_file_limit = os.environ.get("PG_TEMP_FILE_LIMIT", "1GB")
        if temp_file_limit:
            option_parts.append(f"-c temp_file_limit={temp_file_limit}")

        statement_timeout_ms = os.environ.get("PG_STATEMENT_TIMEOUT_MS", "30000")
        if statement_timeout_ms:
            option_parts.append(f"-c statement_timeout={statement_timeout_ms}")

        lock_timeout_ms = os.environ.get("PG_LOCK_TIMEOUT_MS", "5000")
        if lock_timeout_ms:
            option_parts.append(f"-c lock_timeout={lock_timeout_ms}")

        idle_timeout_ms = os.environ.get(
            "PG_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS", "10000"
        )
        if idle_timeout_ms:
            option_parts.append(
                f"-c idle_in_transaction_session_timeout={idle_timeout_ms}"
            )

        return " ".join(option_parts)

    def _get_conn(self):
        conn = self.pool.getconn()
        register_vector(conn)
        return conn

    def _put_conn(self, conn):
        self.pool.putconn(conn)

    def execute(self, query: str, params: Optional[tuple] = None) -> List[dict]:
        """
        Execute a query and return all rows as a list of dicts.
        Use for SELECT queries.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                conn.commit()
                if cur.description:
                    return [dict(row) for row in cur.fetchall()]
                return []
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def execute_void(self, query: str, params: Optional[tuple] = None) -> None:
        """
        Execute a query that returns no rows (INSERT/UPDATE/DELETE).
        """
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def execute_many(self, query: str, params_list: List[tuple]) -> None:
        """
        Execute a query for each item in params_list (batch insert/update).
        Uses execute_batch for performance.
        """
        if not params_list:
            return
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, query, params_list, page_size=500)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def execute_values(self, query: str, params_list: List[tuple], template: str = None) -> None:
        """
        Bulk insert using execute_values (much faster than execute_many for large batches).
        query should be like: INSERT INTO table (col1, col2) VALUES %s
        """
        if not params_list:
            return
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur, query, params_list, template=template, page_size=500
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def make_embeddings(self, texts: List[str]) -> List[List[float]]:
        # 1. Setup Client
        from parliament import PARLIAMENT

        base_url = os.environ.get(PARLIAMENT.embeddings.base_url_env)
        if not base_url:
            raise RuntimeError(
                f"{PARLIAMENT.embeddings.base_url_env} is not set. It must point at an "
                f"OpenAI-compatible embeddings endpoint, e.g. http://localhost:8003/v1"
            )
        client = OpenAI(base_url=base_url, api_key=os.environ.get("EMBEDDING_API_KEY", "none"))

        # 2. Request Embeddings
        # We pass 'dimensions' in the body. Qwen3 usually supports this.
        response = client.embeddings.create(
            input=texts,
            model=os.environ.get("LLM_MODEL_EMBEDDING", PARLIAMENT.embeddings.model),
            extra_body={"dimensions": PARLIAMENT.embeddings.dimension},
        )

        # 3. Safety Slice
        # Even if the server returns the full 3584 dims by mistake, 
        # this ensures your DB doesn't throw a dimension mismatch error.
        dim = PARLIAMENT.embeddings.dimension
        # Truncate defensively: not every server honours the `dimensions` request,
        # and a wider vector than the column would fail deep inside pgvector.
        return [emb.embedding[:dim] for emb in response.data]

    def close(self):
        """Close all connections in the pool."""
        self.pool.closeall()
