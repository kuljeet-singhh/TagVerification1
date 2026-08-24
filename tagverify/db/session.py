"""
Database connection.

WHAT CHANGED FROM THE PREVIOUS IMPLEMENTATION, AND WHY IT MATTERS
-----------------------------------------------------------------
This used to be Neon's HTTP driver, where every single query is its own HTTPS request.
Latency was therefore (number of queries x round-trip time), measured at 562-2435ms per trip
cross-region. Most of the surrounding code was shaped by that: a hand-written CTE to fold two
queries into one, TTL caches in front of tiny tables, and no transactions at all (the HTTP
driver cannot do them).

A pooled connection from a long-lived process removes that tax. The CTE and the TTL caches
are kept anyway — they are still the right thing, just no longer load-bearing — but
transactions now work, and a query is no longer a network event.

Use the POOLED Neon connection string (the host containing "-pooler").
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tagverify.config import settings

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class DatabaseNotConfigured(RuntimeError):
    """
    Raised when DATABASE_URL is unset.

    This is a normal, reportable condition rather than a crash: /api/v1/health has to be able
    to say "the database is not configured" and still return 200. Monitoring that needs the
    thing it monitors to be healthy is not monitoring.
    """


def _normalise(url: str) -> str:
    """Point SQLAlchemy at the async psycopg3 driver whatever form the URL arrives in."""
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://"):
        if url.startswith(prefix):
            return url.replace(prefix, "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def init_engine() -> AsyncEngine:
    """Create the engine and session factory. Called once from the app lifespan."""
    global _engine, _sessionmaker

    url = (settings().database_url or "").strip()
    if not url:
        raise DatabaseNotConfigured("DATABASE_URL is not set")

    if _engine is None:
        _engine = create_async_engine(
            _normalise(url),
            # Neon's pooler sits in front of this, so the local pool stays small. The
            # important setting is pre_ping: a pooled connection that Neon has recycled
            # underneath us should be replaced silently, not surfaced as a 500.
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=300,
            echo=False,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def is_configured() -> bool:
    return bool((settings().database_url or "").strip())


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """
    A session with commit-on-success / rollback-on-error.

    Transactions are available again now that we are off the HTTP driver, so use them rather
    than relying on every statement being independently atomic.
    """
    if _sessionmaker is None:
        # Lazily initialise so that scripts, tests and the CLI do not have to run the
        # FastAPI lifespan just to talk to the database.
        init_engine()
    assert _sessionmaker is not None

    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency form of `session_scope`."""
    async with session_scope() as session:
        yield session
