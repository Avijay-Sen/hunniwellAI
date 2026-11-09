import os
from pathlib import Path

import psycopg
import pytest

from kg.config import apply_schema

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/hunniwell_test",
)
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "kg" / "schema.sql"
EXPECTED_TABLES = {"companies", "company_appearances", "sources", "claims"}


@pytest.fixture()
def raw_conn():
    conn = psycopg.connect(TEST_DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "drop table if exists claims, sources, documents, company_aliases, "
            "company_appearances, companies, ingest_runs, periods, "
            "schema_migrations cascade"
        )
    try:
        yield conn
    finally:
        conn.close()


def _tables(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public'"
        )
        return {r[0] for r in cur.fetchall()}


def _indexes(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "select indexname from pg_catalog.pg_indexes "
            "where schemaname = 'public'"
        )
        return {r[0] for r in cur.fetchall()}


def test_apply_schema_creates_all_objects(raw_conn):
    apply_schema(raw_conn, SCHEMA_PATH)

    tables = _tables(raw_conn)
    assert EXPECTED_TABLES <= tables

    indexes = _indexes(raw_conn)
    assert "claims_idem" in indexes
    assert "claims_embedding" in indexes

    # claims_idem must be a UNIQUE index
    with raw_conn.cursor() as cur:
        cur.execute(
            "select indisunique from pg_catalog.pg_index i "
            "join pg_catalog.pg_class c on c.oid = i.indexrelid "
            "where c.relname = 'claims_idem'"
        )
        assert cur.fetchone()[0] is True

    # claims_embedding must use the hnsw access method
    with raw_conn.cursor() as cur:
        cur.execute(
            "select am.amname from pg_catalog.pg_class c "
            "join pg_catalog.pg_am am on am.oid = c.relam "
            "where c.relname = 'claims_embedding'"
        )
        assert cur.fetchone()[0] == "hnsw"


def test_apply_schema_is_idempotent(raw_conn):
    apply_schema(raw_conn, SCHEMA_PATH)
    # second application must not raise
    apply_schema(raw_conn, SCHEMA_PATH)
    assert EXPECTED_TABLES <= _tables(raw_conn)
