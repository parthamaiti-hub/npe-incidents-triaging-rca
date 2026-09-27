"""Shared OpenAI access layer for classification fallback and RCA
synthesis -- one place each feature calls into, matching
how app.opa_client is the one place OPA gets called from.

Factory function + explicit client parameter (not a cached global), same
dependency-injection style as app.idempotency.make_redis /
app.events.make_connection -- tests pass a mock client directly instead of
patching a module-level singleton.
"""

from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import OPENAI_API_KEY, OPENAI_EMBEDDING_MODEL, OPENAI_MODEL

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


def make_openai_client(api_key: str = OPENAI_API_KEY) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key)


async def embed_texts(client: AsyncOpenAI, texts: list[str]) -> list[list[float]]:
    """Batched embedding call -- callers should pass every text they need
    embedded in one call rather than looping embed_texts([single]), same
    reasoning any batch API exists for."""
    if not texts:
        return []
    response = await client.embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=texts)
    return [d.embedding for d in response.data]


async def structured_completion(
    client: AsyncOpenAI,
    system_prompt: str,
    user_prompt: str,
    response_model: type[ResponseModelT],
) -> ResponseModelT:
    """One shared structured-output call for both classification fallback
    and RCA synthesis -- response_format=response_model constrains the
    model to a schema Pydantic validates directly, never freeform prose
    parsed after the fact. This is the closed-set anti-hallucination
    discipline both classification fallback and RCA synthesis depend on:
    the caller's response_model is what enforces "never invent a label
    outside the known set," not a post-hoc string check."""
    completion = await client.chat.completions.parse(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format=response_model,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise ValueError("OpenAI structured completion returned no parsed result")
    return parsed
