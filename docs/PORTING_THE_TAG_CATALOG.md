# Porting the tag catalog to another project

**Status:** reference. **Written:** 2026-08-26.

This answers one question: *if we build this editable-tag-catalog feature somewhere else, how
much of it carries over?*

Short answer — **the CRUD half ports as-is; the publish half depends entirely on how the other
project's model works.** Most of the value is in the half that ports.

For why this service is built the way it is, see
[`TAG_CRUD_IMPLEMENTATION.md`](./TAG_CRUD_IMPLEMENTATION.md) (what was built) and
[`TAG_MANAGEMENT_API.md`](./TAG_MANAGEMENT_API.md) (the measurements behind it — marked
superseded, but §1, §3.1 and §3.2 still hold). This document is about a *different* codebase.

---

## 1. What ports unchanged

None of this touches the model, so it copies over whatever the target looks like:

- **The `content_tags` table.** Slug as primary key, phrase lists, a `status` of
  `active | retired`, `sort_order`, and the `updated_at` / `updated_by` provenance columns.
- **The validation rules** (`tagverify/tags/catalog.py`). Minimum phrase counts, the slug
  pattern, no duplicate phrases, no phrase both positive and negative, no phrase owned by two
  tags, a ceiling on active tags.
- **Warnings that advise but never block.** Too few phrases, no rationale recorded, an
  unmirrored quantifier. A tag still saves. This distinction is worth keeping: a rule that
  refuses teaches people to work around it, a rule that advises teaches them the craft.
- **Retire instead of delete.** A hard delete strands whatever keyed off the slug — labelled
  images, measured thresholds, and any downstream system still referencing it.
- **The admin surface** and the JSON API shape.

That is most of the work, and it is the part that decides whether tags are any *good*.

---

## 2. What changes — three questions about the target

### Q1. Does its model read the prompts fresh, or load them once?

If prompts are read **per request** (a database lookup, a config file re-read), there is **no
publish step at all**. Save is live. Everything in §3 disappears.

This service loads once, at import, and precomputes an embedding per phrase — that is the
entire reason a publish step exists.

### Q2. Can the model reach the database?

If **yes**, publishing is just "tell it to reload itself". A one-line ping. No pack file,
nothing sent over the wire.

If **no** — as here, where the model runs on a Hugging Face Space with no database
credentials — the prompts have to *travel*. That is what `packs.json` is: a transport format,
not a config file.

### Q3. Does its storage survive a restart?

If the model runs on a normal server with a real disk: write the file, reload, done.

If storage is ephemeral (a Space, a scale-to-zero container, a fresh pod), it will come back
holding whatever is baked into the image or the repo — silently losing anything pushed at
runtime. Then you need something to notice and re-push.

### The four shapes

| Target's model | The flow becomes |
|---|---|
| Reads prompts per request | **Save = live.** No publish, no fingerprints, no banner. |
| Loads once, has DB access | Save, then ping it to reload. No file transfer. |
| Loads once, separate tier, no DB | **This service.** Export → push → re-push after a restart. |
| A *trained* classifier | Does not apply at all — see §4. |

---

## 3. The case that does not port: a trained model

Everything above assumes a **zero-shot** model — one that already understands language, so a
tag is just words and works the moment the model sees them.

If the target uses a **trained** classifier, adding a tag means collecting labelled examples
and retraining. Hours or days, not seconds. There is no "push to make it live", and the CRUD
UI is managing training labels rather than prompts. The schema and the review workflow may
still be useful; the publish pipeline is not.

Establish which of the two you are dealing with **before** designing anything here. It is the
question that changes the answer most, and it is easy to assume.

---

## 4. The gotcha to check: do tags compete?

In this service every tag's positives compete against every other tag's in one softmax — the
catalog itself acts as the negative space. That is why adding a tag has a cost beyond itself.

**How large that cost actually is, measured** (`TAG_MANAGEMENT_API.md` §3.2 — a 21st tag added
and 60 images re-scored against all 20 originals):

| | |
|---|---|
| Scores that moved more than 0.01 | **12 / 1200 (1.0%)** |
| Verdict band flips | **3 / 1200 (0.2%)** |

So the accuracy effect of an unrelated new tag is **small**. Do not repeat the folklore that
"every score moves" — it is technically true and practically misleading.

The real cost is **bookkeeping**, and it is not small:

- the pack fingerprint changes, so every tag's calibration stamp goes stale and every tag
  reports `calibrated: false` until re-measured;
- the previous calibration file is refused on a fingerprint mismatch;
- any score cache keyed on that fingerprint goes cold.

**If the target scores each tag independently** (a sigmoid per tag, separate models, separate
prompts), none of this applies. Adding a tag disturbs nothing and needs no recalibration. Check
which one you have — it changes how expensive a new tag is by a lot.

---

## 5. The principles worth keeping

The commands will not transfer. These will.

### The database is the truth. The model is a cache.

Never let the cache become the store. Anything pushed into a running process is lost on
restart, so the push is an optimisation and the database is the record. A file in git is a
seed for a cold boot, not a source of truth — two sources of truth silently drift apart, which
is the failure this design exists to prevent.

### Ask what the model actually holds. Do not assume, and do not read a file.

The temptation is to track publish state in your own database — a `published_at`, a stored
fingerprint. Resist it. That records *what you think you published*, which is a different fact
from what the process is really serving, and the two diverge exactly when it matters: a push
that failed, a restart that reverted, a deploy that rolled back.

Reading the artifact off disk has the same flaw in a different coat, and it breaks the moment
the two tiers are on different machines.

### Say nothing when there is nothing to say.

An always-on "remember to publish" warning is one people learn to scroll past — and then a tag
sits unpublished with nothing pointing at it. That happened here. The banner only earns
attention by being silent when everything is current.

The corollary: when you genuinely cannot tell — the model did not answer, or it is too old to
report what you need — **claim nothing**. Do not flag everything as stale because a health
check timed out. That is the same lie in a new coat.

### Fingerprint the content, not just the names.

This one cost us a real bug, and it is the least obvious.

The staleness check originally compared *slugs*: which tags does the model know about. That
catches a tag the model has never seen. It is completely blind to a tag that was **edited** —
the slug is unchanged, so the check passes while the model scores against the old phrases.

Every phrase in a tag called `saloon` was rewritten from a western bar to a hair salon. The
admin page showed no warning at all. The playground went on matching *"a crowded old-fashioned
saloon"*, and everything looked fine.

The fix: have the model report a per-tag digest of what it is actually scoring with, and
compare against a digest computed the same way from the database row. Two rules make it work:

- **One definition, called by both sides.** Two implementations will drift, and then the
  check reports "stale" forever and nobody trusts it again. Here it lives in
  `inference/versioning.py`, the one module shipped to both tiers.
- **Digest only what can move a score.** Include the phrases and any per-tag scoring constant;
  exclude labels, descriptions and prose. Otherwise fixing a typo in a description reports the
  model as stale, and you are back to the always-on warning.

---

## 6. A checklist for the port

1. Which of the four shapes in §2 is the target? Answer Q1–Q3 first.
2. Zero-shot or trained (§3)? If trained, stop — most of this does not apply.
3. Do tags compete or score independently (§4)?
4. Copy the schema, the validation rules and retire-not-delete. These are the value.
5. Build the publish path only if §2 says you need one.
6. Whatever you build, make the UI tell the truth about what the model is *actually* serving —
   fingerprints, not names.
