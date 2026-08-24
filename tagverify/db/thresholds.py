"""
Writes to `tag_thresholds`.

Reads live in tagverify/tags/decide.py, next to the memo that caches them and the rule that
applies them. Only the write side is here, for the same reason every other statement in this
package is: a route handler that builds its own SQL is a place where the schema can drift out
of sight of the models, and where a `commit()` can go missing without anything failing loudly.

This is the ONLY path that sets thresholds by hand. `dooh apply-calibration` writes measured
numbers through the CLI instead, and the difference is recorded in `updated_by` — 'admin'
here, 'calibrate' there, 'seed' for the initial import from packs.json.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Upsert, not update. The previous implementation only ever UPDATEd, so a tag present in
# packs.json with no row here silently no-opped — the admin pressed Save, nothing happened,
# and no error appeared anywhere.
_UPSERT = text(
    """
    insert into tag_thresholds
      (slug, threshold_low, threshold_high, sigmoid_floor,
       calibrated, precision, recall, updated_at, updated_by)
    values (:slug, :low, :high, :floor, false, null, null, now(), 'admin')
    on conflict (slug) do update set
      threshold_low = :low,
      threshold_high = :high,
      sigmoid_floor = :floor,
      -- Hand-editing means these are no longer the calibrated numbers.
      calibrated = false,
      precision = null,
      recall = null,
      updated_at = now(),
      updated_by = 'admin'
    """
)


async def upsert_threshold(
    session: AsyncSession,
    slug: str,
    *,
    low: float,
    high: float,
    floor: float,
) -> None:
    """
    Save hand-edited thresholds for one tag, and mark them uncalibrated.

    `calibrated` drops to false and precision/recall are cleared, because the numbers a human
    typed are no longer the ones calibrate.py measured. Leaving the flag true would let a
    guess keep presenting itself as a measurement, which is the failure mode this product
    exists to prevent — see tagverify/tags/decide.py.

    Does NOT invalidate the threshold memo. That is the caller's job: the memo is process
    state, not database state, and a background writer must not silently reach into the
    serving path. The admin handler calls invalidate_threshold_cache() straight after this.

    Range and ordering validation happens before the call. A `low` above `high` would make a
    score satisfy both "absent" and "present" and leave the ORDER of the checks in banding.py
    to decide the verdict.
    """
    await session.execute(_UPSERT, {"slug": slug, "low": low, "high": high, "floor": floor})
    await session.commit()
