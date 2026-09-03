"""
The tag catalog: validation rules, the admin surface, and the JSON API.

The validation tests need neither a database nor the model -- they are the important half,
because a created tag is LIVE at the next request, with no measured thresholds and no
activation gate, so these checks ARE the safety model. There is no publish runbook left to
share the job with.

There is less to validate than there once was. The phrase rules -- duplicates, positive /
negative overlap, cross-tag phrase ownership -- went with migration 0003, along with the
columns they guarded. They protected SigLIP's rival pools; there are no pools.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from sqlalchemy import delete

from tagverify.auth import admin
from tagverify.db.models import ContentTag
from tagverify.db.session import session_scope
from tagverify.scoring import prompt as prompt_module
from tagverify.tags import catalog
from tagverify.tags.decide import Thresholds, effective_calibrated
from tagverify.web import admin as admin_web
from tests.conftest import needs_db

SLUG = "zz_test_tag"

#: The label is long enough to clear GOOD_LABEL, so the happy path carries no warnings and a
#: test that asserts on warnings is asserting on the thing it named.
GOOD = {
    "label": "Test tag category",
    "description": "Only ever created by the test suite, and never by anything else at all.",
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
    assert valid.description == GOOD["description"]
    assert valid.warnings == []


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
    two catalog entries a screen owner reads as one blocked category, each answering the
    model separately.
    """
    async with session_scope() as session:
        valid = await catalog.validate_tag(session, slug=f"  {SLUG.upper()}  ", **GOOD)
    assert valid.slug == SLUG


@needs_db
async def test_a_retired_slug_stays_reserved(clean_tag) -> None:
    """
    A retired row still owns its labelled eval images and its measured thresholds, so reusing
    its slug would silently inherit both. Retire is not delete -- see catalog.retire_tag for
    what a hard delete strands in DOOH's devices.blocked_tags.

    This used to be half of a pair: its sibling asserted that a retired tag RELEASED its
    phrases back into the pool while keeping its slug. The phrases went with migration 0003;
    the slug rule is the half that was ever about identity.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)
        await catalog.retire_tag(session, SLUG)

    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError) as caught:
            await catalog.validate_tag(session, slug=SLUG, **GOOD)
    assert "retired" in caught.value.message


# ------------------------------------------------------- retirement is reversible


@needs_db
async def test_a_retired_tag_can_be_made_active_again(clean_tag) -> None:
    """
    The inverse of retirement, which had no code path back at all.

    Retiring deliberately keeps the row, its eval images and its thresholds -- so keeping all
    of that and offering no way to use it again made a misclick permanent.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)
        await catalog.retire_tag(session, SLUG)

    async with session_scope() as session:
        row = await catalog.restore_tag(session, SLUG, actor="admin")
        assert row.status == "active"
        assert row.updated_by == "admin"

    async with session_scope() as session:
        active = await catalog.list_tags(session, include_retired=False)
        assert SLUG in {row.slug for row in active}


@needs_db
async def test_restoring_is_refused_when_the_catalog_is_full(clean_tag, monkeypatch) -> None:
    """
    THE ONE GUARD THAT IS NOT A MIRROR OF RETIRE, and the reason this test matters most.

    `validate_tag` refuses a new tag at MAX_TAGS active, but that check runs on CREATE. A
    restore walks straight past it, so retire one / create one / restore it would leave the
    catalog over the cap -- which is not a soft limit: the model refuses more than MAX_TAGS in
    one request, so exceeding it fails EVERY upload rather than degrading anything.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)
        await catalog.retire_tag(session, SLUG)
        active = len(await catalog.list_tags(session, include_retired=False))

    # A cap the live catalog is already at, so the restore is the call that would exceed it.
    monkeypatch.setattr(catalog, "MAX_TAGS", active)

    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError) as caught:
            await catalog.restore_tag(session, SLUG)
    assert "full" in caught.value.message

    async with session_scope() as session:
        row = await catalog.get_tag(session, SLUG)
        assert row is not None and row.status == "retired", "the refusal must not half-apply"


@needs_db
async def test_restoring_an_active_tag_is_refused_rather_than_silently_doing_nothing(
    clean_tag,
) -> None:
    """A success that changed nothing is worse than an error that says why."""
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError) as caught:
            await catalog.restore_tag(session, SLUG)
    assert "already active" in caught.value.message


@needs_db
async def test_restoring_an_unknown_slug_is_refused(clean_tag) -> None:
    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError):
            await catalog.restore_tag(session, "zz_never_existed")


@needs_db
async def test_restoring_does_not_release_the_slug_to_a_different_tag(clean_tag) -> None:
    """
    Restore revives the SAME row. It must not become a back door around the reservation that
    `test_a_retired_slug_stays_reserved` pins -- a restored tag still holds its own name.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)
        await catalog.retire_tag(session, SLUG)

    async with session_scope() as session:
        await catalog.restore_tag(session, SLUG)

    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError) as caught:
            await catalog.validate_tag(session, slug=SLUG, **GOOD)
    assert "already" in caught.value.message


