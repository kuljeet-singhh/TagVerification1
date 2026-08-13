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
import io
import time

import gradio as gr
from PIL import Image

from detector import Detector

# ---------------------------------------------------------------- startup ----
# Loaded once, at import. Also builds the text-embedding cache for every prompt
# in packs.json, which is what makes each request a single image forward pass.
DETECTOR = Detector("packs.json")

TAG_CHOICES = [(pack["label"], slug) for slug, pack in DETECTOR.packs.items()]

# Guard against someone posting a 50MP TIFF. The Next.js client already
# downscales to 768px, so anything large here is a misbehaving caller.
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_TAGS_PER_CALL = 30


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


def _validate_tags(tags: list[str]) -> list[str]:
    if not tags:
        raise gr.Error("at least one tag is required")
    if len(tags) > MAX_TAGS_PER_CALL:
        raise gr.Error(f"too many tags ({len(tags)}), limit is {MAX_TAGS_PER_CALL}")

    unknown = [t for t in tags if t not in DETECTOR.packs]
    if unknown:
        # Echo the valid list back. A caller typo must never be silently
        # swallowed -- "unknown tag" and "content absent" mean opposite things.
        raise gr.Error(
            f"unknown tag(s): {', '.join(unknown)}. "
            f"known tags: {', '.join(sorted(DETECTOR.packs))}"
        )
    return tags


def analyze_b64(image_b64: str, tags: list[str]) -> dict:
    """
    Machine endpoint. Returns the payload the Next.js API tier reshapes into its
    own public contract.

    `present` is True / False / None, where None means the score landed in the
    uncertain band and the caller should escalate to a vision LLM.
    """
    # Cheap validation first: rejecting a bad tag list costs nothing, while
    # base64-decoding a 12MB image to then discover the tags were empty is waste.
    tags = _validate_tags(tags)
    image = _decode_image(image_b64)

    start = time.perf_counter()
    verdicts = DETECTOR.analyze(image, tags)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

    return {
        "model": DETECTOR.model_id,
        "packs_version": DETECTOR.packs_version,
        "latency_ms": elapsed_ms,
        "image_size": list(image.size),
        "results": [v.to_dict() for v in verdicts],
    }


def health() -> dict:
    """Cheap liveness probe. Used by the keepalive cron and /api/v1/health."""
    return {
        "status": "ok",
        "model": DETECTOR.model_id,
        "packs_version": DETECTOR.packs_version,
        "tags": sorted(DETECTOR.packs),
        "prompts": len(DETECTOR._prompt_rows),
    }


def tag_catalog() -> dict:
    """Tag list with descriptions. Thresholds are deliberately NOT exposed --
    they are tuning internals, not part of the contract."""
    return {
        "packs_version": DETECTOR.packs_version,
        "tags": [
            {
                "slug": slug,
                "label": pack["label"],
                "description": pack.get("description", ""),
            }
            for slug, pack in DETECTOR.packs.items()
        ],
    }


# ------------------------------------------------------------------- the UI --

BADGE = {"present": "🔴 PRESENT", "absent": "🟢 absent", "uncertain": "🟡 uncertain"}


def _ui_analyze(image: Image.Image | None, tags: list[str]):
    """UI handler. Same detector call, but formats for human eyes and draws the
    winning crop so you can see WHERE the model thinks it found the content."""
    if image is None:
        raise gr.Error("upload an image first")
    tags = _validate_tags(tags)

    start = time.perf_counter()
    verdicts = DETECTOR.analyze(image, tags)
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
        f"· model `{DETECTOR.model_id}` · packs `{DETECTOR.packs_version}`\n\n"
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
    )

    # ----------------------------------------------------- API definitions --
    # The endpoints Next.js talks to. Declared inside the Blocks context (that
    # is what registers them on this app) but with no components attached, so
    # the public contract doesn't drift when the UI layout changes.
    gr.api(analyze_b64, api_name="analyze")
    gr.api(health, api_name="health")
    gr.api(tag_catalog, api_name="tags")

if __name__ == "__main__":
    # 7860 is the port HF Spaces expects.
    demo.queue(max_size=32).launch(server_name="0.0.0.0", server_port=7860)
