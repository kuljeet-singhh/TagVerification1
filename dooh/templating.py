"""
Jinja2 setup and the handful of globals every template needs.

WHY HAND-WRITTEN CSS RATHER THAN TAILWIND
-----------------------------------------
The previous app used Tailwind, which needs Node. Keeping it would have meant either a
`package.json` in a project whose whole point is being pure Python, or downloading a
standalone binary as a build prerequisite. Neither is worth it for five pages: `static/
css/app.css` is one hand-authored stylesheet built on CSS custom properties, so there is no
build step at all — edit the file, reload the browser. The token layer at the top of that
file is the design system, and it is the only place colours and spacing are defined.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

PACKAGE_DIR = Path(__file__).parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


#: path -> ((mtime_ns, size), digest). Not an lru_cache — see asset().
_asset_digests: dict[str, tuple[tuple[int, int], str]] = {}


def asset(path: str) -> str:
    """
    Cache-busting URL for a static file.

    The digest is memoised against the file's mtime and size, NOT for the life of the
    process. That distinction is the whole point, and getting it wrong is expensive to debug.

    This was an `lru_cache`, so the URL only changed when the process restarted. Its docstring
    claimed assets "update the instant the file changes on a redeploy" — true in production,
    where a redeploy is a new process, and false in development, where `make dev` runs uvicorn
    with --reload and --reload watches *.py. Editing the hand-authored CSS or an ES module
    therefore changed nothing the browser could see: same URL, so the browser kept the module
    it had already evaluated.

    That failure is silent and it points away from itself. The server IS serving the new
    bytes; it is simply never asked for them under a new name. So the file on disk is right,
    curl looks right, and only the page is wrong. It cost a real debugging session.

    It also broke the promise at the top of this module — "there is no build step at all —
    edit the file, reload the browser" — which is load-bearing here, because this project has
    no bundler and hand-editing static files is the normal workflow rather than an edge case.

    Cost is one stat() per asset per render: a few microseconds against a request that already
    makes database round trips. The file itself is re-read only when it has actually changed.
    """
    file = STATIC_DIR / path
    try:
        stat = file.stat()
    except OSError:
        # A missing asset should render a plain URL and 404 visibly, not raise mid-template.
        return f"/static/{path}"

    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _asset_digests.get(path)
    if cached is not None and cached[0] == signature:
        return f"/static/{path}?v={cached[1]}"

    digest = hashlib.sha256(file.read_bytes()).hexdigest()[:8]
    _asset_digests[path] = (signature, digest)
    return f"/static/{path}?v={digest}"


# NOTE: static responses deliberately carry no `Cache-Control: immutable`, despite the
# versioned URLs above making that look safe. `playground.js` does
# `import { announce, toast } from "./app.js"` — a relative specifier with NO `?v=`, so
# app.js is also fetched under a bare, unversioned URL. Freezing that for a year would make
# every future app.js edit unshippable. Versioning an ES module import specifier needs a
# bundler or an import map, and this project has neither on purpose. ETag revalidation is
# the right trade here; do not "optimise" it without solving that import first.


def relative_time(value: datetime | None) -> str:
    """"3h ago" / "never" — absolute timestamps in a table are noise you have to decode."""
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    seconds = (datetime.now(UTC) - value).total_seconds()
    if seconds < 60:
        return "just now"
    for limit, divisor, unit in (
        (3600, 60, "m"),
        (86400, 3600, "h"),
        (2592000, 86400, "d"),
    ):
        if seconds < limit:
            return f"{int(seconds // divisor)}{unit} ago"
    return value.strftime("%d %b %Y")


def percent(value: float | None) -> str:
    return "—" if value is None else f"{round(value * 100)}%"


#: Verdict presentation, defined ONCE.
#:
#: Templates look up by band rather than composing class names, so there is exactly one place
#: that decides what "uncertain" looks like and what it is called. The word matters as much
#: as the colour: "Needs review" states the required action, where the old "UNCERTAIN" left
#: the reader to work out that `present: null` is a task and not a middle value.
BANDS: dict[str, dict[str, str]] = {
    "present": {
        "word": "Present",
        "icon": "alert",
        "note": "This content is in the creative.",
    },
    "uncertain": {
        "word": "Needs review",
        "icon": "question",
        "note": "The score fell between the thresholds. A human has to decide.",
    },
    "absent": {
        "word": "Absent",
        "icon": "check",
        "note": "This content was not found.",
    },
}

templates.env.globals.update(
    asset=asset,
    BANDS=BANDS,
    now=lambda: datetime.now(UTC),
)
templates.env.filters.update(
    relative_time=relative_time,
    percent=percent,
)


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=context or {},
        status_code=status_code,
        headers=headers,
    )


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"
