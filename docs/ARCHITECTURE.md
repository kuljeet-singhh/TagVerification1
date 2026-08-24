# Architecture — DOOH Tag Verification

An exhaustive reference for this service: every module, route, table, constant and code path,
with `file:line` pointers.

This document **describes**; it does not govern. `AGENTS.md` remains the authority on the
invariants and `README.md` on getting started — where they already say something well, this file
points at them rather than restating it. Where a number appears here it was read out of the
source, and the source wins if they ever disagree.

**Verified against:** `packs_version` `c14ac5bdcfa9`, `RULE_VERSION` `dcf11c44481b`,
`SAMPLER_VERSION` `014a0062d35e`, 254 collected tests.

---

## Contents

1. [What this service is](#1-what-this-service-is)
2. [System context](#2-system-context)
3. [Repository map](#3-repository-map)
4. [Request lifecycle](#4-request-lifecycle)
5. [Web tier — `tagverify/`](#5-web-tier--tagverify)
6. [Analysis pipeline — `tagverify/analyze/`](#6-analysis-pipeline--tagverifyanalyze)
7. [Model tier — `inference/`](#7-model-tier--inference)
8. [The verdict rule in full](#8-the-verdict-rule-in-full)
9. [Fingerprints](#9-fingerprints)
10. [Caching and retroactivity](#10-caching-and-retroactivity)
11. [Invariants](#11-invariants)
12. [Testing](#12-testing)
13. [Build, run, deploy](#13-build-run-deploy)
14. [Configuration reference](#14-configuration-reference)
15. [Constants reference](#15-constants-reference)
16. [Known drift](#16-known-drift)

---

## 1. What this service is

In digital out-of-home ad ops, a creative arrives tagged `alcohol`, `gambling`, `vaping` and so
on. Those tags drive placement rules — you cannot put alcohol on a screen outside a school. This
service checks the tags against the pixels: for stills, and for video, where it samples the
scenes and reports at which second the content appears.

It is a compliance tool, and that shapes every design decision in it.

### The three-valued verdict

`present` is `true`, `false`, or **`null`**. `null` means *uncertain — a human has to look*. It
is not "probably absent", not "we couldn't check", and above all not `false`. The API, the UI,
the aggregation rule and the type signatures all go out of their way to keep the two apart.

| `present` | `band` | Meaning |
|---|---|---|
| `true` | `present` | The content is in the creative. Block it. |
| `null` | `uncertain` | The score fell between the thresholds. Escalate to a human. |
| `false` | `absent` | The content was not found. |

The failure mode this product exists to prevent is **"we didn't check" presented as "we checked
and it's clean"**. That sentence, or a variant of it, appears in the source at
`tagverify/analyze/run.py:186-191`, `tagverify/analyze/video.py:299-302`, `tagverify/analyze/intake.py:14-23`
and `tagverify/analyze/aggregate.py:15-31`. Every partial-result path is an error instead.

Two consequences that surprise people:

- **An unknown tag fails the whole request.** If a caller misspells `alcohol`, nothing is
  analysed and nothing is returned. Skipping the tag and returning the others would report
  "clean" for a check that never ran (`tagverify/analyze/run.py:110-115`).
- **A frame that fails fails the whole video.** There is no partial video result
  (`tagverify/analyze/run.py:197-201`).

A second commitment sits alongside the first: **`calibrated: false` must stay visible**. An
uncalibrated verdict is a guess derived from hand-written provisional thresholds. Presenting a
guess as a measurement is the same class of error as flattening `null` to `false`, so the flag
is carried out through the API (`tagverify/tags/decide.py:102-126`), the tag catalog
(`tagverify/api/v1/tags.py:58-65`) and the admin UI.

---

## 2. System context

### Position in the DOOH platform

Three sibling repos live under `Dooh/`:

| Repo | What it is |
|---|---|
| `dooh-frontend` | The marketplace / booking UI. |
| `dooh-backend` | NestJS API. Owns bookings, screens, creatives. |
| `profile` | **This repo.** Tag verification. |

`dooh-backend` is the production consumer. Its `src/content-verification/` module (~2,900 LOC)
holds the client, a circuit breaker, and the policy layer that turns a verdict into a decision
about a creative:

| File | Lines | Role |
|---|---|---|
| `client.ts` | 361 | HTTP client for this service. Requires `PROFILE_API_URL` and `PROFILE_API_KEY`. |
| `policy.ts` | 251 | Turns verdicts into block / allow / review. |
| `creative-verification.service.ts` | 421 | The creative-upload flow. |
| `content-verification.service.ts` | 282 | Catalog caching, availability. |
| `circuit-breaker.ts` | 117 | Stops hammering a service that is down. |
| `errors.ts` | 216 | Typed errors, including which are this service's fault and which are not. |
| `types.ts` | 127 | The response contract, mirrored. |

Two details of that integration constrain this service:

- The backend caches the tag catalog for an hour, so during that window this service may already
  be applying newer thresholds than the catalog the backend last read. That is why every verdict
  carries its own `calibrated` flag and `decision_version` rather than relying on the catalog.
- `PROFILE_API_KEY` is a `dooh_live_…` key sent as `x-api-key`, **server-to-server only** — it is
  never exposed to a browser (`dooh-backend/src/content-verification/types.ts:6`).
- With `PROFILE_API_URL`/`PROFILE_API_KEY` unset the backend does not fail open: it disables
  verification and flags every creative for review
  (`dooh-backend/src/content-verification/content-verification.service.ts:76`).

### Two tiers, two virtualenvs, on purpose

| | `tagverify/` (web app) | `inference/` (model) |
|---|---|---|
| Job | Serve HTTP: API, playground, docs, admin | Score one still against prompts |
| venv | `.venv` at the repo root | `inference/.venv` |
| Deps | `pyproject.toml` | `inference/requirements.txt` |
| Deployed to | Docker / any container host | a Hugging Face Space (`sdk: gradio`) |
| Heavy deps | none (~18MB `av` is the largest) | torch, transformers, gradio (~2GB) |

The split exists so that serving HTTP does not require 2GB of ML wheels. **Never add `torch`,
`transformers` or `gradio` to `pyproject.toml`, and never import `inference.detector` from
`tagverify/`.** `av` is not an exception to that rule: it is a codec binding for reading video
(~18MB, ffmpeg bundled in its wheels, no system package), it is not ML, and it does not cross
the tiers — frame sampling happens in the web tier and the Space still only ever receives a
single still (`pyproject.toml:22-26`, `AGENTS.md:22-26`).

The one shared file is **`inference/banding.py`**, which imports nothing but `typing`. It lives
in `inference/` rather than `tagverify/` because `inference/` is `git subtree push`-ed to the Space
and cannot import from the web app — the dependency can only point one way
(`inference/banding.py:1-25`). `tests/test_banding.py` AST-walks the module and asserts its
imports are a subset of `{"typing", "__future__"}` so this cannot silently regress.

`inference/` runs **flat** inside the Space (`import detector`) and **as a package** in this
repo (`from inference.banding import ...`). That is why `detector.py` and `calibrate.py` carry a
try/except import shim (`inference/detector.py:60-65`). Do not "simplify" it away.

### How it fits together

```
   browser (playground)                      integrator / dooh-backend
   POST /ui/analyze                          POST /api/v1/analyze  (x-api-key)
   per-IP rate limit                         per-key rate limit, per-frame billing
          │                                            │
          └──────────────────────┬─────────────────────┘
                                 ▼
                 tagverify/analyze/intake.py      sniff bytes → image | video
                                 │           sha256, downscale to 768px, sample frames
                                 ▼
                 tagverify/analyze/run.py         ← ONE shared pipeline. No demo path.
                                 │
        ┌────────────────────────┼──────────────────────────┐
        ▼                        ▼                          ▼
  result cache             tag_thresholds            SigLIP 2 detector
  (Postgres `analyses`)    (Postgres)                (HF Space, inference/)
  RAW SCORES, not               │                    scores one still per call
  verdicts                      │                          │
        │                       │                          │
        └───────────────────────┴──────────┬───────────────┘
                                           ▼
                             tagverify/tags/decide.py     apply thresholds
                                           │         (re-decided on EVERY read)
                                           ▼
                             inference/banding.py    ← the verdict rule,
                                           │           shared by all callers
                                           ▼
                          tagverify/analyze/aggregate.py  collapse frames → one per tag
                                           │
                                           ▼
                                       response
```

Both entry points run the *same* pipeline. There is no demo path that behaves differently from
the real one. The playground differs from the API in exactly two ways: it authenticates by IP
instead of by key, and it renders HTML instead of JSON (`tagverify/web/playground.py:4-16`).

---

## 3. Repository map

### Top level

| Path | What it is |
|---|---|
| `tagverify/` | The web application. One FastAPI process serves the API, playground, docs and admin. |
| `inference/` | The SigLIP 2 detector, deployed separately to a Hugging Face Space. |
| `tests/` | pytest. Runs against the ASGI app in-process. |
| `docs/` | `DEPLOY.md`, `schema.sql`, and this file. |
| `README.md` | Onboarding, quick start, the invariants in prose. |
| `AGENTS.md` | Rules for contributors. `CLAUDE.md` is a one-line include of it. |
| `Makefile` | Every developer command. |
| `pyproject.toml` | Web-tier dependencies, ruff/mypy/pytest config, the `dooh` entry point. |
| `Dockerfile` | Two-stage build of the web tier only. |
| `.github/workflows/keepalive.yml` | 6-hourly ping so the free Space does not sleep. |

### Python, by file

```
tagverify/                                 inference/
  __init__.py                  3             __init__.py                12   (zero imports, on purpose)
  main.py                    174             banding.py                106   ← the verdict rule
  config.py                   54             versioning.py              58
  cli.py                     299             detector.py               371
  templating.py              163             app.py                    234   (HF Space entry point)
  errors.py                   84             calibrate.py              538
  analyze/                                   sweep.py                  316
    intake.py                464             smoke_test.py             192
    video.py                 329             fetch_demo_eval.py        515
    aggregate.py              79
    run.py                   218           tests/
  api/                                       conftest.py               111
    v1/analyze.py            145             support/videos.py         112   (helper, never collected)
    v1/tags.py                71             api/test_analyze.py       329
    v1/usage.py               47             api/test_errors.py         98
    v1/health.py              99             api/test_auth.py           53
  auth/                                      api/test_tags.py           30
    keys.py                   94             api/test_usage.py          25
    authenticate.py          170             api/test_health.py         23
    deps.py                  118             test_video.py             340
    admin.py                 150             test_tag_coverage.py      287
  db/                                        test_intake.py            235
    models.py                212             test_banding.py           225
    session.py               124             test_admin.py             208
    cache.py                 112             test_aggregate.py         191
    usage.py                  80             test_decision_version.py  167
    thresholds.py             67             test_templating.py        136
  scoring/client.py          259             test_cli.py                76
  tags/decide.py             324
  tags/groups.py              72           ≈ 9,500 lines of Python in total
  web/playground.py          210
  web/admin.py               206
  web/docs.py                 69
```

Two names in that tree are load-bearing and easy to misread:

- **`scoring/client.py`** is the HTTP client that *calls* the model tier. It is not `inference/`,
  which *is* the model tier. The two used to both be called `inference`, one nested inside the
  other, and the rule in `AGENTS.md` about never importing `inference.detector` from the web
  package existed partly to compensate for that.
- **`errors.py` sits at the package root**, not under `api/`. `ApiError` is app-wide: `main.py`
  installs the handlers, `auth/deps.py` raises them, and the web tier renders them as HTML.

### Templates, CSS, JS (~3,200 lines, no build step)

```
tagverify/templates/                          tagverify/static/
  base.html                  118           css/app.css                657   ← the design tokens
  _icons.html                 33           css/pages.css              529
  pages/playground.html       98           js/playground.js           480
  pages/docs.html            424           js/app.js                  161
  pages/admin.html            80           js/htmx.min.js          vendored
  pages/admin_login.html      32           favicon.svg
  pages/admin_unconfigured.html 24
  pages/error.html            17
  partials/results.html      118
  partials/verdict_card.html  79
  partials/keys_panel.html    98
  partials/threshold_row.html 66
  partials/thresholds_panel.html 52
  partials/inference_down.html 45
  partials/tag_picker.html    42
  partials/results_error.html 23
  partials/results_empty.html 10
```

There is no `package.json` and nothing here needs Node. Keep it that way (`AGENTS.md:78-83`).

### Git state

| | |
|---|---|
| Branch | `main`, 5 commits |
| `origin` | `https://github.com/kuljeet-singhh/Dooh-TagVerefication.git` |
| `space` | `https://huggingface.co/spaces/kuljeet1/tagVerefication` |

The repo began as a Next.js app (`Initial commit from Create Next App`) and was rewritten in
Python. **The rewrite is currently staged but not committed** — `git status` shows the whole
`tagverify/` tree added and the whole `app/` tree deleted. Any TypeScript, Drizzle, Vercel or `npm`
reference you find in a comment is stale and should be fixed (`AGENTS.md:7-8`); section 16 lists
the ones known to remain.

---

## 4. Request lifecycle

One ordered trace, `POST /api/v1/analyze` with a video, from bytes to JSON. Function names and
line numbers are the ones to breakpoint on.

| # | Step | Where | Notes |
|---|---|---|---|
| 1 | `guard` dependency resolves | `tagverify/auth/deps.py:40` | Reads `x-api-key` or `Authorization: Bearer`. Authenticates **and charges one unit** in a single CTE before the body is read. 401 / 429 exit here. |
| 2 | `read_intake` | `tagverify/api/v1/analyze.py:37` | Branches on content type: multipart, JSON, or `BAD_REQUEST`. |
| 3 | `intake.build` | `tagverify/analyze/intake.py:389` | Empty check, then `sha256` of the **original** bytes, then `video.looks_like_video(data)` decides the branch. **The bytes decide, never the filename.** |
| 4a | `_build_image` | `tagverify/analyze/intake.py:307` | 10MB cap → verify → downscale to 768px → JPEG q90 → one `Frame` at `t=0.0`. |
| 4b | `_build_video` | `tagverify/analyze/intake.py:330` | 50MB cap → `probe()` → 60s cap → `sample()` → downscale + encode + byte-dedupe each frame. |
| 5 | `run_analysis` | `tagverify/analyze/run.py:92` | The shared pipeline begins. Returns `(outcome, audit_row)`. |
| 6 | `cached_inference_health()` | `tagverify/scoring/client.py:227` | 60s memo. Needed first: supplies `packs_version` (part of the cache key) and the tag list. |
| 7 | **Unknown-tag gate** | `tagverify/analyze/run.py:110-115` | Any tag not in the live catalog → `AnalysisRejected`, **before any inference**. Audit row is `None`; nothing was checked. |
| 8 | `find_cached` | `tagverify/db/cache.py:42` | Key = (`image_hash`, `packs_version`, sorted tag set). Returns **raw scores**, never verdicts. |
| 9 | `cached_thresholds` | `tagverify/tags/decide.py:248` | 30s memo over `tag_thresholds`. Invalidated explicitly when admin saves. |
| 10 | `_score_frames` | `tagverify/analyze/run.py:179` | Cache miss only. **Sequential** — the Space is one queued process on two free vCPUs. `TOTAL_BUDGET_S = 30.0` checked *between* frames. Any frame failing raises. |
| 11 | `decide_all` | `tagverify/tags/decide.py:207` | Every frame decided independently against the thresholds. |
| 12 | `aggregate` | `tagverify/analyze/aggregate.py:63` | Collapse per-frame verdicts to one per tag: present > uncertain > absent, highest score inside the winning band. |
| 13 | `charge_extra` | `tagverify/auth/authenticate.py:96` | `frames_analyzed - 1` extra units, **only on a cache miss**. Best-effort; failures are logged, not raised. |
| 14 | `background.add_task(write_audit)` | `tagverify/api/v1/analyze.py:109` | The audit row is written after the response, in its own session. |
| 15 | Response | `tagverify/api/v1/analyze.py:115-145` | 200 + `X-RateLimit-Limit` / `X-RateLimit-Remaining`. |

The playground's `POST /ui/analyze` (`tagverify/web/playground.py:128`) enters at step 2 with an
IP-based limiter instead of step 1, passes `api_key_id=None`, and renders
`partials/results.html` instead of JSON. Steps 3–14 are byte-for-byte the same code.

---

## 5. Web tier — `tagverify/`

`tagverify/__init__.py` is three lines: a docstring and `__version__ = "1.0.0"`.

### 5.1 Application construction — `main.py` (174 lines)

`create_app()` (`main.py:76`) builds the FastAPI instance:

```python
app = FastAPI(
    title="DOOH Tag Verification",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)
```

FastAPI's auto-generated docs are **disabled deliberately**: `/docs` is the project's own
hand-written page (`tagverify/web/docs.py`), and a generated schema would be a competing, less
accurate description of the same API.

- Static mount (`main.py:88`): `app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")`.
- API routers (`main.py:90-91`): `analyze`, `tags`, `usage`, `health`, all under `prefix="/api/v1"`.
- Web routers (`main.py:93-95`), no prefix: `playground`, `docs`, `admin`.
- Module singleton at `main.py:174`: `app = create_app()` — the uvicorn target.

**Lifespan** (`main.py:49-73`):

1. `logging.basicConfig(level=settings().log_level.upper(), ...)`.
2. If `is_configured()`, `init_engine()` inside a `try/except` that only **logs** — a bad
   `DATABASE_URL` must not stop boot, or `/health` could never report it.
3. Otherwise a warning: database-backed routes will report degraded.
4. Starts `_prune_daily()` as a task.
5. On shutdown: cancel the pruner, `dispose_engine()`.

`_prune_daily()` (`main.py:35-46`) loops forever: `prune_usage_counters()` then sleep 24h, all
inside `with suppress(Exception)`. It exists because the previous implementation defined the
prune function and never called it, so `usage_counters` grew for the life of the database.

**Exception handlers** — `install_error_handlers(app)` (`main.py:105-171`). `_wants_html(request)`
is simply `not request.url.path.startswith("/api/")`.

| Handler | Catches | HTML path | API path |
|---|---|---|---|
| `_api_error` | `ApiError` | `pages/error.html` at the mapped status | `exc.response()` |
| `_not_authorised` | `admin_web.NotAuthorised` | `pages/admin_login.html`, "Your session has expired." at **403** | same (admin is never under `/api/`) |
| `_validation` | `RequestValidationError` | — | `BAD_REQUEST` 400, message `"{loc}: {msg}"` from the first error |
| `_http` | `StarletteHTTPException` | `pages/error.html` | `{"error": code, "message": detail}`; 401→`MISSING_KEY`, 404→`BAD_REQUEST`, 429→`RATE_LIMITED`, else `INTERNAL` |
| `_unexpected` | bare `Exception` | `pages/error.html`, "Something went wrong on our side." at 500 | `INTERNAL` 500. Always `log.exception` first. |

### 5.2 Configuration — `config.py` (54 lines)

**Nothing here raises on a missing value.** A missing `DATABASE_URL` degrades to a
`/api/v1/health` response that reports the problem, rather than a process that refuses to boot.
Monitoring you cannot reach is not monitoring.

```python
model_config = SettingsConfigDict(
    env_file=(".env", ".env.local"),   # .env.local second, so an existing checkout keeps working
    env_file_encoding="utf-8",
    extra="ignore",
)
```

| Field | Env var | Type | Default |
|---|---|---|---|
| `database_url` | `DATABASE_URL` | `str \| None` | `None` |
| `hf_space` | `HF_SPACE` | `str \| None` | `None` |
| `inference_url` | `INFERENCE_URL` | `str \| None` | `None` |
| `hf_token` | `HF_TOKEN` | `str \| None` | `None` |
| `admin_password` | `ADMIN_PASSWORD` | `str \| None` | `None` |
| `log_level` | `LOG_LEVEL` | `str` | `"INFO"` |
| `playground_rate_limit_per_min` | `PLAYGROUND_RATE_LIMIT_PER_MIN` | `int` | `20` |
| `admin_login_attempts_per_min` | `ADMIN_LOGIN_ATTEMPTS_PER_MIN` | `int` | `5` |

`inference_target` (property, `config.py:47-49`) returns `hf_space` stripped, else
`inference_url` stripped, else `None` — **`HF_SPACE` wins when both are set**, so leaving a
stale `INFERENCE_URL` in production is harmless but leaving a stale `HF_SPACE` in local dev is
not.

`settings()` is `@lru_cache(maxsize=1)`, so it is a process-lifetime singleton. Every call site
uses it rather than reading `os.environ` directly — including the test suite's skip markers
(`tests/conftest.py:49-54`), because `.env` values are loaded by pydantic-settings and never
exported to the environment.

Section 14 has the full operator-facing table.

### 5.3 Route inventory

Fourteen declared routes plus the static mount.

| Method | Path | Handler | Auth |
|---|---|---|---|
| `POST` | `/api/v1/analyze` | `tagverify/api/v1/analyze.py:73` | API key (`guard`) |
| `GET` | `/api/v1/tags` | `tagverify/api/v1/tags.py:26` | API key (`guard`) |
| `GET` | `/api/v1/usage` | `tagverify/api/v1/usage.py:22` | API key (`guard`) |
| `GET` | `/api/v1/health` | `tagverify/api/v1/health.py:82` | **none, on purpose** |
| `GET` | `/` | `tagverify/web/playground.py:98` | none |
| `GET` | `/ui/catalog` | `tagverify/web/playground.py:107` | none |
| `POST` | `/ui/analyze` | `tagverify/web/playground.py:128` | per-IP rate limit |
| `GET` | `/docs` | `tagverify/web/docs.py:46` | none |
| `GET` | `/admin` | `tagverify/web/admin.py:64` | session cookie |
| `POST` | `/admin/login` | `tagverify/web/admin.py:82` | throttled per IP |
| `POST` | `/admin/logout` | `tagverify/web/admin.py:111` | none (logging out needs no session) |
| `POST` | `/admin/keys` | `tagverify/web/admin.py:121` | `require_admin` + CSRF |
| `POST` | `/admin/keys/{key_id}/revoke` | `tagverify/web/admin.py:157` | `require_admin` + CSRF |
| `POST` | `/admin/thresholds/{slug}` | `tagverify/web/admin.py:170` | `require_admin` + CSRF |
| — | `/static/*` | `StaticFiles` mount, `main.py:88` | none |

### 5.4 The public API

#### `POST /api/v1/analyze`

**Accepted request shapes** — all three go through `tagverify/analyze/intake.py`:

| Content type | Media field | Tags field |
|---|---|---|
| `multipart/form-data` | `media` (preferred), `image`, or **any** file part with a non-empty filename | `tags` repeated, or one comma-separated `tags` |
| `application/json` | `media_base64` / `image_base64` — bare base64 or a `data:image/...;base64,` URL | `tags` as a list or a comma string |
| `application/json` | `media_url` / `image_url` — SSRF-guarded, redirects not followed | same |

Anything else → `BAD_REQUEST`, "Use multipart/form-data or application/json."

The "any file part" fallback (`intake.py:189-216`) is deliberate. A template once renamed its
input to `media` while the running server read only `image`; the request looked file-less and
the user was told to "choose a creative" — the one thing they had definitely done. When no file
is found at all, the error message names the fields that *did* arrive
(`intake.form_field_names`, names only, never values).

```bash
curl -X POST http://localhost:8000/api/v1/analyze \
  -H "x-api-key: dooh_live_..." \
  -F "media=@creative.jpg" \
  -F "tags=alcohol" -F "tags=gym_fitness"
```

A video goes to the same endpoint in the same field. Which kind it is comes from the bytes.

**Response, image** (HTTP 200, `analyze.py:115-145`):

```jsonc
{
  "request_id": "0b0e…",          // uuid4
  "image_hash": "…",              // sha256 of the SUBMITTED bytes, 64 hex chars
  "image_bytes": 148213,
  "model": "google/siglip2-base-patch16-224",
  "packs_version": "c14ac5bdcfa9",
  "decision_version": "…",        // 12 hex chars
  "cached": false,
  "latency_ms": 812,
  "results": [{
    "tag": "alcohol",
    "present": true,              // true | false | null  ← null means NEEDS A HUMAN
    "score": 0.977,
    "confidence": "high",         // high | medium | low
    "decided_by": "siglip",       // or "sigmoid_floor" when the absolute veto fired
    "calibrated": false,          // ← thresholds were never measured; this is a guess
    "evidence": {
      "top_phrase": "a glass of beer with foam",
      "crop": [0, 0.5, 0.5, 1],   // [x0, y0, x1, y1] as 0-1 fractions of the image
      "sigmoid": 0.4413
    }
  }],
  "uncalibrated_tags": ["alcohol"]
}
```

**Response, video** — same shape plus two additions:

```jsonc
{
  "results": [{
    "tag": "alcohol",
    "present": true,
    "evidence": {
      "top_phrase": "a can of beer",
      "crop": [0, 0.5, 0.5, 1],
      "sigmoid": 0.4413,
      "frame": { "index": 2, "timestamp_s": 2.0 }   // ← at which second
    }
  }],
  "media": { "kind": "video", "duration_s": 9.6, "frames_analyzed": 4, "frames_considered": 4 }
}
```

Three keys are **conditional**, and their absence is meaningful:

| Key | Present when | Why conditional |
|---|---|---|
| `uncalibrated_tags` | at least one tag is uncalibrated | Its presence is itself the signal; an empty array invites being ignored. |
| `media` | `kind == "video"` | An image response stays byte-identical to what integrations parsed before video existed. |
| `evidence.frame` | the verdict came from a video frame | Same reason. |

`frames_considered` exceeding `frames_analyzed` is how a caller learns coverage was partial.

**Per-frame billing** (`analyze.py:111-113`):

```python
if not outcome.cached and outcome.frames_analyzed > 1:
    await charge_extra(session, auth.key.id, outcome.frames_analyzed - 1)
```

`guard` charges exactly one unit before the body is read — right for a still, wrong for a video,
where six frames is six times the model time. The difference is charged after the fact so a
video cannot be used as the cheap way to consume six times the capacity. **A cache hit ran
nothing and stays one unit**, which is what `not outcome.cached` guarantees.
`tests/api/test_analyze.py:210` pins both halves.

#### `GET /api/v1/tags`

Requires a key. Returns the live catalog joined against the threshold table:

```jsonc
{
  "packs_version": "c14ac5bdcfa9",
  "decision_version": "…",
  "count": 20,
  "tags": [{ "slug": "alcohol", "label": "Alcoholic content",
             "description": "Beer, wine, spirits, cocktails, bars, or drinking of alcohol.",
             "calibrated": true }]
}
```

`calibrated` (`tags.py:58-65`) is `row.calibrated AND (row.packs_version_seen is empty OR it
matches the live pack)` — a threshold measured against a different pack stops claiming to be
calibrated even though it is still applied.

**Thresholds are never exposed.** Publishing them would invite gaming and freeze them into the
public contract. `decision_version` is not an exception: it is a truncated one-way hash of them,
published so a caller can key its own cache on the rule that produced a verdict instead of
serving a superseded one. `tests/api/test_tags.py:26` asserts no threshold field name appears
anywhere in the response text.

#### `GET /api/v1/usage`

Requires a key, and is strictly scoped to the key presented — there is no aggregate view here
(that lives behind `/admin`).

```jsonc
{
  "key": { "name": "Acme", "masked": "dooh_live_A1b2C3d4…", "rate_limit_per_min": 60 },
  "today": { "since": "2026-08-24T00:00:00Z", "requests": 412,
             "analyses": 380, "cache_hits": 91, "avg_latency_ms": 640 },
  "rate_limit": { "limit": 60, "remaining": 58 }
}
```

#### `GET /api/v1/health`

**Unauthenticated and always HTTP 200**, even when everything is down. Two reasons: monitoring
that needs a credential tends to stop being monitoring, and a 500 would tell a load balancer to
pull the instance when the unhealthy thing is a downstream dependency. Any non-200 therefore
means the app itself is down.

```jsonc
{
  "status": "ok",              // "ok" iff database.ok AND inference.ok
  "checked_in_ms": 41,
  "database":  { "ok": true },
  "inference": { "ok": true, "warm": true, "model": "…", "packs_version": "c14ac5bdcfa9",
                 "tags": 20, "prompts": 266 },
  "decision":  { "rule": "dcf11c44481b", "version": "…" }
}
```

- `database` (`health.py:28`) reports `{"ok": false, "error": "DATABASE_URL is not set"}` rather
  than throwing when unconfigured.
- `inference` (`health.py:41`) distinguishes **warming** (`{"ok": false, "warm": false,
  "warming": true}`) from broken (`"warming": false`). A free Space sleeps after 48h idle;
  `degraded` with `warming: true` right after a deploy is normal.
- `decision` (`health.py:61`) always reports `rule` — it is knowable without Postgres — and adds
  `version` when the thresholds load. `decision` is **deliberately excluded from `status`**: a
  stale rule is a fact, not a fault, and folding it in would page someone for a deploy.

`rule` is the answer to *"is my change live?"*. It is computed from the bytes of the banding
module **this process loaded**, once, at import. Compare it with
`shasum -a 256 inference/banding.py`; if they differ the server is stale and needs restarting. A
server started without `--reload` once served a superseded rule for hours with nothing anywhere
saying so.

### 5.5 The error envelope — `errors.py` (84 lines)

Flat, never nested: `{"error": CODE, "message": "...", ...extra}`. Extras are merged into the
same object; `retry_after`, when an `int`, is also emitted as a `Retry-After` header so
retryability is machine-readable (`errors.py:73-84`).

| Code | Status | Meaning |
|---|---|---|
| `MISSING_KEY` | 401 | No `x-api-key` and no bearer token. |
| `INVALID_KEY` | 401 | Unknown **or revoked** — identical responses, always. |
| `RATE_LIMITED` | 429 | Over the per-minute limit. Carries `retry_after` and `limit`. |
| `UNKNOWN_TAG` | 400 | A requested tag is not in the catalog. Carries `unknown_tags`, `known_tags`. Nothing was analysed. |
| `NO_TAGS` | 400 | No tags supplied. |
| `NO_IMAGE` | 400 | No media found. Message names the fields that did arrive. |
| `IMAGE_TOO_LARGE` | 413 | Over 10 MB. |
| `INVALID_IMAGE` | 400 | Empty, or does not decode. |
| `INVALID_IMAGE_URL` | 400 | Bad scheme, unresolvable host, private/loopback address, redirect, or non-2xx. |
| `VIDEO_TOO_LARGE` | 413 | Over 50 MB. |
| `VIDEO_TOO_LONG` | 413 | Over 60 s. |
| `INVALID_VIDEO` | 400 | Does not decode, or no frames could be read. |
| `BAD_REQUEST` | 400 | Unsupported content type, malformed JSON, failed validation. |
| `INFERENCE_WARMING` | 503 | The Space is asleep or loading. Carries `retry_after`. **Retryable.** |
| `INFERENCE_FAILED` | 502 | The Space was reachable and failed. |
| `INTERNAL` | 500 | Unhandled. |

`ApiError(code, message, **extra)` (`errors.py:60`) is the exception form; `api_error(...)`
builds the `JSONResponse`. Unmapped codes fall back to 500.

### 5.6 Authentication and rate limiting — `auth/`

#### Key format — `keys.py` (94 lines)

`dooh_live_` + 32 CSPRNG bytes as unpadded base64url. `key_hash` is `sha256(full plaintext)`;
`key_prefix` is the **first 8 characters of the secret** (not of the plaintext), kept for
display and for a non-scanning lookup. `masked_key()` renders `dooh_live_<prefix>…`.

The plaintext is never stored and is shown exactly once — by `dooh create-key` or by the admin
UI. Lookup is an exact match on a unique index over the hash, so there is no string comparison
to time-attack.

**Why sha256 and not bcrypt/argon2**: the key is 32 bytes of CSPRNG output, not a human
password. There is no dictionary to attack, so a slow KDF would only add latency to every
request (`keys.py:1-31`).

There is deliberately **no `verify_api_key()`** helper. An authenticate-only function that does
not also charge the rate limit is a footgun; the only entry point is
`authenticate_and_charge()`.

Revocation sets `revoked_at`; keys are never hard-deleted, because `analyses` rows reference
them.

#### The single-statement CTE — `authenticate.py` (170 lines)

`_QUERY` (`authenticate.py:60-83`) does everything in one round trip:

```sql
with k as (
  select id, name, key_prefix, rate_limit_per_min
    from api_keys where key_hash = :key_hash and revoked_at is null
),
bump as (
  insert into usage_counters (api_key_id, window_start, count)
  select k.id, :window_start, 1 from k
  on conflict (api_key_id, window_start)
    do update set count = usage_counters.count + 1
  returning count
),
touched as (
  update api_keys set last_used_at = :now where id = (select id from k)
)
select k.id, k.name, k.key_prefix, k.rate_limit_per_min,
       coalesce((select count from bump), 1) as count
  from k
```

It began as a latency optimisation — Neon's HTTP driver made every query a separate HTTPS
request (560–800 ms cross-region). That saving is now microseconds, but **it stays because it is
the only race-free form**: a read-then-write would let two concurrent requests both observe
`count = 59` against a limit of 60 and both proceed.

The window is `now.replace(second=0, microsecond=0)` — a fixed one-minute bucket. Fixed windows
permit up to 2× the limit across a boundary; this is abuse protection, not billing enforcement,
and the comment says so.

`charge_extra(session, api_key_id, extra)` (`authenticate.py:96-128`) tops up the same row. It
**recomputes** the window rather than accepting one, because a request straddling a minute
boundary should charge the window it finishes in — charging an already-rolled window would be
free. It is deliberately best-effort: wrapped in `try/except Exception` with a `log.exception`,
because the caller has already received a real answer.

#### `guard` — exact semantics (`deps.py:40-76`)

1. Read `x-api-key`. If absent, read `Authorization` and accept `Bearer <key>`.
2. Still absent → `MISSING_KEY` (401).
3. `authenticate_and_charge`. Not `AuthOk` → `INVALID_KEY` (401). **Unknown and revoked keys
   return byte-identical responses** — never confirm a key exists (`AGENTS.md` rule 8,
   `tests/api/test_auth.py` pins it).
4. **The charge happens before the limit check.** `if outcome.count > outcome.limit:` →
   `RATE_LIMITED` (429) with `retry_after` and `limit`. Note the strict `>`: the limit-th
   request succeeds. An over-limit request **still increments the counter**, so a client that
   keeps hammering keeps its own counter climbing — abuse is self-limiting rather than free to
   retry.
5. Returns `Guarded(key, limit, remaining=max(0, limit - count))`, whose `.headers()` supplies
   `X-RateLimit-Limit` and `X-RateLimit-Remaining`.

A structural reject happens first, with no database touch: a token that does not start with
`dooh_live_` fails immediately (`authenticate.py:134-135`). That leaks only "not shaped like one
of our keys", which is public information.

#### The playground limiter (`deps.py:83-118`)

The playground carries no API key by design, so it is limited per IP. `_hits: dict[str,
deque[float]]` is **in-process**: it resets on restart and each worker has its own. That is the
right size for the job; a shared store is not worth a Redis dependency here.

Unlike the API's fixed window this one is a **sliding** 60-second window. `client_ip()` reads
`request.client.host`, which uvicorn populates from `X-Forwarded-For` only under
`--proxy-headers`.

> **Do not add `--proxy-headers` for direct LAN access.** It makes uvicorn trust
> `X-Forwarded-For`, so any device on the network could spoof its IP and walk past this limit.
> Use it only behind a real reverse proxy, with `--forwarded-allow-ips` set to that proxy's
> address. `make serve-lan` omits it on purpose (`Makefile:31-35`).

### 5.7 Admin authentication — `auth/admin.py` (150 lines)

A single shared password, not user accounts. The session cookie is HMAC-signed **with the
password itself**, so it cannot be forged without knowing it and changing the password
invalidates every existing session for free.

| Constant | Value |
|---|---|
| `COOKIE` | `dooh_admin` |
| `MAX_AGE_SECONDS` | `7 * 24 * 60 * 60` (7 days) |
| `AdminState` | `"ok" \| "locked" \| "unconfigured"` |

Cookie value is `f"{issued_at_ms}.{hmac_sha256(password, issued_at_ms)}"`. `state(request)`
(`admin.py:43-70`) returns `"locked"` for: no cookie, no `.`, an empty half, a non-numeric
timestamp, an age over 7 days, or **a negative age** — a future-dated cookie must not be treated
as freshly issued. The signature check is `hmac.compare_digest`.

`issue_cookie` (`admin.py:73-92`) sets `httponly=True`, `samesite="lax"`, `path="/"`,
`max_age=MAX_AGE_SECONDS`, and:

```python
secure=request.url.scheme == "https"
```

Derived from the request, not hard-coded. Hard-coding it on breaks plain-HTTP localhost silently
(the browser drops the cookie and login appears to do nothing); hard-coding it off ships an
insecure cookie in production. Behind a TLS terminator this requires `--proxy-headers`, which is
why the `Dockerfile` CMD sets it.

**CSRF** (`admin.py:106-124`): the token is `hmac(password, "csrf")` — derived from the admin
password so it needs no separate secret and rotates with it. Next.js Server Actions carried an
implicit origin check; plain form POSTs do not. `SameSite=Lax` is the first layer, this is the
second. The token rides on `<body hx-headers='{"X-CSRF-Token": ...}'>` in `base.html`, so every
HTMX request inherits it.

**Login throttle** (`admin.py:127-150`): sliding 60-second window against
`admin_login_attempts_per_min` (default 5), in-process per IP. `record_attempt` is called **only
on a failed password**; a successful login clears the bucket.

**Admin defaults closed.** With `ADMIN_PASSWORD` unset the state is `"unconfigured"` — a
distinct value from `"locked"` — and `/admin` renders an explanation instead of opening. These
pages mint API keys and change the numbers that decide compliance verdicts; defaulting open
would be the dangerous failure.

**Authorisation lives on every handler, not on the page** (`web/admin.py:1-31`). Each POST is
independently reachable, so `require_admin(request)` (`web/admin.py:37-43`) re-checks the
session *and*, for POSTs, the CSRF header. It raises `NotAuthorised`, which `main.py` turns back
into the login page at 403.

### 5.8 Database — `db/`

Postgres (Neon). Async SQLAlchemy 2.x over psycopg3. Column names, types and defaults are
byte-identical to what the previous Drizzle schema created: this is a rewrite of the
application, **not a migration of the data**, and every already-issued API key keeps working
because `key_hash` is still `sha256(plaintext)` (`db/models.py:1-31`).

There is no migration tool, deliberately (`pyproject.toml:14-16`). `docs/schema.sql` provisions
a new database; the existing one needs nothing. Add Alembic when the first real schema change
lands.

#### `api_keys` (`models.py:34`)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | `TEXT` | no | — | PK, a uuid4 |
| `name` | `TEXT` | no | — | who the key is for |
| `key_hash` | `TEXT` | no | — | `sha256(plaintext)` hex |
| `key_prefix` | `TEXT` | no | — | first 8 chars of the secret, for display |
| `rate_limit_per_min` | `INTEGER` | no | `60` (python **and** server) | |
| `revoked_at` | `TIMESTAMPTZ` | yes | — | set by revoke; never deleted |
| `last_used_at` | `TIMESTAMPTZ` | yes | — | touched by the auth CTE |
| `created_at` | `TIMESTAMPTZ` | no | `now()` | |

Indexes: `api_keys_key_hash_idx` (**unique**), `api_keys_key_prefix_idx`.

#### `tag_thresholds` (`models.py:67`)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `slug` | `TEXT` | no | — | PK |
| `threshold_low` | `REAL` | no | `0.3` | |
| `threshold_high` | `REAL` | no | `0.55` | |
| `sigmoid_floor` | `REAL` | no | `0.005` | |
| `escalate` | `BOOLEAN` | no | `true` | in the table; **not** carried into `Thresholds`, so not folded into `decision_version` |
| `calibrated` | `BOOLEAN` | no | `false` | false until `calibrate.py` has run |
| `precision` | `REAL` | yes | — | `apply-calibration` stores the **Wilson lower bound**, not the point estimate |
| `recall` | `REAL` | yes | — | |
| `packs_version_seen` | `TEXT` | yes | — | the pack these numbers were tuned against |
| `updated_at` | `TIMESTAMPTZ` | no | `now()` | |
| `updated_by` | `TEXT` | yes | — | `'seed'`, `'admin'` or `'calibrate'` |

No indexes beyond the PK. The defaults `0.3 / 0.55 / 0.005` match `FALLBACK = Thresholds()`
(`tags/decide.py:143`) and `packs.json`'s `defaults` exactly.

**Why thresholds live in Postgres and prompts do not** (`models.py:67-90`): prompt packs must
live in `inference/packs.json` because the Space encodes them into text embeddings at startup.
Thresholds are only ever comparisons against a returned score, so the web tier applies them
after the fact — which means calibration and hand-tuning take effect immediately, with no
redeploy of the model.

#### `analyses` (`models.py:120`) — audit log **and** result cache

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `request_id` | `TEXT` | no | — | PK, a uuid4 |
| `api_key_id` | `TEXT` | yes | — | FK → `api_keys.id` **`ON DELETE SET NULL`**; `NULL` for playground traffic |
| `image_hash` | `TEXT` | no | — | sha256 of the submitted bytes. **The media itself is never stored.** |
| `tags_requested` | `JSONB` | no | — | stored **sorted** (`tag_key()`) |
| `results` | `JSONB` | no | — | **raw scores**, not decided verdicts; every frame's rows for a video |
| `packs_version` | `TEXT` | yes | — | |
| `latency_ms` | `INTEGER` | yes | — | |
| `cached` | `BOOLEAN` | no | `false` | |
| `created_at` | `TIMESTAMPTZ` | no | `now()` | |

Indexes: `analyses_image_hash_idx`, `analyses_api_key_created_idx (api_key_id, created_at)`.

`results` holds **every frame's** raw scores, not just the winning frame's. Which frame wins
depends on the thresholds, so a cached video re-read after a threshold change has to be
re-aggregated from all of them. Storing only the winner would silently freeze a video's verdict
at the thresholds of the day it was first analysed (`analyze/run.py:142-151`).

`ON DELETE SET NULL` is what makes the test suite's key fixture safe to hard-delete
(`tests/conftest.py:67-68`).

#### `eval_images` (`models.py:156`)

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `TEXT` | no | PK |
| `image_hash` | `TEXT` | no | hash of the eval file's **PATH**, not its bytes — deliberately **not** comparable with `analyses.image_hash` |
| `storage_ref` | `TEXT` | no | where the file lives |
| `tag_slug` | `TEXT` | no | |
| `label` | `BOOLEAN` | no | ground truth |
| `note` | `TEXT` | yes | e.g. `"score=0.94 sigmoid=0.31"` |
| `created_at` | `TIMESTAMPTZ` | no | `now()` |

Indexes: `eval_images_hash_tag_idx (image_hash, tag_slug)` **unique** — the target of
`apply-calibration`'s `ON CONFLICT DO NOTHING` — and `eval_images_tag_idx`.

#### `usage_counters` (`models.py:190`)

| Column | Type | Null | Default |
|---|---|---|---|
| `api_key_id` | `TEXT` | no | — |
| `window_start` | `TIMESTAMPTZ` | no | — (truncated to the minute) |
| `count` | `INTEGER` | no | `0` |

`PRIMARY KEY (api_key_id, window_start)` — the composite PK the `ON CONFLICT` upserts rely on.

#### Engine and sessions — `session.py` (124 lines)

```python
create_async_engine(_normalise(url),
    pool_size=5, max_overflow=5,
    pool_pre_ping=True, pool_recycle=300, echo=False)
async_sessionmaker(_engine, expire_on_commit=False)
```

Neon's pooler sits in front, so the local pool stays small. The important setting is
**`pool_pre_ping`**: a connection Neon recycled underneath us should be replaced silently, not
surfaced as a 500.

`_normalise(url)` (`session.py:50-59`) rewrites `postgres://`, `postgresql://` and
`postgresql+asyncpg://` all to `postgresql+psycopg://`, so any connection string Neon hands you
works unedited.

`DatabaseNotConfigured(RuntimeError)` is a **normal, reportable condition**, not a crash — it is
what lets `/health` say "not configured" and still return 200.

`session_scope()` (`session.py:98`) is an async context manager that lazily calls `init_engine()`
if needed, so scripts, tests and the CLI work without running the FastAPI lifespan. It commits
on success and rolls back on error. `get_session()` is the FastAPI dependency form.

#### Result cache — `cache.py` (112 lines)

The cache stores **raw scores, not verdicts**. Scores are the expensive part (a model forward
pass) and never change for the same image and prompts; verdicts are a cheap threshold comparison
expected to be retuned often. So the expensive half is cached and the cheap half is recomputed
on every read.

`find_cached(session, image_hash, tags, packs_version)` (`cache.py:42`):

```sql
select request_id, results, packs_version
  from analyses
 where image_hash = :image_hash
   and packs_version = :packs_version
   and (select coalesce(jsonb_agg(t order by t), '[]'::jsonb)
          from jsonb_array_elements_text(tags_requested) as t) = (:wanted)::jsonb
 order by created_at desc
 limit 1
```

The key is **(image sha256, sorted tag set, packs_version)**. Sorting on both sides means tag
order never fragments the cache (`tests/api/test_analyze.py` asserts a reordered tag list is a hit).

`record_analysis(...)` (`cache.py:74`) is keyword-only and opens its **own** `session_scope()`,
because it runs as a background task detached from the request session. It is wrapped in
`try/except Exception` with a `log.exception` — a logging failure must not cost the caller a
result, but unlike the previous implementation the failure is **logged rather than swallowed**.

A cache **hit still writes a row**: the table grows per request, not per unique image, because
the row is an audit record of a request being answered, not a record of an inference being run.

#### Usage reads — `usage.py` (80 lines)

The counter *increment* is not here — it is folded into the auth CTE. This file holds reads and
the sweeper: `utc_midnight()`, `usage_today()`, `analyses_today()` (returns `(analyses,
cache_hits, avg_latency_ms)` in one query), and `prune_usage_counters()`
(`delete from usage_counters where window_start < now() - interval '1 day'`), wired to both
`dooh prune-usage` and the daily lifespan task.

#### Threshold writes — `thresholds.py` (67 lines)

One function, `upsert_threshold(session, slug, *, low, high, floor)`. It is an **upsert, not an
update**: the previous implementation only ever `UPDATE`d, so a tag present in `packs.json` with
no row here silently no-opped — the admin pressed Save, nothing happened, and no error appeared
anywhere.

It sets `calibrated = false` and clears `precision`/`recall`, because numbers a human typed are
no longer the ones `calibrate.py` measured. Leaving the flag true would let a guess keep
presenting itself as a measurement.

It does **not** invalidate the 30-second threshold memo. That is the caller's job
(`web/admin.py` calls `invalidate_threshold_cache()` immediately after), because the memo is
process state rather than database state and a writer must not reach into the serving path. Skip
it and a saved threshold takes up to half a minute to appear, with nothing saying why.

Reads live in `tags/decide.py`, next to the memo and the rule that applies them. This is the only
path that sets thresholds by hand; `dooh apply-calibration` writes measured numbers through the
CLI, and `updated_by` records which happened — `'admin'`, `'calibrate'` or `'seed'`.

### 5.9 The decision layer — `tags/decide.py` (324 lines)

This is where a raw score becomes a verdict. It is pure — no database, no network — and imports
the rule itself from `inference/banding.py`.

#### Shapes

```python
@dataclass(slots=True) class FrameRef:  index: int; timestamp_s: float
@dataclass(slots=True) class Evidence:  top_phrase: str; crop: list[float]; sigmoid: float
                                        frame: FrameRef | None = None
@dataclass(slots=True) class Verdict:   tag: str; present: bool | None; score: float
                                        confidence: Confidence; decided_by: DecidedBy
                                        calibrated: bool; evidence: Evidence
                                        band: Band = "absent"
@dataclass(slots=True) class Thresholds:
    threshold_low: float = 0.3
    threshold_high: float = 0.55
    sigmoid_floor: float = 0.005
    calibrated: bool = False
    packs_version_seen: str | None = None
    precision: float | None = None
    recall: float | None = None

FALLBACK = Thresholds()          # decide.py:143 — used when a tag has no row
```

`Verdict.to_api()` (`decide.py:102-126`) is the public shape; `evidence["frame"]` is added only
when the frame reference exists.

#### `decide(raw, threshold, live_packs_version=None)` (`decide.py:146`)

It uses only `score`, `sigmoid`, `top_phrase` and `crop` from the raw row. **The Space's own
`present` and `band` are deliberately ignored**, because the Space decided them with the
provisional thresholds baked into `packs.json`, and the authoritative ones live in Postgres.

The staleness rule (`decide.py:174-177`):

```python
stale = bool(live_packs_version) and bool(t.packs_version_seen) and (
    t.packs_version_seen != live_packs_version
)
calibrated = t.calibrated and not stale
```

If a threshold was calibrated against a different pack than the one now serving, the threshold
is **still applied** — it is the best available — but `calibrated` reverts to `false`, because
it no longer describes the scores being produced. *We stop trusting it; we do not stop using
it.* A missing threshold row behaves the same way: `FALLBACK` is applied and `calibrated` is
`false`.

`decide_all(results, thresholds, live_packs_version)` (`decide.py:207`) maps `decide` over every
raw row — which for a video means **every frame is decided independently, before anything is
collapsed**. See section 8 for why the order matters.

#### Threshold loading and the 30-second memo

`load_thresholds(session)` selects the whole `tag_thresholds` table into
`dict[str, Thresholds]`. `cached_thresholds(session)` (`decide.py:248`) memoises it for
`_TTL_SECONDS = 30.0`. That TTL is the lag between saving a threshold and it taking effect — and
the admin UI calls `invalidate_threshold_cache()` explicitly on save, so the lag is zero for the
person who made the change.

#### `decision_version` — the fold (`decide.py:257-318`)

```python
digest = hashlib.sha256(f"{RULE_VERSION}|{SAMPLER_VERSION}".encode())

def fold(key: str, t: Thresholds) -> None:
    digest.update(f"|{key}".encode())
    for f in fields(t):
        value = getattr(t, f.name)
        token = value.hex() if isinstance(value, float) else f"\x01{value!r}"
        digest.update(f":{f.name}={token}".encode())

for slug in sorted(thresholds):
    fold(slug, thresholds[slug])
fold("\x00fallback", FALLBACK)

return digest.hexdigest()[:12]
```

Every design choice in those ten lines is load-bearing:

| Choice | Why |
|---|---|
| Seeded with `RULE_VERSION\|SAMPLER_VERSION` | The rule and the frame sampler are part of how a number is read. |
| **Every field folded mechanically**, not hand-picked | A hand-picked tuple under-covers the moment somebody adds a field and wires it into `decide()`. Over-covering costs one spare cache generation when calibration writes a new `precision`. |
| `float.hex()` | Exact and unambiguous. `repr()` invites a formatting change to quietly re-key every cache in the fleet. |
| `\x01` prefix on non-floats | So `None` cannot collide with `""`. |
| Slugs iterated **sorted** | Dict order must not make an unchanged config look changed. |
| `FALLBACK` folded under `"\x00fallback"` | `decide()` does `t = threshold or FALLBACK`, so editing those defaults changes verdicts for every uncalibrated tag. `\x00` is a key no slug can collide with — slugs are `[a-z_]`. |
| Truncated to 12 hex chars | 48 bits. A one-way digest of thresholds is not the thresholds, so publishing it leaks nothing. |

`tests/test_decision_version.py` (19 cases) pins all of this, including that the digest does not
contain any literal threshold value.

### 5.10 Tag display groups — `tags/groups.py` (72 lines)

Presentation only; never reaches the API.

| Group | `restricted` | Slugs |
|---|---|---|
| Restricted | `True` | `alcohol`, `tobacco_smoking`, `vaping`, `gambling`, `pharma_medicine`, `revealing_clothing` |
| Food & drink | `False` | `junk_food`, `sugary_drinks`, `restaurant_dining`, `protein_supplements` |
| Health & lifestyle | `False` | `gym_fitness`, `beauty_cosmetics`, `fashion_apparel`, `jewellery`, `travel_tourism` |
| Commerce & services | `False` | `automotive`, `real_estate`, `mobile_electronics`, `banking_finance`, `education` |

`group_tags()` (`groups.py:55`) emits groups in this order and appends anything left over to an
**"Other"** group. A tag added to `packs.json` but not to this file still shows up — adding a tag
never silently hides it from the UI.

### 5.11 Templating — `templating.py` (163 lines)

Jinja2 via `Jinja2Templates`. `render(request, template, context, status_code, headers)` is the
one entry point; `is_htmx(request)` checks the `hx-request` header.

**Why hand-written CSS rather than Tailwind**: Tailwind needs Node, which would mean a
`package.json` in a pure-Python project or a standalone binary as a build prerequisite.
`static/css/app.css` is one hand-authored stylesheet built on CSS custom properties — no build
step. The token layer at the top of that file *is* the design system.

**Asset cache-busting** — `asset(path)` (`templating.py:36`, memo dict at `:33`) returns
`/static/{path}?v={sha256(bytes)[:8]}`, memoised on the file's **`(st_mtime_ns, st_size)`**, not
on the process lifetime. This was an `lru_cache`, whose docstring claimed assets "update the
instant the file changes on a redeploy" — true in production, false under `make dev`, because
`--reload` watches `*.py`, so editing CSS or an ES module changed nothing the browser could see.
That failure is silent and points away from itself: the server *is* serving the new bytes, it is
simply never asked for them under a new name. Cost is one `stat()` per asset per render. A
missing file degrades to a plain `/static/{path}` so it 404s visibly rather than raising
mid-template.

A standing note at `templating.py:78-85`: static responses deliberately carry **no
`Cache-Control: immutable`**, because `playground.js` does `import { … } from "./app.js"` — a
relative specifier with no `?v=`, so `app.js` is also fetched under a bare unversioned URL.
Freezing that would make every future `app.js` edit unshippable. Versioning an ES module import
specifier needs a bundler or an import map; ETag revalidation is the right trade.

**Filters**: `relative_time` (`"just now"`, `"3h ago"`, then an absolute date past 30 days) and
`percent` (`"—"` for `None`).

**The `BANDS` global** (`templating.py:117-133`) defines verdict presentation once; templates
look it up by band rather than composing class names:

```python
BANDS = {
    "present":   {"word": "Present",      "icon": "alert",
                  "note": "This content is in the creative."},
    "uncertain": {"word": "Needs review", "icon": "question",
                  "note": "The score fell between the thresholds. A human has to decide."},
    "absent":    {"word": "Absent",       "icon": "check",
                  "note": "This content was not found."},
}
```

The word matters as much as the colour: **"Needs review"** states the required action, where
"UNCERTAIN" left the reader to work out that `present: null` is a task, not a middle value.

### 5.12 Frontend

No build step, no `package.json`, htmx vendored. Everything is server-rendered Jinja2 with HTMX
fragment swaps and two hand-written ES modules.

#### Templates

| File | Renders |
|---|---|
| `base.html` | The shell: head, the blocking theme-restore script (before first paint), `hx-headers` carrying the CSRF token, skip link, progress bar, left rail (nav, pack/model identifiers, health dot linking to `/api/v1/health`, theme toggle), `#main`, `#toasts`, `#announcer`. |
| `_icons.html` | One `{% macro icon(name, size) %}` with 20 inline SVG paths, drawn in `currentColor` so icons follow band colours. |
| `pages/playground.html` | The main page: the upload form (`hx-post="/ui/analyze"`, multipart, targeting `#results`), dropzone, tag picker, and the results column. |
| `pages/docs.html` | The hand-written API reference. Sections: `#two-fields`, `#authentication`, `#analyze`, `#decisions`, `#calibration`, `#other`, `#errors`. |
| `pages/admin.html` | Keys and Thresholds tabs, plus the live band-ruler preview that disables Save when `low > high`. |
| `pages/admin_login.html` | The sign-in card. |
| `pages/admin_unconfigured.html` | Explains that `ADMIN_PASSWORD` is unset and access is **refused**, not allowed. |
| `pages/error.html` | Generic error card (`code`, `message`). |
| `partials/results.html` | The `/ui/analyze` swap target: summary chips, the uncalibrated callout, the video-coverage callout, the evidence figure with one absolutely-positioned `.crop-box` per verdict, then `#verdict-list` **grouped by band** — never globally score-sorted. |
| `partials/verdict_card.html` | One verdict. Band chip, score, and either the three-segment threshold ruler **or**, when `decided_by == "sigmoid_floor"`, a veto explanation instead — a 0.94 bar labelled "absent" reads as a bug. |
| `partials/results_empty.html` | Idle state, suggesting near-miss test creatives (juice vs `alcohol`, baby formula vs `protein_supplements`). |
| `partials/results_error.html` | Warning callout. When retryable, a Retry button using `hx-include="#analyze-form"` so the already-downscaled file is re-sent without re-picking. |
| `partials/inference_down.html` | The warming panel. Self-polls `/ui/catalog` every 8s; the replacement fragment carries no trigger, which is what stops the poll. |
| `partials/tag_picker.html` | The 20 tags in four labelled fieldsets with All/None. Groups use `aria-labelledby` on a visible heading, **not** `<legend class="sr-only">` — Chrome's UA stylesheet overrode the hidden positioning, causing horizontal overflow on phones and double announcement. |
| `partials/keys_panel.html` | Key table, create form, and the one-time plaintext callout with Reveal / Copy / dismiss. |
| `partials/thresholds_panel.html` | Uncalibrated-count callout and the threshold grid. A real `<table>` cannot hold a form spanning cells, so it is a CSS grid with `display: contents` on each form. |
| `partials/threshold_row.html` | One row = one form posting to `/admin/thresholds/{slug}`, with a read-only ruler preview, three number inputs, a calibration chip, Save, and an error line. |

#### The design tokens — `static/css/app.css`

The governing rule, stated at the top of the file: **colour means a verdict**. Red, amber and
green are reserved for present / uncertain / absent. The interface itself is monochrome, and the
only chromatic accent in the chrome is a single azure used for links and focus rings — never a
fill, never a border on a result — so nothing in the chrome can be mistaken for an outcome. All
colours and spacing come from this block; **do not introduce literal values in templates.**

```css
:root {
  /* Neutral ramp */
  --gray-25:#fcfcfd;  --gray-50:#f8f9fa;  --gray-100:#f1f2f5; --gray-200:#e5e7ec;
  --gray-300:#d5d8e0; --gray-400:#9ba1ad; --gray-500:#6d7480; --gray-600:#4e5561;
  --gray-700:#363c46; --gray-800:#22262e; --gray-900:#14171d; --gray-950:#0b0d11;

  /* Azure — link text and focus rings ONLY. */
  --azure-100:#e4edfd; --azure-300:#8ab4ff; --azure-500:#2563eb;
  --azure-600:#1d4ed8;  --azure-700:#1a3fae;

  /* Semantic surfaces. Templates use ONLY these, never the ramps above. */
  --bg:var(--gray-50);      --surface:#ffffff;        --surface-2:var(--gray-50);
  --surface-3:var(--gray-100); --border:var(--gray-200); --border-strong:var(--gray-300);
  --fg:var(--gray-900);     --fg-muted:var(--gray-600);
  --fg-subtle:var(--gray-500); --fg-faint:var(--gray-400);

  /* Interface accent — monochrome. */
  --accent:var(--gray-900); --accent-hover:var(--gray-800); --accent-fg:#ffffff;
  --accent-text:var(--azure-600); --accent-soft:rgb(37 99 235 / .07);
  --accent-line:rgb(37 99 235 / .26); --ring:var(--azure-500);

  /* Code surfaces stay dark in both themes. */
  --code-bg:var(--gray-950); --code-fg:#d8dbe2;

  /* Verdict bands. The only colour that carries meaning.
     Text steps are -700/-800 because -600 fails AA on a tint. */
  --present-bg:#fef2f2;   --present-line:#fecaca;   --present-fg:#b42318;   --present-solid:#dc2626;
  --uncertain-bg:#fffbeb; --uncertain-line:#fde68a; --uncertain-fg:#92400e; --uncertain-solid:#d97706;
  --absent-bg:#ecfdf5;    --absent-line:#a7f3d0;    --absent-fg:#065f46;    --absent-solid:#059669;

  /* Type */
  --font-sans:"Inter var","Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
              "Helvetica Neue",Arial,sans-serif;
  --font-mono:ui-monospace,"SF Mono","SFMono-Regular","JetBrains Mono","Menlo","Consolas",monospace;

  /* Type scale — named by role, not by size. */
  --t-display:29px; --t-h1:21px; --t-h2:17px; --t-h3:15px;
  --t-body:14.5px;  --t-sm:13px; --t-xs:12px; --t-2xs:11px;

  /* Space scale */
  --s-1:4px; --s-2:8px; --s-3:12px; --s-4:16px; --s-5:20px; --s-6:24px; --s-7:32px; --s-8:44px;

  /* Radii */
  --r-xs:3px; --r-sm:5px; --r-md:7px; --r-lg:10px; --r-xl:14px; --r-full:999px;

  /* Elevation — hairline-first */
  --shadow-xs:0 1px 1px rgb(20 23 29 / .04);
  --shadow-sm:0 1px 2px rgb(20 23 29 / .05), 0 1px 3px rgb(20 23 29 / .06);
  --shadow-md:0 2px 4px -2px rgb(20 23 29 / .05), 0 6px 14px -4px rgb(20 23 29 / .10);
  --shadow-lg:0 8px 12px -6px rgb(20 23 29 / .06), 0 20px 36px -8px rgb(20 23 29 / .14);

  --rail-w:232px; --header-h:52px;
  --ease:cubic-bezier(.16,1,.3,1); --dur-1:100ms; --dur-2:160ms; --dur-3:240ms;
  color-scheme:light;
}
```

Dark is defined **twice, identically** — once under
`@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { … } }` and once under
`:root[data-theme="dark"] { … }` — so an explicit toggle wins in both directions. In dark, the
band colours become alpha tints over the same hues (`rgb(220 38 38 / .14)` etc.) with lighter
text steps, and the surfaces drop to `#08090b` / `#0f1114`.

`pages.css` (529 lines) holds page-level layout and depends entirely on these tokens.

#### `static/js/app.js` (161 lines)

App-wide behaviour, entirely via event delegation on `document.body`, so **nothing needs
rebinding after an HTMX swap**.

- **Theme** — reads `data-theme` or `prefers-color-scheme`; the toggle flips it and persists to
  `localStorage`.
- **Progress bar** — driven by `htmx:beforeRequest` / `beforeSwap` / `afterRequest`.
- **Toasts** — `toast(message, kind)`, `role="alert"` for errors. `htmx:responseError` parses the
  JSON body and toasts `message`; `htmx:sendError` → "Network unreachable."; `htmx:timeout` →
  "The request timed out." Server-driven toasts arrive via `HX-Trigger`.
- **`announce(message)`** — writes to the `aria-live="polite"` region.
- **Confirm** — intercepts `htmx:confirm` and builds a native `<dialog>` (focus trap, Esc and
  inertness for free) instead of `window.confirm`.
- **Clipboard** — falls back to a hidden textarea and `execCommand("copy")`, because the
  Clipboard API needs a secure context and plain-HTTP localhost is the common case.

#### `static/js/playground.js` (480 lines)

An ES module importing `announce` and `toast` from `app.js`.

| Constant | Value |
|---|---|
| `MAX_EDGE` | `768` |
| `QUALITY` | `0.9` |
| `PASSTHROUGH_TYPES` | `image/jpeg`, `image/png`, `image/webp` |
| `MAX_PASSTHROUGH_BYTES` | 8 MB |
| `MAX_VIDEO_BYTES` | 50 MB |

- **`downscale(file)`** — canvas resize to 768px longest edge at q0.9. Pass-through only for the
  three formats the server certainly reads and under 8 MB; HEIC/AVIF/TIFF are always re-encoded
  so an iPhone pick does not become an `INVALID_IMAGE`.
- **`looksLikeVideo(file)`** — MIME or extension. Explicitly *"only about giving fast feedback,
  never about deciding what runs"* — the server sniffs the bytes.
- **`acceptVideo(file)`** — passes the file through untouched via `DataTransfer` into the real
  file input, previews it from an object URL, and probes `loadedmetadata` only to show duration.
- **`accept(file)`** — revokes the previous object URL (the old build leaked one blob per
  upload) and writes the result back into the input, so a plain multipart submit carries it. No
  custom XHR.
- **Dropzone** — drag enter/leave uses a **depth counter**, because a plain `dragleave` fires
  when crossing onto a child and made the zone flicker. Click, Enter/Space, and a document-level
  paste handler all work.
- **Tag picker** — search filter, per-group All/None, and a Copy-link button building
  `?tags=slug,slug`. The submit button's label always states what is missing rather than being a
  silent dead end.
- **Evidence focus** — clicking a verdict card moves `data-focused` across the crop boxes
  (geometry is server-rendered as inline percentages, so no measurement and no resize listener),
  **seeks `#evidence-video` to that frame's timestamp**, and rewrites the caption. Arrow keys
  move through the list.
- **Elapsed timer** — after 5 seconds the label switches to
  `"…s — the Space may be waking from sleep, which takes 30–60s."`

#### The HTMX status-code convention

A **user-fixable problem comes back as HTTP 200 with the panel re-rendered**, carrying an inline
error — never a 4xx, which HTMX would drop on the floor, leaving a button that appears to do
nothing. A 4xx/5xx means something actually broke, and `app.js` turns it into a toast. Both
halves are load-bearing; `tests/test_admin.py` asserts the 200 side for empty key names and
out-of-range thresholds.

### 5.13 The CLI — `cli.py` (290 lines)

A Typer app, installed as `dooh` via `[project.scripts]`. Everything reads `DATABASE_URL` from
the environment or `.env`, exactly as the app does.

| Command | Arguments | What it does |
|---|---|---|
| `dooh seed-thresholds` | — | Upserts one `tag_thresholds` row per pack in `inference/packs.json`, using that pack's overrides or the file's `defaults`. **Rows marked `calibrated` are skipped, never overwritten**, so re-seeding cannot clobber measured thresholds with the guesses in `packs.json`. Prints `insert` / `refresh` / `skip` per slug and a summary. |
| `dooh create-key NAME` | `--rate-limit / -r` (default 60, 1–10000) | Issues a key and prints the plaintext **once**, with a warning that only a hash is stored. |
| `dooh apply-calibration [REPORT]` | default `inference/calibration.json` | Applies measured thresholds. **Refuses on a pack-version mismatch** (prints both and exits 1). Applies only tags with `calibrated: true`, storing `precision_lower` — the Wilson lower bound, not the point estimate — into `precision`. Then bulk-inserts `eval_images` rows with `ON CONFLICT DO NOTHING`. |
| `dooh prune-usage` | — | Drops expired rate-limit rows; prints the count. |
| `dooh health` | — | Prints the `/api/v1/health` database and inference report as JSON, without starting a server. |

`packs_version()` (`cli.py:41-50`) delegates to `inference.versioning.scorer_version` rather than
reimplementing it, because a drifted copy would not warn — it would make `apply-calibration`
silently refuse to apply measured thresholds.

Two bugs are documented inline in `apply-calibration` and pinned by `tests/test_cli.py`:

1. A per-row `await session.rollback()` unwound the enclosing `session_scope()` transaction and
   discarded every threshold just applied, **while still printing success** (`cli.py:212-223`).
   The test asserts the string `rollback` does not appear in the function's source.
2. `rowcount` is `-1` for `ON CONFLICT DO NOTHING` through this driver, which printed
   "recorded -1 labels" — hence the `RETURNING` clause (`cli.py:242-245`).

---

## 6. Analysis pipeline — `tagverify/analyze/`

### 6.1 Intake — `intake.py` (464 lines)

**The kind is decided by the bytes, not the caller.** Not the multipart field name, not the
filename, not the `Content-Type`. A compliance tool that lets the caller choose which checks run
is not a compliance tool: a video renamed `creative.jpg` must still be analysed as a video
(`intake.py:14-23`, `AGENTS.md` rule 12).

| Constant | Value | Why |
|---|---|---|
| `MAX_IMAGE_BYTES` | 10 MB | The old 4 MB cap existed only for Vercel's serverless body limit; this one protects the model host. |
| `MAX_EDGE` | 768 px | SigLIP consumes 224×224 tiles and scores the full image plus nine half-size crops, so detail past 768px on the longest edge is discarded by the model anyway. |
| `JPEG_QUALITY` | 90 | Transport re-encode. |

The nine error codes it can raise: `NO_IMAGE`, `NO_TAGS`, `IMAGE_TOO_LARGE`, `INVALID_IMAGE`,
`INVALID_IMAGE_URL`, `VIDEO_TOO_LARGE`, `VIDEO_TOO_LONG`, `INVALID_VIDEO`, `BAD_REQUEST` — all
carried on `MediaIntakeError(code, message)`.

**Shapes:**

```python
@dataclass(slots=True) class Frame:
    base64: str
    hash: str            # sha256 of this frame's ENCODED bytes — used only to drop
                         # byte-identical survivors of the visual dedupe. NOT a cache key.
    index: int
    timestamp_s: float   # always 0.0 for a still

@dataclass(slots=True) class Intake:
    kind: Literal["image", "video"]
    hash: str            # sha256 of the raw SUBMITTED bytes — the cache key and audit record
    bytes: int
    tags: list[str]
    frames: list[Frame] = []
    duration_s: float | None = None
    frames_considered: int = 1
```

`build(data, tags)` (`intake.py:389`) hashes the **original** bytes before any processing: two
callers sending the same file must get the same cache key whether or not our re-encoding step
happened to be deterministic for them.

**The SSRF guard** (`intake.py:119-183`) — for `media_url` fetches:

- Scheme must be `http` or `https`.
- The hostname is resolved via `socket.getaddrinfo` off the event loop, and **every** resolved
  address must be public.
- Refused: private, loopback, link-local (`169.254.0.0/16` — cloud instance metadata),
  multicast, reserved, unspecified, and CGNAT `100.64.0.0/10`. An IPv4-mapped IPv6 address is
  unwrapped and re-judged as v4, so `::ffff:127.0.0.1` is caught.
- An unparseable address is refused rather than allowed.
- `follow_redirects=False` — a redirect could hop to a private address *after* validation, so
  redirects are refused outright rather than re-validated per hop. Timeout 10s; any status ≥ 300
  is `INVALID_IMAGE_URL`.

`tests/test_intake.py` runs 14 refused addresses and 3 allowed ones.

**Tag normalisation** (`intake.py:229`): accepts a list or a comma-separated string, trims, drops
empties, dedupes preserving order. Empty result → `NO_TAGS`.

### 6.2 Video sampling — `video.py` (329 lines)

| Constant | Value | Why |
|---|---|---|
| `MAX_VIDEO_BYTES` | 50 MB | Matches the backend's cap in `creative-upload-rules.ts`. |
| `MAX_DURATION_S` | 60.0 | Creatives are capped at 10s upstream; this is headroom for direct API callers. |
| `MAX_FRAMES` | 6 | A latency budget, not a quality one. ~1s warm per frame; the backend gives a video upload a 35s deadline, sized against `TOTAL_BUDGET_S = 30`. |
| `MIN_FRAME_GAP_S` | 0.35 | Two frames closer together than this are the same moment. |
| `SIGNATURE_GRID` | 8 | 8×8 in colour = 192 numbers per frame. |
| `SIGNATURE_DISTANCE` | 8.0 | Mean absolute difference per value on a 0–255 scale. |

**Sniffing** (`video.py:86-95`) is two byte comparisons:

```python
return data[:4] == b"\x1a\x45\xdf\xa3"   # EBML — WebM / Matroska
    or data[4:8] == b"ftyp"              # MP4 / MOV
```

**Sampling** — `sample(data)` (`video.py:293`), in order:

1. `probe(data)` for duration and dimensions. A duration of `0.0` means **unknown, not empty** —
   callers do not refuse the file.
2. `_decode_keyframes(data)` sets `stream.codec_context.skip_frame = "NONKEY"`, so the decoder
   throws non-keyframes away before reconstructing them. I-frames are where the encoder itself
   decided the picture changed — as close to a free scene-cut detector as exists. Decoding every
   frame of a 10s clip would be ~300 images.
3. **If fewer than two keyframes were found**, `_decode_spread(data, MAX_FRAMES)` does two full
   decode passes: one to collect timestamps, one to convert only the wanted indices. It does not
   seek, because a seek can only land on a keyframe — with exactly one, every seek returned the
   opening frame, the dedupe discarded them all as repeats, and an eight-second creative was
   judged on its first frame alone, indistinguishable from a genuinely static card. Two passes
   are required because PyAV reuses frame buffers.
4. No frames at all → `VideoDecodeError`, never an empty list. A caller handed an empty result
   would report "no tags present".
5. `_dedupe(frames)`, then `_thin(unique, MAX_FRAMES)`.

**The colour-aware dedupe.** `signature(image)` resizes to 8×8 RGB and flattens to 192 integers.
`looks_the_same(a, b)` is the **mean absolute difference across all 192 values, cutoff ≤ 8.0**.
Each candidate is compared against **every** kept frame, not just the previous one, so an
A→B→A creative does not keep two copies of A.

> **Why not an average hash.** A 64-bit aHash greyscales and thresholds each cell against the
> frame's own mean, so a flat-colour card has every bit zero — **a solid red frame and a solid
> blue frame produce the same hash.** DOOH creatives are full of flat-colour backgrounds, and
> the consequence is silent: the second scene is dropped as a duplicate and the response still
> says the video was analysed. Keeping colour values fixes both blind spots at once —
> greyscaling loses red-vs-blue, self-relative thresholding loses light-vs-dark
> (`video.py:216-228`).

`_thin` always keeps the **first and last** frames: a creative's opening and closing cards are
where the brand and the legal small print live.

**`frames_considered` vs `frames_analyzed`.** `considered` is the count of distinct frames after
dedupe but **before** the `MAX_FRAMES` cap. It exists so that "we looked at 6 of 11 scenes" is
never mistaken for "we looked at everything". `frames_analyzed` is derived in `run.py` from the
**raw scores**, not from intake — so a cache hit reports what was actually analysed rather than
what this request happened to re-sample.

Nothing is written to disk: decoding is from an in-memory `io.BytesIO`, which is what upholds
"never store the media" for video.

### 6.3 Aggregation — `aggregate.py` (79 lines)

The rule: **present (any frame) > uncertain (any frame) > absent (every frame)**, and within the
winning band the highest-scoring frame supplies the evidence.

```python
_PRECEDENCE = {"absent": 0, "uncertain": 1, "present": 2}

def _rank(v): return (_PRECEDENCE.get(v.band, 0), v.score)

def aggregate(verdicts):
    best = {}
    for v in verdicts:
        incumbent = best.get(v.tag)
        if incumbent is None or _rank(v) > _rank(incumbent):
            best[v.tag] = v
    return list(best.values())
```

Band first; score only as the tie-break **inside** a band. Tag order follows first appearance.

**Why not `max(score)`** — two reasons, both producing a confident wrong answer rather than an
error, and both silent in production (`aggregate.py:15-31`):

1. `present: null` would get flattened. A video with one uncertain frame and five absent ones
   has a higher-scoring absent frame about as often as not.
2. **The sigmoid floor is a per-frame veto**, applied before banding. A frame can score 0.92 and
   still be absent because it resembles nothing — "a photo of a mountain reads as alcohol".
   Ranking by raw score would let exactly those vetoed frames win, reintroducing that bug one
   level up, where the veto cannot see it.

**Evidence is carried whole from one frame** (`aggregate.py:34-40`): the winning frame's score,
sigmoid, phrase and crop travel together and are never mixed with another frame's. A verdict
assembled from the highest score of one frame and the highest sigmoid of another describes no
frame that exists, and its crop would point at the wrong place.

A single frame in returns that verdict straight back out — which is what keeps the still-image
path bit-for-bit unchanged by this module rather than merely intended to be.

The rule lives here rather than in `inference/banding.py` because the Space only ever sees a
single still and has no concept of a video, so this rule has exactly one caller.

### 6.4 The shared pipeline — `run.py` (218 lines)

`TOTAL_BUDGET_S = 30.0` is a ceiling on the whole media, on top of the per-call
`CALL_TIMEOUT_S = 8.0` in the inference client. Six frames each allowed the full 8s would
otherwise let one request occupy a connection for the better part of a minute. It is checked
**between** frames, so it never interrupts a call in flight — it just declines to start another
one, and frame 0 is never pre-empted.

**Result types:**

```python
@dataclass(slots=True) class AnalysisSuccess:
    ok, request_id, image_hash, image_bytes, model, packs_version, decision_version,
    cached, latency_ms, verdicts, kind, duration_s, frames_analyzed, frames_considered,
    uncalibrated, thresholds

@dataclass(slots=True) class AnalysisRejected:
    ok = False; code = "UNKNOWN_TAG"; unknown: list[str]; known: list[str]
```

`thresholds` is for the UI's band ruler and is **never returned by the public API**.

`run_analysis(...)` returns `(outcome, audit_row)` — **the caller is responsible for writing the
audit row as a background task**, so the response is not held up by it. On the unknown-tag path
the audit row is `None`: nothing was checked, so there is nothing to record.

`_score_frames` (`run.py:179`) is **sequential on purpose**: the Space is a single queued process
on two free CPU cores, so issuing frames concurrently reorders the queue without shortening it,
and costs the ability to stop early on a failure. Only for video does it stamp `frame_index` and
`timestamp_s` onto each raw row — an image's rows keep exactly the shape they always had, so
cache entries written before video existed still decode cleanly.

---

## 7. Model tier — `inference/`

### 7.1 The detector — `detector.py` (371 lines)

| | |
|---|---|
| Model | `google/siglip2-base-patch16-224` |
| Embedding dim | 768 |
| `MAX_TEXT_LEN` | 64 — SigLIP was trained with every caption padded to exactly 64 tokens; padding differently silently degrades scores |

**Startup** encodes all 266 prompts once into a text-embedding table
(`Detector.__init__`, `detector.py:162`). That is the whole performance trick: it is the
difference between ~300ms and several seconds on the two free CPU cores a Hugging Face Space
gets. It also reads `logit_scale` and `logit_bias` off the model, because
`logit = exp(logit_scale) · cosine + logit_bias`; the bias cancels inside softmax but genuinely
matters for the sigmoid.

**Cropping** — `build_crops(image, grid=3)` (`detector.py:107`) returns **10 crops**: the whole
image plus a 3×3 grid of overlapping windows, each covering half the width and half the height
(`size = 0.5`, `step = 0.25`). All ten go through one batched forward pass.

SigLIP resizes its input to 224×224, so a beer bottle in the corner of a 4K billboard becomes
about eight pixels of brown mush. The overlap matters: non-overlapping tiles slice an object on
a boundary, whereas overlapping windows guarantee anything smaller than a quarter-image sits
fully inside at least one window. The source calls this *"the single biggest accuracy win in the
pipeline."*

**`_verdict(slug, logits, crops)`** (`detector.py:300`) — the softmax pool:

```python
columns = pos + neg + self._distractor_rows + self._rival_rows[slug]
probs = logits[:, columns].softmax(dim=-1)
positive_mass = probs[:, :len(pos)].sum(dim=-1)      # per crop
best = int(positive_mass.argmax())
score = float(positive_mass[best])                   # the RELATIVE question

sigmoids = torch.sigmoid(logits[best, pos])
sigmoid = float(sigmoids.max())                      # the ABSOLUTE question
top_phrase = self._prompt_rows[pos[int(sigmoids.argmax())]]
```

**The catalog is also the negative space.** Each tag is scored by softmax over its positives, its
hard negatives, the six shared distractors — **and every other tag's positives**
(`_rival_rows`). Without that last part, a pool that cannot describe the image hands its mass to
the tag's positives by default. Measured: a real gym poster scored **0.97** for `gambling` on
"a sports betting app on a phone screen", because gambling's negatives are all board games and
machines and the shared distractors are textureless backdrops, while `gym_fitness` sat unused in
the same catalog. Adding the rival positives dropped it to **0.06**. It costs nothing — the
logits already cover every prompt, so it only widens a column slice — and it means a tag's
coverage of the real ad space now decides how well every *other* tag behaves. Rival rows are
deduped (a phrase appearing twice would be counted twice in the denominator) and **sorted**, for
determinism.

An unknown slug raises `KeyError` listing the known tags — the caller's bug, never silently
skipped.

### 7.2 The prompt pack — `packs.json`

| | |
|---|---|
| `prompt_template` | `"This is a photo of {}."` |
| `shared_distractors` | 6 |
| Tags | **20** |
| Positive prompts | 123 |
| Hard negatives | 139 |
| Unique interned prompt rows | **266** |
| Defaults | `threshold_low 0.30`, `threshold_high 0.55`, `sigmoid_floor 0.005`, `escalate true` |

The 20 slugs: `alcohol`, `tobacco_smoking`, `vaping`, `gym_fitness`, `protein_supplements`,
`gambling`, `junk_food`, `sugary_drinks`, `pharma_medicine`, `beauty_cosmetics`, `jewellery`,
`automotive`, `real_estate`, `fashion_apparel`, `restaurant_dining`, `mobile_electronics`,
`travel_tourism`, `education`, `banking_finance`, `revealing_clothing`.

**`revealing_clothing` is the only tag with a per-tag threshold override**, and it only raises
the floor to `0.02`: a false "revealing" flag on a normal creative is an expensive mistake, so
it demands a bit more absolute resemblance. Every other tag inherits the defaults.

The file's `$comment` block records two prompt-writing rules, each with the measurement that
produced it:

1. **Mirror the phrasings.** With only "a tin of baby formula" as a negative, infant formula
   scored **0.709** for `protein_supplements`; adding "a scoop of baby formula milk powder"
   dropped it to **0.224** while real whey protein stayed at **0.999**.
2. **Name the confusable neighbour.** A photo of a hamburger scored **0.939** for `alcohol` —
   above champagne, cocktails and wine — because alcohol's positives describe bar and restaurant
   *scenes* and pub food matches that setting. Naming the scene explicitly as a negative is what
   separates "a bar" from "food served in a bar".

Similarly, a portrait "BIG SALE OFFER" poster scored **0.6167** for `gambling` and a real
advertiser was refused; four advertising negatives took it to **0.0377** while a slot machine
stayed at 0.9964 and roulette at 0.9999.

`detect_objects` appears on 19 of the 20 tags but is **not read anywhere** in the current
pipeline — see section 16.

### 7.3 The Space — `app.py` (234 lines)

The model loads at **import time**, not on first request, so the Space is either fully ready or
still booting — never half-warm.

| Limit | Value |
|---|---|
| `MAX_IMAGE_BYTES` | 12 MB |
| `MAX_TAGS_PER_CALL` | 30 |
| Port | 7860 (what HF Spaces expects) |
| Queue | `demo.queue(max_size=32)` |

Three endpoints, registered with `gr.api`:

| `api_name` | Signature | Returns |
|---|---|---|
| `analyze` | `(image_b64: str, tags: list[str])` | `{model, packs_version, latency_ms, image_size, results[]}` |
| `health` | `()` | `{status, model, packs_version, tags[], prompts}` |
| `tags` | `()` | `{packs_version, tags: [{slug, label, description}]}` |

Tags are validated **before** the image is decoded, because validation is cheap. **Thresholds are
deliberately not exposed** by `tags` — they are tuning internals, not part of the contract.

The Gradio UI exists for manual poking and is registered with `api_name=False`, so it is never
part of the public API surface. It draws boxes only for `present` and `uncertain` bands —
boxing every absent tag is just noise.

`inference/requirements.txt` pins `torch>=2.9.0`, `transformers>=4.49.0` (4.49 is the first
release with SigLIP 2), `gradio>=5.0.0`, `pillow>=10.0.0`, `numpy>=1.26.0`, off the CPU-only
PyTorch index — on Linux that avoids ~2GB of unused CUDA libraries, and the index is additive so
pip still falls back to PyPI on macOS arm64.

### 7.4 The client — `tagverify/scoring/client.py` (259 lines)

The **only** module that knows the inference service is a Gradio Space.

Gradio's HTTP API is a three-step protocol (upload the file, POST for an `event_id`, then parse
a Server-Sent Events stream). `gradio_client` hides that, and sending the image as **base64
rather than a file upload skips the upload step entirely** — one round trip.

`gradio_client` is synchronous, so every call goes through `anyio.to_thread.run_sync`. Blocking
the event loop on a multi-second model call would stall every other request in the process.

| Constant | Value |
|---|---|
| `CALL_TIMEOUT_S` | 8.0 |
| `CONNECT_TIMEOUT_S` | 6.0 |
| `_TTL_S` | 60.0 (health and catalog memo) |

- The client is cached behind an `asyncio.Lock`, so concurrent cold-start requests share one
  handshake instead of stampeding. **A failed handshake is never cached** — one blip would
  poison the process until restart.
- `hf_token` is passed **only if non-empty**; passing an empty one is treated as a malformed
  credential rather than as "no credential".
- **There is no retry loop.** One call; on any non-timeout exception the client is reset (a
  dropped connection usually means the Space restarted) and the error is raised. Retry is the
  caller's job, expressed as a 503 with `retry_after`.

Two exception types, mapped straight to HTTP:

| Exception | Meaning | HTTP |
|---|---|---|
| `InferenceWarming(message, retry_after=30)` | Asleep or loading weights | 503 `INFERENCE_WARMING` + `Retry-After` |
| `InferenceError` | Reachable and failed, or an unrecognisable response | 502 `INFERENCE_FAILED` |

Responses are validated with pydantic (`RawVerdict`, `RawAnalysis`, `InferenceHealth`,
`TagCatalog`). The web tier **ignores the Space's own `present`/`band`** and re-decides from
`score` + `sigmoid` — but still parses those fields, so a shape change in the Space is caught
here rather than surfacing as a `KeyError` deep in the pipeline.

The 60-second memo on health and catalog exists because `/analyze` needs `packs_version` (part
of the cache key) and the tag list (to reject unknown tags) before doing anything. It is the
difference between a 280ms cache hit and a 900ms one. The accepted cost: up to a minute of
scores keyed to the previous pack version after a deploy.

### 7.5 Calibration — `calibrate.py`, `sweep.py`, `calibration.json`

`calibrate.py` (538 lines) sweeps thresholds against the eval corpus and writes
`calibration.json`, which `dooh apply-calibration` reads. It writes JSON rather than touching
the database directly, because giving the Space a `DATABASE_URL` would put production database
credentials into an environment whose only job is running a model.

| Constant | Value | Meaning |
|---|---|---|
| `MIN_PER_CLASS` | 8 | Below this a tag is scored and reported but **not** marked calibrated. |
| `MAX_ESCALATION_RATE` | 0.35 | A budget dial, not a correctness one. |
| `--target-precision` | 0.90 | Default. |

The selection objective (`calibrate.py:360`):

```python
best = max(feasible, key=lambda o: (round(o.recall, 4), -o.fn, -round(o.escalation_rate, 4)))
```

**Maximise recall, then minimise false negatives, then minimise escalation** — in that order. An
earlier version tie-broke on low escalation, which collapsed the uncertain band to zero width
and turned every borderline positive into a confident "absent".

`pick_floor` is half the smallest true-positive sigmoid, clamped to `[0.001, 0.05]`.

A **Wilson lower bound** is computed and reported for every tag but is *not* used for selection —
gating on the bound made every tag fail. It is what `apply-calibration` stores as `precision`,
though, so the database holds the conservative number. The intuition it encodes: 5/5 correct is
a point estimate of 100% but bounds at ~57%; a flawless classifier on 10 positives bounds at
72%; clearing 90% takes roughly 35 consecutive correct calls.

When no threshold pair is feasible, the report names the three highest-scoring negatives with
their scores and tells you to add hard negatives describing them.

**Current state of `calibration.json`: 8 of 20 tags are applied.** `alcohol`, `automotive`,
`beauty_cosmetics`, `fashion_apparel`, `gambling`, `jewellery`, `real_estate` and `vaping` are
`calibrated: true`. The other 12 were measured and then **held back by hand** after
`inference/sweep.py` showed that applying all 20 was a net regression: cross-tag false blocks
went **152 → 211 (+39%)** for mean recall **0.665 → 0.652**. Only tags that improved or held
level on both axes were applied. Re-run `sweep.py` after any recalibration before applying.

`sweep.py` (315 lines) is the blast-radius report: it scores every eval image against **all 20
tags in one forward pass** (a column slice, not a second pass) and counts, per tag, how many
images owned by *other* tags it calls present. It exists because the same beer that `alcohol`
scored 0.977 also came back **0.993 for `restaurant_dining`** and **0.868 for `sugary_drinks`** —
the advertiser is told their beer poster contains a restaurant. It carefully distinguishes "no
eval images (not measured)" from "detects nothing despite having positives", because those are
opposite findings that must never share a line.

### 7.6 The eval corpus and the smoke test

`inference/eval/` holds **468 images across 20 categories**, each with `pos/` and `neg/`
subdirectories, built by `fetch_demo_eval.py` from Wikipedia lead images. Counts range from 8
positives (`vaping`, exactly on `MIN_PER_CLASS`) to 14 (`alcohol`). The directory is
**git-ignored** — tens of megabytes of regenerable photos do not belong in the history, and real
creatives dropped in to replace the demo set are ignored for a second reason: they are
advertiser material.

`fetch_demo_eval.py` (515 lines) is rate-limit-aware in a way worth preserving: asking for
`piprop=original` got throttled after ~25 files, so it requests an 800px thumbnail; at a 1s gap
the whole run degraded to roughly one image per six minutes, all of it spent in backoff. The 3s
gap costs about twenty minutes for a full fetch and does not trip the limiter at all.

`smoke_test.py` (189 lines) is the golden ML set: **17 cases across 14 images**, run by
`make inference-smoke`. It proves that extracting `banding.py` changed no ML behaviour. Notable
cases: `Wine.jpg` must be **uncertain** for `alcohol` (it scores 0.549 against a threshold of
0.55 — it misses by a thousandth), `Mount_Everest.jpg` must be absent for `alcohol`,
`gym_fitness` and `junk_food`, and `Weight_training.jpg` must be absent for `gambling` — the
cross-tag regression guard. **Any image that cannot be fetched exits 2**: a test that "passes"
because it silently tested nothing is worse than no test at all.

---

## 8. The verdict rule in full

`inference/banding.py` is 106 lines, imports nothing but `typing`, and is the **single source of
truth**. It used to be written out three times — in `detector.py`, in `calibrate.py`, and in the
TypeScript API tier — each copy carrying a "must stay identical" comment, and the copies
drifted. Do not copy it.

```python
Band       = Literal["present", "absent", "uncertain"]
DecidedBy  = Literal["siglip", "sigmoid_floor"]
Confidence = Literal["high", "medium", "low"]

def band_of(score, sigmoid, low, high, floor) -> tuple[Band, bool | None, DecidedBy]:
    if sigmoid < floor:
        return "absent", False, "sigmoid_floor"
    if score >= high:
        return "present", True, "siglip"
    if score <= low:
        return "absent", False, "siglip"
    return "uncertain", None, "siglip"
```

Four branches. Three things about them are load-bearing.

### The sigmoid floor is a veto, and it is checked first

Softmax must sum to 1, so an image containing nothing relevant can still hand a large share of
the probability mass to a positive prompt purely by beating equally-irrelevant options. The
softmax score answers a **relative** question — *does this look more like beer than like
anything else in the pool?* The sigmoid over the positive logits alone answers an **absolute**
one — *does this look like beer at all?*

Without the veto, **a photo of a mountain reads as alcohol**. Reordering these checks brings that
back.

**Keep the floor low.** Measured true positives run as low as **0.028** sigmoid (`alcohol`) while
true negatives sit at 0.000. It is a backstop, not a gate; raise it and you start vetoing real
detections. A floor of 0.10 would veto genuine ones.

### Both score comparisons are inclusive

`>= high` and `<= low`. A score sitting exactly on a threshold is **decided**, never uncertain.
`tests/test_banding.py` pins both boundaries explicitly.

### Do not add a second sigmoid check above the floor

This was tried and removed, and `band_of`'s docstring records why it cannot work. A "near-floor"
band downgraded a high score to `uncertain` when the sigmoid cleared the floor by less than 4×,
to stop a gym poster being refused as `gambling` at sigmoid **0.0104**. But a genuine
cheeseburger sits at **0.0108** for `junk_food` — four ten-thousandths away, on the opposite
side of the truth. The band did not separate them; it just moved the error from false positives
to false negatives, and a real burger reached a screen that blocks junk food.

The sigmoid's scale is **per-tag**, not global: a measured true positive is 0.1037 for
`sugary_drinks`, 0.0280 for `alcohol`, 0.0138 for `junk_food`. No global band fits all three, and
a per-tag one is just `sigmoid_floor`, which already exists and is already calibratable. The gym
poster's real defect was its **score**, and it was fixed where scores are made — the competition
pool in `detector.py`'s `_verdict`.

`tests/test_banding.py:111` exists specifically so nobody reintroduces this on the next false
positive.

### Confidence

```python
def confidence_of(score, low, high, band) -> Confidence:
    if band == "uncertain":
        return "low"                      # by definition it cleared neither threshold
    margin = score - high if band == "present" else low - score
    return "high" if margin >= 0.20 else "medium" if margin >= 0.08 else "low"
```

Confidence is *how far past the threshold did we land*, not a second opinion. An uncertain
verdict is always low confidence — there is no margin to measure.

---

## 9. Fingerprints

Four hashes, all 12 hex characters (48 bits), each answering a different question.

| Fingerprint | Covers | Computed as | Where |
|---|---|---|---|
| `packs_version` | The prompts **and** the scoring code | `sha256(packs.json bytes + b"\x00" + detector.py bytes)[:12]` | `inference/versioning.py:43` |
| `RULE_VERSION` | The verdict rule alone | `sha256(banding.py bytes)[:12]`, read **once at import** | `tagverify/tags/decide.py:45` |
| `SAMPLER_VERSION` | Which frames of a video get scored | `sha256(tagverify/analyze/video.py bytes)[:12]`, once at import | `tagverify/tags/decide.py:64` |
| `decision_version` | The rule, the sampler **and** every threshold | The fold in §5.9, seeded with `RULE_VERSION\|SAMPLER_VERSION` | `tagverify/tags/decide.py:257` |

The rule of thumb: **if a change moves a NUMBER it belongs in `packs_version`; if it changes how
that number is READ, it belongs in `decision_version`** (`AGENTS.md` rule 6).

### Why both halves are needed

`packs_version` tells a caller when the model's numbers changed. `decision_version` tells them
when the way those numbers are read changed. The gap was not academic: an integrator caching our
verdicts keyed on the pack alone kept serving a refusal we had already decided was wrong, and
never called back to find out — quietly undoing the retroactivity the raw-score cache exists to
provide.

`packs_version` covering `detector.py` and not just the prompts has the same origin: when
cross-tag competition was added to `_verdict()`, every score moved while a prompts-only
fingerprint sat still. The NUL separator between the two files cannot occur in either, so no
concatenation of one file's tail with the other's head can collide with a different pair.

`SAMPLER_VERSION` exists because for video, which frames get sampled decides which pixels are
ever scored. A sampler bug once judged an eight-second creative on its opening frame and passed
a blocked one; the fix could not reach that creative, because the wrong verdict was cached under
a `(packs_version, decision_version)` pair that a sampler change did not move. It lives in the
web tier rather than in `packs_version` because `packs_version` is stamped by the detector inside
the Space, which only ever sees a single still. The cost — bumping the sampler re-keys cached
*image* verdicts too — is wasteful but never wrong.

### Publishing them is not a leak

`decision_version` is a one-way hash, so it exposes no thresholds. It is published precisely so
a caller can key its own cache on the rule that produced a verdict instead of serving a
superseded one. `tests/test_decision_version.py` asserts no literal threshold value appears in
the digest.

### "Is my change live?"

`decision.rule` in `/api/v1/health` is computed from the bytes of the banding module this
process **loaded**, once, at import.

```bash
shasum -a 256 inference/banding.py | cut -c1-12
curl -s http://localhost:8000/api/v1/health | jq -r .decision.rule
```

If they differ, the server is stale and needs restarting. This check exists because a server
started without `--reload` once served a superseded rule for hours with nothing anywhere saying
so. Re-reading the file per request would be worse than useless — it would answer the question
with a confident yes about code the process is not executing.

---

## 10. Caching and retroactivity

### What is cached

**Raw scores, not verdicts.** Scores are the expensive part — a model forward pass — and never
change for the same image and the same prompts. Verdicts are a cheap threshold comparison,
expected to be retuned often. So the expensive half is cached and the cheap half is recomputed
on every read.

The consequence is **retroactivity**: change a threshold and every image already in the cache is
re-decided under the new one, immediately, with no re-inference and no cache invalidation.

### The key

`(image_hash, packs_version, sorted tags_requested)`.

- `image_hash` is the sha256 of the **submitted** bytes — hashed before any downscale, so two
  callers sending the same file always agree.
- Tags are sorted on both write and read, so tag order never fragments the cache.
- `packs_version` is in the key because a different pack produces different scores.
- **`decision_version` is deliberately NOT in the key.** Putting it there would defeat the whole
  design: a threshold change would miss the cache and re-run inference, rather than re-deciding
  what is already stored.

### What is stored

For a video, **every frame's raw rows**, not just the winning frame's. Which frame wins depends
on the thresholds, so a cached video re-read after a threshold change has to be re-aggregated
from all of them. Storing only the winner would silently freeze a video's verdict at the
thresholds of the day it was first analysed.

### The audit row

Every request writes one, **including a cache hit**. The `analyses` table therefore grows per
request, not per unique image, because the row is an audit record of a request being answered,
not a record of an inference being run. Writes happen as a FastAPI background task in their own
session, and failures are logged rather than swallowed.

The media itself is **never stored** — only its sha256, which is enough for dedupe, caching and
an audit trail. Video is decoded from an in-memory buffer and never touches disk either.

### The other caches

| Cache | TTL | Scope | Invalidation |
|---|---|---|---|
| Thresholds (`decide.py:244`) | 30 s | per process | explicit, on admin save |
| Inference health (`client.py`) | 60 s | per process | none |
| Tag catalog (`client.py`) | 60 s | per process | none |
| Playground IP hits (`deps.py:83`) | 60 s sliding | per process | none |
| Admin login attempts (`admin.py:127`) | 60 s sliding | per process | cleared on success |

All of these are per-process, so N uvicorn workers means N copies. That is acceptable because
the rate limiter's authority is the shared `usage_counters` table, and the caches are short TTLs
over data that changes a couple of times a week (`docs/DEPLOY.md:131-138`).

---

## 11. Invariants

The 12 rules from `AGENTS.md`, with where each is enforced and what pins it.

| # | Rule | Enforced at | Pinned by |
|---|---|---|---|
| 1 | **`present: null` means uncertain, not false.** Never flatten it. | `inference/banding.py:86`, `tags/decide.py:102-126` | `test_banding.py::test_uncertain_is_null_not_false` |
| 2 | **An unknown tag is an error.** Refuse the whole request. | `analyze/run.py:110-115`, `detector.py:280` | `test_api.py` (`results` absent from the 400 body) |
| 3 | **The sigmoid floor is checked BEFORE the score bands.** No second sigmoid check above it. | `inference/banding.py:80-81` | `test_banding.py::test_veto_is_checked_before_the_bands`, `::test_a_high_score_just_above_the_floor_is_present` |
| 4 | **`calibrated: false` must stay visible** in the API and the UI. | `tags/decide.py:174-177`, `api/v1/tags.py:58-65`, `partials/verdict_card.html` | `test_banding.py` stale-pack group (5 tests) |
| 5 | **Never store the media**, only its sha256 — video included. | `analyze/intake.py:401`, `analyze/video.py` (in-memory `BytesIO`) | `test_intake.py` (hash over original bytes) |
| 6 | **Every tag competes against every other tag's positives.** Two fingerprints follow. | `detector.py:212-219`, `versioning.py`, `tags/decide.py` | `smoke_test.py` (Weight_training vs gambling), `test_decision_version.py` |
| 7 | **Never expose thresholds** through `/api/v1/tags`. | `api/v1/tags.py` | `test_api.py:174` (no threshold field name in the response text) |
| 8 | **Unknown and revoked API keys return identical responses.** | `auth/deps.py:52-56`, `authenticate.py` (one `AuthFailed`) | `test_api.py::test_unknown_and_malformed_keys_are_indistinguishable` |
| 9 | **Admin defaults closed.** Every mutating handler re-checks the session. | `auth/admin.py:43-49`, `web/admin.py:37-43` | `test_admin.py` (3 endpoints × 403, 7 malformed cookies) |
| 10 | **Video frames are decided individually, then collapsed.** Never rank by raw score. | `analyze/aggregate.py:55-71` | `test_aggregate.py::test_a_vetoed_high_score_frame_never_wins` |
| 11 | **A frame that fails fails the request.** No partial video verdicts. | `analyze/run.py:197-207` | `test_video.py:248` (stops at frame 2 of 3, raises) |
| 12 | **The media kind is sniffed from the bytes**, never the field name, filename or Content-Type. | `analyze/video.py:86-95`, `intake.py:403` | `test_video.py::test_the_kind_comes_from_the_bytes_not_the_name` |

Two more that are not numbered in `AGENTS.md` but behave like invariants:

- **Colour means a verdict.** Red, amber and green are reserved for present / uncertain / absent;
  the chrome is monochrome plus one azure for links and focus rings. All values come from the
  token block in `app.css` — no literal hex in templates.
- **No build step, no Node.** Hand-authored CSS, Jinja2 templates, htmx vendored. There is no
  `package.json` and there should not be one.

---

## 12. Testing

**254 collected cases across 15 modules**, plus `conftest.py` and `tests/support/`. Tests
run against the ASGI app **in-process** via `httpx.ASGITransport`, so no server has to be started
and no port has to be free.

| File | Lines | Cases | Covers |
|---|---|---|---|
| `api/` | 558 | 31 | The public API end to end, one file per endpoint — `test_analyze.py` (stills and video; per-frame billing), `test_errors.py` (envelope, SSRF, unknown tag), `test_auth.py`, `test_tags.py`, `test_usage.py`, `test_health.py`. |
| `test_tag_coverage.py` | 287 | 65 | All 20 tags detect their own canonical content, as a still and inside a video. |
| `test_intake.py` | 235 | 45 | SSRF guard, tag normalisation, size limits, JSON shapes, form field discovery. |
| `test_banding.py` | 225 | 27 | **The decision layer.** See below. |
| `test_admin.py` | 208 | 25 | Cookie forgery, login throttling, CSRF, threshold and key validation. |
| `test_video.py` | 340 | 24 | Decoding, sampling, sniffing, limits, the partial-result guard. |
| `test_decision_version.py` | 167 | 19 | Fingerprint invariants. |
| `test_aggregate.py` | 191 | 10 | **Frame collapsing.** See below. |
| `test_templating.py` | 136 | 6 | Asset cache-busting; one verdict-card render branch. |
| `test_cli.py` | 76 | 2 | `apply-calibration` idempotence and the no-mid-transaction-rollback regression. |

### The two highest-value files

**`tests/test_banding.py`** — if you change how a verdict is decided, it should fail. If it
doesn't, the test is wrong. It pins: both threshold boundaries as inclusive; `uncertain` as
`null` with `confidence == "low"`; a 0.99 score with zero sigmoid vetoed to absent; a **0.028**
sigmoid *not* vetoed; the veto ordering; the removed near-floor band (0.0104 and 0.0108 both
present); the five stale-pack cases; and — at the source level, because importing `detector.py`
would pull in torch — that `detector.py` and `calibrate.py` import the shared rule and no longer
inline `sigmoid < floor`, that `banding.py` does not fingerprint itself, and that its imports are
a subset of `{"typing", "__future__"}`.

**`tests/test_aggregate.py`** — its counterpart for video. If you change how frames combine, it
should fail. The two cases that matter both produce a confident *wrong* answer rather than an
error, and both are silent in production: `test_uncertain_beats_absent_even_when_absent_scores_higher`
(uncertain at 0.31 must beat absent at 0.40) and `test_a_vetoed_high_score_frame_never_wins`
(a floor-vetoed 0.92 frame must lose to a genuine present 0.60). It also pins that evidence is
carried whole from one frame and that a single frame passes through untouched.

### What skips, and why

Skip markers are defined in `tests/conftest.py:49-54` and read `tagverify.config.settings()` — **not**
`os.environ`, because values come from `.env`/`.env.local`, which pydantic-settings loads but
never exports.

| Marker | Skips when | Used by |
|---|---|---|
| `needs_db` | `DATABASE_URL` is blank | `test_api.py` (23), `test_admin.py` (6), `test_tag_coverage.py` (7) |
| `needs_inference` | neither `HF_SPACE` nor `INFERENCE_URL` is set | `test_api.py` (15), `test_tag_coverage.py` (7) |

**Fully offline** (no DB, no model, no network): `test_banding.py`, `test_aggregate.py`,
`test_intake.py`, `test_video.py`, `test_templating.py`, `test_decision_version.py`,
`test_cli.py`. The first six are exactly what `make test-fast` runs.

One sharp edge: `inference/eval/` is git-ignored, and `conftest.py`'s `beer_image`, `juice_image`
and `beer_video` fixtures read from it. On a fresh checkout that has never run
`fetch_demo_eval.py` those fixtures **error** rather than skip, while `test_tag_coverage.py`'s
golden images call `pytest.skip` properly.

### Fixtures and helpers

- `client` — an in-process `httpx.AsyncClient` over the ASGI app.
- `api_key` — creates a real key, yields the plaintext, then **hard-DELETEs** the row. A
  revoking fixture leaves a row per run, and a few hundred dead `pytest` rows once buried the
  three keys that actually mattered. Safe because `analyses.api_key_id` is `ON DELETE SET NULL`.
- `beer_video` — beer in **one scene of three**. It is the case the whole video path exists for:
  a first-frame check would miss it entirely.
- `tests/support/videos.py` — an in-memory MP4 encoder (`av`'s wheels bundle ffmpeg, so `libx264` needs
  no system package). `middle_scene_clip()`, `unique_two_scene_clip()` (randomised colours, so
  the sha256 is new and the dedupe keeps both — random *noise* would not work, it averages to
  the same mid-grey on the comparison grid), and `truncated()` (decodable magic, undecodable
  body). It exists because the encoder had already been duplicated three times with slightly
  different sizes and GOP settings.

### Commands

```bash
make check              # ruff + the full pytest suite — run this before committing
make test-fast          # the decision rules alone: no DB, no model, no network
make inference-smoke    # 17 golden ML cases; only if you touched inference/
```

---

## 13. Build, run, deploy

### Local development

```bash
make install                  # .venv + the package in editable mode, with dev extras
cp .env.example .env          # then fill it in — see §14
make dev                      # http://127.0.0.1:8000
```

There is no asset build step. To run the model locally instead of against a deployed Space:

```bash
cd inference
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python app.py                    # serves on :7860
```

Then set `INFERENCE_URL=http://127.0.0.1:7860` and leave `HF_SPACE` unset. The first run
downloads ~400MB of SigLIP 2 weights; after that startup is a few seconds.

### Makefile targets

`ENV_FILE` is `$(firstword $(wildcard .env) $(wildcard .env.local))` — `.env` is the documented
name, `.env.local` is what this checkout already had, so both work. `PORT` defaults to 8000.

| Target | Runs |
|---|---|
| `install` | `python3 -m venv .venv` then `.venv/bin/pip install -e ".[dev]"` |
| `dev` | `.venv/bin/uvicorn tagverify.main:app --reload --env-file $(ENV_FILE) --port $(PORT)` |
| `serve-lan` | the same, plus `--host 0.0.0.0`, behind a banner printing the reachable address |
| `test` | `.venv/bin/python -m pytest` |
| `test-fast` | pytest over `test_banding`, `test_intake`, `test_aggregate`, `test_video`, `test_templating`, `test_decision_version` |
| `lint` | `ruff check tagverify/ tests/ inference/banding.py` |
| `fmt` | `ruff check --fix` then `ruff format`, same paths |
| `check` | `lint test` |
| `inference-smoke` | `cd inference && ./.venv/bin/python smoke_test.py` |
| `clean` | removes `__pycache__`, `.pytest_cache`, `.ruff_cache` |

**`--reload` is required in dev, and not for the usual reason.** Without it the process goes
stale *unevenly*: Jinja re-reads templates from disk per render and `StaticFiles` ignores the
`?v=` query, so the page keeps updating while the Python stays frozen at startup. The result
looks live and is not (`Makefile:37-42`).

### Serving on a LAN

`make dev` binds loopback only. To reach the app from a phone or a test screen:

```bash
make serve-lan                                  # binds 0.0.0.0, prints the address
make serve-lan LAN_IP=192.168.1.24 PORT=8000    # pin the address when the guess is wrong
```

Two warnings apply, both in `README.md:209-218`: **do not add `--proxy-headers`** for direct LAN
access (it lets any device on the network spoof `X-Forwarded-For` past the per-IP playground
limit), and **`/admin` is reachable too**, with the password crossing the network in the clear
over plain HTTP. Fine on a trusted office LAN; not on shared or public Wi-Fi.

`README.md:220-247` has a full diagnostic for LAN connection failures — read the failure mode
first (timeout vs connection refused vs SSL error), check the default gateway on both machines
before blaming the host firewall, and suspect **AP / client isolation** on the router, which is
on by default for a lot of ISP routers and always on for guest SSIDs.

### `pyproject.toml`

| | |
|---|---|
| Name / version | `dooh-tag-verification` 1.0.0 |
| Python | `>=3.12` |
| Entry point | `dooh = "tagverify.cli:app"` |

| Dependency | Constraint | Role |
|---|---|---|
| `fastapi` | `>=0.115` | the app |
| `uvicorn[standard]` | `>=0.32` | the server |
| `jinja2` | `>=3.1` | templates |
| `python-multipart` | `>=0.0.12` | multipart parsing |
| `sqlalchemy[asyncio]` | `>=2.0.36` | ORM / core |
| `psycopg[binary,pool]` | `>=3.2` | Postgres driver (wheels, so no `libpq-dev`) |
| `pydantic` | `>=2.10` | response validation |
| `pydantic-settings` | `>=2.7` | config |
| `gradio-client` | `>=1.5` | talking to the Space |
| `httpx` | `>=0.28` | URL fetches, and the test client |
| `pillow` | `>=10.0` | image decode / downscale |
| `av` | `>=13` | video codec binding (~18MB, ffmpeg bundled) |
| `typer` | `>=0.15` | the CLI |

Dev extras: `pytest>=8.3`, `pytest-asyncio>=0.25`, `ruff>=0.8`, `mypy>=1.14`.

Tooling: ruff `line-length = 100`, `target-version = "py312"`, `select = ["E","F","I","UP","B","SIM"]`,
`ignore = ["B008"]` (FastAPI's `Depends()`/`File()` defaults are the documented idiom). pytest
`asyncio_mode = "auto"`. mypy has a `make typecheck` target but is deliberately **not** part of
`check`, which stays `lint test` until mypy is clean — a red `check` that everyone ignores is
worse than no target.

Packages are **discovered**, not hand-listed: `[tool.setuptools.packages.find]` with
`include = ["tagverify*", "inference*"]`. The list used to be written out by hand, which is a
silent-failure shape — `tagverify/` sits at the repo root and so is importable from the working
tree whether or not it is declared, meaning a forgotten entry ships a broken wheel with a fully
green test suite. `namespaces = false` is load-bearing: the default is `true`, and
`inference/eval/` and `inference/.testimages/` would otherwise be picked up as PEP-420 namespace
packages.

`inference` is discovered alongside `tagverify` because the web app imports the shared banding
rule from it. That same directory is git-subtree-pushed to the Space, where it runs flat.

### Dockerfile

Two stages, both `python:3.12-slim`. **The web application only.**

Stage 1 copies `pyproject.toml`, `README.md`, `tagverify/`, and — critically — **only three files from
the model tier**:

```dockerfile
COPY inference/__init__.py inference/banding.py inference/versioning.py ./inference/
```

Never `detector.py`, never `requirements.txt`. `versioning.py` is there because `cli.py`
imports it at module scope, and without it the `dooh` console script could not start inside
the image. All three import nothing but stdlib, so they cost
nothing; copying the rest would drag ~2GB of torch into an image whose job is serving HTTP.

Stage 2 creates an unprivileged user (`useradd --uid 10001 dooh`), copies the venv and the app,
switches to that user, exposes **8000**, and runs:

```dockerfile
CMD ["uvicorn", "tagverify.main:app", "--host", "0.0.0.0", "--port", "8000",
     "--proxy-headers", "--forwarded-allow-ips", "*"]
```

`--proxy-headers` is **not optional behind a TLS terminator**: `request.url.scheme` is where the
admin cookie's `Secure` flag comes from, and without it cookies behind a proxy would be issued
without it. Set `--forwarded-allow-ips` to your actual proxy address rather than leaving the
shipped `*`.

The app writes nothing to disk — media is held in memory and only hashes are persisted — so a
read-only filesystem works.

### Deploying the Space

```bash
huggingface-cli login    # a WRITE token, to push; the app itself only ever reads
huggingface-cli repo create dooh-tag-check --type space --space_sdk gradio --private
git remote add space https://huggingface.co/spaces/<you>/dooh-tag-check
git subtree push --prefix=inference space main
```

**Do not `git init` inside `inference/`.** It is tracked by the parent repo, and a nested `.git`
makes the parent record it as a gitlink — the directory would arrive on GitHub empty.

`inference/README.md`'s YAML front-matter (`sdk: gradio`, `app_file: app.py`) is what tells HF to
install `requirements.txt` and run `app.py`. **Do not delete it** — without it the Space will not
build. The first build downloads ~400MB of weights; it is ready when the Logs tab shows
`[detector] ready: 20 tags`. Make the Space private.

### Keepalive

Free Spaces sleep after **48 hours** of inactivity, and waking one re-downloads the weights
(30–60s). `.github/workflows/keepalive.yml` pings every six hours:

```yaml
on:
  schedule: [{ cron: "0 */6 * * *" }]
  workflow_dispatch:
```

```bash
curl -sS -m 120 -X POST \
  -H "Authorization: Bearer ${{ secrets.HF_TOKEN }}" \
  -H "Content-Type: application/json" \
  -d '{"data":[]}' \
  "https://${{ vars.HF_SPACE_HOST }}/gradio_api/call/health"
```

It is **inert until two repo settings exist**: secret `HF_TOKEN` (read access) and variable
`HF_SPACE_HOST`. Note the host is the Space's **subdomain** form — owner and name joined by a
**hyphen** (`you-dooh-tag-check.hf.space`) — not the `owner/name` slug that `HF_SPACE` takes.
Trigger it once by hand to confirm.

If it silently stops, the first request after 48h eats the cold start: `/api/v1/analyze` returns
503 `INFERENCE_WARMING` with `retry_after` rather than hanging. It degrades honestly — but it
does degrade.

### Hosting and regions

Vercel is gone: it cannot host a long-lived Python process, and the connection pool and
in-process caches are exactly what make the rewrite fast. Any container host works.

The previous deployment used Neon's HTTP driver, where every query was a separate HTTPS request.
Measured against `us-east-2` from South Asia: **562–2435 ms per query**, and an uncached
`/analyze` took ~1800 ms. Over the normal Postgres wire protocol a query is no longer a network
event and the multiplier is gone — cross-region now costs one RTT on connection setup, a
nuisance rather than the dominant cost. Still, **keep the app in the same region as Neon**; for
India traffic, Mumbai or Singapore beats Ohio.

Regions cannot be changed in place: create a new project, run `docs/schema.sql`, then
`dooh seed-thresholds`. Cheap to do while the only data is 20 threshold rows.

### Provisioning and first key

```bash
psql "$DATABASE_URL" -f docs/schema.sql   # 5 tables, 7 indexes, all IF NOT EXISTS
dooh seed-thresholds                      # load the 20 tag thresholds from packs.json
dooh create-key "Client name" --rate-limit 60
```

The existing database needs no migration — the Python models map onto the same tables the
previous implementation created, column for column.

> ⚠️ **Credential rotation.** `docs/DEPLOY.md:14-18` flags that the Neon connection string in
> `.env.local` was shared in a chat transcript and has not been rotated. Reset it (Neon
> dashboard → Roles → reset password) and update `DATABASE_URL` everywhere before this handles
> anything real.

### Verifying a deploy

```bash
curl -s https://<your-app>/api/v1/health | jq .
```

`degraded` with `inference.warming: true` immediately after a deploy is normal. Then run
`pytest` against the real database and Space, and `make inference-smoke` if `inference/` changed.

---

## 14. Configuration reference

Read once at import by `tagverify/config.py`, from the environment and from `.env` (and `.env.local`).
**Nothing raises on a missing value.**

| Variable | Required | Default | What it does | If unset |
|---|---|---|---|---|
| `DATABASE_URL` | **yes** | — | Postgres. Use the **pooled** Neon string (host containing `-pooler`), in the same region as the app. | App boots; `/api/v1/health` reports `database.ok: false`; every DB-backed route fails. |
| `HF_SPACE` | one of these two | — | The inference Space as `owner/space-name`. **Takes precedence over `INFERENCE_URL`.** | Falls through to `INFERENCE_URL`. |
| `INFERENCE_URL` | | — | Local-dev escape hatch: a direct URL to a running `inference/app.py`. Leave unset in production. | — |
| `HF_TOKEN` | for a private Space | — | Needs **read** access only. Write access is for pushing the Space, not running it. | Passed only when non-empty; a private Space will refuse. |
| `ADMIN_PASSWORD` | yes, in practice | — | Gates `/admin`. | **`/admin` refuses, never opens.** The UI arrives dead rather than open. |
| `LOG_LEVEL` | no | `INFO` | Passed to `logging.basicConfig`. | — |
| `PLAYGROUND_RATE_LIMIT_PER_MIN` | no | `20` | Per-IP sliding limit; the playground carries no API key by design. | — |
| `ADMIN_LOGIN_ATTEMPTS_PER_MIN` | no | `5` | Per-IP sliding limit on failed logins. | — |

Repo settings for GitHub Actions (not application config): secret `HF_TOKEN`, variable
`HF_SPACE_HOST`.

Backend-side (in `dooh-backend`, not here): `PROFILE_API_URL`, `PROFILE_API_KEY`.

---

## 15. Constants reference

| Constant | Value | Where |
|---|---|---|
| `MAX_IMAGE_BYTES` | 10 MB | `tagverify/analyze/intake.py:52` |
| `MAX_EDGE` | 768 px | `tagverify/analyze/intake.py:58` |
| `JPEG_QUALITY` | 90 | `tagverify/analyze/intake.py:59` |
| `MAX_VIDEO_BYTES` | 50 MB | `tagverify/analyze/video.py:44` |
| `MAX_DURATION_S` | 60.0 s | `tagverify/analyze/video.py:48` |
| `MAX_FRAMES` | 6 | `tagverify/analyze/video.py:57` |
| `MIN_FRAME_GAP_S` | 0.35 s | `tagverify/analyze/video.py:60` |
| `SIGNATURE_DISTANCE` | 8.0 (mean abs diff, 0–255) | `tagverify/analyze/video.py:65` |
| `SIGNATURE_GRID` | 8 (→ 192 values) | `tagverify/analyze/video.py:68` |
| `TOTAL_BUDGET_S` | 30.0 s | `tagverify/analyze/run.py:50` |
| `_PRECEDENCE` | `{absent:0, uncertain:1, present:2}` | `tagverify/analyze/aggregate.py:55` |
| `CALL_TIMEOUT_S` | 8.0 s | `tagverify/scoring/client.py:109` |
| `CONNECT_TIMEOUT_S` | 6.0 s | `tagverify/scoring/client.py:110` |
| Health / catalog memo | 60.0 s | `tagverify/scoring/client.py:233` |
| Threshold memo | 30.0 s | `tagverify/tags/decide.py:244` |
| `FALLBACK` thresholds | low 0.3 / high 0.55 / floor 0.005 | `tagverify/tags/decide.py:143` |
| Fingerprint length | 12 hex chars (48 bits) | `decide.py:45,64,318`; `inference/versioning.py:40` |
| Confidence margins | high ≥ 0.20, medium ≥ 0.08 | `inference/banding.py:104-106` |
| `KEY_PREFIX` | `dooh_live_` | `tagverify/auth/keys.py:33` |
| `SECRET_BYTES` / `DISPLAY_CHARS` | 32 / 8 | `tagverify/auth/keys.py:34-35` |
| Admin cookie name / max age | `dooh_admin` / 7 days | `tagverify/auth/admin.py:28-29` |
| Default key rate limit | 60 / min (1–10000) | `tagverify/db/models.py`, `tagverify/cli.py` |
| Rate-limit window | 60 s fixed (API), 60 s sliding (playground, admin login) | `authenticate.py`, `deps.py`, `admin.py` |
| DB pool | `pool_size=5, max_overflow=5, pool_recycle=300, pool_pre_ping=True` | `tagverify/db/session.py:62` |
| `MODEL_ID` | `google/siglip2-base-patch16-224` | `inference/detector.py:67` |
| `MAX_TEXT_LEN` | 64 | `inference/detector.py:72` |
| Crop grid | 3 → 10 crops (full image + 3×3 at size 0.5, step 0.25) | `inference/detector.py:107` |
| Space limits | 12 MB image, 30 tags, port 7860, queue 32 | `inference/app.py:40-41,232` |
| `MIN_PER_CLASS` | 8 | `inference/calibrate.py:71` |
| `MAX_ESCALATION_RATE` | 0.35 | `inference/calibrate.py:80` |
| Default target precision | 0.90 | `inference/calibrate.py` |
| Keepalive cron | `0 */6 * * *` | `.github/workflows/keepalive.yml` |
| Container port | 8000 | `Dockerfile` |
| Tag catalog size | 20 | `inference/packs.json` |
| Unique prompts | 266 (123 pos + 139 neg − 1 overlap + 6 distractors) | `inference/packs.json` |
| Eval corpus | 468 images, 20 categories | `inference/eval/` |
| Test cases | 254 across 10 modules | `tests/` |

---

## 16. Known drift

Accurate as of this writing. None of these are bugs in the running system, but all of them will
mislead someone reading the repo.

| # | Drift | Where | Why it matters |
|---|---|---|---|
| 1 | ~~The Python rewrite is staged but not committed.~~ **Resolved.** Committed as `e5b5365`, with the Next.js `app/` tree removed in the same commit. | — | — |
| 2 | ~~`dooh_tag_verification.egg-info/` is stale.~~ **Resolved.** Regenerated during the `dooh` → `tagverify` rename: `alembic` is gone, `av>=13` is present, and `SOURCES.txt` covers the video work. It stays git-ignored. | — | — |
| 3 | `inference/README.md`'s sample response shows `"packs_version": "1a2db18ba940"`. The current value is **`c14ac5bdcfa9`**. | `inference/README.md` | Someone comparing a live response against the docs will think the pack is wrong. |
| 4 | `detect_objects` is present on 19 of 20 tags in `packs.json` and is **never read** by `detector.py`, `app.py`, `calibrate.py` or `sweep.py`. | `inference/packs.json` | Dead metadata that reads as a feature. Note it is inside the hashed bytes, so editing it moves `packs_version` for no behavioural reason. |
| 5 | `detector.py`'s docstring says "~250 prompts"; the actual unique row count is **266**. | `inference/detector.py:150-160` | Minor, but the number appears in `/api/v1/health` where it can be checked. |
| 6 | Comments in `inference/app.py:6` and `tagverify/scoring/client.py` still refer to "the Next.js API tier". | those files | `AGENTS.md:7-8` says any such reference is stale and should be fixed. |
| 7 | `calibration.json`'s `held_back` block and the 12 rewritten `reason` strings are **hand-authored**, not emitted by `calibrate.py`. Nothing in `tagverify/cli.py` reads `held_back` — it filters on `calibrated` alone. | `inference/calibration.json` | Re-running `calibrate.py` overwrites the file and silently loses that reasoning. Copy it out before recalibrating. |
| 8 | `ADMIN_LOGIN_ATTEMPTS_PER_MIN` is documented in `docs/DEPLOY.md` but is **missing from `.env.example`**. | `.env.example` | An operator copying the example never learns the knob exists. |
| 9 | ⚠️ The Neon connection string in `.env.local` **was shared in a chat transcript and has not been rotated**. | `docs/DEPLOY.md:14-18` | The one item on this list that is a live security issue. Rotate before this handles anything real. |
| 10 | mypy has a `make typecheck` target but is still **not** part of `make check`, deliberately — it is not clean yet, and a red `check` that everyone learns to ignore is worse than no target. | `Makefile` | Type errors are surfaced on demand but not enforced. Fold it into `check` once it passes. |

---

## Where to go next

| Question | Read |
|---|---|
| How do I run this? | `README.md` — Quick start |
| What must I not break? | `AGENTS.md` — "Rules that are load-bearing" |
| How do I deploy it? | `docs/DEPLOY.md` |
| What does the database look like? | `docs/schema.sql` |
| How is a verdict decided? | `inference/banding.py` — read the docstrings, they are the design record |
| What does the API return? | `/docs` in the running app, or §5.4 above |
