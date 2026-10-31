"""Embedding wrapper. Default backend Voyage (voyage-3.5, dim 1024); set
EMBEDDINGS_PROVIDER=ollama for a local 1024-dim backend (mxbai-embed-large).

SOLE author of this module. Callers import it as a module (`from kg import
embeddings`) and call `embeddings.embed(...)`, so tests patch `kg.embeddings.embed`
(or a backend's internals) to avoid the network. Empty input short-circuits before
any backend work. Both backends emit length-1024 vectors so the schema's
vector(1024) column is provider-agnostic.
"""
from __future__ import annotations

import voyageai

from kg.config import (
    get_embeddings_provider,
    get_local_embed_model,
    get_voyage_key,
)

MODEL = "voyage-3.5"
DIM = 1024
_voyage_client: voyageai.Client | None = None
_local_model = None  # lazy-loaded sentence_transformers.SentenceTransformer
_local_model_name: str | None = None  # which model is currently loaded


def _get_voyage_client() -> voyageai.Client:
    global _voyage_client
    if _voyage_client is None:
        _voyage_client = voyageai.Client(api_key=get_voyage_key())
    return _voyage_client


def _embed_voyage(texts: list[str], input_type: str) -> list[list[float]]:
    result = _get_voyage_client().embed(texts, model=MODEL, input_type=input_type)
    return [[float(x) for x in vec] for vec in result.embeddings]

def _prefix_for(model: str, text: str, input_type: str) -> str:
    """Return `text` with whatever prefix `model` expects, or `text`
    unchanged if the model needs none. Centralizing this here means adding a
    different local model later only touches this one function."""
    name = model.lower()
    if "e5" in name:
        # e5-family: DIFFERENT prefix for queries vs. stored documents.
        return ("query: " if input_type == "query" else "passage: ") + text
    if "mxbai" in name:
        if input_type == "query":
            return "Represent this sentence for searching relevant passages: " + text
        return text
    # Unknown model: no prefix (safe default — better than guessing wrong).
    return text


def _get_local_model():
    """Lazily import sentence_transformers and load the configured model.
    Imported lazily (not at module top) so `voyage`-only users never pay the
    torch/sentence-transformers import cost. Re-loads only if the configured
    model name changes mid-process."""
    global _local_model, _local_model_name
    model_name = get_local_embed_model()
    if _local_model is None or _local_model_name != model_name:
        from sentence_transformers import SentenceTransformer  # lazy import

        _local_model = SentenceTransformer(model_name)
        _local_model_name = model_name
    return _local_model


def _embed_local(texts: list[str], input_type: str) -> list[list[float]]:
    model_name = get_local_embed_model()
    prefixed = [_prefix_for(model_name, t, input_type) for t in texts]

    st_model = _get_local_model()
    vectors = st_model.encode(prefixed, normalize_embeddings=True)

    out = [[float(x) for x in vec] for vec in vectors]
    if out and len(out[0]) != DIM:
        raise ValueError(
            f"Local model {model_name!r} returned dim {len(out[0])}, expected {DIM}. "
            f"Use a {DIM}-dim model (e.g. mixedbread-ai/mxbai-embed-large-v1) or "
            f"change the schema dim."
        )
    return out

def embed(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """Embed texts; each vector has length 1024.

    input_type: "document" (default) for stored claims, "query" for search queries.
    Backend chosen by EMBEDDINGS_PROVIDER: "voyage" (API, default) or "local"
    (in-process, via sentence-transformers — no external server).
    Returns [] immediately for empty input (no backend call, no model load).
    """
    if not texts:
        return []
    if get_embeddings_provider() == "local":
        return _embed_local(texts, input_type)
    return _embed_voyage(texts, input_type)
