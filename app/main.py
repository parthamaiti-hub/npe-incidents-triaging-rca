import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import (
    CORS_ALLOWED_ORIGINS,
    LLM_FALLBACK_ENABLED,
    MONGODB_DB,
    MONGODB_URL,
    RABBITMQ_URL,
    RCA_SYNTHESIS_LLM_ENABLED,
    REDIS_URL,
)
from app.db import ensure_indexes, get_database, make_mongo_client
from app.events import declare_incidents_raw, make_channel, make_connection
from app.function_lifecycle import check_function_integrity
from app.function_registry import refresh_function_registry_from_db
from app.idempotency import make_redis
from app.routers.catalog import router as catalog_router
from app.routers.functions import router as functions_router
from app.routers.incidents import router as incidents_router
from app.routers.stats import router as stats_router
from app.routers.workflows import router as workflows_router
from app.vector_store import VectorStore, default_vector_store
from app.webhooks import router as webhooks_router

logger = logging.getLogger(__name__)


def _vector_store_required() -> bool:
    """Chroma sits only on the LLM/RAG paths; the deterministic pipeline
    must stay healthy without it."""
    return LLM_FALLBACK_ENABLED or RCA_SYNTHESIS_LLM_ENABLED


def create_app(
    mongodb_url: str = MONGODB_URL,
    mongodb_db: str = MONGODB_DB,
    redis_url: str = REDIS_URL,
    rabbitmq_url: str = RABBITMQ_URL,
    vector_store: VectorStore | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.mongo = make_mongo_client(mongodb_url)
        app.state.db = get_database(app.state.mongo, mongodb_db)
        app.state.vector_store = vector_store or default_vector_store()
        app.state.redis = make_redis(redis_url)
        app.state.rabbitmq_connection = await make_connection(rabbitmq_url)
        app.state.rabbitmq_channel = await make_channel(app.state.rabbitmq_connection)
        await declare_incidents_raw(app.state.rabbitmq_channel)  # idempotent, same as ensure_indexes
        app.state.http_client = httpx.AsyncClient(timeout=15.0)
        try:
            await ensure_indexes(app.state.db)
            await refresh_function_registry_from_db(app.state.db)
            # Sol-104: a pinned check version that can't run (missing,
            # retired, code removed) is reported now, not when an incident
            # hits it. Logged, not fatal -- GET /functions/integrity shows it.
            for problem in await check_function_integrity(app.state.db):
                log = logger.error if problem["severity"] == "error" else logger.warning
                log("check_type integrity: %s", problem["message"])
        except Exception:
            # MongoDB unreachable at startup -- FUNCTION_REGISTRY keeps its
            # built-in defaults (exactly the fallback it's designed for), and
            # /health reports the database as down.
            pass
        try:
            yield
        finally:
            await app.state.http_client.aclose()
            await app.state.rabbitmq_connection.close()
            await app.state.redis.aclose()
            await app.state.mongo.close()

    app = FastAPI(title="NPE Incident Triage", lifespan=lifespan)
    if CORS_ALLOWED_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=CORS_ALLOWED_ORIGINS,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(webhooks_router)
    app.include_router(catalog_router)
    app.include_router(functions_router)
    app.include_router(workflows_router)
    app.include_router(incidents_router)
    app.include_router(stats_router)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        """Actually exercises each dependency (not just "is the process
        listening") -- a 200 from /docs only proves FastAPI itself is up,
        not that the DB/Redis/RabbitMQ connections this app needs are live.

        The vector store (Chroma) is always reported, but only fails the
        check when an LLM feature that reads it is enabled."""
        checks: dict[str, str] = {}
        healthy = True

        try:
            await request.app.state.db.command("ping")
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {exc}"
            healthy = False

        try:
            await request.app.state.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"error: {exc}"
            healthy = False

        try:
            if not request.app.state.rabbitmq_connection.is_closed:
                checks["rabbitmq"] = "ok"
            else:
                checks["rabbitmq"] = "not connected"
                healthy = False
        except Exception as exc:
            checks["rabbitmq"] = f"error: {exc}"
            healthy = False

        try:
            await request.app.state.vector_store.heartbeat()
            checks["vector_store"] = "ok"
        except Exception as exc:
            if _vector_store_required():
                checks["vector_store"] = f"error: {exc}"
                healthy = False
            else:
                checks["vector_store"] = f"unavailable (not required, LLM features off): {exc}"

        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "unhealthy", "checks": checks},
        )

    return app


app = create_app()
