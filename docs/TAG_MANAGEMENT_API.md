# Tag Management API — Design Document

**Feature:** DOOH admins create content tags through an API in `profile`; screen owners then block them.
**Spans:** `profile` (owns the API and the taxonomy) · `dooh-backend` (proxies, admin-gated) · `dooh-frontend` (admin UI)
**Status:** ⬜️ **Superseded** — see [TAG_CRUD_IMPLEMENTATION.md](./TAG_CRUD_IMPLEMENTATION.md)
for what was actually built. Kept for its measurements, which still hold and are the reason
this file is here. What changed: §4.1's "Postgres becomes the source of truth" was
adopted, but §4.2's `draft`/`active`/`retired` state machine and §4.4's evidence-gated
`activate` endpoint were **not** — a created tag is live at the next request. Its file
paths are stale throughout: the package is `tagverify/`, not `dooh/`.

> **§1's premise no longer describes the system.** "A tag is a prompt pack, not a database
> row" was true of SigLIP, which ranked an image against a pool of phrases. That model was
> removed in `f357a1c`, and migration `0003` dropped the `positives`, `negatives`,
> `rationale` and `sigmoid_floor` columns with it. **A tag is now its description** — one
> sentence, sent to the model verbatim, and the whole of what is asked.
>
> Read §1–§2 as history, and read the two authoring rules in §1 as advice about what a
> description must say rather than about a phrase pool: mirror the confusable case, and name
> the neighbour that actually gets mistaken for the thing. The 0.709 → 0.224 measurement is
> why the exclusion clause in a description earns its keep. The phrases themselves are
> archived at `inference/phrase_packs_archive.json`.

**Written:** 2026-08-24
**Related:** [SCREEN_CONTENT_POLICY.md](./SCREEN_CONTENT_POLICY.md) — the feature this extends

---

## 0. The question this document answers

> Can we build a CRUD API for tags, use it from DOOH admin, and will a tag an admin creates
> accurately detect content in images and video?

**Yes to the API. No to the accuracy — not on the day the tag is created.**

That is not a limitation of the API design. It is what a tag *is*, and the design below makes a new
tag safe by default rather than pretending otherwise.

---

## 1. A tag is a prompt pack, not a database row

In `inference/packs.json`, a tag is a list of positive phrases plus **hard negatives** — and that
is its entire definition. The model has no other concept of it. `detector.py` puts it plainly:

> We never train anything. **Adding a tag = writing sentences.**

The file's own header carries two authoring rules, both with measured evidence behind them:

- **RULE 1 — mirror the phrasings.** If a positive says "a scoop of protein powder", a negative
  must say "a scoop of *other thing*". Measured: with only "a tin of baby formula" as the negative,
  infant formula scored 0.709 (wrongly present). Adding "a scoop of baby formula milk powder"
  dropped it to 0.224 while real whey protein stayed at 0.999.
- **RULE 2 — name the confusable neighbour**, not something random. "A plain brick wall" as an
  alcohol negative teaches nothing; "a bottle of fruit juice" is what actually gets mistaken for it.

**The negatives do most of the work.** A tag-creation form that asks only for a label and a
description will produce tags that do not work, and there is no way for the API to compensate.

---

## 2. Why a newly created tag cannot be trusted

Two facts, both measured on 2026-08-24 (see SCREEN_CONTENT_POLICY.md §4.3):

1. **A new tag has no thresholds, so it runs on the guessed `0.30 / 0.55` defaults** — and since
   the §6.4 policy change every `present` verdict blocks, calibrated or not. The full-catalogue
   sweep measured that regime at **152 cross-tag false blocks across ~440 images**. A tag created
   this morning lands straight in it and begins refusing paying advertisers.
2. **Fixing that needs ≥8 positive and ≥8 negative labelled images** (`MIN_PER_CLASS` in
   `calibrate.py`), a calibration run, **and** a cross-tag sweep. Calibration alone is not enough:
   applying all 20 calibrated tags on 2026-08-24 measured as a *net regression* (cross-tag false
   blocks 152 → 211, mean recall 0.665 → 0.652), because `calibrate.py` optimises against each
   tag's own 24 images, which contain no examples of the other nineteen categories.

This is the whole reason for the `draft` state in §4.

---

## 3. Two measurements that change the design

### 3.1 Re-encoding does not require a restart

`dooh/db/models.py` currently states:

> The prompt packs (positives / hard negatives) MUST live in inference/packs.json, because the
> Space encodes them into text embeddings at startup. Changing a prompt means re-encoding, which
> means a restart. **There is no way around that**, and storing prompts here too would just create
> two sources of truth that silently drift apart.

Measured on an M-series Mac:

```
re-encoding all 266 prompts        : 4797 ms
encoding one new tag's 14 prompts  :  367 ms
```

`Detector._row()` is append-only and `text_embeds` is a concatenable tensor, so an in-process
rebuild is mechanically straightforward. **The first half of that comment is no longer true** once
a reload path exists. The second half — two sources of truth drifting — is a genuine risk and is
answered in §4.1 by making Postgres authoritative and `packs.json` a seed only.

