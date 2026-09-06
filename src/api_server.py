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
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse, Response
import uvicorn

SOCKET_PATH = "/tmp/qgis_bridge.sock"
SOCKET_TIMEOUT = 30  # seconds

# ── Async job registry ──────────────────────────────────────────
# In-memory registry for async jobs submitted via POST /api/submit.
# A reader thread per job keeps the bridge socket open and streams newline-
# delimited frames (_ack, _heartbeat*, _result) into the registry. Clients
# poll GET /api/job/{id} which returns the current row plus derived
# heartbeat_age_s — when that exceeds ~10s the Qt main thread is probably
# frozen. Rows are evicted 1 h after they finish (4 h hard cap for any state).
JOBS_LOCK = threading.RLock()
JOBS: dict = {}
MAX_JOBS = 500
JOB_TTL_DONE_S = 3600       # 1 h after finished_at for terminal states
JOB_TTL_HARD_S = 4 * 3600   # 4 h hard cap even for still-running rows
MAX_CONCURRENT_ASYNC = 20   # guard against bridge connection exhaustion

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


@app.post("/api/restart_qgis")
async def restart_qgis():
    """Force le redémarrage du processus QGIS (kill PID → respawn par
    supervisord avec autorestart=true). Utilisé quand le bridge est
    figé/deadlocké et que l'agent détecte un état dégradé.

    Renvoie immédiatement (le respawn prend ~5-15s). Le client doit
    poller /health avant de retenter des appels QGIS.

    Sans auth : endpoint exposé en cluster-internal uniquement (port 8080
    écoute sur 0.0.0.0 mais l'ingress ne le route pas — service ClusterIP
    seulement, accessible depuis l'agent pod du même namespace).
    """
    import signal
    try:
        # Localiser le PID de qgis.bin sans dépendre du PID 32 (fragile)
        result = subprocess.run(
            ["pgrep", "-f", "qgis.bin"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
        if not pids:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "qgis.bin process not found"},
            )
        killed = []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except ProcessLookupError:
                pass
        return {
            "ok": True,
            "killed_pids": killed,
            "message": (
                f"Signaled SIGTERM to {len(killed)} qgis.bin process(es). "
                "Supervisord will respawn within ~5-15s. Poll /health."
            ),
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
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


# ── Async job endpoints ──────────────────────────────────────────

def _count_running_jobs() -> int:
    with JOBS_LOCK:
        return sum(1 for j in JOBS.values() if j.get("status") in ("queued", "running", "qt_frozen"))


def _evict_if_needed():
    """Drop oldest terminal rows when at cap. Assumes caller holds JOBS_LOCK."""
    if len(JOBS) <= MAX_JOBS:
        return
    terminal_states = {"done", "error", "cancelled", "dropped"}
    terminal = [(jid, row.get("finished_at", 0)) for jid, row in JOBS.items()
                if row.get("status") in terminal_states]
    terminal.sort(key=lambda kv: kv[1])
    for jid, _ in terminal[: max(1, len(JOBS) - MAX_JOBS + 10)]:
        JOBS.pop(jid, None)


def _read_frames(sock: socket.socket):
    """Generator yielding parsed JSON frames from a socket using newline framing.

    Ignores malformed lines (logs to stderr) rather than aborting on one bad
    frame. Stops when the peer closes the connection.
    """
    buffer = b""
    sock.settimeout(None)  # async frames can take many minutes to arrive
    while True:
        try:
            chunk = sock.recv(65536)
        except OSError:
            return
        if not chunk:
            # Flush any remaining bytes as a final line
            if buffer.strip():
                try:
                    yield json.loads(buffer.decode())
                except Exception:
                    pass
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line.decode())
            except Exception as e:
                print(f"[api_server] bad async frame: {e}", flush=True)


