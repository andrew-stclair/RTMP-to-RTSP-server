# syntax=docker/dockerfile:1.7
###############################################################################
# Stage 1 — download & verify mediamtx
###############################################################################
FROM alpine:3.21 AS mediamtx-fetch

ARG MEDIAMTX_VERSION=v1.12.2
ARG TARGETARCH

# Install tools needed only for this stage
RUN apk add --no-cache curl tar

# Download the correct binary for the build platform
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64)   ARCH="amd64" ;; \
      arm64)   ARCH="arm64" ;; \
      arm)     ARCH="armv7" ;; \
      *)       echo "Unsupported arch: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    FILENAME="mediamtx_${MEDIAMTX_VERSION}_linux_${ARCH}.tar.gz"; \
    URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/${FILENAME}"; \
    curl -fsSL "${URL}" -o /tmp/mediamtx.tar.gz; \
    tar -xzf /tmp/mediamtx.tar.gz -C /tmp mediamtx; \
    chmod 0755 /tmp/mediamtx

###############################################################################
# Stage 2 — build Python deps into a virtual-env (no build tools in final image)
###############################################################################
FROM python:3.12-slim AS python-deps

WORKDIR /build

COPY requirements.txt .

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

###############################################################################
# Stage 3 — minimal runtime image
###############################################################################
FROM python:3.12-slim AS runtime

# ── Security: create a non-root user/group ────────────────────────────────────
RUN groupadd --gid 10001 appgroup && \
    useradd  --uid 10001 --gid appgroup --no-create-home --shell /sbin/nologin appuser

# ── Copy mediamtx binary ──────────────────────────────────────────────────────
COPY --from=mediamtx-fetch --chown=root:root --chmod=0755 \
     /tmp/mediamtx /usr/local/bin/mediamtx

# ── Copy Python virtual-env ───────────────────────────────────────────────────
COPY --from=python-deps /opt/venv /opt/venv

# ── Copy application files ────────────────────────────────────────────────────
COPY --chown=appuser:appgroup server.py healthcheck.py /app/

# ── Copy mediamtx config into a dedicated dir (mounted read-only) ─────────────
COPY --chown=root:appgroup --chmod=0640 mediamtx.yml /etc/mediamtx/mediamtx.yml

# ── Runtime directories that need to be writable ─────────────────────────────
# mediamtx writes nothing to disk by default; the only writable paths needed
# are /tmp (for process sockets) and potentially /var/log if logging to file.
# We create them here so they can be mounted as tmpfs in read-only mode.
RUN mkdir -p /tmp && chmod 1777 /tmp

# ── Drop all capabilities, run as non-root ────────────────────────────────────
USER appuser

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MEDIAMTX_BIN=/usr/local/bin/mediamtx \
    MEDIAMTX_CFG=/etc/mediamtx/mediamtx.yml \
    MEDIAMTX_API=http://127.0.0.1:9997 \
    CONTROL_PORT=8080 \
    POLL_INTERVAL=10 \
    RESTART_DELAY=2 \
    API_TOKEN=""

# ── Ports ──────────────────────────────────────────────────────────────────────
# 1935 — RTMP ingest
# 8554 — RTSP output
# 8080 — Python control-plane HTTP
# 9997 — mediamtx REST API (internal; only expose if needed)
# 9998 — mediamtx Prometheus metrics (internal)
EXPOSE 1935 8554 8080

WORKDIR /app

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python /app/healthcheck.py

ENTRYPOINT ["python", "/app/server.py"]
