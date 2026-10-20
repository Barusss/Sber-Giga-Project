"""Optional local embeddings for hybrid clause matching.

`fastembed` is an optional dependency: without it, or on any provider error,
analysis keeps working with the lexical channel and reports the reason in trace.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from typing import Callable, Sequence

from app.core.config import settings

EMBEDDING_DIM = 384
_CACHE_LIMIT = 2048


class EmbeddingError(RuntimeError):
    pass


class EmbeddingService:
    def __init__(
        self,
        *,
        enabled: bool | None = None,
        model: str | None = None,
        batch_size: int | None = None,
        provider: Callable[[list[str]], Sequence[Sequence[float]]] | None = None,
    ) -> None:
        self._model_name = model or settings.embedding_model
        self._batch_size = batch_size or settings.embedding_batch_size
        self._provider = provider
        self._embedder = None
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self.model_id = f"fastembed:{self._model_name}"
        self.dim = EMBEDDING_DIM
        self.reason: str | None = None

        if provider is not None:
            self.enabled = enabled is not False
        elif enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = settings.embedding_enabled
            if self.enabled and settings.database_url.startswith("sqlite"):
                self.enabled = False
                self.reason = "SQLite: embeddings are only enabled for PostgreSQL"

        if not self.enabled and self.reason is None:
            self.reason = "EMBEDDING_ENABLED=false"

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "model": self.model_id,
            "dim": self.dim,
            "reason": self.reason,
        }

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not self.enabled or not texts:
            return []
        prefixed = [self._with_prefix(text, "passage") for text in texts]
        return self._embed(prefixed)

    def embed_query(self, text: str) -> list[float] | None:
        if not self.enabled or not text.strip():
            return None
        vectors = self._embed([self._with_prefix(text, "query")])
        return vectors[0] if vectors else None

    def _with_prefix(self, text: str, kind: str) -> str:
        if "e5" in self._model_name.casefold():
            return f"{kind}: {text}"
        return text

    def _embed(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float] | None] = [None] * len(texts)
        missing: list[tuple[int, str]] = []
        for index, text in enumerate(texts):
            cached = self._cache.get(text)
            if cached is None:
                missing.append((index, text))
            else:
                self._cache.move_to_end(text)
                result[index] = cached

        try:
            for start in range(0, len(missing), self._batch_size):
                batch = missing[start:start + self._batch_size]
                vectors = self._call_provider([text for _, text in batch])
                if len(vectors) != len(batch):
                    raise EmbeddingError("Embedding provider returned a wrong batch size")
                for (index, text), vector in zip(batch, vectors):
                    values = [float(value) for value in vector]
                    if len(values) != self.dim:
                        raise EmbeddingError(
                            f"Expected {self.dim}-dimensional vectors, got {len(values)}"
                        )
                    if not all(math.isfinite(value) for value in values):
                        raise EmbeddingError("Embedding contains non-finite values")
                    self._cache[text] = values
                    self._cache.move_to_end(text)
                    result[index] = values
        except Exception as exc:  # optional channel must never break the analysis
            self.enabled = False
            self.reason = str(exc) or exc.__class__.__name__
            return []

        while len(self._cache) > _CACHE_LIMIT:
            self._cache.popitem(last=False)
        return [vector for vector in result if vector is not None]

    def _call_provider(self, texts: list[str]) -> list[Sequence[float]]:
        if self._provider is not None:
            return list(self._provider(texts))
        if self._embedder is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise EmbeddingError(
                    "fastembed is not installed; lexical matching is used"
                ) from exc
            self._embedder = TextEmbedding(model_name=self._model_name)
        return list(self._embedder.embed(texts))
