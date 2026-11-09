"""Per-file document registry and ingest runs (migration 001).

The weekly file ingest (ai/file_ingest) uses these to decide what changed
since the last run and to keep exactly one source per file. Nothing here
commits unless the function name says so (start_run / finish_run); the
caller owns the transaction so a file is ingested all-or-nothing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

import psycopg

from kg.claims import _insert_claims
from kg.models import ClaimInput


@dataclass
class DocumentRow:
    id: str
    relpath: str
    sha256: Optional[str]
    size_bytes: Optional[int]
    mtime: Optional[datetime]
    status: str
    extractor_version: Optional[str]


@dataclass
class SyncResult:
    claim_ids: list[str]
    new: int
    kept: int
    superseded: int


# ---- ingest runs ---------------------------------------------------------------

def start_run(conn: psycopg.Connection, *, root: str, host: str, status: str = "running") -> str:
    with conn.cursor() as cur:
        cur.execute(
            "insert into ingest_runs (root, host, status) values (%s, %s, %s) returning id",
            (root, host, status),
        )
        run_id = str(cur.fetchone()[0])
    conn.commit()
    return run_id


def finish_run(conn: psycopg.Connection, run_id: str, *, status: str, stats: dict) -> None:
    conn.rollback()  # never let a failed file's transaction swallow the summary
    with conn.cursor() as cur:
        cur.execute(
            "update ingest_runs set finished_at = now(), status = %s, stats = %s where id = %s",
            (status, json.dumps(stats), run_id),
        )
    conn.commit()


# ---- documents -----------------------------------------------------------------

def load_documents(conn: psycopg.Connection, relpath_prefixes: Optional[Iterable[str]] = None) -> dict[str, DocumentRow]:
    """All known documents keyed by relpath, optionally limited to prefixes."""
    sql = "select id, relpath, sha256, size_bytes, mtime, status, extractor_version from documents"
    params: list = []
    prefixes = list(relpath_prefixes or [])
    if prefixes:
        sql += " where " + " or ".join(["relpath like %s"] * len(prefixes))
        params = [_like_prefix(p) for p in prefixes]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {
            r[1]: DocumentRow(str(r[0]), r[1], r[2], r[3], r[4], r[5], r[6])
            for r in cur.fetchall()
        }


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.rstrip("/") + "/%"


def upsert_document(
    conn: psycopg.Connection,
    *,
    relpath: str,
    run_id: str,
    company_id: Optional[str] = None,
    sha256: Optional[str] = None,
    size_bytes: Optional[int] = None,
    mtime: Optional[datetime] = None,
    file_kind: Optional[str] = None,
    period_id: Optional[str] = None,
    status: str = "active",
    error: Optional[str] = None,
    extractor_version: Optional[str] = None,
    ingested: bool = False,
) -> str:
    """Insert or update a document by relpath; returns its id. ``ingested``
    marks this run as the one that (re)extracted the file's claims."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into documents
                (relpath, company_id, sha256, size_bytes, mtime, file_kind, period_id,
                 status, error, extractor_version, first_seen_run, last_seen_run, last_ingested_run)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (relpath) do update set
                company_id = coalesce(excluded.company_id, documents.company_id),
                sha256 = excluded.sha256,
                size_bytes = excluded.size_bytes,
                mtime = excluded.mtime,
                file_kind = excluded.file_kind,
                period_id = coalesce(excluded.period_id, documents.period_id),
                status = excluded.status,
                error = excluded.error,
                extractor_version = excluded.extractor_version,
                last_seen_run = excluded.last_seen_run,
                last_ingested_run = coalesce(excluded.last_ingested_run, documents.last_ingested_run),
                updated_at = now()
            returning id
            """,
            (
                relpath, company_id, sha256, size_bytes, mtime, file_kind, period_id,
                status, error, extractor_version, run_id, run_id, run_id if ingested else None,
            ),
        )
        return str(cur.fetchone()[0])


def touch_documents(conn: psycopg.Connection, document_ids: list[str], run_id: str) -> None:
    """Record that unchanged documents were seen by this run."""
    if not document_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "update documents set last_seen_run = %s where id = any(%s::uuid[])",
            (run_id, document_ids),
        )


def supersede_document_claims(conn: psycopg.Connection, document_ids: list[str]) -> int:
    """Supersede every active claim from these documents' sources. Returns
    the number of claims superseded."""
    if not document_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "update claims set status = 'superseded' where status = 'active' and source_id in "
            "(select id from sources where document_id = any(%s::uuid[]))",
            (document_ids,),
        )
        return cur.rowcount


def retire_documents(conn: psycopg.Connection, document_ids: list[str], run_id: str) -> int:
    """Mark documents deleted (file gone from the folder) and supersede their
    active claims; history stays queryable. Returns claims superseded."""
    if not document_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "update documents set status = 'deleted', last_seen_run = %s, updated_at = now() "
            "where id = any(%s::uuid[])",
            (run_id, document_ids),
        )
    return supersede_document_claims(conn, document_ids)


# ---- claims for one document ----------------------------------------------------

def sync_document_claims(
    conn: psycopg.Connection,
    company_id: str,
    source_id: str,
    claims: list[ClaimInput],
    writer: str,
    *,
    period_id: Optional[str] = None,
    run_id: Optional[str] = None,
    as_of: Optional[date] = None,
) -> SyncResult:
    """Make the source's ACTIVE claims exactly ``claims``.

    Unchanged claims are kept (not re-embedded), new ones inserted, and active
    claims of this source that are no longer in the file are superseded, so a
    changed file keeps its old facts as history. Does not commit."""
    with conn.cursor() as cur:
        cur.execute(
            "select id from claims where source_id = %s and status = 'active'",
            (source_id,),
        )
        before = {r[0] for r in cur.fetchall()}

    ids = _insert_claims(
        conn, company_id, source_id, claims, writer,
        period_id=period_id, run_id=run_id, as_of=as_of,
    )
    kept_ids = set(ids)

    with conn.cursor() as cur:
        cur.execute(
            "update claims set status = 'superseded' "
            "where source_id = %s and status = 'active' and id <> all(%s)",
            (source_id, list(kept_ids)),
        )
        superseded = cur.rowcount

    return SyncResult(
        claim_ids=ids,
        new=len(kept_ids - before),
        kept=len(kept_ids & before),
        superseded=superseded,
    )
