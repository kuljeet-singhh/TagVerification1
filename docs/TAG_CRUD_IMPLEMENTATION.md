# Tag CRUD — Implementation Plan

**Feature:** an admin creates content tags through an API in `profile`; the new tag shows up
alongside the existing 20.
**Spans:** `profile` only. **No `dooh-backend` or `dooh-frontend` changes in this work.**
**Status:** ✅ Built — see §11 for what implementation changed about the plan
**Supersedes:** [TAG_MANAGEMENT_API.md](./TAG_MANAGEMENT_API.md) — its §4.1 "Postgres becomes the
source of truth" is **adopted**; its §4.2 `draft` / `active` / `retired` state machine and its §4.4
evidence-gated `activate` endpoint are **not** (see §0). Its measurements, and its analysis of what a
tag is, remain accurate and are carried forward here. Its file paths are stale — see the note in §0.
**Related:** [../../dooh-frontend/docs/SCREEN_CONTENT_POLICY.md](../../dooh-frontend/docs/SCREEN_CONTENT_POLICY.md)

---

## 0. Constraints

These came from the person who asked for the feature. They are constraints, not preferences, and
several of them cut scope that the earlier design doc assumed:

- **Do not change any working functionality.** `GET /api/v1/tags`, the analyze pipeline, the
  threshold path and the 20 existing tags all keep their exact current behaviour.
- **All tags live in Postgres**, the existing 20 included. `packs.json` stops being hand-authored and
  becomes a *generated artifact*. See §3 — this does **not** touch the serve path.
- A created tag **appears and works immediately** once the pack is published. No `draft` state, no
  hidden-until-activated gate, no evidence-gated `activate` endpoint.
- **Update is included** — rewriting hard negatives is the accuracy loop, and delete-and-recreate
  would orphan a slug's eval images and thresholds.
- **Admin session only** (`require_admin`). No `scopes` column on `api_keys`: the analyze key must
  not be able to change what the analyzer looks for.
- **Restart now, live reload later** (§2.1).
- **The import must be lossless** — `seed-tags` then `export-packs` must preserve every prompt, key
  and ordering. It reformats the file once and is *semantically* inert, which was measured before
  any code was written. See §3.1.

Because `packs.json` remains the delivery format and the Space remains the catalog source,
**`GET /api/v1/tags` and the analyze pipeline are untouched.** The only thing that changes is *who
writes the file*: a human before, `export-packs` after. After a publish that endpoint returns 21 tags
with no change to a single line of it.

> Note on stale references. The package is `tagverify/`, not `dooh/` — every `dooh/...` path in
> TAG_MANAGEMENT_API.md is wrong. `tests/test_api.py:174` is now `tests/api/test_tags.py:20`. And
> "audit pair, as `TagThreshold` has" is wrong: `TagThreshold` has only `updated_at`/`updated_by`,
> and **no model in this repo has a `created_by`** at all.

---

## 1. What a tag is — and why validation is the only safety net here

From `packs.json`'s own header: *"Adding a tag = writing sentences."* A tag is positives plus **hard
negatives**, and the negatives do most of the work. Two authoring rules, both with measured evidence:

- **RULE 1 — mirror the phrasings.** With only "a tin of baby formula" as a negative, infant formula
  scored 0.709 (wrongly present). Adding "a scoop of baby formula milk powder" dropped it to 0.224
  while real whey protein stayed at 0.999.
- **RULE 2 — name the confusable neighbour.** "A plain brick wall" as an alcohol negative teaches
  nothing; "a bottle of fruit juice" is what actually gets mistaken for it.

And one rule that comes from the code rather than the docs — `inference/detector.py:212-219`:

```python
every_positive = {row for slug in self.packs for row in self.index[slug]["pos"]}
spoken_for = set(self.index[slug]["pos"]) | set(self.index[slug]["neg"]) | distractors
self._rival_rows[slug] = sorted(every_positive - spoken_for)
```

**The catalog is the negative space.** Every tag is scored by softmax against every *other* tag's
positives — that is what took a gym poster's `gambling` score from 0.974 to 0.061. Because `_row()`
interns by exact string and `spoken_for` is subtracted, **a phrase duplicated between two tags is
removed from both their rival pools**, silently weakening cross-tag competition for each. That is a
correctness rule, not style, and §4.2 enforces it.

Because a new tag goes live as soon as it is published, **write-time validation and the publish
runbook are the whole safety model.** There is no activation gate to catch a bad pack later.

---

## 2. Schema — `content_tags`, the source of truth for every tag

