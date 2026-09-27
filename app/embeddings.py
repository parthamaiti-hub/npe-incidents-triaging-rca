"""pgvector-backed RAG retrieval.

Two corpora, same Postgres instance (pgvector extension, no separate
vector-DB service -- see app.db's before_create event):

- ClassificationEmbedding: SystemFootprint rows + resolved
  Incident rows, retrieved as closed-set grounding candidates for
  app.classification.classify_with_llm_fallback.
- FeedbackEmbedding: embedded RcaFeedback, composite text of
  an execution's own diagnosis plus the feedback itself, retrieved as
  grounding context for LLM/RAG-primary RCA synthesis in app.rca_worker.
"""

import datetime
import uuid

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import RAG_EMBED_FEEDBACK_ONLY, RAG_RETENTION_MONTHS
from app.llm_client import embed_texts
from app.models import (
    ClassificationEmbedding,
    FeedbackEmbedding,
    Incident,
    RcaFeedback,
    SystemFootprint,
    WorkflowExecution,
)


def _footprint_text(footprint: SystemFootprint) -> str:
    suffix = f" ({footprint.notes})" if footprint.notes else ""
    return f"{footprint.footprint_type}: {footprint.value}{suffix}"


async def embed_footprint(session: AsyncSession, client: AsyncOpenAI, footprint: SystemFootprint) -> None:
    text = _footprint_text(footprint)
    [embedding] = await embed_texts(client, [text])
    session.add(
        ClassificationEmbedding(
            id=str(uuid.uuid4()),
            kind="footprint",
            source_system_id=footprint.source_system_id,
            category=None,
            text=text,
            embedding=embedding,
        )
    )


async def embed_resolved_incident(session: AsyncSession, client: AsyncOpenAI, incident: Incident) -> None:
    """Called right after an incident reaches RESOLVED/LLM_RESOLVED --
    embed-on-write, incremental, not a batch re-embed job."""
    if incident.source_system_id is None or incident.category is None:
        return
    [embedding] = await embed_texts(client, [incident.raw_text])
    session.add(
        ClassificationEmbedding(
            id=str(uuid.uuid4()),
            kind="incident",
            source_system_id=incident.source_system_id,
            category=incident.category,
            text=incident.raw_text,
            embedding=embedding,
            incident_id=incident.id,
        )
    )


async def retrieve_classification_candidates(
    session: AsyncSession,
    client: AsyncOpenAI,
    raw_text: str,
    source_system_id: str | None = None,
    limit: int = 5,
) -> list[ClassificationEmbedding]:
    """Top-K nearest ClassificationEmbedding rows to raw_text -- the closed-
    set grounding context for classify_with_llm_fallback. Scoped to
    one source_system_id for the category-only ("ANY" rule already won)
    mode; unscoped for the no-match-at-all mode."""
    [query_embedding] = await embed_texts(client, [raw_text])
    stmt = select(ClassificationEmbedding).order_by(ClassificationEmbedding.embedding.cosine_distance(query_embedding)).limit(
        limit
    )
    if source_system_id is not None:
        stmt = stmt.where(ClassificationEmbedding.source_system_id == source_system_id)
    return list((await session.scalars(stmt)).all())


def _feedback_text(feedback: RcaFeedback, execution: WorkflowExecution) -> str:
    rca = execution.rca or {}
    return (
        f"matched_pattern={rca.get('matched_pattern')} rca_status={execution.rca_status} "
        f"root_cause_summary={rca.get('root_cause_summary', '')} "
        f"feedback_comment={feedback.comment or ''}"
    )


async def embed_feedback(
    session: AsyncSession, client: AsyncOpenAI, feedback: RcaFeedback, execution: WorkflowExecution
) -> None:
    """Embeds one RcaFeedback for future RCA-synthesis grounding.
    Synchronous, called inline on feedback write -- low volume, not a hot
    path, doesn't need the Redis Stream decoupling RCA synthesis uses. Respects RAG_EMBED_FEEDBACK_ONLY (default true): today every
    RcaFeedback row already carries a comment/score by construction, so
    this is mostly a no-op guard, not a filter -- the real curation lever
    is that ONLY feedback gets embedded at all, never every resolved
    execution indiscriminately (the corpus retention/curation policy)."""
    if RAG_EMBED_FEEDBACK_ONLY and feedback.confidence_score is None:
        return
    text = _feedback_text(feedback, execution)
    [embedding] = await embed_texts(client, [text])
    session.add(
        FeedbackEmbedding(
            id=str(uuid.uuid4()),
            rca_feedback_id=feedback.id,
            workflow_execution_id=execution.id,
            text=text,
            confidence_score=feedback.confidence_score,
            embedding=embedding,
        )
    )


async def retrieve_feedback_context(
    session: AsyncSession, client: AsyncOpenAI, evidence_text: str, limit: int = 5
) -> list[FeedbackEmbedding]:
    """Top-K nearest FeedbackEmbedding rows to this execution's evidence --
    grounding context for RCA synthesis, bounded by
    RAG_RETENTION_MONTHS so the corpus doesn't grow unmanaged at the
    confirmed 1,000-incidents/24h scale."""
    [query_embedding] = await embed_texts(client, [evidence_text])
    cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(
        days=RAG_RETENTION_MONTHS * 30
    )
    stmt = (
        select(FeedbackEmbedding)
        .where(FeedbackEmbedding.created_at >= cutoff)
        .order_by(FeedbackEmbedding.embedding.cosine_distance(query_embedding))
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())
