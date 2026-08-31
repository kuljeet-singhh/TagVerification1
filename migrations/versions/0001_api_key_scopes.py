"""api_keys.scopes — let a key be authorised for tag writes

The first migration in this repo. Everything before it was provisioned from docs/schema.sql,
which is still the file that creates a NEW database; this one exists because `api_keys` holds
live credentials and cannot be re-created.

NOT NULL with a '[]' default rather than nullable: every existing key is analyze-only, which
is exactly what an empty list means, so there is no "unknown" state to model and no backfill
to get wrong. The default also means a key inserted by older code — during a rolling deploy,
say — lands in the safe state rather than a null the scope check would have to special-case.

Revision ID: 0001_api_key_scopes
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_api_key_scopes"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    # Safe to reverse: dropping the column returns every key to analyze-only, which is what
    # they all were before it existed. A key issued FOR tag writes stops being able to make
    # them, which is the correct direction to fail.
    op.drop_column("api_keys", "scopes")
