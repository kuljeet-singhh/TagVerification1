# Deploying

Everything ships by `git push`. No Docker, no image builds, no registry.

| what | where | how |
|---|---|---|
| the whole repo | GitHub — [`kuljeet-singhh/Dooh-TagVerefication`](https://github.com/kuljeet-singhh/Dooh-TagVerefication) | `git push origin main` |
| `inference/` | Hugging Face Space (`sdk: gradio`) | `git subtree push` to the Space's remote |
| everything else | Vercel | connect the GitHub repo above |
| database | Neon | already provisioned |

---

## ⚠️ Read this first: pick the right region

**This is the single biggest performance decision in the project, and it is
painful to change later.**

The Neon HTTP driver sends every query as a separate HTTPS request, so request
latency is `number of queries x round-trip time`. Measured against the current
`us-east-2` (Ohio) database from South Asia:

```
one round trip:      562-2435 ms   (median ~790 ms)
/analyze uncached:      ~1800 ms
/analyze cached:         ~281 ms
```

Almost all of that is geography, not code. The same queries against a database
in the same region as the compute run in **single-digit milliseconds**.

So:

1. **Vercel must be deployed in the same region as Neon.** Set it in Project
   Settings → Functions → Region. If Neon is `us-east-2`, pick `Cleveland,
   USA (us-east-2)`. Mismatch here silently costs ~700ms per query.
2. **Consider moving Neon closer to your users.** If the screens, the team, and
   the traffic are in India, a Mumbai or Singapore region will beat Ohio for
   everyone — including local development. Neon regions cannot be changed in
   place; you create a new project and re-run `npm run db:push && npm run seed`.
   Do that now if you are going to do it at all, while the only data is 20
   threshold rows.

The code already minimises round trips regardless of region (auth + rate limit
are one CTE, thresholds are memoised, independent reads run in parallel), but no
amount of that beats not crossing an ocean.

---

## 1. GitHub

Everything lives in one repo, `inference/` included. Vercel deploys from it and
the Space is fed out of it.

```bash
git remote add origin https://github.com/kuljeet-singhh/Dooh-TagVerefication.git
git push -u origin main
```

`.gitignore` excludes `.env*`, so no credential in `.env.local` is ever pushed.
`inference/.gitignore` excludes `.venv/`, `__pycache__/`, `eval/`, and
`calibration.json` — the last is a local artifact that `calibrate.py` writes and
nothing on the Space reads.

## 2. The inference Space

```bash
# once
huggingface-cli login   # a WRITE token, to push; the app itself only ever reads
huggingface-cli repo create dooh-tag-check --type space --space_sdk gradio --private
```

Then push `inference/` up as the Space's root. **Do not `git init` inside
`inference/`** — it is tracked by the repo above, and a nested `.git` makes the
parent record it as a gitlink, so the directory would arrive on GitHub empty.
`git subtree` does the same job from the repo root:

```bash
git remote add space https://huggingface.co/spaces/<you>/dooh-tag-check
git subtree push --prefix=inference space main
```

`inference/README.md`'s front-matter (`sdk: gradio`, `app_file: app.py`) is what
tells HF to install `requirements.txt` and run `app.py`, and `--prefix` puts it
at the Space root where HF looks for it. **Do not delete that front-matter** —
without it the Space will not build.

First build takes a few minutes and downloads ~400MB of SigLIP 2 weights. Watch
the Space's Logs tab. It is ready when you see `[detector] ready: 20 tags`.

Make the Space **private**. Next.js authenticates with an HF token, so nothing
needs to be publicly reachable.

### Keeping it awake

Free Spaces sleep after **48 hours** of inactivity, and a cold start re-downloads
the weights (~30-60s) — long enough to blow Vercel's 10s function limit. A cron
every 6 hours keeps it warm with a wide safety margin.

`.github/workflows/keepalive.yml` already does this, but it is inert until you
set two things on the GitHub repo (Settings → Secrets and variables → Actions):

| kind | name | value |
|---|---|---|
| secret | `HF_TOKEN` | the read token |
| variable | `HF_SPACE_HOST` | `<you>-dooh-tag-check.hf.space` |

Note the host is the Space's *subdomain* form — owner and name joined by a
hyphen, not the `<you>/dooh-tag-check` slug that `HF_SPACE` takes. Trigger it
once by hand (Actions → keepalive → Run workflow) to confirm both are right.

If this cron ever silently stops, the first request after 48h eats the cold
start. `/api/v1/analyze` returns `503 INFERENCE_WARMING` with `retry_after`
rather than hanging, so it degrades honestly — but it does degrade.

## 3. Vercel

Import the GitHub repo, then set environment variables:

| variable | value |
|---|---|
| `DATABASE_URL` | the Neon **pooled** connection string |
| `HF_SPACE` | `<you>/dooh-tag-check` |
| `HF_TOKEN` | an HF token with **read** access only |
| `ADMIN_PASSWORD` | a real password — **not** the `dev-admin-password` in `.env.local` |

All four are required. `ADMIN_PASSWORD` is easy to forget because nothing in the
public API needs it, but `lib/auth/admin.ts` refuses every `/admin` request when
it is unset — the admin UI arrives dead rather than open, which is the right
failure but a confusing one if you were not expecting it.

Leave `INFERENCE_URL` unset in production — it is the local-dev escape hatch, and
`HF_SPACE` takes precedence anyway.

Set the function region to match Neon (see the warning above).

## 4. Database

Already done for the current database, but for a fresh one:

```bash
npm run db:push    # create tables
npm run seed       # load the 20 tag thresholds from inference/packs.json
```

`npm run seed` is safe to re-run: rows marked `calibrated` are never overwritten,
so re-seeding cannot clobber measured thresholds with the guesses in `packs.json`.

## 5. Issue a key

```bash
npm run create-key -- "Client name" 60
```

The plaintext is printed **once** — only a sha256 hash is stored. If it is lost,
revoke and reissue.

---

## Verifying a deployment

```bash
# unauthenticated, safe to hit from anywhere
curl https://<your-app>/api/v1/health

# full end-to-end suite (42 checks) against the deployed app
BASE_URL=https://<your-app> npm run smoke:api -- <api-key>
```

`/api/v1/health` reports `database` and `inference` separately, and distinguishes
"Space is warming" from "Space is broken". A `degraded` status with
`inference.warming: true` right after deploy is normal.

## Local development

```bash
# terminal 1 — inference
cd inference && ./.venv/bin/python app.py      # :7860

# terminal 2 — app
npm run dev                                     # :3000
```

With `HF_SPACE` empty in `.env.local`, the app talks to `INFERENCE_URL`
(`http://127.0.0.1:7860`), so you can develop without deploying the Space at all.

## Rotating credentials

The Neon connection string currently in `.env.local` was shared in a chat
transcript. Rotate it in the Neon dashboard (Roles → reset password) before this
handles anything real, and update `DATABASE_URL` in both `.env.local` and Vercel.