New model in `tagverify/db/models.py`, following house conventions exactly (`Mapped[...]` /
`mapped_column`, `Text` never `String(n)`, `JSONB` from the postgresql dialect, `REAL` for floats,
both `default=` and `server_default=` on non-null columns, `DateTime(timezone=True)` +
`server_default=func.now()`, indexes in `__table_args__`, `#:` comments on non-obvious columns):

| column | type | notes |
|---|---|---|
| `slug` | `Text` | PK, `^[a-z][a-z0-9_]*$` |
| `label`, `description` | `Text` | shown to screen owners |
| `positives`, `negatives` | `JSONB` → `Mapped[list[str]]` | the prompt pack |
| `rationale` | `Text` nullable | *why* these negatives — the prose currently in the `//negatives` keys, which is real institutional knowledge with nowhere else to go |
| `sigmoid_floor` | `REAL` nullable | per-tag override; only `revealing_clothing` uses one today (0.02) |
| `status` | `Text` | `active` / `retired`, default `active`. `Text`, not an enum type — there are no enum types in this schema. See §6 for why `retired` exists |
| `sort_order` | `Integer` | preserves the file's tag order across a round-trip; reordering would change the bytes |
| `extra` | `JSONB` | **passthrough** for keys this schema does not model — see below |
| `created_at`, `created_by`, `updated_at`, `updated_by` | | `updated_by` is free-text provenance, matching `tag_thresholds`' `'admin'` / `'calibrate'` / `'seed'` convention |

`Index("content_tags_status_idx", "status")`.

**`extra` exists to make the migration provable.** `packs.json` carries two keys that nothing reads —
`detect_objects` (no reader anywhere) and `escalate` (written by `seed-thresholds` into a
`tag_thresholds` column that `decide()` never consults; `Thresholds` at
`tagverify/tags/decide.py:129` has no such field). The tidy move is to drop them. The *safe* move is
to carry them through untouched, so `seed-tags` → `export-packs` loses nothing (§3.1). Dropping them
would make the export a *semantic* change rather than a reformat, and the difference matters: a
reformat needs no recalibration, a semantic change does. `extra` also absorbs any future key added to
the file by hand, so a round-trip can never silently discard one.

**Migration:** there is no migration tooling, deliberately (`pyproject.toml:14-16`). Add the table to
`docs/schema.sql` by hand with `IF NOT EXISTS`. **Also fix that file's regeneration header**
(`docs/schema.sql:3-7`) — it still says `from dooh.db.models import Base` and would fail today.

`tag_thresholds.slug` and `eval_images.tag_slug` are loose strings with no FK. Leave them that way —
an FK now would make retiring a tag fail against its own history rows.

### 2.1 What "publish" costs: a model restart, not a code deploy

`Detector.__init__` reads `packs.json` and encodes every prompt into `text_embeds` **once, at
import** — `inference/app.py:34` instantiates at module level, and no runtime path re-reads the file.
So a created tag reaches the model only when that process starts again.

Today that is cheap, because the model is local: `HF_SPACE` is empty, so `tagverify/config.py:49`
falls through to `INFERENCE_URL=http://127.0.0.1:7860`. Publishing is `dooh export-packs`, then
restart `app.py`. Seconds — no git, no deploy, no code change.

**This stops being enough when the model moves to a Hugging Face Space.** That Space runs from its
own git repo on a different machine with an ephemeral filesystem, so a `packs.json` written by the
web tier never reaches it — and adding a tag would become a real `git subtree push` redeploy.

The fix is a `reload_packs` endpoint on the Space, **deliberately deferred to a follow-up, to be
built before the Space is ever deployed.** Recording why it is not free, so that follow-up is scoped
honestly: re-encoding all 266 prompts measures at 4797ms and one new tag's 14 prompts at 367ms, and
`_row()` is already append-only with `text_embeds` concatenable — the encoding is the easy part. The
hard parts are that `_rival_rows` must be rebuilt for **every** tag; that the swap needs a lock
because `demo.queue(max_size=32)` means an `analyze` can be in flight; that `packs_version` must be
hashed from the **pushed bytes** rather than from disk, or the fingerprint sits still while every
score moves; and that `gr.api` endpoints carry no authorization at all, so a mutating one needs its
own shared secret, not the read-scoped `HF_TOKEN`.

---

## 3. Publishing: `packs.json` becomes a generated artifact

Two new CLI commands, following the established shape (a sync Typer command wrapping an inner
`async def run()` under `asyncio.run()`, session from `session_scope()`, `on_conflict_do_nothing`
rather than per-row try/except — `tests/test_cli.py:52` enforces that no CLI command calls
`rollback` inside `session_scope`):

