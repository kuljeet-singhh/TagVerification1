# Admin Creative Review Surface — Implementation Plan

**Goal:** show flagged creatives to the DOOH admin — *"this ad needs review; this screen blocks X,
and this ad may contain X."*
**Spans:** `dooh-backend` · `dooh-frontend` (`profile` is consumed only — **no change required here**)
**Status:** 🔴 Planned, not started
**Relates to:** `dooh-frontend/docs/SCREEN_CONTENT_POLICY.md` §8 Phase 4 — this plan builds the
"admin surface" bullet. That document remains the single source of truth for the feature; update its
§0 tracker and change log in the same commit as the code.

> **Note on placement.** This plan concerns the DOOH policy layer, not the tag-verification service.
> It is filed here at the user's request; the feature's own tracker lives in
> `dooh-frontend/docs/SCREEN_CONTENT_POLICY.md`.

---

## 1. Context

The Screen Content Policy feature already does the detection. At upload, the backend gate asks
`profile` whether the creative contains any tag the screen owner blocked, and takes one of three
actions (SCREEN_CONTENT_POLICY §6.4, `dooh-backend/src/content-verification/policy.ts:121`):

| Verdict | Action | Visible anywhere today? |
|---|---|---|
| `present: true` | **Refused** at upload | ✅ inline red message naming the tag |
| `present: null` — the model declined to answer | **Allowed, flagged for review** | ❌ **nowhere** |
| check never ran / file undecodable / composite mismatch | **Allowed, flagged for review** | ❌ **nowhere** |

The flag is fully built and already on the wire:

- `creative-verification.service.ts:408` builds a `CreativeReview`
  (`reason`, `findings[]`, `packsVersion`, `decisionVersion`, `checkedAt`)
- `bookings.controller.ts:275,389` writes it onto the media item
- `formatCreativeResponse` (`src/common/utils/creative-media.ts:148`) appends `needsReview` +
  `flagged` to the creative on **every** response
- `dooh-frontend/lib/creatives.ts:63` deliberately preserves `review` through normalization, with
  the comment *"these are what tell an admin why a creative is in the review queue."*

**Nothing renders it.** A repo-wide grep for `needsReview|flagged|\.review\b` across
`components/ app/ lib/ features/` hits only type declarations. An uncertain creative is accepted and
then looks identical to a clean one, so the admin approving the booking has no idea the model could
not clear it.

Confirmed reachable end to end: `GET /bookings/:id` calls `formatCreativeResponse`
(`bookings.service.ts:2181-2202`) with a bare `.select()`, nothing between the service and the wire
strips jsonb sub-fields (`TransformInterceptor` is purely additive), and
`getBookingDisplayMediaItems(booking)` already delivers `media.review` into the review modal's
`CreativeColumn`. **The data is sitting inside the component, unrendered.**

**Why this is the primary path, not an edge case.** SCREEN_CONTENT_POLICY §4.2 measured **~40% of
genuine positives** returning `present: null`. The review queue is where most true detections land.

**Decision taken with the user:** the message names the blocked category, consistent with the
hard-block message which already names the tag. The admin surface is the deliverable; a short
advertiser-facing notice ships alongside it so the advertiser knows the creative is waiting on a
human.

**Out of scope** (stay 🔴 in Phase 4): the admin *override* of a hard block, the fail-closed re-check
at `proceedToPayment`, the keepalive cron, and the retry worker.

---

## 2. Three gaps found while tracing this

1. **`device.blockedTags` never reaches either admin booking endpoint.** `findOne`'s select
   (`bookings.service.ts:2158-2171`) takes only `devices.name` and `devices.locationLabel`; the list
   select (`:2018-2048`) takes only `devices.name`. It matters because `findings[]` is *empty* for
   `NOT_VERIFIED` and `UNSUPPORTED_MEDIA` — there the screen's blocked list is the only context the
   admin has.

   For `UNCERTAIN` it is derivable: `evaluatePolicy` filters `verdicts.filter(v => blocked.has(v.tag))`
   (`policy.ts:130`), so every finding **is** a blocked tag, and each already carries its human `label`.

2. **The admin list returns no creative at all** — only a `creativeCount` scalar subquery
   (`:2033-2044`). A row-level badge therefore needs a new field; it cannot be derived client-side.

