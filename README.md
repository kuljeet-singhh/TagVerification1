# DOOH Tag Verification

**Does this ad creative actually contain the content it is tagged with?**

In digital out-of-home ad ops a creative arrives tagged `alcohol`, `gambling`, `vaping` and so
on, and those tags drive placement rules — you cannot put alcohol on a screen outside a school.
This service checks the tags against the pixels — for stills, and for video, where it samples
the scenes and tells you at which second the content appears.

It is a compliance tool, and that shapes every design decision in it. Most importantly: a
verdict can be **`present: null`**, meaning *uncertain — a human has to look*. That is not the
same as absent, and the API, the UI and the docs all go out of their way to keep the two apart.

---

## Contents

- [How it fits together](#how-it-fits-together)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Commands](#commands)
- [Testing](#testing)
- [The API](#the-api)
- [Serving on a LAN](#serving-on-a-lan)
- [Things worth knowing before you change anything](#things-worth-knowing-before-you-change-anything)

---

## How it fits together

```
                  browser (playground)          integrator (API key)
                        │                              │
                        └───────────┬──────────────────┘
                                    ▼
                       dooh/analyze/run.py            ← one shared pipeline
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

## Repository layout

| Path | What it is |
|---|---|
| `dooh/` | The web application: FastAPI, Jinja2 templates, HTMX. One process serves the API, the playground, the docs and the admin. |
| `inference/` | The SigLIP 2 detector, deployed separately to a Hugging Face Space. Has its own `requirements.txt` and virtualenv on purpose — the web app must never depend on torch. |
| `inference/banding.py` | The rule that turns a score into a verdict. Imported by the detector, by the calibration sweep, and by the API. **The single source of truth — do not copy it.** |
| `dooh/analyze/video.py` | Decoding a video and choosing which frames are worth scoring. Keyframes, then a colour-aware dedupe. |
| `dooh/analyze/aggregate.py` | The rule that collapses per-frame verdicts into one per tag. Pure, like `banding.py`, and tested the same way. |
| `tests/` | pytest. Runs against the ASGI app in-process; tests needing the database or the model skip cleanly when those are not configured. |
| `docs/DEPLOY.md` | Deployment, regions, keepalive, credential rotation. |
| `docs/schema.sql` | The database schema, for reference and for provisioning by hand. |

**Two virtualenvs, deliberately.** `.venv` at the repo root serves HTTP; `inference/.venv` runs
the model. Never add `torch`, `transformers` or `gradio` to `pyproject.toml`, and never import
`inference.detector` from `dooh/` — the split is what keeps a web deploy from pulling 2GB of ML
wheels. The one shared file is `inference/banding.py`, which imports nothing.

## Quick start

```bash
make install                  # .venv + the package in editable mode, with dev extras
cp .env.example .env          # then fill it in — see Configuration below
make dev                      # http://127.0.0.1:8000
```

There is no asset build step. The CSS is hand-authored, htmx is vendored, and there is no
`package.json` — nothing here needs Node.

To run the model locally instead of against a deployed Space:

```bash
cd inference
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python app.py                    # serves on :7860
```

Then set `INFERENCE_URL=http://127.0.0.1:7860` and leave `HF_SPACE` unset. The first run
downloads ~400MB of SigLIP 2 weights; after that startup is a few seconds.

## Configuration

Read once at import by `dooh/config.py`, from the environment and from `.env` (`.env.local` is
also read, so an existing checkout keeps working). **Nothing raises on a missing value** — a
missing `DATABASE_URL` degrades to a `/api/v1/health` response that reports the problem, rather
than a process that refuses to boot. Monitoring you cannot reach is not monitoring.

| Variable | Required | What it does |
|---|---|---|
| `DATABASE_URL` | yes | Postgres. Use the **pooled** connection string, and keep it in the same region as the app — see the region warning in `docs/DEPLOY.md`. |
| `HF_SPACE` | one of these two | The inference Space, as `owner/space-name`. Takes precedence over `INFERENCE_URL`. |
| `INFERENCE_URL` | | Local-dev escape hatch — a direct URL to a locally running `inference/app.py`. Leave unset in production. |
| `HF_TOKEN` | for a private Space | Needs **read** access only. Write access is for pushing the Space, not for running it. |
| `ADMIN_PASSWORD` | yes, in practice | Gates `/admin`. **Unset means refused, never open.** |
| `LOG_LEVEL` | no | Defaults to `INFO`. |
| `PLAYGROUND_RATE_LIMIT_PER_MIN` | no | Defaults to 20. The playground carries no API key by design, so it is limited per IP. |
| `ADMIN_LOGIN_ATTEMPTS_PER_MIN` | no | Defaults to 5. |

## Commands

```bash
dooh seed-thresholds              # populate tag_thresholds from inference/packs.json
dooh create-key "Client name"     # issue an API key (printed once, only a hash is stored)
dooh apply-calibration            # apply measured thresholds from calibrate.py
dooh prune-usage                  # drop expired rate-limit rows
dooh health                       # the /api/v1/health report, without a server
```

`seed-thresholds` is safe to re-run: rows marked `calibrated` are never overwritten, so
re-seeding cannot clobber measured thresholds with the guesses in `packs.json`.

## Testing

```bash
make check              # ruff + the full pytest suite — run this before committing
make test-fast          # the decision rule and image intake alone: no DB, no model, no network
make inference-smoke    # 17 golden ML cases; only needed if you touched inference/
```

`tests/test_banding.py` is the highest-value file in the repo. If you change how a verdict is
decided, it should fail. If it doesn't, the test is wrong.

## The API

Full reference at `/docs` in the running app. In short:

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
  set to that proxy's address.
- **`/admin` is reachable too.** The session cookie is issued without `Secure` over plain HTTP
  (correct — a `Secure` cookie would be silently dropped and login would appear to do nothing),
  which also means the password crosses the network in the clear. Fine on a trusted office LAN;
  do not do it on shared or public Wi-Fi.

If a device cannot connect, read the failure mode first — it narrows things faster than
guessing. A **timeout** means the packets are being dropped somewhere in between; **connection
refused** means they arrived and nothing was listening, so the IP or the port is wrong; an
**SSL error** means the browser force-upgraded to HTTPS, and this server is plain HTTP.

For a timeout, work outwards, not inwards. The host firewall is the last suspect, not the
first:

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
level up. See `dooh/analyze/aggregate.py`.

**A frame we could not analyse fails the whole request.** There is no partial video result.
Returning verdicts computed over four frames of six, with nothing saying so, is "we didn't
check" presented as "we checked and it's clean".

**Frames are sampled, not exhaustive.** Keyframes first (the encoder's own scene-cut signal),
then a colour-aware dedupe, then a cap. `media.frames_considered` exceeding
`media.frames_analyzed` is how a caller learns coverage was partial.

**A video is charged per frame.** `guard` charges one unit per request before the body is
read, which is right for a still and wrong for a video: six frames is six times the model
time. The difference is charged after the fact in `dooh/api/v1/analyze.py`, so a video cannot
be used as the cheap way to consume six times the capacity. A cache hit ran nothing and stays
one unit.

**Admin defaults closed.** With `ADMIN_PASSWORD` unset, `/admin` refuses rather than opening,
and every mutating handler re-checks the session itself. These pages mint API keys and change
the numbers that decide compliance verdicts; defaulting open would be the dangerous failure.

**Colour means a verdict.** Red, amber and green are reserved for present / uncertain / absent.
The interface itself is monochrome, with a single azure used only for links and focus rings, so
nothing in the chrome can be mistaken for a result. All colours and spacing come from the token
block at the top of `dooh/static/css/app.css` — do not introduce literal values in templates.
