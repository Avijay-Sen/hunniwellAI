#!/usr/bin/env python3
"""
migrate_to_kg.py — One-time backfill: Airtable -> kg/ (Supabase Postgres)

WHY THIS EXISTS: `--kg` on ingest.py is opt-in, so any company ingested
before that flag was used (or with the flag omitted) exists in Airtable but
NOT in the kg/ vector database. This script closes that gap by pulling every
existing Airtable record and writing it into kg/ as claims, using the exact
same functions ingest.py already uses for new records (resolve_company,
upsert_source, write_claims) — no new database logic, this only calls
existing kg/ functions.

WHAT IT DOES NOT DO: it does not touch Airtable (read-only there), does not
re-run Claude extraction (uses the already-extracted values sitting in
Airtable), and does not modify scrapers/ or ingest.py.

FIELD-NAME MATCHING: Airtable column names are matched to ingest.py's
FIELD_MAP loosely — case-insensitive, ignoring a trailing "(N)" column-number
suffix (e.g. "Class of device (7)" matches "Class of Device") and ignoring
periods ("Dev. Stage" == "Dev Stage"). So a table whose columns are numbered
still migrates correctly.

EVENT: if the table has no "Event" column, pass --default-event "<name>" and
every record without its own Event gets that value.

SAFETY / IDEMPOTENCY: Airtable's `upsert_source` always inserts a NEW source
row on every call (it is not idempotent by name despite the name). Since
claims_idem's uniqueness includes source_id, calling this script twice on the
same company would create a fresh source + duplicate claims each time. So
this script keeps its OWN local state file (kg_migration_state.json, next to
this script) recording which "<event>/<company>" keys have already been
migrated, exactly the same pattern ingest.py uses for .processed.json — skip
if already recorded, so re-runs are always safe.

Usage:
    python scripts/migrate_to_kg.py --dry-run --default-event "My Event"
    python scripts/migrate_to_kg.py --default-event "My Event"
    python scripts/migrate_to_kg.py --event "JPM 2026 (260115)"   # one event only
    python scripts/migrate_to_kg.py --reset-state                 # forget progress

Requires the same env vars as ai/airtable_ingest/ingest.py for Airtable
(AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME) plus whatever kg/
needs to connect (SUPABASE_DB_URL, or TEST_DATABASE_URL for the local test DB).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import requests

from kg.companies import resolve_company
from kg.config import connect
from kg.models import ClaimInput
from kg.sources import upsert_source
from kg.claims import write_claims

# Reuse the exact same field map ingest.py already uses, so json_key names
# match what write_claims expects — imported, not redefined, so this script
# can never drift out of sync with ingest.py's schema.
from ai.airtable_ingest.ingest import FIELD_MAP, _coerce_value, _KG_SKIP_KEYS

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "kg_migration_state.json"

_SUFFIX_RE = re.compile(r"\s*\(\d+\)\s*$")


def _norm(name: str) -> str:
    """Loose form of a column name used only for matching:
    'Class of device (7)' -> 'class of device',
    'Dev. Stage (9)'      -> 'dev stage',
    'Equity Raised ($M)(15)' -> 'equity raised ($m)'."""
    n = _SUFFIX_RE.sub("", name).strip().lower()
    n = n.replace(".", "")
    return re.sub(r"\s+", " ", n)


# Loose Airtable column name -> our internal json_key.
_NORMALIZED_FIELD_MAP = {_norm(v): k for k, v in FIELD_MAP.items()}

# Tag for claims written by this backfill. Distinct from "company_submitted"
# (used by live ingest) so it's always possible to tell which claims came from
# the original pipeline vs. this one-time migration. Reliability 4: these
# values already passed through the adversarial-audit step before Airtable.
_MIGRATION_SOURCE_KIND = "airtable_backfill"
_MIGRATION_RELIABILITY = 4


def load_state() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_state(state: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(state), indent=2))


def fetch_airtable_records(base_id: str, table: str, api_key: str, event_filter: str | None = None) -> list[dict]:
    """Pull every record from the Airtable table, paging through the API's
    100-per-page limit. Read-only — never writes back to Airtable."""
    url = f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}"
    headers = {"Authorization": f"Bearer {api_key}"}
    records: list[dict] = []
    offset = None

    while True:
        params = {}
        if offset:
            params["offset"] = offset
        if event_filter:
            # Airtable formula filter, exact match on the Event field.
            params["filterByFormula"] = f"{{Event}} = '{event_filter}'"

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))

        offset = data.get("offset")
        if not offset:
            break

    return records


def airtable_record_to_claim_inputs(
    fields: dict, default_event: str | None = None
) -> tuple[str, str, list[ClaimInput], list[str]]:
    """Convert one Airtable record's `fields` dict into
    (company, event, claim_inputs, ignored_column_names).

    Columns are matched to FIELD_MAP loosely (see _norm). `ignored` lists the
    columns present on the record that matched nothing, so they are reported
    rather than silently dropped."""
    mapped: dict[str, str] = {}
    ignored: list[str] = []
    for airtable_name, value in fields.items():
        json_key = _NORMALIZED_FIELD_MAP.get(_norm(airtable_name))
        if json_key is None:
            ignored.append(airtable_name)
            continue
        coerced = _coerce_value(value)
        if coerced:
            mapped[json_key] = coerced

    company = mapped.get("company", "").strip()
    event = mapped.get("event", "").strip() or (default_event or "").strip()

    claim_inputs = [
        ClaimInput(field=k, value=v)
        for k, v in mapped.items()
        if k not in _KG_SKIP_KEYS
    ]
    return company, event, claim_inputs, ignored


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill existing Airtable records into kg/.")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be written, make no kg/ writes.")
    ap.add_argument("--event", help="Restrict to one Event value (exact match).")
    ap.add_argument("--default-event", help="Event name to use for records that have no Event column/value.")
    ap.add_argument("--reset-state", action="store_true", help="Forget migration progress and exit.")
    args = ap.parse_args()

    if args.reset_state:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            print(f"Removed {STATE_FILE}")
        else:
            print("No state file to remove.")
        return

    airtable_key = os.environ.get("AIRTABLE_API_KEY")
    base_id = os.environ.get("AIRTABLE_BASE_ID")
    table_name = os.environ.get("AIRTABLE_TABLE_NAME")
    if not all([airtable_key, base_id, table_name]):
        print("ERROR: AIRTABLE_API_KEY / AIRTABLE_BASE_ID / AIRTABLE_TABLE_NAME must be set.")
        sys.exit(1)

    print("Fetching records from Airtable (read-only)...")
    records = fetch_airtable_records(base_id, table_name, airtable_key, event_filter=args.event)
    print(f"  found {len(records)} record(s)")

    state = load_state()
    conn = None if args.dry_run else connect()

    migrated = 0
    skipped = 0
    failed = 0
    all_ignored: set[str] = set()

    try:
        for rec in records:
            fields = rec.get("fields", {})
            company, event, claim_inputs, ignored = airtable_record_to_claim_inputs(
                fields, default_event=args.default_event
            )
            all_ignored.update(ignored)

            if not company or not event:
                missing = [n for n, v in (("Company", company), ("Event", event)) if not v]
                hint = "  (no Event column? pass --default-event \"<name>\")" if "Event" in missing else ""
                print(f"  skip: record {rec.get('id')} has no value for {missing}{hint}")
                skipped += 1
                continue

            state_key = f"{event}/{company}"
            if state_key in state:
                skipped += 1
                continue

            if not claim_inputs:
                print(f"  skip (no migratable fields): {state_key}")
                skipped += 1
                continue

            if args.dry_run:
                print(f"\n[dry-run] would migrate: {state_key}")
                for c in claim_inputs:
                    preview = c.value if len(c.value) <= 80 else c.value[:80] + "..."
                    print(f"    {c.field}: {preview}")
                migrated += 1
                continue

            try:
                # relpath is a synthetic key (not a real CompanyFiles path) —
                # fine, since it's only used as this migration's own dedup
                # key inside company_appearances, same role a scraped relpath
                # plays for a live ingest.
                relpath = f"airtable-backfill/{state_key}"
                company_id = resolve_company(conn, relpath, company, event)
                source_id = upsert_source(
                    conn,
                    company_id,
                    kind=_MIGRATION_SOURCE_KIND,
                    uri=None,
                    reliability=_MIGRATION_RELIABILITY,
                    title="Airtable backfill migration",
                    writer="migrate_to_kg",
                )
                write_claims(conn, company_id, source_id, claim_inputs, writer="migrate_to_kg")

                state.add(state_key)
                save_state(state)  # save after each company so a crash mid-run loses no progress
                migrated += 1
                print(f"  migrated: {state_key} ({len(claim_inputs)} claims)")
            except Exception as e:
                print(f"  ERROR migrating {state_key}: {e}")
                failed += 1
    finally:
        if conn is not None:
            conn.close()

    if all_ignored:
        print(f"\nAirtable columns NOT migrated (no match in FIELD_MAP): {sorted(all_ignored)}")
    print(f"\nDone. migrated={migrated} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()