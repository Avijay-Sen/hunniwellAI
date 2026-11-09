"""Text extraction for every supported company file type.

PDF / DOCX / PPTX / TXT / MD reuse the extractors in ai/airtable_ingest/ingest.py;
XLSX and CSV are added here. Video/audio (e.g. Zoom recordings) are skipped.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from ai.airtable_ingest.ingest import EXTRACTORS as _BASE_EXTRACTORS

# Bump when extraction/chunking changes, so a run can re-ingest everything.
EXTRACTOR_VERSION = "1"

MAX_SHEET_ROWS = 2_000
MAX_TEXT_CHARS = 400_000

MEDIA_EXTS = {".mp4", ".mov", ".m4a", ".mp3", ".wav", ".avi", ".mkv", ".webm", ".m4v"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
# Bookkeeping files written by the existing scripts inside company folders.
IGNORED_SUFFIXES = {".json", ".log", ".tmp", ".lnk"}
# Whole words in a file name that mark it as internal notes (reliability 5).
_NOTES_WORDS = {"note", "notes", "meeting", "meetings", "minutes", "call", "calls", "memo", "diligence", "dd"}


def extract_xlsx(path: Path) -> str:
    from openpyxl import load_workbook  # optional dependency, imported lazily

    wb = load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    try:
        for ws in wb.worksheets:
            parts.append(f"--- Sheet: {ws.title} ---")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= MAX_SHEET_ROWS:
                    parts.append(f"[... truncated after {MAX_SHEET_ROWS} rows]")
                    break
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    parts.append(" | ".join(cells))
    finally:
        wb.close()
    return "\n".join(parts)


def extract_csv(path: Path) -> str:
    parts: list[str] = []
    with path.open(newline="", encoding="utf-8", errors="ignore") as f:
        for i, row in enumerate(csv.reader(f)):
            if i >= MAX_SHEET_ROWS:
                parts.append(f"[... truncated after {MAX_SHEET_ROWS} rows]")
                break
            cells = [c.strip() for c in row if c.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


EXTRACTORS = {**_BASE_EXTRACTORS, ".xlsx": extract_xlsx, ".xlsm": extract_xlsx, ".csv": extract_csv}

FILE_KINDS = {
    ".pdf": "pdf", ".pptx": "pptx", ".docx": "docx", ".txt": "text", ".md": "text",
    ".xlsx": "spreadsheet", ".xlsm": "spreadsheet", ".csv": "spreadsheet",
}


def is_ignored(path: Path) -> bool:
    name = path.name
    return (
        name in IGNORED_NAMES
        or name.startswith(".")
        or name.startswith("~$")  # Office lock files
        or path.suffix.lower() in IGNORED_SUFFIXES
    )


def file_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MEDIA_EXTS:
        return "media"
    return FILE_KINDS.get(ext, "other")


def extract_text(path: Path) -> str:
    """Extracted text ('' if nothing readable). Raises on unsupported types."""
    fn = EXTRACTORS.get(path.suffix.lower())
    if fn is None:
        raise ValueError(f"unsupported file type: {path.suffix}")
    return (fn(path) or "")[:MAX_TEXT_CHARS]


def source_kind_for(path: Path) -> str:
    """kg source kind (drives reliability, kg/models.py DEFAULT_RELIABILITY)."""
    name = path.name.lower()
    if name.startswith("web_research"):
        return "open_internet"  # generated web-research memo (ai/airtable_ingest/web_ingest.py)
    if _NOTES_WORDS & set(re.split(r"[^a-z]+", path.stem.lower())):
        return "internal_notes"
    kind = file_kind(path)
    if kind in ("pptx", "pdf"):
        return "pitch_deck"
    if kind == "spreadsheet":
        return "company_submitted"
    return "internal_notes" if kind in ("docx", "text") else "company_submitted"


def chunk_text(text: str, size: int = 3_000, overlap: int = 300) -> list[str]:
    """Split text into ~size-char chunks on paragraph/line boundaries where
    possible, with ``overlap`` chars of context carried between chunks."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
            if cut > size // 2:
                end = start + cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks
