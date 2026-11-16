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
    "claims", "sources", "documents", "company_aliases", "ingest_unmatched", "company_appearances",
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


LATER = "Later Event (260601)"


def _add_company_folder(root, event, name, text):
    d = root / event / name
    d.mkdir(parents=True)
    (d / "notes.txt").write_text(text)


def test_near_miss_name_is_held_for_review_not_created(conn, root):
    _run(conn, root)
    _add_company_folder(root, LATER, "Acme Health AI", "Acme Health AI raised a 7M Series A.")
    stats = run_mod.run(conn, root, events=[LATER], use_llm=False, log=lambda *_: None)

    assert stats["companies_held_for_review"] == 1
    assert stats["files_held_for_review"] == 1
    assert stats["status"] == "succeeded"
    assert _q(conn, "select count(*) from companies")[0][0] == 2  # no near-duplicate company
    assert _q(conn, "select count(*) from documents where relpath like %s", LATER + "%")[0][0] == 0
    row = _q(conn, "select u.name_raw, c.name_raw, u.status from ingest_unmatched u "
                   "join companies c on c.id = u.candidate_company_id")[0]
    assert row == ("Acme Health AI", "Acme Health", "pending")

    # still held on the next run, and still listed once
    stats = run_mod.run(conn, root, events=[LATER], use_llm=False, log=lambda *_: None)
    assert stats["files_held_for_review"] == 1
    assert _q(conn, "select count(*) from ingest_unmatched")[0][0] == 1


def test_review_alias_then_ingest_merges_into_existing_company(conn, root):
    from kg.companies import approve_alias

    _run(conn, root)
    _add_company_folder(root, LATER, "Acme Health AI", "Acme Health AI raised a 7M Series A.")
    run_mod.run(conn, root, events=[LATER], use_llm=False, log=lambda *_: None)
    acme = str(_q(conn, "select id from companies where name_raw = 'Acme Health'")[0][0])

    assert approve_alias(conn, "Acme Health AI") == acme
    stats = run_mod.run(conn, root, events=[LATER], use_llm=False, log=lambda *_: None)
    assert stats["files_ingested"] == 1
    assert _q(conn, "select count(*) from companies")[0][0] == 2
    assert _q(conn, "select company_id::text from documents where relpath like %s", LATER + "%")[0][0] == acme
    assert _q(conn, "select status from ingest_unmatched")[0][0] == "aliased"


def test_review_new_then_ingest_creates_company(conn, root):
    from kg.companies import approve_new

    _run(conn, root)
    _add_company_folder(root, LATER, "Acme Health AI", "A different company.")
    run_mod.run(conn, root, events=[LATER], use_llm=False, log=lambda *_: None)

    approve_new(conn, "Acme Health AI")
    stats = run_mod.run(conn, root, events=[LATER], use_llm=False, log=lambda *_: None)
    assert stats["files_ingested"] == 1
    assert stats["companies_created"] == 1
    assert _q(conn, "select count(*) from companies")[0][0] == 3


def test_unrelated_new_name_is_created_without_review(conn, root):
    _run(conn, root)
    _add_company_folder(root, LATER, "Zeta Neuro", "Zeta Neuro builds an EEG headset.")
    stats = run_mod.run(conn, root, events=[LATER], use_llm=False, log=lambda *_: None)
    assert stats["files_ingested"] == 1
    assert stats.get("companies_held_for_review", 0) == 0
    assert _q(conn, "select count(*) from ingest_unmatched")[0][0] == 0


def test_review_cli_lists_and_decides(conn, root, capsys):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "review_unmatched", Path(__file__).resolve().parents[2] / "scripts" / "review_unmatched.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    _run(conn, root)
    _add_company_folder(root, LATER, "Acme Health AI", "x")
    run_mod.run(conn, root, events=[LATER], use_llm=False, log=lambda *_: None)

    assert cli.main(["--db-url", TEST_DATABASE_URL]) == 0
    out = capsys.readouterr().out
    assert "Acme Health AI" in out and "HW-0001 Acme Health" in out
    assert cli.main(["--db-url", TEST_DATABASE_URL, "--alias", "Acme Health AI", "--to", "HW-0002"]) == 0
    beta = _q(conn, "select id::text from companies where company_code = 2")[0][0]
    assert _q(conn, "select company_id::text from company_aliases")[0][0] == beta
    assert cli.main(["--db-url", TEST_DATABASE_URL]) == 0
    assert "Nothing to review" in capsys.readouterr().out
    assert cli.main(["--db-url", "postgresql://u:p@db.example.supabase.co:5432/postgres",
                     "--new", "X"]) == 2


