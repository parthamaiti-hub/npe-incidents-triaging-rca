"""Regenerates the ChromaDB RAG corpora from MongoDB, the source of truth.

Chroma only ever holds derived data, so this is the answer to every way the
two stores can drift apart: a crash between a Mongo write and its vector
upsert, a best-effort embed that failed (OpenAI or Chroma down), a lost
Chroma volume, or a new embedding model (whose collections start empty,
see app.vector_store). Idempotent: vector ids are deterministic and written
with upsert, so re-running it never duplicates anything.

Per corpus it also deletes vectors whose source document is gone (e.g. a
footprint deleted while Chroma was unreachable).

  footprints -- every SystemFootprint
  incidents  -- every fully-classified Incident (within
                RAG_CLASSIFICATION_RETENTION_MONTHS, when set)
  feedback   -- every RcaFeedback, with its execution's diagnosis

Usage:
    uv run python -m scripts.rebuild_vector_index                     # everything, re-embed all
    uv run python -m scripts.rebuild_vector_index --missing           # only fill the gaps
    uv run python -m scripts.rebuild_vector_index --only footprints --missing
    uv run python -m scripts.rebuild_vector_index --missing --dry-run # report gaps, change nothing

Re-embedding calls OpenAI (OPENAI_API_KEY); --dry-run never does.
"""

import argparse
import asyncio
import datetime
import json

from openai import AsyncOpenAI
from pymongo.asynchronous.database import AsyncDatabase

from app.classification import FULLY_CLASSIFIED_STATUSES
from app.config import RAG_CLASSIFICATION_RETENTION_MONTHS
from app.db import get_database, make_mongo_client
from app.embeddings import (
    embed_feedbacks,
    embed_footprints,
    embed_resolved_incidents,
    feedback_vector_id,
    footprint_vector_id,
    incident_vector_id,
)
from app.llm_client import make_openai_client
from app.models import Incident, RcaFeedback, SystemFootprint, WorkflowExecution
from app.repositories.base import find
from app.vector_store import VectorStore, default_vector_store

CORPORA = ("footprints", "incidents", "feedback")
BATCH_SIZE = 100
PAGE_SIZE = 1000


async def _vector_ids(collection, where: dict | None) -> set[str]:
    ids: set[str] = set()
    offset = 0
    while True:
        page = await collection.get(where=where, include=[], limit=PAGE_SIZE, offset=offset)
        ids.update(page["ids"])
        if len(page["ids"]) < PAGE_SIZE:
            return ids
        offset += PAGE_SIZE


def _batches(items: list, size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _sync_corpus(
    collection,
    where: dict | None,
    expected: dict[str, object],
    embed_batch,
    *,
    missing: bool,
    dry_run: bool,
) -> dict:
    """expected: vector id -> source document(s). Embeds all of them (or
    only the ones without a vector, with missing=True) and deletes vectors
    with no source document."""
    present = await _vector_ids(collection, where)
    to_embed = [key for key in expected if not (missing and key in present)]
    stale = sorted(present - expected.keys())
    if not dry_run:
        for batch in _batches(to_embed):
            await embed_batch([expected[key] for key in batch])
        for batch in _batches(stale):
            await collection.delete(ids=batch)
    return {"source": len(expected), "had_vector": len(present & expected.keys()), "embedded": len(to_embed), "stale_deleted": len(stale)}


async def rebuild(
    db: AsyncDatabase,
    vs: VectorStore,
    client: AsyncOpenAI | None = None,
    only: list[str] | None = None,
    missing: bool = False,
    dry_run: bool = False,
) -> dict:
    only = list(only or CORPORA)
    client = client or make_openai_client()
    report: dict = {"dry_run": dry_run, "missing_only": missing}

    if "footprints" in only:
        footprints = await find(db, SystemFootprint)
        report["footprints"] = await _sync_corpus(
            await vs.classification(),
            {"kind": "footprint"},
            {footprint_vector_id(f.id): f for f in footprints},
            lambda batch: embed_footprints(vs, client, batch),
            missing=missing,
            dry_run=dry_run,
        )

    if "incidents" in only:
        filter: dict = {
            "classification_status": {"$in": list(FULLY_CLASSIFIED_STATUSES)},
            "source_system_id": {"$ne": None},
            "category": {"$ne": None},
        }
        if RAG_CLASSIFICATION_RETENTION_MONTHS is not None:
            filter["received_at"] = {
                "$gte": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                - datetime.timedelta(days=RAG_CLASSIFICATION_RETENTION_MONTHS * 30)
            }
        incidents = await find(db, Incident, filter)
        report["incidents"] = await _sync_corpus(
            await vs.classification(),
            {"kind": "incident"},
            {incident_vector_id(i.id): i for i in incidents},
            lambda batch: embed_resolved_incidents(vs, client, batch),
            missing=missing,
            dry_run=dry_run,
        )

    if "feedback" in only:
        feedbacks = await find(db, RcaFeedback)
        executions = {
            e.id: e
            for e in await find(db, WorkflowExecution, {"_id": {"$in": list({f.workflow_execution_id for f in feedbacks})}})
        }
        pairs = {feedback_vector_id(f.id): (f, executions[f.workflow_execution_id]) for f in feedbacks if f.workflow_execution_id in executions}
        report["feedback"] = await _sync_corpus(
            await vs.feedback(),
            None,
            pairs,
            lambda batch: embed_feedbacks(vs, client, batch),
            missing=missing,
            dry_run=dry_run,
        )

    return report


def rebuild_sync(only: list[str] | None = None, missing: bool = False, dry_run: bool = False) -> dict:
    """For synchronous callers (dataloadscripts.load_catalog)."""

    async def _run() -> dict:
        mongo = make_mongo_client()
        try:
            return await rebuild(get_database(mongo), default_vector_store(), only=only, missing=missing, dry_run=dry_run)
        finally:
            await mongo.close()

    return asyncio.run(_run())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", choices=CORPORA, help="Corpus to rebuild (repeatable; default: all)")
    parser.add_argument("--missing", action="store_true", help="Embed only documents that have no vector yet")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; no OpenAI calls, no writes")
    args = parser.parse_args()
    print(json.dumps(rebuild_sync(only=args.only, missing=args.missing, dry_run=args.dry_run), indent=2))


if __name__ == "__main__":
    main()
