"""
A scorer that answers from a fixture file. Development and tests only.

WHY THIS IS WORTH A FILE
------------------------
Before the VLM, the whole stack ran offline: a second virtualenv and ~400MB of SigLIP weights
to install once, but no network afterwards. A hosted model removes that, and without a
substitute every click in the playground costs money and a train journey stops the work.

So this is the third `SCORER`. It answers instantly, for free, with no key and no network,
which is what makes the admin forms, the playground, the try-before-you-save preview and the
review queue buildable without touching a provider. It also gives the test suite a scorer
that behaves like a scorer rather than a mock stitched into each test.

IT OBEYS THE SAME RULES AS THE REAL ONES
----------------------------------------
An unknown image is `present: null`, never `present: false`. A fake that cleared everything it
had not been told about would make the review queue look empty in development and full in
production, and would quietly teach everyone that null is rare. AGENTS.md rule 1 is not
suspended because the answers are canned.

NEVER A DEPLOYMENT. `/api/v1/health` reports the active scorer by name, so a box running this
one says so.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.analyze.intake import Intake
from tagverify.scoring import prompt as prompt_module
from tagverify.scoring.base import ScorerHealth
from tagverify.tags.catalog import list_tags

log = logging.getLogger(__name__)

MODEL = "fake"

#: image_sha256 -> {slug: true | false | null}. Absent file, absent hash and absent slug all
#: mean "not sure", which is the safe direction. Keep it out of version control if it grows
#: real hashes; a handful of fixtures is more useful than a big one.
FIXTURE = Path(__file__).with_name("fake_verdicts.json")


def _fixture() -> dict[str, dict[str, bool | None]]:
    if not FIXTURE.exists():
        return {}
    try:
        return json.loads(FIXTURE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        # A broken fixture must not look like a clean creative. Warn loudly, answer null.
        log.warning("fake scorer: could not read %s (%s); answering null", FIXTURE, exc)
        return {}


async def health(session: AsyncSession) -> ScorerHealth:
    """The real catalog, so the unknown-tag gate behaves exactly as it does in production."""
    rows = await list_tags(session, include_retired=False)
    specs = {row.slug: (row.description or "").strip() for row in rows}
    return ScorerHealth(
        tags=sorted(specs),
        packs_version=prompt_module.catalog_fingerprint(MODEL, specs),
        model=MODEL,
        specs=specs,
    )


async def score_frames(intake: Intake, scorer_health: ScorerHealth) -> list[dict[str, Any]]:
    """Canned verdicts, in the same raw-row shape every other scorer produces."""
    canned = _fixture().get(intake.hash, {})
    rows: list[dict[str, Any]] = []

    for frame in intake.frames:
        for slug in intake.tags:
            present = canned.get(slug)
            row: dict[str, Any] = {
                "tag": slug,
                "present": present,
                "score": 1.0 if present is not None else 0.0,
                "top_phrase": (
                    f"fixture says {present!r}"
                    if slug in canned
                    else "no fixture for this image; answering uncertain"
                ),
                "decided_by": "vlm",
            }
            if intake.kind == "video":
                row["frame_index"] = frame.index
                row["timestamp_s"] = frame.timestamp_s
            rows.append(row)

    return rows
