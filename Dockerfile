# ═══════════════════════════════════════════════════════════════════
# BigQgisMCP — QGIS Desktop as an MCP Server
# ═══════════════════════════════════════════════════════════════════
#
# Single container with:
#   - QGIS Desktop 3.34 LTR (full GUI)
#   - Xvfb + fluxbox + x11vnc + noVNC (browser access)
#   - MCP Server (Streamable HTTP on :8100)
#   - PyQGIS bridge (UNIX socket for 0-latency control)
#   - MJPEG stream server (canvas capture)
#
# Ports:
#   6080 — noVNC (QGIS in browser)
#   8100 — MCP Server (Streamable HTTP)
#   8080 — REST API (health, external access)
#   8081 — MJPEG stream
#
# Build:  docker build -t bigqgismcp .
# Run:    docker run -p 6080:6080 -p 8100:8100 bigqgismcp

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV QT_QPA_PLATFORM=xcb

# Rendering: GPU or CPU fallback — configured dynamically in entrypoint.sh
# (LIBGL_ALWAYS_SOFTWARE and GALLIUM_DRIVER set at runtime based on GPU detection)
ENV LP_NUM_THREADS=4

# Force X11 session (prevent Wayland detection by x11vnc)
ENV XDG_SESSION_TYPE=x11
ENV XDG_RUNTIME_DIR=/run/user/0
ENV QT_X11_NO_MITSHM=1

# Python
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ── System dependencies ──────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # X11 / Display
    xvfb \
    x11vnc \
    fluxbox \
    x11-xserver-utils \
    xdotool \
    # OpenGL rendering (Mesa software fallback + NVIDIA runtime support)
    mesa-utils \
    libgl1-mesa-dri \
    libgl1 \
    libosmesa6 \
    libglapi-mesa \
    libegl1 \
    libglu1-mesa \
    libglvnd0 \
    libglx-mesa0 \
    # Networking
    websockify \
    curl \
    wget \
    net-tools \
    # Build
    gnupg \
    software-properties-common \
    # Python
    python3 \
    python3-pip \
    python3-venv \
    # Process management
    supervisor \
    # Media
    ffmpeg \
    # Fonts (for proper map labeling)
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# ── QGIS 3.34 LTR from official repository ──────────────────────
RUN wget -qO /etc/apt/keyrings/qgis-archive-keyring.gpg \
        https://download.qgis.org/downloads/qgis-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/qgis-archive-keyring.gpg] \
        https://qgis.org/ubuntu-ltr noble main" \
        > /etc/apt/sources.list.d/qgis.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        qgis \
        qgis-plugin-grass \
        python3-qgis \
        qgis-providers \
    && rm -rf /var/lib/apt/lists/*

# ── noVNC (browser-based VNC client) ─────────────────────────────
RUN wget -qO- https://github.com/novnc/noVNC/archive/v1.5.0.tar.gz \
        | tar xz -C /opt \
    && mv /opt/noVNC-1.5.0 /opt/novnc \
    && ln -s /opt/novnc/vnc.html /opt/novnc/index.html

# ── Python dependencies (MCP server + API) ───────────────────────
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --break-system-packages --no-cache-dir --ignore-installed -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# ── Directory structure ──────────────────────────────────────────
RUN mkdir -p /app /data /tmp/qgis \
    && mkdir -p -m 0700 /run/user/0 \
    && mkdir -p \
    /var/log/supervisor \
    /root/.local/share/QGIS/QGIS3/profiles/default/python/plugins \
    /root/.local/share/QGIS/QGIS3/profiles/default/python/startup \
    /root/.fluxbox

# ── Fluxbox: maximize all windows by default ───────────────────
RUN printf '[app] (name=.*)\n  [Maximized] {yes}\n[end]\n' > /root/.fluxbox/apps

WORKDIR /app

# ── Copy application files ───────────────────────────────────────
COPY main_mcp.py /app/
COPY qgis_app.html /app/
COPY maximize_qgis.sh /app/
COPY datasources.json /app/
COPY setup_qgis_connections.py /app/
COPY src/ /app/src/
COPY skills/ /app/skills/
COPY templates/ /app/templates/
COPY recipes/ /app/recipes/
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY entrypoint.sh /app/

RUN sed -i 's/\r$//' /app/entrypoint.sh /app/maximize_qgis.sh && \
    chmod +x /app/entrypoint.sh /app/maximize_qgis.sh

# ── QGIS startup script (auto-loads bridge on QGIS launch) ──────
RUN cp /app/src/qgis_bridge.py \
    /root/.local/share/QGIS/QGIS3/profiles/default/python/startup/qgis_bridge.py

# ── Ports ────────────────────────────────────────────────────────
# 6080: noVNC web interface
# 8080: REST API (health, external access)
# 8081: MJPEG canvas stream
# 8100: MCP Server (Streamable HTTP)
EXPOSE 6080 8080 8081 8100

# ── Healthcheck ──────────────────────────────────────────────────
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=4 \
    CMD curl -sf http://localhost:8080/health && curl -sf http://localhost:8100/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