def test_a_restored_tag_does_not_claim_a_superseded_calibration() -> None:
    """
    AGENTS.md rule 4, at the surface this feature could most easily break.

    A restored tag's `tag_thresholds` row survived retirement -- that is the payoff of retiring
    rather than deleting. But the fingerprint moved when the tag left the catalog and moves
    again when it returns, so the measurement describes a different question. It must read
    superseded, not calibrated. Nothing in restore_tag may "restore" the calibration too.
    """
    measured = Thresholds(calibrated=True, packs_version_seen="before_the_round_trip")
    assert effective_calibrated(measured, "after_the_round_trip") is False
    assert effective_calibrated(measured, "before_the_round_trip") is True


@needs_db
async def test_an_existing_slug_is_refused(clean_tag) -> None:
    async with session_scope() as session:
        with pytest.raises(catalog.TagValidationError) as caught:
            await catalog.validate_tag(session, slug="alcohol", **GOOD)
    assert "already" in caught.value.message


# --------------------------------------------------------------------- warnings


@needs_db
async def test_a_thin_name_warns_but_still_saves(clean_tag) -> None:
    """
    The last check standing, and the only one that touches accuracy.

    The NAME is what the model is told to look for -- `- dog: dog` is the whole of it -- so a
    vague name is a vague verdict and there is no threshold left to correct it with. It WARNS
    rather than refuses on purpose: a short name is a weak tag, not an invalid one, and a rule
    that refuses teaches people to pad to 13 characters instead of naming the thing better.

    It used to guard the description, back when the description was the question. Moving it
    rather than deleting it is the point: the warning followed the job.
    """
    async with session_scope() as session:
        valid = await catalog.validate_tag(session, slug=SLUG, **{**GOOD, "label": "dog"})
    assert valid.label == "dog"
    assert any("name is short" in w for w in valid.warnings)


@needs_db
async def test_a_thin_description_no_longer_warns(clean_tag) -> None:
    """
    The description is not the question any more, so advice about its length would be advice
    about the wrong field -- and the kind that teaches people to stop reading warnings.
    """
    async with session_scope() as session:
        valid = await catalog.validate_tag(
            session, slug=SLUG, **{**GOOD, "description": "Booze."}
        )
    assert valid.warnings == []


@needs_db
async def test_a_tag_saves_with_no_description_at_all(clean_tag) -> None:
    """
    It is optional, because nothing reads it that can refuse.

    The model is sent the NAME; the description is a blurb for the screen owner choosing what
    to block, and `components/ui/multi-select.tsx` renders nothing at all when it is blank. So
    a refusal here would be a chore with no verdict and no customer behind it.

    Whitespace normalises to "" rather than being stored as spaces, so the picker's truthiness
    check sees what it expects.
    """
    async with session_scope() as session:
        blank = await catalog.validate_tag(session, slug=SLUG, **{**GOOD, "description": ""})
        spaces = await catalog.validate_tag(session, slug=SLUG, **{**GOOD, "description": "   "})
    assert blank.description == ""
    assert spaces.description == "", "whitespace is not a description"


@needs_db
async def test_the_admin_form_saves_a_tag_with_only_a_name(
    client, admin_password: str, clean_tag
) -> None:
    """
    The form asks for one field, and means it.

    A `required` attribute on the description input would fail this test in the browser and
    nowhere else, which is why the assertion is on the rendered markup as well as the save --
    that attribute is the thing most likely to come back.
    """
    page = await client.get("/admin?tab=tags", cookies=cookie(admin_password))
    form = page.text[page.text.index('hx-post="/admin/tags"') :]
    field = form[form.index('name="description"') - 200 : form.index('name="description"') + 200]
    assert "required" not in field, "the description input is required again"

    response = await client.post(
        "/admin/tags",
        data={"label": "Zz nameless thing"},
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert response.status_code == 200

    async with session_scope() as session:
        row = await catalog.get_tag(session, "zz_nameless_thing")
        assert row is not None, "a name alone must be enough"
        assert row.description == ""


# ------------------------------------------------------- the derived identifier


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Pet supplies", "pet_supplies"),
        ("Alcoholic content", "alcoholic_content"),
        ("  Junk & Fast Food!! ", "junk_fast_food"),
        ("Pet_Supplies", "pet_supplies"),
        # Valid, and not what anyone wanted. SLUG_RE needs a leading letter, so the 3 is
        # stripped and "3D printing" lands on d_printing. THIS is why the admin form keeps an
        # override: guessing at "three_d_printing" would be worse, because it would be
        # unpredictable rather than merely wrong.
        ("3D printing", "d_printing"),
        # No letters at all: there is nothing to build an identifier from.
        ("123", ""),
        ("", ""),
    ],
)
def test_slugify(label: str, expected: str) -> None:
    """
    Deliberately identical to the JavaScript it replaced, so nothing already in the catalog
    would derive differently and the "Saved as ..." preview cannot disagree with the save.
    """
    assert catalog.slugify(label) == expected


