"""Natural-language query interface over kg/ (Task: v1, semantic-search only).

Public entry point: ask(question) -> AskResult. Everything here is READ-ONLY —
this package calls kg.semantic_search / kg.resolve_citation / kg.validate_citations
exactly as they exist today and never writes claims, sources, or companies.
"""
from __future__ import annotations

from ai.kg_query.tool import AskResult, ask

__all__ = ["ask", "AskResult"]
