#!/usr/bin/env python3
"""Review company folder names the file ingest held back (migration 002).

A name is held when it doesn't match an existing company exactly but is close
to one (e.g. "Acme Health AI" vs "Acme Health"). Its files are not ingested
until you decide:

    python scripts/review_unmatched.py                       # list pending names
    python scripts/review_unmatched.py --alias "Acme Health AI"            # same company as the suggestion
    python scripts/review_unmatched.py --alias "Acme Health AI" --to HW-0007   # same as another company
    python scripts/review_unmatched.py --new "Acme Health AI"              # a different, new company
    python scripts/review_unmatched.py --all                 # include already-decided names

The next ingest run picks the held files up. Uses TEST_DATABASE_URL, then
SUPABASE_DB_URL (or --db-url); changing a non-local database needs --yes-live.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg

from kg.companies import approve_alias, approve_new, list_unmatched
from kg.config import get_db_url


def _host(url: str) -> str:
    where = url.split("@", 1)[1] if "@" in url else url  # never print the password
    return where.split("/", 1)[0].rsplit(":", 1)[0]


def _company_id(conn, ref: str) -> str:
    """Accept HW-0007, 7, or a company uuid."""
    code = ref.upper().removeprefix("HW-")
    with conn.cursor() as cur:
        if code.isdigit():
            cur.execute("select id from companies where company_code = %s", (int(code),))
        else:
            cur.execute("select id from companies where id::text = %s", (ref,))
        row = cur.fetchone()
    if row is None:
        raise SystemExit(f"No company {ref!r}.")
    return str(row[0])


def _print(rows: list[dict]) -> None:
    if not rows:
        print("Nothing to review.")
        return
    for r in rows:
        cand = (f"HW-{r['candidate_code']:04d} {r['candidate_name']}" if r["candidate_code"] is not None
                else "(none)")
        print(f"[{r['status']}] {r['name_raw']!r}\n    folder: {r['example_relpath']}\n"
              f"    closest existing: {cand}  score={r['score']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--alias", metavar="NAME", help="NAME is another spelling of an existing company")
    g.add_argument("--new", metavar="NAME", help="NAME is a new company")
    ap.add_argument("--to", metavar="COMPANY", help="with --alias: HW-code or id (default: the suggestion)")
    ap.add_argument("--all", action="store_true", help="list decided names too")
    ap.add_argument("--db-url")
    ap.add_argument("--yes-live", action="store_true", help="required to change a non-local database")
    args = ap.parse_args(argv)
    if args.to and not args.alias:
        ap.error("--to needs --alias")

    url = args.db_url or get_db_url()
    host = _host(url)
    changing = bool(args.alias or args.new)
    if changing and host not in ("localhost", "127.0.0.1") and not args.yes_live:
        print(f"Refusing to change non-local database {host} without --yes-live.")
        return 2
    print(f"Database: {host}")

    with psycopg.connect(url, connect_timeout=20) as conn:
        try:
            if args.alias:
                target = approve_alias(conn, args.alias, _company_id(conn, args.to) if args.to else None)
                print(f"{args.alias!r} is now an alias of company {target}.")
            elif args.new:
                approve_new(conn, args.new)
                print(f"{args.new!r} will be created as a new company on the next ingest run.")
            else:
                _print(list_unmatched(conn, None if args.all else "pending"))
        except ValueError as e:
            print(f"Error: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
