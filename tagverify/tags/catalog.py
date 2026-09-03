"""
The tag catalog: validation and CRUD over `content_tags`.

One module, called by both entry points (the JSON API and the admin HTMX handlers), so the
rules that decide what a tag may contain cannot drift between them.

WHY THE VALIDATION IS THE INTERESTING PART
------------------------------------------
A tag is not a database row, it is A CATEGORY THE MODEL IS ASKED ABOUT, and the `label` is
the whole of what it is told to look for. So the only validation that touches accuracy is the
one that reads the name. That is why the short-name warning below is not decoration: it is the
last check standing between a vague name and a vague verdict, and unlike a threshold there is
no second knob to turn afterwards.

`description` is no longer part of the question. It is what a screen owner reads in DOOH's
blocked-categories picker, and it is validated as prose rather than as a prompt.

The phrase rules that used to live here went with migration 0003, and they were the bulk of
this module: duplicate phrases, positive/negative overlap, cross-tag phrase ownership,
mid-sentence capitalisation. Every one of them protected SigLIP's rival pools -- a phrase
shared between two tags was dropped from BOTH, silently weakening the competition each relied
on. There are no pools now, so enforcing any of it would be theatre.

A created tag is LIVE at the next request -- no pack, no publish step, no activation gate. So
these checks are the safety model, and there is less of it than there looks: what stops a bad
tag is a well-written description, and the warnings are how we ask for one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.db.models import ContentTag

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def slugify(label: str) -> str:
    """
    The permanent identifier, derived from the name a person typed.

    THE ONLY IMPLEMENTATION, and that is the point of moving it here. There were two, in
    JavaScript -- one in this repo's admin page and one in DOOH's tag dialog -- and neither was
    authoritative because the server only ever validated the result. The admin form now shows
    the derived slug back to the author BEFORE they commit to it, and a preview computed by a
    different copy of the transform than the one that saves is worse than no preview at all.

    Deliberately identical to the JS it replaces, so no slug already in the catalog would
    derive differently and a reader comparing the two finds no surprise.

    The leading-letter strip is the lossy step and the reason `slug_from_label` exists: SLUG_RE
    requires a leading letter, so "3D printing" becomes `d_printing` -- valid, and not what
    anyone wanted. That is not a bug to fix here (guessing at "three_d_printing" would be worse
    and unpredictable); it is why the admin form keeps an override.
    """
    slug = label.strip().casefold()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = re.sub(r"^_+|_+$", "", slug)
    return re.sub(r"^[^a-z]+", "", slug)


def slug_from_label(label: str) -> str:
    """
    `slugify`, refusing in the caller's own terms when it cannot produce one.

    The message says NAME, not slug. Someone using the admin form never typed a slug, and
    "Slug must start with a letter" is advice about a field that is not on their screen.
    """
    slug = slugify(label)
    if not SLUG_RE.match(slug):
        raise TagValidationError(
            f"“{label.strip()}” cannot become an identifier — a tag's name needs a letter in "
            f"it. Rename it, or set the identifier by hand."
        )
    return slug

#: Below this a NAME is too thin to be a prompt. Advisory, never a refusal: a short name is a
#: weak tag, not an invalid one, and a rule that refuses teaches people to pad it to 13
#: characters rather than to name the thing better.
#:
#: It used to guard the description, when the description was what the model was asked. The
#: name is now the whole of what it is told to look for, so this moved with the job. Shorter
#: than the old 40, because a name is a noun phrase and not a sentence: "Alcoholic content" is
#: 17 characters and is a good one, while "dog" and "cat" are the two in this catalog that
#: this warning is actually for.
GOOD_LABEL = 12



#: THE authority, now that there is no model tier holding a MAX_TAGS_PER_CALL to mirror. This
#: used to be the smaller half of a pair, kept below the Space's own per-call ceiling;
#:
#: It is a real ceiling, not a guard rail: DOOH sends the FULL catalog on every analyze call,
#: so exceeding it does not degrade anything, it makes every upload fail at once. Kept BELOW
#: MAX_TAGS_PER_CALL rather than equal to it, so a full catalog still leaves request headroom.
#:
#: Raised from 30 with the measurement recorded beside MAX_TAGS_PER_CALL: an extra tag costs
#: about 1.2ms of a ~540ms call, because the image encode is shared and a verdict is a column
#: slice. The cost of a bigger catalog is bookkeeping — a moved packs_version stales every
#: calibration — not latency and not accuracy.
MAX_TAGS = 100


class TagValidationError(Exception):
    """A user-fixable problem with a submitted tag. The message is shown as-is."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(slots=True)
class ValidatedTag:
    slug: str
    label: str
    description: str
    #: Non-blocking advice. Shown next to the saved tag, never a reason to refuse.
    warnings: list[str] = field(default_factory=list)


