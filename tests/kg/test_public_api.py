import inspect

import kg

EXPECTED = [
    "resolve_company",
    "upsert_source",
    "write_claims",
    "enrich",
    "query",
    "semantic_search",
    "resolve_citation",
    "validate_citations",
    "to_airtable_record",
    "connect",
    "apply_schema",
    "apply_migrations",
    "add_alias",
    "resolve_company_with_aliases",
    "start_run",
    "finish_run",
    "load_documents",
    "upsert_document",
    "touch_documents",
    "retire_documents",
    "sync_document_claims",
    "parse_event_date",
    "period_for_event",
    "weekly_period",
    "ClaimInput",
    "Claim",
]

def test_public_api_names_importable_from_kg():
    # Every name must import directly from the kg package namespace.
    for name in EXPECTED:
        assert hasattr(kg, name), f"kg.{name} missing from public API"

def test_public_api_each_name_callable_or_class():
    for name in EXPECTED:
        obj = getattr(kg, name)
        # functions are callable; ClaimInput/Claim are dataclasses (classes)
        assert callable(obj) or inspect.isclass(obj), f"kg.{name} not callable/class"

def test_dunder_all_matches_expected_exactly():
    assert hasattr(kg, "__all__")
    assert sorted(kg.__all__) == sorted(EXPECTED)

def test_top_level_imports_resolve():
    # Direct `from kg import X` form must work for every name.
    from kg import (  # noqa: F401
        resolve_company,
        upsert_source,
        write_claims,
        enrich,
        query,
        semantic_search,
        resolve_citation,
        validate_citations,
        to_airtable_record,
        connect,
        ClaimInput,
        Claim,
    )