### 3.2 Adding a tag disturbs the others far less than the docs imply

A 21st tag (`pet_supplies`) was added to a copy of the pack and 60 images were re-scored against
all 20 original tags with both packs:

| | result |
|---|---|
| `packs_version` | `c14ac5bdcfa9` → `825c28ee0ea7` |
| scores that moved > 0.01 | **12 / 1200 (1.0%)** |
| verdict band flips | **3 / 1200 (0.2%)** |

All three flips were knife-edge cases on tags whose calibrated band is degenerate (`low == high`),
e.g. `beauty_cosmetics` 0.863 → 0.862 across a 0.8629 cutoff.

So the cross-tag competition effect (`_rival_rows`, AGENTS.md rule 6) is real but **numerically
small for an unrelated tag**. The expensive consequence is bookkeeping, not scores:

- a new `packs_version` makes every `tag_thresholds.packs_version_seen` stale, so **every existing
  tag reports `calibrated: false`** until re-calibrated;
- `dooh apply-calibration` hard-refuses the previous `calibration.json` (fingerprint mismatch);
- the `analyses` raw-score cache keys on `packs_version`, so it goes cold for every image.

---

## 4. Design

### 4.1 Postgres becomes the source of truth

A new `content_tags` table in `profile/dooh/db/models.py`:

| column | type | notes |
|---|---|---|
| `slug` | Text | PK, `^[a-z0-9_]+$` |
| `label`, `description` | Text | shown to owners |
| `positives`, `negatives` | JSONB | the prompt pack |
| `status` | Text | `draft` / `active` / `retired` — see §4.2 |
| `created_at`, `created_by`, `updated_at`, `updated_by` | | audit pair, as `TagThreshold` has |

Match the existing schema style: `Mapped[...]` / `mapped_column`, `Text` never `String(n)`, `JSONB`
for lists, `DateTime(timezone=True)` with `server_default=func.now()`, both `default=` and
`server_default=`. There is **no migration tooling** — `docs/schema.sql` is regenerated by hand.

`packs.json` becomes the **seed**: a `dooh seed-tags` command imports it once, after which the file
is historical. One authority, one direction, never a merge — which is how the "two sources of
truth" objection is answered.

> Do not carry over `detect_objects` or `escalate` into the new schema. Grep confirms nothing reads
> `detect_objects` at all, and `escalate` is written to a column `decide()` never consults.

### 4.2 Three states, and only one of them can block

| status | in `GET /api/v1/tags` | scored | a screen can block with it |
|---|---|---|---|
| `draft` | **no** | yes | no |
| `active` | yes | yes | yes |
| `retired` | no | no | existing screens keep it until they re-save |

**Every created tag starts as `draft`.** It is scored, so an admin can watch it on the playground
and across the eval set, but it never reaches the owner picker — so it cannot refuse an advertiser
on guessed cutoffs. Promotion is a separate, deliberate action.

`POST /api/v1/tags/{slug}/activate` must **refuse unless the evidence exists**: ≥8 eval images per
side and a calibration row at the live `packs_version`. The error should name what is missing.
Anything weaker and `draft` is decoration.

> This deliberately does **not** reuse the `calibrated` flag as the gate. §6.4 decided that every
> `present` blocks regardless of calibration, and re-gating on it would silently disable the 12
> tags currently running on defaults. `status` is orthogonal and explicit.

### 4.3 Pushing the pack to the Space

A new authenticated `gr.api("reload_packs")` in `inference/app.py` that accepts a full pack,
rebuilds prompt rows, `text_embeds` and `_rival_rows`, then swaps Detector state atomically under a
lock — `demo.queue(max_size=32)` means an `analyze` can be in flight mid-rebuild. `_encode_texts`
is reusable as-is; it already takes an arbitrary phrase list.

Three things this must get right:

- **`packs_version` must be computed from the pushed content, not from disk.** `scorer_version()`
  hashes `packs.json` bytes today; extend it to accept bytes — do not copy it, `versioning.py` is
  explicit about being the single source of truth. Otherwise the fingerprint stays still while
  every score moves, which is exactly the failure that module exists to prevent.
- **Persistence.** The Space's filesystem is ephemeral, it sleeps after 48h and rebuilds from git.
  The web tier must re-push on Space startup. Treat the Space as a cache of the DB, never a store.
- **Auth.** `gr.api` endpoints have no authorisation; the Space is protected only by being private.
  A mutating endpoint needs a shared secret, and it must not be the read-scoped `HF_TOKEN`.

### 4.4 The profile API surface

Follow `dooh/web/admin.py` conventions exactly: `require_admin(request)` as the **first line of
every mutating handler** (authorisation lives on the handler, never on the page), explicit
`session.commit()`, and explicit invalidation of in-process memos (`reset_caches()`,
`invalidate_threshold_cache()`).

| route | behaviour |
|---|---|
| `GET /api/v1/tags` | unchanged shape, now filtered to `status='active'` |
| `POST /api/v1/tags` | create as `draft`; validate slug, uniqueness, **≥4 positives and ≥4 negatives** |
| `DELETE /api/v1/tags/{slug}` | retire, never hard-delete — see §4.5 |
| `POST /api/v1/tags/{slug}/activate` | the evidence gate |

