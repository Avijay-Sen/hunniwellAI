"""Public API for the kg/ company knowledge base.

Assembled ONCE here (Task 14). Backing modules:
  companies, sources, claims, search, citations, export, config, models.
"""
from __future__ import annotations

from kg.citations import resolve_citation, validate_citations
from kg.claims import enrich, query, write_claims
from kg.companies import add_alias, resolve_company, resolve_company_with_aliases
from kg.config import apply_migrations, apply_schema, connect
from kg.documents import (
    finish_run,
    load_documents,
    retire_documents,
    start_run,
    sync_document_claims,
    touch_documents,
    upsert_document,
)
from kg.periods import parse_event_date, period_for_event, weekly_period
from kg.export import to_airtable_record
from kg.models import Claim, ClaimInput
from kg.search import semantic_search
from kg.sources import upsert_source

__all__ = [
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