def test_a_name_with_no_letters_is_refused_in_its_own_terms() -> None:
    """
    The message says NAME, not slug.

    Someone using the admin form never typed a slug, so "Slug must start with a letter" is
    advice about a field that is not on their screen.
    """
    with pytest.raises(catalog.TagValidationError) as caught:
        catalog.slug_from_label("123")
    assert "name" in caught.value.message.lower()
    assert "123" in caught.value.message


@needs_db
async def test_a_colliding_name_is_refused_in_the_terms_it_was_given(
    client, admin_password: str, clean_tag
) -> None:
    """
    The message follows how the identifier ARRIVED, which is the whole point of deriving it.

    "The slug 'alcohol' is already in use" names a field someone who typed "Alcohol" never
    filled in. But "'X' becomes the identifier 'Y'" is equally wrong the other way, when they
    opened the override and typed Y themselves. `validate_tag` compares the slug against
    `slugify(label)` to tell the two apart, so no flag has to be threaded through.
    """
    auth = {"cookies": cookie(admin_password), "headers": {"x-csrf-token": admin.csrf_token()}}

    derived = await client.post(
        "/admin/tags",
        data={"label": "Alcohol", "description": GOOD["description"]},
        **auth,
    )
    assert derived.status_code == 200
    assert "becomes the identifier" in derived.text
    assert "Alcoholic content" in derived.text, "name the tag already holding it"

    typed = await client.post(
        "/admin/tags",
        data={
            "label": "Zz something else",
            "slug": "alcohol",
            "description": GOOD["description"],
        },
        **auth,
    )
    assert typed.status_code == 200
    assert "becomes the identifier" not in typed.text, "they typed it; nothing became anything"
    assert "already in use" in typed.text

    # And the override comes back OPEN, holding what they typed. An identifier that survived a
    # rejection has been seen and possibly corrected by hand.
    assert 'id="tag-slug-override" hidden' not in typed.text
    assert 'value="alcohol"' in typed.text


