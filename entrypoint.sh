#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# BigQgisMCP — Container entrypoint
# ═══════════════════════════════════════════════════════════════════

set -e

echo "╔═══════════════════════════════════════════╗"
echo "║          BigQgisMCP Starting...            ║"
echo "╚═══════════════════════════════════════════╝"

# ── Start Xvfb (virtual X display) ───────────────────────────────
RESOLUTION="${QGIS_RESOLUTION:-1920x1080x24}"
echo "[BigQgisMCP] Starting Xvfb :99 at ${RESOLUTION}"

# Clean stale lock files from previous runs
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

Xvfb :99 -screen 0 "${RESOLUTION}" -ac +extension GLX +render -noreset &
XVFB_PID=$!

# Wait for Xvfb to be ready (up to 10s)
for i in $(seq 1 20); do
    if [ -e /tmp/.X11-unix/X99 ]; then
        echo "[BigQgisMCP] Xvfb running on :99 (PID ${XVFB_PID})"
        break
    fi
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        echo "[BigQgisMCP] ERROR: Xvfb process died"
        exit 1
    fi
    sleep 0.5
done

if [ ! -e /tmp/.X11-unix/X99 ]; then
    echo "[BigQgisMCP] ERROR: Xvfb failed to start after 10s"
    exit 1
fi

# ── Verify OpenGL ─────────────────────────────────────────────────
GL_RENDERER=$(DISPLAY=:99 glxinfo 2>/dev/null | grep "OpenGL renderer" || echo "unknown")
echo "[BigQgisMCP] OpenGL: ${GL_RENDERER}"

# ── Create default QGIS project if none exists ───────────────────
if [ ! -f /projects/current.qgz ]; then
    echo "[BigQgisMCP] No project found, will use default"
fi

# ── Ensure socket cleanup ────────────────────────────────────────
rm -f /tmp/qgis_bridge.sock

# ── Ensure x11vnc sees pure X11 (unset Wayland var entirely) ─────
unset WAYLAND_DISPLAY

# ── Start all services via supervisord ────────────────────────────
echo "[BigQgisMCP] Starting supervisord..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
