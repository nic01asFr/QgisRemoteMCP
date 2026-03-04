"""
QgisRemoteMCP — REST API Server
═══════════════════════════════════════════════════════════════════

FastAPI server that communicates with QGIS via UNIX socket.
Provides:
  - /health endpoint for Docker healthcheck
  - /api/* endpoints for external programmatic access
  - /vnc redirect for convenience

Runs on port 8080.
"""

import json
import re
import socket
import subprocess
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
import uvicorn

SOCKET_PATH = "/tmp/qgis_bridge.sock"
SOCKET_TIMEOUT = 30  # seconds

app = FastAPI(
    title="QgisRemoteMCP API",
    description="REST API for QGIS Desktop control",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def send_command(action: str, params: dict = None, timeout: int = None) -> dict:
    """Send a command to QGIS bridge via UNIX socket."""
    if not os.path.exists(SOCKET_PATH):
        raise HTTPException(503, "QGIS bridge not ready (socket not found)")

    effective_timeout = timeout or SOCKET_TIMEOUT
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(effective_timeout)
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
        raise HTTPException(504, f"QGIS bridge timeout ({effective_timeout}s)")
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
    user_timeout = body.get("timeout", 30)
    return send_command("execute_python",
                        {"code": code, "timeout": user_timeout},
                        timeout=user_timeout + 30)


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


# ── File management ──────────────────────────────────────────────

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB

def _validate_filename(name: str) -> str:
    """Sanitize filename — reject path traversal and unsafe characters."""
    if not name or not name.strip():
        raise HTTPException(400, "Empty filename")
    basename = name.replace("\\", "/").split("/")[-1]
    if ".." in basename or basename.startswith("."):
        raise HTTPException(400, f"Unsafe filename: {basename}")
    if not re.match(r'^[\w\-. ()\[\]]+$', basename):
        raise HTTPException(400, f"Invalid characters in filename: {basename}")
    return basename


@app.get("/api/files")
async def list_files(directory: str = "/data", pattern: str = "*"):
    """List files in /data/."""
    allowed = ["/data"]
    if directory not in allowed:
        raise HTTPException(400, f"Directory must be /data")
    if not os.path.isdir(directory):
        return {"files": [], "count": 0}
    results = []
    for fpath in sorted(Path(directory).glob(pattern)):
        if fpath.is_file():
            stat = fpath.stat()
            results.append({
                "name": fpath.name, "path": str(fpath),
                "size": stat.st_size, "modified": int(stat.st_mtime),
                "suffix": fpath.suffix,
            })
    return {"files": results, "count": len(results)}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file to /data/ via multipart form."""
    name = _validate_filename(file.filename or "upload")
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"File too large: {len(content)} bytes (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)")
    dest = Path("/data") / name
    dest.write_bytes(content)
    return {"success": True, "name": name, "path": str(dest), "size": len(content)}


@app.get("/api/files/{filename}")
async def download_file(filename: str):
    """Download a file from /data/."""
    name = _validate_filename(filename)
    fpath = Path("/data") / name
    if not fpath.exists():
        raise HTTPException(404, f"File not found: {name}")
    return FileResponse(
        path=str(fpath),
        filename=name,
        media_type="application/octet-stream",
    )


@app.delete("/api/files/{filename}")
async def delete_file(filename: str):
    """Delete a file from /data/."""
    name = _validate_filename(filename)
    fpath = Path("/data") / name
    if not fpath.exists():
        raise HTTPException(404, f"File not found: {name}")
    fpath.unlink()
    return {"success": True, "deleted": name}


# ── X11 Input (xdotool) ──────────────────────────────────────────

DISPLAY = os.environ.get("DISPLAY", ":99")
# Parse resolution from env (e.g. "1920x1080x24")
_res = os.environ.get("QGIS_RESOLUTION", "1920x1080x24").split("x")
DISPLAY_W = int(_res[0])
DISPLAY_H = int(_res[1])
ALLOWED_KEY_RE = re.compile(r'^[a-zA-Z0-9_+\- ]+$')


def _xdotool(*args):
    """Run xdotool with the correct DISPLAY."""
    env = {**os.environ, "DISPLAY": DISPLAY}
    subprocess.run(["xdotool", *args], env=env, timeout=2, check=True)


def _clamp_coords(body: dict) -> tuple:
    """Extract and clamp x,y from body to display bounds."""
    x = max(0, min(int(body["x"]), DISPLAY_W))
    y = max(0, min(int(body["y"]), DISPLAY_H))
    return x, y


@app.post("/api/input")
async def send_input(body: dict):
    """Send mouse/keyboard input to X11 display via xdotool."""
    event = body.get("type", "")
    try:
        if event == "click":
            x, y = _clamp_coords(body)
            btn = max(1, min(int(body.get("button", 1)), 3))
            _xdotool("mousemove", "--screen", "0", str(x), str(y),
                     "click", str(btn))

        elif event == "dblclick":
            x, y = _clamp_coords(body)
            _xdotool("mousemove", "--screen", "0", str(x), str(y),
                     "click", "--repeat", "2", "--delay", "50", "1")

        elif event == "mousedown":
            x, y = _clamp_coords(body)
            btn = max(1, min(int(body.get("button", 1)), 3))
            _xdotool("mousemove", "--screen", "0", str(x), str(y),
                     "mousedown", str(btn))

        elif event == "mouseup":
            x, y = _clamp_coords(body)
            btn = max(1, min(int(body.get("button", 1)), 3))
            _xdotool("mousemove", "--screen", "0", str(x), str(y),
                     "mouseup", str(btn))

        elif event == "mousemove":
            x, y = _clamp_coords(body)
            _xdotool("mousemove", "--screen", "0", str(x), str(y))

        elif event == "scroll":
            x, y = _clamp_coords(body)
            direction = body.get("direction", "down")
            clicks = max(1, min(int(body.get("clicks", 3)), 10))
            button = "5" if direction == "down" else "4"
            _xdotool("mousemove", "--screen", "0", str(x), str(y),
                     "click", "--repeat", str(clicks), "--delay", "20", button)

        elif event == "key":
            key = body.get("key", "")
            if not key or not ALLOWED_KEY_RE.match(key):
                raise HTTPException(400, f"Invalid key: {key}")
            _xdotool("key", key)

        elif event == "type":
            text = body.get("text", "")
            if len(text) > 200:
                raise HTTPException(400, "Text too long")
            _xdotool("type", "--delay", "20", "--", text)

        else:
            raise HTTPException(400, f"Unknown event: {event}")

        return {"ok": True}

    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"xdotool error: {e}")
    except (ValueError, KeyError) as e:
        raise HTTPException(400, f"Invalid input: {e}")


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
