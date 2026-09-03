"""
The `dooh` CLI.

    dooh create-key "Client name"     # issue an API key
    dooh health                       # the /api/v1/health report, without a server
    dooh gate                         # score the eval set, against the banked baseline
    dooh prune-usage                  # drop expired rate-limit rows

Five commands are gone with the ranking model: seed-tags, seed-thresholds, export-packs,
push-packs and apply-calibration. Every one of them existed to carry a phrase pack from
Postgres to a machine that could not read Postgres, or to measure the cutoffs that turned its
scores into verdicts. A model that reads its prompt per request needs neither, so a saved tag
is live and there is nothing to publish.
"""

from __future__ import annotations

import asyncio
import json

import typer

from tagverify.db.session import session_scope
from tagverify.db.usage import prune_usage_counters

app = typer.Typer(add_completion=False, help="DOOH Tag Verification operations.")



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


@app.command()
def gate(
    arm: str = typer.Option("both", help="vlm | siglip (report-only) | both"),
    limit: int | None = typer.Option(None, help="score only the first N images (smoke run)"),
    report_only: bool = typer.Option(False, "--report", help="print from cache, score nothing"),
    disagreements: bool = typer.Option(
        False, "--disagreements", help="list images whose verdict contradicts their label"
    ),
) -> None:
    """
    Does the VLM beat SigLIP on the labelled eval set? See tagverify/eval_gate.py.

    Gates on CROSS-TAG FALSE BLOCKS, not per-tag precision. Optimising the latter is what
    the retired calibration sweep did, and applying its output took false blocks from 152 to
    211 while mean recall FELL. Mean recall is the second axis, so an improvement bought by
    wrecking the other one is reported as MIXED rather than PASS.

    Only the live SCORER can be scored. 'siglip' is reported from inference/gate_cache.json
    and never re-scored — those 466 rows are the banked baseline, and there is no ranking
    model left to reproduce them with.

    Resumable: re-running skips what is already in inference/gate_cache.json.
    """
    import json as _json

    from tagverify import eval_gate as gate_mod
    from tagverify.config import settings

    async def run() -> None:
        images = gate_mod.discover(limit)
        cache = (
            _json.loads(gate_mod.CACHE.read_text()) if gate_mod.CACHE.exists() else {}
        )

        # The tag list comes from whichever scorer is live, which is also the set the
        # cross-tag metric ranges over. Both arms must range over the SAME set or the
        # two false-block counts are not comparable.
        async with session_scope() as session:
            from tagverify.analyze.run import _scorer_health

            slugs = sorted((await _scorer_health(session)).tags)

        arms = ["siglip", "vlm"] if arm == "both" else [arm]
        reports: dict[str, dict] = {}

        for which in arms:
            # An arm that cannot be SCORED can still be REPORTED, and the siglip arm is
            # exactly that: it is never the live SCORER again, so its 466 rows in the cache
            # are the banked baseline and reading them back is the whole point of keeping
            # them. `continue` used to sit here and skipped the report too, which is how a
            # plain `dooh gate` ended up with an empty `reports` -- see the write below.
            if not report_only and which != settings().scorer:
                typer.secho(
                    f"  not scoring '{which}': SCORER is '{settings().scorer}'. Re-run with\n"
                    f"  SCORER={which} to score that arm — the gate scores through the\n"
                    f"  production path, so it cannot switch scorers mid-process.\n"
                    f"  Reporting '{which}' from the cache instead.",
                    fg=typer.colors.YELLOW,
                )
            elif not report_only:
                typer.echo(f"\n{len(images)} images x {len(slugs)} tags — arm '{which}'")
                await gate_mod.score_arm(which, images, cache)
                gate_mod.CACHE.write_text(_json.dumps(cache))

            rows = [gate_mod.Scored(**v) for v in cache.get(which, {}).values()]
            if rows:
                reports[which] = gate_mod.report(which, rows, slugs)

        if disagreements:
            for which, rows in ((w, [gate_mod.Scored(**v) for v in cache.get(w, {}).values()])
                                for w in reports):
                found = gate_mod.disagreements(rows)
                typer.echo(f"\n{len(found)} label disagreement(s) from '{which}':")
                for d in found:
                    typer.echo(f"  {d['problem']:38} {d['path']}")
                    if d["evidence"]:
                        typer.echo(f"    saw: {d['evidence'][:88]}")

        for which, rep in reports.items():
            typer.echo(f"\n=== {which} ===")
            typer.echo(f"  images                 {rep['images']}")
            typer.echo(f"  cross-tag false blocks {rep['cross_tag_false_blocks']}")
            typer.echo(f"  mean recall            {rep['mean_recall']:.3f}")
            typer.echo(f"  uncertain positives    {rep['uncertain_positive_rate']:.3f}")
            typer.echo(f"  p50/p95 latency        {rep['latency_ms']['p50']}ms / "
                       f"{rep['latency_ms']['p95']}ms")

        if "siglip" in reports and "vlm" in reports:
            typer.echo("\n" + gate_mod.compare(reports["siglip"], reports["vlm"]))

        # MERGE, never replace, and never write an empty report at all.
        #
        # This was an unconditional `write_text(dumps(reports))`. An arm that produced no
        # rows left `reports` empty, so the file holding the banked SigLIP baseline -- 466
        # images, 146 cross-tag false blocks, 0.688 mean recall, the only thing any future
        # scoring change can be judged against -- was truncated to `{}` by a run that had
        # measured nothing. Merging also means scoring one arm today cannot drop the other
        # arm's numbers from the file.
        if not reports:
            typer.secho(
                f"\n  nothing measured — leaving {gate_mod.OUT} as it is.",
                fg=typer.colors.YELLOW,
            )
            return

        existing = (
            _json.loads(gate_mod.OUT.read_text()) if gate_mod.OUT.exists() else {}
        )
        gate_mod.OUT.write_text(_json.dumps({**existing, **reports}, indent=2))
        typer.echo(f"\nwritten to {gate_mod.OUT} ({', '.join(sorted(reports))})")

    asyncio.run(run())


if __name__ == "__main__":
    app()