3. **`approve()` never clears the flag.** It sets `creatives.moderationStatus = 'APPROVED'`
   (`bookings.service.ts:1195-1198`) but leaves every per-item `review` blob in place, and
   `needsReview` is derived from those blobs (`creative-media.ts:127-145`). A creative an admin has
   *just reviewed* still reports `needsReview: true` forever, so the badge would start lying on day
   one. §3.3 below fixes it.

---

## 3. Backend changes — `dooh-backend`

### 3.1 `findOne` — carry the screen's policy

`src/bookings/bookings.service.ts:2158-2171`: add `blockedTags: schema.devices.blockedTags` to the
select, and `blockedTags: row.blockedTags ?? []` to the return at `:2189-2202`. Mirrors
`loadCreativeUploadContext` at `:2328`/`:2351` exactly.

No migration, no new endpoint, no DTO change — this is a response field, and `forbidNonWhitelisted`
affects request bodies only.

### 3.2 `findAllForAdmin` — a `needsReview` flag per row

`bookings.service.ts:1970-2078`.

**Do not write a raw jsonb SQL predicate.** It would duplicate `deriveCreativeReview` in a second
language and drift from it silently — the same class of problem the derived-not-stored design exists
to avoid. Instead, after the rows query, fetch the creatives for that page's booking ids in one
`inArray(schema.creatives.bookingId, ids)` query and run the **existing** `deriveCreativeReview`
(`src/common/utils/creative-media.ts:127`) on each. One extra query per page of ≤20 rows, one
derivation rule. Map `needsReview: boolean` onto each item beside `creativeCount`.

### 3.3 Clear the flag when the admin acts

In `approve()` (`bookings.service.ts:1182`), alongside the existing `moderationStatus` update, stamp
each flagged media item's `review` with `clearedAt` and `clearedBy: adminId` — **rather than deleting
it**. The evidence is what makes the decision auditable, and SCREEN_CONTENT_POLICY §8 Phase 4
requires recording who acted. `deriveCreativeReview` then skips a cleared review.

- `CreativeReview` (`drizzle/schema.ts:140`) gains optional `clearedAt?: string | null` and
  `clearedBy?: string | null`. It is jsonb, so **no migration** — absent means "not cleared",
  exactly like the existing optional `decisionVersion`.
- This keeps the live-edit hole closed: a *new* creative uploaded onto an already-approved campaign
  (`campaign-creative-manager.tsx`) writes a fresh `review` with no `clearedAt`, so it flags again.
  Deriving "cleared" from `moderationStatus` instead would miss that case, which is why this is not
  done the shorter way.
- Extend `src/common/utils/creative-media.spec.ts` with the cleared case.

---

## 4. Frontend changes — `dooh-frontend`

### 4.1 Fix the drifted types

`types/index.ts:158-167`. `CreativeReview.reason` is missing `'COMPOSITE_MISMATCH'`, which the
backend has been writing since 2026-08-20 (`drizzle/schema.ts:152`), and `decisionVersion`. Add both,
plus the new `clearedAt` / `clearedBy` — otherwise the switch in §4.2 has no case for a reason
already present in the database.

Also add `blockedTags?: string[]` to `Booking` (`:190`) and `needsReview?: boolean` to `AdminBooking`
(`:219`).

### 4.2 New `lib/creative-review.ts` — the copy, in one place

A pure module (no JSX, no hooks), mirroring `policy.ts::buildBlockMessage` in spirit: name what can
be acted on, quote nothing that invites an argument.

```ts
export const CREATIVE_REVIEW_BADGE = 'Needs review';
export function creativeReviewHeadline(review, mediaKind): string   // admin + advertiser
export function creativeReviewDetail(review): FindingDetail[]       // admin ONLY
```

Copy per `reason` (`{noun}` = "image" | "video"):

- **`UNCERTAIN`** — the case this plan exists for; findings carry labels:
  > "This screen blocks **{labels}**, and this {noun} may contain it. The automated check could not
  > decide either way."
- **`NOT_VERIFIED`** — the check never completed (profile down, deadline, rate limit). Must **not**
  claim the creative may contain blocked content; that would be an accusation produced by our own
  infrastructure:
  > "The content check did not complete, so this {noun} was never verified against this screen's
  > rules."
- **`UNSUPPORTED_MEDIA`** — the file could not be decoded. `schema.ts:142` warns older rows used this
  reason to mean "it's a video", so the wording must hold for both.
- **`COMPOSITE_MISMATCH`** — per `policy.ts::mergeComposite`, the disagreement is far more often our
  own blurred letterbox backdrop than their creative. Say so plainly, so the admin does not read it
  as evidence against the advertiser.

