"""
Hugging Face Space entrypoint.

Serves two things from one process:

  1. POST /gradio_api/call/analyze   -- the machine endpoint Next.js calls.
     Registered with gr.api(), so it has no UI attached and takes a base64
     image string rather than an uploaded file. That keeps the call to a single
     round trip instead of Gradio's upload-then-submit dance.

  2. A browser UI for manual testing -- upload a creative, tick tags, see
     scores and the evidence crop. Marked api_name=False so it is never part
     of the public API surface.

The model loads at import time (module level), not on first request, so the
Space is either fully ready or still booting -- never half-warm.

Run locally:  ./.venv/bin/python app.py     (no Docker required)
"""

import base64
import binascii
import hmac
import io
import os
import threading
import time

import gradio as gr
from PIL import Image

from detector import Detector
from versioning import surface_fingerprint

# ---------------------------------------------------------------- startup ----
# Loaded once, at import. Also builds the text-embedding cache for every prompt
# in packs.json, which is what makes each request a single image forward pass.
DETECTOR = Detector("packs.json")

# Snapshotted at import into the browser demo's CheckboxGroup below. A reload does NOT
# refresh it -- the component was built once -- so after publishing a tag the demo's tick
# list is stale until the page is reloaded. Cosmetic: every API surface reads DETECTOR live.
TAG_CHOICES = [(pack["label"], slug) for slug, pack in DETECTOR.packs.items()]

# Held only for the pointer rebind in reload_packs, never across the encode.
_SWAP = threading.Lock()

# Guard against someone posting a 50MP TIFF. The Next.js client already
# downscales to 768px, so anything large here is a misbehaving caller.
MAX_IMAGE_BYTES = 12 * 1024 * 1024
# Raised from 30 when the catalog was opened up to DOOH admin. This is a REQUEST-SIZE guard,
# not a scoring rule: _rival_rows is built from the whole pack at import and never from a
# request's tag list, so a tag's score is identical however many tags a call names.
#
# Measured on an M-series Mac, 768px image, 22-tag pack:
#     1 tag  -> 541 ms      22 tags -> 567 ms
# i.e. ~1.2 ms per extra tag against a ~540 ms image encode that is paid once and shared. At
# 100 tags that projects to ~660 ms. Kept deliberately above MAX_TAGS in catalog.py so the
# catalog filling up is never also a request-size failure.
MAX_TAGS_PER_CALL = 120


def _decode_image(image_b64: str) -> Image.Image:
    """Accept a raw base64 string or a full `data:image/...;base64,...` URL."""
    if not image_b64 or not image_b64.strip():
        raise gr.Error("image is required")

    payload = image_b64.strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")

    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise gr.Error(f"image is not valid base64: {exc}") from exc

    if len(raw) > MAX_IMAGE_BYTES:
        raise gr.Error(
            f"image is {len(raw) // 1024}KB, limit is {MAX_IMAGE_BYTES // 1024}KB"
        )

    try:
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise gr.Error(f"could not decode image: {exc}") from exc


def _validate_tags(detector: Detector, tags: list[str]) -> list[str]:
    """Takes the detector rather than reading the global: see analyze_b64."""
    if not tags:
        raise gr.Error("at least one tag is required")
    if len(tags) > MAX_TAGS_PER_CALL:
        raise gr.Error(f"too many tags ({len(tags)}), limit is {MAX_TAGS_PER_CALL}")

    unknown = [t for t in tags if t not in detector.packs]
    if unknown:
        # Echo the valid list back. A caller typo must never be silently
        # swallowed -- "unknown tag" and "content absent" mean opposite things.
        raise gr.Error(
            f"unknown tag(s): {', '.join(unknown)}. "
            f"known tags: {', '.join(sorted(detector.packs))}"
        )
    return tags


def analyze_b64(image_b64: str, tags: list[str]) -> dict:
    """
    Machine endpoint. Returns the payload the Next.js API tier reshapes into its
    own public contract.

    `present` is True / False / None, where None means the score landed in the
    uncertain band and the caller should escalate to a vision LLM.
    """
    # Bind the global ONCE, then use the local everywhere. reload_packs rebinds DETECTOR
    # while requests are in flight, and this function reads it either side of a multi-second
    # forward pass -- so re-reading it would let a response carry the NEW packs_version
    # stamped on scores the OLD pack produced. A verdict that misreports which pack decided
    # it is worse than no verdict: the whole cache keys on that value.
    detector = DETECTOR

    # Cheap validation first: rejecting a bad tag list costs nothing, while
    # base64-decoding a 12MB image to then discover the tags were empty is waste.
    tags = _validate_tags(detector, tags)
    image = _decode_image(image_b64)

    start = time.perf_counter()
    verdicts = detector.analyze(image, tags)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

    return {
        "model": detector.model_id,
        "packs_version": detector.packs_version,
        "latency_ms": elapsed_ms,
        "image_size": list(image.size),
        "results": [v.to_dict() for v in verdicts],
    }