async def validate_tag(
    session: AsyncSession,
    *,
    slug: str,
    label: str,
    description: str,
    existing: ContentTag | None = None,
) -> ValidatedTag:
    """Validate a create (existing=None) or an update. Raises TagValidationError."""
    # Trimmed and lowercased rather than refused: a slug is an identifier, and "Pet_Supplies"
    # is a typo, not a decision. Normalising also means `Pet_supplies` cannot be created
    # alongside `pet_supplies` -- two rows a screen owner would read as one blocked category.
    slug = slug.strip().casefold()
    if not SLUG_RE.match(slug):
        raise TagValidationError(
            "Slug must start with a letter and contain only lowercase letters, digits and "
            "underscores."
        )

    if existing is None:
        clash = await session.get(ContentTag, slug)
        if clash is not None:
            # Including a retired one: the slug still owns that tag's eval images and its
            # calibration row, and a reused slug would silently inherit both.
            #
            # WORDED FROM WHAT THEY ACTUALLY TYPED. The admin form derives the slug from the
            # label, so telling someone who typed "Alcoholic content" that "the slug 'alcohol'
            # is in use" names a field they never filled in and a word they never wrote. But
            # the override exists, and "'X' becomes the identifier 'Y'" is equally wrong when
            # they typed Y themselves.
            #
            # Comparing against the derivation is how this stays true either way, and it needs
            # no extra parameter threaded through: if the slug IS what the label produces, it
            # was derived (or typed to match, which reads the same).
            state = "retired" if clash.status == "retired" else "in use"
            derived = slug == slugify(label)
            raise TagValidationError(
                (
                    f"“{label.strip()}” becomes the identifier “{slug}”, which is already "
                    f"{state} (“{clash.label}”). Choose a different name, or set the "
                    f"identifier by hand."
                )
                if derived
                else (
                    f"The identifier “{slug}” is already {state} (“{clash.label}”). "
                    f"Retiring a tag does not release it — choose another."
                )
            )

        active = await session.scalar(
            select(func.count()).select_from(ContentTag).where(ContentTag.status == "active")
        )
        if (active or 0) >= MAX_TAGS:
            raise TagValidationError(
                f"The catalog is full at {MAX_TAGS} tags. Every analyze call sends the whole "
                f"catalog, and the model refuses more than {MAX_TAGS} in one request — so "
                f"adding another would fail every upload, not just this tag. Retire one first."
            )
    elif existing.slug != slug:
        raise TagValidationError("A tag's slug cannot be changed. Retire it and create a new one.")

    label = label.strip()
    description = description.strip()
    if not label:
        raise TagValidationError(
            "A name is required — it is what the model is asked to look for, and what a "
            "screen owner sees."
        )

    # The description is NOT required, and has not been since the model stopped reading it. It
    # is what a screen owner reads under the name in DOOH's blocked-categories picker, which
    # renders nothing at all when it is blank (`components/ui/multi-select.tsx`) -- so refusing
    # a save over it would be a chore with no verdict and no customer behind it. Stripped
    # rather than left as typed, so "   " is stored as "" and the picker's truthiness check
    # sees what it expects.

    warnings: list[str] = []

    if len(label) < GOOD_LABEL:
        # THE check. The name is the whole of what the model is told to look for -- the
        # instruction in scoring/prompt.py carries everything about HOW to decide, and the
        # catalog supplies only WHICH categories -- so a vague name is a vague verdict, and
        # unlike a threshold there is no second knob to turn afterwards.
        warnings.append(
            "The name is short. It is the whole of what the model is told to look for, so "
            "“Dogs and puppies” gives it more to go on than “dog”."
        )

    return ValidatedTag(
        slug=slug,
        label=label,
        description=description,
        warnings=warnings,
    )


async def list_tags(session: AsyncSession, *, include_retired: bool = True) -> list[ContentTag]:
    stmt = select(ContentTag).order_by(ContentTag.sort_order, ContentTag.slug)
    if not include_retired:
        stmt = stmt.where(ContentTag.status == "active")
    return list((await session.scalars(stmt)).all())


async def get_tag(session: AsyncSession, slug: str) -> ContentTag | None:
    return await session.get(ContentTag, slug.strip().casefold())


async def create_tag(session: AsyncSession, *, actor: str = "admin", **fields) -> ValidatedTag:
    """Validate and insert. The caller commits and invalidates the memos."""
    valid = await validate_tag(session, **fields)

    last = await session.scalar(select(func.max(ContentTag.sort_order)))
    session.add(
        ContentTag(
            slug=valid.slug,
            label=valid.label,
            description=valid.description,
            status="active",
            sort_order=(last or 0) + 1,
            extra={},
            created_by=actor,
            updated_by=actor,
        )
    )
    await session.flush()
    return valid


