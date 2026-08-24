"""
Typed error codes so callers can branch on `error` instead of regex-matching prose.

UNKNOWN_TAG deserves a note: it is an ERROR, never a silent skip. If a caller misspells
"alcohol" we must not return "no alcohol found" — "we didn't check" and "we checked and it's
clean" mean opposite things, and conflating them in a compliance tool is how an illegal
creative reaches a screen.

The envelope is `{"error": CODE, "message": "...", ...extra}`. FastAPI's default is
`{"detail": ...}`, so `install_error_handlers()` in tagverify/main.py overrides the framework's
handlers — otherwise a validation failure would return a differently-shaped body than every
other error and break callers that branch on `error`.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi.responses import JSONResponse

ApiErrorCode = Literal[
    "INVALID_KEY",
    "MISSING_KEY",
    "RATE_LIMITED",
    "UNKNOWN_TAG",
    "NO_TAGS",
    "NO_IMAGE",
    "IMAGE_TOO_LARGE",
    "INVALID_IMAGE",
    "INVALID_IMAGE_URL",
    "VIDEO_TOO_LARGE",
    "VIDEO_TOO_LONG",
    "INVALID_VIDEO",
    "BAD_REQUEST",
    "INFERENCE_WARMING",
    "INFERENCE_FAILED",
    "INTERNAL",
]

STATUS: dict[str, int] = {
    "MISSING_KEY": 401,
    "INVALID_KEY": 401,
    "RATE_LIMITED": 429,
    "UNKNOWN_TAG": 400,
    "NO_TAGS": 400,
    "NO_IMAGE": 400,
    "IMAGE_TOO_LARGE": 413,
    "INVALID_IMAGE": 400,
    "INVALID_IMAGE_URL": 400,
    "VIDEO_TOO_LARGE": 413,
    "VIDEO_TOO_LONG": 413,
    "INVALID_VIDEO": 400,
    "BAD_REQUEST": 400,
    "INFERENCE_WARMING": 503,
    "INFERENCE_FAILED": 502,
    "INTERNAL": 500,
}


class ApiError(Exception):
    """Raise from anywhere in a request; the handler in main.py renders it."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def response(self) -> JSONResponse:
        return api_error(self.code, self.message, **self.extra)


def api_error(code: str, message: str, **extra: Any) -> JSONResponse:
    status = STATUS.get(code, 500)
    headers: dict[str, str] = {}

    # Make retryability machine-readable, not something the caller has to infer from prose.
    retry_after = extra.get("retry_after")
    if isinstance(retry_after, int):
        headers["Retry-After"] = str(retry_after)

    return JSONResponse(
        {"error": code, "message": message, **extra}, status_code=status, headers=headers
    )
