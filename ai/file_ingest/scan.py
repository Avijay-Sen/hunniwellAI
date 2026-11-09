"""Walk the CompanyFiles tree and classify each file against the documents
registry: new / changed / unchanged / deleted.

Layout and event whitelist come from ai/airtable_ingest (iter_event_companies,
events.EVENT_LAYOUTS): <Event>/<Company>/... or <Event>/<Category>/<Company>/...
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ai.airtable_ingest.events import EVENT_LAYOUTS
from ai.airtable_ingest.ingest import iter_event_companies
from ai.file_ingest.extract import EXTRACTOR_VERSION, file_kind, is_ignored

MAX_FILE_BYTES_DEFAULT = 50 * 1024 * 1024


@dataclass
class FileEntry:
    path: Path
    relpath: str  # relative to the root, POSIX separators — the documents key
    event_folder: str  # top-level event folder name (drives the period)
    event_label: str  # "<event>" or "<event> - <category>" (company_appearances.event)
    company_name: str
    company_relpath: str
    size_bytes: int
    mtime: datetime
    sha256: Optional[str] = None


@dataclass
class ScanResult:
    new: list[FileEntry] = field(default_factory=list)
    changed: list[FileEntry] = field(default_factory=list)
    unchanged: list[FileEntry] = field(default_factory=list)
    deleted_relpaths: list[str] = field(default_factory=list)
    event_folders: list[str] = field(default_factory=list)

    @property
    def to_ingest(self) -> list[FileEntry]:
        return self.new + self.changed


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def select_event_folders(root: Path, events: Optional[Iterable[str]] = None) -> list[Path]:
    """Explicit --event names (must exist), else the EVENT_LAYOUTS whitelist
    folders that exist under root (same rule as ai.airtable_ingest.ingest)."""
    if events:
        out = []
        for name in events:
            p = root / name
            if not p.is_dir():
                raise FileNotFoundError(f"event folder not found under root: {name!r}")
            out.append(p)
        return out
    return [root / name for name in EVENT_LAYOUTS if (root / name).is_dir()]


def iter_files(root: Path, event_dirs: list[Path]) -> Iterator[FileEntry]:
    for event_dir in event_dirs:
        for company_dir, company_rel, event_label in iter_event_companies(event_dir):
            for path in sorted(company_dir.rglob("*")):
                if not path.is_file() or is_ignored(path):
                    continue
                if any(part.startswith(".") for part in path.relative_to(company_dir).parts):
                    continue
                st = path.stat()
                yield FileEntry(
                    path=path,
                    relpath=path.relative_to(root).as_posix(),
                    event_folder=event_dir.name,
                    event_label=event_label,
                    company_name=company_dir.name,
                    company_relpath=company_rel,
                    size_bytes=st.st_size,
                    mtime=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                )


def is_skipped(e: FileEntry, max_bytes: int = MAX_FILE_BYTES_DEFAULT) -> Optional[str]:
    """Document status for a file that is recorded but not ingested, else None."""
    kind = file_kind(e.path)
    if kind == "media" or e.size_bytes > max_bytes:
        return "skipped_large"
    if kind == "other":
        return "unsupported"
    return None


def classify(
    entries: Iterable[FileEntry],
    known: dict,
    *,
    force: bool = False,
    max_bytes: int = MAX_FILE_BYTES_DEFAULT,
) -> ScanResult:
    """Compare files on disk with ``known`` (relpath -> kg.documents.DocumentRow).

    A file whose size and mtime match its stored row is unchanged without
    being re-hashed (cheap on a synced OneDrive folder). Otherwise it is hashed;
    a matching hash is still unchanged (e.g. a re-sync touched mtime). Skipped
    files (media, unsupported, oversized) are never hashed. Rows for files no
    longer on disk are reported as deleted.
    """
    result = ScanResult()
    seen: set[str] = set()
    for e in entries:
        seen.add(e.relpath)
        row = known.get(e.relpath)
        skipped = is_skipped(e, max_bytes) is not None
        same_stat = (
            row is not None
            and row.size_bytes == e.size_bytes
            and row.mtime is not None
            and abs((row.mtime - e.mtime).total_seconds()) < 1
        )
        if skipped:
            (result.unchanged if same_stat and row.status != "deleted" else
             (result.new if row is None else result.changed)).append(e)
            continue
        retry = row is not None and row.status in ("error", "deleted", "skipped_large", "unsupported")
        stale_extractor = row is not None and row.extractor_version != EXTRACTOR_VERSION
        if row is None:
            e.sha256 = sha256_of(e.path)
            result.new.append(e)
            continue
        if force or retry or stale_extractor:
            e.sha256 = sha256_of(e.path)
            result.changed.append(e)
            continue
        if same_stat and row.sha256:
            e.sha256 = row.sha256
            result.unchanged.append(e)
            continue
        e.sha256 = sha256_of(e.path)
        (result.unchanged if e.sha256 == row.sha256 else result.changed).append(e)
    result.deleted_relpaths = sorted(
        rp for rp, row in known.items() if rp not in seen and row.status != "deleted"
    )
    return result
