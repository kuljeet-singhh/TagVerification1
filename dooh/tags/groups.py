"""
Grouping for the tag picker.

Twenty ungrouped pills in a scrolling box is the worst part of the old UI: nothing
distinguishes `pharma_medicine` from `protein_supplements` at a glance, and the tags whose
verdicts carry a regulatory consequence sit interleaved with the ones that do not.

The grouping is presentation only — it never reaches the API, and a tag that appears in
packs.json but not here still shows up (in "Other"), so adding a tag to the pack never
silently hides it from the UI.
"""

from __future__ import annotations

from typing import Any

#: (title, restricted, slugs). Order is the display order.
GROUPS: list[tuple[str, bool, list[str]]] = [
    (
        "Restricted",
        True,
        [
            "alcohol",
            "tobacco_smoking",
            "vaping",
            "gambling",
            "pharma_medicine",
            "revealing_clothing",
        ],
    ),
    (
        "Food & drink",
        False,
        ["junk_food", "sugary_drinks", "restaurant_dining", "protein_supplements"],
    ),
    (
        "Health & lifestyle",
        False,
        ["gym_fitness", "beauty_cosmetics", "fashion_apparel", "jewellery", "travel_tourism"],
    ),
    (
        "Commerce & services",
        False,
        [
            "automotive",
            "real_estate",
            "mobile_electronics",
            "banking_finance",
            "education",
        ],
    ),
]


def group_tags(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_slug = {tag["slug"]: tag for tag in tags}
    grouped: list[dict[str, Any]] = []
    placed: set[str] = set()

    for title, restricted, slugs in GROUPS:
        members = [by_slug[slug] for slug in slugs if slug in by_slug]
        placed.update(tag["slug"] for tag in members)
        if members:
            grouped.append({"title": title, "restricted": restricted, "tags": members})

    # Anything the pack added since this file was last touched. Better a slightly untidy
    # group than a tag the UI silently cannot reach.
    leftover = [tag for tag in tags if tag["slug"] not in placed]
    if leftover:
        grouped.append({"title": "Other", "restricted": False, "tags": leftover})

    return grouped
