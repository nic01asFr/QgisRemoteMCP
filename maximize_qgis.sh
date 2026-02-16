#!/bin/bash
# Wait for QGIS window to appear, then maximize it
echo "[maximize] Waiting for QGIS window..."
for i in $(seq 1 60); do
    WID=$(DISPLAY=:99 xdotool search --name "QGIS" 2>/dev/null | head -1)
    if [ -n "$WID" ]; then
        echo "[maximize] Found QGIS window $WID, maximizing..."
        sleep 2
        DISPLAY=:99 xdotool windowactivate "$WID" 2>/dev/null
        DISPLAY=:99 xdotool windowsize "$WID" 1920 1080 2>/dev/null
        DISPLAY=:99 xdotool windowmove "$WID" 0 0 2>/dev/null
        DISPLAY=:99 wmctrl -i -r "$WID" -b add,maximized_vert,maximized_horz 2>/dev/null || true
        echo "[maximize] QGIS maximized"
        exit 0
    fi
    sleep 1
done
echo "[maximize] QGIS window not found after 60s"
