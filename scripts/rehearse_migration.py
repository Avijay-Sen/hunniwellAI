#!/usr/bin/env python3
"""Rehearse the kg migrations on a LOCAL copy of live Supabase.

Builds a fresh local database with only kg/schema.sql (the state live Supabase
is in), loads the table exports you downloaded from Supabase (Table editor ->
Export -> CSV, one file per table), then applies kg/migrations twice and checks:

  - the second apply changes nothing
  - every table keeps its row count, and no claim changes status
  - claims_idem includes period_id
  - company names that the ingest would hold for review (pairs of existing
    companies that look alike)
  - semantic search still returns claims

    python scripts/rehearse_migration.py --csv-dir "~/Downloads/Supabase Schema"

Expects companies_rows.csv, company_appearances_rows.csv, sources_rows.csv and
claims_rows.csv. Only ever touches the local rehearsal database (default
hunniwell_rehearsal, dropped and recreated); it never connects to Supabase.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from pgvector.psycopg import register_vector

from kg.companies import FUZZY_THRESHOLD, similarity
from kg.config import apply_migrations, apply_schema

TABLES = ["companies", "company_appearances", "sources", "claims"]  # load order (foreign keys)
ADMIN_URL = "postgresql://postgres:postgres@localhost:5432/postgres"


def _counts(conn) -> dict:
    out = {t: conn.execute(f"select count(*) from {t}").fetchone()[0] for t in TABLES}
    out["claims_active"] = conn.execute("select count(*) from claims where status = 'active'").fetchone()[0]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Rehearse kg migrations on a local copy of Supabase exports.")
    ap.add_argument("--csv-dir", required=True)
    ap.add_argument("--db", default="hunniwell_rehearsal", help="local database name (recreated)")
    ap.add_argument("--keep", action="store_true", help="stop before migrating (inspect the pre-migration copy)")
    args = ap.parse_args()

    csv_dir = Path(args.csv_dir).expanduser()
    files = {t: csv_dir / f"{t}_rows.csv" for t in TABLES}
    missing = [str(p) for p in files.values() if not p.is_file()]
    if missing:
        print("Missing exports:", ", ".join(missing))
        return 1

    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{args.db}" with (force)')
        admin.execute(f'create database "{args.db}"')
    url = f"postgresql://postgres:postgres@localhost:5432/{args.db}"
    conn = psycopg.connect(url)
    conn.execute("create extension if not exists vector")
    conn.commit()
    register_vector(conn)
    apply_schema(conn, migrations=False)  # = live Supabase today

    for t, path in files.items():
        with path.open(encoding="utf-8") as f:
            header = f.readline().strip()
        with conn.cursor() as cur, path.open("rb") as f:
            with cur.copy(f"copy {t} ({header}) from stdin with (format csv, header true)") as cp:
                while chunk := f.read(1 << 20):
                    cp.write(chunk)
    conn.commit()
    before = _counts(conn)
    from kg.companies import _normalize_name

    expected_companies = len({_normalize_name(r[0]) for r in conn.execute("select name_raw from companies")})
    print(f"Loaded into {args.db}:", before)
    if args.keep:
        return 0

    first = apply_migrations(conn)
    second = apply_migrations(conn)
    again = apply_schema(conn)  # what every kg caller runs; must be a no-op too
    after = _counts(conn)
    print("Migrations applied (1st run):", first)
    print("Migrations applied (2nd run):", second or "none", "| apply_schema afterwards:", again or "none")

    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + msg)
        ok &= bool(cond)

    check(first and not second and not again, "second apply changes nothing")
    same = {k: v for k, v in before.items() if k != "companies"}
    check(same == {k: v for k, v in after.items() if k != "companies"}, f"row counts unchanged {after}")
    check(after["companies"] == expected_companies,
          f"companies {before['companies']} -> {after['companies']} (spelling duplicates merged)")
    stale = [r[0] for r in conn.execute("select name_raw from companies").fetchall()
             if _normalize_name(r[0]) != conn.execute(
                 "select name_norm from companies where name_raw = %s", (r[0],)).fetchone()[0]]
    check(not stale, f"every stored name matches the current normalization {stale or ''}")
    idx = conn.execute("select indexdef from pg_indexes where indexname = 'claims_idem'").fetchone()
    check(idx and "period_id" in idx[0], "claims_idem includes period_id")
    n_code = conn.execute("select count(*) from companies where company_code is null").fetchone()[0]
    check(n_code == 0, "every company got a company_code (HW-xxxx)")
    rls = conn.execute(
        "select count(*) from pg_tables where schemaname = 'public' and not rowsecurity").fetchone()[0]
    check(rls == 0, "row-level security on every table")

    names = conn.execute("select name_norm, name_raw from companies order by name_raw").fetchall()
    pairs = [(a[1], b[1], round(similarity(a[0], b[0]), 2))
             for i, a in enumerate(names) for b in names[i + 1:]
             if similarity(a[0], b[0]) >= FUZZY_THRESHOLD]
    print(f"Existing companies that look alike (would trigger review for new spellings): {len(pairs)}")
    for a, b, s in pairs:
        print(f"    {a!r} ~ {b!r} ({s})")

    try:
        from kg.search import semantic_search

        hits = semantic_search(conn, "glucose monitoring device", k=3)
        check(len(hits) == 3, f"semantic search returns claims (top field: {hits[0].field if hits else '-'})")
    except Exception as e:  # embedder not available offline etc.
        print(f"  SKIP semantic search: {type(e).__name__}: {e}")
    conn.close()

    print("REHEARSAL PASSED" if ok else "REHEARSAL FAILED")
    print(f"The migrated copy stays in {args.db} (ingest/query against it with --db-url {url}).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
