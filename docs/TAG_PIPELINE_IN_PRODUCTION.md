# The tag pipeline in production — what breaks, and why

**What this is:** the operational counterpart to [`DEPLOY.md`](./DEPLOY.md). That one tells you
the commands. This one tells you what will bite you once they have run, and the reasoning behind
each answer.
**Spans:** `profile` (owns the catalog and publish) · `dooh-backend` (proxies, enforces) ·
`dooh-frontend` (admin UI, owner picker)
**Status:** ✅ current as of 2026-08-27. Every claim below is traced to a file and line.
**Related:** [`TAG_CRUD_IMPLEMENTATION.md`](./TAG_CRUD_IMPLEMENTATION.md) (what was built) ·
[`../../dooh-frontend/docs/SCREEN_CONTENT_POLICY.md`](../../dooh-frontend/docs/SCREEN_CONTENT_POLICY.md)
(the enforcement pipeline) ·
[`../../dooh-frontend/docs/ADMIN_TAG_MANAGEMENT.md`](../../dooh-frontend/docs/ADMIN_TAG_MANAGEMENT.md)
(the flag-first lifecycle)

---

## 1. The pipeline, and who owns what

```
   DOOH admin                    profile                      the model
   ──────────                    ───────                      ─────────
   create tag ──POST───────────► content_tags (Postgres)
                                      │
   press Publish ──POST──────────────►│ render packs.json ────► reload_packs
                                                                     │
   owner picker ◄──GET /content-tags──────────────────────────────────┘
        │                                                    (the live catalog
        ▼                                                     comes FROM the model)
   devices.blocked_tags
        │
        ▼
   creative upload ──► analyze ──► evaluatePolicy ──► allow / flag / block
```

Four different things, four different owners. Confusing them is the source of most questions:

| Thing | Owner | Where it lives |
|---|---|---|
| The tag itself — slug, label, description, positives, negatives | `profile` | `content_tags` |
| The live prompt pack the model scores against | `profile` | `inference/packs.json` → pushed to the model |
| How strictly a tag is enforced (`flag` / `block`) | `dooh-backend` | `content_tag_policy` |
| Which tags a screen refuses | `dooh-backend` | `devices.blocked_tags` (jsonb, max 64) |

DOOH **never** mirrors the catalog. It stores slug strings and proxies everything else. That is
deliberate and is argued at length in `ADMIN_TAG_MANAGEMENT.md` §3.1: the catalog doubles as the
negative space that keeps every tag's score honest, so a per-tenant catalog makes *every* tenant's
verdicts worse.

---

## 2. Nothing is live until Publish

`Detector.__init__` reads `packs.json` and encodes every prompt into embeddings **once, at
import**. No runtime path re-reads it, and the model has no access to Postgres. So:

- **Create** writes a row. The model has never heard of it.
- **Publish** renders the catalog to bytes and hands them to the model.

Both write handlers return `pending_publish: true` for exactly this reason, and
`tagverify/api/v1/tags.py` deliberately does *not* clear any cache on a write — a row that cannot
reach the model yet has no stale cache to clear.

**Publish is whole-catalog by nature.** One pack, one fingerprint; there is no such thing as
publishing a single tag, and an endpoint shaped `/tags/{slug}/publish` would be lying about that.
A consequence worth stating plainly: **if someone else has a half-finished edit pending, your
Publish takes it live.**

That is not a race to engineer around. It is the reason new tags default to `flag` mode — a tag
that goes live early *records*, it does not refuse anyone.

**It is not free.** Re-encoding the prompt table takes seconds and moves `packs_version`, which
means every tag reports `calibrated: false` until re-measured, `apply-calibration` refuses the
previous calibration file, and the raw-score cache goes cold. Batch your edits and publish once.

---

## 3. `packs.json` — what it is, and why it still exists

```
packs.json
├── $comment            ← authoring notes (the RULE 1 / RULE 2 measurements)
├── prompt_template     ← "This is a photo of {}."
├── shared_distractors  ← 6 phrases every tag competes against
├── defaults            ← threshold_low 0.30, threshold_high 0.55, sigmoid_floor
└── tags                ← the catalog  ← ONLY this comes from the database
```

The four file-level keys are listed at `tagverify/tags/packs.py:44`. They were never migrated into
`content_tags`, because they are not tags.

