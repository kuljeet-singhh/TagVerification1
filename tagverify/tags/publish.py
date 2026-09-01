"""
Turning `content_tags` into a live pack.

Two callers do this: `dooh export-packs --push` and `POST /api/v1/tags/publish`. They exist
for different people — an operator at a terminal, and DOOH's admin UI over HTTP — but they
must mean exactly the same thing by "published", so the rendering and the push live here and
neither caller reimplements them.

That is the same rule `_push` in cli.py already follows for its own half, and the reason is
not tidiness. `packs_version` is a hash of the rendered BYTES. Two renderers that differ by a
single character would produce two fingerprints for one catalog, and every downstream thing
that keys on it — the score cache, the calibration stamps, the "is my change live" banner —
would disagree about which is current.

The CLI keeps its own reporting on top of this (reformat vs semantic change, the calibration
re-stamp advice). That is presentation for a human at a terminal and has no business in an
API response.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import inference
from tagverify.db.models import ContentTag, PackHeader
from tagverify.scoring.client import (
    InferenceHealth,
    push_packs,
    require_publishing_configured,
)
from tagverify.tags import packs as packlib

#: The pack the model reads. Same constant the CLI uses, resolved the same way.
PACKS = Path(inference.__file__).parent / "packs.json"


class PackExportError(RuntimeError):
    """The catalog cannot be rendered. A caller-fixable state, not a crash."""


async def render_catalog(session: AsyncSession) -> str:
    """
    The active catalog as pack text, byte-for-byte what `dooh export-packs` would write.

    Takes NO path, deliberately. It used to take the destination and read it back, because
    the file-level block lived only in the file -- open the pack to write the pack. That
    circular read is why publishing worked from a checkout and failed inside the Docker image,
    which copies banding.py and versioning.py out of `inference/` and not the pack. The block
    lives in `pack_header` now, so rendering touches no filesystem at all; leaving the
    parameter in place would imply a dependency that is gone.

    Refuses two states rather than papering over them:

    * an EMPTY catalog, because writing one would silently disarm every screen policy in the
      product — every analyze call would come back with nothing to compare against;
    * a MISSING header, because everything outside the `tags` array (the prompt template, the
      shared distractors, the defaults, the authoring notes) is what the model scores
      against. Publishing without `prompt_template` would not degrade one tag, it would break
      scoring for all of them at once.
    """
    rows = (
        await session.scalars(
            select(ContentTag)
            .where(ContentTag.status == "active")
            .order_by(ContentTag.sort_order, ContentTag.slug)
        )
    ).all()

    if not rows:
        raise PackExportError(
            "The tag catalog is empty. Refusing to publish a pack with no tags — it would "
            "disable content checking everywhere rather than change it."
        )

    phraseless = [r.slug for r in rows if not r.positives or not r.negatives]
    if phraseless:
        # A tag authored while a VLM was scoring needs no phrases, because a VLM reads the
        # description instead (docs/VLM_SCORING.md 2). SigLIP cannot: a pack entry with an
        # empty pool has nothing to rank, so the tag would score NOTHING while looking
        # perfectly present in the file.
        #
        # REFUSE, do not skip, and do not merely warn. Downstream, a tag missing from the pack
        # produces no verdict, dooh-backend sees fewer verdicts than the screen's blocked tags
        # and policy.ts falls through to NOT_VERIFIED — which is a FLAG, not a block. The
        # screen quietly stops enforcing a category its owner chose and nothing anywhere says
        # so. That is the failure mode in TAG_PIPELINE_IN_PRODUCTION.md 5, and this is the last
        # point at which it is still visible and cheap to fix.
        listed = ", ".join(f"“{slug}”" for slug in sorted(phraseless))
        raise PackExportError(
            f"{len(phraseless)} active tag(s) have no positives or negatives and cannot be "
            f"scored by SigLIP: {listed}. They were written for the VLM, which reads the "
            f"description instead. Publishing would put them in the pack with an empty pool, "
            f"where they would score nothing and every screen blocking them would silently "
            f"stop enforcing. Either give them phrases, or retire them before publishing."
        )

    header = await session.scalar(select(PackHeader.body).where(PackHeader.id == 1))
    if not header:
        raise PackExportError(
            "No pack header is stored, so the prompt template, shared distractors and "
            "defaults are unknown. Run `dooh seed-tags` to import them from "
            "inference/packs.json — publishing without them would break scoring for every "
            "tag, not just one."
        )

    tags = [
        packlib.row_to_pack(
            {
                "slug": r.slug,
                "label": r.label,
                "description": r.description,
                "positives": r.positives,
                "negatives": r.negatives,
                "rationale": r.rationale,
                "sigmoid_floor": r.sigmoid_floor,
                "extra": r.extra,
            }
        )
        for r in rows
    ]
    return packlib.render_pack(packlib.parse_header(header), tags)


async def publish_catalog(session: AsyncSession, out: Path = PACKS) -> InferenceHealth:
    """
    Write the pack and push it into the running model. Returns the model's own health.

    Configuration is checked BEFORE anything is written. Publishing is a two-step act — write
    the pack, push it — and a step that cannot possibly succeed should not leave the first
    step done. Without this, a missing shared secret wrote the catalog to disk and then
    refused, leaving a file the model would silently adopt on its next restart. Caught by the
    first end-to-end run against an unconfigured environment.

    Then the file is written FIRST and pushed second. That order matters on a restart: the
    model boots from whatever is on disk, so a push that succeeded against a stale file would
    be undone the moment the process came back. Writing first means disk and memory agree.

    The pushed bytes are the file's own, not a re-render — `push_packs` hashes exactly what it
    is given, and re-rendering here would risk a fingerprint that differs from the file the
    model will reload from.

    The file not existing is now a normal state, not an error: in the deployed image nothing
    puts a pack there, and the first publish creates it. The read below is only an
    unchanged-check, so its absence means "changed" — which is correct, and is why it is a
    missing_ok comparison rather than a guard.
    """
    require_publishing_configured()
    rendered = await render_catalog(session)
    current = out.read_text() if out.exists() else None
    if current != rendered:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered)
    return await push_packs(out.read_bytes())


def is_semantic_change(session_rendered: str, before: dict) -> bool:
    """
    Did anything the MODEL sees change, or is this only a reformat?

    Bytes moving is not the same as scores moving. A reformat still shifts `packs_version`,
    but the measured thresholds remain correct and only need re-stamping — where a semantic
    change owes a real recalibration. Conflating the two is how a catalog silently drifts.
    """
    return json.loads(session_rendered) != before
