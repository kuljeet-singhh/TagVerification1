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

The second path decides *what the questions can be* — the catalog the model is asked about:

```
   admin UI    ─┐
                ├──►   content_tags   ──►  read per request, as the prompt
   DOOH admin  ─┘      (Postgres)
   (tags:write)
```

**A saved tag is live.** There is no publish step, no pack file and no fingerprint to push,
because the model reads its prompt on every request — so the database is the only source of
truth and nothing can be stale relative to it. A tag written a second ago is in the next call.

That was not always true. Until 2026-09-01 a SigLIP 2 detector ranked each image against a pool
of hand-written phrases encoded into embeddings at startup, on a machine with no database
access, so every tag needed 5+ positives, 6+ hard negatives, a calibrated threshold and a
`dooh export-packs --push` before it scored anything. All of it existed to carry that pool
across a tier boundary. See `docs/VLM_SCORING.md`.

## Repository layout

| Path | What it is |
|---|---|
| `tagverify/` | The web application: FastAPI, Jinja2 templates, HTMX. One process serves the API, the playground, the docs and the admin. |
| `tagverify/tags/` | The catalog: `catalog.py` validates and does CRUD over `content_tags`, `packs.py` is pure read/write of the pack file, `publish.py` renders the catalog and pushes it to the model, `decide.py` holds the thresholds side of `decision_version`. |
| `tagverify/scoring/` | The scorer. `prompt.py` is the question and the JSON schema; `vlm.py` and `gemini.py` are the two providers, sharing everything but the SDK call; `fake.py` answers from fixtures for offline work; `registry.py` resolves `SCORER` to one of them. |
| `tagverify/analyze/video.py` | Decoding a video and choosing which frames are worth scoring. Keyframes, then a colour-aware dedupe. |
| `tagverify/analyze/aggregate.py` | The rule that collapses per-frame verdicts into one per tag. Pure, like `banding.py`, and tested the same way. |
| `inference/` | **Data only.** The labelled eval images and the measurements taken over them, including `gate_cache.json` — the SigLIP baseline the removal was judged against. The detector itself is preserved on `main`. |
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
| `ADMIN_PASSWORD` | yes, in practice | Gates `/admin`. **Unset means refused, never open.** |
| `LOG_LEVEL` | no | Defaults to `INFO`. |
| `PLAYGROUND_RATE_LIMIT_PER_MIN` | no | Defaults to 20. The playground carries no API key by design, so it is limited per IP. |
| `ADMIN_LOGIN_ATTEMPTS_PER_MIN` | no | Defaults to 5. |

## Commands

The `dooh` CLI, in roughly the order a catalog change moves through it:

```bash
dooh create-key "Client name"     # issue an API key (printed once, only a hash is stored)
dooh gate                         # SigLIP vs VLM over the labelled eval set
dooh health                       # the /api/v1/health report, without a server
dooh prune-usage                  # drop expired rate-limit rows
```



**`create-key`** takes `--rate-limit N` (default 60) and `--tags-write`, which adds the
`tags:write` scope. Issue that as a **separate key** — do not add the scope to a key already in
use for analyze. A key that scores creatives and a key that can change what scoring means are
different blast radii.


## Adding or changing a tag

Through the admin UI at **`/admin?tab=tags`** or the JSON API. A tag is three fields:

| | |
|---|---|
| `slug` | `^[a-z][a-z0-9_]*$`, and it **cannot be renamed**. A slug in use *or retired* is refused, because DOOH stores slug strings and reusing one silently re-points existing screen rules. |
| `label` | What a screen owner sees. |
| `description` | **This is the prompt.** It is sent to the model verbatim and is the whole of what is asked. |

**Save is live.** No publish, no calibration, no eval images.

**Write the description as a specification, not a blurb.** It is the only knob — there is no
threshold to tune afterwards — so name the thing, name its boundary, and say what does *not*
count. The exclusion clause is the part that earns its keep: it is what stops an alcohol-free
beer, or a wine-themed logo, being blocked as alcohol. It is the direct successor to the hard
negative phrases, and those were where the measured accuracy came from.

**Retire, never delete.** `retire_tag` sets `status='retired'`; there is no hard delete. A
deleted slug is stranded in DOOH's `devices.blocked_tags`, where every upload then falls
through to `NOT_VERIFIED` forever.

`positives` and `negatives` are still accepted and still stored, unused. They are the rollback
path to `main`, where the ranking model still needs them.

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
`DATABASE_URL` is unset and `needs_inference` when no REAL scorer is configured — the
suite pins `SCORER=fake`, which is configured by definition, so a test asserting a genuine
detection has to skip rather than assert a tautology.
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
