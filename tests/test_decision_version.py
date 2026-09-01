"""
The fingerprint of "how a verdict gets decided".

This exists because of a real incident. profile re-decides from raw scores on every read so a
threshold edit applies retroactively to images already analysed — and a downstream integrator
cached our DECIDED verdicts keyed on `packs_version` alone, so the retroactivity stopped at
the HTTP boundary and superseded refusals kept being served. `decision_version` is the value
that lets a consumer key on the rule as well as the prompts.

The same incident had a second half: the server was running without `--reload`, so an edited
banding rule never took effect and nothing anywhere said so. RULE_VERSION is read once at
import for that reason, and `test_the_rule_version_describes_the_loaded_module` is what stops
someone "fixing" it into a per-request read, which would make it always agree and therefore
always useless.

Pure: no database, no model, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import tagverify.tags.decide as decide_module
from tagverify.tags.decide import RULE_VERSION, Thresholds, decision_version

ROOT = Path(__file__).resolve().parent.parent


def t(**over) -> Thresholds:
    return Thresholds(**over)


def test_it_looks_like_a_version() -> None:
    assert re.fullmatch(r"[0-9a-f]{12}", decision_version({"alcohol": t()}))
    assert re.fullmatch(r"[0-9a-f]{12}", RULE_VERSION)



def test_it_is_stable_across_calls_and_dict_identity() -> None:
    """A fresh dict with the same contents must not look like a change."""
    assert decision_version({"a": t(), "b": t()}) == decision_version({"a": t(), "b": t()})


def test_dict_order_is_not_a_change() -> None:
    """Iteration order is not configuration. Without the sort this is flaky, not wrong."""
    assert decision_version({"a": t(), "b": t()}) == decision_version({"b": t(), "a": t()})


@pytest.mark.parametrize(
    "over",
    [
        {"threshold_low": 0.31},
        {"threshold_high": 0.56},
        {"sigmoid_floor": 0.006},
        {"calibrated": True},
        {"packs_version_seen": "deadbeef1234"},
        {"precision": 0.9},
        {"recall": 0.9},
    ],
)
def test_every_field_moves_it(over: dict) -> None:
    """
    Mechanically folded rather than hand-picked, so a field added later and wired into
    `decide()` cannot silently escape the fingerprint. `precision`/`recall` do not affect a
    verdict today; covering them costs one spare cache generation and removes a whole class
    of future bug.
    """
    assert decision_version({"alcohol": t(**over)}) != decision_version({"alcohol": t()})


def test_adding_or_removing_a_tag_moves_it() -> None:
    base = {"alcohol": t()}
    assert decision_version({**base, "gambling": t()}) != decision_version(base)
    assert decision_version({}) != decision_version(base)


def test_none_does_not_collide_with_empty_string() -> None:
    """`packs_version_seen` is nullable, and null means something different from blank."""
    assert decision_version({"a": t(packs_version_seen=None)}) != decision_version(
        {"a": t(packs_version_seen="")}
    )


def test_editing_the_fallback_moves_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `decide()` does `t = threshold or FALLBACK`, so these defaults decide every tag with no
    row — 19 of 20 today. A fingerprint of only the rows would call that change invisible.
    """
    before = decision_version({"alcohol": t()})
    monkeypatch.setattr(decide_module, "FALLBACK", Thresholds(threshold_high=0.9))
    assert decision_version({"alcohol": t()}) != before


def test_the_fallback_key_cannot_collide_with_a_real_slug() -> None:
    """A tag literally named after the sentinel must not shadow it."""
    assert decision_version({"\x00fallback": t(threshold_high=0.9)}) != decision_version(
        {"\x00fallback": t()}
    )


def test_it_does_not_expose_the_numbers() -> None:
    """AGENTS.md rule 6. A digest of the thresholds is not the thresholds."""
    version = decision_version({"alcohol": t(threshold_high=0.7818, sigmoid_floor=0.0016)})
    assert "0.7818" not in version
    assert "0.0016" not in version


# ------------------------------------------------- the scorer fingerprint





