"""
The tables, mapped exactly onto the existing Neon schema.

Column names, types and defaults are deliberately byte-identical to what the previous
Drizzle schema created, because this is a rewrite of the application and NOT a migration
of the data. In particular `api_keys.key_hash` stays sha256(plaintext) hex, so every key
already issued keeps working.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    REAL,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    """
    API keys issued to consuming projects.

    We never store the key itself. `key_hash` is sha256(plaintext) and `key_prefix` is the
    first 8 visible characters, kept only so the dashboard can show "dooh_live_a1b2c3d4…"
    and so lookup does not need a full table scan. The plaintext is shown exactly once, at
    creation, and is unrecoverable after.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    # server_default as well as default: these were column DEFAULTs in the schema this
    # replaces, and a python-side default only applies to inserts made through the ORM.
    rate_limit_per_min: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default=text("60")
    )
    #: What this key may do BEYOND analysing images. Empty is the norm and means analyze-only.
    #:
    #: It exists so DOOH can edit the tag catalog over HTTP without holding ADMIN_PASSWORD,
    #: which also mints keys and edits thresholds. The scope is checked in
    #: auth/deps.py::require_tags_write.
    #:
    #: Additive by design: an unscoped key behaves exactly as it did before this column, so
    #: the analyze key DOOH already holds keeps working and cannot rewrite the taxonomy. Issue
    #: a SECOND key for tag writes rather than widening that one — the separation is the whole
    #: point, and it is the property auth/deps.py's refusal to key-authorise tag writes was
    #: protecting in the first place.
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("api_keys_key_hash_idx", "key_hash", unique=True),
        Index("api_keys_key_prefix_idx", "key_prefix"),
    )


class TagThreshold(Base):
    """
    Per-tag decision thresholds.

    WHY THESE ARE SEPARATE FROM THE PROMPTS
    ---------------------------------------
    This used to read "the prompt packs MUST live in inference/packs.json ... there is no way
    around that, and storing prompts here too would just create two sources of truth". The
    prompts now live in `content_tags` below, so that is no longer where they are -- but the
    reasoning was half right and the half that held is worth keeping.

    What was true: the Space encodes prompts into text embeddings at startup, so changing one
    still means a restart. packs.json remains how the prompts reach the model; it is just
    GENERATED from the table now instead of hand-edited. What the objection got right was the
    drift risk, and that is answered by direction rather than by location: one authority
    (Postgres), one direction (export), and nobody editing the file by hand.

    Thresholds are different. They are only ever *comparisons against a returned score*, so
    the API tier can apply them after the fact. Keeping them here means calibration results
    and hand-tuning take effect immediately, with no redeploy — which matters, because
    thresholds are the thing that will actually get tuned repeatedly.

    `packs_version_seen` records which pack fingerprint a threshold was calibrated against,
    so we can spot thresholds that are stale because someone rewrote the prompts underneath
    them. See tagverify/tags/decide.py.
    """

    __tablename__ = "tag_thresholds"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    threshold_low: Mapped[float] = mapped_column(
        REAL, nullable=False, default=0.3, server_default=text("0.3")
    )
    threshold_high: Mapped[float] = mapped_column(
        REAL, nullable=False, default=0.55, server_default=text("0.55")
    )
    sigmoid_floor: Mapped[float] = mapped_column(
        REAL, nullable=False, default=0.005, server_default=text("0.005")
    )
    escalate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    #: False until calibrate.py has run against labelled images for this tag.
    calibrated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Measured precision at the chosen threshold, if calibrated. Note that
    #: apply-calibration stores the Wilson LOWER BOUND here, not the point estimate.
    precision: Mapped[float | None] = mapped_column(REAL)
    recall: Mapped[float | None] = mapped_column(REAL)
    #: Pack fingerprint these numbers were tuned against.
    packs_version_seen: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(Text)


class Analysis(Base):
    """
    Audit log — one row per analyze call.

    Deliberately stores `image_hash` and NOT the image. Zero storage cost, best privacy, and
    the hash still gives dedupe plus a result cache.

    `results` holds the RAW scores from the model, not the decided verdicts. That distinction
    is the whole point of the cache; see tagverify/db/cache.py.
    """

    __tablename__ = "analyses"

    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    api_key_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    image_hash: Mapped[str] = mapped_column(Text, nullable=False)
    tags_requested: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    results: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    packs_version: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Cache lookups hit (hash, tags) — see tagverify/db/cache.py
        Index("analyses_image_hash_idx", "image_hash"),
        Index("analyses_api_key_created_idx", "api_key_id", "created_at"),
    )


class EvalImage(Base):
    """
    Labelled images for calibration. `label` is the ground truth: does this image genuinely
    contain `tag_slug`?

    This is the table that decides whether the product is trustworthy. Until it has ~20-40
    rows per tag (half of them deliberate near-misses), every threshold in tag_thresholds is
    a guess.

    Note: `image_hash` here is a hash of the eval file's PATH, not its bytes, so it is not
    comparable with `analyses.image_hash`. That is inherited behaviour, kept so existing rows
    stay meaningful.
    """

    __tablename__ = "eval_images"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    image_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: Where the file lives locally / in the eval set. Not user traffic.
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    tag_slug: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Free-text note, e.g. "juice bottle - should NOT read as alcohol".
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("eval_images_hash_tag_idx", "image_hash", "tag_slug", unique=True),
        Index("eval_images_tag_idx", "tag_slug"),
    )


