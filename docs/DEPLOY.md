# Deploying

Three pieces, deployed independently:

| what | where | how |
|---|---|---|
| the whole repo | GitHub — [`kuljeet-singhh/Dooh-TagVerefication`](https://github.com/kuljeet-singhh/Dooh-TagVerefication) | `git push origin main` |
| the web app | a host that **stays running** — see §1 | `pip install .` + `uvicorn` |
| database | Neon Postgres | already provisioned |

There used to be a fourth: a Hugging Face Space running a SigLIP 2 detector, pushed with `git
subtree`, kept awake by a cron, and fed a phrase pack by a publish step. All of it is gone. The
model reads its prompt from Postgres on every request, so there is no second tier to deploy, no
pack to push, and **no publish step to forget**. That last one was this service's worst failure
mode and it is now structurally impossible; §5 explains what it looked like, because it is worth
knowing what the current shape buys you.

---

## ⚠️ Rotate two credentials first

1. **The Neon connection string** in `.env.local`. It was exposed the same way — shared in a chat
   transcript — and has **not** been rotated. Reset it (Neon dashboard → Roles → reset password)
   and update `DATABASE_URL` everywhere.
2. **The VLM provider API key.** This is the credential that costs money if it leaks: it bills
   per request against a commercial tier, and nothing in this app caps spend. Issue a key
   scoped to this deployment rather than reusing a personal one, and treat a leak as an
   immediate rotation rather than a monitored one.

Do both before this handles anything real.

---

## Region still matters, but far less than it used to

The first deployment used Neon's HTTP driver, where every query is a separate HTTPS request, so
latency was `number of queries × round-trip time`. Measured against `us-east-2` from South Asia
that was 562–2435 ms **per query**, and `/analyze` uncached took ~1800 ms.

The Python app uses a pooled connection over the normal Postgres wire protocol, so a query is no
longer a network event and the multiplier is gone. Cross-region still costs one RTT on connection
setup and adds latency per round trip, so keep the app near Neon — but a mismatch is now a
nuisance rather than the dominant cost.

**The model call has since become the dominant cost anyway.** An uncached `/analyze` waits on a
provider round trip measured in seconds, against database queries measured in milliseconds. That
does not make the database region free, but it does mean the first thing to measure when
`/analyze` is slow is the provider, not Neon.

Regions cannot be changed in place: create a new project and re-run the schema. Cheap to do now,
while the only data is the tag catalog and the keys.

`dooh-backend` calls this service **synchronously** — an advertiser's upload waits on the
verdict — so it wants to be close too. Its own timeouts are sized above this service's model
timeouts on purpose, so the budget is generous; the latency a user feels is still the sum of
every hop. App, database and backend in one region.

---

## 1. The web app

Vercel is gone: it cannot host a long-lived Python process, and the connection pool and
in-process caches are what make this fast.

**There is no container, and none is needed.** Nothing in this service shells out — no
`subprocess`, no ffmpeg binary — and every native dependency arrives bundled in its wheel: `av`
carries ffmpeg statically, `psycopg[binary]` carries libpq, Pillow is prebuilt. So there are no
apt packages, no build tools and no image to build. The whole install is:

```bash
python3 -m venv /opt/tagverify/venv
/opt/tagverify/venv/bin/pip install .
/opt/tagverify/venv/bin/uvicorn tagverify.main:app --host 0.0.0.0 --port 8000
```

**Python 3.12 or newer** (`pyproject.toml`). Prefer whatever version you run the tests on — a
host Python that differs from the development one is a difference nothing has checked.

### Behind a reverse proxy — read this before adding `--proxy-headers`

Add it **only** when something really does terminate TLS in front of this process, and always
name that proxy:

```bash
uvicorn tagverify.main:app --host 127.0.0.1 --port 8000 \
  --proxy-headers --forwarded-allow-ips '<the proxy's IP>'
```

**Never `--forwarded-allow-ips '*'`.** That tells uvicorn to trust `X-Forwarded-For` from any
client, and `request.client` is what the playground's per-IP rate limit and the admin login
lockout are both keyed on. With `*`, a caller picks their own IP by setting a header: the limit
that stands between a public URL and unlimited model spend on your key stops working, and so
does the only throttle on guessing the admin password.

Without a proxy, leave both flags off. The trade is that the admin session cookie takes its
`Secure` flag from the request scheme, so over plain HTTP it is issued without `Secure` — which
is correct behaviour, but it means the password crosses the network in the clear. Do not put
`/admin` on the open internet without TLS.

### Keeping it running

A `uvicorn` you started by hand dies with your shell and does not come back after a reboot. On a
VM, systemd is the smallest thing that fixes both:

```ini
# /etc/systemd/system/tagverify.service
[Unit]
Description=DOOH Tag Verification
After=network-online.target

[Service]
User=tagverify
WorkingDirectory=/opt/tagverify/app
EnvironmentFile=/opt/tagverify/app/.env
ExecStart=/opt/tagverify/venv/bin/uvicorn tagverify.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now tagverify`. Run it as a dedicated non-root user; the service needs no
privileged port and writes nothing it owns. On a PaaS, the platform's own process supervisor
does this job and the `ExecStart` line is the only part you need.

A plain VM, Fly.io, Render or Railway all work — anything that keeps a process alive.
2 vCPU / 4GB, in the same region as Neon and `dooh-backend`. No GPU: there is no local model any
more, and no persistent disk requirement either, because **nothing is written to disk at all** —
creatives are held in memory and only their hashes are stored.

### On serverless

An earlier version of this file ruled serverless out as *structurally* impossible, because
publishing swapped an in-memory prompt table and anything that scales to zero would throw it
away. **That argument no longer applies** — there is no in-memory catalog and no publish. Being
honest about it matters, because the weaker reasons are easy to weigh and the old one was not:

- **Cold starts land on the user.** A scale-to-zero instance pays connection setup and app
  import on the first request, on top of a model call already measured in seconds — and an
  advertiser is watching an upload spinner for the whole of it.
- **The connection pool stops being a pool.** Per-request instances open and discard connections,
  which is exactly the pattern the wire-protocol switch was meant to escape.
- **The daily prune never fires.** `usage_counters` is swept by a background task started in
  `lifespan`; a process that exits between requests never reaches it, and the table grows.

None of that is fatal, and a scale-to-zero deployment would work. It would just be slower and
untidier than a box that stays up, for no saving worth having at this size.

### Environment

| variable | value |
|---|---|
| `DATABASE_URL` | the Neon **pooled** connection string (the host containing `-pooler`) |
| `ADMIN_PASSWORD` | a real password — not the one in your local `.env` |
| `SCORER` | `vlm`. The default, so it can be left unset; set it explicitly anyway, because the alternative is `fake` and a deployment that silently answers from a fixture is the one mistake here that looks like it is working. |
| `VLM_PROVIDER` | `anthropic` or `gemini` |
| `VLM_MODEL` | the model id, set **together** with the provider |
| `ANTHROPIC_API_KEY` *or* `GEMINI_API_KEY` | whichever the provider needs |

`VLM_MODEL` is deliberately not defaulted per provider. A model id that does not match its
provider is the kind of mistake that shows up as a bill rather than as an error, so both are
stated or neither is trusted.

`ADMIN_PASSWORD` is easy to forget, because nothing in the public API needs it — but `/admin`
refuses every request when it is unset. The admin UI arrives dead rather than open, which is the
right failure and a confusing one if you were not expecting it.

**Never deploy `SCORER=fake`.** It answers `present: null` for everything without contacting a
model. Every verdict becomes "needs a human", nothing is ever blocked, and the service looks
healthy the whole time.

Optional: `LOG_LEVEL` (default `INFO`), `PLAYGROUND_RATE_LIMIT_PER_MIN` (default 20) and
`ADMIN_LOGIN_ATTEMPTS_PER_MIN` (default 5). The playground carries no API key by design, so it is
limited per IP — if the deployment is public, that limit is what stops it being free inference,
billed to you, for anyone who finds the URL.

### Scaling

The rate limiter and the health cache are per-process, so with N workers each gets its own. That
is fine: the rate limiter's authority is the `usage_counters` table, which is shared, and the
caches are short TTLs over data that changes rarely. Run one worker per core with `--workers N`.

## 2. Database

For a fresh database:

```bash
psql "$DATABASE_URL" -f docs/schema.sql   # tables and indexes
```

That is the whole of it. There is nothing to seed — the tag catalog is created through `/admin`
or the JSON API, and a saved tag is live at the next request.

For an **existing** database, bring it forward:

```bash
make migrate            # alembic upgrade head
make migrate-sql        # the same, printed as SQL instead of applied — a dry run
```

The schema file and the migrations are not alternatives: the first describes the destination and
provisions a new database, the second describes how a database that already holds data gets
there. **A new column belongs in both**, or provisioning and migrating diverge. Alembic takes
`DATABASE_URL` from the app's own settings, so the credential never lands in a tracked file.

Read `migrate-sql` output before a production run. A migration you have not read is a migration
you are trusting blind — and two of the four below drop columns.

| revision | what it does |
|---|---|
| `0001_api_key_scopes` | Adds `api_keys.scopes` (JSONB, default `[]`) — the `tags:write` scope lives here. A migration rather than a schema rewrite because `api_keys` holds live credentials and cannot be recreated. |
| `0002_pack_header` | Adds `pack_header`, a singleton row holding the non-tag half of the old published pack. **Vestigial** — nothing reads or writes it now. Kept because dropping a table earns nothing. |
| `0003_drop_phrase_columns` | Drops `content_tags.positives`, `.negatives`, `.rationale`, `.sigmoid_floor`. These were the ranking model's question and had been read by nothing for some time. No fingerprint covers them, so **no cached verdict is re-keyed**. Archived to a JSON file in `inference/` first. |
| `0004_drop_decision_thresholds` | Drops `tag_thresholds.threshold_low`, `.threshold_high`, `.sigmoid_floor`, `.escalate`. Not a tidy-up: the admin form over them folded into `decision_version`, so saving a number nothing read re-keyed every cached verdict here and in `dooh-backend`, and the same write set `calibrated = false` with nothing left able to set it back. |

API keys are `sha256(plaintext)` and always have been, so **every key already issued keeps
working** across all four. Keys predating `0001` get an empty scope list, which is analyze-only,
which is what they already were.

## 3. Issue a key

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
a busy integration. Without `--tags-write` a key is analyze-only and every catalog write is
refused with an error naming the scope it lacks.

---

## 4. Verifying a deployment

```bash
# unauthenticated, safe to hit from anywhere
curl https://<your-app>/api/v1/health

# the full suite, against a real database
make check
```

`/api/v1/health` always returns 200 with the status in the body, so any non-200 means the app
itself is down rather than something a monitor has to parse degradation out of. Check four fields
in it, not just the status:

| field | what it tells you |
|---|---|
| `inference.scorer` | which scorer and model are in the request path, as `vlm:gemini:gemini-3.7-flash`. **If this says `fake`, the deployment is answering from a fixture.** |
| `inference.ok` | whether the active scorer is configured and its catalog is readable. `false` with an error naming a missing API key is the usual first-deploy failure. |
| `inference.tags` | how many active tags the model is being asked about. It comes from the same database read that builds the prompt, so it cannot disagree with what is actually being scored. |
| `decision.rule` | a hash of the decision rule this process loaded, at import. If it does not change after you deploy a change to that rule, you are looking at a stale process. |

`inference.warm` is always `true`, and that is a statement rather than a stub: nothing is loaded
at startup, so there is no cold state to be warm or cold about.

**Do not treat `inference.packs_version` as a drift alarm.** It used to be one — it fingerprinted
a pack pushed to a separate machine, so it moving on its own meant the model had reverted. It is
now computed from the database on every call, over the model id, the prompt and every active
tag's name. It moves when someone renames a tag or the model id changes, and it cannot move for
any other reason. Consumers still need it: it is half of the cache key on a verdict, so a caller
that caches decided verdicts keys on it and on `decision.version` together.

Note that this endpoint does **not** call the provider. There is no process of ours to probe, and
billing a request on every monitoring hit would buy nothing the next real request does not find
out anyway. So a green health check means *configured and readable*, not *the provider is up* —
that shows as a `502 INFERENCE_FAILED` on the first real analyze, not here.

## 5. What the current shape removed

Worth knowing, because it is the reason this file is half the length it was.

The model used to run on a free Hugging Face Space and score against a phrase pack pushed to it.
Those Spaces sleep after 48 hours, and a slept Space **rebuilds from its own git repo on wake** —
so the pack reverted to whatever was last pushed, and every tag created through the admin UI
since then vanished from the model.

End to end: the slug was missing from the catalog, so `dooh-backend` produced no verdict for it,
saw fewer verdicts than blocked tags, and fell through to `NOT_VERIFIED` — a **flag, not a
block**. The upload proceeded. Screens quietly stopped enforcing the categories their owners had
chosen, and nothing surfaced it: no error, no toast, no log anyone was reading. For a compliance
tool that is the worst available failure.

It required a keepalive cron to make unlikely, a republish after every restart to recover from,
and a monitor watching the tag count to notice at all. None of those exist now, and none is
needed: the prompt is read from Postgres per request, so there is no copy of the catalog anywhere
that can be older than the database.

## 6. Local development

Installing and provisioning are in the README. Once that is done:

```bash
make dev              # http://127.0.0.1:8000
```

One process, one terminal. Then confirm the database and the scorer are both reachable without
starting a server:

```bash
dooh health
```

There is no asset build step. The CSS is hand-authored and htmx is vendored, so editing a
template or a stylesheet just needs a browser reload — but note that `--reload` watches `*.py`
only, so a Python change restarts the server and a template change does not need to.

Set `SCORER=fake` locally when you are working on something that is not the scorer. It costs
nothing, needs no key and never leaves the machine. Just never let it reach a deployment.
