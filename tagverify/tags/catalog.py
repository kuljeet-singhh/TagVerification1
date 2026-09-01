"""
The tag catalog: validation and CRUD over `content_tags`.

One module, called by both entry points (the JSON API and the admin HTMX handlers), so the
rules that decide what a tag may contain cannot drift between them.

WHY THE VALIDATION IS THE INTERESTING PART
------------------------------------------
A tag is not a database row, it is a list of positive phrases plus HARD NEGATIVES, and the
negatives do most of the work. From the pack file's own header, with the measurement
attached: with only "a tin of baby formula" as a negative, infant formula scored 0.709 --
wrongly present. Adding "a scoop of baby formula milk powder" dropped it to 0.224 while real
whey protein stayed at 0.999. Asymmetric phrasing rigs the softmax toward whichever side owns
the words that describe the photo.

A created tag goes live as soon as the pack is published, with no measured thresholds and no
activation gate. So these checks, plus the publish runbook, ARE the safety model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.db.models import ContentTag

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Below this a description is too thin to be a prompt. Advisory, never a refusal.

#: Below this a description is too thin to be a prompt. Advisory, never a refusal: a short
#: description is a weak tag, not an invalid one, and a rule that refuses teaches people to
#: pad it to 41 characters rather than to write a better one.
GOOD_DESCRIPTION = 40



#: Mirrors MAX_TAGS_PER_CALL in inference/app.py, which is the authority. Duplicated rather
#: than imported because tagverify must never import from the model tier (see AGENTS.md);
#: this follows the same pattern fetch_demo_eval.py uses for MIN_PER_CLASS.
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
    positives: list[str]
    negatives: list[str]
    rationale: str | None
    sigmoid_floor: float | None
    #: Non-blocking advice. Shown next to the saved tag, never a reason to refuse.
    warnings: list[str] = field(default_factory=list)


def normalise_phrase(raw: str) -> str:
    """
    Phrases are substituted into "This is a photo of {}." so they must read as a noun phrase
    mid-sentence. Trailing punctuation is stripped rather than rejected -- it is a typo, not a
    decision, and refusing the whole form over one full stop helps nobody.

    Case is deliberately NOT forced. Every phrase shipped today is lowercase, but a brand name
    is a legitimate reason to capitalise and silently lowercasing it would be a content change
    we were never asked to make. A leading capital earns a warning instead.
    """
    return re.sub(r"[.\s]+$", "", raw.strip())


def _clean_list(raw: list[str], what: str, minimum: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        phrase = normalise_phrase(item)
        if not phrase:
            continue
        if phrase.casefold() in seen:
            raise TagValidationError(f"Duplicate {what[:-1]}: “{phrase}”.")
        seen.add(phrase.casefold())
        out.append(phrase)
    if len(out) < minimum:
        raise TagValidationError(
            f"A tag needs at least {minimum} {what} — this one has {len(out)}. "
            f"The {what} are what the model compares against; too few and it has nothing to "
            f"weigh the image against."
        )
    return out


async def _existing_positives(
    session: AsyncSession, *, exclude_slug: str | None = None
) -> dict[str, str]:
    """
    Every other ACTIVE tag's positives, mapped phrase -> owning slug.

    Retired tags are excluded on purpose. The collision matters because two tags sharing a
    phrase lose it from both their rival pools -- but a retired tag is not exported, so its
    phrases are not in the pack and cannot be in anyone's pool. Blocking on them would refuse
    wording that is genuinely free. (The SLUG check above deliberately does the opposite and
    does include retired rows: the row still exists, and reusing its slug would inherit its
    eval images and thresholds.)
    """
    stmt = select(ContentTag.slug, ContentTag.positives).where(ContentTag.status == "active")
    rows = (await session.execute(stmt)).all()
    owned: dict[str, str] = {}
    for slug, positives in rows:
        if slug == exclude_slug:
            continue
        for phrase in positives or []:
            owned.setdefault(normalise_phrase(phrase).casefold(), slug)
    return owned


async def validate_tag(
    session: AsyncSession,
    *,
    slug: str,
    label: str,
    description: str,
    positives: list[str],
    negatives: list[str],
    rationale: str | None = None,
    sigmoid_floor: float | None = None,
    existing: ContentTag | None = None,
) -> ValidatedTag:
    """Validate a create (existing=None) or an update. Raises TagValidationError."""
    # Trimmed and lowercased rather than refused: a slug is an identifier, and "Pet_Supplies"
    # is a typo, not a decision. Normalising also means `Pet_supplies` cannot be created
    # alongside `pet_supplies` and quietly compete with it in the same rival pool.
    slug = slug.strip().casefold()
    if not SLUG_RE.match(slug):
        raise TagValidationError(
            "Slug must start with a letter and contain only lowercase letters, digits and "
            "underscores."
        )

    if existing is None:
        clash = await session.get(ContentTag, slug)
        if clash is not None:
            # Including a retired one: the slug still owns that tag's eval images and
            # thresholds, and a reused slug would silently inherit both.
            state = "retired" if clash.status == "retired" else "in use"
            raise TagValidationError(f"The slug “{slug}” is already {state}.")

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
        raise TagValidationError("Label is required — it is what a screen owner sees.")
    if not description:
        raise TagValidationError("Description is required — it is what a screen owner sees.")

    # No minimum. The phrases were SigLIP's question -- it ranked an image against them and
    # could not answer without a pool -- and SigLIP is gone. They are kept as nullable columns
    # so a rollback to `main` is a checkout rather than a re-authoring exercise, and validated
    # if supplied so a half-filled tag cannot poison that rollback.
    positives = _clean_list(positives, "positives", 0)
    negatives = _clean_list(negatives, "negatives", 0)

    overlap = {p.casefold() for p in positives} & {n.casefold() for n in negatives}
    if overlap:
        raise TagValidationError(
            f"“{sorted(overlap)[0]}” is listed as both a positive and a negative."
        )

    # The rule that comes from the code rather than the docs. detector.py interns prompts by
    # exact string and subtracts a tag's own phrases from its rival pool, so a phrase shared
    # between two tags is removed from BOTH their rival sets -- silently weakening cross-tag
    # competition for each. That competition is what took a gym poster's `gambling` score from
    # 0.974 to 0.061, so this is a correctness rule, not tidiness.
    owned = await _existing_positives(session, exclude_slug=existing.slug if existing else None)
    for phrase in positives:
        owner = owned.get(phrase.casefold())
        if owner:
            raise TagValidationError(
                f"“{phrase}” is already a positive for “{owner}”. A phrase shared between two "
                f"tags is dropped from both of their rival pools, which weakens the "
                f"competition both rely on. Word it differently."
            )

    if sigmoid_floor is not None and not 0 <= sigmoid_floor <= 1:
        raise TagValidationError("Sigmoid floor must be between 0 and 1.")

    warnings: list[str] = []


    if len(description) < GOOD_DESCRIPTION:
        # Load-bearing under a VLM in a way it never was under SigLIP, where it was only the
        # blurb a screen owner read in the picker. It is now the whole question the model is
        # asked, so a vague one is a vague verdict — and unlike a threshold there is no second
        # knob to turn afterwards.
        warnings.append(
            "The description is short. It is what the model is actually asked, so name the "
            "thing, name its boundary, and say what does NOT count."
        )
    rationale = (rationale or "").strip() or None
    # Only worth asking for when there ARE negatives to justify. On a phrase-less tag it read
    # "say why these negatives were chosen" about an empty list, which is the kind of advice
    # that teaches people to stop reading the warnings.
    if not rationale and negatives:
        warnings.append(
            "No rationale recorded. Saying why these negatives were chosen is what stops the "
            "next person undoing a fix they cannot see."
        )
    capitalised = [p for p in positives + negatives if p[:1].isupper()]
    if capitalised:
        warnings.append(
            f"“{capitalised[0]}” starts with a capital. Phrases are substituted into "
            f"“This is a photo of {{}}.”, so they read mid-sentence."
        )

    return ValidatedTag(
        slug=slug,
        label=label,
        description=description,
        positives=positives,
        negatives=negatives,
        rationale=rationale,
        sigmoid_floor=sigmoid_floor,
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
            positives=valid.positives,
            negatives=valid.negatives,
            rationale=valid.rationale,
            sigmoid_floor=valid.sigmoid_floor,
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

    # The floor is MEASURED (dooh apply-calibration), never typed into the edit form, so
    # neither the admin form nor TagPayload sends one. Absent has to mean "leave it alone":
    # the assignment below is unconditional, so without this, editing a tag's negatives --
    # the whole reason edit exists -- silently discards the calibrated floor that rule 3
    # checks BEFORE the score bands.
    if fields.get("sigmoid_floor") is None:
        fields["sigmoid_floor"] = row.sigmoid_floor

    valid = await validate_tag(session, existing=row, **fields)

    row.label = valid.label
    row.description = valid.description
    row.positives = valid.positives
    row.negatives = valid.negatives
    row.rationale = valid.rationale
    row.sigmoid_floor = valid.sigmoid_floor
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


# ------------------------------------------------------------------ publish state
