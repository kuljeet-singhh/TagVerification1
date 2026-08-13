"""
Smoke / golden-set test for the detector.

Run:  ./.venv/bin/python smoke_test.py

Downloads a handful of real photos from Wikipedia (cached in ./.testimages)
and asserts the detector's verdicts.

The NEGATIVE cases are the point of this file. A detector that answers "yes"
to everything scores 100% recall and is worthless. Orange juice must not read
as alcohol; infant formula must not read as protein powder; a mountain must not
read as anything at all.
"""

import io
import json
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image

from detector import Detector

CACHE = Path(".testimages")
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
# Wikimedia rejects requests without a descriptive User-Agent (HTTP 403).
UA = {"User-Agent": "dooh-tagcheck-smoketest/1.0 (contact: dev@localhost)"}

# (wikipedia article, tag slug, expected `present`)
CASES: list[tuple[str, str, bool]] = [
    # ---- should be PRESENT -------------------------------------------------
    ("Beer", "alcohol", True),
    ("Wine", "alcohol", True),
    ("Cigarette", "tobacco_smoking", True),
    ("Whey_protein", "protein_supplements", True),
    ("Weight_training", "gym_fitness", True),
    ("Hamburger", "junk_food", True),
    ("Engagement_ring", "jewellery", True),
    # ---- should be ABSENT: the hard negatives ------------------------------
    # Looks like alcohol (bottle, glass, liquid) but isn't:
    ("Orange_juice", "alcohol", False),
    ("Coffee", "alcohol", False),
    # Looks exactly like a protein tub in a photograph:
    ("Infant_formula", "protein_supplements", False),
    # Totally unrelated -- this is the sigmoid-floor guard. Without it, softmax
    # mass gets shared among equally-irrelevant options and a mountain can be
    # scored as "contains alcohol".
    ("Mount_Everest", "alcohol", False),
    ("Mount_Everest", "gym_fitness", False),
    ("Mount_Everest", "junk_food", False),
]


def _get(url: str, timeout: int, attempts: int = 4) -> bytes:
    """GET with retry. Wikimedia hands out 429s and occasional TLS timeouts when
    a script fetches several images back to back, and a flaky download must not
    be mistaken for a detector regression."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))  # linear backoff, polite to the API
    raise RuntimeError(last)


def fetch(article: str) -> Image.Image:
    """Download an article's lead image, cached on disk. Downscaled to 768px on
    the longest edge to match what the production client uploads."""
    CACHE.mkdir(exist_ok=True)
    cached = CACHE / f"{article}.jpg"

    if not cached.exists():
        meta = json.loads(_get(WIKI_SUMMARY.format(article), timeout=30))
        url = (meta.get("originalimage") or meta.get("thumbnail") or {}).get("source")
        if not url:
            raise RuntimeError(f"no image for article {article!r}")

        img = Image.open(io.BytesIO(_get(url, timeout=60))).convert("RGB")
        img.thumbnail((768, 768))
        img.save(cached, "JPEG", quality=90)

    return Image.open(cached).convert("RGB")


def main() -> int:
    detector = Detector("packs.json")

    # Group cases per image so each photo is encoded once for all its tags.
    by_article: dict[str, list[tuple[str, bool]]] = {}
    for article, tag, expected in CASES:
        by_article.setdefault(article, []).append((tag, expected))

    print(f"\n{'image':<18} {'tag':<22} {'want':<7} {'got':<9} {'score':>6} {'sig':>6} {'by':<14}")
    print("-" * 92)

    failures: list[str] = []
    skipped: list[str] = []
    checked = 0
    total_ms = 0.0

    for article, wanted in by_article.items():
        try:
            image = fetch(article)
        except Exception as exc:  # network flake shouldn't look like a detector bug
            print(f"{article:<18} SKIPPED (fetch failed: {exc})")
            skipped.append(f"{article}: {exc}")
            continue

        tags = [t for t, _ in wanted]
        start = time.perf_counter()
        verdicts = detector.analyze(image, tags)
        elapsed = (time.perf_counter() - start) * 1000
        total_ms += elapsed

        for (tag, expected), v in zip(wanted, verdicts):
            checked += 1
            got = {True: "present", False: "absent", None: "uncertain"}[v.present]
            want = "present" if expected else "absent"
            ok = v.present == expected
            mark = "" if ok else "   <-- FAIL"
            if not ok:
                failures.append(
                    f"{article}/{tag}: wanted {want}, got {got} "
                    f"(score={v.score}, sigmoid={v.sigmoid}, by={v.decided_by}, "
                    f"phrase={v.top_phrase!r})"
                )
            print(
                f"{article:<18} {tag:<22} {want:<7} {got:<9} "
                f"{v.score:>6.3f} {v.sigmoid:>6.3f} {v.decided_by:<14}{mark}"
            )
        print(f"{'':18} ({len(tags)} tags in {elapsed:.0f} ms)")

    print("-" * 92)
    images_done = len(by_article) - len(skipped)
    per_image = total_ms / images_done if images_done else 0.0
    print(
        f"checked {checked}/{len(CASES)} cases across {images_done} images "
        f"in {total_ms:.0f} ms ({per_image:.0f} ms/image)"
    )

    # A test that "passes" because it silently tested nothing is worse than no
    # test at all. Any skip is a failure of the run, not a pass.
    if skipped:
        print(f"\n{len(skipped)} image(s) could not be fetched -- results are incomplete:")
        for s in skipped:
            print(f"  - {s}")
        return 2

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        print(
            "\nNote: thresholds in packs.json are PROVISIONAL guesses. Failures here\n"
            "tell us which prompt packs or thresholds need work -- that is what\n"
            "calibrate.py and real labelled images are for."
        )
        return 1

    print("\nall cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
