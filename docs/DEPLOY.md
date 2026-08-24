# Deploying

Two independently deployed pieces:

| what | where | how |
|---|---|---|
| the whole repo | GitHub — [`kuljeet-singhh/Dooh-TagVerefication`](https://github.com/kuljeet-singhh/Dooh-TagVerefication) | `git push origin main` |
| `inference/` | Hugging Face Space (`sdk: gradio`) | `git subtree push` to the Space's remote |
| `dooh/` (the web app) | any host that runs a container | `docker build` + `docker run` |
| database | Neon Postgres | already provisioned |

---

## ⚠️ Rotate the database credential first

The Neon connection string in `.env.local` was shared in a chat transcript and has not been
rotated. Reset it (Neon dashboard → Roles → reset password) and update `DATABASE_URL`
everywhere before this handles anything real.

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
re-run the schema plus `dooh seed-thresholds`. Cheap to do now, while the only data is 20
threshold rows.

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
it is ready when you see `[detector] ready: 20 tags`.

Make the Space **private**. The app authenticates with an HF token, so nothing needs to be
publicly reachable.

Note that `banding.py` now ships to the Space alongside `app.py` and `detector.py` — it is the
verdict rule, shared with the API tier. It imports nothing, so it adds no install cost.

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

## 2. The web app

Vercel is gone: it cannot host a long-lived Python process, and the connection pool and
in-process caches are exactly what make the rewrite fast. Any container host works — Fly.io,
Railway, Render, Cloud Run, or a plain VM.

```bash
docker build -t dooh-tag-verification .
docker run -p 8000:8000 --env-file .env dooh-tag-verification
```

Or without Docker:

```bash
pip install .
uvicorn dooh.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '<proxy ip>'
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

All four are required. `ADMIN_PASSWORD` is easy to forget because nothing in the public API
needs it, but `/admin` refuses every request when it is unset — the admin UI arrives dead
rather than open, which is the right failure but a confusing one if you were not expecting it.

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
dooh seed-thresholds                      # load the 20 tag thresholds from packs.json
```

`seed-thresholds` is safe to re-run: rows marked `calibrated` are never overwritten, so
re-seeding cannot clobber measured thresholds with the guesses in `packs.json`.

**The existing database needs no migration.** The Python models map onto the same tables the
previous implementation created, and API keys are still `sha256(plaintext)`, so every key
already issued keeps working.

## 4. Issue a key

```bash
dooh create-key "Client name" --rate-limit 60
```

The plaintext is printed **once** — only a sha256 hash is stored. If it is lost, revoke and
reissue.

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

If you touched `inference/`, also run the golden ML cases:

```bash
make inference-smoke
```

## Local development

```bash
# terminal 1 — inference
cd inference && ./.venv/bin/python app.py     # :7860

# terminal 2 — the app
make dev                                       # :8000
```

With `HF_SPACE` empty in `.env`, the app talks to `INFERENCE_URL`
(`http://127.0.0.1:7860`), so you can develop without deploying the Space at all.

There is no asset build step. The CSS is hand-authored and htmx is vendored, so editing a
template or a stylesheet just needs a browser reload.
