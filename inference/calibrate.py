"""
Threshold calibration.

    ./.venv/bin/python calibrate.py                 # calibrate every tag with images
    ./.venv/bin/python calibrate.py alcohol gym_fitness
    ./.venv/bin/python calibrate.py --target-precision 0.95

Turns the guessed thresholds in packs.json into MEASURED ones, by scoring labelled
images and picking, per tag, the cutoffs that hit a target precision while
detecting as much as possible.

--------------------------------------------------------------------------
WHERE THE IMAGES GO
--------------------------------------------------------------------------
    inference/eval/<tag-slug>/pos/    images that DO contain the tag
    inference/eval/<tag-slug>/neg/    images that do NOT

e.g.

    inference/eval/alcohol/pos/beer-billboard.jpg
    inference/eval/alcohol/neg/mango-juice-ad.jpg     <- a NEAR MISS

The `neg` folder is the one that decides whether this works. Filling it with
random unrelated photos measures nothing: the model already scores a mountain at
0.03 for alcohol. Fill it with the things that actually get confused — juice and
soft drinks for `alcohol`, baby formula for `protein_supplements`, a yoga studio
for `gym_fitness`. Half your negatives should be images a careless human might
mislabel.

--------------------------------------------------------------------------
WHY THIS WRITES JSON INSTEAD OF THE DATABASE
--------------------------------------------------------------------------
This script runs on your machine and emits `calibration.json`. A separate
`dooh apply-calibration` loads that into Postgres.

That split is deliberate. This file lives in the directory that gets deployed to
a Hugging Face Space, and giving the Space a DATABASE_URL would put production
database credentials into an environment whose only job is running a model. The
JSON is also a reviewable artifact — you can read what changed before it takes
effect on live verdicts.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from detector import Detector

# The banding rule, shared with detector.py and the API tier. Calibration must
# evaluate candidate thresholds with the exact function production serves with.
try:
    from .banding import band_of  # packaged
except ImportError:  # pragma: no cover - the normal path for this CLI script
    from banding import band_of  # flat

EVAL_DIR = Path("eval")
OUT_FILE = Path("calibration.json")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# Below this many labelled examples per side, a precision figure is not a
# measurement — 3 correct guesses out of 3 is not 100% precision. Tags under the
# floor still get scored and reported, but are NOT marked calibrated.
MIN_PER_CLASS = 8

# Default ceiling on the uncertain band's width, overridable with --max-escalation.
#
# This is a budget dial, not a correctness one. Escalation spends free-tier
# vision-LLM calls, so it cannot be the default path — but the band is also what
# prevents borderline positives being confidently called "absent", so squeezing it
# to zero trades a visible cost for an invisible compliance risk. Raise it if
# false negatives matter more to you than neuron budget.
MAX_ESCALATION_RATE = 0.35


# --------------------------------------------------------------------- stats --

def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    """
    Lower bound of the 95% confidence interval for a proportion.

    This is REPORTED, not used for selection. Raw precision on a small sample
    reads confidently: 5/5 correct is a point estimate of 100%, but its lower
    bound is only ~57%. The bound is the honest number to quote to a client.

    It is not a gate, because it cannot be met at realistic sample sizes — a
    flawless classifier on 10 positives bounds at 72%, and clearing 90% takes
    roughly 35 consecutive correct calls. Gating on it made every tag fail and
    misattributed a sample-size limit to the prompt pack.
    """
    if total == 0:
        return 0.0
    p = successes / total
    denominator = 1 + z**2 / total
    centre = p + z**2 / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))
    return max(0.0, (centre - margin) / denominator)


# -------------------------------------------------------------------- types --

@dataclass
class Scored:
    """One eval image's raw numbers for one tag."""

    path: str
    label: bool
    score: float
    sigmoid: float


