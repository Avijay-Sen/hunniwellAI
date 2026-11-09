"""Time periods that claims are tagged with (docs/ingestion-plan.md).

An *event* period comes from an event folder whose name ends in a (YYMMDD)
date suffix, e.g. "JPM 2026 (260115)" -> 2026-01-15. Folders without a date
get a *historical* period with no dates. *Weekly* periods ("2026-W45") label
the week an ingest ran. Functions here do not commit; the caller does.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

import psycopg

_DATE_SUFFIX_RE = re.compile(r"\((\d{2})(\d{2})(\d{2})\)\s*$")


def parse_event_date(label: str) -> Optional[date]:
    """Date from a trailing (YYMMDD) suffix, or None if absent/invalid."""
    m = _DATE_SUFFIX_RE.search(label or "")
    if not m:
        return None
    yy, mm, dd = (int(g) for g in m.groups())
    try:
        return date(2000 + yy, mm, dd)
    except ValueError:
        return None


def get_or_create_period(
    conn: psycopg.Connection,
    label: str,
    kind: str,
    starts_on: Optional[date] = None,
    ends_on: Optional[date] = None,
) -> str:
    """Idempotent on label; returns the period id. An existing period keeps
    its original kind/dates."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into periods (label, kind, starts_on, ends_on)
            values (%s, %s, %s, %s)
            on conflict (label) do update set label = excluded.label
            returning id
            """,
            (label, kind, starts_on, ends_on),
        )
        return str(cur.fetchone()[0])


def period_for_event(conn: psycopg.Connection, event_folder: str) -> tuple[str, Optional[date]]:
    """(period_id, event_date) for an event folder name."""
    d = parse_event_date(event_folder)
    if d is None:
        return get_or_create_period(conn, event_folder.strip(), "historical"), None
    return get_or_create_period(conn, event_folder.strip(), "event", d, d), d


def weekly_label(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def weekly_period(conn: psycopg.Connection, d: date) -> str:
    monday = d - timedelta(days=d.weekday())
    return get_or_create_period(conn, weekly_label(d), "weekly", monday, monday + timedelta(days=6))
