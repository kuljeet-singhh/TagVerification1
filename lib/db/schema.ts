import {
  pgTable,
  text,
  integer,
  real,
  timestamp,
  jsonb,
  boolean,
  index,
  uniqueIndex,
  primaryKey,
} from "drizzle-orm/pg-core";

/**
 * API keys we issue to consuming projects.
 *
 * We never store the key itself. `keyHash` is sha256(plaintext) and `keyPrefix`
 * is the first 8 visible characters, kept only so the dashboard can show
 * "dooh_live_a1b2c3d4..." and so lookup doesn't need a full table scan. The
 * plaintext is shown exactly once, at creation, and is unrecoverable after.
 */
export const apiKeys = pgTable(
  "api_keys",
  {
    id: text("id").primaryKey(),
    name: text("name").notNull(),
    keyHash: text("key_hash").notNull(),
    keyPrefix: text("key_prefix").notNull(),
    rateLimitPerMin: integer("rate_limit_per_min").notNull().default(60),
    revokedAt: timestamp("revoked_at", { withTimezone: true }),
    lastUsedAt: timestamp("last_used_at", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    uniqueIndex("api_keys_key_hash_idx").on(t.keyHash),
    index("api_keys_key_prefix_idx").on(t.keyPrefix),
  ],
);

/**
 * Per-tag decision thresholds.
 *
 * WHY THESE LIVE IN POSTGRES AND THE PROMPTS DO NOT
 * -------------------------------------------------
 * The prompt packs (positives / hard negatives) MUST live in
 * inference/packs.json, because the Space encodes them into text embeddings at
 * startup. Changing a prompt means re-encoding, which means a restart. There is
 * no way around that, and storing prompts here too would just create two
 * sources of truth that silently drift apart.
 *
 * Thresholds are different. They are only ever *comparisons against a returned
 * score*, so the API tier can apply them after the fact. Keeping them here
 * means calibration results and hand-tuning take effect immediately, with no
 * redeploy — which matters, because thresholds are the thing that will actually
 * get tuned repeatedly.
 *
 * So: the Space returns raw `score` / `sigmoid`, and this table decides the
 * verdict. `packsVersionSeen` records which pack fingerprint a threshold was
 * calibrated against, so we can spot thresholds that are stale because someone
 * rewrote the prompts underneath them.
 */
export const tagThresholds = pgTable("tag_thresholds", {
  slug: text("slug").primaryKey(),
  thresholdLow: real("threshold_low").notNull().default(0.3),
  thresholdHigh: real("threshold_high").notNull().default(0.55),
  sigmoidFloor: real("sigmoid_floor").notNull().default(0.005),
  escalate: boolean("escalate").notNull().default(true),

  /** false until calibrate.py has run against labelled images for this tag. */
  calibrated: boolean("calibrated").notNull().default(false),
  /** Measured precision at the chosen threshold, if calibrated. */
  precision: real("precision"),
  recall: real("recall"),
  /** Pack fingerprint these numbers were tuned against. */
  packsVersionSeen: text("packs_version_seen"),
  updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  updatedBy: text("updated_by"),
});

/**
 * Audit log — one row per analyze call.
 *
 * Deliberately stores `imageHash` and NOT the image. Zero storage cost, best
 * privacy, and the hash still gives dedupe plus a result cache.
 *
 * Doubles as the tuning feedback loop: querying for tags that land in the
 * uncertain band too often tells us which thresholds or packs need work.
 */
export const analyses = pgTable(
  "analyses",
  {
    requestId: text("request_id").primaryKey(),
    apiKeyId: text("api_key_id").references(() => apiKeys.id, { onDelete: "set null" }),
    imageHash: text("image_hash").notNull(),
    tagsRequested: jsonb("tags_requested").$type<string[]>().notNull(),
    results: jsonb("results").$type<unknown[]>().notNull(),
    packsVersion: text("packs_version"),
    latencyMs: integer("latency_ms"),
    cached: boolean("cached").notNull().default(false),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // Cache lookups hit (hash, tags) — see lib/db/cache.ts
    index("analyses_image_hash_idx").on(t.imageHash),
    index("analyses_api_key_created_idx").on(t.apiKeyId, t.createdAt),
  ],
);

/**
 * Labelled images for calibration. `label` is the ground truth: does this image
 * genuinely contain `tagSlug`?
 *
 * This is the table that decides whether the product is trustworthy. Until it
 * has ~20-40 rows per tag (half of them deliberate near-misses), every
 * threshold in tag_thresholds is a guess.
 */
export const evalImages = pgTable(
  "eval_images",
  {
    id: text("id").primaryKey(),
    imageHash: text("image_hash").notNull(),
    /** Where the file lives locally / in the eval set. Not user traffic. */
    storageRef: text("storage_ref").notNull(),
    tagSlug: text("tag_slug").notNull(),
    label: boolean("label").notNull(),
    /** Free-text note, e.g. "juice bottle - should NOT read as alcohol". */
    note: text("note"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    uniqueIndex("eval_images_hash_tag_idx").on(t.imageHash, t.tagSlug),
    index("eval_images_tag_idx").on(t.tagSlug),
  ],
);

/**
 * Fixed-window rate limiting. One row per (key, minute).
 *
 * A token bucket in Postgres would be more elegant, but at our volume a fixed
 * window is a single upsert and needs no background sweeper. Old rows are
 * cheap to delete on a schedule.
 */
export const usageCounters = pgTable(
  "usage_counters",
  {
    apiKeyId: text("api_key_id").notNull(),
    /** Truncated to the minute for rate limiting. */
    windowStart: timestamp("window_start", { withTimezone: true }).notNull(),
    count: integer("count").notNull().default(0),
  },
  (t) => [primaryKey({ columns: [t.apiKeyId, t.windowStart] })],
);

export type ApiKey = typeof apiKeys.$inferSelect;
export type TagThreshold = typeof tagThresholds.$inferSelect;
export type Analysis = typeof analyses.$inferSelect;
export type EvalImage = typeof evalImages.$inferSelect;
