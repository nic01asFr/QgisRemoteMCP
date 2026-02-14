# CLAUDE.md — BigQgisMCP

## Project

BigQgisMCP exposes a full QGIS Desktop instance as an MCP Server.
Single Docker container with QGIS GUI + Xvfb + noVNC + MCP Server.

## Architecture

```
Container (single)
  supervisord
  ├── Xvfb :99 (virtual display)
  ├── fluxbox (window manager)
  ├── QGIS Desktop (GUI, PyQGIS bridge via startup script)
  ├── x11vnc → websockify/noVNC (:6080)
  ├── api_server.py (FastAPI REST :8080)
  ├── main_mcp.py (MCP Server :8100)
  └── stream_server.py (MJPEG :8081)
```

Communication: MCP Server → UNIX socket → QGIS Bridge (runs inside QGIS)

## Key files

- `main_mcp.py` — MCP Server with all tools, resources, prompts
- `src/qgis_bridge.py` — Runs inside QGIS, UNIX socket listener
- `src/api_server.py` — FastAPI REST wrapper
- `src/stream_server.py` — MJPEG stream
- `skills/*.md` — MCP Resources (PyQGIS, Processing, cartography, etc.)
- `Dockerfile` — Single container build
- `supervisord.conf` — Process orchestration
- `entrypoint.sh` — Container startup

## Build & Run

```bash
docker compose up -d --build
# MCP: http://localhost:8100/mcp
# VNC: http://localhost:6080
# API: http://localhost:8080
```

## Development

Source files are mounted as volumes in dev. Edit locally, restart container:
```bash
docker compose restart bigqgismcp
```

QGIS bridge changes require full restart (loaded at QGIS startup).

## External services

Vision backends (Moondream, SAMGeo3, DepthPro) run separately.
Configured via env vars, called from PyQGIS scripts via HTTP.

## Testing

```bash
# Health
curl http://localhost:8080/health

# Screenshot
curl http://localhost:8080/api/screenshot

# Execute Python
curl -X POST http://localhost:8080/api/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "result[\"version\"] = Qgis.version()"}'
```