def _async_reader_loop(job_id: str, sock: socket.socket):
    """Consume bridge frames for a single job, update JOBS[job_id]."""
    try:
        got_result = False
        for frame in _read_frames(sock):
            now = time.time()
            if "_ack" in frame:
                ack = frame["_ack"] or {}
                with JOBS_LOCK:
                    row = JOBS.get(job_id)
                    if row is not None:
                        row["status"] = "running"
                        row["started_at"] = ack.get("submitted_at", now)
                        row["stage"] = "dispatched"
                        row["heartbeat_at"] = now
            elif "_heartbeat" in frame:
                hb = frame["_heartbeat"] or {}
                with JOBS_LOCK:
                    row = JOBS.get(job_id)
                    if row is not None:
                        row["heartbeat_at"] = hb.get("ts", now)
                        row["qt_lag_ms"] = hb.get("qt_lag_ms", 0)
                        row["stage"] = hb.get("stage", row.get("stage", "running"))
                        if hb.get("warning") == "qt_frozen":
                            row["status"] = "qt_frozen"
                        elif row["status"] == "qt_frozen":
                            # Qt recovered
                            row["status"] = "running"
            elif "_result" in frame:
                res = frame["_result"] or {}
                got_result = True
                with JOBS_LOCK:
                    row = JOBS.get(job_id)
                    if row is not None:
                        row["status"] = "done" if res.get("success") else "error"
                        row["result"] = res.get("result")
                        row["error"] = res.get("error")
                        row["finished_at"] = res.get("finished_at", now)
                        row["stage"] = "finished"
                        row["heartbeat_at"] = now
                # _result is final; stop reading
                break
        if not got_result:
            # Socket closed before _result frame — bridge likely died
            with JOBS_LOCK:
                row = JOBS.get(job_id)
                if row is not None and row.get("status") not in ("done", "error", "cancelled"):
                    row["status"] = "dropped"
                    row["error"] = "bridge connection closed before result"
                    row["finished_at"] = time.time()
                    row["stage"] = "finished"
    except Exception as e:
        with JOBS_LOCK:
            row = JOBS.get(job_id)
            if row is not None and row.get("status") not in ("done", "error", "cancelled"):
                row["status"] = "error"
                row["error"] = f"reader exception: {e}"
                row["finished_at"] = time.time()
    finally:
        try:
            sock.close()
        except Exception:
            pass


@app.post("/api/submit")
async def submit_job(body: dict):
    """Submit a bridge action for asynchronous execution.

    Returns immediately with {"job_id": "..."}. The client polls
    GET /api/job/{job_id} to track progress and retrieve the result.
    """
    action = body.get("action", "")
    params = body.get("params", {}) or {}
    if not action:
        raise HTTPException(400, "Missing 'action' field")
    if action.startswith("_"):
        # Underscore-prefixed actions are internal control-plane actions on the
        # bridge (_job_status, _cancel_job) and must not be submittable as jobs.
        raise HTTPException(400, f"Action {action!r} is not submittable")
    if not os.path.exists(SOCKET_PATH):
        raise HTTPException(503, "QGIS bridge not ready (socket not found)")

    if _count_running_jobs() >= MAX_CONCURRENT_ASYNC:
        raise HTTPException(429, f"Too many concurrent async jobs (cap {MAX_CONCURRENT_ASYNC})")

    job_id = uuid.uuid4().hex[:16]
    now = time.time()

    # Open bridge socket for the job's lifetime
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT)  # only for the initial connect + send
    try:
        sock.connect(SOCKET_PATH)
    except (ConnectionRefusedError, FileNotFoundError):
        try: sock.close()
        except Exception: pass
        raise HTTPException(503, "QGIS bridge connection refused")
    except Exception as e:
        try: sock.close()
        except Exception: pass
        raise HTTPException(500, f"Bridge connect error: {e}")

    request = json.dumps({
        "action": action,
        "params": params,
        "_async": True,
        "_job_id": job_id,
    }).encode()
    try:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
    except Exception as e:
        try: sock.close()
        except Exception: pass
        raise HTTPException(500, f"Bridge send error: {e}")

    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "action": action,
            "status": "queued",
            "stage": "queued",
            "submitted_at": now,
            "started_at": None,
            "finished_at": None,
            "heartbeat_at": now,
            "qt_lag_ms": 0,
            "result": None,
            "error": None,
        }
        _evict_if_needed()

    threading.Thread(
        target=_async_reader_loop, args=(job_id, sock), daemon=True,
        name=f"async-reader-{job_id}",
    ).start()

    return {"job_id": job_id, "submitted_at": now, "action": action}