Multi-label joins use the same `"A, B and C"` phrasing as `buildBlockMessage`, so a block and a flag
read in one voice.

> **The audience split is load-bearing.** `types/content-tags.ts:9` states `calibrated` is for the
> admin review surface **only** — showing it to an owner or advertiser would promise a difference in
> enforcement that does not exist. The same applies to `score` and `topPhrase` (`policy.ts:86`). So
> `creativeReviewDetail` renders on the admin surface and nowhere else.

### 4.3 New `components/bookings/creative-review-panel.tsx`

Per flagged item:

- a `Needs review` pill;
- the headline from §4.2;
- per finding: label, `score` as a percentage, a `Measured`/`Guess` marker from `calibrated`,
  `topPhrase` in quotes, and `at {frameTimestampS}s` for a video — the timestamp is the one number
  worth quoting (`policy.ts:98`), because it points at the frame to re-cut;
- `slot: 'ALTERNATE'` labelled as the audience creative — it really plays on the screen under an
  audience trigger, so it is not metadata;
- the screen's full blocked list from `booking.blockedTags`, labelled with the existing
  `useContentTags({ enabled })` hook's `labelFor(slug)` (`lib/hooks/use-content-tags.ts`) — the same
  pattern `device-detail-client.tsx:251` uses. `enabled` is gated on a non-empty list so most
  bookings make no extra request. Findings need no catalog; they carry their own `label`.
- `packsVersion` / `decisionVersion` / `checkedAt` as small muted metadata, so a stale verdict is
  distinguishable from a fresh one.

Styling: reuse `styles/booking-review-modal.module.css` `.infoSection` (`:172`) and
`.infoSectionTitle` (`:183`) for the card, and the `.rejectionNote` (`:379`) panel shape for the
callout.

### 4.4 Render it in the review modal — but gated

`components/bookings/booking-review-modal-body.tsx` is shared by **three** surfaces: admin
(`booking-detail-modal.tsx:177`), owner (`owner-booking-detail-modal.tsx:70`), and advertiser
(`advertiser-booking-detail-modal.tsx:71`).

Add `showContentReview?: boolean` to `BookingReviewModalBodyProps` (`:117-127`), alongside the
existing `showAdvertiserInfo` / `showVenueSplit` / `showAssignedSlot` flags, and pass it **only** from
the admin modal. This is required, not tidiness: SCREEN_CONTENT_POLICY §6.4 and the `calibrated`
comment both say owners must not see this, and §8 Phase 4 states *"Screen owners deliberately do not
get the override."*

Place the panel at the top of `CreativeColumn` (`:260-275`), above `creativePreviewHeader`, so the
verdict sits directly above the picture it is about. Add a per-media flag chip on `creativeCard`
(`:295-334`), mirroring the existing absolutely-positioned `creativeRoleBadge`
(`booking-review-modal.module.css:457`), so a multi-creative booking shows *which* item is flagged.

### 4.5 Badge the queue rows

`components/bookings/booking-admin-client.tsx` — render the pill on any row with
`booking.needsReview`, in **both** renderings of the list: the mobile `AdminBookingCard` (`:97-219`)
and the desktop table row (`:562-677`). They are parallel, and missing one is the usual bug on this
surface.

Make it a small sibling component, **not** a new entry in `STATUS_CONFIG`
(`booking-status-badge.tsx:24`) — that map is keyed on booking-status strings, and this is not one.

### 4.6 Styles

`styles/bookings-admin.module.css` — add `.statusNeedsReview` next to `.statusPaused` (`:1132`),
which is the existing precedent for a non-booking-status pill in this file. Use `#fef3c7 / #92400e`:
the obvious amber `#fffbeb / #b45309` is `.statusPending` and the orange `#fff7ed / #c2410c` is
`.statusRefunded`, so either would read as a booking status at a glance. Reuse `.statusBadge`
(`:1052`) for the shape — its responsive overrides at `:535` and `:1594` then apply for free.

### 4.7 Advertiser-facing notice (secondary)

Uses `creativeReviewHeadline` only — **never** `creativeReviewDetail`.

- `components/bookings/creative/creative-gallery.tsx:10,40,60` — carry `review` onto `GalleryItem` in
  both mappers (one line each; they already receive the full `CreativeMediaItem`).
- `creative-uploaded-media-card.tsx:11` — `review` on `UploadedMediaCardItem`, an amber badge in the
  thumbnail block (`:196`), and a one-line `role="status"` note in the same slot as the existing
  short-video notice (`:220`).
