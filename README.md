# RTMP → RTSP Server

Receives a single RTMP stream and re-serves it to **up to 100+ concurrent RTSP clients** with minimal latency and **zero transcoding** (when the source codec is H.264 + AAC, which is the RTMP standard).

## Architecture

```
OBS / encoder
     │  RTMP  (port 1935)
     ▼
┌─────────────────────────────────────────────┐
│  Docker container                           │
│                                             │
│  ┌──────────────────────────────────────┐   │
│  │  MediaMTX  (Go binary)               │   │
│  │  • RTMP server  :1935                │   │
│  │  • RTSP server  :8554                │   │
│  │  • REST API     :9997  (internal)    │   │
│  │  • Zero-copy RTP fan-out             │   │
│  └──────────────────────────────────────┘   │
│                                             │
│  ┌──────────────────────────────────────┐   │
│  │  Python control plane  :8080         │   │
│  │  • Supervises MediaMTX process       │   │
│  │  • /healthz  /metrics  /streams      │   │
│  │  • /on_publish  /on_read  (auth)     │   │
│  └──────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
     │  RTSP  (port 8554)
     ▼
RTSP clients (VLC, ffplay, cameras, …)
```

**Why MediaMTX?**  
MediaMTX re-packetizes H.264 NAL units from RTMP/FLV directly into RTP without decoding, keeping CPU use at ~2-5 % on a modern i5 for 100 sessions.

## Quick start

### Docker (recommended)

```bash
docker run -d \
  --name rtmp-rtsp \
  -p 1935:1935/tcp \
  -p 8554:8554/tcp \
  -p 8000:8000/udp \
  -p 8001:8001/udp \
  -p 8080:8080/tcp \
  --read-only \
  --tmpfs /tmp \
  ghcr.io/andrew-stclair/rtmp-to-rtsp-server:latest
```

Push your stream to `rtmp://<host>:1935/live/stream` (e.g. from OBS), then play:

```bash
ffplay rtsp://<host>:8554/live/stream
vlc    rtsp://<host>:8554/live/stream
```

Any stream key works — just keep it consistent between publisher and viewers.

### docker-compose

```yaml
services:
  rtmp-rtsp:
    image: ghcr.io/andrew-stclair/rtmp-to-rtsp-server:latest
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp
    ports:
      - "1935:1935/tcp"   # RTMP ingest
      - "8554:8554/tcp"   # RTSP output (TCP)
      - "8000:8000/udp"   # RTSP RTP (UDP)
      - "8001:8001/udp"   # RTSP RTCP (UDP)
      - "8080:8080/tcp"   # control plane (optional — don't expose publicly)
    environment:
      API_TOKEN: "changeme"   # protect /streams and /clients endpoints
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MEDIAMTX_BIN` | `/usr/local/bin/mediamtx` | Path to mediamtx binary |
| `MEDIAMTX_CFG` | `/etc/mediamtx/mediamtx.yml` | Path to mediamtx config |
| `CONTROL_PORT` | `8080` | Python HTTP control plane port |
| `POLL_INTERVAL` | `10` | Seconds between mediamtx API polls |
| `RESTART_DELAY` | `2` | Seconds to wait before restarting mediamtx |
| `API_TOKEN` | *(empty)* | Bearer token for `/streams`, `/clients` endpoints. Leave empty to disable auth. |

## Control plane endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `GET /healthz` | No | Liveness probe — 200 OK when mediamtx is running |
| `GET /metrics` | No | Prometheus text metrics |
| `GET /streams` | Token | Active RTMP publishers |
| `GET /clients` | Token | Active RTSP sessions |
| `POST /on_publish` | Token | Webhook for mediamtx publish events (add auth logic here) |
| `POST /on_read` | Token | Webhook for mediamtx read events |

## Custom stream-key auth

Uncomment the `runOnPublish` / `runOnRead` hooks in `mediamtx.yml` and add your logic to the `handle_on_publish` / `handle_on_read` functions in `server.py`.  Returning HTTP 4xx rejects the connection.

## Low-latency tuning tips

- Set GOP to 1–2 seconds in your encoder (OBS → Output → Keyframe Interval = 1).
- Prefer **UDP** RTSP transport on LAN (`vlc --rtsp-tcp` disables this — don't use it on LAN).
- `writeQueueSize: 128` is already set in `mediamtx.yml` (default is 512).
- Typical end-to-end latency: **< 500 ms**.

## Security notes

- Container runs as **non-root UID/GID 10001**.
- Filesystem is **read-only** by default (`--read-only`); `/tmp` is a tmpfs.
- MediaMTX API (`:9997`) and Prometheus metrics (`:9998`) are **not** exposed outside the container.
- Only HLS, WebRTC, SRT, and RTSPS are disabled in `mediamtx.yml` to reduce attack surface.

## Building locally

```bash
docker build -t rtmp-rtsp-server .
docker run --rm -p 1935:1935/tcp -p 554:8554/tcp -p 554:8000/udp -p 8080:8080/tcp \
  --read-only --tmpfs /tmp rtmp-rtsp-server
```