def _row_with_derived(row: dict) -> dict:
    """Return a copy of the row with derived timing fields."""
    out = dict(row)
    now = time.time()
    submitted_at = row.get("submitted_at") or now
    out["age_s"] = round(now - submitted_at, 2)
    hb = row.get("heartbeat_at")
    out["heartbeat_age_s"] = round(now - hb, 2) if hb else None
    # Client-useful probably_frozen flag
    if row.get("status") in ("queued", "running", "qt_frozen"):
        out["probably_frozen"] = (out["heartbeat_age_s"] or 0) > 10 or row.get("status") == "qt_frozen"
    else:
        out["probably_frozen"] = False
    return out


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    """Return the current state of a submitted job.

    If the job is still running and heartbeats have been stale for >10 s, this
    also fires a single status-mode probe against the bridge on a fresh socket
    to distinguish "Qt frozen but bridge alive" from "bridge dead".
    """
    with JOBS_LOCK:
        row = JOBS.get(job_id)
        if row is None:
            raise HTTPException(404, f"Unknown job_id: {job_id}")
        row = _row_with_derived(row)

    # Cross-check via status-mode probe when heartbeat is stale
    if row["status"] in ("running", "queued") and (row["heartbeat_age_s"] or 0) > 10:
        probe = None
        try:
            probe_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe_sock.settimeout(2)
            probe_sock.connect(SOCKET_PATH)
            probe_sock.sendall(json.dumps({
                "action": "_job_status",
                "params": {"job_id": job_id},
            }).encode())
            probe_sock.shutdown(socket.SHUT_WR)
            data = b""
            while True:
                chunk = probe_sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            probe_sock.close()
            if data:
                probe = json.loads(data.decode())
        except Exception as e:
            probe = {"error": f"status probe failed: {e}"}
        row["bridge_probe"] = probe
        if probe and "error" in probe and "connection" in probe.get("error", "").lower():
            row["status"] = "bridge_unreachable"

    return row


@app.delete("/api/job/{job_id}")
async def cancel_job(job_id: str):
    """Best-effort cancel. Succeeds only if job is still queued on the bridge."""
    with JOBS_LOCK:
        row = JOBS.get(job_id)
        if row is None:
            raise HTTPException(404, f"Unknown job_id: {job_id}")
        if row.get("status") in ("done", "error", "cancelled", "dropped"):
            return {"success": False, "status": "already_terminal", "current_status": row["status"]}

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(SOCKET_PATH)
        sock.sendall(json.dumps({
            "action": "_cancel_job",
            "params": {"job_id": job_id},
        }).encode())
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        sock.close()
        resp = json.loads(data.decode()) if data else {"error": "no response"}
    except Exception as e:
        raise HTTPException(502, f"Cancel probe failed: {e}")

    if resp.get("success"):
        with JOBS_LOCK:
            row = JOBS.get(job_id)
            if row is not None and row.get("status") not in ("done", "error"):
                row["status"] = "cancelled"
                row["stage"] = "cancelled"
                row["finished_at"] = time.time()
    return resp


@app.get("/api/jobs")
async def list_jobs(status: str = "", limit: int = 50):
    """List jobs, optionally filtered by status. Newest first."""
    limit = max(1, min(int(limit), 200))
    with JOBS_LOCK:
        rows = list(JOBS.values())
    rows.sort(key=lambda r: r.get("submitted_at", 0), reverse=True)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return {"count": len(rows), "jobs": [_row_with_derived(r) for r in rows[:limit]]}


def _job_ttl_sweeper():
    """Background thread: evict terminal rows older than JOB_TTL_DONE_S and
    anything older than JOB_TTL_HARD_S regardless of state."""
    terminal_states = {"done", "error", "cancelled", "dropped"}
    while True:
        try:
            time.sleep(60)
            now = time.time()
            with JOBS_LOCK:
                victims = []
                for jid, row in JOBS.items():
                    age = now - row.get("submitted_at", now)
                    finished = row.get("finished_at")
                    if row.get("status") in terminal_states and finished is not None:
                        if now - finished > JOB_TTL_DONE_S:
                            victims.append(jid)
                    elif age > JOB_TTL_HARD_S:
                        victims.append(jid)
                for jid in victims:
                    JOBS.pop(jid, None)
        except Exception as e:
            print(f"[api_server] ttl sweeper: {e}", flush=True)


@app.on_event("startup")
async def _start_sweeper():
    threading.Thread(target=_job_ttl_sweeper, daemon=True, name="job-ttl-sweeper").start()


# ── Convenience endpoints ────────────────────────────────────────

@app.get("/api/project")
async def get_project():
    return send_command("get_project_info")


@app.get("/api/layers")
async def list_layers():
    return send_command("list_layers")


@app.get("/api/screenshot")
async def screenshot(width: int = 1280, height: int = 720, format: str = "jpeg"):
    """Capture the QGIS desktop.

    - format=jpeg (default): returns raw image/jpeg bytes — `curl -o file.jpg` works directly.
    - format=json: returns the bridge envelope `{image_base64, format, size}`.

    The bridge always encodes JPEG (q≤75, ≤1MB) regardless; format only controls the wrapper.
    """
    resp = send_command("screenshot", {"width": width, "height": height})
    if format == "json":
        return resp
    if "error" in resp:
        raise HTTPException(500, resp["error"])
    b64 = resp.get("image_base64")
    if not b64:
        raise HTTPException(500, "Bridge returned no image data")
    import base64 as _b64
    return Response(content=_b64.b64decode(b64), media_type="image/jpeg")


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