@dataclass
class Outcome:
    low: float
    high: float
    floor: float
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    uncertain: int = 0
    uncertain_pos: int = 0
    uncertain_neg: int = 0

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn + self.uncertain

    @property
    def positives(self) -> int:
        return self.tp + self.fn + self.uncertain_pos

    @property
    def precision(self) -> float:
        decided = self.tp + self.fp
        return self.tp / decided if decided else 0.0

    @property
    def precision_lower(self) -> float:
        return wilson_lower_bound(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float:
        # An uncertain positive is NOT a detection — it is a request for a human
        # or a vision LLM. Counting it as recall would flatter the numbers.
        return self.tp / self.positives if self.positives else 0.0

    @property
    def escalation_rate(self) -> float:
        return self.uncertain / self.total if self.total else 0.0


@dataclass
class TagReport:
    slug: str
    n_pos: int
    n_neg: int
    chosen: Outcome | None
    calibrated: bool
    reason: str
    scored: list[Scored] = field(default_factory=list)


# ------------------------------------------------------------------ scoring --

def discover(slugs: list[str] | None) -> dict[str, dict[bool, list[Path]]]:
    """Find eval/<slug>/{pos,neg}/* images."""
    if not EVAL_DIR.is_dir():
        return {}

    found: dict[str, dict[bool, list[Path]]] = {}
    for tag_dir in sorted(EVAL_DIR.iterdir()):
        if not tag_dir.is_dir():
            continue
        if slugs and tag_dir.name not in slugs:
            continue

        buckets: dict[bool, list[Path]] = {True: [], False: []}
        for label, folder in ((True, "pos"), (False, "neg")):
            sub = tag_dir / folder
            if sub.is_dir():
                buckets[label] = sorted(
                    p for p in sub.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
                )
        if buckets[True] or buckets[False]:
            found[tag_dir.name] = buckets
    return found


def score_images(detector: Detector, slug: str, buckets: dict[bool, list[Path]]) -> list[Scored]:
    scored: list[Scored] = []
    total = len(buckets[True]) + len(buckets[False])
    done = 0

    for label, paths in ((True, buckets[True]), (False, buckets[False])):
        for path in paths:
            done += 1
            try:
                with Image.open(path) as raw:
                    image = raw.convert("RGB")
                    # Match what the production client uploads, so calibrated
                    # thresholds describe the same pixels production will see.
                    image.thumbnail((768, 768))
                    verdict = detector.analyze(image, [slug])[0]
            except Exception as exc:
                print(f"    skip {path.name}: {exc}")
                continue

            scored.append(
                Scored(
                    path=str(path),
                    label=label,
                    score=verdict.score,
                    sigmoid=verdict.sigmoid,
                )
            )
            print(
                f"\r    scoring {slug}: {done}/{total}",
                end="",
                flush=True,
            )
    print()
    return scored


# ------------------------------------------------------------------ sweeping --

def evaluate(scored: list[Scored], low: float, high: float, floor: float) -> Outcome:
    out = Outcome(low=low, high=high, floor=floor)
    for item in scored:
        # The SAME function production serves with (banding.py), not a copy of it.
        # Calibrating against different logic than we serve would make the measured
        # numbers describe nothing.
        _band, predicted, _decided_by = band_of(item.score, item.sigmoid, low, high, floor)

        if predicted is None:
            out.uncertain += 1
            if item.label:
                out.uncertain_pos += 1
            else:
                out.uncertain_neg += 1
        elif predicted and item.label:
            out.tp += 1
        elif predicted and not item.label:
            out.fp += 1
        elif not predicted and item.label:
            out.fn += 1
        else:
            out.tn += 1
    return out


def pick_floor(scored: list[Scored]) -> float:
    """
    Choose the sigmoid veto.

    Measured true positives have sigmoids as low as 0.028, while true negatives
    sit at 0.000 — so this must be a low backstop, not a gate. We take half the
    smallest sigmoid among genuine positives, so no positive in the eval set can
    ever be vetoed, then clamp into a sane range.
    """
    positive_sigmoids = [s.sigmoid for s in scored if s.label]
    if not positive_sigmoids:
        return 0.005
    return min(0.05, max(0.001, min(positive_sigmoids) * 0.5))


def calibrate_tag(
    slug: str, scored: list[Scored], target: float, max_escalation: float
) -> TagReport:
    n_pos = sum(1 for s in scored if s.label)
    n_neg = len(scored) - n_pos

    report = TagReport(
        slug=slug, n_pos=n_pos, n_neg=n_neg, chosen=None, calibrated=False, reason="", scored=scored
    )

    if n_pos == 0 or n_neg == 0:
        report.reason = "needs both pos/ and neg/ images"
        return report

    floor = pick_floor(scored)

    # Candidate cutoffs: every observed score, nudged so a score exactly equal to
    # an observation falls on the intended side, plus the extremes.
    observed = sorted({s.score for s in scored})
    candidates = sorted({0.0, 1.0, *observed, *(v + 1e-6 for v in observed)})

    # Selection is judged on the MEASURED precision, not its confidence bound.
    #
    # An earlier version required the 95% lower bound to clear the target, which
    # is unreachable on a realistic eval set: a flawless classifier on 10
    # positives has a lower bound of 72%, and clearing 90% needs ~35 consecutive
    # correct calls. That made every tag "fail" and — worse — blamed the prompt
    # pack for what was really a sample-size limit. The bound is still computed
    # and reported, because it is the number worth quoting to a client; it just
    # is not a gate.
    feasible: list[Outcome] = []
    for high in candidates:
        for low in candidates:
            if low > high:
                continue
            outcome = evaluate(scored, low, high, floor)
            if outcome.tp == 0:
                continue  # detects nothing; useless regardless of precision
            if outcome.precision < target:
                continue
            if outcome.escalation_rate > max_escalation:
                continue
            feasible.append(outcome)

    if not feasible:
        # Name the images standing in the way. "Precision is too low" sends
        # someone hunting; "Hamburger.jpg scored 0.94" tells them exactly which
        # hard negative the pack is missing.
        best = max(
            (evaluate(scored, 0.0, h, floor) for h in candidates),
            key=lambda o: (o.precision, o.recall),
            default=None,
        )
        blockers = sorted(
            (s for s in scored if not s.label),
            key=lambda s: -s.score,
        )[:3]
        blocker_text = ", ".join(
            f"{Path(s.path).name} ({s.score:.3f})" for s in blockers
        )
        report.reason = (
            f"cannot reach {target:.0%} precision at any threshold "
            f"(best {best.precision:.0%} with {best.recall:.0%} recall). "
            f"Highest-scoring negatives: {blocker_text}. "
            f"Add hard negatives to the pack describing those, then re-run."
        )
        return report

    # Objective, in priority order:
    #   1. detect as much as possible (recall)
    #   2. then MINIMISE FALSE NEGATIVES
    #   3. then minimise escalation
    #
    # Step 2 is the one that matters and is easy to get wrong. A positive we fail
    # to detect can either be called "absent" (a false negative) or "uncertain"
    # (escalated to a vision LLM or a human). Those are not equivalent: this is a
    # compliance tool, and an alcohol creative confidently marked clean is the
    # failure that puts an illegal ad on a screen. An earlier version tie-broke on
    # low escalation, which collapsed the uncertain band to zero width and turned
    # every borderline positive into a confident "absent" — precision looked
    # identical and the product was materially worse.
    #
    # So: prefer to admit uncertainty over asserting absence, bounded by the
    # escalation cap.
    best = max(
        feasible,
        key=lambda o: (round(o.recall, 4), -o.fn, -round(o.escalation_rate, 4)),
    )
    report.chosen = best

    if n_pos < MIN_PER_CLASS or n_neg < MIN_PER_CLASS:
        report.reason = (
            f"scored, but not marked calibrated: needs >={MIN_PER_CLASS} images per side "
            f"(have {n_pos} pos / {n_neg} neg)"
        )
        return report

    report.calibrated = True
    report.reason = "calibrated"
    return report


# -------------------------------------------------------------------- output --

def print_report(reports: list[TagReport], target: float) -> None:
    print(f"\n{'tag':<22} {'pos':>4} {'neg':>4} {'low':>6} {'high':>6} {'floor':>6} "
          f"{'prec':>6} {'lo95':>6} {'recall':>7} {'esc':>6}  status")
    print("-" * 108)

    for r in sorted(reports, key=lambda x: x.slug):
        if r.chosen:
            c = r.chosen
            print(
                f"{r.slug:<22} {r.n_pos:>4} {r.n_neg:>4} {c.low:>6.3f} {c.high:>6.3f} "
                f"{c.floor:>6.3f} {c.precision:>6.1%} {c.precision_lower:>6.1%} "
                f"{c.recall:>7.1%} {c.escalation_rate:>6.1%}  "
                f"{'OK' if r.calibrated else 'not calibrated'}"
            )
            if not r.calibrated:
                print(f"{'':22} -> {r.reason}")
        else:
            print(f"{r.slug:<22} {r.n_pos:>4} {r.n_neg:>4} {'':>6} {'':>6} {'':>6} "
                  f"{'':>6} {'':>6} {'':>7} {'':>6}  FAILED")
            print(f"{'':22} -> {r.reason}")

    print("-" * 108)
    ok = [r for r in reports if r.calibrated]
    print(f"{len(ok)}/{len(reports)} tags calibrated at >={target:.0%} measured precision")
    print(
        "\nprec = measured precision at the chosen thresholds, and what selection is\n"
        "judged on. lo95 = the 95% confidence LOWER bound — the honest number to quote,\n"
        "since it accounts for sample size. recall counts uncertain positives as misses,\n"
        "because an uncertain verdict is not a detection. esc = share of images landing\n"
        "in the uncertain band, i.e. what gets escalated to a vision LLM."
    )

    # Sample size is usually the binding constraint on what can be CLAIMED, and it
    # is invisible unless spelled out. Someone reading "100% precision" off 10
    # images will quote it to a client; the bound is what stops that.
    thin = [r for r in reports if r.chosen and r.chosen.precision_lower < target]
    if thin:
        print(
            f"\nNote: {len(thin)} tag(s) hit {target:.0%} measured precision but have a lower\n"
            f"bound below it — the sample is too small to claim that number confidently.\n"
            f"Perfect precision needs roughly 35 positives to support a 90% lower bound\n"
            f"(10 gives 72%, 20 gives 84%). More labelled images is the only fix; the\n"
            f"thresholds themselves are already the best the data supports."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate per-tag decision thresholds.")
    parser.add_argument("slugs", nargs="*", help="tags to calibrate (default: all with images)")
    parser.add_argument("--target-precision", type=float, default=0.90)
    parser.add_argument(
        "--max-escalation",
        type=float,
        default=MAX_ESCALATION_RATE,
        help="max share of images allowed to land in the uncertain band "
        "(default %(default)s). Raise it to trade vision-LLM budget for fewer "
        "false negatives.",
    )
    parser.add_argument("--out", default=str(OUT_FILE))
    args = parser.parse_args()

    if not 0 < args.target_precision < 1:
        print("--target-precision must be between 0 and 1")
        return 2

    found = discover(args.slugs or None)
    if not found:
        print(
            f"No labelled images found.\n\n"
            f"Create folders like:\n"
            f"    {EVAL_DIR}/alcohol/pos/     images that DO contain alcohol\n"
            f"    {EVAL_DIR}/alcohol/neg/     images that do NOT (near-misses!)\n\n"
            f"Aim for {MIN_PER_CLASS}-20 per side. The negatives matter most: use the\n"
            f"things that actually get confused (juice bottles for alcohol, baby\n"
            f"formula for protein powder), not random unrelated photos."
        )
        return 1

    detector = Detector("packs.json")

    unknown = [slug for slug in found if slug not in detector.packs]
    if unknown:
        print(f"\nno such tag(s) in packs.json: {', '.join(unknown)}")
        print(f"known: {', '.join(sorted(detector.packs))}")
        return 2

    print(f"\ncalibrating {len(found)} tag(s) at >={args.target_precision:.0%} precision\n")
    started = time.perf_counter()

    reports: list[TagReport] = []
    for slug, buckets in found.items():
        scored = score_images(detector, slug, buckets)
        reports.append(
            calibrate_tag(slug, scored, args.target_precision, args.max_escalation)
        )

    print_report(reports, args.target_precision)
    print(f"\nscored in {time.perf_counter() - started:.1f}s")

    payload = {
        "packs_version": detector.packs_version,
        "model": detector.model_id,
        "target_precision": args.target_precision,
        "min_per_class": MIN_PER_CLASS,
        "tags": [
            {
                "slug": r.slug,
                "calibrated": r.calibrated,
                "reason": r.reason,
                "n_pos": r.n_pos,
                "n_neg": r.n_neg,
                **(
                    {
                        "threshold_low": round(r.chosen.low, 4),
                        "threshold_high": round(r.chosen.high, 4),
                        "sigmoid_floor": round(r.chosen.floor, 4),
                        "precision": round(r.chosen.precision, 4),
                        "precision_lower": round(r.chosen.precision_lower, 4),
                        "recall": round(r.chosen.recall, 4),
                        "escalation_rate": round(r.chosen.escalation_rate, 4),
                        "confusion": {
                            "tp": r.chosen.tp,
                            "fp": r.chosen.fp,
                            "fn": r.chosen.fn,
                            "tn": r.chosen.tn,
                            "uncertain": r.chosen.uncertain,
                        },
                    }
                    if r.chosen
                    else {}
                ),
                # Kept so a misclassification can be traced to the exact file,
                # and so eval_images can record what was labelled.
                "images": [
                    {
                        "path": s.path,
                        "label": s.label,
                        "score": round(s.score, 4),
                        "sigmoid": round(s.sigmoid, 4),
                    }
                    for s in r.scored
                ],
            }
            for r in reports
        ],
    }

    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {args.out}")
    print("\nApply it to the database with:\n    dooh apply-calibration")

    failed = [r for r in reports if not r.calibrated]
    if failed:
        print(f"\n{len(failed)} tag(s) not calibrated — see the status column above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
