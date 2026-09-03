"""
The admin gate.

Ported from scripts/smoke-admin.mjs, which forged candidate cookies to prove each rejection
branch. These pages mint API keys and change the thresholds that decide compliance verdicts,
so every branch that could let someone in is worth an explicit test.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from tagverify.auth import admin
from tests.conftest import needs_db


def forge(issued_at: str, key: str) -> str:
    signature = hmac.new(key.encode(), issued_at.encode(), hashlib.sha256).hexdigest()
    return f"{issued_at}.{signature}"


def now_ms() -> int:
    return int(time.time() * 1000)


# ------------------------------------------------------------ cookie forgery


@pytest.mark.parametrize(
    ("label", "cookie"),
    [
        ("no cookie", None),
        ("empty", ""),
        ("no separator", "abcdef"),
        ("empty signature", "1700000000000."),
        ("empty timestamp", ".deadbeef"),
        ("non-numeric timestamp", "notatime.deadbeef"),
        ("garbage signature", "1700000000000.deadbeef"),
    ],
)
async def test_malformed_cookies_are_rejected(client, label: str, cookie: str | None) -> None:
    cookies = {admin.COOKIE: cookie} if cookie is not None else {}
    response = await client.get("/admin", cookies=cookies)
    assert "Admin sign in" in response.text, label


async def test_signature_from_the_wrong_key_is_rejected(client) -> None:
    forged = forge(str(now_ms()), "definitely-not-the-admin-password")
    response = await client.get("/admin", cookies={admin.COOKIE: forged})
    assert "Admin sign in" in response.text


async def test_expired_cookie_is_rejected(client, admin_password: str) -> None:
    eight_days_ago = str(now_ms() - (8 * 24 * 60 * 60 * 1000))
    response = await client.get(
        "/admin", cookies={admin.COOKIE: forge(eight_days_ago, admin_password)}
    )
    assert "Admin sign in" in response.text


async def test_future_dated_cookie_is_rejected(client, admin_password: str) -> None:
    """A negative age must not be treated as freshly issued."""
    tomorrow = str(now_ms() + (24 * 60 * 60 * 1000))
    response = await client.get("/admin", cookies={admin.COOKIE: forge(tomorrow, admin_password)})
    assert "Admin sign in" in response.text


@needs_db
async def test_a_correctly_signed_cookie_is_accepted(client, admin_password: str) -> None:
    response = await client.get(
        "/admin", cookies={admin.COOKIE: forge(str(now_ms()), admin_password)}
    )
    assert response.status_code == 200
    assert "Admin sign in" not in response.text
    assert "Create a key" in response.text


# -------------------------------------------------------------------- login


async def test_wrong_password_is_401(client) -> None:
    response = await client.post("/admin/login", data={"password": "wrong"})
    assert response.status_code == 401
    assert "Incorrect password." in response.text


async def test_correct_password_sets_a_cookie_and_redirects(client, admin_password: str) -> None:
    response = await client.post(
        "/admin/login", data={"password": admin_password}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"

    cookie = response.headers.get("set-cookie", "")
    assert admin.COOKIE in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    # The password itself must never appear in the cookie — only an HMAC of the timestamp.
    assert admin_password not in cookie


async def test_login_throttles_repeated_failures(client) -> None:
    """
    The previous implementation had no lockout at all, leaving a single shared password as
    the whole of the defence against online guessing.
    """
    admin._attempts.clear()
    statuses = [
        (await client.post("/admin/login", data={"password": f"guess-{n}"})).status_code
        for n in range(8)
    ]
    assert 429 in statuses, statuses
    admin._attempts.clear()


# ----------------------------------------------------- mutations need auth


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/admin/keys", {"name": "sneaky", "rate_limit": 60}),
        ("post", "/admin/keys/some-id/revoke", {}),
        ("post", "/admin/tags", {"label": "Sneaky", "description": "d"}),
        ("post", "/admin/tags/alcohol/retire", {}),
        ("post", "/admin/tags/alcohol/restore", {}),
    ],
)
async def test_mutations_reject_an_unauthenticated_caller(client, method, path, payload) -> None:
    """
    Each of these is independently reachable. Guarding only the page that renders the form
    would leave them callable by anyone who knows the URL.
    """
    response = await getattr(client, method)(path, data=payload)
    assert response.status_code == 403


@needs_db
async def test_mutations_reject_a_missing_csrf_token(client, admin_password: str) -> None:
    """
    Server Actions carried an implicit origin check that plain form POSTs do not, so the port
    would otherwise open a hole that did not exist before.
    """
    response = await client.post(
        "/admin/keys",
        data={"name": "no-csrf", "rate_limit": 60},
        cookies={admin.COOKIE: forge(str(now_ms()), admin_password)},
    )
    assert response.status_code == 403


@needs_db
async def test_the_calibration_tab_is_gone(client, admin_password: str) -> None:
    """
    It reported a constant, so it went.

    Nothing could write `tag_thresholds` -- `inference/calibrate.py` and
    `dooh apply-calibration` went with the ranking model -- so the panel showed 0 of 25
    measured and could show nothing else. THE RULE IT DISPLAYED IS UNTOUCHED: `calibrated`
    is still on every API response and every verdict card, which is where AGENTS.md rule 4
    actually bites. See tests/test_calibration.py.

    The stale-bookmark case is the second half. `?tab=calibration` is no longer in
    `ADMIN_TABS`, so admin.py falls it back to keys rather than rendering a page with every
    panel hidden -- which is what an unrecognised tab used to do.
    """
    for url in ("/admin", "/admin?tab=calibration"):
        page = await client.get(
            url, cookies={admin.COOKIE: forge(str(now_ms()), admin_password)}
        )
        assert page.status_code == 200, url
        assert 'id="panel-calibration"' not in page.text, url
        assert 'data-tab="calibration"' not in page.text, url
        # Fell back to a VISIBLE panel rather than rendering every one of them hidden:
        # the keys div carries no `hidden` attribute only when keys is the selected tab.
        assert '<div id="panel-keys" role="tabpanel" >' in page.text, url


@needs_db
async def test_create_key_rejects_an_empty_name(client, admin_password: str) -> None:
    response = await client.post(
        "/admin/keys",
        data={"name": "   ", "rate_limit": 60},
        cookies={admin.COOKIE: forge(str(now_ms()), admin_password)},
        headers={"x-csrf-token": admin.csrf_token()},
    )
    # A user-fixable problem comes back as 200 with the message beside the field, not as a
    # 4xx that HTMX would drop on the floor.
    assert response.status_code == 200
    assert "Give the key a name." in response.text


@needs_db
@pytest.mark.parametrize("limit", [0, 10_001])
async def test_create_key_rejects_an_out_of_range_limit(client, admin_password, limit) -> None:
    response = await client.post(
        "/admin/keys",
        data={"name": "ok", "rate_limit": limit},
        cookies={admin.COOKIE: forge(str(now_ms()), admin_password)},
        headers={"x-csrf-token": admin.csrf_token()},
    )
    assert "between 1 and 10000" in response.text


# ------------------------------------------------------- the shell rail's health dot
