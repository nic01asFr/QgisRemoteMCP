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
Xvfb :99 -screen 0 "${RESOLUTION}" -ac +extension GLX +render -noreset &
sleep 2

# Verify display
if ! xdpyinfo -display :99 > /dev/null 2>&1; then
    echo "[BigQgisMCP] ERROR: Xvfb failed to start"
    exit 1
fi
echo "[BigQgisMCP] Xvfb running on :99"

# ── Verify OpenGL ─────────────────────────────────────────────────
GL_RENDERER=$(DISPLAY=:99 glxinfo 2>/dev/null | grep "OpenGL renderer" || echo "unknown")
echo "[BigQgisMCP] OpenGL: ${GL_RENDERER}"

# ── Create default QGIS project if none exists ───────────────────
if [ ! -f /projects/current.qgz ]; then
    echo "[BigQgisMCP] No project found, will use default"
fi

# ── Ensure socket cleanup ────────────────────────────────────────
rm -f /tmp/qgis_bridge.sock

# ── Start all services via supervisord ────────────────────────────
echo "[BigQgisMCP] Starting supervisord..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
