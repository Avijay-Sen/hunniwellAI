#!/usr/bin/env python3
"""Stand up the kg/ schema on a target Postgres (local Docker or your own Supabase).

Enables the pgvector extension, creates the kg tables and applies every
kg/migrations file (idempotent — safe to re-run).

Usage:
    # uses TEST_DATABASE_URL, then SUPABASE_DB_URL, from your .env
    # (a non-local database also needs --yes-live; take a backup first):
    python scripts/kg_apply_schema.py

    # or target an explicit database:
    python scripts/kg_apply_schema.py --db-url "postgresql://user:pwd@host:5432/postgres"

Local quick start (no secrets):
    docker run -d --name hunni-pg -e POSTGRES_PASSWORD=postgres \\
      -e POSTGRES_DB=hunniwell_test -p 5432:5432 pgvector/pgvector:pg16
    python scripts/kg_apply_schema.py
"""
import argparse
import os
import sys

# Make the repo root importable when run as a plain file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from pgvector.psycopg import register_vector

from kg.config import apply_schema, get_db_url

EXPECTED = {"companies", "company_appearances", "sources", "claims"}


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply kg/schema.sql (enables pgvector) to a Postgres DB.")
    ap.add_argument("--db-url", help="Target DB URL. Defaults to TEST_DATABASE_URL then SUPABASE_DB_URL from .env.")
    ap.add_argument("--yes-live", action="store_true",
                    help="Required to change a non-local database (e.g. Supabase). Take a backup first.")
    args = ap.parse_args()

    url = args.db_url or get_db_url()
    where = url.split("@", 1)[1] if "@" in url else url  # never print the password
    host = where.split("/", 1)[0].rsplit(":", 1)[0]
    if host not in ("localhost", "127.0.0.1") and not args.yes_live:
        print(f"Refusing to change non-local database {host} without --yes-live.")
        sys.exit(2)
    print(f"Applying kg schema + migrations to ...@{where}")

    conn = psycopg.connect(url, connect_timeout=20)
    conn.execute("create extension if not exists vector")
    conn.commit()
    register_vector(conn)
    newly = apply_schema(conn)
    print("Migrations applied now:", newly or "none (already up to date)")
    have = {r[0] for r in conn.execute(
        "select tablename from pg_tables where schemaname='public'").fetchall()} & EXPECTED
    conn.close()

    print("Tables present:", sorted(have))
    if have >= EXPECTED:
        print("OK: kg schema ready.")
    else:
        print("ERROR: missing", sorted(EXPECTED - have))
        sys.exit(1)


if __name__ == "__main__":
    main()
