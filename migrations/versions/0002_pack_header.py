"""pack_header — the non-tag half of packs.json, so publishing needs no file

`dooh export-packs` used to READ inference/packs.json in order to WRITE it: the four
file-level keys (`$comment`, `prompt_template`, `shared_distractors`, `defaults`) lived only
there, so render_catalog had to open the old file to preserve them. Inside the Docker image
that file does not exist -- the Dockerfile copies banding.py and versioning.py out of
inference/ and nothing else -- so publishing failed in production while working fine from a
checkout. This table removes the file from the input side.

`body` IS TEXT, DELIBERATELY. packs_version hashes the exported file's bytes, so a key
emitted in a different order is a different pack to every downstream fingerprint: every tag
would flip to `calibrated: false` and apply-calibration would refuse the existing calibration
file, all for a change that moved no score. JSONB does not preserve key order and `defaults`
carries five keys in a fixed one. TEXT stores the bytes and hands them back unchanged.

No backfill here. The row is written by `dooh seed-tags`, which already owns reading the pack
file, and which stays non-destructive so re-running it cannot revert an edit. A missing row
makes render_catalog refuse with a message naming the command -- the same shape as its
existing empty-catalog refusal, and better than a migration guessing at content it would have
to read from a file this database has no access to.

Revision ID: 0002_pack_header
Revises: 0001_api_key_scopes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_pack_header"
down_revision = "0001_api_key_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pack_header",
        # Always 1. The CHECK is what makes "the header" singular in the schema rather than
        # by convention -- with two rows the exporter would have to pick one, and picking is
        # exactly the ambiguity worth refusing.
        sa.Column("id", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="pack_header_singleton"),
    )


def downgrade() -> None:
    # Reversing loses the header, and the only other copy is whatever inference/packs.json
    # happens to hold. Export the pack before downgrading if that file is not current --
    # `dooh export-packs` writes exactly the block this table stores.
    op.drop_table("pack_header")
