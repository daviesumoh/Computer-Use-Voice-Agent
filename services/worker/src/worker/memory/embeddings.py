"""Turning text into vectors for similarity search."""

from collections.abc import Sequence
from typing import Protocol

from openai import AsyncOpenAI

EMBEDDING_MODEL = "text-embedding-3-small"
DIMENSIONS = 1536  # the width of memories.embedding


class Embedder(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(model=EMBEDDING_MODEL, input=list(texts))
        return [item.embedding for item in response.data]


def vector_literal(vector: Sequence[float] | None) -> str | None:
    """pgvector's text form, for a `%s::extensions.vector` parameter."""
    return None if vector is None else "[" + ",".join(f"{x:.7g}" for x in vector) + "]"
