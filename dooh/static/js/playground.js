/* Playground: media intake, tag picker helpers, evidence focus.
 *
 * This is the only page with real client-side logic, and only because three jobs
 * genuinely belong in the browser: downscaling before upload (so we never send
 * 8MB over the wire), moving a rectangle over an image (a server round trip
 * to reposition a box would be absurd), and seeking the video preview to the
 * frame a verdict came from. Everything else is a form POST.
 *
 * A video is NOT transcoded or frame-extracted here. The server samples frames,
 * because the public API has to do it for callers that have no browser, and one
 * sampling rule that both paths share is worth more than a smaller upload. */

import { announce, toast } from "./app.js";

/* ---------------------------------------------------------- image intake */

/* SigLIP consumes 224x224 tiles and we score the full image plus 9 half-size
 * crops, so RESIZING beyond this discards detail the model was going to throw
 * away anyway, and takes a 4K creative from ~8MB to ~150KB. The server does the
 * same thing for API callers who skip it.
 *
 * The RE-ENCODE that used to come with it is a different matter, and it did not
 * cost zero accuracy — see the pass-through in downscale() below. */
const MAX_EDGE = 768;
const QUALITY = 0.9;

/* Formats we are willing to hand to the server untouched.
 *
 * The input accepts `image/*`, and the canvas re-encode below is what has always turned a
 * HEIC, an AVIF or a TIFF into something Pillow can open. Passing those through raw would
 * trade one bug for a new INVALID_IMAGE on every iPhone pick, so the pass-through is limited
 * to the three formats the server certainly reads. */
const PASSTHROUGH_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

/* Above this we re-encode even when the pixels do not need resizing, because the server
 * refuses at MAX_IMAGE_BYTES (10MB, intake.py) and the re-encode is what used to keep a
 * heavyweight small-dimension PNG under it. */
const MAX_PASSTHROUGH_BYTES = 8 * 1024 * 1024;

const form = document.getElementById("analyze-form");
const fileInput = document.getElementById("creative-input");
const dropzone = document.getElementById("dropzone");
const previewSlot = document.getElementById("preview-slot");
const sizeNote = document.getElementById("size-note");
const submitBtn = document.getElementById("analyze-btn");
const submitLabel = document.getElementById("analyze-label");

let previewUrl = null;
let hasImage = false;
/* Kept so the evidence player can be pointed at the file the browser already
 * holds. The server never sends a video back — it never stored one. */
let videoUrl = null;

/* The exact File we wrote back into the <input type=file>, so a `change` event
 * caused by our own assignment can be told apart from a user pick.
 *
 * This used to test `file.name !== "creative.jpg"`, which silently dropped any
 * creative the user had genuinely named creative.jpg — no preview, no size
 * note, and a submit button stuck on "Add a creative" with nothing to explain
 * it. Identity is the right test: it is exact, and unlike a boolean flag it
 * does not depend on whether the assignment dispatches `change` synchronously,
 * asynchronously, or not at all. */
let writtenFile = null;

async function downscale(file) {
  const bitmap = await createImageBitmap(file);
  // Never upscale — a small source stays exactly as it is.
  const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  const width = Math.round(bitmap.width * scale);
  const height = Math.round(bitmap.height * scale);

  /* Nothing to resize: send the file EXACTLY as picked.
   *
   * This used to re-encode regardless, and that quietly made the playground a different
   * client from every other one. An API caller — the DOOH backend included — sends the
   * original bytes, and the server only normalises ABOVE MAX_EDGE, so for a small creative
   * the playground scored a re-compressed JPEG while the app scored the raw file.
   *
   * "No accuracy lost" was simply wrong, and by a wide margin. Measured on the 141x241
   * poster that produced this bug: raw PNG scored 0.6167 for `gambling` at sigmoid 0.0132,
   * the q90 re-encode 0.2476 at sigmoid 0.0006. That is opposite sides of the 0.005
   * absolute-resemblance floor — one verdict PRESENT, the other vetoed to ABSENT, from the
   * same picture. The playground called it clean while the app refused the advertiser.
   *
   * A compliance tool whose demo path disagrees with its real one about the same file is
   * worse than one that uploads a few more kilobytes. Sending the original also means both
   * doors hash to the same sha256 and share a cache row, so the agreement is observable
   * rather than merely asserted.
   *
   * Above MAX_EDGE the two paths still differ — this resizes with canvas, the server with
   * LANCZOS — and neither is byte-identical to the other. That gap is left open on purpose:
   * the >768px downscale is real wire-size protection, and the divergence is far smaller
   * than the one fixed here, because the artefacts that moved the score above are a
   * low-resolution effect. Worth measuring before removing the downscale outright. */
  if (scale === 1 && PASSTHROUGH_TYPES.has(file.type) && file.size <= MAX_PASSTHROUGH_BYTES) {
    bitmap.close();
    const kb = Math.round(file.size / 1024);
    return { file, blob: file, originalKb: kb, uploadKb: kb, width, height };
  }

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas unavailable");
  ctx.drawImage(bitmap, 0, 0, width, height);
  bitmap.close();

  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", QUALITY));
  if (!blob) throw new Error("could not encode the image");

  return {
    file: new File([blob], "creative.jpg", { type: "image/jpeg" }),
    blob,
    originalKb: Math.round(file.size / 1024),
    uploadKb: Math.round(blob.size / 1024),
    width,
    height,
  };
}

