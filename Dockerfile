# The web application only. `inference/` is deployed separately to a Hugging Face Space and
# must never be installed here — it would drag ~2GB of torch into an image whose job is
# serving HTTP.
#
# Two files are copied in as exceptions: inference/banding.py, because the API tier shares the
# verdict rule with the model service, and inference/versioning.py, because dooh/cli.py imports
# it at module scope and without it the `dooh` console script cannot start in this image. Both
# import nothing but stdlib, so they cost nothing. Do not "tidy" either path away.

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# psycopg[binary] ships wheels, so no libpq-dev and no compiler are needed.
COPY pyproject.toml README.md ./
COPY dooh ./dooh
COPY inference/__init__.py inference/banding.py inference/versioning.py ./inference/

RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir .


FROM python:3.12-slim

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Never run as root. The app writes nothing to disk — images are held in memory and only
# their hashes are persisted — so a read-only filesystem works fine too.
RUN useradd --create-home --uid 10001 dooh
WORKDIR /app

COPY --from=build /venv /venv
COPY --from=build /app/dooh ./dooh
COPY --from=build /app/inference ./inference

USER dooh
EXPOSE 8000

# --proxy-headers so request.url.scheme is the client's, not the proxy hop's. The admin
# session cookie's Secure flag is taken from it; without this, cookies behind a TLS
# terminator would be issued without Secure. Set --forwarded-allow-ips to your proxy.
CMD ["uvicorn", "dooh.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