**`dooh seed-tags`** — import all 20 tags from `inference/packs.json` into `content_tags`, once.
Per-tag keys this schema models go to their columns; `//negatives` becomes `rationale`; anything
else (`detect_objects`, `escalate`) goes to `extra`. `sort_order` records file position. Idempotent
and non-destructive: it never overwrites an existing row, so re-running it cannot clobber an edit.

**`dooh export-packs`** — write `inference/packs.json` from the database: every `status='active'` row
in `sort_order`, under a **preserved file-level block**.

The file-level keys are not tags and do not belong in a tags table — `$comment` (which carries the
RULE 1 / RULE 2 evidence in §1, genuine institutional knowledge), `prompt_template`,
`shared_distractors` and `defaults`. `export-packs` reads them from the existing file and writes them
back unchanged. Serialization must be deterministic — `indent=2`, insertion order preserved, the same
separators and trailing newline the file has today — because `packs_version` hashes these bytes.

Retired tags are simply absent from the output.

**One source of truth.** After `seed-tags`, `packs.json` is output, never input — except for that
file-level block. Nobody hand-edits it again; that is what §9's doc updates are for.

### 3.1 The migration is semantically inert, and provably so

**Measured, not assumed.** A byte-identical round-trip is *not* achievable, so the acceptance test
first written into this plan was wrong and has been replaced. `json.dumps(..., indent=2)` differs
from the hand-authored file in three ways, all pure formatting:

- hand-inserted blank lines between top-level sections;
- the literal `0.30`, which reparses and re-emits as `0.3`;
- `detect_objects` arrays hand-compacted onto a single line.

Reproducing those needs a bespoke pretty-printer that would fight every future edit. Not worth it.

What *is* provable, and what was actually verified before any code was written:

```
parsed structures identical: True
scoring surface identical:   True     # prompt_template, shared_distractors,
                                      # every tag's positives/negatives, the defaults
```

So the one-time reformat changes **no prompt, no threshold and no score**. It changes only the
file's bytes — and therefore `packs_version`, which is a hash of those bytes.

**That distinction is what makes the migration cheap.** Because the scores are bit-identical, the
existing measured thresholds in `calibration.json` remain exactly correct. Nothing needs
re-calibrating; the numbers only need re-stamping against the new fingerprint. Concretely:

```
dooh seed-tags
dooh export-packs                      # one-time reformat; packs_version moves
# confirm the reformat is inert: parsed structure equals the pre-export file
# bump packs_version in calibration.json to the new value, then:
dooh apply-calibration                 # re-stamps packs_version_seen via the existing tested path
```

Do **not** re-run `calibrate.py` here. It is unnecessary (no score moved) and it would wipe the
hand-edited `held_back` block — see §3.3.

The two acceptance tests, both automated in §8:

1. **Semantic no-op** — the structure parsed from the exported file equals the structure parsed from
   the file that preceded it. This is the one that catches a lossy round-trip: a dropped key, a
   reordered tag, a mangled prompt.
2. **Byte stability from then on** — once the file is machine-formatted, a second `export-packs`
   over unchanged data produces an identical file. After the initial reformat, `packs_version` moves
   only when a tag actually changes.

This is why §2 carries the `extra` passthrough and `sort_order` columns: without them test 1 fails,
and the failure is exactly the silent prompt-pack corruption they exist to prevent.

### 3.2 The one unavoidable side effect

Adding **any** tag changes `packs.json`, and `scorer_version()` hashes that file's bytes together
with `detector.py`'s. So `packs_version` moves, and three things follow. All of them happen today too
if you hand-edit the file, so this is not a regression — but the runbook must say so:

- `_rival_rows` is rebuilt for every tag, so **every existing tag's scores move slightly.**
- Every `tag_thresholds.packs_version_seen` goes stale, so **every existing tag reports
  `calibrated: false`** until re-calibrated (`tagverify/api/v1/tags.py:58-65` computes this today).
- `dooh apply-calibration` hard-refuses the previous `calibration.json` on its fingerprint check
  (`tagverify/cli.py:170-181`), and the raw-score cache (`tagverify/db/cache.py`, keyed on
  `packs_version`) goes cold.

### 3.3 The publish runbook — to be written into `docs/`

```
dooh export-packs --push                              # DB → packs.json → the RUNNING model (packs_version MOVES)
                                                      # --push replaces the old "restart the model tier" step.
                                                      # Without it, export alone leaves the process on its old pack.
dooh seed-thresholds                                  # threshold row for the new slug (existing command)
cd inference && ./.venv/bin/python calibrate.py       # ALL tags, not just the new one
./.venv/bin/python sweep.py --thresholds calibration.json
# compare cross_fire against the previous sweep BEFORE applying
dooh apply-calibration
```

