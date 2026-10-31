from __future__ import annotations

import os
from pathlib import Path
from typing import Union

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

load_dotenv()

_DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


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


def apply_schema(
    conn: psycopg.Connection,
    sql_path: Union[str, Path, None] = None,
) -> None:
    """Apply kg/schema.sql to ``conn``. Idempotent (create ... if not exists)."""
    path = Path(sql_path) if sql_path is not None else _DEFAULT_SCHEMA_PATH
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