async def update_tag(
    session: AsyncSession, slug: str, /, *, actor: str = "admin", **fields
) -> ValidatedTag:
    """
    Validate and apply an edit. The caller commits and invalidates the memos.

    Edit exists rather than delete-and-recreate because a slug owns its eval images and its
    measured thresholds; recreating would strand both. What is edited now is the description,
    which IS the prompt -- so an edit here moves `packs_version` and correctly stales every
    verdict decided against the old wording.

    The `/` is load-bearing, not style. `validate_tag` REQUIRES a `slug` field -- that is how
    it rejects a rename, by comparing the submitted value against `existing.slug`. Without
    the marker, a caller's `slug=` binds to this positional parameter instead of landing in
    `**fields`, and every call raises "got multiple values for argument 'slug'" before
    validate_tag is ever entered. Positional-only keeps the two roles apart: the positional
    says WHICH row, the keyword says what the submitter claims the slug is.
    """
    row = await get_tag(session, slug)
    if row is None:
        raise TagValidationError(f"No tag “{slug}”.")

    valid = await validate_tag(session, existing=row, **fields)

    row.label = valid.label
    row.description = valid.description
    row.updated_at = datetime.now(UTC)
    row.updated_by = actor
    await session.flush()
    return valid


async def retire_tag(session: AsyncSession, slug: str, *, actor: str = "admin") -> ContentTag:
    """
    Retire, never delete.

    A hard delete would leave the slug sitting in DOOH's `devices.blocked_tags`, which only
    self-heals on the next write to that device. Until someone re-saves the screen the list is
    non-empty, so the fast path is skipped; the slug is missing from the catalog, so it yields
    no verdict; and the policy falls through to NOT_VERIFIED. The screen would generate review
    entries for every upload, forever, with nothing pointing at why.
    """
    row = await get_tag(session, slug)
    if row is None:
        raise TagValidationError(f"No tag “{slug}”.")
    row.status = "retired"
    row.updated_at = datetime.now(UTC)
    row.updated_by = actor
    await session.flush()
    return row


async def restore_tag(session: AsyncSession, slug: str, *, actor: str = "admin") -> ContentTag:
    """
    Put a retired tag back in the catalog. The inverse of `retire_tag`, and the reason
    retirement is allowed to be non-destructive in the first place.

    Retiring kept the row, its `eval_images` and its `tag_thresholds` -- that is the whole
    argument for "retire, never delete" -- and then offered no way to use any of it again, so
    a misclick was permanent and a category could never come back when policy changed.

    WHAT THIS COSTS, because it is not visible from the button:
    the tag re-enters `GET /api/v1/tags` and the prompt, so `packs_version` moves and every
    cached verdict here and in dooh-backend re-keys -- every creative is analysed once more, at
    the model's price. Retiring costs exactly the same; it is a property of changing the
    catalog, not of this direction.

    ITS CALIBRATION COMES BACK READING SUPERSEDED, AND THAT IS CORRECT. The stored
    `tag_thresholds` row survived, but `decide.effective_calibrated` compares its
    `packs_version_seen` against the live fingerprint, which moved when the tag left and moves
    again now. A measurement taken against a different catalog is stale, not calibrated --
    AGENTS.md rule 4. Do not "restore" the calibration alongside the tag.

    It does NOT release the slug: this revives the same row, so `validate_tag`'s reservation
    (a retired slug is still taken) is untouched and this is not a way to free a name.
    """
    row = await get_tag(session, slug)
    if row is None:
        raise TagValidationError(f"No tag “{slug}”.")

    if row.status != "retired":
        # Never a silent no-op. The button only renders on a retired row, so reaching here
        # means the table was stale -- say so rather than reporting a success that did nothing.
        raise TagValidationError(
            f"“{row.label}” is already active. Reload the page."
        )

    # THE ONE CHECK THAT IS NOT A MIRROR OF `retire_tag`. `validate_tag` refuses a new tag at
    # MAX_TAGS active, but that runs on CREATE -- restoring would walk straight past it, and
    # retire five / create five / restore five would leave the catalog over the cap. Which is
    # not a soft limit: see MAX_TAGS above, exceeding it fails every upload at once rather than
    # degrading anything.
    active = await session.scalar(
        select(func.count()).select_from(ContentTag).where(ContentTag.status == "active")
    )
    if (active or 0) >= MAX_TAGS:
        raise TagValidationError(
            f"The catalog is full at {MAX_TAGS} active tags. Every analyze call sends the "
            f"whole catalog, and the model refuses more than {MAX_TAGS} in one request — so "
            f"bringing this one back would fail every upload, not just this tag. Retire "
            f"another first."
        )

    row.status = "active"
    row.updated_at = datetime.now(UTC)
    row.updated_by = actor
    await session.flush()
    return row


# ------------------------------------------------------------------ publish state
