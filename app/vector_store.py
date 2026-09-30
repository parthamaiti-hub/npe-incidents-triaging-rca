"""ChromaDB holder for the two RAG corpora.

Chroma is derived data: MongoDB is the source of truth, and
scripts/rebuild_vector_index.py regenerates every collection from it. So a
lost volume, a crash between a Mongo write and its Chroma upsert, or an
embedding-model change is always "run the rebuild", never data loss.

The app computes embeddings itself (app.llm_client.embed_texts) and passes
them in; collections are created with embedding_function=None so Chroma
never downloads a local model, and with cosine space so ranking matches the
old pgvector cosine_distance ordering.

A collection's dimensionality is fixed at first insert, so the embedding
model and dimensions are part of its name: switching models means a new,
empty collection plus a rebuild, never a mixed-dimension failure.

Connecting is lazy -- the deterministic pipeline never touches Chroma, so the
app starts (and /health stays green, see app.main) with Chroma down.
"""

import asyncio
import re

import chromadb
from chromadb.api import AsyncClientAPI
from chromadb.api.models.AsyncCollection import AsyncCollection

from app.config import (
    CHROMA_AUTH_TOKEN,
    CHROMA_COLLECTION_PREFIX,
    CHROMA_HOST,
    CHROMA_PORT,
    CHROMA_SSL,
    OPENAI_EMBEDDING_DIMENSIONS,
    OPENAI_EMBEDDING_MODEL,
)

CLASSIFICATION = "classification"
FEEDBACK = "feedback"


def collection_name(prefix: str, base: str, model: str, dims: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return f"{prefix}{base}__{slug}_{dims}"


class VectorStore:
    def __init__(
        self,
        host: str = CHROMA_HOST,
        port: int = CHROMA_PORT,
        ssl: bool = CHROMA_SSL,
        auth_token: str = CHROMA_AUTH_TOKEN,
        prefix: str = CHROMA_COLLECTION_PREFIX,
        model: str = OPENAI_EMBEDDING_MODEL,
        dims: int = OPENAI_EMBEDDING_DIMENSIONS,
    ):
        self.host = host
        self.port = port
        self.ssl = ssl
        self.auth_token = auth_token
        self.prefix = prefix
        self.model = model
        self.dims = dims
        self._client: AsyncClientAPI | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def name(self, base: str) -> str:
        return collection_name(self.prefix, base, self.model, self.dims)

    async def client(self) -> AsyncClientAPI:
        # The HTTP client belongs to the event loop it was created in;
        # scripts and tests may drive this from more than one asyncio.run().
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            headers = {"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else None
            self._client = await chromadb.AsyncHttpClient(host=self.host, port=self.port, ssl=self.ssl, headers=headers)
            self._loop = loop
        return self._client

    async def collection(self, base: str) -> AsyncCollection:
        client = await self.client()
        return await client.get_or_create_collection(
            self.name(base), embedding_function=None, configuration={"hnsw": {"space": "cosine"}}
        )

    async def classification(self) -> AsyncCollection:
        return await self.collection(CLASSIFICATION)

    async def feedback(self) -> AsyncCollection:
        return await self.collection(FEEDBACK)

    async def heartbeat(self) -> int:
        return await (await self.client()).heartbeat()

    async def drop_all(self) -> None:
        """Deletes this store's collections (tests, and a full rebuild)."""
        client = await self.client()
        for base in (CLASSIFICATION, FEEDBACK):
            try:
                await client.delete_collection(self.name(base))
            except Exception:  # noqa: BLE001 -- NotFoundError: nothing to drop
                pass


_default: VectorStore | None = None


def default_vector_store() -> VectorStore:
    """The process-wide store built from config, for callers that weren't
    handed one explicitly (workers, scripts)."""
    global _default
    if _default is None:
        _default = VectorStore()
    return _default
