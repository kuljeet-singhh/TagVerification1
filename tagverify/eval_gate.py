"""
The gate: does the VLM actually beat SigLIP on this repo's labelled images?

    dooh gate --arm siglip          # the baseline, through the production path
    dooh gate --arm vlm             # whatever SCORER/VLM_PROVIDER say
    dooh gate --limit 20            # a cheap smoke run first
    dooh gate --report              # print the comparison, score nothing new

WHY THIS FILE IS NOT inference/sweep.py
---------------------------------------
`sweep.py` answers the same question for SigLIP and its method is copied here
deliberately (see `cross_fire` and `recall` below -- if the definitions drift, the
152 / 0.665 baseline stops being a baseline). But it lives in the directory that is
git-subtree-pushed to a Space, so it must never touch Postgres, and a VLM arm needs
exactly that: the catalog IS the prompt now. So the gate lives in the web tier and
calls both scorers the way production calls them.

That is also why the SigLIP arm goes through `scoring/client.py` rather than
importing `detector` directly: it measures what production actually serves, not what
a script can reproduce.

THE METRIC, AND WHY IT IS THIS ONE
----------------------------------
NOT per-tag precision/recall. That is the metric `inference/calibrate.py` optimises,
it optimises each tag against that tag's own 24 images -- a set containing no
examples of the other nineteen categories -- and applying its output raised cross-tag
false blocks from 152 to 211 while mean recall FELL. A metric that cannot see its own
worst failure is not the one to gate on.

So the gate is:

  1. CROSS-TAG FALSE BLOCKS. For every image, every tag that is not its owner and
     comes back `present`. Each one is a real creative that a screen blocking that
     tag would refuse, described by a category it does not belong to. Baseline: 152.

  2. MEAN RECALL, second. An uncertain positive is NOT a detection -- it is a request
     for a human -- so it counts against recall exactly as sweep.py counts it.
     Baseline: 0.665.

A LABEL AUDIT FALLS OUT OF IT, AND IS NEEDED
--------------------------------------------
`eval/alcohol/neg/Hamburger.jpg` is filed as a negative and contains a visible glass
of beer. It is the image behind four negative phrases added to `alcohol`, which are
therefore suppressing a true positive. One wrong label in a 24-image set is 4% of
that tag, so the baseline measures the labels as much as the model.

A reading model makes this cheap in a way SigLIP never could: it says WHAT it saw, so
`--disagreements` prints every image whose evidence contradicts its folder and a human
confirms or overturns in seconds. Run that BEFORE trusting either arm's numbers.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tagverify.analyze import intake
from tagverify.analyze.run import _score, _scorer_health
from tagverify.db.session import session_scope
from tagverify.scoring.client import InferenceWarming
from tagverify.tags.decide import cached_thresholds, decide_all

EVAL_DIR = Path("inference/eval")
CACHE = Path("inference/gate_cache.json")
OUT = Path("inference/gate.json")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

#: The figures this has to beat, from SCREEN_CONTENT_POLICY.md 4.3 over 464 images.
#: Reported alongside, never used as a pass/fail constant: the set has drifted to 473
#: and the labels are under audit, so the honest comparison is siglip-now vs vlm-now
#: measured in the same run. These are the historical anchor, not the gate.
BASELINE_FALSE_BLOCKS = 152
BASELINE_MEAN_RECALL = 0.665

#: Gemini answers in 5-9s. Two at a time, not four: the first run at four produced a
#: 503 on every image, and four was never tested against a working quota, so some of
#: that was very likely self-inflicted. Two puts a 473-image arm around 25 minutes,
#: which the resumable cache makes a non-issue. Raise it once a full arm has completed
#: cleanly, not before.
CONCURRENCY = 2
RETRIES = 4


@dataclass
class Scored:
    """One image, scored against every active tag by one arm."""

    path: str
    #: The tag whose eval folder it came from.
    owner: str
    #: True in pos/, False in neg/.
    label: bool
    #: slug -> "present" | "uncertain" | "absent"
    bands: dict[str, str] = field(default_factory=dict)
    #: slug -> what the model said it saw. Empty for SigLIP, which reports a canned
    #: phrase rather than an observation; it is what makes the label audit possible.
    evidence: dict[str, str] = field(default_factory=dict)
    latency_ms: int = 0


def discover(limit: int | None = None) -> list[tuple[str, bool, Path]]:
    """Every eval/<slug>/{pos,neg}/* image, as (owner, label, path). Same as sweep.py."""
    if not EVAL_DIR.is_dir():
        raise SystemExit(f"no {EVAL_DIR}/ — run inference/fetch_demo_eval.py first")

    found: list[tuple[str, bool, Path]] = []
    for tag_dir in sorted(EVAL_DIR.iterdir()):
        if not tag_dir.is_dir():
            continue
        for label, folder in ((True, "pos"), (False, "neg")):
            sub = tag_dir / folder
            if not sub.is_dir():
                continue
            for path in sorted(sub.iterdir()):
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                    found.append((tag_dir.name, label, path))
    return found[:limit] if limit else found


# ------------------------------------------------------------------- scoring


async def _score_one(
    session: Any, owner: str, label: bool, path: Path, tags: list[str]
) -> Scored | None:
    """
    One image against every tag, through the SAME pipeline production uses.

    Deliberately `_score` + `decide_all` rather than `run_analysis`: the result cache
    would serve a stored verdict and the gate would measure the cache. Everything else
    -- the scorer, the prompt, the banding, the tri-state -- is production's.
    """
    built = intake.build(path.read_bytes(), tags)
    health = await _scorer_health(session)
    thresholds = await cached_thresholds(session)

    for attempt in range(RETRIES):
        try:
            started = time.monotonic()
            raw = await _score(built, health)
            latency = int((time.monotonic() - started) * 1000)
            break
        except InferenceWarming:
            # Expected against a busy hosted model, and against a cold Space. Backing
            # off is the whole point of that exception existing.
            await asyncio.sleep(3 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {path.name}: {type(exc).__name__}: {str(exc)[:90]}")
            return None
    else:
        print(f"  skip {path.name}: still busy after {RETRIES} attempts")
        return None

    verdicts = decide_all(raw, thresholds, health.packs_version)
    return Scored(
        path=str(path),
        owner=owner,
        label=label,
        bands={v.tag: v.band for v in verdicts},
        evidence={v.tag: v.evidence.top_phrase for v in verdicts},
        latency_ms=latency,
    )


async def score_arm(
    arm: str, images: list[tuple[str, bool, Path]], cache: dict[str, Any]
) -> list[Scored]:
    """
    Score every image, resuming from the cache.

    RESUMABLE ON PURPOSE. A 473-image VLM arm is ten minutes and real money; losing it
    to one 503 at image 400 is the difference between a measurement that gets made and
    one that gets skipped.
    """
    done = cache.setdefault(arm, {})
    todo = [(o, lab, p) for o, lab, p in images if str(p) not in done]
    print(f"  {arm}: {len(done)} cached, {len(todo)} to score")

    if not todo:
        return [Scored(**v) for v in done.values()]

    semaphore = asyncio.Semaphore(CONCURRENCY)
    counter = {"n": 0}
    started = time.monotonic()

    async def one(owner: str, label: bool, path: Path) -> None:
        async with semaphore, session_scope() as session:
            health = await _scorer_health(session)
            result = await _score_one(session, owner, label, path, list(health.tags))
        counter["n"] += 1
        if result is not None:
            done[str(path)] = result.__dict__
        rate = (time.monotonic() - started) / counter["n"]
        left = rate * (len(todo) - counter["n"]) / 60
        print(f"\r  {arm}: {counter['n']}/{len(todo)} ({rate:.1f}s/img, ~{left:.1f} min left)",
              end="", flush=True)

    for chunk in range(0, len(todo), 40):
        await asyncio.gather(*(one(o, lab, p) for o, lab, p in todo[chunk:chunk + 40]))
        CACHE.write_text(json.dumps(cache))  # checkpoint every 40
    print()
    return [Scored(**v) for v in done.values()]


# ------------------------------------------------------------------- metrics
#
# Copied from inference/sweep.py, definitions included. If these drift, the historical
# figures stop describing the same quantity and the comparison quietly becomes a
# different one.


def cross_fire(scored: list[Scored], slugs: list[str]) -> tuple[int, dict[str, int]]:
    """
    Every (image, tag) where the tag is NOT the image's owner and fired `present`.

    The blast radius of blocking a tag: each one is a creative refused on a screen
    blocking it, described by a category it does not belong to.
    """
    per_tag: dict[str, int] = defaultdict(int)
    total = 0
    for item in scored:
        for slug in slugs:
            if slug == item.owner:
                continue
            if item.bands.get(slug) == "present":
                per_tag[slug] += 1
                total += 1
    return total, dict(per_tag)


def recall_of(scored: list[Scored], slug: str) -> tuple[float, int, int]:
    """
    tp / positives for one tag, on its own folder.

    An UNCERTAIN positive is not a detection — it is a request for a human — so it
    counts against recall. sweep.py counts it the same way.
    """
    mine = [s for s in scored if s.owner == slug and s.label]
    if not mine:
        return 0.0, 0, 0
    tp = sum(1 for s in mine if s.bands.get(slug) == "present")
    unsure = sum(1 for s in mine if s.bands.get(slug) == "uncertain")
    return tp / len(mine), tp, unsure


def disagreements(scored: list[Scored]) -> list[dict[str, str]]:
    """
    Images whose own-tag verdict contradicts the folder they are filed in.

    The label audit. `alcohol/neg/Hamburger.jpg` is the known case: filed negative,
    contains a visible glass of beer. Reported with the evidence sentence so a human
    can settle it against the picture in seconds — which is only possible because a
    reading model says what it saw.
    """
    out = []
    for item in scored:
        band = item.bands.get(item.owner)
        if item.label and band == "absent":
            verdict = "labelled POSITIVE, model says absent"
        elif not item.label and band == "present":
            verdict = "labelled NEGATIVE, model says present"
        else:
            continue
        out.append({
            "path": item.path,
            "tag": item.owner,
            "problem": verdict,
            "evidence": item.evidence.get(item.owner, ""),
        })
    return out


def report(arm: str, scored: list[Scored], slugs: list[str]) -> dict[str, Any]:
    """The two gate numbers, plus what is needed to act on them."""
    total, per_tag = cross_fire(scored, slugs)

    # Only tags that actually have positives in the eval set have a recall. Four do not
    # (coffee_shop, saloon, dog, cat), and averaging a 0.0 for them would report a
    # catalog problem as a model problem.
    owners_with_positives = {s.owner for s in scored if s.label}
    recalls = {slug: recall_of(scored, slug) for slug in slugs if slug in owners_with_positives}

    n_positives = sum(1 for s in scored if s.label)
    n_uncertain = sum(1 for _r, _tp, u in recalls.values() for _ in [0]) and sum(
        u for _r, _tp, u in recalls.values()
    )
    latencies = sorted(s.latency_ms for s in scored if s.latency_ms)

    def pct(xs: list[int], q: float) -> int:
        return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0

    return {
        "arm": arm,
        "images": len(scored),
        "tags": len(slugs),
        # THE GATE.
        "cross_tag_false_blocks": total,
        "mean_recall": round(
            statistics.fmean(r for r, _t, _u in recalls.values()), 4
        ) if recalls else 0.0,
        # What to act on when a number is bad.
        "cross_tag_by_tag": dict(sorted(per_tag.items(), key=lambda kv: -kv[1])),
        "recall_by_tag": {k: round(v[0], 3) for k, v in sorted(recalls.items())},
        "tags_without_positives": sorted(set(slugs) - owners_with_positives),
        # ~40% under SigLIP. Every one of these lands in a review queue.
        "uncertain_positive_rate": round(n_uncertain / n_positives, 3) if n_positives else 0.0,
        "latency_ms": {
            "p50": pct(latencies, 0.50),
            "p95": pct(latencies, 0.95),
            "max": latencies[-1] if latencies else 0,
        },
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> str:
    """The verdict, in the terms the plan set."""
    fb_b, fb_a = before["cross_tag_false_blocks"], after["cross_tag_false_blocks"]
    rc_b, rc_a = before["mean_recall"], after["mean_recall"]
    lines = [
        f"{'':22}{before['arm']:>12}{after['arm']:>12}{'change':>12}",
        "-" * 58,
        f"{'cross-tag false blocks':22}{fb_b:>12}{fb_a:>12}{fb_a - fb_b:>+12}",
        f"{'mean recall':22}{rc_b:>12.3f}{rc_a:>12.3f}{rc_a - rc_b:>+12.3f}",
        f"{'uncertain positives':22}{before['uncertain_positive_rate']:>12.3f}"
        f"{after['uncertain_positive_rate']:>12.3f}"
        f"{after['uncertain_positive_rate'] - before['uncertain_positive_rate']:>+12.3f}",
        f"{'p95 latency (ms)':22}{before['latency_ms']['p95']:>12}"
        f"{after['latency_ms']['p95']:>12}"
        f"{after['latency_ms']['p95'] - before['latency_ms']['p95']:>+12}",
        "",
    ]
    # Both axes, because improving one by wrecking the other is exactly what applying
    # all 20 calibrations did: 152 -> 211 false blocks while mean recall FELL.
    if fb_a < fb_b and rc_a >= rc_b:
        lines.append("PASS — fewer wrong blocks AND recall held or improved.")
    elif fb_a < fb_b:
        lines.append("MIXED — fewer wrong blocks, but recall fell. Weigh it deliberately.")
    elif rc_a > rc_b:
        lines.append("MIXED — better recall bought with MORE wrong blocks. That is the trade")
        lines.append("        calibration already made, and it was the wrong one.")
    else:
        lines.append("FAIL — no better on either axis. This is the honest place to stop.")
    return "\n".join(lines)
