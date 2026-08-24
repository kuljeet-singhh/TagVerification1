"""
The five tables, mapped exactly onto the existing Neon schema.

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
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
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

    WHY THESE LIVE IN POSTGRES AND THE PROMPTS DO NOT
    -------------------------------------------------
    The prompt packs (positives / hard negatives) MUST live in inference/packs.json, because
    the Space encodes them into text embeddings at startup. Changing a prompt means
    re-encoding, which means a restart. There is no way around that, and storing prompts here
    too would just create two sources of truth that silently drift apart.

    Thresholds are different. They are only ever *comparisons against a returned score*, so
    the API tier can apply them after the fact. Keeping them here means calibration results
    and hand-tuning take effect immediately, with no redeploy — which matters, because
    thresholds are the thing that will actually get tuned repeatedly.

    `packs_version_seen` records which pack fingerprint a threshold was calibrated against,
    so we can spot thresholds that are stale because someone rewrote the prompts underneath
    them. See dooh/tags/decide.py.
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
    is the whole point of the cache; see dooh/db/cache.py.
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
        # Cache lookups hit (hash, tags) — see dooh/db/cache.py
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

    Old rows are pruned by `dooh.cli prune-usage` and by a daily task in the app lifespan.
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