Given that tags now live in Postgres, the file survives for three reasons:

1. **The model cannot read the database.** It is a separate process — in production, a separate
   machine — with no credentials and no business having any. The catalog has to travel to it as
   data, and this file is that data.
2. **`packs_version` is a hash of its bytes.** That fingerprint keys the verdict cache
   (`creative_tag_analyses`), stamps calibration results, and answers "is my change live". To hash
   something you need one canonical, byte-stable serialisation.
3. **It is what the model boots from.** On startup there is no other source.

You could move the header into Postgres and generate the whole file (§4 recommends exactly that),
but a file — or at least a byte stream — remains, because the model needs bytes and
`packs_version` needs something to hash.

---

## 4. ✅ Fixed: Publish used to fail from the container

**Resolved 2026-08-27 by moving the header into Postgres.** Kept here because the shape of the
bug explains why `pack_header.body` is the type it is, and someone will eventually be tempted to
undo that.

**The symptom was:** an admin creates a tag, it saves, they press Publish and get a 400. The tag
never reaches the model — half the feature works.

**The cause** was a circular read. Publish had to **open** `packs.json` to **write**
`packs.json`, because the header existed nowhere else, and `Dockerfile:21` copies only three
files out of `inference/` to keep the ML stack out of the web image. On a checkout the file is
right there, so it never showed up in development.

Refusing was the right behaviour — writing a header-less pack would silently break scoring for
every tag at once, since `prompt_template` and `shared_distractors` are live scoring inputs.

### What changed

`pack_header` — one row, one `body` column holding the four keys as text. `render_catalog`
(`tagverify/tags/publish.py:45`) reads that instead of the file, so the export is now
`database → bytes` and needs nothing on disk. `dooh seed-tags` populates it from `packs.json`
on the one-time import, non-destructively. `TAG_CRUD_IMPLEMENTATION.md` §3's one loose end —

> After `seed-tags`, `packs.json` is output, never input — **except for that file-level block**

— no longer has an exception.

### 🔺 `body` is TEXT, and that is load-bearing

`packs_version` hashes the exported file's **bytes**, so a header that comes back from storage
with its keys in a different order is a different pack to every downstream fingerprint: every tag
flips to `calibrated: false`, `apply-calibration` refuses the existing calibration file, and the
score cache goes cold — all for a change that moved no score.

JSONB does not preserve key order. `packs.py:46-48` already says so about `_EXTRA_ORDER`, for the
same reason.

**The sharp edge is one level down, which is easy to miss.** `render_pack` normalises the
*top-level* keys itself — it emits `FILE_LEVEL_KEYS` in a fixed order — so reordering those is
harmless. But `defaults` is copied through as a nested dict and nothing re-orders its five keys
(`threshold_low`, `threshold_high`, `//sigmoid_floor`, `sigmoid_floor`, `escalate`). That is where
JSONB would have bitten, silently.

`test_nested_header_key_order_is_load_bearing` in `tests/test_packs.py` pins exactly this: it
asserts a nested reorder *does* change the bytes, so if that ever stops being true the test fails
rather than quietly protecting nothing.

---

## 5. 🔴 Failure: a model restart silently disables enforcement

**This is the one that can actually hurt you.** It is silent, and it degrades compliance.

**Cause.** A free Hugging Face Space sleeps after ~48h. When it wakes it **rebuilds from its own
git repo**, so it reverts to the `packs.json` committed at the last `git subtree push`. Every tag
created through the admin UI since then is gone from the model. `tagverify/cli.py` says so
directly: *"On a Hugging Face Space this is a cache, never a store."*

**What that does**, traced through code that already exists:

1. The slug is missing from `catalogMap` →
   `dooh-backend/src/content-verification/creative-verification.service.ts:155` logs a warning and
   produces no verdict for it.
2. `evaluatePolicy` sees fewer verdicts than blocked tags →
   `dooh-backend/src/content-verification/policy.ts:217-220` falls through to `NOT_VERIFIED`.
3. `NOT_VERIFIED` is a **flag, not a block**. The upload proceeds.

So screens quietly stop enforcing the categories their owners chose, and **nothing surfaces it**.
No error, no toast, no log anyone is reading. For a compliance tool that is the worst available
failure mode.

**Nothing re-publishes automatically.** `tagverify/main.py:50` (`lifespan`) initialises the
database engine and starts the usage pruner. It does not check the model.