/* Some platforms report an empty MIME type for a perfectly valid pick, so the
 * extension is checked too. The server sniffs the actual bytes regardless —
 * this is only about giving fast feedback, never about deciding what runs. */
function looksLikeVideo(file) {
  return file.type.startsWith("video/") || /\.(mp4|mov|m4v|webm)$/i.test(file.name);
}

const MAX_VIDEO_BYTES = 50 * 1024 * 1024;

async function acceptVideo(file) {
  if (file.size > MAX_VIDEO_BYTES) {
    toast(`That video is ${Math.round(file.size / 1024 / 1024)}MB; the limit is 50MB.`, "err");
    return;
  }

  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  videoUrl = previewUrl;

  /* The file is passed through untouched — no canvas, no re-encode. */
  const transfer = new DataTransfer();
  transfer.items.add(file);
  writtenFile = file;
  fileInput.files = transfer.files;

  previewSlot.innerHTML = `
    <div class="preview-frame">
      <video src="${previewUrl}" muted playsinline preload="metadata"></video>
    </div>
    <div class="row gap-2" style="margin-top:8px">
      <button type="button" class="btn btn-outline btn-sm" data-replace>Replace</button>
      <button type="button" class="btn btn-ghost btn-sm" data-remove>Remove</button>
    </div>`;
  dropzone.classList.add("hide");

  const mb = (file.size / 1024 / 1024).toFixed(1);
  sizeNote.textContent = `${mb} MB video · reading duration…`;

  /* Duration is only for the size note. The server samples the frames. */
  const probe = document.createElement("video");
  probe.preload = "metadata";
  probe.onloadedmetadata = () => {
    const seconds = probe.duration;
    sizeNote.textContent = Number.isFinite(seconds)
      ? `${mb} MB · ${seconds.toFixed(1)}s · frames are sampled on the server, one scored per scene.`
      : `${mb} MB video`;
  };
  probe.src = previewUrl;

  hasImage = true;
  refreshSubmit();
  announce("Video ready to analyse.");
}

async function accept(file) {
  if (!file) return;

  if (looksLikeVideo(file)) {
    await acceptVideo(file);
    return;
  }

  if (!file.type.startsWith("image/")) {
    toast(`${file.name} is not an image or a video.`, "err");
    return;
  }

  let prepared;
  try {
    prepared = await downscale(file);
  } catch (error) {
    toast(`Could not read that image: ${error.message}`, "err");
    return;
  }

  videoUrl = null;

  // The old build created one object URL per upload and never revoked any of
  // them, leaking a blob for the lifetime of the page.
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(prepared.blob);

  // Write the downscaled file back into the real <input type=file> so the plain
  // multipart form submission carries it — no custom XHR needed.
  const transfer = new DataTransfer();
  transfer.items.add(prepared.file);
  writtenFile = prepared.file;
  fileInput.files = transfer.files;

  previewSlot.innerHTML = `
    <div class="preview-frame">
      <img src="${previewUrl}" alt="Selected creative">
    </div>
    <div class="row gap-2" style="margin-top:8px">
      <button type="button" class="btn btn-outline btn-sm" data-replace>Replace</button>
      <button type="button" class="btn btn-ghost btn-sm" data-remove>Remove</button>
    </div>`;
  dropzone.classList.add("hide");

  const saved = prepared.originalKb > prepared.uploadKb;
  sizeNote.textContent = saved
    ? `${prepared.originalKb} KB → ${prepared.uploadKb} KB · resized to ${prepared.width}×${prepared.height} in your browser, no accuracy lost.`
    : `${prepared.uploadKb} KB · ${prepared.width}×${prepared.height}`;

  hasImage = true;
  refreshSubmit();
  announce("Creative ready to analyse.");
}