def health() -> dict:
    """Cheap liveness probe. Used by the keepalive cron and /api/v1/health."""
    detector = DETECTOR  # one read: a reload must not split this payload across two packs
    return {
        "status": "ok",
        "model": detector.model_id,
        "packs_version": detector.packs_version,
        "tags": sorted(detector.packs),
        "prompts": len(detector._prompt_rows),
        # Per-tag, so the admin page can see an EDITED tag and not just a missing one. `tags`
        # above is names only, and a tag keeps its name across an edit -- which is how a
        # rewritten saloon went on being scored against its old phrases with nothing warning.
        #
        # Recomputed per call rather than cached on the Detector: it is 22 sha256 hashes over
        # a few KB, and building it in detector.py would fold it into packs_version, so
        # changing what it covers would later invalidate every cached score for nothing.
        "prompt_fingerprints": {
            slug: surface_fingerprint(
                pack["positives"], pack["negatives"], pack.get("sigmoid_floor")
            )
            for slug, pack in detector.packs.items()
        },
    }


def tag_catalog() -> dict:
    """Tag list with descriptions. Thresholds are deliberately NOT exposed --
    they are tuning internals, not part of the contract."""
    detector = DETECTOR  # one read; also stops the loop below iterating a swapped-out dict
    return {
        "packs_version": detector.packs_version,
        "tags": [
            {
                "slug": slug,
                "label": pack["label"],
                "description": pack.get("description", ""),
            }
            for slug, pack in detector.packs.items()
        ],
    }


def reload_packs(pack_json: str, secret: str) -> dict:
    """
    Publish a new pack into the RUNNING process. Returns the same payload as health().

    Why this exists: __init__ encodes every prompt once, so without it a created tag reaches
    the model only when the process restarts. Why it takes the pack in the BODY rather than
    re-reading packs.json: in production this runs on a Space whose filesystem is ephemeral
    and whose only writer is git, on a different machine from `dooh export-packs` -- a
    disk-reading reload would work locally and do nothing where it matters.

    Defaults closed, like the admin gate. gr.api endpoints carry no authorization of their
    own, and the Space is shielded only by HF privacy plus a READ-scoped token that is
    already deployed to the web tier and to CI -- so anything mutating needs a secret of its
    own. With RELOAD_SECRET unset this refuses rather than opening.
    """
    global DETECTOR

    expected = os.environ.get("RELOAD_SECRET", "")
    if not expected:
        raise gr.Error("reload_packs is disabled: RELOAD_SECRET is not set on this Space")
    if not hmac.compare_digest(secret or "", expected):
        raise gr.Error("reload_packs: bad secret")

    if not pack_json or not pack_json.strip():
        raise gr.Error("reload_packs: empty pack")

    # The exporter's bytes, unmodified -- packs_version hashes them, so re-encoding the
    # string here (or pretty-printing it) would move the fingerprint away from the one
    # `dooh export-packs` computed and break `dooh apply-calibration`.
    pack_bytes = pack_json.encode()

    try:
        # OUTSIDE the lock. Re-encoding the whole prompt table measures around 4.8s, and
        # holding a lock across that would stall every analyze queued behind it. The clone
        # is invisible until the rebind below, so building it concurrently is safe.
        rebuilt = DETECTOR.rebuilt(pack_bytes)
    except Exception as exc:
        # The running pack is untouched: a malformed push must never leave the process
        # unable to score.
        raise gr.Error(
            f"reload_packs: rejected, still serving the previous pack ({exc})"
        ) from exc

    # The lock covers only the rebind. The encode above is the slow part and it is already
    # done; what this serialises is two concurrent pushes racing to be last.
    with _SWAP:
        DETECTOR = rebuilt

    print(f"[detector] reloaded: {len(rebuilt.packs)} tags, packs {rebuilt.packs_version}")
    return health()


# ------------------------------------------------------------------- the UI --

BADGE = {"present": "🔴 PRESENT", "absent": "🟢 absent", "uncertain": "🟡 uncertain"}


