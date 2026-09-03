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
JSON API — and a saved tag is live immediately, because the model is handed the catalog as its
prompt on every request. The sibling `dooh-backend` proxies those writes and enforces the
verdicts; it never mirrors the catalog.

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
        result cache        calibration state         the VLM
        (Postgres,           (Postgres,               (per-request prompt,
         RAW answers)         has this been            built from the catalog)
              │               measured?)
              └─────────────────────┼──────────────────────┘
                                    ▼
                          tagverify/tags/decide.py    ← the verdict rule
```

The cache holds **raw answers, not verdicts**, and the rule is re-applied on every read — so a
change to `decide.py` reaches creatives that were analysed months ago. That is the whole reason
`decision_version` is published: a consumer caching our *decided* verdicts has to key on it, or
the retroactivity stops at the HTTP boundary.

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
| `tagverify/tags/` | The catalog: `catalog.py` validates and does CRUD over `content_tags`; `decide.py` turns a raw answer into a verdict, owns `decision_version`, and is the only place staleness is judged (`effective_calibrated`). |
| `tagverify/scoring/` | The scorer. `prompt.py` is the question and the JSON schema; `vlm.py` and `gemini.py` are the two providers, sharing everything but the SDK call; `fake.py` answers from fixtures for offline work; `registry.py` resolves `SCORER` to one of them. |
| `tagverify/analyze/video.py` | Decoding a video and choosing which frames are worth scoring. Keyframes, then a colour-aware dedupe. |
| `tagverify/analyze/aggregate.py` | The rule that collapses per-frame verdicts into one per tag. Pure — no I/O, no database — so it is tested exhaustively and cheaply. |
| `inference/` | **Data only — no code runs from here.** `eval/` holds the labelled images; `gate_cache.json` banks the SigLIP baseline the removal was judged against (146 cross-tag false blocks, 0.688 mean recall over 466 images); `phrase_packs_archive.json` holds the packs themselves, dumped the moment before migration 0003 dropped the columns. The measurement outlives the code. The detector is preserved on `main`. |
| `migrations/` | Alembic. Brings an **existing** database forward; a new one is provisioned from `docs/schema.sql`. |
| `tests/` | pytest. Runs against the ASGI app in-process; tests needing the database or the model skip cleanly when those are not configured. |

### `docs/`

| File | What it is |
|---|---|
| [`VLM_SCORING.md`](docs/VLM_SCORING.md) | **Start here for how scoring works.** The design of the reading-model path: why a ranking model was replaced, the prompt, the schema, the cost model, and §5.2's plan for an eval harness that writes calibration back. |
| [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Exhaustive `file:line` reference for every module, route, table and constant. **Stale in places** — it predates the VLM rewrite and still documents threshold routes and partials that no longer exist. It describes; it does not govern. |
| [`DEPLOY.md`](docs/DEPLOY.md) | Deployment, end to end: installing and supervising the process, the environment it needs, provisioning the database, issuing keys, and what to check afterwards. |
| [`schema.sql`](docs/schema.sql) | The schema as it should *be* — provisions a new database by hand. |
| [`TAG_PIPELINE_IN_PRODUCTION.md`](docs/TAG_PIPELINE_IN_PRODUCTION.md) | The operational counterpart to `DEPLOY.md`: what bites you after the commands have run, and why. |
| [`TAG_CRUD_IMPLEMENTATION.md`](docs/TAG_CRUD_IMPLEMENTATION.md) | The authority on what actually shipped for tag CRUD, and what implementation changed about the plan. |
| [`TAG_MANAGEMENT_API.md`](docs/TAG_MANAGEMENT_API.md) | **Superseded** design doc. Kept for its measurements and its analysis of what a tag is; its file paths predate the `dooh` → `tagverify` rename. |
| [`PORTING_THE_TAG_CATALOG.md`](docs/PORTING_THE_TAG_CATALOG.md) | How much of the editable-catalog feature carries to another project: the CRUD half ports as-is, the publish half does not. |
| [`ADMIN_CREATIVE_REVIEW_SURFACE.md`](docs/ADMIN_CREATIVE_REVIEW_SURFACE.md) | Planned, not started — a DOOH admin "this ad needs review" surface. Needs no change in this repo. |

**One virtualenv, now.** There used to be two on purpose — `.venv` served HTTP and
`inference/.venv` ran a SigLIP 2 model on a Hugging Face Space, and the rule was never to let the
ML stack cross into the web tier. A model that reads its prompt per request needs no local
weights, so that split, the Space and the publish step between them are all gone: one venv, one
`pyproject.toml`, one deploy. The heaviest thing left is `av` for video decode — **no `torch`, no
`transformers`, no `gradio`**, and it should stay that way.

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

### 4. Run

```bash
make dev              # http://127.0.0.1:8000
```

Then confirm the database and the scorer are actually reachable — without starting a second
server:

```bash
dooh health
```

It prints the same report as `GET /api/v1/health`, with `database` and `inference` called out
separately. `inference` names the scorer that is switched on and whether it is configured, so a
missing API key shows up here rather than at the first upload.

## Configuration

Read once at import by `tagverify/config.py`, from the environment and from `.env` / `.env.local`.
**Nothing raises on a missing value** — a missing `DATABASE_URL` degrades to a
`/api/v1/health` response that reports the problem, rather than a process that refuses to boot.
Monitoring you cannot reach is not monitoring.

| Variable | Required | What it does |
|---|---|---|
| `DATABASE_URL` | yes | Postgres. Use the **pooled** connection string, and keep it in the same region as the app — see the region warning in `docs/DEPLOY.md`. |
| `ADMIN_PASSWORD` | yes, in practice | Gates `/admin`. **Unset means refused, never open.** |
| `SCORER` | no | `vlm` (default) or `fake`. `fake` answers from a fixture — no key, no network, no spend — for offline work and the test suite. **Never deploy it.** `siglip` was the third value and is gone. |
| `VLM_PROVIDER` | with `SCORER=vlm` | `anthropic` (default) or `gemini`. Both share the prompt, the schema and every failure rule; only the SDK call differs. |
| `VLM_MODEL` | with `SCORER=vlm` | Defaults to `claude-opus-5`. **Set it together with the provider** — it is deliberately not defaulted per provider, because a model id that silently does not match its provider shows up as a bill rather than as an error. |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | one of them | Whichever the provider needs. Use a **paid** key: these are other companies' commercial creatives, and free tiers commonly train on submitted content. |
| `LOG_LEVEL` | no | Defaults to `INFO`. |
| `PLAYGROUND_RATE_LIMIT_PER_MIN` | no | Defaults to 20. The playground carries no API key by design, so it is limited per IP. |
| `ADMIN_LOGIN_ATTEMPTS_PER_MIN` | no | Defaults to 5. |

An unrecognised `SCORER` reports *not configured* rather than falling back to a scorer nobody
asked for — scoring with something other than what was requested is the sort of thing that gets
noticed a month later.

## Commands

The `dooh` CLI is four commands. It used to be nine, and the ordering of that list mattered
because a catalog change had to be walked from Postgres to the model by hand; nothing needs
walking now.

```bash
dooh create-key "Client name"     # issue an API key (printed once, only a hash is stored)
dooh gate                         # score the labelled eval set and compare against the baseline
dooh health                       # the /api/v1/health report, without a server
dooh prune-usage                  # drop expired rate-limit rows
```

**Five commands are gone** with the ranking model: `seed-tags`, `seed-thresholds`,
`export-packs`, `push-packs` and `apply-calibration`. Every one existed to carry a phrase pack
from Postgres to a machine that could not read Postgres, or to measure the cutoffs that turned
its scores into verdicts. A model that reads its prompt per request needs neither.

**`create-key`** takes `--rate-limit N` (default 60) and `--tags-write`, which adds the
`tags:write` scope. Issue that as a **separate key** — do not add the scope to a key already in
use for analyze. A key that scores creatives and a key that can change what scoring means are
different blast radii.


## Adding or changing a tag

Through the admin UI at **`/admin?tab=tags`** or the JSON API. A tag is three fields; the admin
form asks for a name, and takes the other two on trust:

| | |
|---|---|
| `label` | The name, and **this is the prompt**. It reaches the model as `- alcohol: Alcoholic content` and nothing else about the tag does. Also what a screen owner sees when choosing what to block. |
| `description` | **Optional.** Shown to the screen owner beneath the name; a tag without one shows just its name. **Not sent to the model**; editing it moves no fingerprint and re-scores nothing. |
| `slug` | `^[a-z][a-z0-9_]*$`, and it **cannot be renamed**. A slug in use *or retired* is refused, because DOOH stores slug strings and reusing one silently re-points existing screen rules. |

**The admin form derives the slug from the label** (`catalog.slugify` — the only implementation;
there used to be two, both in JavaScript, neither authoritative) and shows it back as you type,
from the server. There is an `Edit` beside it, and it is not decoration: most tags in this
catalog carry a slug shorter than their label — `alcohol` for "Alcoholic content",
`pharma_medicine` for "Pharmaceutical and medicine". The value is permanent and retiring a tag
does not release it, so the preview is the only moment anyone gets to notice it is wrong.

The JSON API still requires an explicit `slug`. A caller already has an identifier in mind, or
it would not be calling.

**Save is live.** No publish, no calibration, no eval images.

**The name is the only knob.** Everything about *how* to decide lives in
`scoring/prompt.py`'s system instruction — judge only what is visible, read labels and
packaging, count it present if it appears anywhere however small, answer `null` rather than
guess. The catalog supplies only *which* categories, one line each. So name a tag the way you
would to someone seeing it for the first time, and fix a misfiring one by renaming it.

**What a name cannot carry is a boundary**, and that is the accepted cost of this shape. There
is nowhere to write "packaged juice counts" or "alcohol-free 0.0% beer does not", so the model
decides those cases and its answer can move when the model version does. Where two categories
overlap — `junk_food` and `restaurant_dining`, `protein_supplements` and `pharma_medicine` —
expect both to fire on the ambiguous creative. `dooh gate` over `inference/eval/` is how you
find out; see [Testing](#testing).

**Retire, never delete — and retirement is reversible.** `retire_tag` sets
`status='retired'`; there is no hard delete. A deleted slug is stranded in DOOH's
`devices.blocked_tags`, where every upload then falls through to `NOT_VERIFIED` forever.

`restore_tag` puts a retired tag back, from the Restore button on its row in the admin tag
table. It revives the *same* row, so it is not a way to release a slug to a different tag —
the reservation above still holds. Restoring re-checks the `MAX_TAGS` active cap, which
`validate_tag` only applies on create. Both directions move `packs_version`, so every cached
verdict re-keys and each creative is analysed once more; and a calibration measured before the
round trip comes back reading superseded, which is `effective_calibrated` doing its job.

`positives`, `negatives`, `rationale` and `sigmoid_floor` were dropped from `content_tags` in
migration 0003. They were the rollback path to `main`, and `development` is not rolling back:
`SCORER` no longer accepts `siglip`. The packs themselves — 158 positives and 179 mirrored
negatives across 24 tags, with the measurements that justified them — are kept in
`inference/phrase_packs_archive.json`, beside `gate_cache.json`. The API still accepts the four
fields and ignores them, so DOOH's tag admin keeps working until it drops them too.

## Testing

```bash
make check              # ruff + the full pytest suite — run this before committing
make test-fast          # the pure decision rules and intake: no DB, no model, no network
make typecheck          # mypy, on demand
```

`make check` is **lint plus tests only** — `typecheck` is deliberately not folded into it,
because mypy is not clean yet and a red `check` that everyone learns to ignore is worse than no
target at all. Fold it in once it passes.

**There is no CI.** `.github/workflows/` is empty — nothing runs lint or tests on push, so
`make check` on your machine is the only gate there is.

The suite runs **in-process** against the ASGI app over `httpx.ASGITransport`: no server, no
port. `tests/api/` covers the JSON API, `tests/support/` holds shared helpers (an in-memory MP4
encoder, so video tests need neither fixtures on disk nor a network), and the rest sit at the top
level. Two markers in `tests/conftest.py` gate what cannot always run: `needs_db` skips when
`DATABASE_URL` is unset, and `needs_inference` when no **real** scorer is configured — the suite
pins `SCORER=fake`, which is configured by definition, so a test asserting a genuine detection
has to skip rather than assert a tautology. Both read through `Settings`, not `os.environ`, so
your `.env` counts.

That pin is an unconditional `os.environ["SCORER"] = "fake"`, not a `setdefault`. A real
environment variable outranks `.env.local`, and this is the only way to stop a developer's own
configuration deciding what the suite tests — which had already happened, turning ten tests red
on one machine and green on another.

`tests/test_aggregate.py` is the highest-value file in the repo. If you change how video frames
combine, it should fail; if it doesn't, the test is wrong. Its former counterpart
`tests/test_banding.py` was deleted along with the rule it guarded — there is no score to band
any more.

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

Keys go in `x-api-key` or `Authorization: Bearer`. The `tags:write` routes accept **either** a
scoped API key **or** a logged-in admin session with a valid CSRF token, which is what lets the
admin UI and an integrator share one implementation. A plain analyze key carries no scopes and
is refused, with an error that names the scope it lacks rather than a bare 403.

There is **no publish endpoint**. A saved tag is live at the next request, so there is nothing to
publish and nothing to reset. Write handlers still return `"pending_publish": true`, which is now
a formality kept only because `dooh-backend` parses for it — do not build anything new on it.

Un-retiring is admin-only. `POST /admin/tags/{slug}/restore` has no `/api/v1` counterpart, by
design: restoring re-checks the active-tag cap and revives a specific row, which is a decision
someone should be looking at a screen to make.

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
      "top_phrase": "a woman holding two steins of beer",
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
    "score": 0.977,           // the model's confidence in its OWN answer. Nothing enforces on it.
    "confidence": "high",     // high | medium | low, bucketed from score. Triage only.
    "decided_by": "vlm",
    "calibrated": false,      // ← never measured, or measured then reworded; either way a guess
    "evidence": {
      "top_phrase": "a woman holding two steins of beer",   // what it SAW
      "crop": null,           // null from this scorer — never 0
      "sigmoid": null
    }
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
  set to that proxy's address — never `*`, which trusts the header from anyone.
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
every upload against that screen falls through to `NOT_VERIFIED` forever. Retirement itself is
reversible — `restore_tag`, and the Restore button on a retired row — because the row and its
eval images survive it. That is what makes retiring the safe direction. Note the stranding above
is not unique to deleting: a retired tag leaves `GET /api/v1/tags` too, so DOOH cannot tell the
two apart. What a hard delete would additionally lose is the slug reservation, and with it the
guarantee that a reused name cannot inherit another tag's labelled images and measurements.

**`decision_version` fingerprints the other half — how the answer is read.** `packs_version`
covers the *question* (the model id, the prompt, the schema, and every active tag's slug and
name); `decision_version` covers the *rule that interprets the reply*. Both are needed, and the
gap was not academic: an integrator caching our verdicts keyed on the pack alone kept serving a
refusal we had already decided was wrong, and never called back to find out — quietly undoing
the retroactivity the raw-answer cache exists to provide. It is a one-way hash, so it exposes
nothing about the rule itself.

It also answers *"is my change live?"*. `decision.rule` in `/api/v1/health` is a hash of the
bytes of `tagverify/tags/decide.py` **as this process loaded them**, once, at import. If it does
not move after you edit that file, the server is stale and needs restarting — a server started
without `--reload` once served a superseded rule for hours with nothing anywhere saying so.

**Every tag currently reads `calibrated: false`, and that is honest rather than broken.**
Nothing writes `tag_thresholds` today — `apply-calibration` went with the ranking model — so no
tag has a current measurement, and an uncalibrated verdict is stated as the educated guess it
is. A measurement taken against a different `packs_version` is *stale*, not calibrated: the
prompts were reworded underneath the answer. That comparison is `decide.effective_calibrated`
and it lives in exactly one place, because it used to be written out three times and one of the
copies disagreed with the other two for every stale row. `docs/VLM_SCORING.md` §5.2 is the plan
for an eval harness that writes those columns back.

**An unknown tag is an error, never a silent skip.** If a caller misspells `alcohol` the whole
request is refused. "We didn't check" and "we checked and it's clean" mean opposite things.

**The media is never stored.** Only its sha256, which is enough for dedupe, caching and an
audit trail. Video is decoded from an in-memory buffer and never touches the disk either.

**A video is present if ANY frame is present.** Each sampled frame is decided independently, and
only then are the verdicts collapsed: present beats uncertain beats absent, and within the
winning band the highest-scoring frame supplies the evidence. **Band first, score only as the
tie-break** — ranking by raw score alone would let a confident *absent* outrank a genuine
*present* and report a creative nobody actually cleared. All six frames now travel in one
request, but that is a transport detail: the schema carries a verdict per frame per tag, the
prompt asks for per-frame independence in as many words, and the collapse lives in
`tagverify/analyze/aggregate.py` and nowhere else.

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
