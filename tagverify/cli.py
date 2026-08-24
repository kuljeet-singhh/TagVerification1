"""
Operational commands.

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

from inference.versioning import scorer_version
from tagverify.db.models import EvalImage, TagThreshold
from tagverify.db.session import session_scope
from tagverify.db.usage import prune_usage_counters

app = typer.Typer(add_completion=False, help="DOOH Tag Verification operations.")

PACKS = Path("inference/packs.json")
CALIBRATION = Path("inference/calibration.json")


#: The scoring code, whose bytes are part of the fingerprint alongside the prompts.
DETECTOR = Path("inference/detector.py")


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
) -> None:
    """Issue an API key. The plaintext is printed ONCE and cannot be recovered."""

    async def run() -> None:
        from tagverify.auth.keys import create_api_key

        async with session_scope() as session:
            issued = await create_api_key(session, name.strip(), rate_limit)

        typer.echo(f"\n  {issued.plaintext}\n")
        typer.secho(
            "  Copy it now — only a hash is stored, so this cannot be shown again.",
            fg=typer.colors.YELLOW,
        )
        typer.echo(f"  name: {name}   limit: {rate_limit}/min\n")

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
