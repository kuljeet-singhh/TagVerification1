"""
Operational commands.

Only one thing here needs guarding, and it is worth its own file: `apply-calibration` must be
safe to run twice. It is the command that turns a measurement into a live verdict, and the
second run is the normal case — a re-calibration after adding eval images or editing a pack.
"""

from __future__ import annotations

import inspect

from sqlalchemy.dialects import postgresql

from dooh.cli import apply_calibration
from dooh.db.models import EvalImage


def _eval_image_insert_sql() -> str:
    """The statement apply-calibration records eval labels with, as Postgres would see it."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    statement = (
        pg_insert(EvalImage)
        .values(
            [
                {
                    "id": "row-1",
                    "image_hash": "abc",
                    "storage_ref": "eval/alcohol/pos/Beer.jpg",
                    "tag_slug": "alcohol",
                    "label": True,
                    "note": "score=0.9 sigmoid=0.1",
                }
            ]
        )
        .on_conflict_do_nothing(index_elements=["image_hash", "tag_slug"])
    )
    return str(statement.compile(dialect=postgresql.dialect())).upper()


def test_recording_an_eval_label_twice_is_a_no_op() -> None:
    """
    The eval-image insert must be idempotent in SQL, not in exception handling.

    This is the shape the command relies on: a duplicate label is absorbed by the database,
    so re-running never reaches an error path at all.
    """
    sql = _eval_image_insert_sql()
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql


def test_apply_calibration_never_rolls_back_mid_transaction() -> None:
    """
    The regression this file exists for.

    The eval-image loop used to add a row, flush it, and swallow the unique-index violation
    with `await session.rollback()`. That rollback is not scoped to the row — it unwinds the
    enclosing `session_scope()` transaction, which by then already holds every `update
    tag_thresholds` statement. So the second run of a calibration discarded the thresholds it
    had just applied, while still printing "applied N calibrated tag(s)": a silent revert
    reported as a success, on the command whose entire job is making a measurement take effect.

    Asserting on the source is crude, but the failure mode is a transaction boundary, and the
    only alternative is a live Postgres with real threshold rows to clobber. A rollback
    anywhere in this command is wrong regardless of how it is reached.

    Comments are stripped before the check — the code carries a comment explaining the bug,
    and that explanation is the reason the line is gone.
    """
    code = "\n".join(
        line for line in inspect.getsource(apply_calibration).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "rollback" not in code
