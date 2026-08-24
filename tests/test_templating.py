"""
Static asset cache-busting.

The bug this guards against was silent and pointed away from itself: the server served the
new bytes, `curl` looked correct, the file on disk was correct, and only the browser was
wrong — because the URL never changed, so the browser kept the module it had already
evaluated. A stale `?v=` is indistinguishable from "my edit did nothing".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tagverify import templating


@pytest.fixture
def static_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway static root, so nothing is written into the installed package."""
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    # raising=False so this fixture still applies to an implementation that memoises
    # differently — the test should then fail on its assertion, which is the real signal,
    # rather than erroring in setup, which proves nothing.
    monkeypatch.setattr(templating, "_asset_digests", {}, raising=False)
    return tmp_path


def test_editing_a_file_changes_its_url(static_dir: Path) -> None:
    """
    THE regression. Against the previous `lru_cache` implementation this fails: the digest was
    computed once per process, and uvicorn's --reload watches *.py, so editing CSS or JS left
    the URL identical and the browser went on running the old file.
    """
    asset = static_dir / "js" / "app.js"
    asset.parent.mkdir()
    asset.write_text("export const a = 1;\n")
    before = templating.asset("js/app.js")

    asset.write_text("export const a = 2;  // edited\n")
    after = templating.asset("js/app.js")

    assert before != after
    assert "?v=" in after


def test_an_unchanged_file_keeps_its_digest(static_dir: Path) -> None:
    (static_dir / "app.css").write_text("body { color: red }")

    first = templating.asset("app.css")
    assert templating.asset("app.css") == first
    assert templating.asset("app.css") == first


def test_an_unchanged_file_is_not_reread(
    static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The memoisation still has to do its job — this runs for every asset on every render, so
    re-hashing an unchanged file each time would be real waste.
    """
    (static_dir / "app.css").write_text("body { color: red }")
    templating.asset("app.css")

    reads = {"n": 0}
    original = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads["n"] += 1
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    templating.asset("app.css")

    assert reads["n"] == 0


def test_a_file_rewritten_with_identical_content_still_busts(static_dir: Path) -> None:
    """
    mtime changes even when the bytes do not, so the digest is recomputed — and, being a
    content hash, comes back the same. Same URL for the same content is the correct outcome;
    what matters is that the check is cheap and never wrong.
    """
    target = static_dir / "app.css"
    target.write_text("body { color: red }")
    first = templating.asset("app.css")

    target.touch()
    target.write_text("body { color: red }")

    assert templating.asset("app.css") == first


def test_a_missing_asset_degrades_instead_of_raising(static_dir: Path) -> None:
    """A missing file should 404 visibly in the network tab, not blow up mid-template."""
    assert templating.asset("js/nope.js") == "/static/js/nope.js"


# ------------------------------------------------------- the verdict card branches


def _card(decided_by: str, band: str, present: bool | None, sigmoid: float) -> str:
    """Render partials/verdict_card.html for one verdict."""
    from tagverify.tags.decide import Evidence, Thresholds, Verdict

    verdict = Verdict(
        tag="gambling",
        present=present,
        score=0.94,
        confidence="low",
        decided_by=decided_by,
        calibrated=False,
        band=band,
        evidence=Evidence(
            top_phrase="a sports betting app on a phone screen",
            crop=[0.0, 0.0, 1.0, 1.0],
            sigmoid=sigmoid,
        ),
    )
    template = templating.templates.env.get_template("partials/verdict_card.html")
    return template.render(
        verdict=verdict,
        result=type("R", (), {"thresholds": {"gambling": Thresholds()}})(),
    )


def test_a_vetoed_card_still_explains_the_veto() -> None:
    """The existing branch must survive the new one."""
    html = _card("sigmoid_floor", "absent", False, 0.0045)

    assert "Vetoed by the absolute-resemblance floor." in html
    assert "Clears the floor" not in html
    # A vetoed verdict gets the explanation INSTEAD of the ruler — a 0.94 bar labelled
    # "absent" reads as a bug.
    assert 'class="ruler"' not in html
