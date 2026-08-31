# DOOH Tag Verification

**Does this ad creative actually contain the content it is tagged with?**

In digital out-of-home ad ops a creative arrives tagged `alcohol`, `gambling`, `vaping` and so
on, and those tags drive placement rules — you cannot put alcohol on a screen outside a school.
This service checks the tags against the pixels — for stills, and for video, where it samples
the scenes and tells you at which second the content appears.

It is a compliance tool, and that shapes every design decision in it. Most importantly: a
verdict can be **`present: null`**, meaning *uncertain — a human has to look*. That is not the
same as absent, and the API, the UI and the docs all go out of their way to keep the two apart.

It also **owns the tag catalog**. Tags are created and edited here — through the admin UI or the
JSON API — and published to the model as one pack. The sibling `dooh-backend` proxies those
writes and enforces the verdicts; it never mirrors the catalog.

---

## Contents

- [How it fits together](#how-it-fits-together)
- [Repository layout](#repository-layout)
- [Setup](#setup)
- [Configuration](#configuration)
- [Commands](#commands)
- [Adding or changing a tag](#adding-or-changing-a-tag)
- [Testing](#testing)
- [The API](#the-api)
- [Serving on a LAN](#serving-on-a-lan)
- [Things worth knowing before you change anything](#things-worth-knowing-before-you-change-anything)

---

## How it fits together

Two paths run through this repo. The first answers a question about a creative:

```
                  browser (playground)          integrator (API key)
                        │                              │
                        └───────────┬──────────────────┘
                                    ▼
                       tagverify/analyze/run.py            ← one shared pipeline
                                    │
              ┌─────────────────────┼──────────────────────┐
              ▼                     ▼                      ▼
        result cache          thresholds            SigLIP 2 detector
        (Postgres)            (Postgres)            (HF Space, inference/)
                                    │
                                    ▼
                          inference/banding.py       ← the verdict rule,
                                                       shared by all three callers
```

Both entry points — the browser playground and an API key holder — run the *same* pipeline.
There is no demo path that behaves differently from the real one.

The second path decides *what the questions can be* — the catalog the model scores against:

```
   admin UI    ─┐
                ├──►   content_tags    ──dooh export-packs──►  inference/packs.json
   DOOH admin  ─┘      pack_header                                      │
   (tags:write)        (Postgres)                          push_packs (RELOAD_SECRET)
                                                                        ▼
                                                              SigLIP 2 (HF Space)
                                                              reload_packs — no restart
```

**A saved tag is a database row. It is not live until the catalog is published.** The detector
encodes every prompt into embeddings once, at import, and has no access to Postgres — so a
`POST /api/v1/tags` writes a row the model has never heard of. That is why every write handler
returns `pending_publish: true`.

There is no draft/active state machine; `content_tags.status` is only `active` or `retired`.
Publishing is **whole-catalog by nature** — one pack, one `packs_version` fingerprint — with
two consequences worth knowing before you press it:

- If someone else has a half-finished edit pending, **your publish takes it live.** New tags
  default to `flag` mode in `dooh-backend` for exactly this reason: a tag that goes live early
  records what it would have blocked, it does not refuse anyone.
- Publishing moves `packs_version`, so **every** tag reverts to `calibrated: false` until the
  catalog is recalibrated — not just the one you edited.

See [`docs/TAG_PIPELINE_IN_PRODUCTION.md`](docs/TAG_PIPELINE_IN_PRODUCTION.md) for what breaks
once this is deployed, and who owns which half.

## Repository layout

| Path | What it is |
|---|---|
| `tagverify/` | The web application: FastAPI, Jinja2 templates, HTMX. One process serves the API, the playground, the docs and the admin. |
| `tagverify/tags/` | The catalog: `catalog.py` validates and does CRUD over `content_tags`, `packs.py` is pure read/write of the pack file, `publish.py` renders the catalog and pushes it to the model, `decide.py` holds the thresholds side of `decision_version`. |
| `tagverify/scoring/client.py` | Our client **for** the model tier. Keep the two words apart: `inference/` is the tier, `scoring/` is the client. |
| `tagverify/analyze/video.py` | Decoding a video and choosing which frames are worth scoring. Keyframes, then a colour-aware dedupe. |
| `tagverify/analyze/aggregate.py` | The rule that collapses per-frame verdicts into one per tag. Pure, like `banding.py`, and tested the same way. |
| `inference/` | The SigLIP 2 detector, deployed separately to a Hugging Face Space. Has its own `requirements.txt` and virtualenv on purpose — the web app must never depend on torch. |
| `inference/banding.py` | The rule that turns a score into a verdict. Imported by the detector, by the calibration sweep, and by the API. **The single source of truth — do not copy it.** |
| `inference/packs.json` | The live prompt pack. **Generated** from the database by `dooh export-packs` — do not hand-edit its `tags` array. |
| `migrations/` | Alembic. Brings an **existing** database forward; a new one is provisioned from `docs/schema.sql`. |
| `tests/` | pytest. Runs against the ASGI app in-process; tests needing the database or the model skip cleanly when those are not configured. |

### `docs/`

| File | What it is |
|---|---|
| [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Exhaustive `file:line` reference for every module, route, table and constant. It describes; it does not govern. |
| [`DEPLOY.md`](docs/DEPLOY.md) | Deployment: the three targets (repo, HF Space subtree push, container), regions, keepalive, credential rotation. |
| [`schema.sql`](docs/schema.sql) | The schema as it should *be* — provisions a new database by hand. |
| [`TAG_PIPELINE_IN_PRODUCTION.md`](docs/TAG_PIPELINE_IN_PRODUCTION.md) | The operational counterpart to `DEPLOY.md`: what bites you after the commands have run, and why. |
| [`TAG_CRUD_IMPLEMENTATION.md`](docs/TAG_CRUD_IMPLEMENTATION.md) | The authority on what actually shipped for tag CRUD, and what implementation changed about the plan. |
| [`TAG_MANAGEMENT_API.md`](docs/TAG_MANAGEMENT_API.md) | **Superseded** design doc. Kept for its measurements and its analysis of what a tag is; its file paths predate the `dooh` → `tagverify` rename. |
| [`PORTING_THE_TAG_CATALOG.md`](docs/PORTING_THE_TAG_CATALOG.md) | How much of the editable-catalog feature carries to another project: the CRUD half ports as-is, the publish half does not. |
| [`ADMIN_CREATIVE_REVIEW_SURFACE.md`](docs/ADMIN_CREATIVE_REVIEW_SURFACE.md) | Planned, not started — a DOOH admin "this ad needs review" surface. Needs no change in this repo. |

**Two virtualenvs, deliberately.** `.venv` at the repo root serves HTTP; `inference/.venv` runs
the model. Never add `torch`, `transformers` or `gradio` to `pyproject.toml`, and never import
`inference.detector` from `tagverify/` — the split is what keeps a web deploy from pulling 2GB of ML
wheels. The one shared file is `inference/banding.py`, which imports nothing.

## Setup

### Prerequisites

**Python ≥ 3.12** and a Postgres database (Neon in every deployed environment). That is all.
There is no asset build step: the CSS is hand-authored, htmx is vendored, and there is no
`package.json` — nothing here needs Node.

### 1. Install

```bash
make install          # creates .venv and installs the package in editable mode, with dev extras
```

This gives you the `dooh` CLI at `.venv/bin/dooh`. Activate the venv, or prefix the commands
below with `.venv/bin/`.

### 2. Configure

```bash
cp .env.example .env  # then fill it in — see Configuration below
```

`.env.local` is also read, so an existing checkout keeps working. If both files set the same key,
**`.env.local` wins** — it is read second, and pydantic-settings gives the last file priority.
Real environment variables outrank both. (`make dev` is the one exception worth knowing: it
passes the *first* of the two that exists to `uvicorn --env-file`, which injects it as real
environment variables, so under `make dev` a key set in `.env` does win.)

### 3. Create the database

A **new** database is provisioned from the schema file:

```bash
export $(grep -E '^DATABASE_URL=' .env)   # psql reads the shell, not .env
psql "$DATABASE_URL" -f docs/schema.sql
```

An **existing** database is brought forward with Alembic:

```bash
make migrate          # alembic upgrade head
make migrate-sql      # the same, printed as SQL instead of applied — a dry run
```

The two are not alternatives. `schema.sql` describes the destination; the migrations describe
how a database that already holds data gets there. **A new column belongs in both files**, or
provisioning and migrating diverge. Alembic reads `DATABASE_URL` through the app's own settings
(`migrations/env.py`), so the credential never lands in a tracked file.

### 4. Seed it

```bash
dooh seed-thresholds  # tag_thresholds, from inference/packs.json
dooh seed-tags        # content_tags + pack_header, from inference/packs.json
```

Both are non-destructive and safe to re-run: `seed-thresholds` never overwrites a row marked
`calibrated`, and `seed-tags` never overwrites an existing tag. Run them **from a checkout, not
from the container** — the image deliberately ships only three files out of `inference/`, and
`packs.json` is not one of them.

### 5. Start the model tier

To run the model locally instead of against a deployed Space:

```bash
cd inference
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python app.py                    # serves on :7860
```

Then set `INFERENCE_URL=http://127.0.0.1:7860` and leave `HF_SPACE` unset. The first run
downloads ~400MB of SigLIP 2 weights; after that startup is a few seconds. It is ready when the
log says `[detector] ready: 23 tags`.

To use the deployed Space instead, set `HF_SPACE` and `HF_TOKEN` and skip this step entirely —
`HF_SPACE` takes precedence over `INFERENCE_URL`.

### 6. Run

```bash
make dev              # http://127.0.0.1:8000
```

Then confirm both tiers are actually reachable — without starting a second server:

```bash
dooh health
```

It prints the same report as `GET /api/v1/health`, with `database` and `inference` called out
separately. `degraded` with `inference.warming: true` is normal for the first minute against a
cold Space.

## Configuration

Read once at import by `tagverify/config.py`, from the environment and from `.env` / `.env.local`.
**Nothing raises on a missing value** — a missing `DATABASE_URL` degrades to a
`/api/v1/health` response that reports the problem, rather than a process that refuses to boot.
Monitoring you cannot reach is not monitoring.

| Variable | Required | What it does |
|---|---|---|
| `DATABASE_URL` | yes | Postgres. Use the **pooled** connection string, and keep it in the same region as the app — see the region warning in `docs/DEPLOY.md`. |
| `HF_SPACE` | one of these two | The inference Space, as `owner/space-name`. Takes precedence over `INFERENCE_URL`. |
| `INFERENCE_URL` | | Local-dev escape hatch — a direct URL to a locally running `inference/app.py`. Leave unset in production. |
| `HF_TOKEN` | for a private Space | Needs **read** access only. Write access is for pushing the Space, not for running it. |
| `ADMIN_PASSWORD` | yes, in practice | Gates `/admin`. **Unset means refused, never open.** |
| `RELOAD_SECRET` | to publish | Shared secret for pushing a new pack into the running model. Must be the **same value** as `RELOAD_SECRET` in the Space's own environment; publishing is refused if either side is unset. It is not the `HF_TOKEN` and should not be set to it. |
| `LOG_LEVEL` | no | Defaults to `INFO`. |
| `PLAYGROUND_RATE_LIMIT_PER_MIN` | no | Defaults to 20. The playground carries no API key by design, so it is limited per IP. |
| `ADMIN_LOGIN_ATTEMPTS_PER_MIN` | no | Defaults to 5. |

## Commands

The `dooh` CLI, in roughly the order a catalog change moves through it:

```bash
dooh seed-tags                    # import packs.json into content_tags + pack_header (once)
dooh seed-thresholds              # populate tag_thresholds from inference/packs.json
dooh export-packs                 # render packs.json FROM the database
dooh push-packs                   # push an existing packs.json into the running model
dooh apply-calibration            # apply measured thresholds from calibrate.py
dooh create-key "Client name"     # issue an API key (printed once, only a hash is stored)
dooh prune-usage                  # drop expired rate-limit rows
dooh health                       # the /api/v1/health report, without a server
```

**`export-packs`** takes `--out` (default `inference/packs.json`), `--check` (report what would
change and exit 1 rather than writing), and `--push` (write, then publish live in one step). It
distinguishes a **reformat** from a **content change** and tells you which you have — the
distinction matters because the first export after moving the catalog into the database is
semantically inert but still moves `packs_version`, since that is a hash of the file's bytes.
A reformat needs the calibration re-stamped; a content change needs the catalog recalibrated.

**`push-packs`** sends the pack file's own bytes to the Space's `reload_packs` endpoint, so a
new catalog goes live **without a restart**. It needs `RELOAD_SECRET` on both sides. If the new
pack fails to encode, the model keeps serving the previous one and says so.

**`create-key`** takes `--rate-limit N` (default 60) and `--tags-write`, which adds the
`tags:write` scope. Issue that as a **separate key** — do not add the scope to a key already in
use for analyze. A key that scores creatives and a key that can change what scoring means are
different blast radii.

`seed-thresholds` and `seed-tags` are safe to re-run: neither overwrites existing rows, so
re-seeding cannot clobber measured thresholds with the guesses in `packs.json`.

## Adding or changing a tag

Through the admin UI at **`/admin?tab=tags`** or the JSON API — never by editing `packs.json`.
The file is generated, so a hand edit to its `tags` array is silently reverted by the next
export. (Everything *outside* that array — `$comment`, `prompt_template`, `shared_distractors`,
`defaults` — lives in the `pack_header` row and is preserved verbatim.)

The rules `tagverify/tags/catalog.py` enforces, and why:

- **At least 5 positives and 6 negatives** (8 and 10 are advised). Below that the softmax pool
  is too thin to be honest.
- **Slugs match `^[a-z][a-z0-9_]*$` and cannot be renamed.** A slug already in use *or retired*
  is refused — DOOH stores slug strings, so reusing one silently re-points existing screen rules.
- **A positive phrase may not be shared with another active tag's positives.** `detector.py`
  subtracts a tag's own phrases from its rival pool, so a shared phrase gets dropped from
  **both** pools and helps neither tag.
- **Mirror the phrasings.** If a positive says *"a scoop of protein powder"*, a negative must say
  *"a scoop of <something else>"* — a real measurement went from 0.709 (wrongly present) to 0.224
  on that one change. Name the confusable neighbour, not something random: `a plain brick wall`
  teaches the model nothing about alcohol; `a bottle of fruit juice` is what actually gets
  mistaken for it. Unmirrored phrasing is flagged as a warning, not an error.
- **Retire, never delete.** `retire_tag` sets `status='retired'`; there is no hard delete. A
  deleted slug is stranded in DOOH's `devices.blocked_tags`, where every upload then falls
  through to `NOT_VERIFIED` forever.
- **The cap is 100 active tags**, mirroring `MAX_TAGS_PER_CALL` in `inference/app.py` — DOOH
  sends the whole catalog on every analyze call, so exceeding it fails every upload, not just
  the new tag.

Then publish:

```bash
dooh export-packs --push
```

**There is no publish button in the admin UI.** The tags panel shows a banner naming this
command and lists which slugs are unpublished or edited. Publishing is whole-catalog — re-read
[How it fits together](#how-it-fits-together) if that is news — and it moves `packs_version`,
so the catalog needs recalibrating afterwards, not just the tag you touched.

## Testing

```bash
make check              # ruff + the full pytest suite — run this before committing
make test-fast          # the pure decision rules and intake: no DB, no model, no network
make typecheck          # mypy, on demand
make inference-smoke    # 17 golden ML cases; only needed if you touched inference/
```

`make check` is **lint plus tests only** — `typecheck` is deliberately not folded into it,
because mypy is not clean yet and a red `check` that everyone learns to ignore is worse than no
target at all. Fold it in once it passes.

**There is no CI.** `.github/workflows/` holds one job, `keepalive.yml`, which pings the Space
every six hours so a free Space does not sleep. Nothing runs lint or tests on push — `make
check` on your machine is the gate.

The suite is 18 files: `tests/api/` for the JSON API, `tests/support/` for shared helpers (an
in-memory MP4 encoder, so video tests need neither fixtures on disk nor a network), and the rest
at the top level. Two markers in `tests/conftest.py` gate the rest: `needs_db` skips when
`DATABASE_URL` is unset and `needs_inference` when neither `HF_SPACE` nor `INFERENCE_URL` is.
Both read through `Settings`, not `os.environ`, so your `.env` counts.

`tests/test_banding.py` is the highest-value file in the repo. If you change how a verdict is
decided, it should fail. If it doesn't, the test is wrong. `tests/test_aggregate.py` is its
counterpart for video: if you change how frames combine, that one should fail.

## The API

Full reference at `/docs` in the running app — that is a hand-written page; FastAPI's own
`/docs`, `/redoc` and `/openapi.json` are disabled.

| Method | Path | Auth |
|---|---|---|
| `POST` | `/api/v1/analyze` | API key |
| `GET` | `/api/v1/tags` | API key |
| `GET` | `/api/v1/usage` | API key |
| `GET` | `/api/v1/health` | none — always 200 |
| `GET` | `/api/v1/tags/managed` | `tags:write` |
| `POST` | `/api/v1/tags` | `tags:write` |
| `PATCH` | `/api/v1/tags/{slug}` | `tags:write` |
| `DELETE` | `/api/v1/tags/{slug}` (retires) | `tags:write` |
| `POST` | `/api/v1/tags/publish` | `tags:write` |

Keys go in `x-api-key` or `Authorization: Bearer`. The `tags:write` routes accept **either** a
scoped API key **or** a logged-in admin session with a valid CSRF token, which is what lets the
admin UI and an integrator share one implementation. A plain analyze key carries no scopes and
is refused, with an error that names the scope it lacks rather than a bare 403.

Write handlers return `"pending_publish": true` and deliberately do **not** reset any cache — a
row that cannot reach the model yet has no stale cache to clear. Only `POST /tags/publish` does.

```bash
curl -X POST http://localhost:8000/api/v1/analyze \
  -H "x-api-key: dooh_live_..." \
  -F "media=@creative.jpg" \
  -F "tags=alcohol" -F "tags=gym_fitness"
```

A video goes to the same endpoint, in the same field. Which kind it is comes from the bytes,
never the filename or the content type:

```bash
curl -X POST http://localhost:8000/api/v1/analyze \
  -H "x-api-key: dooh_live_..." \
  -F "media=@creative.mp4" -F "tags=alcohol"
```

```jsonc
{
  "results": [{
    "tag": "alcohol",
    "present": true,
    "evidence": {
      "crop": [0, 0.5, 0.5, 1],
      "frame": { "index": 2, "timestamp_s": 2.0 }   // ← at which second
    }
  }],
  "media": { "kind": "video", "duration_s": 9.6, "frames_analyzed": 4, "frames_considered": 4 }
}
```

```jsonc
{
  "results": [{
    "tag": "alcohol",
    "present": true,          // true | false | null  ← null means NEEDS A HUMAN
    "score": 0.977,
    "confidence": "high",
    "decided_by": "siglip",   // or "sigmoid_floor" when the absolute veto fired
    "calibrated": false,      // ← the thresholds were never measured; this is a guess
    "evidence": { "top_phrase": "a glass of beer with foam", "crop": [0, 0.5, 0.5, 1] }
  }],
  "uncalibrated_tags": ["alcohol"]
}
```

`GET /api/v1/health` needs no key and always returns 200 with the status in the body, so it
works as a monitoring target — any non-200 means the app itself is down, rather than something
a monitor has to parse degradation out of.

## Serving on a LAN

`make dev` binds the loopback only, which is the right default for a `--reload` server. To reach
it from a phone, a colleague's laptop or a test screen:

```bash
make serve-lan          # binds 0.0.0.0 and prints the address to open
```

The banner's address is guessed from the first non-loopback interface. Pin it when the guess
picks the wrong one, or to keep it stable across DHCP leases — `PORT` overrides the same way:

```bash
make serve-lan LAN_IP=192.168.1.24 PORT=8000
```

Then, from the other machine, check reachability before blaming the app:

```bash
curl -s http://192.168.1.24:8000/api/v1/health
```

Two things to know before you do:

- **Do not add `--proxy-headers` for direct LAN access.** It makes uvicorn trust
  `X-Forwarded-For`, so any device on the network could spoof its IP and walk past the per-IP
  playground rate limit. Use it only behind a real reverse proxy, with `--forwarded-allow-ips`
  set to that proxy's address. (The `Dockerfile` does set it, correctly — there is a proxy there.)
- **`/admin` is reachable too.** The session cookie is issued without `Secure` over plain HTTP
  (correct — a `Secure` cookie would be silently dropped and login would appear to do nothing),
  which also means the password crosses the network in the clear. Fine on a trusted office LAN;
  do not do it on shared or public Wi-Fi.

<details>
<summary><strong>If a device cannot connect</strong> — read the failure mode first, it narrows things faster than guessing</summary>

A **timeout** means the packets are being dropped somewhere in between; **connection refused**
means they arrived and nothing was listening, so the IP or the port is wrong; an **SSL error**
means the browser force-upgraded to HTTPS, and this server is plain HTTP.

For a timeout, work outwards, not inwards. The host firewall is the last suspect, not the first:

1. **Same subnet?** "Same Wi-Fi" is not the same network. A mesh node, a range extender doing
   its own NAT, or a guest SSID will hand out a different range under the same name. Compare
   the *default gateway* on both machines — `ipconfig` on Windows, `ifconfig` / `netstat -rn`
   here. If they differ, that is the whole answer, and no setting on this host will fix it.
2. **Do the packets even arrive?** This is the test that settles it:

   ```bash
   sudo tcpdump -ni en1 'tcp port 8000'      # then load the page on the other device
   ```

   Nothing at all means the network dropped it — most often **AP / client isolation** on the
   router, which lets every device reach the gateway and the internet but not each other. It is
   on by default on a lot of ISP routers, and always on for guest SSIDs. Look for "AP
   Isolation", "Client Isolation" or "Wireless Isolation" in the router admin. A SYN with no
   reply means this host dropped it — go to step 3.
3. **Only now, the host firewall.** System Settings → Network → Firewall, plus
   `sudo pfctl -s info` if something has installed `pf` rules.

A quick check for isolation without any of the above: ping another device on the LAN from here.
Reaching the router but nothing else is the signature.

</details>

## Things worth knowing before you change anything

**The sigmoid floor is a veto, and it is checked first.** Softmax must sum to 1, so an image
containing nothing relevant can still hand a large share of the mass to a positive prompt purely
by beating equally-irrelevant options. Without the veto, a photo of a mountain reads as alcohol.
Keep the floor low — measured true positives run as low as 0.028.

**The catalog is also the negative space.** Each tag is scored by softmax over its positives,
its hard negatives, the shared distractors — and every *other* tag's positives. Without that
last part a pool that cannot describe the image hands its mass to the tag's positives by
default: a gym poster scored 0.97 for `gambling` because gambling's negatives are all board
games and machines and nothing in the pool described a designed poster, while `gym_fitness` sat
unused in the same catalog. It costs nothing (the logits already cover every prompt) and it
means a tag's coverage of the real ad space now decides how well every *other* tag behaves.

A second sigmoid check above the floor was tried for this and removed — see `band_of`. The
sigmoid's scale is per-tag, so no global band separates a false positive at 0.0104 from a true
one at 0.0108.

**This is also why the catalog is shared, not per-tenant.** A per-tenant catalog would make
*every* tenant's verdicts worse, because each one's negative space would shrink to their own
tags. `dooh-backend` stores slug strings and proxies; it never mirrors the catalog.

**`packs.json` is generated.** The catalog lives in `content_tags` and `pack_header`, and
`dooh export-packs` renders the file from them. A hand edit to the `tags` array survives exactly
until the next export. `render_catalog` reads no file at all when it renders — that circular
read is why publishing once worked from a checkout and failed in Docker.

**Publishing takes everyone's edits live.** One pack, one fingerprint; there is no such thing as
publishing a single tag, and an endpoint shaped `/tags/{slug}/publish` would be lying about
that. It also moves `packs_version`, so the whole catalog reverts to `calibrated: false`.

**Retire, never delete.** A hard delete strands the slug in DOOH's `devices.blocked_tags` and
every upload against that screen falls through to `NOT_VERIFIED` forever.

**`decision_version` fingerprints the other half — the rule and the cutoffs.** `packs_version`
tells a caller when the model's numbers changed; this tells them when the way those numbers are
read changed. Both are needed, and the gap was not academic: an integrator caching our verdicts
keyed on the pack alone kept serving a refusal we had already decided was wrong, and never
called back to find out — quietly undoing the retroactivity the raw-score cache exists to
provide. It is a one-way hash, so it exposes no thresholds. It also answers *"is my change
live?"*: `decision.rule` in `/api/v1/health` is computed from the bytes of the banding module
this process **loaded**, once, at import. Compare it with `shasum -a 256 inference/banding.py`
— if they differ, the server is stale and needs restarting. A server started without `--reload`
once served a superseded rule for hours with nothing anywhere saying so.

**`packs_version` fingerprints the prompt pack.** If a threshold was calibrated against a
different pack than the one now serving, the threshold is still applied — it is the best
available — but `calibrated` reverts to `false`, because it no longer describes the scores being
produced.

**An unknown tag is an error, never a silent skip.** If a caller misspells `alcohol` the whole
request is refused. "We didn't check" and "we checked and it's clean" mean opposite things.

**The media is never stored.** Only its sha256, which is enough for dedupe, caching and an
audit trail. Video is decoded from an in-memory buffer and never touches the disk either.

**A video is present if ANY frame is present.** Each sampled frame is scored and *decided*
independently, and only then are the verdicts collapsed: present beats uncertain beats absent,
and the highest-scoring frame inside the winning band supplies the evidence. Deciding first is
what matters — the sigmoid floor is a per-frame veto, so ranking frames by raw score would let
a vetoed 0.92 frame beat a genuine 0.60 one and reintroduce "a mountain reads as alcohol" one
level up. See `tagverify/analyze/aggregate.py`.

**A frame we could not analyse fails the whole request.** There is no partial video result.
Returning verdicts computed over four frames of six, with nothing saying so, is "we didn't
check" presented as "we checked and it's clean".

**Frames are sampled, not exhaustive.** Keyframes first (the encoder's own scene-cut signal),
then a colour-aware dedupe, then a cap. `media.frames_considered` exceeding
`media.frames_analyzed` is how a caller learns coverage was partial.

**A video is charged per frame.** `guard` charges one unit per request before the body is
read, which is right for a still and wrong for a video: six frames is six times the model
time. The difference is charged after the fact in `tagverify/api/v1/analyze.py`, so a video cannot
be used as the cheap way to consume six times the capacity. A cache hit ran nothing and stays
one unit.

**Admin defaults closed.** With `ADMIN_PASSWORD` unset, `/admin` refuses rather than opening,
and every mutating handler re-checks the session itself. These pages mint API keys and change
the numbers that decide compliance verdicts; defaulting open would be the dangerous failure.

**Colour means a verdict.** Red, amber and green are reserved for present / uncertain / absent.
The interface itself is monochrome, with a single azure used only for links and focus rings, so
nothing in the chrome can be mistaken for a result. All colours and spacing come from the token
block at the top of `tagverify/static/css/app.css` — do not introduce literal values in templates.