**Trap:** re-running `calibrate.py` silently overwrites the hand-added `held_back` block in
`calibration.json` and re-marks all 20 tags calibrated. That block encodes the measured finding that
applying all 20 calibrations was a *net regression* — cross-tag false blocks 152 → 211 (+39%) for
mean recall 0.665 → 0.652. Preserve it across the run. The durable fix is to move that decision into
the database later.

---

## 4. The CRUD surface

Both entry points call one service module, `tagverify/tags/catalog.py`, so the rules cannot drift.

### 4.1 Routes

JSON API — new handlers alongside the existing `GET` in `tagverify/api/v1/tags.py`:

| route | behaviour |
|---|---|
| `GET /api/v1/tags` | **unchanged.** Still the live Space catalog; returns 21 after a publish |
| `GET /api/v1/tags/managed` | list stored tags with their prompts — admin only |
| `POST /api/v1/tags` | create |
| `PATCH /api/v1/tags/{slug}` | edit label / description / prompts / rationale / floor |
| `DELETE /api/v1/tags/{slug}` | retire — §6 |

Admin UI routes, all `require_admin`:

| route | behaviour |
|---|---|
| `POST /admin/tags` | create, re-rendering the panel |
| `GET /admin/tags/{slug}/edit` | swap the row for its edit form |
| `GET /admin/tags/{slug}/row` | cancel — swap back, discarding edits |
| `POST /admin/tags/{slug}` | save the edit; a rejection returns the **form**, not the row, so the author's text survives |
| `POST /admin/tags/{slug}/retire` | retire |

The two `GET`s are admin-gated as firmly as the writes: they return the prompt pack, which is
not public — `GET /api/v1/tags` deliberately publishes only slug, label and description.

Admin UI — HTMX handlers in `tagverify/web/admin.py`, mirroring `save_threshold`
(`admin.py:170-206`) exactly: `require_admin(request)` as the first line, `Form(...)` fields
validated by hand, a **200 with the partial re-rendered** on user-fixable errors (never a 4xx — HTMX
drops those), then the write, then explicit memo invalidation, then re-render.

A created tag is **not live until `export-packs` runs and the model restarts**. The admin UI must say
so on the row — otherwise an admin creates a tag, sees nothing happen, and creates it again.

### 4.2 Validation — where accuracy is bought

In `catalog.py`, applied identically on create and update.

**Hard refusals:**

- `slug` matches `^[a-z][a-z0-9_]*$` and is unique across `content_tags` — **including retired rows**,
  since reusing a slug would silently inherit its eval images and thresholds.
- **≥5 positives and ≥6 negatives.** The existing catalog averages 6 and 7; the best-measured tag
  (`alcohol`, recall 0.93) has 8 and 12.
- **No phrase may duplicate any other tag's positive** — the `_rival_rows` rule in §1. Name the
  colliding tag in the error.
- No duplicates within a tag. Phrases trimmed, non-empty, lowercase, no trailing period — they are
  substituted into `"This is a photo of {}."`, so they must read as a noun phrase.
- **Total tags ≤ 30.** `MAX_TAGS_PER_CALL = 30` (`inference/app.py:41`) and DOOH sends the full
  catalog on every call. At 20 today that is room for ten more, not unlimited. Refuse clearly rather
  than letting analyze start failing.

**Warnings — shown in the admin UI, never blocking:**

- **RULE 1 mirror check.** Extract each positive's leading phrase head ("a bottle of", "a glass of",
  "a scoop of") and flag any head with no counterpart among the negatives. A heuristic, so it advises
  rather than refuses — but it is the highest-value thing the form can tell an author, because it is
  exactly the failure the baby-formula measurement records.
- Fewer than 8 positives or 10 negatives.
- Empty `rationale` — prompting for *why* these negatives is how RULE 2 actually gets followed.

### 4.3 Auth

`require_admin` (`tagverify/web/admin.py:34`) checks the `dooh_admin` cookie and, on POST, the
double-submit CSRF token. It raises `NotAuthorised`, which `tagverify/main.py:126-136` renders as
**HTML** — wrong for `/api/*`.

Add a JSON-native `require_admin_api(request)` doing the same two checks but raising `ApiError`, so
it flows through the existing `{error, message}` envelope. If the code it needs is not already in
`ApiErrorCode` (`tagverify/errors.py:21-38`), add it there **and** to the `STATUS` map **and** to the
documented list in `tagverify/web/docs.py:18-43`, which is kept in sync by hand. `admin_state(request)`
already exists at `tagverify/auth/deps.py:79` and is currently unused — it is the intended hook.

