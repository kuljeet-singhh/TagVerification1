"""
The decision layer. No database, no inference service — `decide()` is pure.

These are the highest-value tests in the project: they cover the three rules that are easy
to break and expensive to get wrong (the sigmoid veto ordering, the uncertain band, and the
stale-pack guard), plus the extraction of `band_of` into inference/banding.py, which is now
shared with the model service and the calibration sweep.

Ported from scripts/smoke-decide.mts.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from dooh.tags.decide import Thresholds, decide
from inference.banding import band_of, confidence_of

PACKS = "a5788f8d8e1d"
ROOT = Path(__file__).resolve().parent.parent


def threshold(**over: Any) -> Thresholds:
    base = {
        "threshold_low": 0.25,
        "threshold_high": 0.78,
        "sigmoid_floor": 0.005,
        "calibrated": True,
        "precision": 0.61,
        "recall": 0.6,
        "packs_version_seen": PACKS,
    }
    return Thresholds(**(base | over))


def raw(**over: Any) -> dict[str, Any]:
    base = {
        "tag": "alcohol",
        "present": True,
        "score": 0.9,
        "sigmoid": 0.5,
        "band": "present",
        "confidence": "high",
        "decided_by": "siglip",
        "top_phrase": "a glass of beer with foam",
        "crop": [0, 0, 1, 1],
    }
    return base | over


# ------------------------------------------------------------------- banding


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.90, True),   # above high
        (0.10, False),  # below low
        (0.50, None),   # inside the band -> uncertain, NOT false
        (0.78, True),   # exactly at high: inclusive
        (0.25, False),  # exactly at low: inclusive
    ],
)
def test_banding(score: float, expected: bool | None) -> None:
    assert decide(raw(score=score), threshold()).present is expected


def test_uncertain_is_null_not_false() -> None:
    """
    The distinction the whole product rests on. `null` means a human must look;
    flattening it to False is how a non-compliant creative reaches a screen.
    """
    verdict = decide(raw(score=0.5), threshold())
    assert verdict.present is None
    assert verdict.band == "uncertain"
    assert verdict.confidence == "low"


# --------------------------------------------------- the sigmoid floor veto


def test_high_score_with_zero_sigmoid_is_vetoed() -> None:
    """
    Softmax must sum to 1, so an image containing nothing relevant can still hand a large
    share to a positive prompt purely by beating equally-irrelevant options. Without the
    veto a photo of a mountain reads as alcohol.
    """
    verdict = decide(raw(score=0.99, sigmoid=0.0), threshold())
    assert verdict.present is False
    assert verdict.decided_by == "sigmoid_floor"


def test_genuine_low_sigmoid_positive_is_not_vetoed() -> None:
    """Measured true positives run as low as 0.028 sigmoid. The floor must not eat them."""
    assert decide(raw(score=0.9, sigmoid=0.028), threshold()).present is True


def test_veto_is_checked_before_the_bands() -> None:
    """Order matters: a below-floor sigmoid wins even against a score above `high`."""
    band, present, decided_by = band_of(score=0.99, sigmoid=0.001, low=0.25, high=0.78, floor=0.005)
    assert (band, present, decided_by) == ("absent", False, "sigmoid_floor")


# ------------------------------------------- the band that was tried and removed


def test_a_high_score_just_above_the_floor_is_present() -> None:
    """
    A "near-floor" band used to downgrade this to uncertain, so that a gym poster scoring 0.99
    for `gambling` at sigmoid 0.0104 went to review instead of refusing the advertiser.

    It was removed because it cannot work. A genuine cheeseburger sits at 0.0108 for
    `junk_food` — four ten-thousandths away, on the opposite side of the truth — so the band
    did not separate false positives from true ones, it only chose which kind of error to
    make. It chose the one that puts junk food on a screen that blocks junk food.

    The gym poster's actual defect was its score, and it is fixed in detector.py's competition
    pool. This test is here so nobody reintroduces the band on the next false positive.
    """
    assert band_of(0.99, 0.0108, 0.25, 0.78, 0.005) == ("present", True, "siglip")
    assert band_of(0.99, 0.0104, 0.25, 0.78, 0.005) == ("present", True, "siglip")


# -------------------------------------------------------- stale-pack guard


def test_matching_pack_version_stays_calibrated() -> None:
    assert decide(raw(), threshold(), PACKS).calibrated is True


def test_pack_changed_after_calibration_is_reported_uncalibrated() -> None:
    assert decide(raw(), threshold(), "different_hash").calibrated is False


def test_stale_thresholds_are_still_applied() -> None:
    """Still the best guess available — we stop trusting it, we do not stop using it."""
    assert decide(raw(score=0.9), threshold(), "different_hash").present is True


def test_no_live_version_raises_no_false_alarm() -> None:
    assert decide(raw(), threshold(), None).calibrated is True


def test_never_calibrated_tag_stays_uncalibrated() -> None:
    assert decide(raw(), threshold(calibrated=False), PACKS).calibrated is False


# ------------------------------------------------------ missing threshold row


def test_missing_row_falls_back_to_defaults_and_is_uncalibrated() -> None:
    verdict = decide(raw(score=0.9), None, PACKS)
    assert verdict.present is True
    assert verdict.calibrated is False


# ---------------------------------------------------------------- confidence


@pytest.mark.parametrize(
    ("score", "band", "expected"),
    [
        (0.99, "present", "high"),      # 0.21 past high
        (0.88, "present", "medium"),    # 0.10 past high
        (0.80, "present", "low"),       # 0.02 past high
        (0.02, "absent", "high"),       # 0.23 below low
        (0.15, "absent", "medium"),     # 0.10 below low
        (0.24, "absent", "low"),        # 0.01 below low
        (0.50, "uncertain", "low"),     # no margin exists
    ],
)
def test_confidence_margins(score: float, band: str, expected: str) -> None:
    assert confidence_of(score, 0.25, 0.78, band) == expected


# ---------------------------------------------------- the shared extraction


@pytest.mark.parametrize("module", ["detector.py", "calibrate.py"])
def test_inference_modules_import_the_shared_rule(module: str) -> None:
    """
    The rule used to be written out three times, in two languages, with comments in each
    copy insisting they stay identical. This asserts the copies are gone.

    Checked against the source rather than by importing, because detector.py pulls in torch
    and transformers, which live in inference/.venv and deliberately not in the web app's
    environment.
    """
    source = (ROOT / "inference" / module).read_text()

    assert "from banding import" in source or "from .banding import" in source

    # The literal shape of the old inlined rule. If either of these comes back, the copy is
    # back with it.
    assert "sigmoid < floor" not in source
    assert 'decided_by = "sigmoid_floor"' not in source


def test_the_rule_does_not_fingerprint_itself() -> None:
    """
    `decision_version` hashes this file's bytes, and the temptation is to put that hash here
    next to the rule it describes. It cannot go here: banding.py ships flat to the Space and
    must import nothing (see the test below). The hashing lives in dooh/tags/decide.py.
    """
    source = (ROOT / "inference" / "banding.py").read_text()
    assert "hashlib" not in source


def test_banding_module_has_no_dependencies() -> None:
    """
    banding.py ships to the Hugging Face Space as a flat file and is imported by the web app
    as a package. It can only work in both if it imports nothing of its own.
    """
    tree = ast.parse((ROOT / "inference" / "banding.py").read_text())
    modules = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [None])
    }
    assert modules <= {"typing", "__future__"}, modules
