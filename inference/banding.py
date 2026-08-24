"""
The banding rule: turn a raw score into a verdict.

THIS IS THE SINGLE SOURCE OF TRUTH. DO NOT COPY IT.
---------------------------------------------------
This rule used to be written out three separate times -- in detector.py (for the Space's
own provisional verdict), in calibrate.py (so a threshold sweep evaluates candidates exactly
the way production will), and again in TypeScript in the API tier. All three carried comments
insisting they had to stay identical, which is the kind of promise that holds right up until
it doesn't. Now the API tier is Python too, so all three import this instead.

The functions here are deliberately pure: floats in, verdict out. No model, no torch, no
database, no I/O. That is what makes the whole product's most consequential decision
straightforward to unit-test.

WHY THIS FILE LIVES IN inference/ RATHER THAN dooh/
---------------------------------------------------
inference/ is git-subtree-pushed to a Hugging Face Space as a standalone app, so it cannot
import from the web application. The web application CAN import from here, so this is the
only direction the dependency can point.

That means this module has to work in two layouts: flat inside the Space (`import banding`)
and packaged in the repo (`from inference.banding import ...`). It has no imports of its own,
so it works unchanged in both. Its callers use a try/except import shim.
"""

from __future__ import annotations

from typing import Literal

Band = Literal["present", "absent", "uncertain"]
DecidedBy = Literal["siglip", "sigmoid_floor"]
Confidence = Literal["high", "medium", "low"]

def band_of(
    score: float,
    sigmoid: float,
    low: float,
    high: float,
    floor: float,
) -> tuple[Band, bool | None, DecidedBy]:
    """
    Decide a verdict from a raw score and its absolute-resemblance check.

    Returns (band, present, decided_by), where `present` is None for "uncertain" -- meaning
    the score landed between the thresholds and a human (or a vision LLM) should look. That
    is NOT the same as absent, and callers that flatten it to False are the reason this
    distinction is carried all the way out to the public API.

    ORDER MATTERS. The sigmoid floor is a VETO and is checked FIRST.
    ---------------------------------------------------------------
    Softmax must sum to 1, so an image containing nothing relevant can still hand a large
    share of the probability mass to a positive prompt purely by beating equally-irrelevant
    options. Without this check a photo of a mountain reads as "alcohol". The floor asks the
    absolute question -- does this image resemble beer AT ALL? -- and overrides the relative
    one when the answer is no.

    Keep the floor LOW. Measured true positives run as low as 0.028 sigmoid while true
    negatives sit at 0.000. It is a backstop, not a gate; raise it and you start vetoing real
    detections.

    DO NOT ADD A SECOND SIGMOID CHECK ABOVE THE FLOOR
    --------------------------------------------------
    This was tried. A "near-floor" band downgraded a high score to `uncertain` when the sigmoid
    cleared the floor by less than 4x, to stop a gym poster being refused as `gambling` at
    sigmoid 0.0104. It was removed because it cannot work: a genuine cheeseburger sits at
    0.0108 for `junk_food`, four ten-thousandths away and on the opposite side of the truth.
    The band did not separate them, it just moved the error from false positives to false
    negatives -- and a real burger reached a screen that blocks junk food.

    The sigmoid's scale is PER-TAG, not global. A measured true positive is 0.1037 for
    `sugary_drinks`, 0.0280 for `alcohol`, 0.0138 for `junk_food`. No global band fits all
    three, and a per-tag one is just `sigmoid_floor`, which already exists and is already
    calibratable. The gym poster's real defect was its SCORE, and it was fixed where scores
    are made -- see the competition pool in detector.py's `_verdict`.

    Both comparisons against the softmax score are INCLUSIVE (`>= high`, `<= low`), so a
    score sitting exactly on a threshold is decided, never uncertain.
    """
    if sigmoid < floor:
        return "absent", False, "sigmoid_floor"
    if score >= high:
        return "present", True, "siglip"
    if score <= low:
        return "absent", False, "siglip"
    return "uncertain", None, "siglip"


def confidence_of(score: float, low: float, high: float, band: Band | str) -> Confidence:
    """
    How far past the threshold did we land?

    Callers use this to triage: a "present" verdict with low confidence sat just barely over
    the line and is worth a second look even though it was decided.

    An uncertain verdict is always low confidence -- by definition it did not clear either
    threshold, so there is no margin to measure.
    """
    if band == "uncertain":
        return "low"
    margin = score - high if band == "present" else low - score
    if margin >= 0.20:
        return "high"
    if margin >= 0.08:
        return "medium"
    return "low"