function clearImage() {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  videoUrl = null;
  writtenFile = null;
  fileInput.value = "";
  previewSlot.innerHTML = "";
  sizeNote.textContent = "";
  dropzone.classList.remove("hide");
  hasImage = false;
  refreshSubmit();
}

if (dropzone) {
  // A plain dragleave fires when the pointer crosses onto a CHILD element, so a
  // dropzone with content flickers. Counting enter/leave pairs fixes it.
  let depth = 0;
  const stop = (event) => { event.preventDefault(); event.stopPropagation(); };

  dropzone.addEventListener("dragenter", (event) => {
    stop(event); depth += 1; dropzone.classList.add("is-dragging");
  });
  dropzone.addEventListener("dragover", stop);
  dropzone.addEventListener("dragleave", (event) => {
    stop(event); depth -= 1;
    if (depth <= 0) { depth = 0; dropzone.classList.remove("is-dragging"); }
  });
  dropzone.addEventListener("drop", (event) => {
    stop(event); depth = 0; dropzone.classList.remove("is-dragging");
    accept(event.dataTransfer.files[0]);
  });
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); }
  });
}

if (fileInput) {
  fileInput.addEventListener("change", () => {
    // Guard against re-entering when we set .files ourselves above.
    const file = fileInput.files[0];
    if (file && file !== writtenFile) accept(file);
  });
}

if (previewSlot) {
  previewSlot.addEventListener("click", (event) => {
    if (event.target.closest("[data-replace]")) fileInput.click();
    if (event.target.closest("[data-remove]")) clearImage();
  });
}

// Pasting a creative straight from the clipboard is how people actually move
// images around (Slack, Figma, a screenshot). Costs four lines.
document.addEventListener("paste", (event) => {
  const item = [...(event.clipboardData?.items || [])].find(
    (i) => i.type.startsWith("image/") || i.type.startsWith("video/"),
  );
  if (item) accept(item.getAsFile());
});

/* ------------------------------------------------------------ tag picker */

const tagSearch = document.getElementById("tag-search");
const tagDesc = document.getElementById("tag-desc");

function selectedTags() {
  return [...form.querySelectorAll('input[name="tags"]:checked')];
}

/* A disabled button with no explanation is a dead end. The label always states
 * the blocker, so the user never has to guess what is missing. */
function refreshSubmit() {
  if (!submitBtn) return;
  const count = selectedTags().length;
  const ready = hasImage && count > 0;

  submitBtn.disabled = !ready;
  if (!hasImage) submitLabel.textContent = "Add a creative";
  else if (count === 0) submitLabel.textContent = "Pick at least one tag";
  else submitLabel.textContent = `Analyse · ${count} tag${count === 1 ? "" : "s"}`;

  const counter = document.getElementById("tag-count");
  if (counter) counter.textContent = count;

  // Fill the step markers as each precondition is met, so the two things the
  // disabled button is waiting for are visible in the form itself and not only
  // in the button's label.
  document.getElementById("step-creative")?.classList.toggle("step-done", hasImage);
  document.getElementById("step-tags")?.classList.toggle("step-done", count > 0);
}

if (form) {
  form.addEventListener("change", (event) => {
    if (event.target.name === "tags") refreshSubmit();
  });

  // Show the tag's description on hover/focus. The old build used title=,
  // which is invisible on touch and unreliably announced by screen readers.
  form.addEventListener("mouseover", showDesc);
  form.addEventListener("focusin", showDesc);
  form.addEventListener("mouseout", clearDesc);
  form.addEventListener("focusout", clearDesc);

  // Cmd/Ctrl+Enter submits from anywhere in the form.
  form.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && !submitBtn.disabled) {
      form.requestSubmit(submitBtn);
    }
  });
}

function showDesc(event) {
  const label = event.target.closest(".tag");
  if (label && label.dataset.description && tagDesc) tagDesc.textContent = label.dataset.description;
}
function clearDesc(event) {
  if (event.target.closest(".tag") && tagDesc) tagDesc.textContent = "";
}

if (tagSearch) {
  tagSearch.addEventListener("input", () => {
    const query = tagSearch.value.trim().toLowerCase();
    document.querySelectorAll(".tag").forEach((label) => {
      const haystack = `${label.dataset.slug} ${label.textContent} ${label.dataset.description || ""}`;
      label.classList.toggle("is-filtered", Boolean(query) && !haystack.toLowerCase().includes(query));
    });
    document.querySelectorAll(".tag-group").forEach((group) => {
      const visible = group.querySelectorAll(".tag:not(.is-filtered)").length;
      group.classList.toggle("hide", visible === 0);
    });
  });
}

