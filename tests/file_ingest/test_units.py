"""Unit tests for ai/file_ingest that need no database."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai.file_ingest import extract
from ai.file_ingest.extract import EXTRACTOR_VERSION, chunk_text, source_kind_for
from ai.file_ingest.run import build_claims, db_host, is_local
from ai.file_ingest.scan import FileEntry, classify, is_skipped, iter_files, select_event_folders
from kg.documents import DocumentRow
from kg.periods import parse_event_date, weekly_label


# ---- periods ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "label, expected",
    [
        ("JPM 2026 (260115)", date(2026, 1, 15)),
        ("LSI 2026 - USA (260320)", date(2026, 3, 20)),
        ("MTI 2026 - Open Rounds", None),
        ("Bad date (261399)", None),
        ("", None),
    ],
)
def test_parse_event_date(label, expected):
    assert parse_event_date(label) == expected


def test_weekly_label():
    assert weekly_label(date(2026, 11, 5)) == "2026-W45"
    assert weekly_label(date(2027, 1, 1)) == "2026-W53"  # ISO week-year


# ---- chunking / kinds ------------------------------------------------------------

def test_chunk_text_short_and_empty():
    assert chunk_text("") == []
    assert chunk_text("  hello  ") == ["hello"]


def test_chunk_text_covers_everything_with_overlap():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 80 for i in range(40))
    chunks = chunk_text(text, size=1000, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    for i in range(40):  # nothing lost
        assert any(f"Paragraph {i}." in c for c in chunks)


@pytest.mark.parametrize(
    "name, kind",
    [
        ("Pitch Deck 2026.pptx", "pitch_deck"),
        ("deck.pdf", "pitch_deck"),
        ("Meeting notes - Mar 3.docx", "internal_notes"),
        ("call_notes.md", "internal_notes"),
        ("Recall Medical overview.pdf", "pitch_deck"),  # 'call' only as a whole word
        ("financials.xlsx", "company_submitted"),
        ("web_research.txt", "open_internet"),
        ("summary.txt", "internal_notes"),
    ],
)
def test_source_kind_for(name, kind):
    assert source_kind_for(Path(name)) == kind


def test_extract_csv(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text("Metric,2025,2026\nRevenue ($M),1.2,3.4\n,,\n")
    assert extract.extract_text(p) == "Metric | 2025 | 2026\nRevenue ($M) | 1.2 | 3.4"


def test_extract_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cap table"
    ws.append(["Investor", "Amount ($M)"])
    ws.append(["Acme Ventures", 2.5])
    p = tmp_path / "f.xlsx"
    wb.save(p)
    text = extract.extract_text(p)
    assert "--- Sheet: Cap table ---" in text
    assert "Acme Ventures | 2.5" in text


def test_extract_unsupported_raises(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00")
    with pytest.raises(ValueError):
        extract.extract_text(p)


# ---- scanning / classification -----------------------------------------------------

def _tree(tmp_path) -> Path:
    root = tmp_path / "CompanyFiles"
    comp = root / "Demo Event (260115)" / "Acme Health"
    comp.mkdir(parents=True)
    (comp / "deck.txt").write_text("Acme makes a glucose patch.")
    (comp / ".DS_Store").write_text("x")
    (comp / "~$lock.docx").write_text("x")
    (comp / "sub").mkdir()
    (comp / "sub" / "notes.md").write_text("Met the CEO.")
    return root


def test_iter_files_layout_and_ignores(tmp_path):
    root = _tree(tmp_path)
    entries = list(iter_files(root, select_event_folders(root, ["Demo Event (260115)"])))
    rels = sorted(e.relpath for e in entries)
    assert rels == [
        "Demo Event (260115)/Acme Health/deck.txt",
        "Demo Event (260115)/Acme Health/sub/notes.md",
    ]
    e = entries[0]
    assert e.company_name == "Acme Health"
    assert e.company_relpath == "Demo Event (260115)/Acme Health"
    assert e.event_folder == e.event_label == "Demo Event (260115)"


def test_select_event_folders_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        select_event_folders(tmp_path, ["nope"])


def _row(e: FileEntry, sha: str, **kw) -> DocumentRow:
    base = dict(id="d1", relpath=e.relpath, sha256=sha, size_bytes=e.size_bytes,
                mtime=e.mtime, status="active", extractor_version=EXTRACTOR_VERSION)
    base.update(kw)
    return DocumentRow(**base)


def test_classify_new_unchanged_changed_deleted(tmp_path):
    root = _tree(tmp_path)
    event = select_event_folders(root, ["Demo Event (260115)"])
    first = classify(iter_files(root, event), {})
    assert len(first.new) == 2 and not first.changed and not first.unchanged

    known = {e.relpath: _row(e, e.sha256) for e in first.new}
    known["Demo Event (260115)/Gone Co/old.pdf"] = DocumentRow(
        "d2", "Demo Event (260115)/Gone Co/old.pdf", "abc", 1, None, "active", EXTRACTOR_VERSION)

    second = classify(iter_files(root, event), known)
    assert len(second.unchanged) == 2 and not second.new and not second.changed
    assert second.deleted_relpaths == ["Demo Event (260115)/Gone Co/old.pdf"]

    (root / "Demo Event (260115)" / "Acme Health" / "deck.txt").write_text("Acme raised $5M.")
    third = classify(iter_files(root, event), known)
    assert [e.path.name for e in third.changed] == ["deck.txt"]


def test_classify_touched_mtime_same_content_is_unchanged(tmp_path):
    root = _tree(tmp_path)
    event = select_event_folders(root, ["Demo Event (260115)"])
    first = classify(iter_files(root, event), {})
    known = {e.relpath: _row(e, e.sha256, mtime=e.mtime - timedelta(days=1)) for e in first.new}
    assert len(classify(iter_files(root, event), known).unchanged) == 2


def test_classify_retries_errors_and_stale_extractor(tmp_path):
    root = _tree(tmp_path)
    event = select_event_folders(root, ["Demo Event (260115)"])
    first = classify(iter_files(root, event), {})
    a, b = first.new
    known = {a.relpath: _row(a, a.sha256, status="error"),
             b.relpath: _row(b, b.sha256, extractor_version="0")}
    assert len(classify(iter_files(root, event), known).changed) == 2


def test_media_and_large_files_are_skipped_and_never_hashed(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    comp = root / "Demo Event (260115)" / "Acme Health"
    (comp / "zoom.mp4").write_bytes(b"0" * 10)
    (comp / "big.pdf").write_bytes(b"0" * 2000)
    (comp / "photo.heic").write_bytes(b"0")
    import ai.file_ingest.scan as scan

    hashed = []
    real = scan.sha256_of
    monkeypatch.setattr(scan, "sha256_of", lambda p: hashed.append(p.name) or real(p))
    res = classify(iter_files(root, select_event_folders(root, ["Demo Event (260115)"])), {}, max_bytes=1000)
    status = {e.path.name: is_skipped(e, 1000) for e in res.new}
    assert status["zoom.mp4"] == "skipped_large"
    assert status["big.pdf"] == "skipped_large"
    assert status["photo.heic"] == "unsupported"
    assert status["deck.txt"] is None
    assert not {"zoom.mp4", "big.pdf", "photo.heic"} & set(hashed)


# ---- run helpers -------------------------------------------------------------------

def test_db_host_and_is_local():
    assert db_host("postgresql://u:secret@localhost:5432/db") == "localhost:5432"
    assert is_local("postgresql://u:p@localhost:5432/db")
    assert is_local("postgresql://u:p@127.0.0.1/db")
    assert not is_local("postgresql://u:p@db.abc.supabase.co:5432/postgres")
    assert "secret" not in db_host("postgresql://u:secret@h:1/db")


def test_build_claims_labels_chunks():
    e = FileEntry(Path("/x/deck.pdf"), "E/C/deck.pdf", "E", "E", "C", "E/C", 1,
                  datetime.now(timezone.utc))
    claims = build_claims(e, "hello world", [])
    assert len(claims) == 1
    assert claims[0].field == "doc_chunk"
    assert claims[0].value.startswith("[deck.pdf — part 1/1]\n")
