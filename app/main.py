from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import CORS_ALLOWED_ORIGINS, DATABASE_URL, RABBITMQ_URL, REDIS_URL
from app.db import make_async_engine, make_async_session_factory
from app.events import declare_incidents_raw, make_channel, make_connection
from app.function_registry import refresh_function_registry_from_db
from app.idempotency import make_redis
from app.routers.catalog import router as catalog_router
from app.routers.functions import router as functions_router
from app.routers.incidents import router as incidents_router
from app.routers.stats import router as stats_router
from app.routers.workflows import router as workflows_router
from app.webhooks import router as webhooks_router


def create_app(
    database_url: str = DATABASE_URL,
    redis_url: str = REDIS_URL,
    rabbitmq_url: str = RABBITMQ_URL,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_async_engine(database_url)
        app.state.session_factory = make_async_session_factory(engine)
        app.state.redis = make_redis(redis_url)
        app.state.rabbitmq_connection = await make_connection(rabbitmq_url)
        app.state.rabbitmq_channel = await make_channel(app.state.rabbitmq_connection)
        await declare_incidents_raw(app.state.rabbitmq_channel)  # idempotent, same as Base.metadata.create_all
        app.state.http_client = httpx.AsyncClient(timeout=15.0)
        try:
            async with app.state.session_factory() as session:
                await refresh_function_registry_from_db(session)
        except Exception:
            # function_definition table may not exist yet on a fresh/
            # unmigrated DB -- FUNCTION_REGISTRY keeps its built-in
            # defaults, which is exactly the fallback it's designed for.
            pass
        try:
            yield
        finally:
            await app.state.http_client.aclose()
            await app.state.rabbitmq_connection.close()
            await app.state.redis.aclose()
            await engine.dispose()

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
        not that the DB/Redis/RabbitMQ connections this app needs are live."""
        checks: dict[str, str] = {}
        healthy = True

        try:
            async with request.app.state.session_factory() as session:
                await session.execute(text("SELECT 1"))
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

        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "unhealthy", "checks": checks},
        )

    return app


app = create_app()
