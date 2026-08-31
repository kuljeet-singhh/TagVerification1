"""
The tag catalog: validation rules, the admin surface, and the JSON API.

The validation tests need neither a database nor the model -- they are the important half,
because a created tag goes live with no measured thresholds and no activation gate, so these
checks and the publish runbook ARE the safety model.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from sqlalchemy import delete

from inference.versioning import surface_fingerprint
from tagverify.api.v1 import tags as tags_api
from tagverify.auth import admin
from tagverify.db.models import ContentTag
from tagverify.db.session import session_scope
from tagverify.scoring.client import InferenceError
from tagverify.tags import catalog
from tagverify.web import admin as admin_web
from tests.conftest import ROOT, needs_db

SLUG = "zz_test_tag"

GOOD = {
    "label": "Test tag",
    "description": "Only ever created by the test suite.",
    "positives": [
        "a bag of test pellets",
        "a jar of test powder",
        "a tester holding a clipboard",
        "a laboratory test bench",
        "a rack of test tubes",
    ],
    "negatives": [
        "a bag of gravel",
        "a jar of honey",
        "a chef holding a clipboard",
        "a kitchen work bench",
        "a rack of coats",
        "a plain cardboard box",
    ],
    "rationale": "Written by tests; nothing here is measured.",
}


def cookie(password: str) -> dict[str, str]:
    issued = str(int(time.time() * 1000))
    signature = hmac.new(password.encode(), issued.encode(), hashlib.sha256).hexdigest()
    return {admin.COOKIE: f"{issued}.{signature}"}


@pytest.fixture
async def clean_tag():
    """
    Remove every `zz_`-prefixed slug before and after, so a failed run cannot poison the next.

    Prefix rather than the one slug: these tests run against the REAL database (there is no
    fixture schema), so anything they leave behind is still there next time. A test that
    validates a second throwaway slug would otherwise strand it.
    """

    async def drop() -> None:
        async with session_scope() as session:
            await session.execute(delete(ContentTag).where(ContentTag.slug.like("zz\\_%")))

    await drop()
    yield
    await drop()


# ------------------------------------------------------------------ validation


@needs_db
async def test_a_well_formed_tag_is_accepted(clean_tag) -> None:
    async with session_scope() as session:
        valid = await catalog.validate_tag(session, slug=SLUG, **GOOD)
    assert valid.slug == SLUG
    assert len(valid.positives) == 5


@needs_db
@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("positives", GOOD["positives"][:4], "at least 5 positives"),
        ("negatives", GOOD["negatives"][:5], "at least 6 negatives"),
    ],
)
async def test_too_few_phrases_is_refused(clean_tag, field, value, fragment) -> None:
    """The negatives do most of the work, so the floor on them is higher than on positives."""
    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError) as caught:
            await catalog.validate_tag(session, slug=SLUG, **{**GOOD, field: value})
    assert fragment in caught.value.message


@needs_db
@pytest.mark.parametrize("slug", ["9leading_digit", "has-a-hyphen", "has space", ""])
async def test_malformed_slugs_are_refused(clean_tag, slug: str) -> None:
    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError):
            await catalog.validate_tag(session, slug=slug, **GOOD)


@needs_db
async def test_slug_case_and_padding_are_normalised_not_refused(clean_tag) -> None:
    """
    A slug is an identifier, so case is a typo rather than a decision.

    Normalising also closes a real hole: `Zz_Test_Tag` and `zz_test_tag` would otherwise be
    two rows whose positives compete against each other in the same rival pool, which is the
    opposite of what cross-tag competition is for.
    """
    async with session_scope() as session:
        valid = await catalog.validate_tag(session, slug=f"  {SLUG.upper()}  ", **GOOD)
    assert valid.slug == SLUG


@needs_db
async def test_a_phrase_owned_by_another_tag_is_refused(clean_tag) -> None:
    """
    The rule that comes from detector.py rather than from the docs.

    Prompts are interned by exact string, and a tag's own phrases are subtracted from its
    rival pool -- so a phrase shared by two tags vanishes from BOTH their pools and weakens
    the cross-tag competition each relies on. That competition is what took a gym poster's
    `gambling` score from 0.974 to 0.061, so this is correctness, not tidiness.
    """
    async with session_scope() as session:
        stolen = (await catalog.get_tag(session, "alcohol")).positives[0]
        with pytest.raises(catalog.TagValidationError) as caught:
            await catalog.validate_tag(
                session, slug=SLUG, **{**GOOD, "positives": [stolen, *GOOD["positives"][1:]]}
            )
    assert "alcohol" in caught.value.message


@needs_db
async def test_a_retired_tag_does_not_reserve_its_phrases(clean_tag) -> None:
    """
    The mirror of the rule above, and the reason the two checks disagree on retired rows.

    A retired tag is not exported, so its phrases are not in the pack and cannot be in
    anyone's rival pool — the wording is genuinely free again. Its SLUG stays reserved
    though, because the row still owns that tag's eval images and thresholds.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)
        await catalog.retire_tag(session, SLUG)

    async with session_scope() as session:
        # The phrases come back: validating a different slug that reuses them must pass.
        valid = await catalog.validate_tag(session, slug="zz_other_tag", **GOOD)
        assert valid.positives == GOOD["positives"]

        # The slug does not: it still exists, retired.
        with pytest.raises(catalog.TagValidationError) as caught:
            await catalog.validate_tag(session, slug=SLUG, **GOOD)
    assert "retired" in caught.value.message