### Fixes

**Structural — stop using an ephemeral host.** See §8. A persistent disk means the file publish
writes is the file the model boots from, and the problem cannot occur.

**Belt and braces — auto-resync.** Both halves already exist: `catalog.publish_state()`
(`tagverify/tags/catalog.py:391`) diffs active rows against the model's live slugs *and prompt
fingerprints*, and `publish_catalog()` (`tagverify/tags/publish.py:94`) does the push. Wire them
into `lifespan` plus a timer.

One rule when doing it: **claim nothing when the model does not answer.** `publish_state` already
returns `None` for "cannot tell", and a failed health check must never trigger a push.

**Visibility — surface it in DOOH admin.** profile's own `/admin` shows a per-row "not live" chip
built from exactly this comparison. `GET /content-tags/managed` does not carry it, so the DOOH
admin screen cannot tell you the model is stale — it only has a standing prose notice that shows
whether or not anything is pending, which is the always-on warning people learn to scroll past.

---

## 6. `RELOAD_SECRET` — two places, two mechanisms

Publish does **two** jobs, and they fail independently:

```
1. Build the pack      (database + header → bytes)   ← §4 is about this
2. Push it to the model (bytes → running model)      ← this section
```

Job 2 needs the secret to match on both sides, and the model to be awake.

| | Local | Production |
|---|---|---|
| **profile** | `.env.local` (`tagverify/config.py:22` loads `.env`, `.env.local`) | container env var |
| **the model** | the `RELOAD_SECRET=… python app.py` command | HF Space → Settings → *Variables and secrets*, **or** a container env var |

**The asymmetry is the confusing part.** profile reads a file. The model reads
`os.environ.get("RELOAD_SECRET", "")` at `inference/app.py:198` and nothing else — there is **no
dotenv, no config loader in the model tier at all**. A `.env` file will never work there, which is
why it goes on the command line.

Both sides default closed. Unset means publishing is refused, never opened — the Space is shielded
only by HF privacy and a read-scoped token that is already deployed in two places, so a mutating
endpoint needs a secret of its own.

### Telling failures apart

| Message | Cause |
|---|---|
| `packs.json does not exist…` | §4 — the header problem |
| `RELOAD_SECRET is not set — refusing to publish` | Unset on **profile's** side |
| `reload_packs is disabled: RELOAD_SECRET is not set on this Space` | Unset on the **model's** side |
| `reload_packs: bad secret` | Both set, but they **do not match** |
| `The model is starting up and cannot accept a pack yet` | Model warming — retry |

**A mismatch is a loud, fail-safe failure.** `require_publishing_configured()` runs *before*
anything is written, so a wrong secret cannot leave a half-published state — the model keeps
serving its old pack, and the admin who pressed the button sees *"Nothing was changed."*

It is a setup problem, not a runtime one. The narrow ways it bites later: rotating one side and
forgetting the other, or recreating the Space from scratch. **One `docker-compose.yml` where the
value is defined once and referenced by both services makes drift impossible.**

---

## 7. Two tiers, yes. Two machines, no.

The `tagverify/` ⁄ `inference/` split is worth keeping. `AGENTS.md` makes it a rule: never add
torch, transformers or gradio to `pyproject.toml`, and never import `inference.detector` from
`tagverify/`. It is what keeps web deploys fast and CI slim.

**Correct the size figure while you are here.** The "~2GB" everyone quotes is the *CUDA* build.
`inference/requirements.txt` already pulls from `https://download.pytorch.org/whl/cpu`, and its own
comment says that is precisely what avoids "~2GB of unused CUDA libraries." The real number is
roughly 250–400MB of wheels plus the weight cache at runtime. Fat, not absurd.

**So why not one process?** Worker fan-out, and it is a correctness problem rather than a cost one.
`DEPLOY.md` says to run one uvicorn worker per core. Workers are separate processes, so each would
hold **its own detector** — and a publish would reach one of them, leaving the rest on the old
pack. Which verdict an advertiser got would depend on which worker answered.

Also real, in descending order: ~1–2GB resident per process; the ~4.8s prompt encode plus a weight
fetch on every web restart, when a web deploy is currently seconds; and CI would start pulling the
ML stack for tests that skip cleanly today.

**None of that argues for two *machines*.** Two containers on one host keeps every benefit of the
split and removes §4, §5 and §6 at a stroke.

