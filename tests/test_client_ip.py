"""
Who a per-IP limit is charged to, and why the RIGHTMOST forwarded entry is the only safe one.

`client_ip` feeds both throttles — the playground's 20/min (tagverify/auth/deps.py) and the
admin login lockout (tagverify/auth/admin.py). Behind a reverse proxy `request.client` is the
proxy for every caller, which does not merely weaken them:

  - the playground budget becomes one bucket for the whole internet, and
  - the admin lockout INVERTS into a denial of service, because tagverify/web/admin.py returns
    429 before the password is checked -- so five wrong guesses from any anonymous visitor
    would lock every admin out, renewably, forever.

The fix is TRUST_PROXY_HOPS. These tests pin both halves of it: that the default changes
nothing at all, and that when it is on, a caller cannot choose their own identity.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from tagverify.auth import deps
from tagverify.config import Settings

REAL = "203.0.113.7"  # what our edge actually saw
FORGED = "198.51.100.99"  # what the caller put in the header themselves
EDGE = "10.1.2.3"  # the proxy's own address, i.e. the socket peer


def request_with(peer: str | None, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 54321) if peer else None,
        }
    )


@pytest.fixture
def hops(monkeypatch):
    """Set TRUST_PROXY_HOPS without touching the process-wide lru_cache on settings()."""

    def _set(n: int):
        monkeypatch.setattr(deps, "settings", lambda: Settings(trust_proxy_hops=n))

    return _set


# --- hops = 0: the default, and it must be indistinguishable from the old behaviour --------


def test_default_returns_the_socket_peer(hops):
    hops(0)
    assert deps.client_ip(request_with(REAL)) == REAL


def test_default_ignores_x_forwarded_for_entirely(hops):
    """On a LAN any device can forge this header, so at hops=0 it must not be read at all."""
    hops(0)
    assert deps.client_ip(request_with(EDGE, f"{FORGED}, {REAL}")) == EDGE


def test_default_without_a_client_is_unknown(hops):
    hops(0)
    assert deps.client_ip(request_with(None)) == "unknown"


def test_zero_is_the_shipped_default():
    """Nothing changes for an existing deployment that sets no new variable."""
    assert Settings().trust_proxy_hops == 0


# --- hops > 0: the entry a caller cannot forge ---------------------------------------------


def test_one_hop_takes_the_rightmost_not_the_leftmost(hops):
    """
    The whole point. Each proxy APPENDS what it saw, so the last entry is our own edge's
    observation; everything left of it is attacker-supplied. uvicorn's --proxy-headers takes
    the leftmost, which is why that flag alone is not enough.
    """
    hops(1)
    assert deps.client_ip(request_with(EDGE, f"{FORGED}, {REAL}")) == REAL


def test_a_forged_header_cannot_choose_the_identity(hops):
    """A caller spraying different leftmost values still lands in one bucket."""
    hops(1)
    seen = {
        deps.client_ip(request_with(EDGE, f"10.0.0.{n}, {REAL}"))
        for n in range(1, 20)
    }
    assert seen == {REAL}


def test_single_entry_chain(hops):
    hops(1)
    assert deps.client_ip(request_with(EDGE, REAL)) == REAL


def test_two_hops_takes_the_second_from_the_right(hops):
    hops(2)
    assert deps.client_ip(request_with(EDGE, f"{FORGED}, {REAL}, 10.9.9.9")) == REAL


def test_whitespace_and_empty_entries_are_tolerated(hops):
    hops(1)
    assert deps.client_ip(request_with(EDGE, f"  {FORGED} ,  {REAL}  , ")) == REAL


# --- configured for a proxy, reached without one -------------------------------------------


def test_missing_header_falls_back_to_the_peer(hops):
    hops(1)
    assert deps.client_ip(request_with(REAL)) == REAL


def test_empty_header_falls_back_to_the_peer(hops):
    hops(1)
    assert deps.client_ip(request_with(REAL, "")) == REAL


def test_chain_shorter_than_the_hop_count_does_not_raise(hops):
    """Take the leftmost we were actually given rather than indexing past the end."""
    hops(5)
    assert deps.client_ip(request_with(EDGE, f"{FORGED}, {REAL}")) == FORGED


# --- the behaviour the limits actually depend on -------------------------------------------


def test_two_callers_behind_one_proxy_are_told_apart(hops):
    """
    The property both throttles need. At hops=0 these collapse to one identity, which is the
    bug: the playground shares a single 20/min budget and either caller can lock the admin
    panel shut for the other.
    """
    hops(1)
    a = deps.client_ip(request_with(EDGE, "203.0.113.1"))
    b = deps.client_ip(request_with(EDGE, "203.0.113.2"))
    assert a != b


def test_trust_proxy_hops_reads_from_the_environment(monkeypatch):
    """The Railway variable has to actually arrive as an int."""
    monkeypatch.setenv("TRUST_PROXY_HOPS", "1")
    assert Settings().trust_proxy_hops == 1
