"""Tests for ai/kg_query/tool.py.

Follows the same convention as tests/kg/: a local Postgres+pgvector (the Docker
container from kg/README.md section A) plus a monkeypatched embedder, so no
real Voyage/Anthropic network calls happen for the kg-facing parts. The Claude
API itself is faked with a small stub client (see FakeAnthropic below) so these
tests run with NO ANTHROPIC_API_KEY and no real model calls either — this
module's job is to prove the tool-calling LOOP and the citation-safety-net are
correct, not to test Claude's own behavior.

Run: python -m pytest tests/kg_query/ -q
(same TEST_DATABASE_URL / Docker container as tests/kg/)
"""
from __future__ import annotations

import os

import pytest

from kg import connect, upsert_source, write_claims
from kg.companies import resolve_company
from kg.config import apply_schema
from kg.models import ClaimInput

from ai.kg_query import tool as kg_query_tool


# ---------------------------------------------------------------------------
# Fixtures — mirrors tests/kg/ conventions (local DB, mocked embedder).
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(monkeypatch):
    """A real connection to the local test Postgres, with the embedder mocked
    so no network call happens. Schema is (re)applied fresh — same pattern
    kg/README.md describes for local test runs."""
    os.environ.setdefault(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/hunniwell_test",
    )

    # Fixed-length fake vectors (1024-dim, matching schema.sql's vector(1024))
    # so write_claims / semantic_search never hit Voyage or Ollama.
    def fake_embed(texts, input_type="document"):
        return [[0.001 * i] * 1024 for i, _ in enumerate(texts, start=1)]

    monkeypatch.setattr("kg.embeddings.embed", fake_embed)

    c = connect()
    apply_schema(c)
    yield c
    # run_ask monkeypatches c.close to a no-op and that patch is still active
    # here, so close via the class: a leaked "idle in transaction" connection
    # holds locks that block later schema changes (and hangs the suite).
    c.rollback()
    type(c).close(c)


@pytest.fixture()
def seeded_company(conn):
    """One company with two claims on the same field — deliberately conflicting
    values from two different-reliability sources, to exercise the
    'claims can disagree' behavior the whole kg/ design is built around."""
    company_id = resolve_company(
        conn, relpath="TestEvent/Acme Health", name="Acme Health", event="TestEvent"
    )
    internal_source = upsert_source(
        conn, company_id, kind="internal_notes", uri=None, writer="test"
    )
    submitted_source = upsert_source(
        conn, company_id, kind="company_submitted", uri="https://acme.example/deck", writer="test"
    )
    claim_ids = write_claims(
        conn,
        company_id,
        internal_source,
        [ClaimInput(field="class_of_device", value="Class III")],
        writer="test",
    )
    write_claims(
        conn,
        company_id,
        submitted_source,
        [ClaimInput(field="class_of_device", value="Class II")],
        writer="test",
    )
    return {"company_id": company_id, "claim_id": claim_ids[0]}


# ---------------------------------------------------------------------------
# _retrieve — thin wrapper around kg.semantic_search
# ---------------------------------------------------------------------------


def test_retrieve_returns_plain_dicts(conn, seeded_company):
    results = kg_query_tool._retrieve(conn, "device classification", 5)
    assert results and all(isinstance(r, dict) for r in results)
    for r in results:
        assert set(r.keys()) == {
            "claim_id", "company_id", "field", "value", "reliability", "source_kind",
        }


# ---------------------------------------------------------------------------
# _finalize — the citation safety net. An invented/incorrect/malformed
# claim_id must NEVER be silently treated as valid or silently ignored.
# ---------------------------------------------------------------------------


def test_finalize_resolves_real_citation(conn, seeded_company):
    claim_id = seeded_company["claim_id"]
    text = f"The device is Class III. [[{claim_id}]]"
    result = kg_query_tool._finalize(conn, text)

    assert result.unverified_citations == []
    assert claim_id in result.citations
    assert result.citations[claim_id]["reliability"] == 5  # internal_notes default
    assert result.answer == text  # raw text is preserved as-is


def test_finalize_flags_hallucinated_citation(conn, seeded_company):
    fake_id = "clm_" + "0" * 32  # well-formed token, but no such row exists
    result = kg_query_tool._finalize(conn, f"The device is Class III. [[{fake_id}]]")

    assert fake_id in result.unverified_citations
    assert fake_id not in result.citations


def test_finalize_flags_real_claim_that_was_not_retrieved(conn, seeded_company):
    claim_id = seeded_company["claim_id"]  # exists and is active...
    result = kg_query_tool._finalize(conn, f"Fact [[{claim_id}]]", allowed_ids=set())
    # ...but the model was never shown it, so it cannot be a grounded citation.
    assert claim_id in result.unverified_citations
    assert result.citations == {}


