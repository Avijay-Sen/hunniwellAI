"""Minimal command-line entry point for ai.kg_query, for testing before any UI
exists. Run:

    python -m ai.kg_query.cli "which companies are working on glucose monitoring?"

Requires the same env vars kg/ already needs (SUPABASE_DB_URL or
TEST_DATABASE_URL, plus VOYAGE_API_KEY or EMBEDDINGS_PROVIDER=local for the
embedder kg.semantic_search uses under the hood) and ANTHROPIC_API_KEY for the
Claude call this module makes itself.
"""
from __future__ import annotations

import sys

from ai.kg_query.tool import ask


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python -m ai.kg_query.cli "<question>"')
        sys.exit(1)

    question = " ".join(sys.argv[1:])
    try:
        result = ask(question)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print("\n" + result.answer + "\n")

    if result.citations:
        print("Sources:")
        for token, info in result.citations.items():
            print(
                f"  {token} -> [{info['source_kind']}, reliability={info['reliability']}] "
                f"{info['source_uri'] or '(no uri)'}"
            )

    if result.warning:
        print(f"⚠ {result.warning}")

    if result.unverified_citations:
        print("\n⚠ WARNING: the model cited claim id(s) that did not resolve to a real "
              "active claim. Treat this answer with extra caution:")
        for token in result.unverified_citations:
            print(f"  {token}")


if __name__ == "__main__":
    main()
