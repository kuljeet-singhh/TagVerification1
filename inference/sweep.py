"""
Score every eval image against EVERY tag, not just its own.

    ./.venv/bin/python sweep.py                          # provisional thresholds
    ./.venv/bin/python sweep.py --thresholds calibration.json
    ./.venv/bin/python sweep.py --tags alcohol gambling  # report on a subset

--------------------------------------------------------------------------
WHY THIS EXISTS ALONGSIDE calibrate.py
--------------------------------------------------------------------------
calibrate.py asks one question per tag: with these labelled images, what cutoffs
give 90% precision? It scores `eval/alcohol/*` against `alcohol` and nothing else.

That leaves the more expensive question unasked. Since 2026-08-14 every `present`
verdict blocks, calibrated or not — so a creative is refused if ANY tag the screen
blocks fires on it, and nothing has ever measured what the other nineteen tags do
to a beer photo. The one number on record is alarming: the same beer that alcohol
scored 0.977 also came back 0.993 for `restaurant_dining` and 0.868 for
`sugary_drinks`. A screen blocking either refuses that ad, and the advertiser is
told their beer poster contains a restaurant.

This sweep scores each image against all 20 tags in one forward pass and reports
that cross-tag firing. It costs nothing extra: detector.analyze() already computes
logits for every prompt in the catalog, so asking for 20 verdicts instead of 1 is
a column slice, not a second pass.

--------------------------------------------------------------------------
WHAT IT DOES NOT DO
--------------------------------------------------------------------------
It never touches Postgres. This file sits in the directory that gets deployed to a
Hugging Face Space, and giving the Space a DATABASE_URL would put production
database credentials in an environment whose only job is running a model — the same
reason calibrate.py writes JSON instead of rows. Pass `--thresholds calibration.json`
to report against the cutoffs that `dooh apply-calibration` will install; without it
the provisional values in packs.json are used and the report says so.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from detector import Detector
from PIL import Image

# The banding rule, shared with detector.py and the API tier. A sweep that judged
# with its own copy of the rule would measure something production does not serve.
try:
    from .banding import band_of  # packaged
except ImportError:  # pragma: no cover - the normal path for this CLI script
    from banding import band_of  # flat

EVAL_DIR = Path("eval")
PACKS = Path("packs.json")
OUT_FILE = Path("sweep.json")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


@dataclass
class Thresholds:
    low: float
    high: float
    floor: float
    calibrated: bool = False


@dataclass
class Scored:
    """One image scored against every tag."""

    path: str
    #: The tag whose eval folder this image came from.
    owner: str
    #: True if it is a positive for its owner, False if a hard negative.
    label: bool
    #: slug -> (score, sigmoid)
    raw: dict[str, tuple[float, float]] = field(default_factory=dict)


def load_thresholds(detector: Detector, report_path: Path | None) -> dict[str, Thresholds]:
    """
    Cutoffs to report against: a calibration report if given, else packs.json.

    A tag missing from the report keeps its provisional values rather than being
    dropped — the report is what HAS been measured, not the list of tags that exist.
    """
    thresholds = {
        slug: Thresholds(*detector.thresholds(slug), calibrated=False)
        for slug in detector.packs
    }

    if not report_path:
        return thresholds

    report = json.loads(report_path.read_text())
    if report.get("packs_version") != detector.packs_version:
        print(
            f"WARNING: {report_path} was measured against packs_version "
            f"{report.get('packs_version')}, but this detector is "
            f"{detector.packs_version}.\n         Its cutoffs describe scores the model no "
            f"longer produces. Re-run calibrate.py.\n",
            file=sys.stderr,
        )

    for tag in report.get("tags", []):
        if not tag.get("calibrated"):
            continue
        thresholds[tag["slug"]] = Thresholds(
            low=tag["threshold_low"],
            high=tag["threshold_high"],
            floor=tag["sigmoid_floor"],
            calibrated=True,
        )
    return thresholds


def discover() -> list[tuple[str, bool, Path]]:
    """Every eval/<slug>/{pos,neg}/* image, as (owner slug, label, path)."""
    if not EVAL_DIR.is_dir():
        raise SystemExit(f"no {EVAL_DIR}/ — run fetch_demo_eval.py first")

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
    return found


def score_all(detector: Detector, images: list[tuple[str, bool, Path]]) -> list[Scored]:
    """One forward pass per image, every tag read off it."""
    slugs = sorted(detector.packs)
    scored: list[Scored] = []
    started = time.monotonic()

    for index, (owner, label, path) in enumerate(images, 1):
        try:
            with Image.open(path) as raw:
                image = raw.convert("RGB")
                # Match what the production client uploads, so the numbers describe
                # the same pixels production will see.
                image.thumbnail((768, 768))
                verdicts = detector.analyze(image, slugs)
        except Exception as exc:
            print(f"\n    skip {path}: {exc}")
            continue

        scored.append(
            Scored(
                path=str(path),
                owner=owner,
                label=label,
                raw={v.tag: (v.score, v.sigmoid) for v in verdicts},
            )
        )
        rate = (time.monotonic() - started) / index
        print(
            f"\r  scoring {index}/{len(images)} "
            f"({rate:.2f}s/image, ~{rate * (len(images) - index) / 60:.1f} min left)",
            end="",
            flush=True,
        )

    print()
    return scored


def verdict_of(scored: Scored, slug: str, thresholds: dict[str, Thresholds]) -> str:
    """"present" | "uncertain" | "absent" for one image against one tag."""
    score, sigmoid = scored.raw[slug]
    t = thresholds[slug]
    band, _present, _decided_by = band_of(score, sigmoid, t.low, t.high, t.floor)
    return band


def own_tag_report(
    scored: list[Scored], slug: str, thresholds: dict[str, Thresholds]
) -> dict[str, float | int]:
    """How the tag does on its own labelled set — the calibrate.py question."""
    mine = [s for s in scored if s.owner == slug]
    counts = {"tp": 0, "fn": 0, "uncertain_pos": 0, "fp": 0, "tn": 0, "uncertain_neg": 0}

    for item in mine:
        band = verdict_of(item, slug, thresholds)
        if item.label:
            key = {"present": "tp", "absent": "fn", "uncertain": "uncertain_pos"}[band]
        else:
            key = {"present": "fp", "absent": "tn", "uncertain": "uncertain_neg"}[band]
        counts[key] += 1

    decided = counts["tp"] + counts["fp"]
    positives = counts["tp"] + counts["fn"] + counts["uncertain_pos"]
    return {
        **counts,
        "n_pos": positives,
        "n_neg": counts["fp"] + counts["tn"] + counts["uncertain_neg"],
        # An uncertain positive is NOT a detection — it is a request for a human.
        "precision": counts["tp"] / decided if decided else 0.0,
        "recall": counts["tp"] / positives if positives else 0.0,
    }


def cross_fire(
    scored: list[Scored], slugs: list[str], thresholds: dict[str, Thresholds]
) -> dict[str, dict[str, int]]:
    """
    For each tag, how many images belonging to OTHER tags it calls present.

    This is the blast radius of blocking that tag: every one of these is a creative
    that would be refused on a screen blocking it, described by a different category.
    """
    fired: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in scored:
        for slug in slugs:
            if slug == item.owner:
                continue
            if verdict_of(item, slug, thresholds) == "present":
                fired[slug][item.owner] += 1
    return {slug: dict(counts) for slug, counts in fired.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thresholds", type=Path, default=None,
                        help="calibration.json to report against (default: packs.json)")
    parser.add_argument("--tags", nargs="*", default=None, help="report on a subset")
    parser.add_argument("--out", type=Path, default=OUT_FILE)
    args = parser.parse_args()

    detector = Detector(str(PACKS))
    thresholds = load_thresholds(detector, args.thresholds)
    slugs = sorted(detector.packs)
    reported = args.tags or slugs

    unknown = [slug for slug in reported if slug not in detector.packs]
    if unknown:
        raise SystemExit(f"no such tag(s) in packs.json: {', '.join(unknown)}")

    images = discover()
    print(f"{len(images)} eval images, {len(slugs)} tags, "
          f"{'calibrated' if args.thresholds else 'PROVISIONAL'} thresholds\n")

    scored = score_all(detector, images)

    print(f"\n{'tag':22}{'pos':>4}{'neg':>4}  {'found':>6}{'unsure':>7}{'missed':>7}"
          f"{'false':>7}  {'prec':>6}{'rec':>6}  cal")
    print("-" * 86)
    per_tag = {}
    for slug in reported:
        r = own_tag_report(scored, slug, thresholds)
        per_tag[slug] = r
        cal = "yes" if thresholds[slug].calibrated else "no"
        if r["n_pos"] == 0 and r["n_neg"] == 0:
            # Printing 0.00 precision here would read as a measurement of a failure.
            print(f"{slug:22}{'-':>4}{'-':>4}  {'no eval images':>27}{'':>14}  {cal}")
            continue
        print(
            f"{slug:22}{r['n_pos']:>4}{r['n_neg']:>4}  {r['tp']:>6}{r['uncertain_pos']:>7}"
            f"{r['fn']:>7}{r['fp']:>7}  {r['precision']:>6.2f}{r['recall']:>6.2f}  {cal}"
        )

    # "No images" and "images, but detected none of them" are opposite findings and must
    # never share a line. The first is a gap in the eval set; the second is a broken tag.
    unmeasured = [slug for slug, r in per_tag.items() if r["n_pos"] == 0]
    dead = [slug for slug, r in per_tag.items() if r["n_pos"] > 0 and r["tp"] == 0]

    if unmeasured:
        print(f"\nNO EVAL IMAGES (not measured, says nothing about the tag): "
              f"{', '.join(unmeasured)}")
    if dead:
        print(f"\nDETECTS NOTHING despite having positives: {', '.join(dead)}")
        print("  Not a threshold problem — the prompt pack does not describe these images.")

    fired = cross_fire(scored, slugs, thresholds)
    print("\n\nCROSS-TAG FIRING — images owned by another tag that this tag calls present")
    print("(the blast radius of blocking it: each one is a creative it would refuse)\n")
    print(f"{'tag':22}{'total':>7}  worst offenders")
    print("-" * 86)
    for slug in sorted(reported, key=lambda s: -sum(fired.get(s, {}).values())):
        counts = fired.get(slug, {})
        total = sum(counts.values())
        worst = ", ".join(
            f"{owner}:{n}" for owner, n in sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        )
        print(f"{slug:22}{total:>7}  {worst}")

    payload = {
        "packs_version": detector.packs_version,
        "model": detector.model_id,
        "thresholds_from": str(args.thresholds) if args.thresholds else "packs.json",
        "per_tag": per_tag,
        "cross_fire": fired,
        "images": [
            {"path": s.path, "owner": s.owner, "label": s.label,
             "raw": {k: [round(v[0], 4), round(v[1], 4)] for k, v in s.raw.items()}}
            for s in scored
        ],
    }
    args.out.write_text(json.dumps(payload, indent=1) + "\n")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