@pytest.mark.parametrize(
    "bad",
    ["clm_" + "A" * 32, "clm_abc", "clm_" + "a" * 31, "clm_"],
)
def test_finalize_flags_malformed_citation_tokens(conn, bad):
    result = kg_query_tool._finalize(conn, f"Fact [[{bad}]]")
    assert bad in result.unverified_citations
    assert result.citations == {}


def test_finalize_deduplicates_repeated_citations(conn, seeded_company):
    claim_id = seeded_company["claim_id"]
    text = f"First mention [[{claim_id}]]. Second mention [[{claim_id}]]."
    result = kg_query_tool._finalize(conn, text)

    assert len(result.citations) == 1
    assert result.unverified_citations == []


def test_finalize_with_no_citations(conn):
    result = kg_query_tool._finalize(conn, "No claims were found for this question.")
    assert result.citations == {}
    assert result.unverified_citations == []


# ---------------------------------------------------------------------------
# ask() — retrieval + ONE Claude call + citation check, with a fake Claude
# client so no real Anthropic API call is made. It does not test Claude's
# actual judgment (that's not testable here).
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_FakeTextBlock(text)] if text is not None else []
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._reply


class _FakeAnthropic:
    def __init__(self, reply):
        self.messages = _FakeMessages(reply)


@pytest.fixture()
def run_ask(conn, monkeypatch):
    """ask() with the Claude reply scripted. `reply` may be a callable taking
    the seeded claim id. Returns (result, fake_client)."""
    # ask() closes the connection in its `finally` block; prevent that here
    # since the `conn` fixture owns the connection's lifecycle for this test.
    monkeypatch.setattr(kg_query_tool, "connect", lambda: conn)
    monkeypatch.setattr(conn, "close", lambda: None)

    def _run(reply, question="what class of device is Acme Health?", **kw):
        fake = _FakeAnthropic(reply)
        monkeypatch.setattr(kg_query_tool.anthropic, "Anthropic", lambda **k: fake)
        result = kg_query_tool.ask(question, anthropic_api_key="test-key", **kw)
        return result, fake

    return _run


def test_ask_with_valid_citation(seeded_company, run_ask):
    claim_id = seeded_company["claim_id"]
    result, fake = run_ask(_FakeResponse(f"It's Class III. [[{claim_id}]]"))

    assert "Class III" in result.answer
    assert claim_id in result.citations
    assert result.unverified_citations == []
    assert result.warning is None
    # exactly one model call, with the retrieved claims embedded in the prompt
    assert len(fake.messages.calls) == 1
    assert claim_id in fake.messages.calls[0]["messages"][0]["content"]
    assert "tools" not in fake.messages.calls[0]


def test_ask_flags_citation_not_in_retrieved_claims(seeded_company, run_ask):
    fake_id = "clm_" + "0" * 32
    result, _ = run_ask(_FakeResponse(f"Made up. [[{fake_id}]]"))
    assert fake_id in result.unverified_citations


def test_ask_truncated_answer_is_warned(seeded_company, run_ask):
    result, _ = run_ask(_FakeResponse("Class III [[clm_2ae30564df7643f3", "max_tokens"))
    assert result.warning and "cut off" in result.warning
    assert result.unverified_citations  # the dangling citation is surfaced


def test_ask_refusal_is_warned(seeded_company, run_ask):
    result, _ = run_ask(_FakeResponse(None, "refusal"))
    assert result.answer == ""
    assert result.warning


def test_ask_empty_answer_is_warned(seeded_company, run_ask):
    result, _ = run_ask(_FakeResponse("", "end_turn"))
    assert result.warning


def test_ask_no_claims_skips_model_call(conn, run_ask, monkeypatch):
    monkeypatch.setattr(kg_query_tool, "semantic_search", lambda *a, **k: [])
    result, fake = run_ask(_FakeResponse("should not be used"))
    assert result.answer == kg_query_tool.NO_CLAIMS_ANSWER
    assert fake.messages.calls == []


@pytest.mark.parametrize("q", ["", "   ", None])
def test_ask_rejects_empty_question(run_ask, q):
    with pytest.raises(ValueError):
        run_ask(_FakeResponse("x"), question=q)


@pytest.mark.parametrize("k", [0, -5, 10_000])
def test_ask_clamps_k(seeded_company, run_ask, k):
    # must not raise (negative LIMIT, huge LIMIT) — k is clamped to [1, MAX_K]
    result, _ = run_ask(_FakeResponse("ok"), k=k)
    assert result.answer == "ok"