The admin page inherits its CSRF token from `templates/base.html:30`, so HTMX forms need nothing
extra; a JSON caller sends the header itself.

### 4.4 Cache invalidation

Every successful mutation clears both in-process memos **in the handler**, not in the writer — that
split is deliberate and documented at `tagverify/db/thresholds.py:58-61` ("the memo is process state,
not database state"): `reset_caches()` (`tagverify/scoring/client.py:256`, currently never called
from production code — this is the wiring it was built for) and `invalidate_threshold_cache()`
(`tagverify/tags/decide.py:321`).

---

## 5. Measuring a new tag after it ships

Because the tag goes live immediately, calibration is an urgent follow-up rather than a gate. Two
things make that visible instead of forgotten:

- The admin tags table shows each DB tag's calibration state, reusing the existing `chip chip-warn`
  "uncalibrated" treatment from `partials/threshold_row.html`. The thresholds panel already renders a
  `callout callout-warn` counting uncalibrated tags — a new tag joins that count for free.
- **`dooh apply-sweep sweep.json`** (optional, recommended) — `sweep.py`'s `cross_fire()`
  (`inference/sweep.py:215`) already measures, per tag, how many images belonging to *other* tags it
  wrongly calls present. Storing that number against the tag turns the blast radius into something an
  admin can see. Today's worst offenders would read: `tobacco_smoking` 44, `mobile_electronics` 41,
  `travel_tourism` 25. Reuse `apply-calibration`'s packs-version fingerprint refusal so a stale sweep
  cannot be applied.

A new tag needs **≥8 labelled images per side** in `inference/eval/<slug>/{pos,neg}/` before
`calibrate.py` will mark it calibrated — `MIN_PER_CLASS` (`inference/calibrate.py:68`), whose comment
gives the reason: *"3 correct guesses out of 3 is not 100% precision."*

---

## 6. Delete means retire

A hard delete leaves dead slugs in DOOH's `devices.blocked_tags`, which only self-heals on the next
write to that device. Until someone re-saves the screen: `blockedTags` is non-empty so the fast path
is skipped, the slug is missing from the catalog so it produces no verdict, and `evaluatePolicy`
falls through to `NOT_VERIFIED` — **a screen quietly generating review-queue entries forever.**

So `DELETE` sets `status='retired'`: dropped from `export-packs` output and therefore from the
catalog on the next publish, with its `tag_thresholds` and `eval_images` rows left intact. The admin
table renders it dimmed, reusing the `.is-revoked` row style (`static/css/app.css:591`) that API keys
already use for exactly this idea.

Now that all 20 live in the table, retire applies to them too. Retiring an *original* tag is a much
bigger act than retiring one nobody has adopted yet — screens are already blocking on it — so the
confirm dialog must say how the slug is currently used rather than asking a generic "are you sure?".

*(DOOH should also sweep `devices.blocked_tags` when a slug leaves the catalog — noted, and
explicitly out of scope here.)*

---

## 7. Admin UI — a third tab

`templates/pages/admin.html` hardcodes exactly two panels, in the markup and in the inline tab script
at `:47-55`. Add a `tags` tab and generalise that script to iterate panels rather than naming them.

New partials mirroring the existing pair:

- `partials/tags_panel.html` — modelled on `keys_panel.html`: a create `<form class="card card-pad">`
  with `hx-post="/admin/tags" hx-target="#tags-panel" hx-swap="outerHTML"`, plus a
  `<table class="data">` inside `<div class="card table-scroll">`, and a `callout callout-warn` when
  any tag is pending publish.
- `partials/tag_row.html` — modelled on `threshold_row.html`: the row's `id` **is** its own
  `hx-target`, with `hx-swap="outerHTML settle:200ms"`.

Reuse existing classes only — `.card`, `.btn`/`.btn-outline`/`.btn-danger`/`.btn-sm`, `.field`,
`.input`, `.chip`/`.chip-warn`, `.callout callout-warn`, `.table-scroll`, `table.data`,
`.is-revoked`, `.when-idle`/`.when-busy`/`.spinner`. **Colour means a verdict** in this UI, so status
chips use the neutral/warn chips, never the present/uncertain/absent palette, and no literal hex
values go in templates — everything comes from the token block at `static/css/app.css:26-143`.

If the tags grid needs its own column layout, follow `.thr-grid` (`static/css/pages.css:470`)
including the `display: contents` trick that lets one `<form>` span grid cells — and duplicate its
≤900px stacking rules, or the tab will not stack on mobile.