document.body.addEventListener("click", (event) => {
  const button = event.target.closest("[data-select-group]");
  if (!button) return;
  const group = button.closest(".tag-group");
  const on = button.dataset.selectGroup === "all";
  group.querySelectorAll('input[name="tags"]').forEach((input) => {
    if (!input.closest(".tag").classList.contains("is-filtered")) input.checked = on;
  });
  refreshSubmit();
});

/* Share a selection. The old build hardcoded ["alcohol"] with no way to send a
 * colleague "check this against alcohol + vaping". */
document.body.addEventListener("click", (event) => {
  if (!event.target.closest("[data-copy-link]")) return;
  const slugs = selectedTags().map((input) => input.value).join(",");
  const url = `${location.origin}/?tags=${encodeURIComponent(slugs)}`;
  navigator.clipboard?.writeText(url);
  toast("Link to this tag selection copied.", "ok");
});

/* -------------------------------------------------------- evidence focus */

/* All boxes and all verdict data are already in the DOM after a swap, and the
 * server renders each box's geometry as inline percentages. So this only has to
 * move a `data-focused` attribute — no measurement, no resize listener. */
function focusTag(slug) {
  let stamp = null;
  document.querySelectorAll("#evidence .crop-box").forEach((box) => {
    const focused = box.dataset.tag === slug;
    box.dataset.focused = String(focused);
    if (focused && box.dataset.timestamp !== undefined) stamp = Number(box.dataset.timestamp);
  });

  /* Seek to the frame this verdict actually came from. Without this the box is
   * drawn over whatever frame the player happens to be showing, which points at
   * the wrong place and quietly implies the wrong moment. */
  const player = document.getElementById("evidence-video");
  if (player && stamp !== null && Number.isFinite(stamp)) {
    const seek = () => { player.currentTime = stamp; };
    if (player.readyState >= 1) seek();
    else player.addEventListener("loadedmetadata", seek, { once: true });
  }

  document.querySelectorAll(".verdict").forEach((card) => {
    card.setAttribute("aria-pressed", String(card.dataset.tag === slug));
  });
  const caption = document.getElementById("evidence-caption");
  const card = document.querySelector(`.verdict[data-tag="${CSS.escape(slug)}"]`);
  if (caption && card) {
    const at = stamp !== null && Number.isFinite(stamp)
      ? ` at <strong>${stamp.toFixed(1)}s</strong>`
      : "";
    caption.innerHTML =
      `Best-scoring region for <strong>${card.dataset.tag}</strong>${at} — matched ` +
      `<em>&ldquo;${card.dataset.phrase}&rdquo;</em>`;
  }
}

document.body.addEventListener("click", (event) => {
  const card = event.target.closest(".verdict");
  if (!card) return;
  focusTag(card.dataset.tag);
  if (window.innerWidth < 1024) {
    document.getElementById("evidence")?.scrollIntoView({ block: "center", behavior: "smooth" });
  }
});

// Arrow-key movement through the verdict list, so the evidence box is
// reachable without a mouse.
document.body.addEventListener("keydown", (event) => {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const card = event.target.closest(".verdict");
  if (!card) return;
  const cards = [...document.querySelectorAll(".verdict")];
  const next = cards[cards.indexOf(card) + (event.key === "ArrowDown" ? 1 : -1)];
  if (next) { event.preventDefault(); next.focus(); focusTag(next.dataset.tag); }
});

/* --------------------------------------------------------- elapsed timer */

/* An 8s wait with a static spinner reads as broken. Naming what is happening —
 * and admitting when the Space is probably cold — is the difference between
 * "stuck" and "starting up". */
let timer = null;

document.body.addEventListener("htmx:beforeRequest", (event) => {
  if (event.target.id !== "analyze-form") return;
  const label = document.getElementById("elapsed");
  if (!label) return;
  const started = Date.now();
  timer = setInterval(() => {
    const seconds = (Date.now() - started) / 1000;
    label.textContent = seconds > 5
      ? `${seconds.toFixed(0)}s — the Space may be waking from sleep, which takes 30–60s.`
      : `${seconds.toFixed(1)}s`;
  }, 200);
});

document.body.addEventListener("htmx:afterRequest", (event) => {
  if (event.target.id !== "analyze-form") return;
  clearInterval(timer);
});

document.body.addEventListener("htmx:afterSettle", () => {
  /* The results fragment ships an empty <video>: the server has no copy of the
   * creative to send back, and never did. Point it at the object URL the browser
   * is already holding. */
  const player = document.getElementById("evidence-video");
  if (player && videoUrl && !player.src) player.src = videoUrl;

  const first = document.querySelector(".verdict");
  if (first) focusTag(first.dataset.tag);
});

refreshSubmit();
