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
from jinja2 import TemplateSyntaxError

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


def _card(decided_by: str, band: str, present: bool | None) -> str:
    """Render partials/verdict_card.html for one verdict."""
    from tagverify.tags.decide import Evidence, Verdict

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
            crop=None,
            sigmoid=None,
        ),
    )
    template = templating.templates.env.get_template("partials/verdict_card.html")
    return template.render(verdict=verdict)


def test_a_card_never_draws_a_threshold_ruler() -> None:
    """
    THE ONE THING THIS INTERFACE MUST NOT DO.

    `score` is the model's own reported certainty, not a similarity, and nothing is compared
    against a cutoff -- migration 0004 removed the last of them. Placing that number on an
    absent/uncertain/present ruler would draw a measurement that never happened.

    Parametrised over a stored `decided_by` this build no longer writes: `creative_tag_analyses`
    still holds rows stamped "siglip" and "sigmoid_floor", and rendering one must not resurrect
    the ruler branch that used to serve them.
    """
    for decided_by in ("vlm", "siglip", "sigmoid_floor"):
        html = _card(decided_by, "absent", False)
        assert 'class="ruler"' not in html, decided_by
        assert "Vetoed by the absolute-resemblance floor." not in html, decided_by
        # The "Read, not ranked." box is gone too. It was the `{% if read %}` branch of the
        # three above, and it outlived the other two by losing its guard rather than by
        # earning its place — printing on every card an explanation of why a ruler no reader
        # had ever seen was missing.
        assert "Read, not ranked." not in html, decided_by


def test_a_card_reports_an_uncertain_verdict_as_needing_review() -> None:
    """
    `present: null` is uncertain, never false — AGENTS.md rule 1, at the last surface.

    That surface used to be the word "not sure" inside the "Read, not ranked." box. It is now
    the band chip, which is the stronger place for it: templating.BANDS calls it "Needs
    review" precisely because that STATES THE REQUIRED ACTION, where a bare "uncertain" left
    the reader to work out that `present: null` is a task and not a middle value.
    """
    html = _card("vlm", "uncertain", None)
    assert "Needs review" in html
    assert "uncalibrated" in html
    # And it must never be dressed as a cleared verdict.
    assert "Absent" not in html
    assert "This content was not found." not in html


def test_an_uncertain_card_does_not_blame_a_threshold_that_does_not_exist() -> None:
    """
    The note on an uncertain card is now the ONLY explanation on it, so it has to be true.

    It read "The score fell between the thresholds" until the box above it was removed —
    describing a mechanism migration 0004 deleted, on the one card where a reviewer is being
    asked to act.
    """
    html = _card("vlm", "uncertain", None)
    assert "threshold" not in html.lower()
    assert "could not tell" in html


# ------------------------------------------------------- every template parses


def test_every_template_compiles() -> None:
    """
    A template that cannot PARSE is a hard 500 that no handler can soften — the exception is
    raised while compiling, before a single byte of HTML exists, so the `try/except` a page
    wraps its own data fetching in never sees it.

    That is exactly how `/docs` broke: a comment reading `// the model's confidence` was added
    inside the single-quoted Jinja literal holding the example JSON response, and the
    apostrophe closed the literal early. Nothing in the suite touched the page, so `make check`
    stayed green and only a browser said otherwise.

    Compiling is not rendering: no context is needed, so this covers every template in the
    package for the cost of a parse.
    """
    env = templating.templates.env
    for path in sorted(templating.TEMPLATE_DIR.rglob("*.html")):
        name = str(path.relative_to(templating.TEMPLATE_DIR))
        try:
            env.get_template(name)
        except TemplateSyntaxError as exc:  # pragma: no cover - the failure path
            pytest.fail(f"{name}:{exc.lineno} does not parse: {exc.message}")
