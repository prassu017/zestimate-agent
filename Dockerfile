# syntax=docker/dockerfile:1.6
#
# Multi-stage build for zestimate-agent.
#
#   builder stage  — creates a venv with all deps + the package wheel
#   runtime stage  — python:3.12-slim-bookworm with just the venv copied over
#
# Final image: ~150MB, non-root user, no build toolchain, entrypoint = API.
# Override CMD for the CLI:
#
#   docker run --rm -e UNBLOCKER_API_KEY=... zestimate-agent \
#     zestimate lookup "123 Main St, Seattle, WA 98101"

ARG PYTHON_VERSION=3.12

# ─── Stage 1: builder ───────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv

# Build deps required by some wheels (selectolax, lxml transitively).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv we can copy into the runtime stage cleanly.
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /build

# Copy only the metadata first so pip install caches across code changes.
COPY pyproject.toml README.md ./
COPY src ./src

# Install the package with the api extra — this is the production target.
RUN pip install --upgrade pip && \
    pip install ".[api]"


# ─── Stage 2: runtime ───────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv \
    LOG_FORMAT=json \
    API_HOST=0.0.0.0 \
    API_PORT=8000

# Runtime-only system packages. curl is for the HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root user.
RUN groupadd --system app && useradd --system --gid app --home /app app

# Copy the venv built in stage 1.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# .cache is writable for the sqlite result cache + rentcast usage counter.
RUN mkdir -p /app/.cache && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# tini reaps zombie children from httpx subprocesses cleanly on SIGTERM.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "zestimate_agent.api:create_app", \
     "--factory", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--no-access-log"]