class UsageCounter(Base):
    """
    Fixed-window rate limiting. One row per (key, minute).

    A token bucket in Postgres would be more elegant, but at our volume a fixed window is a
    single upsert and needs no background sweeper. Fixed windows allow a burst across a
    boundary (up to 2x the limit in a pathological case); this is abuse protection for a
    free-tier service, not billing enforcement.

    Old rows are pruned by `tagverify.cli prune-usage` and by a daily task in the app lifespan.
    The previous implementation exported a prune function and never called it.
    """

    __tablename__ = "usage_counters"

    api_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: Truncated to the minute for rate limiting.
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    __table_args__ = (PrimaryKeyConstraint("api_key_id", "window_start"),)


class ContentTag(Base):
    """
    The tag catalog: every tag the detector knows, prompts included.

    This table is the source of truth. `inference/packs.json` is generated from it by
    `dooh export-packs` and read by the model tier at import; nothing hand-edits that file
    any more. The direction is one-way on purpose -- the objection recorded above about two
    sources of truth is answered by there being one authority and one direction, not by
    keeping prompts out of the database.

    A tag IS its prompts. `positives` are what we are hunting; `negatives` are hard negatives
    -- things that look confusably similar and are NOT the tag -- and they do most of the
    work. See the authoring rules in packs.json and docs/TAG_CRUD_IMPLEMENTATION.md 1.
    """

    __tablename__ = "content_tags"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    positives: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    negatives: Mapped[list[str]] = mapped_column(JSONB, nullable=False)

    #: Why THESE negatives. Free prose, carried over from the `//negatives` keys in
    #: packs.json, where it records measurements that would otherwise be lost -- e.g. that a
    #: hamburger scored 0.939 for `alcohol` until bar-food scenes were named as negatives.
    rationale: Mapped[str | None] = mapped_column(Text)

    #: Per-tag override of the pack default. Only `revealing_clothing` sets one today.
    sigmoid_floor: Mapped[float | None] = mapped_column(REAL)

    #: 'active' | 'retired'. Text rather than an enum because nothing else here uses one.
    #: Retired never means deleted -- see docs/TAG_CRUD_IMPLEMENTATION.md 6: a hard delete
    #: leaves the slug in DOOH's devices.blocked_tags, where it produces no verdict and turns
    #: every upload to that screen into a review-queue entry.
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default=text("'active'")
    )

    #: Position in the exported file. Reordering tags would change packs.json's bytes and so
    #: its fingerprint, for no reason, so the order is preserved rather than re-derived.
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    #: Passthrough for pack keys this schema does not model (`detect_objects`, `escalate` --
    #: both dead, read by nothing). Kept rather than dropped so that importing the existing
    #: catalog and exporting it again loses nothing: the export is then a REFORMAT, which
    #: needs no recalibration, instead of a semantic change, which would.
    extra: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Free-text provenance, same convention as tag_thresholds: 'admin' | 'seed'.
    updated_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("content_tags_status_idx", "status"),)


class PackHeader(Base):
    """
    The part of `inference/packs.json` that is not tags.

    `$comment`, `prompt_template`, `shared_distractors` and `defaults` -- packs.py's
    FILE_LEVEL_KEYS. They used to live only in the file, which made `dooh export-packs` read
    the file in order to write it. That circular dependency is why publishing failed inside
    the Docker image, which copies three files out of `inference/` and not the pack.

    WHY `body` IS TEXT AND NOT JSONB
    --------------------------------
    Load-bearing, and the reason this table exists at all rather than four typed columns.
    `packs_version` hashes the exported file's BYTES, so a key emitted in a different order
    is a different pack as far as every downstream fingerprint is concerned -- every tag
    would report `calibrated: false` and `apply-calibration` would refuse the calibration
    file, for a change that moved no score. JSONB does not preserve key order (packs.py says
    the same thing about `_EXTRA_ORDER`, for the same reason), and `defaults` carries five
    keys in a fixed order, one of which -- `//sigmoid_floor` -- is prose.

    TEXT sidesteps it entirely: the bytes go in and come out unchanged, `json.loads` keeps
    their order because Python dicts are insertion-ordered, and `render_pack` re-emits them
    exactly as it always did.

    These are not comments, `$comment` aside. detector.py reads `prompt_template`,
    `shared_distractors` and `defaults` at load time -- they are live scoring inputs, so an
    edit here moves every verdict in the system.
    """

    __tablename__ = "pack_header"

    #: Always 1. A CHECK rather than a convention, because two headers has no meaning -- the
    #: exporter would have to pick one, and picking is exactly the ambiguity to refuse.
    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, default=1, server_default=text("1")
    )

    #: The four keys as a JSON object, verbatim. See the class docstring before changing the
    #: type -- TEXT is the whole point.
    body: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Free-text provenance, same convention as content_tags: 'seed' | 'admin'.
    updated_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("id = 1", name="pack_header_singleton"),)
