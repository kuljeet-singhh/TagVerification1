"""
Operational commands.

    dooh seed-tags
    dooh export-packs
    dooh seed-thresholds
    dooh create-key "Client name" --rate-limit 60
    dooh apply-calibration
    dooh prune-usage

Replaces the four TypeScript scripts under scripts/. Everything reads DATABASE_URL from the
environment or .env, same as the app.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

import inference
from inference.versioning import scorer_version
from tagverify.db.models import ContentTag, EvalImage, PackHeader, TagThreshold
from tagverify.db.session import session_scope
from tagverify.db.usage import prune_usage_counters
from tagverify.tags import packs as packlib
from tagverify.tags.publish import PackExportError, is_semantic_change, render_catalog

app = typer.Typer(add_completion=False, help="DOOH Tag Verification operations.")

# Resolved from the installed `inference` package, not from the process working directory.
# These were literal Path("inference/...") strings, which meant the `dooh` console script only
# worked when invoked from the repo root and failed with a bare "file not found" anywhere
# else. Note the Dockerfile copies only banding.py and versioning.py out of the model tier, so
# inside the image these paths exist but the files do not — which is correct, since these are
# operator commands run against a checkout, and the error now names the missing path.
_INFERENCE_DIR = Path(inference.__file__).resolve().parent

PACKS = _INFERENCE_DIR / "packs.json"
CALIBRATION = _INFERENCE_DIR / "calibration.json"


#: The scoring code, whose bytes are part of the fingerprint alongside the prompts.
DETECTOR = _INFERENCE_DIR / "detector.py"


def packs_version(path: Path = PACKS) -> str:
    """
    The fingerprint the Space stamps onto its scores.

    Delegates rather than reimplements. This used to be a second copy carrying a comment
    saying "Must match detector.py", and the failure mode if it drifted was quiet: this
    function is what `apply-calibration` compares against the value calibrate.py stamped into
    calibration.json, so a mismatch does not warn, it refuses to apply measured thresholds.
    """
    return scorer_version(path, DETECTOR)


# --------------------------------------------------------------------- seed


@app.command("seed-thresholds")
def seed_thresholds() -> None:
    """
    Seed tag_thresholds from inference/packs.json.

    Safe to re-run. Rows already marked `calibrated` are LEFT ALONE — re-seeding must never
    quietly overwrite measured thresholds with the guesses that ship in packs.json.
    """

    async def run() -> None:
        config = json.loads(PACKS.read_text())
        defaults = config["defaults"]

        async with session_scope() as session:
            existing = (await session.execute(select(TagThreshold))).scalars().all()
            calibrated = {row.slug for row in existing if row.calibrated}

            inserted = refreshed = skipped = 0
            for pack in config["tags"]:
                slug = pack["slug"]
                if slug in calibrated:
                    typer.echo(f"  skip     {slug} (calibrated — not overwriting measurements)")
                    skipped += 1
                    continue

                is_new = slug not in {row.slug for row in existing}
                await session.execute(
                    text(
                        """
                        insert into tag_thresholds
                          (slug, threshold_low, threshold_high, sigmoid_floor, escalate,
                           calibrated, updated_at, updated_by)
                        values (:slug, :low, :high, :floor, :escalate, false, now(), 'seed')
                        on conflict (slug) do update set
                          threshold_low = :low, threshold_high = :high,
                          sigmoid_floor = :floor, escalate = :escalate,
                          updated_at = now(), updated_by = 'seed'
                        """
                    ),
                    {
                        "slug": slug,
                        "low": pack.get("threshold_low", defaults["threshold_low"]),
                        "high": pack.get("threshold_high", defaults["threshold_high"]),
                        "floor": pack.get("sigmoid_floor", defaults["sigmoid_floor"]),
                        "escalate": pack.get("escalate", defaults["escalate"]),
                    },
                )
                typer.echo(f"  {'insert' if is_new else 'refresh':8} {slug}")
                if is_new:
                    inserted += 1
                else:
                    refreshed += 1

        typer.echo(
            f"\npacks {packs_version()} — {inserted} inserted, "
            f"{refreshed} refreshed, {skipped} left calibrated."
        )

    asyncio.run(run())


# ---------------------------------------------------------------- create key


@app.command("create-key")
def create_key(
    name: str = typer.Argument(..., help="Who this key is for."),
    rate_limit: int = typer.Option(60, "--rate-limit", "-r", min=1, max=10_000),
    tags_write: bool = typer.Option(
        False,
        "--tags-write",
        help="Also let this key edit the tag catalog. Issue it as a SEPARATE key.",
    ),
) -> None:
    """
    Issue an API key. The plaintext is printed ONCE and cannot be recovered.

    Without --tags-write the key can analyze images and nothing else, which is what a scoring
    client should hold. Do not add the scope to a key that is already in use for analyze: the
    separation is the point, and a second key costs nothing.
    """

    async def run() -> None:
        from tagverify.auth.deps import TAGS_WRITE
        from tagverify.auth.keys import create_api_key

        scopes = [TAGS_WRITE] if tags_write else []
        async with session_scope() as session:
            issued = await create_api_key(session, name.strip(), rate_limit, scopes)

        typer.echo(f"\n  {issued.plaintext}\n")
        typer.secho(
            "  Copy it now — only a hash is stored, so this cannot be shown again.",
            fg=typer.colors.YELLOW,
        )
        typer.echo(f"  name: {name}   limit: {rate_limit}/min")
        typer.echo(f"  scopes: {', '.join(scopes) if scopes else 'analyze only'}\n")
        if scopes:
            typer.secho(
                "  This key can rewrite the tag catalog. Keep it out of the scoring client.",
                fg=typer.colors.YELLOW,
            )

    asyncio.run(run())


# ------------------------------------------------------------- calibration


@app.command("apply-calibration")
def apply_calibration(
    report_path: Path = typer.Argument(CALIBRATION, help="calibration.json from calibrate.py"),
) -> None:
    """
    Apply measured thresholds from a calibration run.

    Refuses if the pack fingerprint has moved since calibration: the numbers would describe
    scores the model no longer produces, which is worse than leaving the guesses in place.
    """

    async def run() -> None:
        if not report_path.exists():
            typer.secho(f"{report_path} not found. Run calibration first:", fg=typer.colors.RED)
            typer.echo("    cd inference && ./.venv/bin/python calibrate.py")
            raise typer.Exit(1)

        report: dict[str, Any] = json.loads(report_path.read_text())
        current = packs_version()

        if current != report.get("packs_version"):
            typer.secho("Pack version mismatch.\n", fg=typer.colors.RED)
            typer.echo(f"  calibration.json measured against: {report.get('packs_version')}")
            typer.echo(f"  inference/packs.json is now:       {current}\n")
            typer.echo(
                "packs.json changed after calibration ran, so these thresholds describe\n"
                "scores the model no longer produces. Re-run calibrate.py."
            )
            raise typer.Exit(1)

        applicable = [tag for tag in report["tags"] if tag.get("calibrated")]
        if not applicable:
            typer.echo("\nNothing to apply — no tag reached its precision target.")
            typer.echo("Add more labelled near-miss negatives, or rewrite the prompt packs.")
            return

        async with session_scope() as session:
            for tag in applicable:
                await session.execute(
                    text(
                        """
                        update tag_thresholds set
                          threshold_low = :low, threshold_high = :high, sigmoid_floor = :floor,
                          calibrated = true, precision = :precision, recall = :recall,
                          packs_version_seen = :packs, updated_at = now(), updated_by = 'calibrate'
                        where slug = :slug
                        """
                    ),
                    {
                        "slug": tag["slug"],
                        "low": tag["threshold_low"],
                        "high": tag["threshold_high"],
                        "floor": tag["sigmoid_floor"],
                        # The confidence LOWER bound, not the point estimate. It is the number
                        # selection was judged against, and the one that survives being quoted
                        # to a client.
                        "precision": tag["precision_lower"],
                        "recall": tag["recall"],
                        "packs": report["packs_version"],
                    },
                )
                typer.echo(
                    f"  {tag['slug']:24} low={tag['threshold_low']:.4f} "
                    f"high={tag['threshold_high']:.4f} P>={tag['precision_lower']:.2f}"
                )

            # Recorded with ON CONFLICT DO NOTHING rather than try/flush/except.
            #
            # This loop used to add each row, flush it, and swallow the unique-index
            # violation with `await session.rollback()`. That rollback is not scoped to the
            # row — it unwinds the enclosing session_scope() transaction, INCLUDING the
            # threshold updates issued just above. Re-running a calibration therefore
            # discarded every threshold it had just applied, while still printing
            # "applied N calibrated tag(s)". The failure needed an eval image that had been
            # recorded before, which is the normal case for the second run onwards.
            #
            # One idempotent statement removes the exception path entirely, and rowcount is
            # the number of genuinely new labels rather than a hand-kept counter.
            rows = [
                {
                    "id": str(uuid.uuid4()),
                    # Hash of the PATH, not the bytes — this identifies an eval-set entry,
                    # not a piece of user traffic, and is deliberately not comparable with
                    # analyses.image_hash.
                    "image_hash": hashlib.sha256(image["path"].encode()).hexdigest()[:32],
                    "storage_ref": image["path"],
                    "tag_slug": tag["slug"],
                    "label": image["label"],
                    "note": f"score={image['score']} sigmoid={image['sigmoid']}",
                }
                for tag in report["tags"]
                for image in tag.get("images", [])
            ]

            recorded = 0
            if rows:
                # RETURNING rather than rowcount: an ON CONFLICT DO NOTHING insert reports
                # rowcount as -1 through this driver, which printed "recorded -1 labels".
                # Counting the returned ids is exact, and skipped rows return nothing — so
                # this is the number of genuinely NEW labels, which is what the line claims.
                result = await session.execute(
                    pg_insert(EvalImage)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["image_hash", "tag_slug"])
                    .returning(EvalImage.id)
                )
                recorded = len(result.scalars().all())

        typer.echo(
            f"\napplied {len(applicable)} calibrated tag(s); "
            f"recorded {recorded} eval image label(s)."
        )
        typer.echo(
            "Thresholds take effect within 30s (the API memoises them), and those tags "
            "stop being reported as uncalibrated."
        )

    asyncio.run(run())


# ----------------------------------------------------------------- upkeep


@app.command("prune-usage")
def prune_usage() -> None:
    """Delete rate-limit counter rows older than a day."""
    deleted = asyncio.run(prune_usage_counters())
    typer.echo(f"pruned {deleted} row(s)")


@app.command("health")
def health() -> None:
    """Print the same report as GET /api/v1/health, without starting a server."""

    async def run() -> None:
        from tagverify.api.v1.health import _database, _inference

        report = {"database": await _database(), "inference": await _inference()}
        typer.echo(json.dumps(report, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    app()


# ---------------------------------------------------------------------- tags


@app.command("seed-tags")
def seed_tags() -> None:
    """
    Import inference/packs.json into content_tags and pack_header. Run once.

    Both halves of the file, not just the tags: the file-level block (`prompt_template`,
    `shared_distractors`, `defaults` and the `$comment` notes) goes to `pack_header`, which is
    what lets `export-packs` stop reading the pack in order to write it. Until that row
    exists, publishing refuses — it is the only copy of what the model actually scores
    against.

    Non-destructive: an existing row is never overwritten, so re-running this cannot revert an
    edit made through the admin UI. That matters more than it looks — this is the command
    someone will reach for when they are not sure whether it ran.
    """

    async def run() -> None:
        pack = packlib.load_pack(PACKS)
        rows = [packlib.pack_to_row(tag, i) for i, tag in enumerate(pack["tags"])]

        async with session_scope() as session:
            existing = set((await session.execute(select(ContentTag.slug))).scalars().all())
            added = 0
            for row in rows:
                if row["slug"] in existing:
                    continue
                session.add(ContentTag(created_by="seed", updated_by="seed", **row))
                added += 1

            header_present = await session.scalar(select(PackHeader.id).where(PackHeader.id == 1))
            if header_present is None:
                session.add(
                    PackHeader(id=1, body=packlib.header_from_pack(pack), updated_by="seed")
                )

        typer.echo(f"  imported {added} tag(s); {len(rows) - added} already present")
        typer.echo(
            "  imported the pack header"
            if header_present is None
            else "  pack header already present"
        )
        if added:
            typer.echo("  next: dooh export-packs")

    asyncio.run(run())


async def _push(pack: Path) -> None:
    """
    Publish a pack file into the running model. Shared by `export-packs --push` and
    `push-packs`, so the two cannot drift on what "published" means.

    Sends the file's BYTES rather than a re-render: packs_version hashes exactly these, so
    anything that re-serialises on the way would give the model a different fingerprint from
    the one this CLI just printed, and `apply-calibration` would then refuse the calibration
    measured against it.
    """
    from tagverify.scoring.client import InferenceError, InferenceWarming, push_packs

    try:
        health = await push_packs(pack.read_bytes())
    except (InferenceError, InferenceWarming) as exc:
        typer.secho(f"  push failed: {exc}", fg=typer.colors.RED)
        typer.echo("  the file is written; restart the model tier to publish it instead")
        raise typer.Exit(1) from exc

    typer.secho(
        f"  published: {len(health.tags)} tag(s) live, packs {health.packs_version}",
        fg=typer.colors.GREEN,
    )
    if health.packs_version != packs_version(pack):
        # Only possible if the model hashed different bytes -- a re-serialisation somewhere,
        # or a detector.py that differs from this checkout's.
        typer.secho(
            f"  WARNING: the model reports {health.packs_version} but this file hashes to "
            f"{packs_version(pack)}. Scores will be stamped with a fingerprint that does not "
            f"match this pack.",
            fg=typer.colors.RED,
        )


@app.command("push-packs")
def push_packs_command(
    pack: Path = typer.Option(PACKS, "--pack", help="The pack file to publish."),
) -> None:
    """
    Publish inference/packs.json into the RUNNING model, without restarting it.

    The model encodes every prompt once at startup, so a tag written by `dooh export-packs`
    is not live until either this command runs or the process restarts. Needs RELOAD_SECRET
    set here and as RELOAD_SECRET on the model tier; publishing is refused when either is
    unset.

    On a Hugging Face Space this is a cache, never a store: the filesystem is ephemeral and a
    cold start rebuilds from git, so re-run this after any Space restart. The "not live yet"
    banner in /admin is what catches a revert.
    """

    async def run() -> None:
        if not pack.exists():
            typer.secho(
                f"{pack} does not exist — run `dooh export-packs` first.", fg=typer.colors.RED
            )
            raise typer.Exit(1)
        await _push(pack)

    asyncio.run(run())


@app.command("export-packs")
def export_packs(
    out: Path = typer.Option(PACKS, "--out", help="Where to write the pack file."),
    check: bool = typer.Option(
        False, "--check", help="Report what would change and write nothing."
    ),
    push: bool = typer.Option(
        False, "--push", help="Also publish into the running model, so no restart is needed."
    ),
) -> None:
    """
    Write inference/packs.json from the database. This file is generated — do not hand-edit it.

    The FIRST export reformats the file: the hand-authored original carries blank lines,
    writes 0.30 where json re-emits 0.3, and keeps detect_objects on one line. That reformat
    is semantically inert — every prompt, threshold and key survives — but packs_version
    hashes the file's BYTES, so the fingerprint moves while no score does.

    That distinction is the whole point: because nothing the model sees changed, the measured
    thresholds in calibration.json are still correct and only need re-stamping. Bump
    packs_version in calibration.json and run `dooh apply-calibration`. Do NOT re-run
    calibrate.py — it is unnecessary and it would wipe the hand-edited `held_back` block.
    """

    async def run() -> None:
        before = packlib.load_pack(out) if out.exists() else None

        # Rendered by tags/publish.py, which POST /api/v1/tags/publish also uses. Two
        # renderers would mean two packs_version values for one catalog — and everything
        # downstream keys on that value.
        try:
            async with session_scope() as session:
                rendered = await render_catalog(session)
        except PackExportError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(1) from exc

        # `before is None` is a NORMAL state now, not the impossible one it used to be.
        # render_catalog stopped reading the pack when the file-level block moved into
        # pack_header, so a first export onto a machine with no pack file is expected --
        # which is the entire point of that change. Everything downstream that compared
        # against the old file has to cope with there not being one.
        unchanged = before is not None and out.read_text() == rendered
        previous_version = packs_version(out) if out.exists() else None

        # Say plainly which kind of change this is. A reformat needs no recalibration; a
        # semantic change does, and conflating the two is how a catalog silently drifts.
        # With no previous file there is nothing to have reformatted, so it is a content
        # change by definition -- claiming "reformat only" would send the reader to a
        # re-stamp shortcut whose precondition (no score moved) cannot be checked.
        semantic = True if before is None else is_semantic_change(rendered, before)

        if unchanged:
            typer.echo("  no change")
            return

        if check:
            kind = "semantic change" if semantic else "reformat only"
            typer.echo(f"  would rewrite {out} ({kind})")
            raise typer.Exit(1)

        out.write_text(rendered)
        typer.echo(f"  wrote {len(json.loads(rendered)['tags'])} tag(s) to {out}")
        typer.echo(f"  packs_version: {packs_version(out)}")

        if push:
            await _push(out)
        else:
            typer.echo("  not live yet: run `dooh push-packs`, or restart the model tier")

        if semantic:
            typer.secho(
                "  content changed — recalibrate (see docs/TAG_CRUD_IMPLEMENTATION.md 3.3).",
                fg=typer.colors.YELLOW,
            )
            return

        # A reformat moves the fingerprint without moving a score, so the measured thresholds
        # are still correct and only need re-stamping against the new value. That shortcut is
        # ONLY valid if the reformat is the sole change since calibration ran — otherwise
        # re-stamping would assert that measurements describe scores produced by code that has
        # since changed, which is exactly the claim `calibrated` exists to make honestly.
        measured = None
        if CALIBRATION.exists():
            measured = json.loads(CALIBRATION.read_text()).get("packs_version")

        if measured == previous_version:
            typer.echo(
                "  reformat only: no prompt or threshold changed, so no recalibration is\n"
                "  owed. Bump packs_version in calibration.json to the value above and run\n"
                "  `dooh apply-calibration` to re-stamp. Do NOT re-run calibrate.py — it\n"
                "  would wipe the hand-edited `held_back` block."
            )
        else:
            typer.secho(
                f"  reformat only — no score moved here. But calibration.json was measured\n"
                f"  against {measured}, and the pack was already at {previous_version} before\n"
                f"  this export, so the thresholds were ALREADY stale and every tag was\n"
                f"  already reporting calibrated:false. Re-stamping would be a false claim.\n"
                f"  A real recalibration is owed, and it was owed before this change.",
                fg=typer.colors.YELLOW,
            )

    asyncio.run(run())