`hx-confirm` is already intercepted and styled (`static/js/app.js:94`), so retire gets a proper dialog
for free. There is no `tag` icon in `_icons.html`; `sparkle` and `scan` are unused in admin.

Minor: `tagverify/tags/groups.py:18-52` hardcodes the playground's tag grouping by slug, so a new tag
appears there in the "Other" bucket. Correct behaviour, no change needed.

---

## 8. Test debt that a 21st tag breaks

Three places hard-code the catalog. All must become catalog-driven — this is required, not optional,
because the suite fails the moment a tag is published:

- `tests/api/test_tags.py:20` — `assert body["count"] == len(body["tags"]) == 20`, and `:21` pins the
  per-tag key set with `set(...) ==`. Assert internal consistency and a non-empty catalog instead.
- `tests/test_tag_coverage.py:48` — `GOLDEN` is a second hard-coded copy of the 20 slugs, and
  `ALL_TAGS = sorted(GOLDEN)`. Derive the tag list from the live catalog and skip slugs with no golden
  image, so a new tag does not fail the suite before it has eval images.
- Prose counts in `inference/sweep.py:22` and `inference/fetch_demo_eval.py:7`.

New tests:

- **The round-trip tests (§3.1), the most important here.** Two of them, and they check different
  things: (a) *semantic no-op* — the structure parsed from the exported file equals the structure
  parsed from the file before it, which catches a dropped key, a reordered tag or a mangled prompt;
  (b) *byte stability* — exporting twice over unchanged data yields identical bytes, so
  `packs_version` moves only when a tag really changes. Test (a) must compare the full parsed
  structure, not just the slug list, or a lost `//negatives` would sail through.
- `catalog.py` validation as pure unit tests — no DB, no model. Cover the cross-tag phrase collision
  and the ≤30 cap explicitly.
- `export-packs`: a second export is byte-identical to the first; a retired tag disappears; the
  file-level `$comment` / `prompt_template` / `shared_distractors` / `defaults` block survives.
- `seed-tags` is non-destructive: editing a tag then re-running it does not revert the edit.
- Extend the parametrised auth test at `tests/test_admin.py:122-136` to cover every new mutating route
  unauthenticated, and `:141-149` for missing CSRF.

`needs_db` runs against a **real** database — there is no fixture schema — so `content_tags` must
exist in the dev DB before these pass.

---

## 9. Docs this contradicts, which must be updated

Four places state that a tag is added by editing a file in git. After this work that is not just
stale but actively harmful — a hand edit to `packs.json` is silently reverted by the next
`export-packs`:

- `inference/packs.json`'s `$comment` header — *"Adding a tag = editing this file. Never touch
  Python."* This block is preserved verbatim through the round-trip, so it must be rewritten to say
  the file is **generated** and to point at the admin UI. Note that editing it moves `packs_version`,
  so do it in the same deliberate step as the first real publish, not during the §3.1 no-op check.
- `inference/README.md`
- `inference/detector.py`'s module header
- the "two sources of truth" note in `tagverify/db/models.py` — its objection is now answered: there
  is one authority and one direction.

Update all four, and mark `docs/TAG_MANAGEMENT_API.md` as superseded by what actually shipped.

---

## 10. Verification

```bash
make check      # ruff + full pytest
make test-fast  # decision rules alone
```

| # | Check | Expected |
|---|---|---|
| 1 | **The regression that matters most** — before publishing anything, call `GET /api/v1/tags` | Same 20 tags, same keys, same `packs_version` and `decision_version` as today. DOOH is live against this endpoint and it must be untouched |
| 2 | `psql "$DATABASE_URL" -f docs/schema.sql` on a fresh DB | Creates `content_tags`; re-running is a no-op |
| 2b | **The migration is inert** — `dooh seed-tags`, then `dooh export-packs`, then `git diff inference/packs.json` | A **formatting-only** diff: blank lines removed, `0.30` → `0.3`, `detect_objects` expanded. Every prompt, slug and threshold identical. Verify by parsing both sides and comparing structures — if *that* differs, the round-trip is lossy; stop and fix it (§3.1) |
| 2d | After the reformat: bump `packs_version` in `calibration.json`, run `dooh apply-calibration` | The same 8 tags report `calibrated: true` again. **Do not re-run `calibrate.py`** — no score moved, and it would wipe `held_back` |
| 2c | Re-run `dooh seed-tags` after editing a tag | The edit survives; seeding never overwrites |
| 3 | `POST /api/v1/tags` | Stored, visible in `/admin?tab=tags`, marked pending publish, and **not yet** in `GET /api/v1/tags` |
| 4 | Validation | A phrase duplicating another tag's positive is refused and the error names that tag; 4 positives are refused; a 31st tag is refused; a positive with no mirrored negative saves **with a warning** |
| 5 | `dooh export-packs`, then diff `packs.json` | The 20 existing entries unchanged, including their `//` comment keys and the file-level block; the new tag appended. Run it twice — the second run changes nothing |
| 6 | Restart the model tier | `GET /api/v1/tags` returns **21**, the new tag included, and the playground scores it |
| 7 | `packs_version` | Moved; every existing tag now reports `calibrated: false`. Run the §3.3 runbook and confirm recovery — and that `held_back` survived |
| 8 | `PATCH` an **existing** tag's negatives — `restaurant_dining`, recall 0.45 — then re-export and restart | The score on a known false-positive image moves in the expected direction. This is the accuracy loop working end to end, on a tag that could not be touched at all under the earlier design |
| 9 | Retire the tag | Next export drops it from `packs.json`; its `eval_images` and `tag_thresholds` rows still exist; the admin row renders dimmed |
| 10 | Mutations unauthenticated, and with no CSRF token | Refused — JSON envelope on `/api/v1/*`, re-rendered login on `/admin/*` |
| 11 | Any mutation | Visible on the very next request (both memos cleared), not up to 60s later |

