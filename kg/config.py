from __future__ import annotations

import os
from pathlib import Path
from typing import Union

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

load_dotenv()

_DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def get_db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError(
            "No database URL: set SUPABASE_DB_URL (or TEST_DATABASE_URL)."
        )
    return url


def get_voyage_key() -> str:
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        raise RuntimeError("VOYAGE_API_KEY is not set.")
    return key


def get_embeddings_provider() -> str:
    """Embedding backend for embed(): 'voyage' (API, default) or 'local'
    (sentence-transformers, runs in-process, no server/API call)."""
    return (os.environ.get("EMBEDDINGS_PROVIDER") or "voyage").strip().lower()

def get_local_embed_model() -> str:
    """Hugging Face model id loaded in-process by the 'local' backend.
    Override via LOCAL_EMBED_MODEL if you want a different model; defaults to
    mxbai-embed-large-v1 (1024-dim, matches schema.sql's vector(1024))."""
    return os.environ.get("LOCAL_EMBED_MODEL") or "mixedbread-ai/mxbai-embed-large-v1"


def connect() -> psycopg.Connection:
    conn = psycopg.connect(get_db_url())
    register_vector(conn)
    return conn


def migration_files() -> list[Path]:
    """kg/migrations/NNN_*.sql in apply order."""
    return sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply pending kg/migrations files in order, one transaction per file,
    recording each in schema_migrations; returns the versions applied now.

    Already-recorded versions are skipped: their ALTER TABLEs would take
    exclusive locks on busy tables (claims) on every call. Each file is still
    written to be idempotent, so a failed apply can simply be re-run."""
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.schema_migrations') is not null")
        if cur.fetchone()[0]:
            cur.execute("select version from schema_migrations")
            done = {r[0] for r in cur.fetchall()}
        else:
            done = set()
    conn.commit()

    newly: list[str] = []
    for path in migration_files():
        version = path.stem
        if version in done:
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute(
                    "insert into schema_migrations (version) values (%s) "
                    "on conflict (version) do nothing returning version",
                    (version,),
                )
                if cur.fetchone() is not None:
                    newly.append(version)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return newly


def apply_schema(
    conn: psycopg.Connection,
    sql_path: Union[str, Path, None] = None,
    *,
    migrations: bool = True,
) -> list[str]:
    """Apply kg/schema.sql (idempotent: create ... if not exists), then every
    migration in kg/migrations unless ``migrations=False``. Returns the
    migration versions applied for the first time."""
    path = Path(sql_path) if sql_path is not None else _DEFAULT_SCHEMA_PATH
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    return apply_migrations(conn) if migrations else []
