from datetime import datetime
from typing import Optional

import psycopg

from kg.models import DEFAULT_RELIABILITY


def upsert_source(
    conn: psycopg.Connection,
    company_id: str,
    kind: str,
    uri: Optional[str],
    *,
    tag: Optional[str] = None,
    reliability: Optional[int] = None,
    title: Optional[str] = None,
    writer: str,
    document_id: Optional[str] = None,
    period_id: Optional[str] = None,
    captured_at: Optional[datetime] = None,
    commit: bool = True,
) -> str:
    """Insert a source row, defaulting reliability from DEFAULT_RELIABILITY[kind].

    Returns the source id (uuid string). When reliability is None and kind
    is unknown, reliability is stored as NULL (no KeyError).

    Without ``document_id`` this always inserts a new row (original behaviour).
    With ``document_id`` it is an upsert: a file keeps ONE source across ingest
    runs (unique sources.document_id, migration 001), so claims_idem dedupes
    re-ingested claims instead of duplicating them. ``document_id``,
    ``period_id`` and ``captured_at`` need migration 001 and are only sent when
    given, so callers that don't pass them work on an un-migrated database.

    ``commit=False`` leaves the write in the caller's transaction.
    """
    if reliability is None:
        reliability = DEFAULT_RELIABILITY.get(kind)

    cols = ["company_id", "kind", "reliability", "uri", "tag", "title", "writer"]
    vals: list = [company_id, kind, reliability, uri, tag, title, writer]
    for col, val in (("document_id", document_id), ("period_id", period_id), ("captured_at", captured_at)):
        if val is not None:
            cols.append(col)
            vals.append(val)

    sql = (
        f"insert into sources ({', '.join(cols)}) "
        f"values ({', '.join(['%s'] * len(cols))}) "
    )
    if document_id is not None:
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "document_id")
        sql += f"on conflict (document_id) where document_id is not null do update set {updates} "
    sql += "returning id"

    with conn.cursor() as cur:
        cur.execute(sql, vals)
        source_id = cur.fetchone()[0]
    if commit:
        conn.commit()
    return str(source_id)
