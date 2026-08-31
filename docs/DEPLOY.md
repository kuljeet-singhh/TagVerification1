# Deploying

Three independently deployed pieces, plus the repo they come from:

| what | where | how |
|---|---|---|
| the whole repo | GitHub — [`kuljeet-singhh/Dooh-TagVerefication`](https://github.com/kuljeet-singhh/Dooh-TagVerefication) | `git push origin main` |
| `inference/` | Hugging Face Space (`sdk: gradio`) | `git subtree push` to the Space's remote |
| `tagverify/` (the web app) | a container host that **stays running** — see §2 | `docker build` + `docker run` |
| database | Neon Postgres | already provisioned |

**This file gives you the commands.** Its operational counterpart,
[`TAG_PIPELINE_IN_PRODUCTION.md`](./TAG_PIPELINE_IN_PRODUCTION.md), tells you what bites you once
they have run — read it before a real deployment, not after. The two things in it that change how
you deploy at all are §5 (a sleeping Space silently reverts the catalog and stops enforcing) and
§8 (why serverless is structurally excluded). Both are summarised in place below.

---

## ⚠️ Rotate two credentials first

Both were exposed the same way — shared in a chat transcript — and neither has been rotated.

1. **The Neon connection string** in `.env.local`. Reset it (Neon dashboard → Roles → reset
   password) and update `DATABASE_URL` everywhere.
2. **`RELOAD_SECRET`**, same file. Rotate it on **both sides at once** — this app and the Space —
   or publishing stops working until they agree again.

Do both before this handles anything real.

---

## Region still matters, but far less than it used to

The previous deployment used Neon's HTTP driver, where every query is a separate HTTPS
request, so latency was `number of queries x round-trip time`. Measured against `us-east-2`
from South Asia that was 562–2435 ms **per query**, and `/analyze` uncached took ~1800 ms.

The Python app uses a pooled connection over the normal Postgres wire protocol, so a query is
no longer a network event and the multiplier is gone. Cross-region still costs you one RTT on
connection setup and adds latency to each round trip, so **keep the app in the same region as
Neon** — but a mismatch is now a nuisance rather than the dominant cost.

If the screens, the team and the traffic are in India, a Mumbai or Singapore Neon region will
still beat Ohio for everyone. Regions cannot be changed in place: create a new project and
re-run the schema plus `dooh seed-thresholds` and `dooh seed-tags`. Cheap to do now, while the
only data is 23 threshold rows and the catalog they describe.

`dooh-backend` calls this service **synchronously, inside a 7-second upload deadline**, so it
wants to be close too. App, database and backend in one region.

---

## 1. The inference Space

Unchanged by the Python rewrite — this half was always Python.

```bash
huggingface-cli login    # a WRITE token, to push; the app itself only ever reads
huggingface-cli repo create dooh-tag-check --type space --space_sdk gradio --private
```

**Do not `git init` inside `inference/`.** It is tracked by the parent repo, and a nested
`.git` makes the parent record it as a gitlink, so the directory would arrive on GitHub
empty. `git subtree` does the same job from the repo root:

```bash
git remote add space https://huggingface.co/spaces/<you>/dooh-tag-check
git subtree push --prefix=inference space main
```

`inference/README.md`'s front-matter (`sdk: gradio`, `app_file: app.py`) is what tells HF to
install `requirements.txt` and run `app.py`, and `--prefix` puts it at the Space root where
HF looks for it. **Do not delete that front-matter** — without it the Space will not build.

First build takes a few minutes and downloads ~400MB of SigLIP 2 weights. Watch the Logs tab;
it is ready when you see `[detector] ready: 23 tags`.

Make the Space **private**. The app authenticates with an HF token, so nothing needs to be
publicly reachable.

Note that `banding.py` now ships to the Space alongside `app.py` and `detector.py` — it is the
verdict rule, shared with the API tier. It imports nothing, so it adds no install cost.

### Set `RELOAD_SECRET` on the Space

Space → Settings → *Variables and secrets* → `RELOAD_SECRET`, the **same value** you give the web
app. Without it the Space refuses every publish with `reload_packs is disabled: RELOAD_SECRET is
not set on this Space`, and the catalog can only change by pushing the Space again.

It defaults closed for a reason: `gr.api` endpoints carry no authorization of their own, and the
Space is shielded only by HF privacy plus a read-scoped token that is already deployed to the web
tier and to CI. `reload_packs` mutates what the model scores against, so it needs a secret of its
own. It is read from the environment at `inference/app.py:198` and nowhere else.

### Keeping it awake

Free Spaces sleep after **48 hours** of inactivity, and a cold start re-downloads the weights
(30–60s). `.github/workflows/keepalive.yml` pings it every 6 hours, but is inert until you set
two things on the GitHub repo (Settings → Secrets and variables → Actions):

| kind | name | value |
|---|---|---|
| secret | `HF_TOKEN` | the read token |
| variable | `HF_SPACE_HOST` | `<you>-dooh-tag-check.hf.space` |

The host is the Space's *subdomain* form — owner and name joined by a hyphen, not the
`<you>/dooh-tag-check` slug that `HF_SPACE` takes. Trigger it once by hand (Actions →
keepalive → Run workflow) to confirm both are right.

If the cron silently stops, the first request after 48h eats the cold start.
`/api/v1/analyze` returns `503 INFERENCE_WARMING` with `retry_after` rather than hanging, and
the playground polls itself back to life — so it degrades honestly. But it does degrade.

### 🔴 A Space that sleeps also reverts the catalog

**This is the most dangerous property of deploying the model to a free Space, and it is not a
cold-start nuisance.** When a slept Space wakes it **rebuilds from its own git repo**, so
`packs.json` reverts to whatever was committed at the last `git subtree push`. Every tag created
through the admin UI since then is gone from the model.

What that does, end to end: the slug is missing from the catalog, so `dooh-backend` produces no
verdict for it, `evaluatePolicy` sees fewer verdicts than blocked tags and falls through to
`NOT_VERIFIED` — which is a **flag, not a block**. The upload proceeds. Screens quietly stop
enforcing the categories their owners chose, and nothing surfaces it: no error, no toast, no log
anyone is reading. For a compliance tool that is the worst available failure mode.

**Nothing re-publishes automatically.** `lifespan` in `tagverify/main.py` initialises the database
engine and starts the usage pruner; it does not check the model.

Two ways out, in order of preference:

- **Stop using an ephemeral host for the model** — §2 below and
  [`TAG_PIPELINE_IN_PRODUCTION.md`](./TAG_PIPELINE_IN_PRODUCTION.md) §8. With a persistent disk,
  the file publish writes is the file the model boots from, and this cannot happen.
- **Re-publish after any model restart** — `dooh export-packs --push`. Until auto-resync is wired
  in, treat "the Space restarted" and "the catalog needs republishing" as the same event.

`/api/v1/health` reports `inference.tags` and `inference.packs_version`, so a monitor can catch
it: if the tag count drops or `packs_version` moves without anyone publishing, the model reverted.

## 2. The web app

Vercel is gone: it cannot host a long-lived Python process, and the connection pool and
in-process caches are exactly what make the rewrite fast.

**Not *any* container host, though — it has to be one that stays running.** Serverless is
structurally excluded, not merely slow: `reload_packs` swaps an **in-memory** prompt table, and
that *is* the publish mechanism. Anything that scales to zero or runs per-request throws it away —
every cold start re-encodes, and a publish reaches only whichever instance happened to answer. So
no Lambda, no Cloud Functions, no Workers, and no Cloud Run with scale-to-zero.

What works: a plain VM, Fly.io, Render, or Railway — anything that keeps a container alive and can
attach a persistent disk. GPU platforms are unnecessary; `detector.py` has no device handling and
this is CPU-only.

The recommended shape is **one box, two containers, `docker-compose`**, in the same region as Neon
and `dooh-backend`. 2 vCPU / 4GB is comfortable. That also fixes the catalog-reversion problem
above, because the model boots from a disk the publish wrote. Sizing, what else it fixes and the
full argument are in [`TAG_PIPELINE_IN_PRODUCTION.md`](./TAG_PIPELINE_IN_PRODUCTION.md) §8.

```bash
docker build -t dooh-tag-verification .
docker run -p 8000:8000 --env-file .env dooh-tag-verification
```

Or without Docker:

```bash
pip install .
uvicorn tagverify.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '<proxy ip>'
```

**`--proxy-headers` is not optional behind a TLS terminator.** The admin session cookie's
`Secure` flag is taken from the request scheme; without it, uvicorn sees the proxy's plain-HTTP
hop and issues the cookie without `Secure`.

### Environment

| variable | value |
|---|---|
| `DATABASE_URL` | the Neon **pooled** connection string (the host containing `-pooler`) |
| `HF_SPACE` | `<you>/dooh-tag-check` |
| `HF_TOKEN` | an HF token with **read** access only |
| `ADMIN_PASSWORD` | a real password — not the one in your local `.env` |
| `RELOAD_SECRET` | the **same value** set on the Space (§1) — a real secret, not the one in your local `.env` |

All five are required. `ADMIN_PASSWORD` is easy to forget because nothing in the public API
needs it, but `/admin` refuses every request when it is unset — the admin UI arrives dead
rather than open, which is the right failure but a confusing one if you were not expecting it.

`RELOAD_SECRET` is the same kind of trap one step further along: everything works until someone
presses Publish, which then refuses with `RELOAD_SECRET is not set — refusing to publish`. Note
the two failure messages name their side, so you never have to guess which half is unset — that
one is this app; `reload_packs is disabled: …on this Space` is the model.

Leave `INFERENCE_URL` unset in production; it is the local-dev escape hatch, and `HF_SPACE`
takes precedence anyway.

Optional: `PLAYGROUND_RATE_LIMIT_PER_MIN` (default 20) and `ADMIN_LOGIN_ATTEMPTS_PER_MIN`
(default 5). The playground carries no API key by design, so it is limited per IP — if the
deployment is public, that limit is what stops it being free inference for anyone who finds
the URL.

### Scaling

The rate limiter and the threshold/health caches are per-process, so with N workers each gets
its own. That is fine — the rate limiter's authority is the `usage_counters` table, which is
shared, and the caches are 30s/60s TTLs over data that changes twice a week. Run one worker
per core with `--workers N`.

## 3. Database

For a fresh database:

```bash
psql "$DATABASE_URL" -f docs/schema.sql   # tables and indexes
dooh seed-thresholds                      # the 23 tag thresholds, from packs.json
dooh seed-tags                            # content_tags + pack_header, from packs.json
```

For an **existing** database, bring it forward first:

```bash
make migrate            # alembic upgrade head
make migrate-sql        # the same, printed as SQL instead of applied — a dry run
```

`schema.sql` and the migrations are not alternatives: the first describes the destination and
provisions a new database, the second describes how a database that already holds data gets there.
**A new column belongs in both**, or provisioning and migrating diverge. Alembic takes
`DATABASE_URL` from the app's own settings (`migrations/env.py`), so the credential never lands in
a tracked file.

Both seed commands are safe to re-run: `seed-thresholds` never overwrites a row marked
`calibrated`, so re-seeding cannot clobber measured thresholds with the guesses in `packs.json`,
and `seed-tags` never overwrites an existing tag.

> **Run the seeding commands from a checkout, not from the container.** `seed-thresholds`,
> `seed-tags` and `apply-calibration` read `inference/packs.json` and
> `inference/calibration.json`, and the image deliberately carries only three files out of
> `inference/` — those two data files are not among them. The commands resolve their paths from
> the installed `inference` package, so they work from any working directory in a checkout and
> fail with a `FileNotFoundError` naming the missing path inside the image. `create-key`,
> `prune-usage` and `health` touch only the database and run fine either way.
>
> `export-packs` and `POST /api/v1/tags/publish` render **from the database** and need no file, so
> those do work in the image. That is why `pack_header` exists — see
> [`TAG_PIPELINE_IN_PRODUCTION.md`](./TAG_PIPELINE_IN_PRODUCTION.md) §4 for the bug it fixed. If
> publishing fails there, the answer is `dooh seed-tags`, not adding `packs.json` to the
> `Dockerfile`.

### Migrations

There are two, and an existing database needs both:

| revision | what it adds |
|---|---|
| `0001_api_key_scopes` | `api_keys.scopes` (JSONB, default `[]`) — the `tags:write` scope lives here. It is a migration rather than a schema rewrite because `api_keys` holds live credentials and cannot be recreated. |
| `0002_pack_header` | `pack_header` — a singleton row holding the non-tag half of `packs.json`. No backfill; `dooh seed-tags` writes the row. |

What the previous version of this file said — *the existing database needs no migration* — was
true when the Python rewrite landed and is no longer. What still holds from it: the Python models
map onto the same tables the previous implementation created, and API keys are still
`sha256(plaintext)`, so **every key already issued keeps working**. Existing keys simply get an
empty scope list, which is analyze-only, which is what they already were.

## 4. Issue a key

```bash
dooh create-key "Client name" --rate-limit 60
```

The plaintext is printed **once** — only a sha256 hash is stored. If it is lost, revoke and
reissue.

`dooh-backend` needs a **second, separate key** to proxy catalog writes:

```bash
dooh create-key "DOOH admin (tags)" --tags-write
```

Do not add the scope to a key already in use for analyze. A key that scores creatives and a key
that can change what scoring *means* are different blast radii, and only one of them is handed to
a busy integration. Without `--tags-write` a key is analyze-only and every `/api/v1/tags` write is
refused with an error naming the scope it lacks.

## 5. Publish the catalog

**A deployment is not finished until the catalog is published.** The model boots from whatever
`packs.json` was committed at the last `git subtree push`; every tag created through the admin UI
since then exists only as a database row until someone publishes.

```bash
dooh export-packs --push
```

Or `POST /api/v1/tags/publish` with a `tags:write` key, which does exactly the same thing —
`tagverify/tags/publish.py` is the single implementation both call, because `packs_version` hashes
the rendered bytes and two renderers would mean two fingerprints for one catalog.

Publishing is **whole-catalog**: it takes everyone's pending edits live, and it moves
`packs_version`, so every tag reverts to `calibrated: false` until the catalog is recalibrated.
There is no per-tag publish and there is no publish button in the admin UI — the tags panel shows
a banner naming this command.

> **⚠️ Unverified — check this on your first container deploy.** `publish_catalog` writes
> `packs.json` to `/app/inference/` before pushing it. The `Dockerfile` copies that directory as
> root and then drops to `USER dooh` (uid 10001) with no `chown`, so on the ordinary reading of
> POSIX permissions the first publish inside the image should fail with a `PermissionError` on
> that write — the push never happens, because the write comes first and deliberately so (a push
> against a stale file would be undone by the next restart). This was **not** reproduced: the
> Docker daemon was not running when it was written down, and it may well be moot on a host that
> overrides `USER` or mounts a writable volume there. Confirm it before trusting a container
> publish:
>
> ```bash
> docker run --rm --entrypoint sh dooh-tag-verification \
>   -c 'touch /app/inference/.probe && echo writable || echo NOT-writable'
> ```
>
> If it prints `NOT-writable`, the fix is a `chown` in the `Dockerfile` (`COPY --chown=dooh:dooh`
> on the `inference` layer, or `RUN chown dooh:dooh /app/inference`) — **not** baking `packs.json`
> into the image, which reintroduces the bug
> [`TAG_PIPELINE_IN_PRODUCTION.md`](./TAG_PIPELINE_IN_PRODUCTION.md) §4 records fixing. Until it
> is confirmed, publish with `dooh export-packs --push` from a checkout, which is unaffected.

---

## Verifying a deployment

```bash
# unauthenticated, safe to hit from anywhere
curl https://<your-app>/api/v1/health

# the full suite against a real database and a real Space
pytest
```

`/api/v1/health` reports `database` and `inference` separately, distinguishes "Space is
warming" from "Space is broken", and always returns 200 with the status in the body. A
`degraded` status with `inference.warming: true` right after deploy is normal.

Check three fields in it, not just the status:

| field | what it tells you |
|---|---|
| `inference.tags` | how many tags the **model** is actually serving. If this is below the active row count in `content_tags`, the catalog is not published — or the Space restarted and reverted it. |
| `inference.packs_version` | which pack is live. It moving on its own means the model reverted. |
| `decision.rule` | the banding module this process loaded, at import. Compare with `shasum -a 256 inference/banding.py`; if they differ the server is stale and needs restarting. |

Then confirm publishing works end to end, before you need it in anger — a `RELOAD_SECRET`
mismatch is invisible until the first publish:

```bash
dooh export-packs --check    # renders from the DB, writes nothing, exits 1 if it would change
```

If you touched `inference/`, also run the golden ML cases:

```bash
make inference-smoke
```

## Local development

First-time setup — installing, provisioning and seeding — is in the
[README](../README.md#setup). Once that is done:

```bash
# terminal 1 — inference
cd inference && RELOAD_SECRET=dev-secret ./.venv/bin/python app.py     # :7860

# terminal 2 — the app
make dev                                                               # :8000
```

With `HF_SPACE` empty in `.env`, the app talks to `INFERENCE_URL`
(`http://127.0.0.1:7860`), so you can develop without deploying the Space at all.

The local `app.py` reads `RELOAD_SECRET` from its own environment, not from `.env` — it is a
separate process in a separate virtualenv. Set it inline as above and match it in `.env` if you
want to exercise publishing locally; leave both unset and everything except publish still works.

There is no asset build step. The CSS is hand-authored and htmx is vendored, so editing a
template or a stylesheet just needs a browser reload.