def _ui_analyze(image: Image.Image | None, tags: list[str]):
    """UI handler. Same detector call, but formats for human eyes and draws the
    winning crop so you can see WHERE the model thinks it found the content."""
    if image is None:
        raise gr.Error("upload an image first")
    detector = DETECTOR  # bound once, same reason as analyze_b64
    tags = _validate_tags(detector, tags)

    start = time.perf_counter()
    verdicts = detector.analyze(image, tags)
    elapsed_ms = (time.perf_counter() - start) * 1000

    rows = [
        "| tag | verdict | score | sigmoid | confidence | matched phrase |",
        "|---|---|---|---|---|---|",
    ]
    boxes = []
    width, height = image.size

    for v in sorted(verdicts, key=lambda x: -x.score):
        rows.append(
            f"| `{v.tag}` | {BADGE[v.band]} | **{v.score:.3f}** | {v.sigmoid:.3f} "
            f"| {v.confidence} | _{v.top_phrase}_ |"
        )
        # Only annotate detections; boxing every absent tag is just noise.
        if v.band in ("present", "uncertain"):
            x0, y0, x1, y1 = v.crop
            boxes.append(
                (
                    (int(x0 * width), int(y0 * height), int(x1 * width), int(y1 * height)),
                    f"{v.tag} {v.score:.2f}",
                )
            )

    summary = (
        f"**{len(verdicts)} tag(s) in {elapsed_ms:.0f} ms** "
        f"· model `{detector.model_id}` · packs `{detector.packs_version}`\n\n"
        + "\n".join(rows)
        + "\n\n_score = softmax mass on the tag's positive prompts (the calibrated "
        "signal). sigmoid = absolute resemblance, used only as a low backstop._"
    )

    payload = {
        "latency_ms": round(elapsed_ms, 1),
        "results": [v.to_dict() for v in verdicts],
    }
    return summary, (image, boxes), payload


with gr.Blocks(title="DOOH Tag Verification") as demo:
    gr.Markdown(
        "# DOOH Tag Verification\n"
        "Check whether a creative actually contains the content it is tagged with.\n\n"
        "Scoring: each tag's positive prompts compete in a softmax against "
        "hand-written **hard negatives** (whiskey vs *juice*) plus shared "
        "distractors. The image is scored as a whole and as 9 overlapping "
        "windows, keeping the best — so a small bottle in a corner still counts."
    )

    with gr.Row():
        with gr.Column(scale=1):
            image_in = gr.Image(type="pil", label="Creative", height=320)
            tags_in = gr.CheckboxGroup(
                choices=TAG_CHOICES,
                value=["alcohol"],
                label="Tags to verify",
            )
            run_btn = gr.Button("Analyze", variant="primary")

        with gr.Column(scale=2):
            summary_out = gr.Markdown(label="Results")
            annotated_out = gr.AnnotatedImage(label="Evidence — winning crop", height=320)
            json_out = gr.JSON(label="Raw payload")

    run_btn.click(
        _ui_analyze,
        inputs=[image_in, tags_in],
        outputs=[summary_out, annotated_out, json_out],
        api_name=False,  # UI only — the real API surface is gr.api() below
        concurrency_id="detector",  # see the API definitions below
    )

    # ----------------------------------------------------- API definitions --
    # The endpoints Next.js talks to. Declared inside the Blocks context (that
    # is what registers them on this app) but with no components attached, so
    # the public contract doesn't drift when the UI layout changes.
    #
    # concurrency_id groups analyze, the UI button and reload_packs into ONE queue at limit
    # 1. Without it Gradio gives every function object its own group -- the browser demo can
    # already run Detector.analyze alongside an API call today -- and a reload would swap the
    # pack out from under an in-flight request. Sharing the id costs a 4.8s stall on the
    # requests queued behind a publish, which is the right trade for never scoring an image
    # against a pack that is halfway out the door.
    #
    # health and tags stay OUT of the group on purpose: the keepalive cron pings health every
    # few minutes and must not sit behind a reload or a slow analyze.
    gr.api(analyze_b64, api_name="analyze", concurrency_id="detector")
    gr.api(reload_packs, api_name="reload_packs", concurrency_id="detector")
    gr.api(health, api_name="health")
    gr.api(tag_catalog, api_name="tags")

if __name__ == "__main__":
    # 7860 is the port HF Spaces expects.
    demo.queue(max_size=32).launch(server_name="0.0.0.0", server_port=7860)