---

## 11. What implementation changed about this plan

Three things were measured during the build that the plan got wrong or did not know. All are
folded into the sections above; recorded here so the corrections are not invisible.

### 11.1 "Byte-identical round-trip" was unachievable, and the wrong test anyway

The plan's original acceptance criterion — `seed-tags` then `export-packs` reproduces
`packs.json` byte for byte — cannot hold. Measured before any code was written: the
hand-authored file carries blank lines between sections, writes `0.30` where `json` re-emits
`0.3`, and keeps `detect_objects` on one line. Reproducing that needs a bespoke pretty-printer.

The criterion was replaced with the one that actually matters (§3.1): the reformat must be
**semantically inert**, verified by comparing parsed structures and the scoring surface. It is
— `tests/test_packs.py` pins it — and the export is byte-stable from then on. The `extra`
passthrough and `sort_order` columns are what make that true; without them the export drops
`detect_objects` and `escalate` and stops being a reformat.

### 11.2 The calibration was already stale, before this work

`calibration.json` was measured against `c14ac5bdcfa9`. The live pack was already at
`2474f58a272b` at `HEAD`, untouched. So **every tag was already reporting `calibrated: false`**
(`api/v1/tags.py:58-65`), and `dooh apply-calibration` would already have refused on its
fingerprint check.

That invalidates the plan's tidy "bump `packs_version` in calibration.json and re-stamp"
shortcut, which is only honest when a reformat is the *sole* change since calibration ran.
`export-packs` now checks this and says which situation you are in rather than printing advice
that would be a false claim. A real recalibration is owed, and it was owed before this change.

### 11.3 A docstring in `detector.py` moves `packs_version`

`scorer_version()` hashes `packs.json` **and** `detector.py`, so the comment fix in §9 moved
the fingerprint on its own. Correct — the fingerprint is meant to cover the scoring code — but
worth knowing before editing a comment there and wondering why the catalog went uncalibrated.

### 11.4 Smaller corrections

- **No cache invalidation on tag writes.** The plan said to call `reset_caches()`. It would be
  cargo cult: `GET /api/v1/tags` is served from the model tier, which cannot see a new row
  until `export-packs` runs and the model restarts. Clearing a cache would imply an immediacy
  the system does not have. The handlers return `pending_publish` instead, and the admin panel
  leads with a "not live yet" callout.
- **The mirror check needed narrowing to be worth having.** As first written it took a
  phrase's first two words as its head, which is really the *subject* — and a tag's negatives
  are about different subjects by definition, so it warned on **20 of 20** shipped tags.
  Restricted to quantifier heads ("a bottle of") it warns on 13, and `alcohol` and
  `protein_supplements` come back clean — the two tags whose negatives were hand-hardened by
  the measurements the rule comes from. `tests/test_content_tags.py` pins that agreement, so a
  future change cannot drift it back to warning about everything.
- **Slugs are normalised, not refused.** Case and padding are typos, not decisions — and
  normalising also stops `Pet_Supplies` and `pet_supplies` existing as two rows whose
  positives compete in the same rival pool.
- **Post-commit reads.** `session.commit()` expires every attribute, so building a response
  from an instance afterwards lazy-loads from a context that cannot await it. The handlers
  re-read the row instead, and `updated_at` is a Python timestamp rather than `func.now()`.

---

## 12. Follow-up: the create form

Two defects found by using the admin UI, both fixed.

