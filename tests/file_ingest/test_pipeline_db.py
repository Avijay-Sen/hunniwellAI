"""End-to-end tests for ai/file_ingest against the LOCAL test Postgres
(same Docker container as tests/kg; never Supabase). The embedder and Claude
are faked, so no network calls happen.

Run: python -m pytest tests/file_ingest -q
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from types import SimpleNamespace

import psycopg
import pytest
from pgvector.psycopg import register_vector

from ai.file_ingest import run as run_mod
from kg.config import apply_schema
from kg.search import semantic_search
from kg.sources import upsert_source

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/hunniwell_test",
)
_TABLES = (
    "claims", "sources", "documents", "company_aliases", "company_appearances",
    "companies", "ingest_runs", "periods",
)
EVENT = "Demo Event (260115)"


def _fake_embed(texts, input_type="document"):
    """Deterministic bag-of-words vectors so semantic search is meaningful."""
    out = []
    for t in texts:
        v = [0.0] * 1024
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 1024] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v] if any(v) else [1.0] + [0.0] * 1023)
    return out


@pytest.fixture()
def conn(monkeypatch):
    assert run_mod.is_local(TEST_DATABASE_URL), "file_ingest tests only run against a local DB"
    embed_calls = []

    def embed(texts, input_type="document"):
        embed_calls.append(list(texts))
        return _fake_embed(texts, input_type)

    monkeypatch.setattr("kg.embeddings.embed", embed)
    c = psycopg.connect(TEST_DATABASE_URL)
    register_vector(c)
    apply_schema(c)
    with c.cursor() as cur:
        cur.execute("truncate " + ", ".join(_TABLES) + " restart identity cascade")
    c.commit()
    c.embed_calls = embed_calls
    yield c
    c.rollback()
    c.close()


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "CompanyFiles"
    acme = r / EVENT / "Acme Health"
    acme.mkdir(parents=True)
    (acme / "Pitch deck.txt").write_text(
        "Acme Health builds a continuous glucose monitoring patch for type 2 diabetes. "
        "Raising a 5M seed round."
    )
    (acme / "Meeting notes.md").write_text("Call with the CEO: FDA 510(k) submission planned for Q3 2026.")
    (acme / "financials.csv").write_text("Metric,2025\nRevenue ($M),0.4\n")
    beta = r / EVENT / "Beta Cardio"
    beta.mkdir(parents=True)
    (beta / "overview.txt").write_text("Beta Cardio makes an implantable cardiac rhythm monitor.")
    return r


def _q(conn, sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _run(conn, root, **kw):
    kw.setdefault("use_llm", False)
    return run_mod.run(conn, root, events=[EVENT], log=lambda *_: None, **kw)


def test_first_run_ingests_and_tags_everything(conn, root):
    stats = _run(conn, root)
    assert stats["status"] == "succeeded"
    assert stats["files_ingested"] == 4
    assert stats["claims_new"] == 4  # one chunk per small file

    # one source per file, all tagged with document + event period
    assert _q(conn, "select count(*), count(distinct document_id) from sources")[0] == (4, 4)
    (label, kind, starts) = _q(conn, "select label, kind, starts_on::text from periods")[0]
    assert (label, kind, starts) == (EVENT, "event", "2026-01-15")
    untagged = _q(conn, "select count(*) from claims where period_id is null or run_id is null "
                        "or as_of is null")[0][0]
    assert untagged == 0
    assert _q(conn, "select count(*) from companies")[0][0] == 2
    assert _q(conn, "select count(*) from companies where company_code is null")[0][0] == 0
    run_row = _q(conn, "select status, stats->>'files_ingested' from ingest_runs")[0]
    assert run_row == ("succeeded", "4")

    kinds = dict(_q(conn, "select title, kind from sources"))
    assert kinds["Meeting notes.md"] == "internal_notes"
    assert kinds["financials.csv"] == "company_submitted"


def test_second_run_is_idempotent_and_skips_unchanged(conn, root):
    _run(conn, root)
    n_claims = _q(conn, "select count(*) from claims")[0][0]
    calls_before = len(conn.embed_calls)

    stats = _run(conn, root)
    assert stats.get("files_ingested", 0) == 0
    assert stats["files_unchanged"] == 4
    assert _q(conn, "select count(*) from claims")[0][0] == n_claims
    assert _q(conn, "select count(*) from sources")[0][0] == 4
    assert len(conn.embed_calls) == calls_before  # nothing re-embedded
    assert _q(conn, "select count(*) from ingest_runs")[0][0] == 2


def test_force_rerun_creates_no_duplicates(conn, root):
    _run(conn, root)
    n_claims = _q(conn, "select count(*) from claims")[0][0]
    stats = _run(conn, root, force=True)
    assert stats["files_ingested"] == 4
    assert stats["claims_new"] == 0 and stats["claims_kept"] == 4
    assert _q(conn, "select count(*) from claims")[0][0] == n_claims
    assert _q(conn, "select count(*) from sources")[0][0] == 4


def test_changed_file_supersedes_old_claims_and_keeps_history(conn, root):
    _run(conn, root)
    deck = root / EVENT / "Acme Health" / "Pitch deck.txt"
    deck.write_text("Acme Health builds a glucose patch. Raising a 12M Series A round.")

    stats = _run(conn, root)
    assert stats["files_ingested"] == 1
    assert stats["claims_superseded"] == 1 and stats["claims_new"] == 1

    rows = _q(conn, "select c.status, c.value like '%%12M%%', c.period_id is not null from claims c "
                    "join sources s on s.id = c.source_id where s.title = 'Pitch deck.txt' "
                    "order by c.created_at")
    assert rows == [("superseded", False, True), ("active", True, True)]
    assert _q(conn, "select count(*) from sources where title = 'Pitch deck.txt'")[0][0] == 1


def test_deleted_file_is_retired(conn, root):
    _run(conn, root)
    (root / EVENT / "Beta Cardio" / "overview.txt").unlink()
    stats = _run(conn, root)
    assert stats["files_deleted"] == 1
    assert _q(conn, "select status from documents where relpath like %s",
              f"{EVENT}/Beta Cardio/%")[0][0] == "deleted"
    assert _q(conn, "select count(*) from claims c join sources s on s.id = c.source_id "
                    "where s.title = 'overview.txt' and c.status = 'active'")[0][0] == 0


def test_bad_file_is_recorded_and_run_continues(conn, root, monkeypatch):
    real = run_mod.extract_text

    def flaky(path):
        if path.name == "financials.csv":
            raise RuntimeError("corrupt file")
        return real(path)

    monkeypatch.setattr(run_mod, "extract_text", flaky)
    stats = _run(conn, root)
    assert stats["status"] == "partial"
    assert stats["files_error"] == 1 and stats["files_ingested"] == 3
    status, err = _q(conn, "select status, error from documents where relpath like '%%financials.csv'")[0]
    assert status == "error" and "corrupt file" in err

    monkeypatch.setattr(run_mod, "extract_text", real)  # fixed -> retried next run
    stats = _run(conn, root)
    assert stats["files_ingested"] == 1 and stats["status"] == "succeeded"


def test_skipped_and_empty_files_are_recorded(conn, root):
    acme = root / EVENT / "Acme Health"
    (acme / "pitch recording.mp4").write_bytes(b"0" * 100)
    (acme / "blank.txt").write_text("   ")
    stats = _run(conn, root)
    assert stats["files_skipped_large"] == 1
    assert stats["files_empty"] == 1
    statuses = dict(_q(conn, "select relpath, status from documents"))
    assert statuses[f"{EVENT}/Acme Health/pitch recording.mp4"] == "skipped_large"
    assert statuses[f"{EVENT}/Acme Health/blank.txt"] == "empty"
    # second run: skipped/empty files are not re-processed
    assert _run(conn, root).get("files_ingested", 0) == 0


def test_llm_field_claims(conn, root):
    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.messages = self

        def create(self, **kw):
            self.calls += 1
            text = ('{"indication": "Type 2 diabetes", "size_of_round_m": "5", "bogus_key": "x"}'
                    if "glucose" in kw["messages"][0]["content"] else "{}")
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")

    client = FakeClient()
    stats = run_mod.run(conn, root, events=[EVENT], client=client, log=lambda *_: None)
    assert client.calls == 4 and stats["llm_calls"] == 4
    fields = dict(_q(conn, "select field, value from claims where field <> 'doc_chunk'"))
    assert fields == {"indication": "Type 2 diabetes", "size_of_round_m": "5"}


def test_semantic_search_finds_chunks_and_filters_by_period(conn, root):
    _run(conn, root)
    hits = semantic_search(conn, "glucose monitoring patch", k=1)
    assert "glucose" in hits[0].value and hits[0].field == "doc_chunk"
    period_id = _q(conn, "select id from periods")[0][0]
    assert semantic_search(conn, "glucose", k=10, period_id=str(period_id))
    assert semantic_search(conn, "glucose", k=10, period_id="00000000-0000-0000-0000-000000000000") == []


def test_aliases_merge_company_spellings(conn, root):
    from kg.companies import add_alias

    _run(conn, root)
    acme_id = _q(conn, "select id from companies where name_raw = 'Acme Health'")[0][0]
    add_alias(conn, "AcmeHealth Diagnostics", str(acme_id))
    other = root / "Later Event (260601)" / "AcmeHealth Diagnostics"
    other.mkdir(parents=True)
    (other / "update.txt").write_text("Acme Health update: CE mark received.")
    run_mod.run(conn, root, events=["Later Event (260601)"], use_llm=False, log=lambda *_: None)
    assert _q(conn, "select count(*) from companies")[0][0] == 2
    # history across periods for one company
    assert _q(conn, "select count(distinct c.period_id) from claims c where c.company_id = %s",
              acme_id)[0][0] == 2


def test_upsert_source_without_document_id_keeps_old_behaviour(conn):
    cid = _q(conn, "insert into companies (name_norm, name_raw) values ('x', 'X') returning id")[0][0]
    conn.commit()
    a = upsert_source(conn, str(cid), "internal_notes", None, writer="t")
    b = upsert_source(conn, str(cid), "internal_notes", None, writer="t")
    assert a != b


def test_refuses_live_db_without_flag(tmp_path, capsys):
    rc = run_mod.main(["--root", str(tmp_path), "--db-url",
                       "postgresql://u:p@db.example.supabase.co:5432/postgres"])
    assert rc == 2
    assert "--yes-live" in capsys.readouterr().out
