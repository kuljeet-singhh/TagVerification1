"""drop the decision thresholds from tag_thresholds

`threshold_low`, `threshold_high` and `sigmoid_floor` were the verdict: a score at or above
high was present, at or below low was absent, and the floor was an absolute-resemblance veto
checked before either. All three applied to a SIMILARITY, which means nothing on its own --
measured true positives ran 0.028 to 0.902 -- so every tag had to be calibrated before any
verdict meant anything.

`decide()` stopped reading them when the ranking model was removed in f357a1c. A read model
answers the question, so `present` arrives decided and there is nothing to band (AGENTS.md
rule 3). They have been inert since; this revision stops them being inert AND editable.

WHY THIS IS A FIX AND NOT A TIDY-UP. The admin form over these numbers was not harmless. Every
field of `Thresholds` folds into `decision_version` mechanically, so saving a cutoff re-keyed
every cached verdict here and in dooh-backend -- a full re-score for a change that could not
alter one verdict. And the same save set `calibrated = false` and nulled precision/recall,
which IS load-bearing and, with `apply-calibration` deleted, was a one-way door.

`escalate` goes with them: a boolean read by nothing but its own column definition, already
named as dead beside `detect_objects` in ContentTag.extra.

KEPT: `calibrated`, `precision`, `recall`, `packs_version_seen`. Those answer "has this been
measured, and does the measurement still describe the prompts we serve?", which is rule 4 and
is not repealed by changing models. `docs/VLM_SCORING.md` §5.2 is explicit -- keep the table,
drop only the numbers, let the eval harness write the rest -- so the shape is left alone for
that writer to land into.

NOTHING IS ARCHIVED, unlike migration 0003. The phrase packs were hand-authored and measured;
these cutoffs are provisional guesses seeded from packs.json for 12 of 20 rows, and the 8 real
calibrations keep the halves that were actually measured. A number nothing reads is not a
measurement worth preserving.

Revision ID: 0004_drop_decision_thresholds
Revises: 0003_drop_phrase_columns
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_drop_decision_thresholds"
down_revision = "0003_drop_phrase_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tag_thresholds", "threshold_low")
    op.drop_column("tag_thresholds", "threshold_high")
    op.drop_column("tag_thresholds", "sigmoid_floor")
    op.drop_column("tag_thresholds", "escalate")


def downgrade() -> None:
    """
    Re-adds the four with their original NOT NULL server defaults, which is a complete restore
    in a way 0003's was not: every row is handed the same provisional guess it would have been
    seeded with, and the 8 calibrated rows lose only cutoffs that were computed FROM data still
    present in `precision` / `recall`.

    It restores the shape, not the tuning. If a ranking model ever comes back, recalibrate --
    do not assume these defaults describe it.
    """
    op.add_column(
        "tag_thresholds",
        sa.Column(
            "threshold_low", sa.REAL(), nullable=False, server_default=sa.text("0.3")
        ),
    )
    op.add_column(
        "tag_thresholds",
        sa.Column(
            "threshold_high", sa.REAL(), nullable=False, server_default=sa.text("0.55")
        ),
    )
    op.add_column(
        "tag_thresholds",
        sa.Column(
            "sigmoid_floor", sa.REAL(), nullable=False, server_default=sa.text("0.005")
        ),
    )
    op.add_column(
        "tag_thresholds",
        sa.Column(
            "escalate", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
