#!/usr/bin/env python3
"""
check_airtable.py — confirm your Airtable credentials work, before running
anything that touches real data.

Read-only: makes ONE GET request for up to 20 records. Never writes to Airtable
or to the kg/ database. Never prints your API key.

Reads AIRTABLE_API_KEY / AIRTABLE_BASE_ID / AIRTABLE_TABLE_NAME from
ai/airtable_ingest/.env (the same file migrate_to_kg.py uses), and URL-encodes
the table name for you, so spaces in the name are fine.

Run from the repo root:
    python scripts/check_airtable.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

ENV_PATH = Path("ai/airtable_ingest/.env")


def main() -> None:
    if not ENV_PATH.exists():
        print(f"ERROR: {ENV_PATH} not found. Run this from the repo root, "
              f"and create the file first.")
        sys.exit(1)

    load_dotenv(ENV_PATH)
    key = os.environ.get("AIRTABLE_API_KEY", "").strip()
    base = os.environ.get("AIRTABLE_BASE_ID", "").strip()
    table = os.environ.get("AIRTABLE_TABLE_NAME", "").strip()

    # Show which values are present, without revealing them.
    print(f"API key set:     {bool(key)}")
    print(f"Base ID set:     {bool(base)}  (starts with 'app': {base.startswith('app')})")
    print(f"Table name set:  {bool(table)}")
    if not (key and base and table):
        print("\nSTOP: at least one value is empty in ai/airtable_ingest/.env")
        sys.exit(1)

    url = f"https://api.airtable.com/v0/{base}/{quote(table, safe='')}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {key}"},
            params={"maxRecords": 20},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"\nERROR: could not reach Airtable: {e}")
        sys.exit(1)

    print(f"\nHTTP status: {resp.status_code}")
    data = resp.json()

    if "records" not in data:
        # Airtable error bodies look like {"error": {"type": "...", ...}}
        print("Airtable returned an error:")
        print(data)
        print("\nSTOP: fix the credential above before continuing. Common causes:\n"
              "  AUTHENTICATION_REQUIRED -> key missing/malformed\n"
              "  NOT_FOUND / NOT_AUTHORIZED -> wrong base ID, wrong table name, "
              "or token not granted access to this base\n"
              "  INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND -> token lacks "
              "data.records:read on this base")
        sys.exit(1)

    records = data["records"]
    print(f"Records returned: {len(records)}")
    if not records:
        print("The table is reachable but returned no records.")
        return

    # Airtable omits empty fields from each record, so union across the sample.
    seen: set[str] = set()
    for r in records:
        seen.update(r.get("fields", {}).keys())
    print(f"\nField names seen in this sample ({len(seen)}):")
    for name in sorted(seen):
        print(f"  - {name}")

    # Compare against the field names the migration script expects.
    try:
        from ai.airtable_ingest.ingest import FIELD_MAP

        expected = set(FIELD_MAP.values())
        missing = sorted(expected - seen)
        unexpected = sorted(seen - expected)
        print(f"\nFields migrate_to_kg.py expects but did NOT see in this sample ({len(missing)}):")
        for name in missing:
            print(f"  - {name}")
        print("(Missing here can just mean those cells were empty in the sampled "
              "records, since Airtable omits empty fields. It is only a problem "
              "if a name is spelled differently in Airtable.)")
        if unexpected:
            print(f"\nFields in Airtable that the migration will ignore ({len(unexpected)}):")
            for name in unexpected:
                print(f"  - {name}")
    except Exception as e:  # import problems shouldn't hide the main result
        print(f"\n(Skipped the field-name comparison: {e})")

    print("\nOK: credentials work.")


if __name__ == "__main__":
    main()