### 12.1 A rejected create discarded everything typed

`create_content_tag` re-rendered the panel from `_load(session)` alone, which carries keys,
thresholds and tags — but none of the submitted values — and the template rendered its inputs
with no `value` and its textareas empty. So every rejection cost the author their label,
description, rationale and **both phrase lists**.

The edit form already returned the populated form on rejection, for a reason that applies at
least as strongly here: the phrase lists *are* the tag. A good one carries 8+ positives and 10+
negatives, hand-written and mirrored against each other. Making someone retype all of that
because they were four phrases short is how you get four lazy phrases — the outcome the
validation exists to prevent.

The handler now passes the **raw submitted strings** back on the error path. Raw, not
`_phrases()` output: re-joining a parsed list would drop the author's blank lines and reformat
their text underneath them while they are still editing it. On success `form` is deliberately
absent, so the next tag starts blank instead of inheriting the last one's phrases.

### 12.2 The slug had to be invented

`Pet supplies` → `pet_supplies` is a transform, not a decision, and the field refused hyphens,
capitals and spaces via `SLUG_RE`. It now fills in from the label as you type.

The field is **not** hidden, because the slug is not an internal detail: it is permanent
(`catalog.py` refuses a rename), it is the join key for `tag_thresholds.slug` and
`eval_images.tag_slug`, it is published to DOOH and stored in every screen's
`devices.blocked_tags`, and it is the folder name an admin creates by hand at
`inference/eval/<slug>/{pos,neg}/` before the tag can ever be calibrated. Deriving it silently
would hide a permanent identifier they later have to go and look up.

**Label renders first, then Slug** — the slug is derived from the label, so leading with the
derived value asks the reader to work right-to-left. Both facts above are documented here rather
than in the form: a hint under the input was tried and removed, because the slug column is 200px
wide and the row is `align-items:flex-end`, so six lines of wrapped prose made that column taller
and pushed the Label field out of alignment with it. If it is worth surfacing again, the form's
intro paragraph or a `title` tooltip would carry it without disturbing the layout.

Two details that are easy to get wrong:

- **The listener is delegated on `document`**, not bound to `#tag-label`. The panel is replaced
  wholesale by `hx-swap="outerHTML"`, so a direct binding would be dead after the first
  rejection — exactly when the field matters most.
- **A slug that survived a rejection arrives already `data-touched`**, so a later label edit
  does not overwrite a correction the author made by hand. The alternative — re-deriving
  "touched" on the client — would need the previous label value, which is gone by then.

The client transform is a convenience and never the validation: the server still owns
`SLUG_RE`, which is why a label that slugifies badly (`"3D printing"` → `d_printing`, since the
pattern needs a leading letter) can still be typed over.

---

## 13. The "not live yet" banner tells the truth

A tag was added through the admin UI and did not show up in the playground picker. Nothing was
broken — three layers simply disagreed, and the UI gave no way to find that out:

| Layer | State |
|---|---|
| `content_tags` | present and active, so Admin listed it |
| `inference/packs.json` | absent — `export-packs` had not been run |
| Running model | on an older pack still, so stale even against the file |

The playground picker is built from `cached_tag_catalog()` — the model's own catalog — so it can
only ever show what the model booted with. **Two steps were needed, not one**, and the banner
could not say so because it was static prose rendered unconditionally. A warning that is always
on is one people scroll past, which is exactly what happened.

It now compares the active rows against the slugs the running model reports, and says nothing
when there is nothing to say. Three states: named-and-pending, silent, or "cannot tell" when the
model does not answer. `tag_row.html` carries a matching `not live` chip.

**It asks the model, not the pack file, and that is not incidental.** The Dockerfile copies only
`inference/__init__.py`, `banding.py` and `versioning.py` into the image — `packs.json` and
`detector.py` are **not** in production. Comparing the database against the file, or recomputing
`scorer_version`, would work on a checkout and raise `FileNotFoundError` on a deployed instance.
Asking the model also collapses both failure modes into one check: a slug the model does not
know is missing whether the export never ran or the process was never restarted, and the remedy
is the same two steps either way.

Two traps found while building it, both the same shape — *absence of information rendered as
bad news*:

- `live_slugs is None` (health unreachable) must claim **nothing**. Marking every tag stale
  because health timed out is the same lie in a new coat.
- `tag_row.html` is also rendered standalone by the edit and cancel handlers, which passed only
  `{"row": row}`. With `live_slugs` undefined the chip appeared on **every** row, including live
  ones. Fixed at the cause (the handler now passes it) and guarded in the template
  (`live_slugs is defined`), so a future standalone render cannot silently mislabel.