@needs_db
async def test_a_phrase_cannot_be_both_positive_and_negative(clean_tag) -> None:
    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError):
            await catalog.validate_tag(
                session,
                slug=SLUG,
                **{**GOOD, "negatives": [GOOD["positives"][0], *GOOD["negatives"]]},
            )


@needs_db
async def test_duplicate_phrases_within_a_tag_are_refused(clean_tag) -> None:
    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError):
            await catalog.validate_tag(
                session,
                slug=SLUG,
                **{**GOOD, "positives": [*GOOD["positives"], GOOD["positives"][0]]},
            )


@needs_db
async def test_an_existing_slug_is_refused(clean_tag) -> None:
    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError) as caught:
            await catalog.validate_tag(session, slug="alcohol", **GOOD)
    assert "already" in caught.value.message


# --------------------------------------------------------------------- warnings


def test_the_mirror_check_agrees_with_the_hardened_tags() -> None:
    """
    RULE 1 as a heuristic: a positive's quantifier should have a negative that echoes it.

    Pinned against the two tags whose negatives were hand-hardened by the measurements this
    rule comes from. If a future change starts warning on these, the heuristic has drifted
    away from the evidence and is on its way back to warning about everything.
    """
    packs = json.loads((ROOT / "inference/packs.json").read_text())
    by_slug = {t["slug"]: t for t in packs["tags"]}
    for slug in ("alcohol", "protein_supplements"):
        tag = by_slug[slug]
        assert catalog._mirror_warnings(tag["positives"], tag["negatives"]) == [], slug


def test_the_mirror_check_still_catches_an_unmirrored_quantifier() -> None:
    warnings = catalog._mirror_warnings(
        ["a bottle of whiskey", "a glass of beer"], ["a plain brick wall", "a cloudy sky"]
    )
    assert warnings and "a bottle of" in warnings[0]


# ------------------------------------------------------------------- admin UI


@needs_db
async def test_the_tags_tab_renders(client, admin_password: str, monkeypatch) -> None:
    """
    The banner assertion is driven off a STUBBED model, not the one that happens to be
    running. It used to read the real health, so it passed only while some tag in the real
    database was unpublished -- and went red the moment someone published everything, which
    is the one state the product is supposed to be in.
    """
    monkeypatch.setattr(admin_web, "cached_inference_health", _health_returning(["alcohol"]))

    response = await client.get("/admin?tab=tags", cookies=cookie(admin_password))
    assert response.status_code == 200
    assert 'id="panel-tags"' in response.text
    # The thing people get wrong: a saved tag is not a live tag until it is published.
    assert "not live yet" in response.text


