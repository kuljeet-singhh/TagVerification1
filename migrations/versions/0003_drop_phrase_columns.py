"""drop the SigLIP phrase columns from content_tags

SigLIP ranked an image against a pool of phrases and returned a similarity; it could not
answer "is there alcohol here?" without one, so every tag carried `positives`, hard
`negatives`, the `rationale` explaining why THOSE negatives, and a per-tag `sigmoid_floor`
override. The model was removed in f357a1c. The VLM that replaced it reads a tag's
`description` and nothing else, and has read nothing else since.

THE COLUMNS WERE KEPT ONCE, DELIBERATELY, so that a rollback to `main` would be a checkout
rather than a re-authoring exercise. That is what this revision retires: `development` is
VLM-only, `SCORER` no longer accepts "siglip" (scoring/registry.py KNOWN), and columns kept
for a rollback nobody will make are columns that go stale and mislead instead.

NO FINGERPRINT MOVES. This is the fact that makes the drop safe rather than merely tidy.
`packs_version` is `scoring/prompt.py:catalog_fingerprint`, which hashes model id,
PROMPT_VERSION and every active tag's slug and DESCRIPTION; `decision_version` covers the
decision rule and the thresholds. Neither has ever included a phrase. So every verdict cached
against this catalog -- ours in `creative_tag_analyses` and dooh-backend's -- stays valid, and
nothing is re-scored.

`sigmoid_floor` here is CONTENT_TAGS.sigmoid_floor, the SigLIP pack override, read by nothing.
It is NOT `tag_thresholds.sigmoid_floor`, which is measured, still applied, and still folded
into `decision_version`. Different column, different table, untouched.

The packs are archived at `inference/phrase_packs_archive.json` -- 24 tags, 158 positives, 179
mirrored negatives, 4 rationales -- beside gate_cache.json, for the reason AGENTS.md gives
that directory: the measurement outlives the code. They are hand-authored and the rationales
record what was measured, which is the part that cannot be rewritten from memory.

Revision ID: 0003_drop_phrase_columns
Revises: 0002_pack_header
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_drop_phrase_columns"
down_revision = "0002_pack_header"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("content_tags", "positives")
    op.drop_column("content_tags", "negatives")
    op.drop_column("content_tags", "rationale")
    op.drop_column("content_tags", "sigmoid_floor")


def downgrade() -> None:
    """
    Restores the SHAPE, and cannot restore the CONTENT -- a dropped column takes its data with
    it. Re-add these and every tag has an empty pool, which under SigLIP is worse than no
    column at all: it scores, and it scores wrongly.

    So the real down-migration is two steps, and this is only the first. Replay
    `inference/phrase_packs_archive.json` into the re-added columns before pointing anything
    at them.

    positives/negatives come back NULLABLE, not NOT NULL as they were. NOT NULL with no
    default would refuse to apply against existing rows, and defaulting them to '[]' would
    manufacture the empty pools this docstring is warning about. Nullable states the truth:
    the phrases are not here.
    """
    op.add_column(
        "content_tags",
        sa.Column("positives", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "content_tags",
        sa.Column("negatives", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("content_tags", sa.Column("rationale", sa.Text(), nullable=True))
    op.add_column("content_tags", sa.Column("sigmoid_floor", sa.REAL(), nullable=True))
