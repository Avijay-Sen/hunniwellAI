"""Weekly CompanyFiles -> Supabase ingest (docs/ingestion-plan.md).

For every new or changed file under the event folders: extract text, write
searchable chunk claims (field 'doc_chunk') plus, unless --no-llm, structured
FIELD_MAP claims from Claude. Each file is written in one transaction with
ONE source per file, tagged with the event period and this run, so re-running
is idempotent and a changed file supersedes (not deletes) its old claims.

    python -m ai.file_ingest.run --root ~/OneDrive/CompanyFiles --dry-run
    python -m ai.file_ingest.run --root ... --event "JPM 2026 (260115)" --db-url postgresql://...localhost.../hunniwell_test
    python -m ai.file_ingest.run --root ... --yes-live          # writes to SUPABASE_DB_URL

Requires migration 001 (python scripts/kg_apply_schema.py).
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

import psycopg
from pgvector.psycopg import register_vector

from ai.file_ingest.extract import (
    EXTRACTOR_VERSION,
    chunk_text,
    extract_text,
    file_kind,
    source_kind_for,
)
from ai.file_ingest.scan import (
    MAX_FILE_BYTES_DEFAULT,
    FileEntry,
    ScanResult,
    classify,
    is_skipped,
    iter_files,
    select_event_folders,
)
from kg.companies import resolve_company_with_aliases
from kg.config import get_db_url
from kg.documents import (
    finish_run,
    load_documents,
    retire_documents,
    start_run,
    supersede_document_claims,
    sync_document_claims,
    touch_documents,
    upsert_document,
)
from kg.models import ClaimInput
from kg.periods import period_for_event
from kg.sources import upsert_source

WRITER = "file_ingest"
CHUNK_FIELD = "doc_chunk"


def db_host(url: str) -> str:
    """Host part of a DB URL, without credentials."""
    rest = url.split("@", 1)[1] if "@" in url else url.split("://", 1)[-1]
    return rest.split("/", 1)[0]


def is_local(url: str) -> bool:
    host = db_host(url).rsplit(":", 1)[0]
    return host in ("localhost", "127.0.0.1", "::1", "[::1]")


def build_claims(entry: FileEntry, text: str, field_claims: list[ClaimInput]) -> list[ClaimInput]:
    name = entry.path.name
    chunks = chunk_text(text)
    out = [
        ClaimInput(field=CHUNK_FIELD, value=f"[{name} — part {i}/{len(chunks)}]\n{c}")
        for i, c in enumerate(chunks, 1)
    ]
    return field_claims + out


def ingest_file(conn, entry: FileEntry, *, run_id: str, period_id: str, as_of, client, stats: Counter) -> None:
    """Extract and write one file in one transaction. Raises on failure (the
    caller records the error on the document and moves on)."""
    text = extract_text(entry.path)
    company_id = resolve_company_with_aliases(
        conn, entry.company_relpath, entry.company_name, entry.event_label
    )
    common = dict(
        relpath=entry.relpath, run_id=run_id, company_id=company_id, sha256=entry.sha256,
        size_bytes=entry.size_bytes, mtime=entry.mtime, file_kind=file_kind(entry.path),
        period_id=period_id, extractor_version=EXTRACTOR_VERSION,
    )
    if not text.strip():
        # e.g. a scanned PDF (no OCR yet): keep a record so it shows up in reports.
        doc_id = upsert_document(conn, status="empty", ingested=True, **common)
        n = supersede_document_claims(conn, [doc_id])  # file emptied since last run
        conn.commit()
        stats["files_empty"] += 1
        stats["claims_superseded"] += n
        return

    field_claims: list[ClaimInput] = []
    if client is not None:
        from ai.file_ingest.fields import extract_field_claims

        field_claims = extract_field_claims(client, entry.company_name, entry.path.name, text)
        stats["llm_calls"] += 1
    claims = build_claims(entry, text, field_claims)

    try:
        doc_id = upsert_document(conn, status="active", ingested=True, **common)
        source_id = upsert_source(
            conn, company_id, source_kind_for(entry.path), entry.relpath,
            title=entry.path.name, writer=WRITER, document_id=doc_id,
            period_id=period_id, captured_at=entry.mtime, commit=False,
        )
        res = sync_document_claims(
            conn, company_id, source_id, claims, WRITER,
            period_id=period_id, run_id=run_id, as_of=as_of,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    stats["files_ingested"] += 1
    stats["claims_new"] += res.new
    stats["claims_kept"] += res.kept
    stats["claims_superseded"] += res.superseded
    stats["field_claims"] += len(field_claims)


def record_skipped(conn, entry: FileEntry, status: str, *, run_id: str, period_id: str, stats: Counter) -> None:
    company_id = resolve_company_with_aliases(
        conn, entry.company_relpath, entry.company_name, entry.event_label
    )
    upsert_document(
        conn, relpath=entry.relpath, run_id=run_id, company_id=company_id,
        size_bytes=entry.size_bytes, mtime=entry.mtime, file_kind=file_kind(entry.path),
        period_id=period_id, status=status, extractor_version=EXTRACTOR_VERSION,
    )
    conn.commit()
    stats[f"files_{status}"] += 1


def record_error(conn, entry: FileEntry, err: Exception, *, run_id: str, period_id: str, stats: Counter) -> None:
    conn.rollback()
    upsert_document(
        conn, relpath=entry.relpath, run_id=run_id, sha256=entry.sha256,
        size_bytes=entry.size_bytes, mtime=entry.mtime, file_kind=file_kind(entry.path),
        period_id=period_id, status="error", error=f"{type(err).__name__}: {err}"[:2000],
        extractor_version=EXTRACTOR_VERSION,
    )
    conn.commit()
    stats["files_error"] += 1


def print_plan(scan: ScanResult, max_bytes: int) -> None:
    by_kind = Counter(file_kind(e.path) for e in scan.to_ingest)
    skipped = Counter(is_skipped(e, max_bytes) for e in scan.to_ingest if is_skipped(e, max_bytes))
    ingestable = [e for e in scan.to_ingest if not is_skipped(e, max_bytes)]
    companies = {e.company_relpath for e in scan.new + scan.changed + scan.unchanged}
    print(f"Events: {len(scan.event_folders)}  Companies: {len(companies)}")
    print(f"Files: new={len(scan.new)} changed={len(scan.changed)} "
          f"unchanged={len(scan.unchanged)} deleted={len(scan.deleted_relpaths)}")
    print(f"To ingest: {len(ingestable)} files  by type: {dict(by_kind)}")
    if skipped:
        print(f"Will record but skip: {dict(skipped)}")
    mb = sum(e.size_bytes for e in ingestable) / 1e6
    print(f"Ingestable size: {mb:.1f} MB  (~{len(ingestable)} LLM calls unless --no-llm)")


def run(
    conn: Optional[psycopg.Connection],
    root: Path,
    *,
    events: Optional[list[str]] = None,
    dry_run: bool = False,
    use_llm: bool = True,
    force: bool = False,
    limit: Optional[int] = None,
    max_bytes: int = MAX_FILE_BYTES_DEFAULT,
    client=None,
    log=print,
) -> dict:
    """Scan + ingest. With dry_run nothing is written (conn may be None, in
    which case every file counts as new). Returns the run stats."""
    event_dirs = select_event_folders(root, events)
    prefixes = [d.name for d in event_dirs]
    known: dict = {}
    if conn is not None:
        try:
            known = load_documents(conn, prefixes)
        except psycopg.errors.UndefinedTable:
            conn.rollback()
            if not dry_run:
                raise RuntimeError(
                    "database is missing migration 001 — run: python scripts/kg_apply_schema.py"
                )
            log("Note: database has no documents table yet (migration 001 not applied); "
                "every file counts as new.")
    scan = classify(iter_files(root, event_dirs), known, force=force, max_bytes=max_bytes)
    scan.event_folders = prefixes

    if dry_run:
        print_plan(scan, max_bytes)
        return {"dry_run": True, "new": len(scan.new), "changed": len(scan.changed),
                "unchanged": len(scan.unchanged), "deleted": len(scan.deleted_relpaths)}

    if use_llm and client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    if not use_llm:
        client = None

    stats: Counter = Counter()
    run_id = start_run(conn, root=str(root), host=socket.gethostname())
    started = time.monotonic()
    status = "succeeded"
    try:
        periods: dict[str, tuple] = {}
        for folder in prefixes:
            periods[folder] = period_for_event(conn, folder)
        conn.commit()

        touch_documents(conn, [known[e.relpath].id for e in scan.unchanged], run_id)
        conn.commit()
        stats["files_unchanged"] = len(scan.unchanged)

        todo = scan.to_ingest[:limit] if limit else scan.to_ingest
        for i, entry in enumerate(todo, 1):
            period_id, event_date = periods[entry.event_folder]
            as_of = event_date or entry.mtime.date()
            skipped = is_skipped(entry, max_bytes)
            try:
                if skipped:
                    record_skipped(conn, entry, skipped, run_id=run_id, period_id=period_id, stats=stats)
                    continue
                log(f"[{i}/{len(todo)}] {entry.relpath}")
                ingest_file(conn, entry, run_id=run_id, period_id=period_id, as_of=as_of,
                            client=client, stats=stats)
            except Exception as e:  # one bad file never aborts the run
                log(f"  ERROR {type(e).__name__}: {e}")
                record_error(conn, entry, e, run_id=run_id, period_id=period_id, stats=stats)

        if scan.deleted_relpaths and not limit:
            ids = [known[rp].id for rp in scan.deleted_relpaths]
            stats["claims_superseded"] += retire_documents(conn, ids, run_id)
            conn.commit()
            stats["files_deleted"] = len(ids)

        if stats["files_error"]:
            status = "partial"
    except BaseException:
        status = "failed"
        raise
    finally:
        stats["seconds"] = round(time.monotonic() - started, 1)
        finish_run(conn, run_id, status=status, stats=dict(stats))
    out = dict(stats)
    out["run_id"] = run_id
    out["status"] = status
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest CompanyFiles into the kg database.")
    ap.add_argument("--root", default=os.environ.get("HUNNIWELL_COMPANYFILES_ROOT"),
                    help="CompanyFiles root (default: $HUNNIWELL_COMPANYFILES_ROOT)")
    ap.add_argument("--event", action="append", help="Only this event folder (repeatable). "
                    "Default: every EVENT_LAYOUTS folder present under root.")
    ap.add_argument("--db-url", help="Target DB (default: TEST_DATABASE_URL, then SUPABASE_DB_URL)")
    ap.add_argument("--yes-live", action="store_true",
                    help="Required to write to a non-local database (e.g. Supabase).")
    ap.add_argument("--dry-run", action="store_true", help="Scan and report only; write nothing.")
    ap.add_argument("--no-llm", action="store_true", help="Skip Claude field extraction (chunks only).")
    ap.add_argument("--force", action="store_true", help="Re-ingest unchanged files too.")
    ap.add_argument("--limit", type=int, help="Ingest at most N files (testing).")
    ap.add_argument("--max-file-mb", type=float, default=MAX_FILE_BYTES_DEFAULT / 1024 / 1024)
    args = ap.parse_args(argv)

    if not args.root:
        ap.error("--root is required (or set HUNNIWELL_COMPANYFILES_ROOT)")
    root = Path(args.root).expanduser()
    if not root.is_dir():
        ap.error(f"root is not a directory: {root}")
    max_bytes = int(args.max_file_mb * 1024 * 1024)

    url = args.db_url
    if url is None:
        try:
            url = get_db_url()
        except RuntimeError:
            url = None

    conn = None
    if url is not None:
        if not args.dry_run and not is_local(url) and not args.yes_live:
            print(f"Refusing to write to non-local database {db_host(url)} without --yes-live.")
            return 2
        print(f"Database: {db_host(url)}" + (" (read-only dry run)" if args.dry_run else ""))
        conn = psycopg.connect(url, connect_timeout=20)
        register_vector(conn)
    elif not args.dry_run:
        print("No database URL configured (set SUPABASE_DB_URL or pass --db-url).")
        return 2

    try:
        stats = run(conn, root, events=args.event, dry_run=args.dry_run,
                    use_llm=not args.no_llm, force=args.force, limit=args.limit,
                    max_bytes=max_bytes)
    finally:
        if conn is not None:
            conn.close()
    if not args.dry_run:
        print("Run finished:", {k: v for k, v in sorted(stats.items())})
        return 0 if stats.get("status") == "succeeded" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
