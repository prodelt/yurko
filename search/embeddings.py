"""Pluggable text embedders.

Budget = 0 UAH, so the default production embedder is Google's free Gemini
Embedding API (``GeminiEmbedder``). A local sentence-transformers model
(``LocalEmbedder``) is the offline fallback, and ``NullEmbedder`` (no vectors)
keeps CI and key-less environments fully offline.

Embeddings are only consumed from MVP-1 onwards (hybrid search). In MVP-0 the
factory returns ``NullEmbedder`` unless a key/model is explicitly configured,
so nothing here requires network access or an API key by default.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

# Embedding dimensions per supported model. The active model pins the
# ``article_embeddings.embedding`` column dimension in db/schema.sql.
GEMINI_DIM = 768
LOCAL_E5_SMALL_DIM = 384


class Embedder(Protocol):
    """Common contract for text embedders."""

    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
        ...


class NullEmbedder:
    """No-op embedder: produces no vectors. Hybrid search degrades to FTS."""

    model = "null"
    dim = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


class GeminiEmbedder:
    """Google Gemini Embedding API (free tier) over REST.

    Defaults to ``gemini-embedding-001`` — a top-MTEB GA model whose **free-tier
    quota actually lets us embed the whole corpus at 0 UAH** (the newer
    ``gemini-embedding-2`` is higher quality but its free tier hard-caps after a
    small daily burst; switch to it via ``EMBEDDING_MODEL=gemini-embedding-2`` on
    a paid tier). We request ``outputDimensionality=768`` so the
    ``article_embeddings.embedding`` column stays ``vector(768)`` for any model.

    Uses the public generativelanguage endpoint with an ``X-goog-api-key``
    header (works with both ``AIza...`` and ``AQ...`` keys). Compute runs on
    Google's side. Requests use ``:batchEmbedContents`` when supported, falling
    back to per-item ``:embedContent`` so the embedder works across models.
    """

    _BASE = "https://generativelanguage.googleapis.com/v1beta/models/{model}"
    _BATCH_ENDPOINT = _BASE + ":batchEmbedContents"
    _SINGLE_ENDPOINT = _BASE + ":embedContent"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-embedding-001",
        dim: int = GEMINI_DIM,
        batch_size: int = 50,
        timeout: int = 60,
    ) -> None:
        self.model = model
        self.dim = dim
        self._api_key = api_key
        self._batch_size = batch_size
        self._timeout = timeout
        self._use_batch = True

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import time

        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = texts[start : start + self._batch_size]
            out.extend(self._embed_chunk(chunk))
            # Pause between batches to stay under free-tier RPM limit.
            if start + self._batch_size < len(texts):
                time.sleep(1)
        return out

    def _post(self, url: str, payload: dict[str, Any]) -> Any:
        """POST with exponential backoff on 429 (free-tier rate limit)."""
        import time

        import requests

        response = None
        for attempt in range(5):
            response = requests.post(
                url,
                headers={"Content-Type": "application/json", "X-goog-api-key": self._api_key},
                json=payload,
                timeout=self._timeout,
            )
            if response.status_code == 429:
                time.sleep(2**attempt * 10)  # 10s, 20s, 40s, 80s, 160s
                continue
            return response
        return response

    def _embed_chunk(self, chunk: list[str]) -> list[list[float]]:
        if self._use_batch:
            payload: dict[str, Any] = {
                "requests": [
                    {
                        "model": f"models/{self.model}",
                        "content": {"parts": [{"text": text}]},
                        "outputDimensionality": self.dim,
                    }
                    for text in chunk
                ]
            }
            response = self._post(self._BATCH_ENDPOINT.format(model=self.model), payload)
            if response is not None and response.status_code == 404:
                # Model doesn't expose the batch method — fall back to per-item.
                self._use_batch = False
            else:
                response.raise_for_status()
                data = response.json()
                return [
                    [float(value) for value in item.get("values", [])]
                    for item in data.get("embeddings", [])
                ]

        out: list[list[float]] = []
        for text in chunk:
            payload = {
                "model": f"models/{self.model}",
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": self.dim,
            }
            response = self._post(self._SINGLE_ENDPOINT.format(model=self.model), payload)
            response.raise_for_status()
            values = response.json().get("embedding", {}).get("values", [])
            out.append([float(value) for value in values])
        return out


class LocalEmbedder:
    """Offline fallback using sentence-transformers (CPU). Imported lazily."""

    def __init__(
        self,
        model: str = "intfloat/multilingual-e5-small",
        dim: int = LOCAL_E5_SMALL_DIM,
    ) -> None:
        self.model = model
        self.dim = dim
        self._st_model = None  # lazy

    def _ensure_model(self) -> None:
        if self._st_model is not None:
            return
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

        self._st_model = SentenceTransformer(self.model)  # type: ignore[assignment]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_model()
        assert self._st_model is not None
        vectors = self._st_model.encode(texts, normalize_embeddings=True)
        return [list(map(float, vector)) for vector in vectors]


def make_embedder() -> Embedder:
    """Build the embedder from environment.

    - EMBEDDING_PROVIDER=gemini|local|null overrides auto-detection.
    - Auto: GeminiEmbedder if GOOGLE_API_KEY is set, else NullEmbedder.
    """
    provider = os.getenv("EMBEDDING_PROVIDER", "").strip().lower()
    model = os.getenv("EMBEDDING_MODEL", "").strip()
    google_key = os.getenv("GOOGLE_API_KEY", "").strip()

    if not provider:
        provider = "gemini" if google_key else "null"

    if provider == "gemini":
        if not google_key:
            raise ValueError("EMBEDDING_PROVIDER=gemini requires GOOGLE_API_KEY")
        return GeminiEmbedder(api_key=google_key, model=model or "gemini-embedding-001")
    if provider == "local":
        return LocalEmbedder(model=model or "intfloat/multilingual-e5-small")
    return NullEmbedder()