- `creative-upload-section.tsx` — derive a section-level note from `galleryItems` (not from the last
  upload response, so it survives a reload) and render it as a **separate** element beside `fileError`
  (`:1357`). Keeping them separate matters: a hard block and a review flag can occur in the same
  batch, and one shared slot would let either silently overwrite the other. `blockedNote` at `:1157`
  is unrelated (image/video exclusivity) — do not reuse that name.
- `booking-creatives-card.tsx:201` — badge beside `rejectedThumbBadge`, suppressed when
  `isRejectedView`; once a booking is rejected, "Not approved" is the outcome and "needs review" is
  stale.

### 4.8 Update the feature tracker

`dooh-frontend/docs/SCREEN_CONTENT_POLICY.md` §0 requires updating in the same commit as the code:
append a change-log row, tick the Phase 4 "admin surface" bullet, and move §10 check 6 when its E2E
passes.

**Do not touch** `dooh-frontend/docs/DOOH_Network_V1_PRD.md` or
`dooh-frontend/docs/PROJECT_FLOW.md` — those are read-only reference documents.

---

## 5. Deliberately not doing

A **"Needs review" filter** in the admin status dropdown.

It is not a booking status: `statusFilter` is passed straight through as `?status=`
(`booking-admin-client.tsx:261`), and the counts come from `GET /bookings/summary`. A real filter
needs a new query param, a new summary count, and edits in four frontend places (`StatusFilter` union
`:52`, `STATUS_TABS` `:69`, `AdminBookingSummary['counts']` `types/index.ts:491`, empty-state copy
`:463`).

Client-side filtering is **wrong** here — the list is server-paginated, so it would only ever filter
the current page. The row badge makes the queue scannable; the filter is a clean follow-up if volume
warrants it.

---

## 6. Verification

Needs the local stack from SCREEN_CONTENT_POLICY §4.1: inference on `:7860`, profile on `:8000`,
backend, frontend.

```bash
curl -s http://127.0.0.1:8000/api/v1/health                     # inference.warm must be true
curl -s $PROFILE_API_URL/api/v1/health | jq -r .decision.rule
shasum -a 256 profile/inference/banding.py | cut -c1-12          # must match — §10 check 19
```

| # | Check | Expected |
|---|---|---|
| 1 | **The uncertain path** (SCREEN_CONTENT_POLICY §10 check 6, still 🔴). As SCREEN_OWNER block `alcohol` on a screen (`/owner/inventory?tab=devices&modal=edit&id=<id>`); as ADVERTISER book it and upload `profile/inference/eval/alcohol/pos/Wine.jpg` (measured 0.569 → `present: null`) | Upload **succeeds**. As ADMIN at `/admin/bookings` the row shows `Needs review`, and the modal names "Alcoholic content" as a tag this screen blocks, with score and a `Measured`/`Guess` marker |
| 2 | **Block still blocks** — upload `pos/Beer.jpg` (0.977) | Refused inline; no flagged booking reaches the queue. The two states must not be confusable |
| 3 | **Clean stays clean** — `neg/Coffee.jpg` | No flag, no panel, no layout shift |
| 4 | **Empty findings** — point `PROFILE_API_URL` at a dead host, upload to the same screen | Reason is `NOT_VERIFIED`; the panel falls back to the screen's `blockedTags` and does **not** claim the creative may contain anything |
| 5 | **Video** — upload a flagged MP4 | Panel says "video" and shows the frame timestamp |
| 6 | **Audience separation** — open the same booking in the **owner** and **advertiser** detail modals | The panel is absent from both; `score`, `calibrated`, `topPhrase` appear on no non-admin surface |
| 7 | **Alternate slot** — flag an audience/alternate creative | Reported as `slot: 'ALTERNATE'` (`creative-media.ts:141`); the panel labels it as the audience creative |
| 8 | **The flag clears** — approve the flagged booking, reopen and re-list | `needsReview` false, badge gone, and the `review` blob still in `media_urls` carrying `clearedBy` |
| 9 | **Re-flag after approval** — on that approved campaign, upload another uncertain creative through the campaign editor | It flags again, proving the clear is per-item and not per-booking |
| 10 | **Derivation holds** — delete a flagged image | `needsReview` and the panel go with it |
| 11 | Build | `npm run build` + `npx tsc --noEmit` clean in both DOOH projects; `npm test` green in `dooh-backend` (currently 154/154) |
