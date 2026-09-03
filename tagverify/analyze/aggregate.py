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

2. `score` is not a likelihood of presence. It is the model's confidence in its OWN answer,
   so a frame can be confidently ABSENT at 0.95 and confidently present at 0.70. Ranking by
   score alone would hand the video to the absent frame and clear a creative nobody cleared —
   the same failure as (1), arrived at from the other direction.

   Under SigLIP the second reason was a per-frame veto: a frame could score 0.92 and still be
   absent because it resembled nothing at all ("a photo of a mountain reads as alcohol"). The
   veto is gone with the ranking model, but the conclusion it forced is unchanged, and that is
   the point of writing it down — band precedence survives the reason it was introduced for.

So: band first, score only as the tie-break inside a band.

WHY THE EVIDENCE IS CARRIED WHOLE
---------------------------------
The winning frame's score, phrase and evidence all travel together, never mixed with another
frame's. A verdict assembled from the highest score of one frame and the best phrase of
another describes no frame that exists, and would point a reviewer at the wrong second of the
video. (`crop` and `sigmoid` ride along as None: this scorer fills neither, but they are still
in the response contract and stored rows carry values.)

WHY THIS IS KEPT PURE
---------------------
Floats and dataclasses in, a verdict out — no I/O, no session, no scorer. It is the one place
a video's verdict is decided, so it is the one place worth testing exhaustively, and
tests/test_aggregate.py does. If you change how frames combine, that file should fail.
"""

from __future__ import annotations

from tagverify.tags.decide import Verdict

#: Higher wins. Mirrors the Band literal in tags/decide.py.
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
