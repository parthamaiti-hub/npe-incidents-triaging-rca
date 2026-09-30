"""RAG embedding and retrieval over ChromaDB (see app.vector_store).

Two corpora:

- classification: SystemFootprint entries + resolved Incident texts,
  retrieved as closed-set grounding candidates for
  app.classification.classify_with_llm_fallback.
- feedback: embedded RcaFeedback, composite text of an execution's own
  diagnosis plus the feedback itself, retrieved as grounding context for
  LLM/RAG-primary RCA synthesis in app.rca_worker.

Every vector has a deterministic id (footprint:<id>, incident:<id>,
feedback:<id>) and is written with upsert, so a redelivered message or a
rebuild can never duplicate one. MongoDB writes always happen first; the
vector write after it is best-effort and repairable with
scripts/rebuild_vector_index.py.
"""

import datetime
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.config import RAG_EMBED_FEEDBACK_ONLY, RAG_RETENTION_MONTHS
from app.llm_client import embed_texts
from app.models import Incident, RcaFeedback, SystemFootprint, WorkflowExecution
from app.vector_store import VectorStore


@dataclass(frozen=True)
class ClassificationCandidate:
    id: str
    kind: str
    source_system_id: str
    category: str | None
    text: str


@dataclass(frozen=True)
class FeedbackContext:
    id: str
    text: str
    confidence_score: int
    workflow_execution_id: str


def footprint_vector_id(footprint_id: str) -> str:
    return f"footprint:{footprint_id}"


def incident_vector_id(incident_id: str) -> str:
    return f"incident:{incident_id}"


def feedback_vector_id(rca_feedback_id: str) -> str:
    return f"feedback:{rca_feedback_id}"


def _epoch(value: datetime.datetime) -> int:
    """Chroma metadata filters compare numbers, not dates; stored datetimes
    are naive UTC."""
    return int(value.replace(tzinfo=datetime.timezone.utc).timestamp())


def _now_epoch() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


def footprint_text(footprint: SystemFootprint) -> str:
    suffix = f" ({footprint.notes})" if footprint.notes else ""
    return f"{footprint.footprint_type}: {footprint.value}{suffix}"


def feedback_text(feedback: RcaFeedback, execution: WorkflowExecution) -> str:
    rca = execution.rca or {}
    return (
        f"matched_pattern={rca.get('matched_pattern')} rca_status={execution.rca_status} "
        f"root_cause_summary={rca.get('root_cause_summary', '')} "
        f"feedback_comment={feedback.comment or ''}"
    )


async def embed_footprints(vs: VectorStore, client: AsyncOpenAI, footprints: list[SystemFootprint]) -> None:
    if not footprints:
        return
    texts = [footprint_text(f) for f in footprints]
    embeddings = await embed_texts(client, texts)
    # category is omitted, not None: Chroma metadata values can't be null.
    await (await vs.classification()).upsert(
        ids=[footprint_vector_id(f.id) for f in footprints],
        embeddings=embeddings,
        documents=texts,
        metadatas=[{"kind": "footprint", "source_system_id": f.source_system_id, "created_at_ts": _now_epoch()} for f in footprints],
    )


async def embed_footprint(vs: VectorStore, client: AsyncOpenAI, footprint: SystemFootprint) -> None:
    await embed_footprints(vs, client, [footprint])


async def delete_footprint_vector(vs: VectorStore, footprint_id: str) -> None:
    await (await vs.classification()).delete(ids=[footprint_vector_id(footprint_id)])


async def embed_resolved_incidents(vs: VectorStore, client: AsyncOpenAI, incidents: list[Incident]) -> None:
    incidents = [i for i in incidents if i.source_system_id is not None and i.category is not None]
    if not incidents:
        return
    embeddings = await embed_texts(client, [i.raw_text for i in incidents])
    await (await vs.classification()).upsert(
        ids=[incident_vector_id(i.id) for i in incidents],
        embeddings=embeddings,
        documents=[i.raw_text for i in incidents],
        metadatas=[
            {
                "kind": "incident",
                "source_system_id": i.source_system_id,
                "category": i.category,
                "incident_id": i.id,
                "created_at_ts": _epoch(i.received_at),
            }
            for i in incidents
        ],
    )


async def embed_resolved_incident(vs: VectorStore, client: AsyncOpenAI, incident: Incident) -> None:
    """Called right after an incident reaches RESOLVED/LLM_RESOLVED --
    embed-on-write, incremental, not a batch re-embed job."""
    await embed_resolved_incidents(vs, client, [incident])