@needs_db
async def test_an_explicit_identifier_wins_over_the_derivation(
    client, admin_password: str, clean_tag
) -> None:
    """
    THE OVERRIDE ACTUALLY OVERRIDES, which is the whole reason the field survived being hidden.

    19 of the 24 tags in this catalog carry a slug shorter than their label — `alcohol` for
    "Alcoholic content", `pharma_medicine` for "Pharmaceutical and medicine". The slug is
    permanent and retiring a tag does not release it, so removing the escape hatch would make
    the first surprising derivation unfixable.
    """
    response = await client.post(
        "/admin/tags",
        data={
            "label": "Zz testing and verification",  # would derive zz_testing_and_verification
            "slug": SLUG,
            "description": GOOD["description"],
        },
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert response.status_code == 200

    async with session_scope() as session:
        assert (await catalog.get_tag(session, SLUG)) is not None
        assert (await catalog.get_tag(session, "zz_testing_and_verification")) is None


@needs_db
async def test_the_slug_preview_comes_from_the_server(client, admin_password: str) -> None:
    """
    Rendered by `catalog.slugify`, not by JavaScript.

    There were two JS copies of this transform and neither was authoritative — the server only
    validated the result. A preview that disagrees with what the save writes is worse than no
    preview, and the preview is the only moment anyone gets to notice a permanent identifier
    is wrong.
    """
    response = await client.get(
        "/admin/tags/slug-preview",
        params={"label": "Pet supplies"},
        cookies=cookie(admin_password),
    )
    assert response.status_code == 200
    assert "pet_supplies" in response.text


@needs_db
async def test_the_slug_preview_requires_an_admin_session(client) -> None:
    assert (await client.get("/admin/tags/slug-preview?label=Pet")).status_code == 403


# ------------------------------------------------------------------- admin UI


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
        data={"label": "Zz test tag", "description": GOOD["description"]},
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert response.status_code == 200
    assert SLUG in response.text

    async with session_scope() as session:
        # The form sent no slug at all: "Zz test tag" derived zz_test_tag.
        assert (await catalog.get_tag(session, SLUG)) is not None


@needs_db
@pytest.mark.parametrize(
    "path", ["/admin/tags", "/admin/tags/alcohol/retire", "/admin/tags/alcohol/restore"]
)
async def test_tag_mutations_reject_an_unauthenticated_caller(client, path: str) -> None:
    response = await client.post(path, data={})
    assert response.status_code == 403


@needs_db
@pytest.mark.parametrize(
    "path", ["/admin/tags", "/admin/tags/alcohol/retire", "/admin/tags/alcohol/restore"]
)
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
async def test_managed_tags_lists_the_prompt(client, admin_password: str) -> None:
    """
    The prompt is the description, and it is what a caller managing tags needs to see.

    The assertion on absence is the load-bearing half: dooh-backend maps this payload
    (`content-verification/client.ts` toManagedTag) and a key that reappears here is a phrase
    column that came back with it.
    """
    response = await client.get("/api/v1/tags/managed", cookies=cookie(admin_password))
    assert response.status_code == 200
    body = response.json()
    assert body["count"] > 0

    row = body["tags"][0]
    assert row["description"]
    for gone in ("positives", "negatives", "rationale", "sigmoid_floor"):
        assert gone not in row, f"{gone} was dropped in migration 0003"


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
async def test_editing_the_description_through_the_admin_ui(
    client, admin_password: str, clean_tag
) -> None:
    """
    The accuracy loop, end to end, and a regression guard.

    Rewriting the DESCRIPTION is why edit exists at all: delete-and-recreate would strand the
    slug's labelled eval images and its measured thresholds. It used to be rewriting the hard
    negatives; migration 0003 dropped those and the sentence took over the job.

    THE REGRESSION. Between f357a1c and 0003 this form still rendered `positives` and
    `negatives` as `required` textareas over columns nothing read, so a browser refused to
    submit a description edit until the author retyped phrases that could not affect the
    verdict. Asserting the fields are ABSENT is what keeps them from coming back: a form
    posting a field the server ignores is how that state gets re-entered.
    """
    async with session_scope() as session:
        await catalog.create_tag(session, slug=SLUG, **GOOD)

    auth = {"cookies": cookie(admin_password), "headers": {"x-csrf-token": admin.csrf_token()}}

    form = await client.get(f"/admin/tags/{SLUG}/edit", cookies=auth["cookies"])
    assert form.status_code == 200
    assert 'name="description"' in form.text
    for gone in ("positives", "negatives", "rationale"):
        assert f'name="{gone}"' not in form.text, f"{gone} was dropped in migration 0003"

    reworded = "Test pellets and test powder, but never builders sand or gravel."
    saved = await client.post(
        f"/admin/tags/{SLUG}",
        data={"label": GOOD["label"], "description": reworded},
        **auth,
    )
    assert saved.status_code == 200

    async with session_scope() as session:
        assert (await catalog.get_tag(session, SLUG)).description == reworded


@needs_db
async def test_editing_a_description_moves_the_catalog_fingerprint(clean_tag) -> None:
    """
    The other half of the edit contract, and the reason the description is not just a label.

    It is half of `packs_version` -- `catalog_fingerprint` hashes model id, PROMPT_VERSION and
    every active tag's slug and description -- so rewording one invalidates the verdicts
    decided against the old wording rather than serving them under the new. The phrase columns
    were never in this hash, which is exactly why dropping them re-keyed nothing.
    """
    fingerprint = prompt_module.catalog_fingerprint
    before = fingerprint("m", {SLUG: GOOD["description"]})
    after = fingerprint("m", {SLUG: "Something else entirely, said in different words."})
    assert before != after


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
    """GET, but they render the edit form, so they are gated the same as the writes."""
    assert (await client.get(path)).status_code == 403


# ------------------------------------------------------- the form keeps your text


@needs_db
async def test_a_successful_create_clears_the_form(client, admin_password: str, clean_tag) -> None:
    """The opposite of the above: the next tag must start blank, not inherit the last one."""
    response = await client.post(
        "/admin/tags",
        data={"label": "Zz test tag", "description": GOOD["description"]},
        cookies=cookie(admin_password),
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert response.status_code == 200
    assert 'value="Zz test tag"' not in response.text
    assert f'value="{GOOD["description"]}"' not in response.text
    # It is listed in the table below the form, though — that is how you know it saved.
    assert SLUG in response.text


# ------------------------------------------------- scoped keys reach the write routes


def _key_headers(plaintext: str) -> dict[str, str]:
    """What a server-to-server caller sends. No cookie, no CSRF token."""
    return {"x-api-key": plaintext}


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
