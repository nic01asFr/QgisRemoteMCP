"""
BigQgisMCP — REST API Server
═══════════════════════════════════════════════════════════════════

FastAPI server that communicates with QGIS via UNIX socket.
Provides:
  - /health endpoint for Docker healthcheck
  - /api/* endpoints for external programmatic access
  - /vnc redirect for convenience

Runs on port 8080.
"""

import json
import socket
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
import uvicorn

SOCKET_PATH = "/tmp/qgis_bridge.sock"
SOCKET_TIMEOUT = 30  # seconds

app = FastAPI(
    title="BigQgisMCP API",
    description="REST API for QGIS Desktop control",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def send_command(action: str, params: dict = None) -> dict:
    """Send a command to QGIS bridge via UNIX socket."""
    if not os.path.exists(SOCKET_PATH):
        raise HTTPException(503, "QGIS bridge not ready (socket not found)")

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT)
        sock.connect(SOCKET_PATH)

        request = json.dumps({"action": action, "params": params or {}})
        sock.sendall(request.encode())
        sock.shutdown(socket.SHUT_WR)

        # Read response
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk

        sock.close()
        return json.loads(data.decode())

    except socket.timeout:
        raise HTTPException(504, f"QGIS bridge timeout ({SOCKET_TIMEOUT}s)")
    except ConnectionRefusedError:
        raise HTTPException(503, "QGIS bridge connection refused")
    except Exception as e:
        raise HTTPException(500, f"Bridge communication error: {str(e)}")


# ── Health ────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Healthcheck endpoint for Docker and monitoring."""
    try:
        result = send_command("health")
        return {"api": "ok", "qgis": result}
    except Exception:
        # API is up but QGIS may not be ready yet
        return JSONResponse(
            status_code=503,
            content={"api": "ok", "qgis": "not_ready"}
        )


# ── Generic command endpoint ──────────────────────────────────────

@app.post("/api/command")
async def command(body: dict):
    """Send any command to QGIS bridge."""
    action = body.get("action", "")
    params = body.get("params", {})
    if not action:
        raise HTTPException(400, "Missing 'action' field")
    return send_command(action, params)


# ── Convenience endpoints ────────────────────────────────────────

@app.get("/api/project")
async def get_project():
    return send_command("get_project_info")


@app.get("/api/layers")
async def list_layers():
    return send_command("list_layers")


@app.get("/api/screenshot")
async def screenshot(width: int = 800, height: int = 600, format: str = "png"):
    return send_command("screenshot", {"width": width, "height": height, "format": format})


@app.post("/api/execute")
async def execute_python(body: dict):
    code = body.get("code", "")
    if not code:
        raise HTTPException(400, "Missing 'code' field")
    return send_command("execute_python", {"code": code})


@app.post("/api/processing")
async def run_processing(body: dict):
    algorithm = body.get("algorithm", "")
    parameters = body.get("parameters", {})
    if not algorithm:
        raise HTTPException(400, "Missing 'algorithm' field")
    return send_command("run_processing", {
        "algorithm": algorithm,
        "parameters": parameters,
    })


@app.get("/api/algorithms")
async def list_algorithms(search: str = "", provider: str = "", limit: int = 50):
    return send_command("list_algorithms", {
        "search": search,
        "provider": provider,
        "limit": limit,
    })


# ── VNC redirect ──────────────────────────────────────────────────

@app.get("/vnc")
async def vnc_redirect():
    """Redirect to noVNC interface."""
    return RedirectResponse(url="http://localhost:6080/vnc.html?autoconnect=true&resize=scale")


# ── Run ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Wait for QGIS bridge socket
    print("[API Server] Waiting for QGIS bridge...")
    for i in range(60):
        if os.path.exists(SOCKET_PATH):
            print(f"[API Server] Bridge found after {i}s")
            break
        time.sleep(1)
    else:
        print("[API Server] WARNING: Bridge socket not found, starting anyway")

    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
