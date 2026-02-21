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

# ── Rendering: GPU detection & CPU fallback ──────────────────────
# Check actual OpenGL renderer (not just nvidia-smi, since Xvfb may not use GPU)
GL_RENDERER=$(DISPLAY=:99 glxinfo 2>/dev/null | grep -i "OpenGL renderer" | head -1 || echo "")
echo "[BigQgisMCP] OpenGL: ${GL_RENDERER:-unknown}"

if echo "$GL_RENDERER" | grep -qi "nvidia\|geforce\|quadro\|tesla"; then
    echo "[BigQgisMCP] GPU rendering active (NVIDIA)"
    export RENDERING_MODE=gpu
    unset LIBGL_ALWAYS_SOFTWARE
    unset GALLIUM_DRIVER
elif command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
    echo "[BigQgisMCP] NVIDIA GPU present but OpenGL uses software renderer"
    echo "[BigQgisMCP] (Xvfb limitation — GPU available for CUDA/compute only)"
    export RENDERING_MODE=cpu
    export LIBGL_ALWAYS_SOFTWARE=1
    export GALLIUM_DRIVER=llvmpipe
else
    echo "[BigQgisMCP] No GPU — Mesa llvmpipe (CPU software rendering)"
    export RENDERING_MODE=cpu
    export LIBGL_ALWAYS_SOFTWARE=1
    export GALLIUM_DRIVER=llvmpipe
fi

echo "[BigQgisMCP] Rendering mode: ${RENDERING_MODE}"

# ── Ensure /data directory exists ─────────────────────────────────
mkdir -p /data
echo "[BigQgisMCP] Working directory: /data"

# ── Ensure socket cleanup ────────────────────────────────────────
rm -f /tmp/qgis_bridge.sock

# ── Ensure x11vnc sees pure X11 (unset Wayland var entirely) ─────
unset WAYLAND_DISPLAY

# ── Pre-seed QGIS data source connections ────────────────────────
echo "[BigQgisMCP] Pre-configuring QGIS connections..."
python3 /app/setup_qgis_connections.py || echo "[BigQgisMCP] WARNING: Connection setup failed (non-fatal)"

# ── Start all services via supervisord ────────────────────────────
echo "[BigQgisMCP] Starting supervisord..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
