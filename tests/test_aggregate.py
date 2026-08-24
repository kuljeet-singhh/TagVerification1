"""
Collapsing per-frame verdicts into one per tag.

This is to the video path what tests/test_banding.py is to the decision rule: if you change
how frames combine, this file should fail. If it doesn't, the test is wrong.

The cases that matter are the two that produce a confident WRONG answer rather than an error —
an uncertain frame being buried by absent ones, and a sigmoid-floor-vetoed frame winning on
raw score. Both are silent in production, which is why they are pinned here.
"""

from __future__ import annotations

from dooh.analyze.aggregate import aggregate
from dooh.tags.decide import Evidence, FrameRef, Verdict


def verdict(
    tag: str,
    band: str,
    score: float,
    *,
    frame: int = 0,
    timestamp_s: float = 0.0,
    sigmoid: float = 0.5,
) -> Verdict:
    present = {"present": True, "absent": False, "uncertain": None}[band]
    return Verdict(
        tag=tag,
        present=present,
        score=score,
        confidence="high",
        decided_by="siglip",
        calibrated=True,
        band=band,
        evidence=Evidence(
            top_phrase=f"phrase for frame {frame}",
            crop=[0.0, 0.0, 1.0, 1.0],
            sigmoid=sigmoid,
            frame=FrameRef(frame, timestamp_s),
        ),
    )


# ------------------------------------------------------------------ precedence


def test_one_present_frame_makes_the_video_present() -> None:
    """A bottle on screen for one second of ten is still a bottle on screen."""
    result = aggregate(
        [
            verdict("alcohol", "absent", 0.10, frame=0),
            verdict("alcohol", "present", 0.80, frame=1, timestamp_s=1.0),
            verdict("alcohol", "absent", 0.12, frame=2),
        ]
    )
    assert len(result) == 1
    assert result[0].present is True
    assert result[0].evidence.frame.timestamp_s == 1.0


def test_uncertain_beats_absent_even_when_absent_scores_higher() -> None:
    """
    The one that a plain max(score) gets wrong.

    Five absent frames and one uncertain frame is a video a human still has to look at.
    Reporting it absent is "we didn't check" wearing the costume of "we checked and it's
    clean" — and note the absent frame here scores HIGHER, so score alone picks the wrong one.
    """
    result = aggregate(
        [
            verdict("alcohol", "absent", 0.29, frame=0),
            verdict("alcohol", "uncertain", 0.31, frame=1, timestamp_s=2.0),
            verdict("alcohol", "absent", 0.40, frame=2),
        ]
    )
    assert result[0].present is None
    assert result[0].band == "uncertain"
    assert result[0].evidence.frame.index == 1


def test_present_beats_uncertain() -> None:
    result = aggregate(
        [
            verdict("alcohol", "uncertain", 0.95, frame=0),
            verdict("alcohol", "present", 0.56, frame=1),
        ]
    )
    assert result[0].present is True
    assert result[0].evidence.frame.index == 1


def test_all_absent_stays_absent() -> None:
    result = aggregate(
        [verdict("alcohol", "absent", 0.05, frame=i) for i in range(4)]
    )
    assert result[0].present is False


def test_absent_reports_the_closest_call() -> None:
    """The highest-scoring absent frame is the useful evidence — the one that nearly fired."""
    result = aggregate(
        [
            verdict("alcohol", "absent", 0.05, frame=0),
            verdict("alcohol", "absent", 0.28, frame=1, timestamp_s=3.0),
            verdict("alcohol", "absent", 0.11, frame=2),
        ]
    )
    assert result[0].evidence.frame.index == 1


# ------------------------------------------------------------- the sigmoid veto


def test_a_vetoed_high_score_frame_never_wins() -> None:
    """
    The second one a plain max(score) gets wrong, and the more dangerous of the two.

    A frame can score 0.92 and still be absent because the sigmoid floor vetoed it — it
    resembles nothing at all, the "photo of a mountain reads as alcohol" case. Ranking frames
    by raw score would let exactly those frames win, reintroducing that bug one level up where
    the veto can no longer see it.
    """
    vetoed = verdict("alcohol", "absent", 0.92, frame=0, sigmoid=0.001)
    vetoed.decided_by = "sigmoid_floor"

    result = aggregate([vetoed, verdict("alcohol", "present", 0.60, frame=1, timestamp_s=4.0)])

    assert result[0].present is True
    assert result[0].score == 0.60
    assert result[0].evidence.frame.timestamp_s == 4.0


# ------------------------------------------------------------------- mechanics


def test_evidence_is_carried_whole_from_one_frame() -> None:
    """
    Never assemble a verdict from the best score of one frame and the best sigmoid of another:
    it would describe no frame that exists, and its crop would point at the wrong place.
    """
    winner = verdict("alcohol", "present", 0.70, frame=2, timestamp_s=5.5, sigmoid=0.44)
    winner.evidence.crop = [0.25, 0.25, 0.75, 0.75]

    result = aggregate([verdict("alcohol", "absent", 0.1, frame=0, sigmoid=0.99), winner])

    assert result[0].score == 0.70
    assert result[0].evidence.sigmoid == 0.44
    assert result[0].evidence.crop == [0.25, 0.25, 0.75, 0.75]
    assert result[0].evidence.top_phrase == "phrase for frame 2"


def test_tags_are_collapsed_independently() -> None:
    result = aggregate(
        [
            verdict("alcohol", "absent", 0.10, frame=0),
            verdict("gambling", "present", 0.90, frame=0),
            verdict("alcohol", "present", 0.70, frame=1),
            verdict("gambling", "absent", 0.05, frame=1),
        ]
    )
    by_tag = {v.tag: v for v in result}
    assert by_tag["alcohol"].present is True
    assert by_tag["gambling"].present is True


def test_tag_order_follows_first_appearance() -> None:
    """The response order must still follow the order the caller asked for."""
    result = aggregate(
        [
            verdict("vaping", "absent", 0.1, frame=0),
            verdict("alcohol", "absent", 0.1, frame=0),
            verdict("vaping", "present", 0.9, frame=1),
            verdict("alcohol", "present", 0.9, frame=1),
        ]
    )
    assert [v.tag for v in result] == ["vaping", "alcohol"]


def test_a_single_frame_is_returned_unchanged() -> None:
    """
    The still-image path must be untouched by this module as a matter of construction, not of
    intent — one frame in, that exact verdict out.
    """
    only = verdict("alcohol", "present", 0.81)
    only.evidence.frame = None

    result = aggregate([only])

    assert result == [only]
    assert result[0].evidence.frame is None
