.PHONY: install dev serve-lan test test-fast lint fmt typecheck check inference-smoke clean migrate migrate-sql

VENV := .venv
PY   := $(VENV)/bin/python
PORT ?= 8000

# The env file to load. .env is the documented name; .env.local is what this checkout
# already had, so both work without anyone having to rename anything.
ENV_FILE := $(firstword $(wildcard .env) $(wildcard .env.local))

# First non-loopback IPv4 address, for the "open this on your phone" line below. Override it
# when the guess picks the wrong interface, or to keep the banner stable across DHCP leases:
#
#     make serve-lan LAN_IP=192.168.1.24
#
# This only labels the address; the bind is --host 0.0.0.0 either way.
LAN_IP ?= $(shell ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || \
                  hostname -I 2>/dev/null | awk '{print $$1}')

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e ".[dev]"

# Localhost only. This is the right default: a dev server with --reload should not be
# listening on the network by accident.
dev:
	$(VENV)/bin/uvicorn tagverify.main:app --reload --env-file $(ENV_FILE) --port $(PORT)

# Reachable from other machines on the same network.
#
# --host 0.0.0.0 binds every interface instead of just the loopback. Note there is
# deliberately NO --proxy-headers here: that flag makes uvicorn trust X-Forwarded-For, which
# with direct LAN access would let any device on the network spoof its IP and walk past the
# per-IP playground rate limit. Add it only when there is a real reverse proxy in front,
# together with --forwarded-allow-ips set to that proxy's address.
#
# --reload, same as `dev`. This is a development command — production is the Dockerfile CMD —
# and without it the process goes stale UNEVENLY, which is the part that costs time: Jinja
# re-reads templates from disk per render and StaticFiles ignores the ?v= query, so the page
# keeps updating while the Python stays frozen at startup. The result looks live and is not,
# and the symptom always points somewhere other than the cause. This is not an invitation to
# add --proxy-headers; that one stays off for the reason above.
serve-lan:
	@echo ""
	@echo "  Serving on all interfaces."
	@echo "  This machine:   http://127.0.0.1:$(PORT)"
	@echo "  Other devices:  http://$(LAN_IP):$(PORT)"
	@echo ""
	@echo "  Anyone on this network can now reach the playground and /admin."
	@echo ""
	$(VENV)/bin/uvicorn tagverify.main:app --reload --host 0.0.0.0 --port $(PORT) --env-file $(ENV_FILE)

test:
	$(PY) -m pytest

# Bring an EXISTING database up to date. A new one is provisioned from docs/schema.sql
# instead — that file is the destination, these revisions are the route to it.
migrate:
	$(VENV)/bin/alembic upgrade head

# What `migrate` WOULD run, as SQL, without touching anything. Worth reading before a
# production run: a migration you have not read is a migration you are trusting blind.
migrate-sql:
	$(VENV)/bin/alembic upgrade head --sql

# The decision rules alone. No database, no model, no network — the fastest useful signal
# in the project, and the one to run while editing banding.py or aggregate.py.
test-fast:
	$(PY) -m pytest tests/test_banding.py tests/test_intake.py \
		tests/test_aggregate.py tests/test_video.py tests/test_templating.py \
		tests/test_decision_version.py

# All of inference/, not just banding.py. ruff is static, so it needs none of the ML wheels
# installed in this venv — and the 2,200 lines it used to skip are where the least-reviewed
# code in the repo lives. The exclusions are in pyproject.toml.
lint:
	$(VENV)/bin/ruff check tagverify/ tests/ inference/

fmt:
	$(VENV)/bin/ruff check --fix tagverify/ tests/ inference/
	$(VENV)/bin/ruff format tagverify/ tests/ inference/

# [tool.mypy] has been configured since the rewrite with nothing to invoke it. Deliberately
# not part of `check` yet: it is not clean, and wiring it in before it is would just teach
# everyone to ignore a red `check`.
typecheck:
	$(VENV)/bin/mypy tagverify/

check: lint test

# Proves that extracting banding.py changed no ML behaviour. Needs inference/.venv, which is
# separate from the web app's on purpose.
inference-smoke:
	cd inference && ./.venv/bin/python smoke_test.py

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
