/* App-wide behaviour: theme, progress bar, toasts, confirm dialogs, copy buttons.
 *
 * Everything here uses event delegation on document.body, so nothing needs to be
 * re-bound after an HTMX swap and there is no teardown to get wrong. */

/* ------------------------------------------------------------------- theme */

const root = document.documentElement;

function currentTheme() {
  if (root.dataset.theme) return root.dataset.theme;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

document.body.addEventListener("click", (event) => {
  if (!event.target.closest("[data-theme-toggle]")) return;
  const next = currentTheme() === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) { /* private mode */ }
});

/* ------------------------------------------------------------ progress bar */

const progress = document.getElementById("progress");
let inflight = 0;

function setProgress(value, active) {
  if (!progress) return;
  progress.style.transform = `scaleX(${value})`;
  progress.classList.toggle("is-active", active);
}

document.body.addEventListener("htmx:beforeRequest", () => {
  inflight += 1;
  setProgress(0.3, true);
});
document.body.addEventListener("htmx:beforeSwap", () => setProgress(0.85, true));
document.body.addEventListener("htmx:afterRequest", () => {
  inflight = Math.max(0, inflight - 1);
  if (inflight > 0) return;
  setProgress(1, true);
  setTimeout(() => { setProgress(0, false); }, 220);
});

/* ----------------------------------------------------------------- toasts */

const toastHost = document.getElementById("toasts");

export function toast(message, kind = "info") {
  if (!toastHost) return;
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.setAttribute("role", kind === "err" ? "alert" : "status");
  el.textContent = message;
  toastHost.appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .2s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 220);
  }, kind === "err" ? 6000 : 3500);
}

/* The convention this enforces: a user-fixable problem comes back as 200 plus a
 * rendered fragment, so the user sees it in place. A 4xx/5xx means something
 * actually broke, and HTMX drops the body — without this the page would simply
 * do nothing and the user would be left guessing. */
document.body.addEventListener("htmx:responseError", (event) => {
  const xhr = event.detail.xhr;
  let message = `${xhr.status} — request failed`;
  try {
    const parsed = JSON.parse(xhr.responseText);
    if (parsed.message) message = parsed.message;
  } catch (e) { /* not JSON; the status line will do */ }
  toast(message, "err");
});
document.body.addEventListener("htmx:sendError", () => toast("Network unreachable.", "err"));
document.body.addEventListener("htmx:timeout", () => toast("The request timed out.", "err"));

/* Server-driven toasts: any handler can add `HX-Trigger: {"toast": {...}}`. */
document.body.addEventListener("toast", (event) => {
  if (event.detail) toast(event.detail.message, event.detail.kind || "info");
});

export function announce(message) {
  const node = document.getElementById("announcer");
  if (node) node.textContent = message;
}

/* --------------------------------------------------------------- confirm */

/* window.confirm is unstyled, blocks the whole tab, and looks like a browser
 * warning rather than part of the app. A native <dialog> gives us the focus
 * trap, Esc handling and inertness for free. */
document.body.addEventListener("htmx:confirm", (event) => {
  const question = event.detail.question;
  if (!question) return;
  event.preventDefault();

  const dialog = document.createElement("dialog");
  dialog.className = "confirm";
  dialog.innerHTML = `
    <h3>Are you sure?</h3>
    <p></p>
    <div class="row gap-2" style="justify-content:flex-end">
      <button class="btn btn-outline" value="cancel">Cancel</button>
      <button class="btn btn-primary" value="ok" autofocus>Continue</button>
    </div>`;
  dialog.querySelector("p").textContent = question;
  document.body.appendChild(dialog);

  dialog.addEventListener("click", (e) => {
    const button = e.target.closest("button");
    if (!button) return;
    dialog.close(button.value);
  });
  dialog.addEventListener("close", () => {
    const confirmed = dialog.returnValue === "ok";
    dialog.remove();
    if (confirmed) event.detail.issueRequest(true);
  });
  dialog.showModal();
});

/* ---------------------------------------------------------------- clipboard */

document.body.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;

  const selector = button.getAttribute("data-copy");
  const source = selector ? document.querySelector(selector) : null;
  const text = source ? (source.value ?? source.textContent) : button.dataset.copyText;
  if (!text) return;

  try {
    await navigator.clipboard.writeText(text.trim());
  } catch (e) {
    // Clipboard API needs a secure context; plain-HTTP localhost is the common case.
    const scratch = document.createElement("textarea");
    scratch.value = text.trim();
    scratch.style.position = "fixed";
    scratch.style.opacity = "0";
    document.body.appendChild(scratch);
    scratch.select();
    document.execCommand("copy");
    scratch.remove();
  }

  const original = button.textContent;
  button.textContent = "Copied";
  announce("Copied to clipboard");
  setTimeout(() => { button.textContent = original; }, 1500);
});

/* ------------------------------------------------------------------ config */

document.addEventListener("DOMContentLoaded", () => {
  if (!window.htmx) return;
  window.htmx.config.includeIndicatorStyles = false;
  window.htmx.config.scrollBehavior = "instant";
});
