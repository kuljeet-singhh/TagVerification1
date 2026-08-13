"""
Build a DEMO eval set from Wikipedia, so calibrate.py can be exercised before you
have your own creatives.

    ./.venv/bin/python fetch_demo_eval.py

Writes into eval/<tag>/{pos,neg}/.

This is a DEMO, not a substitute for real data. Wikipedia lead photos are clean,
centred product shots; DOOH creatives have text overlays, logos, models, and odd
crops. Thresholds calibrated on this set describe Wikipedia, not your inventory.
Replace these folders with real creatives you have actually run.

What it does get right is the SHAPE of a good eval set: every `alcohol` negative
here is another drink — juice, coffee, soda, tea — because near-misses are what
measure a detector. A folder of unrelated photos would prove nothing.
"""

from __future__ import annotations

import io
import json
import time
import urllib.request
from pathlib import Path

from PIL import Image

EVAL = Path("eval")
WIKI = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
UA = {"User-Agent": "dooh-tagcheck-eval/1.0 (contact: dev@localhost)"}

DEMO: dict[str, dict[str, list[str]]] = {
    "alcohol": {
        # Things that genuinely contain alcohol.
        "pos": [
            "Beer", "Wine", "Whisky", "Cocktail", "Champagne",
            "Vodka", "Tequila", "Rum", "Sake", "Gin",
        ],
        # HARD negatives: every one is another beverage, photographed the same way.
        # This is the half that decides whether calibration means anything.
        "neg": [
            "Orange_juice", "Coffee", "Milk", "Tea", "Lemonade",
            "Smoothie", "Coconut_water", "Soft_drink", "Energy_drink", "Iced_tea",
        ],
    },
    # Deliberately small, to exercise the "scored but not calibrated — too few
    # samples" branch rather than silently pretending 3 images is a measurement.
    "protein_supplements": {
        "pos": ["Whey_protein", "Protein_bar", "Bodybuilding_supplement"],
        "neg": ["Infant_formula", "Powdered_milk", "Milkshake"],
    },
}


def get(url: str, timeout: int, attempts: int = 4) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last)


def fetch(article: str, destination: Path) -> bool:
    if destination.exists():
        return True
    try:
        meta = json.loads(get(WIKI.format(article), timeout=30))
        url = (meta.get("originalimage") or meta.get("thumbnail") or {}).get("source")
        if not url:
            print(f"    no image: {article}")
            return False
        image = Image.open(io.BytesIO(get(url, timeout=60))).convert("RGB")
        image.thumbnail((768, 768))
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, "JPEG", quality=90)
        return True
    except Exception as exc:
        print(f"    failed {article}: {exc}")
        return False


def main() -> None:
    total = 0
    for slug, buckets in DEMO.items():
        for label, articles in buckets.items():
            for article in articles:
                path = EVAL / slug / label / f"{article}.jpg"
                if fetch(article, path):
                    total += 1
                    print(f"\r  fetched {total} images", end="", flush=True)
                time.sleep(0.4)  # be polite; avoids Wikimedia 429s
    print(f"\n\n{total} images in {EVAL}/")
    print("\nNow run:\n    ./.venv/bin/python calibrate.py")
    print(
        "\nRemember: these are Wikipedia product shots, not DOOH creatives. Replace\n"
        "them with real ones before trusting the thresholds."
    )


if __name__ == "__main__":
    main()