async def incident_is_embedded(vs: VectorStore, incident_id: str) -> bool:
    return bool((await (await vs.classification()).get(ids=[incident_vector_id(incident_id)], include=[]))["ids"])


async def retrieve_classification_candidates(
    vs: VectorStore,
    client: AsyncOpenAI,
    raw_text: str,
    source_system_id: str | None = None,
    limit: int = 5,
) -> list[ClassificationCandidate]:
    """Top-K nearest classification vectors to raw_text -- the closed-set
    grounding context for classify_with_llm_fallback. Scoped to one
    source_system_id for the category-only ("ANY" rule already won) mode;
    unscoped for the no-match-at-all mode. Fewer than `limit` (or none) on a
    small or empty corpus."""
    [query_embedding] = await embed_texts(client, [raw_text])
    result = await (await vs.classification()).query(
        query_embeddings=[query_embedding],
        n_results=limit,
        where={"source_system_id": source_system_id} if source_system_id is not None else None,
        include=["documents", "metadatas"],
    )
    return [
        ClassificationCandidate(
            id=id,
            kind=meta["kind"],
            source_system_id=meta["source_system_id"],
            category=meta.get("category"),
            text=text,
        )
        for id, text, meta in zip(result["ids"][0], result["documents"][0], result["metadatas"][0])
    ]


async def prune_incident_vectors(vs: VectorStore, older_than: datetime.datetime) -> None:
    await (await vs.classification()).delete(
        where={"$and": [{"kind": "incident"}, {"created_at_ts": {"$lt": _epoch(older_than)}}]}
    )


async def embed_feedbacks(
    vs: VectorStore, client: AsyncOpenAI, pairs: list[tuple[RcaFeedback, WorkflowExecution]]
) -> None:
    """Respects RAG_EMBED_FEEDBACK_ONLY (default true): today every
    RcaFeedback already carries a comment/score by construction, so this is
    mostly a no-op guard, not a filter -- the real curation lever is that
    ONLY feedback gets embedded at all, never every resolved execution
    indiscriminately (the corpus retention/curation policy)."""
    if RAG_EMBED_FEEDBACK_ONLY:
        pairs = [(f, e) for f, e in pairs if f.confidence_score is not None]
    if not pairs:
        return
    texts = [feedback_text(f, e) for f, e in pairs]
    embeddings = await embed_texts(client, texts)
    await (await vs.feedback()).upsert(
        ids=[feedback_vector_id(f.id) for f, _ in pairs],
        embeddings=embeddings,
        documents=texts,
        metadatas=[
            {
                "rca_feedback_id": f.id,
                "workflow_execution_id": e.id,
                "confidence_score": f.confidence_score,
                "created_at_ts": _epoch(f.created_at),
            }
            for f, e in pairs
        ],
    )


async def embed_feedback(vs: VectorStore, client: AsyncOpenAI, feedback: RcaFeedback, execution: WorkflowExecution) -> None:
    """Embeds one RcaFeedback for future RCA-synthesis grounding.
    Called inline on feedback write -- low volume, not a hot path, doesn't
    need the Redis Stream decoupling RCA synthesis uses."""
    await embed_feedbacks(vs, client, [(feedback, execution)])


async def retrieve_feedback_context(
    vs: VectorStore, client: AsyncOpenAI, evidence_text: str, limit: int = 5
) -> list[FeedbackContext]:
    """Top-K nearest feedback vectors to this execution's evidence --
    grounding context for RCA synthesis, bounded by RAG_RETENTION_MONTHS so
    the corpus doesn't grow unmanaged at the confirmed 1,000-incidents/24h
    scale."""
    [query_embedding] = await embed_texts(client, [evidence_text])
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=RAG_RETENTION_MONTHS * 30)
    result = await (await vs.feedback()).query(
        query_embeddings=[query_embedding],
        n_results=limit,
        where={"created_at_ts": {"$gte": int(cutoff.timestamp())}},
        include=["documents", "metadatas"],
    )
    return [
        FeedbackContext(
            id=id,
            text=text,
            confidence_score=meta["confidence_score"],
            workflow_execution_id=meta["workflow_execution_id"],
        )
        for id, text, meta in zip(result["ids"][0], result["documents"][0], result["metadatas"][0])
    ]
