"""
`calibrated`, and the one rule it exists to enforce.

AGENTS.md rule 4: an uncalibrated verdict is a guess, and presenting a guess as a measurement
is the failure this product exists to prevent. The subtler half of that rule is a SUPERSEDED
measurement, which is what these tests are mostly about.

THE BUG THEY WERE WRITTEN FOR. The staleness comparison was written out inline twice -- in
`decide()` and again in `api/v1/tags.py` -- and the admin calibration panel had a third answer:
it read `row.calibrated` straight from the database and applied no comparison at all. So the
panel rendered eight tags green, with precision and recall beside them, while every API response
for those same tags reported `calibrated: false`. The measurements were real; they had been
taken against a `packs_version` the prompts moved off.

That panel has since been removed -- nothing could write `tag_thresholds`, so it reported a
constant -- but the rule it broke is unchanged and these tests outlive it. `effective_calibrated`
is the single function every remaining caller uses.
"""

from __future__ import annotations

from tagverify.db.session import session_scope
from tagverify.scoring import registry
from tagverify.tags.decide import (
    FALLBACK,
    Thresholds,
    decide,
    effective_calibrated,
    load_thresholds,
)
from tests.conftest import needs_db

LIVE = "665544af9296"
OLD = "1a2db18ba940"


# ------------------------------------------------------------------ the rule


def test_a_measurement_taken_against_other_prompts_is_not_calibrated() -> None:
    """
    The case the admin panel used to get wrong, stated as plainly as it can be.

    A tag measured under one wording of the question is not measured under another. The number
    was real; it no longer describes what is being asked.
    """
    stale = Thresholds(calibrated=True, packs_version_seen=OLD, precision=0.69, recall=0.93)
    assert effective_calibrated(stale, LIVE) is False


def test_a_current_measurement_is_calibrated() -> None:
    current = Thresholds(calibrated=True, packs_version_seen=LIVE)
    assert effective_calibrated(current, LIVE) is True


def test_never_measured_is_never_calibrated() -> None:
    """No amount of matching fingerprints makes an unmeasured tag measured."""
    assert effective_calibrated(FALLBACK, LIVE) is False
    assert effective_calibrated(Thresholds(packs_version_seen=LIVE), LIVE) is False


def test_an_unknown_live_version_passes_the_stored_flag_through() -> None:
    """
    None means "we could not ask", not "stale".

    Manufacturing a `false` out of our own ignorance would be its own dishonesty, and would
    flip every tag to uncalibrated whenever the scorer was briefly unreachable.
    """
    measured = Thresholds(calibrated=True, packs_version_seen=OLD)
    assert effective_calibrated(measured, None) is True


def test_a_measurement_with_no_recorded_version_is_taken_at_its_word() -> None:
    """Null `packs_version_seen` cannot be shown to be stale, so it is not called stale."""
    assert effective_calibrated(Thresholds(calibrated=True), LIVE) is True


def test_decide_reports_the_same_answer_as_the_helper() -> None:
    """`decide()` must not re-derive it — that is how the three copies drifted apart."""
    raw = {"tag": "alcohol", "present": True, "score": 0.9, "top_phrase": "a glass of beer"}
    stale = Thresholds(calibrated=True, packs_version_seen=OLD)
    assert decide(raw, stale, LIVE).calibrated is effective_calibrated(stale, LIVE)
    assert decide(raw, stale, LIVE).calibrated is False


# --------------------------------------------------- and what the API reports


@needs_db
async def test_the_api_agrees_with_the_helper(client, api_key: str) -> None:
    """
    The regression, at the surface that still exists.

    It used to be the admin calibration panel against `/api/v1/tags`, because those two
    disagreed for every stale calibration: the panel read `TagThreshold.calibrated` straight
    from the database and rendered eight tags as measured while the API reported all of them
    uncalibrated. The panel is gone, so the comparison is now the API against the one function
    -- which is the half that was ever authoritative.

    Compared PER TAG rather than in aggregate: a count that happens to match while individual
    rows disagree is exactly the sort of pass that lets this come back.
    """
    api = await client.get("/api/v1/tags", headers={"x-api-key": api_key})
    assert api.status_code == 200
    from_api = {t["slug"]: t["calibrated"] for t in api.json()["tags"]}
    assert from_api, "no tags to compare"

    async with session_scope() as session:
        stored = await load_thresholds(session)
        live = (await registry.module().health(session)).packs_version

    for slug, calibrated in from_api.items():
        expected = effective_calibrated(stored.get(slug) or FALLBACK, live)
        assert calibrated is expected, (
            f"{slug}: API says calibrated={calibrated}, effective_calibrated says {expected}"
        )