# The tab strip and the panels are two copies of one rule, and they drifted: the keys panel
# hid only on ?tab=thresholds, so ?tab=tags served the keys table and the tags table stacked.
# Presence assertions did not catch it -- exclusivity is the property that matters.
@needs_db
@pytest.mark.parametrize("tab", ["keys", "thresholds", "tags", "", "bogus"])
async def test_exactly_one_admin_panel_is_visible(client, admin_password: str, tab: str) -> None:
    html = (await client.get(f"/admin?tab={tab}", cookies=cookie(admin_password))).text
    expected = tab if tab in admin_web.ADMIN_TABS else "keys"

    for name in admin_web.ADMIN_TABS:
        marker = f'id="panel-{name}" role="tabpanel"'
        assert marker in html, f"{name} panel missing entirely"
        assert (f"{marker} hidden" in html) is (name != expected)

    assert html.count('aria-selected="true"') == 1


@needs_db
async def test_creating_a_tag_through_the_admin_form(
    client, admin_password: str, clean_tag
) -> None:
    response = await client.post(
        "/admin/tags",
        data={
            "slug": SLUG,
            "label": GOOD["label"],
            "description": GOOD["description"],
            "positives": "\n".join(GOOD["positives"]),
            "negatives": "\n".join(GOOD["negatives"]),
            "rationale": GOOD["rationale"],
        },
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert response.status_code == 200
    assert SLUG in response.text

    async with session_scope() as session:
        assert (await catalog.get_tag(session, SLUG)) is not None


@needs_db
async def test_a_rejected_tag_comes_back_as_200_with_the_reason(
    client, admin_password: str, clean_tag
) -> None:
    """HTMX drops 4xx responses, so a user-fixable error must be a re-rendered 200."""
    response = await client.post(
        "/admin/tags",
        data={
            "slug": SLUG,
            "label": GOOD["label"],
            "description": GOOD["description"],
            "positives": "a bag of test pellets",
            "negatives": "\n".join(GOOD["negatives"]),
        },
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert response.status_code == 200
    assert "at least 5 positives" in response.text


@needs_db
@pytest.mark.parametrize("path", ["/admin/tags", "/admin/tags/alcohol/retire"])
async def test_tag_mutations_reject_an_unauthenticated_caller(client, path: str) -> None:
    response = await client.post(path, data={})
    assert response.status_code == 403


@needs_db
@pytest.mark.parametrize("path", ["/admin/tags", "/admin/tags/alcohol/retire"])
async def test_tag_mutations_reject_a_missing_csrf_token(
    client, admin_password: str, path: str
) -> None:
    response = await client.post(path, data={}, cookies=cookie(admin_password))
    assert response.status_code == 403


# -------------------------------------------------------------------- JSON API


@needs_db
async def test_managed_tags_requires_an_admin_session(client, api_key: str) -> None:
    """An API key is deliberately not enough: DOOH holds the only one."""
    response = await client.get("/api/v1/tags/managed", headers={"x-api-key": api_key})
    assert response.status_code == 403
    assert response.json()["error"] == "FORBIDDEN"


@needs_db
async def test_managed_tags_lists_prompts(client, admin_password: str) -> None:
    response = await client.get("/api/v1/tags/managed", cookies=cookie(admin_password))
    assert response.status_code == 200
    body = response.json()
    assert body["count"] > 0
    assert "positives" in body["tags"][0]


@needs_db
async def test_create_and_retire_over_the_api(client, admin_password: str, clean_tag) -> None:
    headers = {"x-csrf-token": admin.csrf_token()}
    created = await client.post(
        "/api/v1/tags",
        json={"slug": SLUG, **GOOD},
        cookies=cookie(admin_password),
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["pending_publish"] is True

    retired = await client.request(
        "DELETE", f"/api/v1/tags/{SLUG}", cookies=cookie(admin_password), headers=headers
    )
    assert retired.status_code == 200
    assert retired.json()["status"] == "retired"

    # Retire is not delete: the row survives, so its eval images and thresholds stay attached.
    async with session_scope() as session:
        assert (await catalog.get_tag(session, SLUG)).status == "retired"


@needs_db
async def test_patching_a_tag_over_the_api(client, admin_password: str, clean_tag) -> None:
    """
    PATCH had no coverage at all, which is how it sat broken: update_tag took `slug`
    positionally AND required it in **fields, so every call raised TypeError before any
    validation ran. The route is the API half of the admin Save button.
    """
    headers = {"x-csrf-token": admin.csrf_token()}
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    response = await client.patch(
        f"/api/v1/tags/{SLUG}",
        json={**GOOD, "negatives": [*GOOD["negatives"], "a bag of builders sand"]},
        cookies=cookie(admin_password),
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["pending_publish"] is True

    async with session_scope() as session:
        assert "a bag of builders sand" in (await catalog.get_tag(session, SLUG)).negatives


@needs_db
async def test_patching_cannot_rename_a_tag(client, admin_password: str, clean_tag) -> None:
    """
    The slug owns the tag's eval images and its measured thresholds, so a rename orphans both.

    The path segment is what picks the row; TagPayload carries no slug of its own. This proves
    the check in validate_tag is actually REACHED -- it was unreachable behind the TypeError.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    response = await client.patch(
        f"/api/v1/tags/{SLUG}",
        json={**GOOD, "slug": "zz_renamed"},
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    # TagPayload ignores an unknown `slug` key, so the rename is a no-op rather than an error
    # -- what matters is that the row keeps its slug and nothing new appears.
    assert response.status_code == 200, response.text
    async with session_scope() as session:
        assert await catalog.get_tag(session, SLUG) is not None
        assert await catalog.get_tag(session, "zz_renamed") is None


@needs_db
async def test_creating_a_duplicate_slug_over_the_api_is_a_400(client, admin_password: str) -> None:
    response = await client.post(
        "/api/v1/tags",
        json={"slug": "alcohol", **GOOD},
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "BAD_REQUEST"


# ----------------------------------------------------------------- editing


@needs_db
async def test_editing_negatives_through_the_admin_ui(
    client, admin_password: str, clean_tag
) -> None:
    """
    The accuracy loop, end to end.

    Rewriting hard negatives is why edit exists at all: delete-and-recreate would strand the
    slug's labelled eval images and its measured thresholds, which are the only things that
    make a tag more than a guess.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    auth = {"cookies": cookie(admin_password), "headers": {"x-csrf-token": admin.csrf_token()}}

    form = await client.get(f"/admin/tags/{SLUG}/edit", cookies=auth["cookies"])
    assert form.status_code == 200
    assert 'name="negatives"' in form.text
    assert GOOD["negatives"][0] in form.text

    saved = await client.post(
        f"/admin/tags/{SLUG}",
        data={
            "label": GOOD["label"],
            "description": GOOD["description"],
            "positives": "\n".join(GOOD["positives"]),
            "negatives": "\n".join([*GOOD["negatives"], "a bag of builders sand"]),
            "rationale": "Added sand: it kept reading as test pellets.",
        },
        **auth,
    )
    assert saved.status_code == 200

    async with session_scope() as session:
        row = await catalog.get_tag(session, SLUG)
        assert "a bag of builders sand" in row.negatives
        assert row.rationale.startswith("Added sand")


@needs_db
async def test_editing_preserves_a_measured_sigmoid_floor(
    client, admin_password: str, clean_tag
) -> None:
    """
    The floor is measured by `dooh apply-calibration`, never typed into the edit form -- so
    an edit that does not mention it must leave it alone.

    update_tag assigns row.sigmoid_floor unconditionally from the validated payload, and the
    form sends no floor, so this used to write None over the calibration on every save. Rule 3
    checks the floor BEFORE the score bands, which makes silently clearing it a scoring change
    disguised as a copy edit.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)
        row = await catalog.get_tag(session, SLUG)
        row.sigmoid_floor = 0.42

    saved = await client.post(
        f"/admin/tags/{SLUG}",
        data={
            "label": GOOD["label"],
            "description": GOOD["description"],
            "positives": "\n".join(GOOD["positives"]),
            "negatives": "\n".join([*GOOD["negatives"], "a bag of builders sand"]),
            "rationale": "Edited the negatives, said nothing about the floor.",
        },
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert saved.status_code == 200

    async with session_scope() as session:
        row = await catalog.get_tag(session, SLUG)
        assert "a bag of builders sand" in row.negatives, "the edit must still have applied"
        assert row.sigmoid_floor == 0.42


@needs_db
async def test_a_rejected_edit_returns_the_form_not_the_row(
    client, admin_password: str, clean_tag
) -> None:
    """The author's text has to survive a rejection, or a long prompt list is retyped."""
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    response = await client.post(
        f"/admin/tags/{SLUG}",
        data={
            "label": GOOD["label"],
            "description": GOOD["description"],
            "positives": "a bag of test pellets",
            "negatives": "\n".join(GOOD["negatives"]),
            "rationale": "",
        },
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert response.status_code == 200
    assert "at least 5 positives" in response.text
    assert 'name="negatives"' in response.text, "must come back as the edit form"


@needs_db
async def test_the_slug_cannot_be_changed_by_editing(clean_tag) -> None:
    """It owns the tag's eval images and thresholds; renaming would silently orphan both."""
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)
        with pytest.raises(catalog.TagValidationError) as caught:
            await catalog.update_tag(session, SLUG, slug="zz_renamed", **GOOD)
    assert "cannot be changed" in caught.value.message


@needs_db
@pytest.mark.parametrize("path", ["/admin/tags/alcohol/edit", "/admin/tags/alcohol/row"])
async def test_edit_views_require_an_admin_session(client, path: str) -> None:
    """GET, but they expose the prompt pack, so they are gated the same as the writes."""
    assert (await client.get(path)).status_code == 403


# ------------------------------------------------------- the form keeps your text


@needs_db
async def test_a_rejected_create_keeps_everything_typed(
    client, admin_password: str, clean_tag
) -> None:
    """
    The regression guard for a silent bug: the form used to come back blank.

    It matters more here than on a typical form because the phrase lists ARE the tag. A good
    one carries 8+ positives and 10+ negatives, hand-written and deliberately mirrored — and
    making someone retype all of that because they were four phrases short is how you get four
    lazy phrases, which is the outcome this validation exists to prevent.
    """
    response = await client.post(
        "/admin/tags",
        data={
            "slug": SLUG,
            "label": GOOD["label"],
            "description": GOOD["description"],
            "positives": "a bag of only one",  # too few, so this is refused
            "negatives": "\n".join(GOOD["negatives"]),
            "rationale": GOOD["rationale"],
        },
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert response.status_code == 200
    assert "at least 5 positives" in response.text

    assert f'value="{SLUG}"' in response.text
    assert GOOD["label"] in response.text
    assert GOOD["description"] in response.text
    assert "a bag of only one" in response.text
    assert GOOD["negatives"][-1] in response.text, "the negatives must survive too"
    assert GOOD["rationale"] in response.text

    # A slug that survived a rejection stops following the label, so a later label edit does
    # not overwrite a correction the author made by hand.
    assert 'data-touched="1"' in response.text


@needs_db
async def test_a_successful_create_clears_the_form(client, admin_password: str, clean_tag) -> None:
    """The opposite of the above: the next tag must start blank, not inherit the last one."""
    response = await client.post(
        "/admin/tags",
        data={
            "slug": SLUG,
            "label": GOOD["label"],
            "description": GOOD["description"],
            "positives": "\n".join(GOOD["positives"]),
            "negatives": "\n".join(GOOD["negatives"]),
            "rationale": GOOD["rationale"],
        },
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert response.status_code == 200
    assert f'value="{SLUG}"' not in response.text
    assert GOOD["positives"][0] not in response.text
    # It is listed in the table below the form, though — that is how you know it saved.
    assert SLUG in response.text


# --------------------------------------------- "not live yet" reflects reality


class _FakeHealth:
    """
    Just enough of InferenceHealth for the admin page: the slugs the model knows, plus the
    two build identifiers the shell rail prints beside its health dot.
    """

    def __init__(self, tags: list[str], prompt_fingerprints: dict[str, str] | None = None) -> None:
        self.tags = tags
        self.packs_version = "deadbeef1234"
        self.model = "google/siglip2-base-patch16-224"
        self.prompt_fingerprints = prompt_fingerprints


def _health_returning(
    tags: list[str] | None,
    exc: Exception | None = None,
    fingerprints: dict[str, str] | None = None,
):
    async def _call():
        if exc is not None:
            raise exc
        return _FakeHealth(tags or [], fingerprints)

    return _call


async def _live_fingerprints(*slugs: str) -> dict[str, str]:
    """What the model WOULD report if it were serving exactly what the database holds."""
    async with session_scope() as session:
        rows = await catalog.list_tags(session, include_retired=False)
    return {
        r.slug: surface_fingerprint(list(r.positives), list(r.negatives), r.sigmoid_floor)
        for r in rows
        if not slugs or r.slug in slugs
    }


@needs_db
async def test_a_tag_the_model_lacks_is_flagged_not_live(
    client, admin_password: str, monkeypatch, clean_tag
) -> None:
    """
    The banner used to warn unconditionally, so nobody read it and a tag sat unpublished.

    Faked rather than driven off a real model: this belongs in the needs_db tier, and the
    point under test is the comparison, not the network call.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    monkeypatch.setattr(admin_web, "cached_inference_health", _health_returning(["alcohol"]))

    html = (await client.get("/admin?tab=tags", cookies=cookie(admin_password))).text
    assert "not live yet" in html
    assert SLUG in html
    # The banner must name the whole publish, not half of it: exporting without pushing
    # leaves the running process on its old pack, which is the trap the wording exists for.
    assert "export-packs --push" in html
    assert "not this web app" in html


@needs_db
async def test_an_edited_tag_is_flagged_even_though_its_slug_is_live(
    client, admin_password: str, monkeypatch, clean_tag
) -> None:
    """
    The gap this whole fingerprint exists for, and the one nothing covered.

    A tag keeps its slug when you rewrite its phrases, so a slug-only comparison sees it in
    the model's list and says nothing -- while the model goes on scoring against the words it
    loaded at startup. That is worse than the missing-tag case it did catch: the tag LOOKS
    published, and the playground quietly matches the old prompts.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    # The model reports the slug, but fingerprinted against phrases we then changed.
    stale = surface_fingerprint(["a phrase the model still has"], GOOD["negatives"], None)
    monkeypatch.setattr(
        admin_web,
        "cached_inference_health",
        _health_returning([SLUG], fingerprints={SLUG: stale}),
    )

    html = (await client.get("/admin?tab=tags", cookies=cookie(admin_password))).text
    assert "edit not live" in html, "the row chip must flag a drifted tag"
    assert "edits not live yet" in html or "edit not live yet" in html
    assert SLUG in html


@needs_db
async def test_a_tag_whose_phrases_match_is_not_flagged(
    client, admin_password: str, monkeypatch, clean_tag
) -> None:
    """The other half: a fingerprint that agrees must produce silence, not a second banner."""
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    monkeypatch.setattr(
        admin_web,
        "cached_inference_health",
        _health_returning(
            [r for r in await _live_fingerprints()],
            fingerprints=await _live_fingerprints(),
        ),
    )

    html = (await client.get("/admin?tab=tags", cookies=cookie(admin_password))).text
    assert "not live yet" not in html
    assert "edit not live" not in html


@needs_db
async def test_a_model_that_reports_no_fingerprints_claims_nothing_about_edits(
    client, admin_password: str, monkeypatch, clean_tag
) -> None:
    """
    The two tiers deploy separately, so the Space can be older than the web app.

    An older model reports slugs but no fingerprints. It can still prove a tag is ABSENT, and
    must still say so -- but it cannot speak to edits, and inventing "edited" from a missing
    field would be the same guess this signal replaced.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    monkeypatch.setattr(
        admin_web, "cached_inference_health", _health_returning(["alcohol"], fingerprints=None)
    )

    html = (await client.get("/admin?tab=tags", cookies=cookie(admin_password))).text
    assert "not live yet" in html, "an absent slug is still knowable without fingerprints"
    assert SLUG in html
    assert "edit not live" not in html, "must not claim an edit it cannot see"


@needs_db
async def test_no_banner_when_every_tag_is_live(
    client, admin_password: str, monkeypatch, clean_tag
) -> None:
    """Silence is the signal. A banner that is always on is one nobody reads."""
    async with session_scope() as session:
        rows = await catalog.list_tags(session, include_retired=False)
        live = [r.slug for r in rows]

    monkeypatch.setattr(admin_web, "cached_inference_health", _health_returning(live))

    html = (await client.get("/admin?tab=tags", cookies=cookie(admin_password))).text
    assert "not live yet" not in html
    assert "not live" not in html


@needs_db
async def test_an_unreachable_model_claims_nothing(
    client, admin_password: str, monkeypatch, clean_tag
) -> None:
    """
    The degraded path, which is the one that would otherwise 500 in production at 3am.

    It must render, and it must NOT mark every tag stale — flagging everything because health
    timed out would be the same lie the stateful banner exists to remove.
    """
    monkeypatch.setattr(
        admin_web,
        "cached_inference_health",
        _health_returning(None, exc=InferenceError("model asleep")),
    )

    response = await client.get("/admin?tab=tags", cookies=cookie(admin_password))
    assert response.status_code == 200
    assert "Cannot tell what is live" in response.text
    assert "not live</span>" not in response.text


# ----------------------------------- the same answer over the JSON API, for DOOH's admin
#
# profile's own admin renders publish state as chips; DOOH's does not share the template, so
# GET /tags/managed has to carry the fact rather than the markup. Same publish_state(), same
# rule that None renders as silence -- these tests pin the transport, not the comparison.


async def _managed_states(client, admin_password: str) -> dict[str, str | None]:
    response = await client.get("/api/v1/tags/managed", cookies=cookie(admin_password))
    assert response.status_code == 200
    return {row["slug"]: row["publish_state"] for row in response.json()["tags"]}


@needs_db
async def test_managed_tags_reports_a_tag_the_model_lacks_as_absent(
    client, admin_password: str, monkeypatch, clean_tag
) -> None:
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    monkeypatch.setattr(tags_api, "cached_inference_health", _health_returning(["alcohol"]))

    assert (await _managed_states(client, admin_password))[SLUG] == "absent"


@needs_db
async def test_managed_tags_reports_a_matching_tag_as_live(
    client, admin_password: str, monkeypatch, clean_tag
) -> None:
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    fingerprints = await _live_fingerprints()
    monkeypatch.setattr(
        tags_api,
        "cached_inference_health",
        _health_returning(list(fingerprints), fingerprints=fingerprints),
    )

    assert (await _managed_states(client, admin_password))[SLUG] == "live"


@needs_db
async def test_managed_tags_reports_a_drifted_tag_as_edited(
    client, admin_password: str, monkeypatch, clean_tag
) -> None:
    """The case slugs alone cannot see: published, then rewritten, still scoring the old words."""
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    stale = surface_fingerprint(["a phrase the model still has"], GOOD["negatives"], None)
    monkeypatch.setattr(
        tags_api,
        "cached_inference_health",
        _health_returning([SLUG], fingerprints={SLUG: stale}),
    )

    assert (await _managed_states(client, admin_password))[SLUG] == "edited"


@needs_db
async def test_managed_tags_still_answers_when_the_model_is_asleep(
    client, admin_password: str, monkeypatch, clean_tag
) -> None:
    """
    200 with every state null, never a 500 and never "nothing is published".

    This endpoint is the admin tag list. A sleeping Space must cost the caller a missing
    signal, not the page -- and a null that a client renders as "not published" would be the
    guess the whole comparison exists to avoid.
    """
    monkeypatch.setattr(
        tags_api,
        "cached_inference_health",
        _health_returning(None, exc=InferenceError("model asleep")),
    )

    states = await _managed_states(client, admin_password)
    assert states, "the list must still render"
    assert set(states.values()) == {None}


# ------------------------------------------------- scoped keys reach the write routes


def _key_headers(plaintext: str) -> dict[str, str]:
    """What a server-to-server caller sends. No cookie, no CSRF token."""
    return {"x-api-key": plaintext}


@needs_db
async def test_a_scoped_key_can_write_tags_without_an_admin_session(client, clean_tag) -> None:
    """
    The whole point of scopes: DOOH edits the catalog holding a key, not ADMIN_PASSWORD.

    No cookie and no CSRF header here on purpose. CSRF defends a browser that can be induced
    to send credentials it already holds; a server sending a secret header is not that.
    """
    from tagverify.auth.deps import TAGS_WRITE
    from tagverify.auth.keys import create_api_key

    async with session_scope() as session:
        issued = await create_api_key(session, "zz-scoped-test", 60, [TAGS_WRITE])

    try:
        response = await client.post(
            "/api/v1/tags", json={"slug": SLUG, **GOOD}, headers=_key_headers(issued.plaintext)
        )
        assert response.status_code == 201, response.text
        assert response.json()["pending_publish"] is True
    finally:
        async with session_scope() as session:
            from tagverify.auth.keys import revoke_api_key

            await revoke_api_key(session, issued.id)


@needs_db
async def test_an_unscoped_key_is_refused_and_told_why(client, api_key: str) -> None:
    """
    The property the old blanket key-refusal was protecting, still enforced.

    There is one analyze key and DOOH holds it. Letting the client that asks "does this
    contain alcohol?" rewrite what alcohol means was the thing to prevent; scopes did not
    open that door. The message names the scope rather than saying "forbidden", because the
    caller holds a valid key and the alternative is guessing which of their keys they sent.
    """
    response = await client.post(
        "/api/v1/tags", json={"slug": "zz_never_created", **GOOD}, headers=_key_headers(api_key)
    )
    assert response.status_code == 403
    assert response.json()["error"] == "FORBIDDEN"
    assert "tags:write" in response.json()["message"]


@needs_db
async def test_no_credential_at_all_is_refused(client) -> None:
    response = await client.post("/api/v1/tags", json={"slug": "zz_never_created", **GOOD})
    assert response.status_code == 403
    assert response.json()["error"] == "FORBIDDEN"


@needs_db
async def test_the_admin_session_still_works_and_still_needs_csrf(
    client, admin_password: str, clean_tag
) -> None:
    """Adding a second way in must not quietly remove the first, or weaken it."""
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    without_csrf = await client.request(
        "DELETE", f"/api/v1/tags/{SLUG}", cookies=cookie(admin_password)
    )
    assert without_csrf.status_code == 403, "CSRF is still required on the session path"

    with_csrf = await client.request(
        "DELETE",
        f"/api/v1/tags/{SLUG}",
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert with_csrf.status_code == 200
    assert with_csrf.json()["status"] == "retired"
