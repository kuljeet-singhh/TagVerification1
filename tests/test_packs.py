"""
The pack round-trip. No database, no model — `tagverify/tags/packs.py` is pure.

This is the file that decides whether moving the tag catalog into Postgres was safe. The
migration reads inference/packs.json into rows and writes it back out, and the failure mode
is silent: a dropped `//negatives`, a reordered tag, a prompt lost from a list. Nothing
downstream would raise. The model would just start scoring against a catalog nobody
intended, and `packs_version` would move for a reason no one could name.

Two properties, and they check different things:

  1. SEMANTIC NO-OP  — importing the shipped catalog and exporting it again produces the
     same parsed structure. This is what catches corruption.
  2. BYTE STABILITY  — exporting twice over unchanged data produces identical bytes, so
     `packs_version` moves only when a tag really changes.

The export deliberately does NOT reproduce the hand-authored file byte for byte; see the
module docstring in tagverify/tags/packs.py for why that is both impossible and fine.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tagverify.tags.packs import (
    FILE_LEVEL_KEYS,
    RATIONALE_KEY,
    header_from_pack,
    join_rationale,
    load_pack,
    pack_to_row,
    parse_header,
    render_pack,
    row_to_pack,
)

PACKS = Path(__file__).resolve().parents[1] / "inference" / "packs.json"


def _round_trip(pack: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    rows = [pack_to_row(tag, i) for i, tag in enumerate(pack["tags"])]
    text = render_pack(pack, [row_to_pack(r) for r in rows])
    return text, json.loads(text)


@pytest.fixture(scope="module")
def shipped() -> dict[str, Any]:
    if not PACKS.exists():  # pragma: no cover - packs.json is checked in
        pytest.skip("inference/packs.json not present")
    return load_pack(PACKS)


def test_round_trip_is_a_semantic_no_op(shipped: dict[str, Any]) -> None:
    """Every prompt, key and value survives. The one property the migration rests on."""
    _, back = _round_trip(shipped)
    assert back == shipped


def test_round_trip_preserves_tag_key_order(shipped: dict[str, Any]) -> None:
    """
    Not required for correctness — dict equality ignores order — but it keeps the one-time
    reformat diff readable, which is what makes it reviewable.
    """
    _, back = _round_trip(shipped)
    for before, after in zip(shipped["tags"], back["tags"], strict=True):
        assert list(before) == list(after), before["slug"]


def test_export_is_byte_stable(shipped: dict[str, Any]) -> None:
    """A second export over unchanged data must not move packs_version."""
    first, parsed = _round_trip(shipped)
    second, _ = _round_trip(parsed)
    assert first == second


def test_scoring_surface_is_untouched(shipped: dict[str, Any]) -> None:
    """
    The narrower claim, stated separately because it is the one with teeth: nothing that
    reaches the model changed. If this passes and the file's bytes still moved, the export
    was a reformat and no recalibration is owed.
    """

    def surface(pack: dict[str, Any]) -> Any:
        return (
            pack["prompt_template"],
            pack["shared_distractors"],
            pack["defaults"],
            [
                (t["slug"], t["positives"], t["negatives"], t.get("sigmoid_floor"))
                for t in pack["tags"]
            ],
        )

    _, back = _round_trip(shipped)
    assert surface(back) == surface(shipped)


def test_file_level_block_survives(shipped: dict[str, Any]) -> None:
    """`$comment` carries the prompt-authoring rules — losing it would lose the evidence."""
    _, back = _round_trip(shipped)
    for key in ("$comment", "prompt_template", "shared_distractors", "defaults"):
        assert back[key] == shipped[key]


def test_header_round_trips_through_text_byte_for_byte(shipped: dict[str, Any]) -> None:
    """
    The property the `pack_header` table rests on, and the reason its column is TEXT.

    `packs_version` hashes the exported file's BYTES, so a header that came back from storage
    with its keys in a different order is a different pack to every downstream fingerprint —
    every tag would report `calibrated: false` and `apply-calibration` would refuse the
    existing calibration file, for a change that moved no score. Storing the block as text and
    parsing it with json.loads keeps the order, because Python dicts are insertion-ordered.

    Compares RENDERED OUTPUT rather than the parsed dicts on purpose: `==` on two dicts is
    true regardless of key order, so a dict comparison here would pass in exactly the case
    this test exists to catch.
    """
    tags = [row_to_pack(pack_to_row(t, i)) for i, t in enumerate(shipped["tags"])]

    from_file = render_pack(shipped, tags)
    from_storage = render_pack(parse_header(header_from_pack(shipped)), tags)

    assert from_storage == from_file


def test_nested_header_key_order_is_load_bearing(shipped: dict[str, Any]) -> None:
    """
    Why `pack_header.body` is TEXT and not JSONB, stated as a failing condition.

    `render_pack` normalises the TOP-level keys itself — it emits FILE_LEVEL_KEYS in a fixed
    order — so reordering those is harmless and proves nothing. The exposure is one level
    down: `defaults` is copied through as a nested dict, and nothing re-orders its five keys.
    JSONB does not preserve that order, so a header stored as JSONB could come back with the
    same values and export to different bytes — moving `packs_version`, marking every tag
    uncalibrated and making `apply-calibration` refuse, for a change that moved no score.

    This test pins the sharp edge. If it ever stops failing on a reorder, the reason TEXT was
    chosen has gone away and the constraint can be revisited deliberately.
    """
    tags = [row_to_pack(pack_to_row(t, i)) for i, t in enumerate(shipped["tags"])]
    baseline = render_pack(shipped, tags)

    top_shuffled = {k: shipped[k] for k in reversed(FILE_LEVEL_KEYS) if k in shipped}
    assert render_pack(top_shuffled, tags) == baseline, "render_pack normalises top-level order"

    reordered = dict(shipped)
    reordered["defaults"] = {k: shipped["defaults"][k] for k in reversed(list(shipped["defaults"]))}
    assert render_pack(reordered, tags) != baseline, (
        "nested defaults order must affect the bytes — if it does not, this test is no longer "
        "protecting anything"
    )

    # And the text round-trip preserves it, which is the whole claim.
    assert render_pack(parse_header(header_from_pack(shipped)), tags) == baseline


def test_unmodelled_keys_ride_in_extra(shipped: dict[str, Any]) -> None:
    """`detect_objects` and `escalate` are read by nothing, and must still come back."""
    by_slug = {t["slug"]: t for t in shipped["tags"]}
    row = pack_to_row(by_slug["alcohol"], 0)
    assert "detect_objects" in row["extra"]
    assert row_to_pack(row)["detect_objects"] == by_slug["alcohol"]["detect_objects"]


def test_rationale_is_joined_for_editing_but_written_back_verbatim() -> None:
    """
    `//negatives` is a LIST of wrapped prose lines. The admin UI edits one text box, so it is
    joined for display — but an untouched tag must export as the original list, or the
    semantic no-op above would fail on the three tags that have one.
    """
    tag = {
        "slug": "x",
        "label": "X",
        "description": "d",
        "positives": ["a bottle of x"],
        RATIONALE_KEY: ["first line", "second line"],
        "negatives": ["a bottle of y"],
    }
    row = pack_to_row(tag, 0)
    assert row["rationale"] == "first line second line"
    assert row_to_pack(row)[RATIONALE_KEY] == ["first line", "second line"]


def test_edited_rationale_is_written_as_prose() -> None:
    """Once a human rewrites it, the original line wrapping is meaningless — emit a string."""
    tag = {
        "slug": "x",
        "label": "X",
        "description": "d",
        "positives": ["a bottle of x"],
        RATIONALE_KEY: ["first line", "second line"],
        "negatives": ["a bottle of y"],
    }
    row = pack_to_row(tag, 0)
    row["rationale"] = "a completely different reason"
    assert row_to_pack(row)[RATIONALE_KEY] == "a completely different reason"


def test_join_rationale_handles_both_shapes() -> None:
    assert join_rationale(["a", "b"]) == "a b"
    assert join_rationale("a") == "a"
    assert join_rationale(None) is None
    assert join_rationale([]) is None


# ------------------------------------------------------- publishing without a restart


async def test_push_refuses_when_no_secret_is_configured(monkeypatch) -> None:
    """
    Defaults closed, like the admin gate. The model tier's gr.api endpoints carry no
    authorization of their own, and the Space is shielded only by HF privacy plus a
    READ-scoped token already deployed to the web tier and to CI — so an unset secret must
    refuse, never fall through to an unauthenticated push.
    """
    from tagverify import config
    from tagverify.scoring import client

    monkeypatch.setattr(config, "settings", lambda: SimpleNamespace(reload_secret=None))
    monkeypatch.setattr(client, "settings", lambda: SimpleNamespace(reload_secret=None))

    # PublishNotConfigured, deliberately NOT InferenceError. "You have not configured this"
    # and "the model tried and failed" want different handling: the first is an operator's job
    # and a retry is pointless, the second invites one. The API maps them to 409 and 502.
    with pytest.raises(client.PublishNotConfigured) as caught:
        await client.push_packs(b'{"tags": []}')
    assert "RELOAD_SECRET" in str(caught.value)
    assert not isinstance(caught.value, client.InferenceError)


async def test_a_successful_push_clears_the_memos(monkeypatch) -> None:
    """
    analyze/run.py stamps cached_inference_health().packs_version onto the audit row and the
    score-cache row. Leave the memo warm across a publish and, for up to its TTL, scores
    computed under the NEW pack get written to Postgres labelled with the OLD one — the
    wrong-fingerprint rows versioning.py exists to make impossible.
    """
    from tagverify.scoring import client

    monkeypatch.setattr(client, "settings", lambda: SimpleNamespace(reload_secret="s3cret"))

    sent = {}

    async def _fake_call(endpoint, *payload, timeout=None):
        sent["endpoint"] = endpoint
        sent["payload"] = payload
        sent["timeout"] = timeout
        return {
            "status": "ok",
            "model": "m",
            "packs_version": "abcdef123456",
            "tags": ["alcohol"],
            "prompts": 3,
        }

    monkeypatch.setattr(client, "_call", _fake_call)
    cleared = []
    monkeypatch.setattr(client, "reset_caches", lambda: cleared.append(True))

    health = await client.push_packs(b'{"tags": []}')

    assert health.packs_version == "abcdef123456"
    assert sent["endpoint"] == "reload_packs"
    assert sent["payload"] == ('{"tags": []}', "s3cret")
    # The read budget is 8s; a full re-encode is ~4.8s plus transfer and does not fit in it.
    assert sent["timeout"] == client.RELOAD_TIMEOUT_S
    assert cleared == [True]


async def test_a_failed_push_leaves_the_memos_alone(monkeypatch) -> None:
    """Nothing changed on the model, so dropping a good memo would only buy a re-read."""
    from tagverify.scoring import client

    monkeypatch.setattr(client, "settings", lambda: SimpleNamespace(reload_secret="s3cret"))

    async def _boom(endpoint, *payload, timeout=None):
        raise client.InferenceError("space asleep")

    monkeypatch.setattr(client, "_call", _boom)
    cleared = []
    monkeypatch.setattr(client, "reset_caches", lambda: cleared.append(True))

    with pytest.raises(client.InferenceError):
        await client.push_packs(b'{"tags": []}')
    assert cleared == []
