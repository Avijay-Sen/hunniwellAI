import kg.embeddings as embeddings


class _FakeEmbedResult:
    def __init__(self, embeddings):
        self.embeddings = embeddings


class _FakeVoyageClient:
    """Records calls and returns one 1024-float vector per input text."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def embed(self, texts, model=None, input_type=None):
        # Record the exact call so the test can assert batching/params.
        self.calls.append({"texts": list(texts), "model": model, "input_type": input_type})
        vectors = [[float(i)] * 1024 for i, _ in enumerate(texts)]
        return _FakeEmbedResult(vectors)


def _install_fake(monkeypatch):
    """Patch the voyageai.Client constructor and the cached module client so no
    network call is ever made. Also stub get_voyage_key so the tests need NO
    VOYAGE_API_KEY in the environment (the key is eval'd eagerly when the client
    is constructed, even though the fake constructor ignores it)."""
    fake = _FakeVoyageClient()
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "voyage")  # deterministic regardless of .env
    monkeypatch.setattr(embeddings, "get_voyage_key", lambda: "test-key")
    monkeypatch.setattr(embeddings.voyageai, "Client", lambda *a, **k: fake)
    # Reset any module-level cached client so the patched constructor is used.
    monkeypatch.setattr(embeddings, "_voyage_client", None, raising=False)
    return fake


def test_embed_returns_one_1024_vector_per_text(monkeypatch):
    _install_fake(monkeypatch)
    out = embeddings.embed(["a", "b"])
    assert len(out) == 2
    assert all(len(v) == 1024 for v in out)
    assert all(isinstance(x, float) for v in out for x in v)


def test_embed_batches_all_texts_in_one_call(monkeypatch):
    fake = _install_fake(monkeypatch)
    embeddings.embed(["a", "b", "c"])
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["texts"] == ["a", "b", "c"]
    assert call["model"] == "voyage-3.5"
    assert call["input_type"] == "document"


def test_embed_defaults_to_document_input_type(monkeypatch):
    fake = _install_fake(monkeypatch)
    embeddings.embed(["a"])
    assert fake.calls[0]["input_type"] == "document"


def test_embed_passes_through_query_input_type(monkeypatch):
    fake = _install_fake(monkeypatch)
    embeddings.embed(["a"], input_type="query")
    assert fake.calls[0]["input_type"] == "query"


def test_embed_empty_list_returns_empty_without_calling_client(monkeypatch):
    fake = _install_fake(monkeypatch)
    out = embeddings.embed([])
    assert out == []
    assert fake.calls == []


# ---- local backend (EMBEDDINGS_PROVIDER=local) ---------------------------------

class _FakeSTModel:
    """Stands in for SentenceTransformer; records the (prefixed) inputs."""

    def __init__(self, dim=1024):
        self.dim = dim
        self.inputs = []

    def encode(self, texts, normalize_embeddings=False):
        self.inputs.extend(texts)
        return [[0.1] * self.dim for _ in texts]


def _install_fake_local(monkeypatch, dim=1024, model="intfloat/multilingual-e5-large"):
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "local")
    monkeypatch.setenv("LOCAL_EMBED_MODEL", model)
    fake = _FakeSTModel(dim)
    monkeypatch.setattr(embeddings, "_get_local_model", lambda: fake)
    return fake


def test_embed_local_routes_and_returns_1024(monkeypatch):
    fake = _install_fake_local(monkeypatch)
    out = embeddings.embed(["a", "b"])
    assert len(out) == 2
    assert all(len(v) == 1024 for v in out)
    assert fake.inputs == ["passage: a", "passage: b"]


def test_embed_local_applies_e5_query_prefix(monkeypatch):
    fake = _install_fake_local(monkeypatch)
    embeddings.embed(["seizure device"], input_type="query")
    assert fake.inputs == ["query: seizure device"]


def test_embed_local_mxbai_prefix_only_for_queries(monkeypatch):
    fake = _install_fake_local(monkeypatch, model="mixedbread-ai/mxbai-embed-large-v1")
    embeddings.embed(["doc"])
    embeddings.embed(["q"], input_type="query")
    assert fake.inputs[0] == "doc"
    assert fake.inputs[1].startswith("Represent this sentence for searching relevant passages: ")


def test_embed_local_rejects_wrong_dim(monkeypatch):
    _install_fake_local(monkeypatch, dim=768)
    try:
        embeddings.embed(["a"])
        assert False, "expected ValueError on dim mismatch"
    except ValueError as e:
        assert "768" in str(e) and "1024" in str(e)
