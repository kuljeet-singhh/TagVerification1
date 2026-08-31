"""
Alembic environment.

WHY THIS EXISTS AT ALL
----------------------
It did not, until the first real schema change. pyproject.toml recorded the plan: docs/
schema.sql provisions a NEW database, the inherited one needed nothing, and a migration tool
would be added when something first had to change in place. `api_keys.scopes` is that change —
the table holds live keys, so it cannot be re-created from schema.sql.

docs/schema.sql is still the provisioning file and still has to be kept in step. It describes
the destination; migrations describe how an existing database gets there.

THE URL IS NOT IN alembic.ini
-----------------------------
It comes from the same Settings object the app uses, so there is one place a connection string
is configured and no chance of a migration running against a different database from the one
serving traffic. It is also why the password never lands in a tracked file.

SYNC, WHILE THE APP IS ASYNC
----------------------------
Migrations are a short-lived offline script with no concurrency to gain from, and Alembic's
async support exists to bridge exactly this gap. Reusing the app's async engine here would
mean an event loop and a greenlet shim for no benefit, so the URL is rewritten to the plain
psycopg driver instead. Same database, same driver family, one fewer moving part.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from tagverify.config import settings
from tagverify.db.models import Base

config = context.config

#: Autogenerate compares against this. Keep it pointed at the app's real metadata so a model
#: change that nobody wrote a migration for shows up as a diff rather than as a surprise in
#: production.
target_metadata = Base.metadata


def _url() -> str:
    url = (settings().database_url or "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set — nothing to migrate.")
    # The app normalises to postgresql+psycopg (async); Alembic runs sync psycopg. Both are
    # psycopg3, so the DSN is identical apart from the driver token.
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it — `alembic upgrade head --sql`."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