def test_dry_run_previews_review_list(conn, root, capsys):
    _run(conn, root)
    _add_company_folder(root, LATER, "Acme Health AI", "x")
    _add_company_folder(root, LATER, "Zeta Neuro", "y")
    run_mod.run(conn, root, events=[LATER], dry_run=True, log=print)
    out = capsys.readouterr().out
    assert "Zeta Neuro" in out and "'Acme Health AI' ~ 'Acme Health'" in out
    assert _q(conn, "select count(*) from ingest_unmatched")[0][0] == 0  # read-only


def test_same_value_in_two_periods_on_one_source_is_two_claims(conn):
    """Migration 002: claims_idem includes period_id (legacy sources are reused
    across events)."""
    from kg.claims import _insert_claims
    from kg.models import ClaimInput
    from kg.periods import period_for_event

    cid = str(_q(conn, "insert into companies (name_norm, name_raw) values ('x', 'X') returning id")[0][0])
    src = upsert_source(conn, cid, "company_submitted", None, writer="t")
    p1, _ = period_for_event(conn, "A (260101)")
    p2, _ = period_for_event(conn, "B (260201)")
    claim = [ClaimInput(field="size_of_round_m", value="5")]
    a = _insert_claims(conn, cid, src, claim, "t", period_id=p1)
    a2 = _insert_claims(conn, cid, src, claim, "t", period_id=p1)
    b = _insert_claims(conn, cid, src, claim, "t", period_id=p2)
    conn.commit()
    assert a == a2 and a != b
    assert _q(conn, "select count(*) from claims")[0][0] == 2


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


def test_migration_003_renormalizes_and_merges_spelling_duplicates(conn):
    """Live rows written by the old normalizer kept spaces; 003 fixes them and
    merges companies that collide under the current rule."""
    from pathlib import Path

    from kg.models import ClaimInput
    from kg.claims import write_claims

    def company(norm, raw):
        return str(_q(conn, "insert into companies (name_norm, name_raw) values (%s, %s) returning id",
                      norm, raw)[0][0])

    keep = company("3dbiofibr", "3DBioFibR")
    dup = company("3d biofibr", "3D BioFibR Inc")
    solo = company("52 north health", "52 North Health")
    for cid, rel in ((keep, "E/3DBioFibR"), (dup, "E/3D BioFibR Inc")):
        _q(conn, "insert into company_appearances (company_id, event, relpath) values (%s, 'E', %s) "
                 "returning id", cid, rel)
    conn.commit()
    s_keep = upsert_source(conn, keep, "company_submitted", None, writer="t")
    s_dup = upsert_source(conn, dup, "company_submitted", None, writer="t")
    write_claims(conn, keep, s_keep, [ClaimInput(field="country", value="USA")], writer="t")
    write_claims(conn, dup, s_dup, [ClaimInput(field="country", value="USA")], writer="t")

    sql = (Path(__file__).resolve().parents[2] / "kg" / "migrations"
           / "003_renormalize_company_names.sql").read_text()
    for _ in range(2):  # idempotent
        conn.execute(sql)
        conn.commit()

    assert sorted(_q(conn, "select name_norm from companies")) == [("3dbiofibr",), ("52northhealth",)]
    assert _q(conn, "select id::text from companies where name_norm = '52northhealth'")[0][0] == solo
    assert _q(conn, "select count(*), count(distinct company_id) from claims")[0] == (2, 1)
    assert _q(conn, "select distinct company_id::text from sources")[0][0] == keep
    assert _q(conn, "select count(*) from company_appearances where company_id = %s", keep)[0][0] == 2
