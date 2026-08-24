"""
Collapsing per-frame verdicts into one verdict per tag.

THE RULE
--------
    present (any frame)  >  uncertain (any frame)  >  absent (every frame)

and within the winning band, the highest-scoring frame supplies the evidence.

A creative is non-compliant if the bottle is on screen for one second of ten, so a tag is
present for the video the moment it is present for any frame. The same logic is what makes
"uncertain" beat "absent": one frame a human needs to look at is a video a human needs to look
at. Only a video where EVERY frame was decided absent is absent.

WHY NOT max(score)
------------------
Two reasons, and both are the kind of bug that produces a confident wrong answer rather than
an error.

1. `present: null` would get flattened. A video with one uncertain frame and five absent ones
   has a higher-scoring absent frame than uncertain one about as often as not, so a plain
   score max would report "absent" for a creative that nobody has actually cleared. Rule 1 in
   AGENTS.md exists precisely to stop that, and it does not stop applying because there are
   now six frames instead of one.

2. The sigmoid floor is a PER-FRAME veto, applied before banding (inference/banding.py). A
   frame can score 0.92 and still be absent because it resembles nothing at all — that is the
   "a photo of a mountain reads as alcohol" case the veto exists for. Ranking frames by raw
   score would let exactly those vetoed frames win, reintroducing the bug one level up where
   the veto cannot see it.

So: band first, score only as the tie-break inside a band.

WHY THE EVIDENCE IS CARRIED WHOLE
---------------------------------
The winning frame's score, sigmoid, phrase and crop all travel together, never mixed with
another frame's. This mirrors detector.py, which picks the best crop and then reports THAT
crop's sigmoid and phrase rather than the best of each independently. A verdict assembled from
the highest score of one frame and the highest sigmoid of another describes no frame that
exists, and its crop would point at the wrong place.

WHY THIS LIVES IN dooh/ AND NOT inference/
------------------------------------------
banding.py had to live in inference/ because the Space cannot import from the web app. That
constraint does not apply here: the Space only ever sees a single still and has no concept of
a video, so this rule has exactly one caller. It is kept pure for the same reason banding.py
is — floats and dataclasses in, a verdict out, so it can be tested exhaustively.
"""

from __future__ import annotations

from dooh.tags.decide import Verdict

#: Higher wins. Mirrors the Band literal in inference/banding.py.
_PRECEDENCE: dict[str, int] = {"absent": 0, "uncertain": 1, "present": 2}


def _rank(verdict: Verdict) -> tuple[int, float]:
    """Band first, score as the tie-break within a band."""
    return (_PRECEDENCE.get(verdict.band, 0), verdict.score)


def aggregate(verdicts: list[Verdict]) -> list[Verdict]:
    """
    One verdict per tag, from verdicts covering one or more frames.

    Tag order is preserved from first appearance, so the response order still follows the
    order the caller asked for. A single frame in gives that frame's verdict straight back
    out, which is what keeps the still-image path bit-for-bit unchanged by this module rather
    than merely intended to be.
    """
    best: dict[str, Verdict] = {}

    for verdict in verdicts:
        incumbent = best.get(verdict.tag)
        if incumbent is None or _rank(verdict) > _rank(incumbent):
            best[verdict.tag] = verdict

    return list(best.values())
