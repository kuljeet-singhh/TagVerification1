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
    Per-tag CALIBRATION state. The name is the table's, and the table's name is history.

    It held decision thresholds: two cutoffs and a resemblance floor applied to a similarity
    score. Migration 0004 dropped all three, plus `escalate`, because `decide()` stopped
    reading them when the ranking model was removed in f357a1c -- a similarity needed
    calibrating before it meant anything, and a read model answers the question instead.

    WHAT IS LEFT, AND WHY IT IS NOT THE SAME THING
    ----------------------------------------------
    One question: has this tag ever been measured against labelled images, and does that
    measurement still describe the prompts we are serving? `packs_version_seen` is the second
    half and is what makes the first half honest -- a calibration taken against a different
    catalog is stale, not calibrated, and `decide.effective_calibrated` is the only place that
    comparison is made. AGENTS.md rule 4.

    NOTHING WRITES THIS TABLE TODAY. `dooh apply-calibration` went with the ranking model and
    the admin hand-edit form went with the thresholds, so every row here is a historical
    measurement and all of them are stale against the live fingerprint. That is reported
    truthfully rather than hidden. `docs/VLM_SCORING.md` §5.2 has the eval harness writing
    these columns back; the table is kept, unchanged in shape, so that lands as a writer and
    not as a migration.
    """

    __tablename__ = "tag_thresholds"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)

    #: False until this tag has been measured against labelled images. Never read raw: pass it
    #: through `decide.effective_calibrated`, which also checks `packs_version_seen`.
    calibrated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Measured precision, if calibrated. The rows written by the retired `apply-calibration`
    #: hold the Wilson LOWER BOUND here, not the point estimate.
    precision: Mapped[float | None] = mapped_column(REAL)
    recall: Mapped[float | None] = mapped_column(REAL)
    #: The `packs_version` these numbers were measured against. Null means "measured, but we
    #: did not record against what" -- treated as current, because inventing staleness we
    #: cannot demonstrate is its own kind of dishonesty.
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
    The tag catalog: every category the scorer knows, and the name each one is asked by.

    This table is the source of truth, and now the ONLY one -- there is no exported file and
    no publish step. A row written here is live at the next request, because the scorer reads
    its prompt per request rather than holding a compiled pack.

    A TAG IS ITS NAME. `label` is sent to the model as `- alcohol: Alcoholic content` and is
    the whole of what it is told to look for, so name quality IS tag quality; see AGENTS.md
    rule 6. It is also half of `packs_version` -- `scoring/prompt.py:catalog_fingerprint`
    hashes every active tag's slug and name -- so RENAMING one invalidates verdicts decided
    under the old name, which is exactly what should happen.

    `description` is NOT part of the question and is in neither fingerprint. It is what a
    screen owner reads under the name when choosing what to block, it is optional, and editing
    it re-scores nothing. It was the prompt until the name-only change, and every docstring in
    this repo said so -- which is why this one says the opposite loudly rather than quietly.

    `positives`, `negatives`, `rationale` and `sigmoid_floor` were dropped in migration 0003.
    They were SigLIP's question: it ranked an image against a pool of phrases and could not
    answer without one. Nothing has read them since SigLIP was removed in f357a1c, and they
    were never in either fingerprint. The packs themselves are kept in
    `inference/phrase_packs_archive.json`, beside gate_cache.json, for the same reason.
    """

    __tablename__ = "content_tags"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)

    #: THE PROMPT. Not a display label -- see the class docstring.
    label: Mapped[str] = mapped_column(Text, nullable=False)

    #: A blurb for the screen owner, and nothing the model sees. NOT NULL but freely empty:
    #: "" is the absent case, because the picker that renders it treats blank and missing
    #: identically and a nullable column would buy a distinction nothing consumes.
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 'active' | 'retired'. Text rather than an enum because nothing else here uses one.
    #: Retired never means deleted -- see docs/TAG_CRUD_IMPLEMENTATION.md 6: a hard delete
    #: leaves the slug in DOOH's devices.blocked_tags, where it produces no verdict and turns
    #: every upload to that screen into a review-queue entry.
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default=text("'active'")
    )

    #: Display order in the admin table and the playground picker, and nothing more.
    #:
    #: It once set the position of a tag in the exported pack file, so reordering rewrote that
    #: file's bytes and moved its fingerprint — a purely cosmetic change re-keying every cached
    #: verdict. That cannot happen now: `scoring/prompt.py::catalog_fingerprint` sorts by slug
    #: precisely so no ordering, stored or incidental, can reach a fingerprint.
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
    The part of the retired published pack that was not tags.

    `$comment`, `prompt_template`, `shared_distractors` and `defaults` -- the pack's
    file-level keys. They used to live only in the file, which made `dooh export-packs` read
    the file in order to write it. That circular dependency is why publishing worked from a
    checkout and failed from a deployment, which never carried the pack.

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

    NOTHING READS THIS TABLE NOW. `prompt_template`, `shared_distractors` and `defaults`
    were live scoring inputs while a detector loaded them at startup; that detector, the pack
    it loaded and the export that wrote it are all gone. The table is kept because dropping
    one earns nothing and a migration to do it is a migration to review.
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