---

## 8. Recommended deployment

**One box, two containers, `docker-compose`, in the same region as Neon and `dooh-backend`.**

Region matters more than usual: `DEPLOY.md` says keep the app near Neon, and `dooh-backend` calls
profile **synchronously inside a 7-second upload deadline**
(`creative-verification.service.ts`, `BATCH_DEADLINE_MS`). All three want to be close.

Sizing: 2 vCPU / 4GB is comfortable. The evidence is that it already runs on the free Space's two
cores, and `Detector.rebuilt()` deliberately shares the weights across a reload, so publishing does
not double resident memory.

| Problem today | Why one box removes it |
|---|---|
| Space naps and reverts the catalog (§5) | Persistent disk — publish writes the file the model boots from |
| Publish 400s from the container (§4) | Shared volume: the file is present |
| Secret drift across two config screens (§6) | One compose file, value defined once |
| Worker fan-out (§7) | Still one detector, unchanged |

**Bake the weights.** `inference/detector.py:169` calls
`AutoModel.from_pretrained("google/siglip2-base-patch16-224")`, which downloads from
huggingface.co on first run. Pre-fetch the cache into the image and set `HF_HUB_OFFLINE=1`, or a
cold container start waits on a third party being online before it can serve.

### What is ruled out, and why

**Serverless is structurally excluded** — not slow, wrong. `reload_packs` swaps an **in-memory**
prompt table; that *is* the publish mechanism. Anything that scales to zero or runs per-request
throws it away: every cold start re-encodes, and a publish reaches only whichever instance
answered. So: no Vercel (already removed from this project), no Lambda, no Cloud Functions, no
Workers, no Cloud Run with scale-to-zero.

**GPU platforms are unnecessary.** Replicate, Modal and friends solve a problem you do not have —
`detector.py` has no device handling; this is CPU-only.

**If you would rather not run a VM:** Render or Fly.io. Both keep a container alive and offer
persistent disks, which is the requirement. More expensive at this size, less ops.

---

## 9. Open gaps, ranked by danger

| | Gap | Why it ranks here |
|---|---|---|
| 🔴 | **Model restart reverts the catalog** (§5) | Silent, and screens stop enforcing what owners chose |
| 🔴 | **No review surface for flagged creatives** | See below |
| 🟡 | **Publish broken in the container** (§4) | Loud — an admin sees the error and it gets fixed |
| 🟡 | **Secret drift** (§6) | Loud and fail-safe |
| ⚫ | **Two leaked credentials** | Must rotate before production regardless |

**The review surface is worth expanding on.** New tags default to `flag`, which records hits
instead of blocking them — that default is what makes admin-authored tags safe to allow at all.
But nothing renders those records. `needsReview`, `findings` and `topPhrase` are plumbed all the
way to the frontend types with **zero display sites**, and `SCREEN_CONTENT_POLICY.md` marks that
phase 🔴 not started while calling the admin override *"the only route back for a wrongly rejected
advertiser."*

So today: every `flag` hit goes into a queue nobody can read, and a `block`-mode tag that wrongly
refuses an advertiser has no recourse path. Flag-first *increases* the volume of flags, so this
gets more urgent as the catalog grows, not less.

**The credentials:** `DEPLOY.md` records that the Neon `DATABASE_URL` was shared in a transcript
and never reset. `RELOAD_SECRET` has since been exposed the same way. Rotate both — and rotate
`RELOAD_SECRET` on **both sides at once**, or publishing stops until they agree.

---

## 10. Fixes, in order

1. **Rotate both credentials.** Neon dashboard → Roles → reset password; and a fresh
   `RELOAD_SECRET` on profile and the model tier together.
2. **`packs.json` into the image** (§4) — one line, unblocks Publish in production.
3. **Move off the ephemeral host** (§8) — this is the one that removes a class of problems rather
   than patching one.
4. **Auto-resync on startup and on a timer** (§5) — the safety net, worth having even after 3.
5. **Surface sync state in DOOH admin** (§5) — so a stale model is visible where people work.
6. **Header into Postgres** (§4) — makes 2 permanently unnecessary and completes the
   one-source-of-truth design.
7. **Build the review surface** (§9) — the largest remaining product gap.

Items 1–3 are what stands between the current code and a defensible production deployment.