The `/admin` UI gains a tags tab mirroring the `thresholds_panel` / `threshold_row` HTMX pattern:
the row is its own swap target, and user-fixable errors return **200 with the partial re-rendered**,
never a 4xx that HTMX drops on the floor.

### 4.5 Deletion must mean retirement

A hard delete leaves dead slugs in `devices.blocked_tags`. Tracing `resolveBlockedTagsForWrite` in
`dooh-backend/src/devices/devices.service.ts`, that column **only self-heals on the next write to
that device**. Until someone re-saves the screen:

- `blockedTags` is non-empty, so `verifyBatch` does not take the fast `!blockedTags.length` path;
- the slug is missing from `catalogMap`, so it produces no verdict;
- `evaluatePolicy` falls through to `NOT_VERIFIED`.

The result is **a screen quietly generating review-queue entries forever**. So: retire upstream, and
have DOOH sweep `devices.blocked_tags` when a slug leaves the catalog.

### 4.6 DOOH side

- `content-tags.controller.ts` — keep `@Public()` on `@Get()` (the marketplace detail page is
  anonymous). Put `@Roles(UserRole.ADMIN)` at **method** level on the writes; a class-level
  decorator would break the public read.
- Add `invalidateTagCatalog()` to `ContentVerificationService` (set `catalogFetchedAtMs = 0`) and
  call it after any successful upstream write. Without it a new tag takes up to 5 minutes to appear.
- Frontend: `app/(admin)/admin/content-tags/page.tsx` + `components/admin/content-tags-client.tsx`,
  mirroring `admin-users-client.tsx` (hand-rolled responsive table + dialog + react-hook-form/zod,
  CSS Modules). Add to `adminNavItems` in `lib/navigation.ts`; extend `contentTagService`.
- **Drop `CONTENT_TAGS_STALE_MS` from 1 hour.** Its comment ("Matches the backend's 1h catalog
  cache") is already wrong — the backend is 5 minutes — and an hour is untenable once tags mutate.

---

## 5. Constraints and open questions

### 5.1 Authorisation — needs a decision before building

DOOH holds a **single unscoped profile API key**. `api_keys` has no scopes/permissions column, and
every API route takes the identical `Depends(guard)`. Any holder of that key could rewrite the
taxonomy.

**Recommendation:** add a `scopes` column to `api_keys` and require an `admin` scope for tag
mutation, so the analyze key cannot change what the analyzer looks for.

### 5.2 There is a ceiling of 30 tags

`MAX_TAGS_PER_CALL = 30` in `inference/app.py`, and DOOH sends the **full catalog on every call**.
The catalog is at 20. That is room for **ten more tags, not unlimited**. Either raise it
deliberately or enforce it at create time with a clear error.

### 5.3 Test debt that blocks every tag creation

- `tests/test_api.py:174` hard-codes `body["count"] == len(body["tags"]) == 20`, and line 175 pins
  the per-tag key set with `set(...) ==`. Any new tag, or any new field, fails it.
- `GOLDEN` in `tests/test_tag_coverage.py` needs an entry **and an eval image** per tag.

Both must become catalog-driven rather than hard-coded.

### 5.4 This design contradicts documented policy

Four places state that tags are added by editing a file in git — `packs.json`'s header ("Adding a
tag = editing this file. Never touch Python."), `inference/README.md`, `detector.py`'s module
header, and `dooh/db/models.py`. **Those comments must be updated as part of the work, not left to
contradict the code.** A stale invariant is worse than none, because the next person will trust it.

---

## 6. Recommended sequencing

**Phase 1 — the feature, minus the live reload.** DB table, CRUD API, admin UI,
draft/active/retired, evidence-gated activation, the DOOH proxy and admin screen. The pack is still
applied by writing `packs.json` and redeploying. This delivers the entire workflow and the whole
safety model with **no new ML machinery**.

**Phase 2 — `reload_packs`.** Make it take effect without a redeploy.

Doing both at once means debugging taxonomy CRUD and in-process embedding surgery simultaneously,
on the code path that decides whether a paying advertiser is refused.

---

## 7. Verification

1. Create a draft tag → it appears in `/admin` and is scored on the playground, and is **absent**
   from `GET /api/v1/tags` and the owner picker.
2. Try to activate with no eval images → refused, naming what is missing.
3. Add ≥8+8 images, calibrate, sweep, activate → it appears in the picker and a screen can block it.
4. `packs_version` moves on create; confirm every existing tag flips to `calibrated: false`, that
   this is visible in `/admin`, then re-calibrate and confirm recovery.
5. Retire the tag → gone from the catalog, and a screen that blocked it does **not** start emitting
   `NOT_VERIFIED` reviews.
6. Restart the Space → the pack is re-pushed from Postgres, `packs_version` unchanged.
7. `make check` in `profile`, `npx jest` in `dooh-backend`, both builds clean.
