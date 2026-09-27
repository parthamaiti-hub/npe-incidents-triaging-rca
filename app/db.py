import asyncio
import sys
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import DDL, Engine, create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DATABASE_URL

if sys.platform == "win32":
    # psycopg's async driver cannot run under Windows' default
    # ProactorEventLoop; it requires a SelectorEventLoop. Irrelevant in the
    # Linux Docker Compose deployment, but needed for local Windows dev.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class Base(DeclarativeBase):
    pass


# Every one of this project's ~20 Base.metadata.create_all(...)
# call sites (dataloadscripts, tests, app startup) needs the pgvector
# extension present before the Vector-typed embedding columns can be
# created. A before_create DDL event fires automatically for all of them,
# so no call site needs to change -- the alternative (a shared "create
# schema" helper) doesn't exist in this codebase; every caller invokes
# create_all directly.
event.listen(
    Base.metadata,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS vector").execute_if(dialect="postgresql"),
)


def make_engine(database_url: str = DATABASE_URL) -> Engine:
    return create_engine(database_url, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, future=True)


def make_async_engine(database_url: str = DATABASE_URL) -> AsyncEngine:
    return create_async_engine(database_url, future=True)


def make_async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def get_async_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session
