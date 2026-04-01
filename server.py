"""
server.py — Python control plane for the RTMP → RTSP relay.

Responsibilities:
  • Launch mediamtx as a supervised child process (auto-restarts on crash).
  • Expose a lightweight HTTP API on :8080 for:
      GET  /healthz          — liveness probe
      GET  /metrics          — stream / session counts (Prometheus text)
      GET  /streams          — list active RTMP publishers
      GET  /clients          — list active RTSP sessions
      POST /on_publish       — optional auth webhook (called by mediamtx)
      POST /on_read          — optional auth webhook (called by mediamtx)
  • Periodically poll the mediamtx REST API and log stats.

Design goals:
  • MediaMTX does all media work (zero-copy RTP fan-out, no transcoding).
  • Python never touches the media path — it only manages the process and
    answers management HTTP requests.
  • asyncio + aiohttp → single thread, no GIL contention.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Any

import aiohttp
from aiohttp import web

# ── Configuration (override via environment variables) ───────────────────────
MEDIAMTX_BIN     = os.getenv("MEDIAMTX_BIN", "/usr/local/bin/mediamtx")
MEDIAMTX_CFG     = os.getenv("MEDIAMTX_CFG", "/etc/mediamtx/mediamtx.yml")
MEDIAMTX_API     = os.getenv("MEDIAMTX_API", "http://127.0.0.1:9997")
CONTROL_PORT     = int(os.getenv("CONTROL_PORT", "8080"))
POLL_INTERVAL    = float(os.getenv("POLL_INTERVAL", "10"))   # seconds
RESTART_DELAY    = float(os.getenv("RESTART_DELAY", "2"))    # seconds between restarts

# Optional bearer token to protect the control HTTP API.
# Leave empty to disable authentication (fine for internal/Docker use).
API_TOKEN        = os.getenv("API_TOKEN", "")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("rtmp-rtsp")

# ── Shared state (written by background tasks, read by HTTP handlers) ─────────
_state: dict[str, Any] = {
    "mediamtx_pid": None,
    "mediamtx_restarts": 0,
    "streams": [],
    "sessions": [],
    "last_poll": 0.0,
}

# ── Prometheus-style metrics counters ─────────────────────────────────────────
_metrics: dict[str, int | float] = {
    "active_publishers": 0,
    "active_readers":    0,
    "total_restarts":    0,
}


# ── Auth helper ───────────────────────────────────────────────────────────────

def _check_token(request: web.Request) -> bool:
    """Return True if the API token check passes (or auth is disabled)."""
    if not API_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {API_TOKEN}"


def _require_auth(handler):
    """Decorator: reject requests that fail token auth."""
    async def wrapper(request: web.Request) -> web.Response:
        if not _check_token(request):
            return web.Response(status=401, text="Unauthorized")
        return await handler(request)
    return wrapper


# ── mediamtx API client ───────────────────────────────────────────────────────

async def _mtx_get(session: aiohttp.ClientSession, path: str) -> dict | list | None:
    """GET from the mediamtx REST API; return parsed JSON or None on error."""
    url = f"{MEDIAMTX_API}{path}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as exc:
        log.debug("mediamtx API unreachable (%s): %s", path, exc)
    return None


async def _poll_mediamtx(session: aiohttp.ClientSession) -> None:
    """Poll mediamtx REST API and update shared state."""
    paths_data    = await _mtx_get(session, "/v3/paths/list")
    sessions_data = await _mtx_get(session, "/v3/rtspsessions/list")

    if paths_data:
        items = paths_data.get("items", [])
        _state["streams"] = [
            {
                "name":    p.get("name"),
                "source":  p.get("source", {}).get("type"),
                "readers": p.get("readers", 0),
            }
            for p in items
            if p.get("ready")
        ]
        _metrics["active_publishers"] = len(_state["streams"])

    if sessions_data:
        items = sessions_data.get("items", [])
        _state["sessions"] = [
            {
                "id":        s.get("id"),
                "remoteAddr": s.get("remoteAddr"),
                "state":     s.get("state"),
                "path":      s.get("path"),
            }
            for s in items
        ]
        _metrics["active_readers"] = len(_state["sessions"])

    _state["last_poll"] = time.time()
    log.info(
        "publishers=%d  rtsp_sessions=%d  restarts=%d",
        _metrics["active_publishers"],
        _metrics["active_readers"],
        _metrics["total_restarts"],
    )


# ── mediamtx process supervisor ───────────────────────────────────────────────

async def _run_mediamtx() -> None:
    """
    Supervise mediamtx: start it, restart on crash, respond to SIGTERM/SIGINT
    by forwarding the signal and waiting for clean exit.
    """
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal():
        log.info("Received shutdown signal — stopping mediamtx…")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    async with aiohttp.ClientSession() as api_session:
        while not stop_event.is_set():
            log.info("Starting mediamtx (bin=%s cfg=%s)", MEDIAMTX_BIN, MEDIAMTX_CFG)
            try:
                proc = await asyncio.create_subprocess_exec(
                    MEDIAMTX_BIN,
                    MEDIAMTX_CFG,
                    # Inherit stdout/stderr so Docker captures mediamtx logs
                    stdout=None,
                    stderr=None,
                )
            except FileNotFoundError:
                log.critical("mediamtx binary not found at %s — exiting", MEDIAMTX_BIN)
                sys.exit(1)

            _state["mediamtx_pid"] = proc.pid
            log.info("mediamtx PID=%d", proc.pid)

            # Background poller task
            async def _poll_loop():
                await asyncio.sleep(3)  # Give mediamtx a moment to start its API
                while not stop_event.is_set():
                    await _poll_mediamtx(api_session)
                    await asyncio.sleep(POLL_INTERVAL)

            poll_task = asyncio.create_task(_poll_loop())

            # Wait for process exit or shutdown signal
            wait_task   = asyncio.create_task(proc.wait())
            stop_task   = asyncio.create_task(stop_event.wait())
            done, _     = await asyncio.wait(
                {wait_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass

            if stop_event.is_set():
                # Graceful shutdown: forward SIGTERM
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        log.warning("mediamtx did not stop in time — sending SIGKILL")
                        proc.kill()
                        await proc.wait()
                log.info("mediamtx stopped cleanly")
                break

            # Unexpected exit — restart
            rc = proc.returncode
            _metrics["total_restarts"] += 1
            _state["mediamtx_restarts"] = _metrics["total_restarts"]
            log.warning(
                "mediamtx exited with code %d — restart #%d in %.1fs",
                rc,
                _metrics["total_restarts"],
                RESTART_DELAY,
            )
            await asyncio.sleep(RESTART_DELAY)


# ── HTTP handlers ──────────────────────────────────────────────────────────────

async def handle_healthz(request: web.Request) -> web.Response:
    pid = _state.get("mediamtx_pid")
    if pid is None:
        return web.Response(status=503, text="mediamtx not running")
    return web.Response(text="ok")


@_require_auth
async def handle_streams(request: web.Request) -> web.Response:
    return web.json_response({"streams": _state["streams"]})


@_require_auth
async def handle_clients(request: web.Request) -> web.Response:
    return web.json_response({"sessions": _state["sessions"]})


async def handle_metrics(request: web.Request) -> web.Response:
    lines = [
        "# HELP rtmp_rtsp_active_publishers Number of active RTMP publishers",
        "# TYPE rtmp_rtsp_active_publishers gauge",
        f"rtmp_rtsp_active_publishers {_metrics['active_publishers']}",
        "# HELP rtmp_rtsp_active_readers Number of active RTSP readers",
        "# TYPE rtmp_rtsp_active_readers gauge",
        f"rtmp_rtsp_active_readers {_metrics['active_readers']}",
        "# HELP rtmp_rtsp_total_restarts Total mediamtx process restarts",
        "# TYPE rtmp_rtsp_total_restarts counter",
        f"rtmp_rtsp_total_restarts {_metrics['total_restarts']}",
        "",
    ]
    return web.Response(
        text="\n".join(lines),
        content_type="text/plain",
    )


@_require_auth
async def handle_on_publish(request: web.Request) -> web.Response:
    """
    Webhook called by mediamtx runOnPublish.
    Return 200 to allow, 401/403 to reject the publisher.
    """
    data = await request.post()
    path = data.get("path", "")
    source_type = data.get("sourceType", "")
    log.info("publish started: path=%s source=%s", path, source_type)
    # Add your custom auth / allow-list logic here.
    return web.Response(text="ok")


@_require_auth
async def handle_on_read(request: web.Request) -> web.Response:
    """
    Webhook called by mediamtx runOnRead.
    Return 200 to allow, 401/403 to reject the reader.
    """
    data = await request.post()
    path = data.get("path", "")
    log.info("read started: path=%s", path)
    # Add your custom auth / allow-list logic here.
    return web.Response(text="ok")


# ── Application setup ──────────────────────────────────────────────────────────

def _build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz",       handle_healthz)
    app.router.add_get("/metrics",       handle_metrics)
    app.router.add_get("/streams",       handle_streams)
    app.router.add_get("/clients",       handle_clients)
    app.router.add_post("/on_publish",   handle_on_publish)
    app.router.add_post("/on_read",      handle_on_read)
    return app


async def _main() -> None:
    # Start HTTP control server
    app     = _build_app()
    runner  = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", CONTROL_PORT)
    await site.start()
    log.info("Control plane listening on :%d", CONTROL_PORT)

    # Run mediamtx supervisor (returns when shutdown signal received)
    await _run_mediamtx()

    await runner.cleanup()
    log.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(_main())
