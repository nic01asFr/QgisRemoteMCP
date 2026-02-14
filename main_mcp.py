"""
BigQgisMCP — MCP Server
═══════════════════════════════════════════════════════════════════

Main entry point. Exposes QGIS Desktop as an MCP Server via
Streamable HTTP on port 8100.

Tools:
  - execute_python: Run PyQGIS scripts (the power tool)
  - get_screenshot: Capture current map canvas
  - get_project_info: Current project state
  - add_layer: Add vector/raster/WFS/WMS layers
  - remove_layer: Remove a layer
  - get_features: Query features with filters
  - run_processing: Execute QGIS Processing algorithms
  - search_algorithms: Find Processing algorithms
  - zoom_to: Navigate the map
  - export_pdf: Export print layout to PDF
  - get_vnc_url: Get URL for interactive QGIS access
  - new_project / open_project / save_project

Resources (Skills):
  - skill://pyqgis — PyQGIS scripting reference
  - skill://processing — Processing algorithms guide
  - skill://cartography — Styling & symbology
  - skill://external-services — How to call vision services
  - skill://data-sources — National datasets reference

Prompts:
  - audit_passages_pietons — Pedestrian crossing audit
  - analyse_territoire — Territory analysis workflow
"""

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

# ── Configuration ─────────────────────────────────────────────────

SOCKET_PATH = "/tmp/qgis_bridge.sock"
SOCKET_TIMEOUT = 60
SKILLS_DIR = Path("/app/skills")
VNC_PORT = int(os.environ.get("QGIS_VNC_PORT", "6080"))
VNC_HOST = os.environ.get("VNC_HOST", "localhost")

# External services (configured via env vars)
MOONDREAM_URL = os.environ.get("MOONDREAM_URL", "http://localhost:8001")
SAMGEO3_URL = os.environ.get("SAMGEO3_URL", "http://localhost:8002")
DEPTHPRO_URL = os.environ.get("DEPTHPRO_URL", "http://localhost:8003")


# ── QGIS Bridge client ───────────────────────────────────────────

def qgis_command(action: str, params: dict = None) -> dict:
    """Send a command to QGIS bridge via UNIX socket."""
    if not os.path.exists(SOCKET_PATH):
        return {"error": "QGIS bridge not ready. QGIS may still be starting up."}

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT)
        sock.connect(SOCKET_PATH)

        request = json.dumps({"action": action, "params": params or {}})
        sock.sendall(request.encode())
        sock.shutdown(socket.SHUT_WR)

        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk

        sock.close()
        return json.loads(data.decode())

    except socket.timeout:
        return {"error": f"QGIS command timed out after {SOCKET_TIMEOUT}s. The operation may still be running."}
    except ConnectionRefusedError:
        return {"error": "Cannot connect to QGIS. The application may be restarting."}
    except Exception as e:
        return {"error": f"Bridge error: {str(e)}"}


# ── MCP Server ────────────────────────────────────────────────────

mcp = FastMCP(
    "BigQgisMCP",
    instructions="""You are connected to a full QGIS Desktop instance running in a container.
You can see and control QGIS through these tools.

## Key capabilities:
- **execute_python**: Write and run PyQGIS scripts. This is your most powerful tool.
  The script runs inside QGIS with full access to qgis.core, iface, processing, etc.
  Store return values in the `result` dict.
- **get_screenshot**: See what's currently displayed on the QGIS map canvas.
- **get_vnc_url**: Give users a URL to interact with QGIS directly in their browser.

## Workflow pattern:
1. Use get_project_info to understand current state
2. Add data layers (add_layer, or via execute_python for complex cases)
3. Process data (run_processing or execute_python)
4. Check results with get_screenshot
5. Let user interact via get_vnc_url
6. Export results (export_pdf, or save_project)

## For external vision services:
Your PyQGIS scripts can call external HTTP services (Moondream, SAMGeo3, DepthPro)
using `import requests` or `import urllib.request`. The service URLs are available as:
""" + f"""
- MOONDREAM_URL = {MOONDREAM_URL}
- SAMGEO3_URL = {SAMGEO3_URL}
- DEPTHPRO_URL = {DEPTHPRO_URL}

Read the skill://external-services resource for usage patterns.

## Skills:
Before writing complex scripts, read the relevant skill:// resources.
They contain PyQGIS patterns, Processing algorithm references, and best practices.
""",
    host="0.0.0.0",
    port=int(os.environ.get("MCP_PORT", "8100")),
)


# ══════════════════════════════════════════════════════════════════
# TOOLS
# ══════════════════════════════════════════════════════════════════


# ── Execute Python (THE power tool) ──────────────────────────────

@mcp.tool()
def execute_python(code: str) -> str:
    """Execute Python/PyQGIS code inside the running QGIS instance.

    The script has access to:
      - qgis.core.* (QgsProject, QgsVectorLayer, QgsRasterLayer, etc.)
      - qgis.utils.iface (map canvas, menus, etc.)
      - processing.run() (1000+ algorithms)
      - project = QgsProject.instance()
      - canvas = iface.mapCanvas()
      - json, os, Path

    Store return values in the `result` dict. Example:
        layers = project.mapLayers()
        result['count'] = len(layers)
        result['names'] = [l.name() for l in layers.values()]

    For calling external services (Moondream, SAMGeo3), use:
        import urllib.request, json
        resp = urllib.request.urlopen('http://moondream:8001/detect', ...)

    Returns: JSON with 'success', 'result', 'stdout', or 'error'.
    """
    response = qgis_command("execute_python", {"code": code})
    return json.dumps(response, indent=2, default=str)


# ── Screenshot ────────────────────────────────────────────────────

@mcp.tool()
def get_screenshot(width: int = 800, height: int = 600) -> Image:
    """Capture the current QGIS map canvas as a PNG image.

    Returns the screenshot as an inline image displayed directly in the conversation.
    Use this to see what's displayed, verify layer additions, check styling, etc.

    Args:
        width: Image width in pixels (default 800)
        height: Image height in pixels (default 600)
    """
    import base64

    response = qgis_command("screenshot", {
        "width": width, "height": height, "format": "png"
    })
    if "error" in response:
        raise ValueError(response.get("error", "Screenshot failed"))

    image_b64 = response.get("image_base64", "")
    image_bytes = base64.b64decode(image_b64)
    return Image(data=image_bytes, format="png")


# ── Project management ────────────────────────────────────────────

@mcp.tool()
def get_project_info() -> str:
    """Get information about the current QGIS project.

    Returns: project title, CRS, list of layers with details,
    available print layouts.
    """
    return json.dumps(qgis_command("get_project_info"), indent=2, default=str)


@mcp.tool()
def new_project(title: str = "New Project", crs: str = "EPSG:2154") -> str:
    """Create a new empty QGIS project.

    Args:
        title: Project title
        crs: Coordinate Reference System (default EPSG:2154 = Lambert 93, France)
    """
    return json.dumps(qgis_command("new_project", {"title": title, "crs": crs}))


@mcp.tool()
def open_project(path: str) -> str:
    """Open an existing QGIS project file (.qgz or .qgs).

    Args:
        path: Full path to the project file (inside container, e.g. /projects/myproject.qgz)
    """
    return json.dumps(qgis_command("open_project", {"path": path}))


@mcp.tool()
def save_project(path: str = "") -> str:
    """Save the current QGIS project.

    Args:
        path: Path to save to (empty = save to current location)
    """
    return json.dumps(qgis_command("save_project", {"path": path}))


# ── Layer management ──────────────────────────────────────────────

@mcp.tool()
def add_layer(
    uri: str,
    name: str = "layer",
    layer_type: str = "vector",
    provider: str = "",
) -> str:
    """Add a layer to the QGIS project.

    Supports multiple layer types:
    - Vector: GeoJSON, Shapefile, GeoPackage, PostGIS, CSV
    - Raster: GeoTIFF, COG, JPEG, PNG
    - WFS: Web Feature Service
    - WMS: Web Map Service

    Args:
        uri: Data source URI. Examples:
             - "/data/roads.geojson"
             - "/data/ortho.tif"
             - "url='https://data.geopf.fr/wfs/ows' typename='BDTOPO_V3:route'"
             - "url=https://data.geopf.fr/wms-r&layers=ORTHOIMAGERY.ORTHOPHOTOS"
        name: Display name for the layer
        layer_type: "vector", "raster", "wfs", or "wms"
        provider: Override data provider (ogr, gdal, WFS, wms, postgres, memory)
    """
    if layer_type == "wfs":
        # Parse WFS params from uri or structured
        return json.dumps(qgis_command("add_wfs_layer", {
            "url": uri, "typename": name, "name": name
        }))
    elif layer_type == "wms":
        return json.dumps(qgis_command("add_wms_layer", {
            "url": uri, "layers": name, "name": name
        }))
    elif layer_type == "raster":
        return json.dumps(qgis_command("add_raster_layer", {
            "uri": uri, "name": name, "provider": provider or "gdal"
        }))
    else:
        return json.dumps(qgis_command("add_vector_layer", {
            "uri": uri, "name": name, "provider": provider or "ogr"
        }))


@mcp.tool()
def remove_layer(layer_id: str) -> str:
    """Remove a layer from the project.

    Args:
        layer_id: Layer ID (get from get_project_info)
    """
    return json.dumps(qgis_command("remove_layer", {"layer_id": layer_id}))


@mcp.tool()
def get_features(
    layer_id: str,
    filter: str = "",
    limit: int = 100,
    include_geometry: bool = True,
) -> str:
    """Query features from a vector layer.

    Args:
        layer_id: Layer ID
        filter: QGIS expression filter (e.g. "type = 'highway'" or "area > 1000")
        limit: Max features to return
        include_geometry: Include WKT geometry in results
    """
    return json.dumps(qgis_command("get_features", {
        "layer_id": layer_id,
        "filter": filter,
        "limit": limit,
        "include_geometry": include_geometry,
    }), indent=2, default=str)


# ── Processing ────────────────────────────────────────────────────

@mcp.tool()
def run_processing(algorithm: str, parameters: dict) -> str:
    """Execute a QGIS Processing algorithm.

    1000+ algorithms available from native, GDAL, GRASS, SAGA providers.
    Use search_algorithms to find the right one.

    Args:
        algorithm: Algorithm ID (e.g. "native:buffer", "gdal:cliprasterbymasklayer",
                   "native:intersection", "grass7:v.dissolve")
        parameters: Algorithm parameters as dict. Layer params can be layer IDs.

    Example:
        run_processing("native:buffer", {
            "INPUT": "my_layer_id_here",
            "DISTANCE": 100,
            "OUTPUT": "memory:"
        })
    """
    return json.dumps(qgis_command("run_processing", {
        "algorithm": algorithm,
        "parameters": parameters,
    }), indent=2, default=str)


@mcp.tool()
def search_algorithms(search: str = "", provider: str = "", limit: int = 20) -> str:
    """Search available QGIS Processing algorithms.

    Args:
        search: Search text (matches algorithm ID and display name)
        provider: Filter by provider (native, gdal, grass7, saga, etc.)
        limit: Max results to return
    """
    return json.dumps(qgis_command("list_algorithms", {
        "search": search,
        "provider": provider,
        "limit": limit,
    }), indent=2)


# ── Navigation ────────────────────────────────────────────────────

@mcp.tool()
def zoom_to(
    extent: list = None,
    layer_id: str = "",
) -> str:
    """Zoom the map canvas.

    Args:
        extent: [xmin, ymin, xmax, ymax] in project CRS. If empty, zoom to all layers.
        layer_id: Zoom to a specific layer's extent.
    """
    params = {}
    if extent:
        params["extent"] = extent
    elif layer_id:
        params["layer_id"] = layer_id
    return json.dumps(qgis_command("zoom_to_extent", params))


# ── Export ────────────────────────────────────────────────────────

@mcp.tool()
def export_pdf(
    layout: str,
    output_path: str = "",
) -> str:
    """Export a QGIS print layout to PDF.

    The layout must exist in the project (created via QGIS GUI or execute_python).
    Use get_project_info to see available layouts.

    Args:
        layout: Print layout name
        output_path: Output PDF path (default: auto-generated in /data/)
    """
    params = {"layout": layout}
    if output_path:
        params["output_path"] = output_path
    return json.dumps(qgis_command("export_pdf", params))


# ── VNC Access ────────────────────────────────────────────────────

@mcp.tool(
    annotations={
        "ui": {
            "resourceUri": "ui://bigqgismcp/qgis-desktop",
            "visibility": ["model", "app"],
        }
    }
)
def get_vnc_url() -> str:
    """Open the QGIS Desktop interactive view.

    Opens the QGIS GUI directly in the conversation as an MCP App.
    The user can interact with QGIS — zoom, pan, edit features,
    modify styles, use any QGIS tool. Everything happens on the same
    QGIS instance that the MCP tools control.

    Call this when:
    - The user wants to see or interact with the map
    - Manual adjustments are needed
    - The user wants to validate results visually
    - Complex styling needs human input
    """
    url = f"http://{VNC_HOST}:{VNC_PORT}/vnc.html?autoconnect=true&resize=scale"
    return json.dumps({
        "vnc_url": url,
        "app": "ui://bigqgismcp/qgis-desktop",
        "description": "QGIS Desktop is now available as an interactive view in the conversation. "
                       "You can also open it in a browser at the URL above."
    })


# ══════════════════════════════════════════════════════════════════
# RESOURCES (Skills)
# ══════════════════════════════════════════════════════════════════

def _load_skill(name: str) -> str:
    """Load a skill markdown file."""
    path = SKILLS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text()
    return f"Skill '{name}' not found at {path}"


@mcp.resource("skill://pyqgis")
def skill_pyqgis() -> str:
    """PyQGIS scripting reference — common patterns, API usage, examples."""
    return _load_skill("pyqgis")


@mcp.resource("skill://processing")
def skill_processing() -> str:
    """QGIS Processing algorithms guide — native, GDAL, GRASS, SAGA."""
    return _load_skill("processing")


@mcp.resource("skill://cartography")
def skill_cartography() -> str:
    """Cartography & styling guide — symbology, labels, print layouts."""
    return _load_skill("cartography")


@mcp.resource("skill://external-services")
def skill_external_services() -> str:
    """Guide for calling external vision services (Moondream, SAMGeo3, DepthPro)
    from within PyQGIS scripts."""
    return _load_skill("external_services")


@mcp.resource("skill://data-sources")
def skill_data_sources() -> str:
    """French national data sources — Panoramax, IGN, BD TOPO, OCS GE, Foncier."""
    return _load_skill("data_sources")


# ── MCP App: QGIS Interactive View ─────────────────────────────

@mcp.resource(
    "ui://bigqgismcp/qgis-desktop",
    name="QGIS Desktop",
    description="Interactive QGIS Desktop — live map canvas with full GUI access via noVNC.",
    mime_type="text/html;profile=mcp-app",
)
def qgis_desktop_app() -> str:
    """QGIS Desktop embedded as an interactive MCP App.

    Displays the full QGIS GUI directly in the conversation via noVNC.
    The user can pan, zoom, select features, edit styles — everything
    they can do in QGIS Desktop. All changes are reflected in MCP tools.
    """
    vnc_url = f"http://{VNC_HOST}:{VNC_PORT}/vnc.html?autoconnect=true&resize=scale&view_only=false"
    stream_url = f"http://{VNC_HOST}:8081/stream"
    api_url = f"http://{VNC_HOST}:8080"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QGIS Desktop — BigQgisMCP</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1a1a2e; color: #e0e0e0; }}
  .header {{ display: flex; align-items: center; justify-content: space-between; padding: 8px 16px; background: #16213e; border-bottom: 1px solid #0f3460; }}
  .header h1 {{ font-size: 14px; font-weight: 600; color: #e94560; }}
  .status {{ display: flex; align-items: center; gap: 8px; font-size: 12px; }}
  .status-dot {{ width: 8px; height: 8px; border-radius: 50%; background: #4ecca3; }}
  .status-dot.offline {{ background: #e94560; }}
  .toolbar {{ display: flex; gap: 6px; padding: 6px 16px; background: #16213e; border-bottom: 1px solid #0f3460; }}
  .toolbar button {{ padding: 4px 12px; font-size: 12px; border: 1px solid #0f3460; background: #1a1a2e; color: #e0e0e0; border-radius: 4px; cursor: pointer; }}
  .toolbar button:hover {{ background: #0f3460; }}
  .toolbar button.active {{ background: #e94560; border-color: #e94560; }}
  .canvas-wrapper {{ position: relative; width: 100%; height: calc(100vh - 80px); background: #000; }}
  .canvas-wrapper iframe {{ width: 100%; height: 100%; border: none; }}
  .canvas-wrapper img {{ width: 100%; height: 100%; object-fit: contain; }}
  .tab-bar {{ display: flex; gap: 2px; padding: 0 16px; }}
  .tab {{ padding: 4px 12px; font-size: 12px; border: 1px solid #0f3460; border-bottom: none; background: #1a1a2e; color: #999; cursor: pointer; border-radius: 4px 4px 0 0; }}
  .tab.active {{ background: #16213e; color: #e0e0e0; }}
  .info-panel {{ display: none; padding: 12px 16px; font-size: 12px; background: #16213e; max-height: 200px; overflow-y: auto; }}
  .info-panel.visible {{ display: block; }}
  .info-panel pre {{ white-space: pre-wrap; color: #4ecca3; }}
</style>
</head>
<body>
  <div class="header">
    <h1>QGIS Desktop</h1>
    <div class="status">
      <div class="status-dot" id="statusDot"></div>
      <span id="statusText">Connecting...</span>
    </div>
  </div>

  <div class="toolbar">
    <button onclick="switchView('interactive')" id="btnInteractive" class="active">Interactive</button>
    <button onclick="switchView('stream')" id="btnStream">Live Stream</button>
    <button onclick="takeScreenshot()" id="btnScreenshot">Screenshot</button>
    <button onclick="toggleInfo()" id="btnInfo">Project Info</button>
    <button onclick="openExternal()">Open in Browser</button>
  </div>

  <div class="canvas-wrapper" id="canvasWrapper">
    <iframe id="vncFrame" src="{vnc_url}" allow="clipboard-write"></iframe>
  </div>

  <div class="info-panel" id="infoPanel">
    <pre id="infoContent">Loading...</pre>
  </div>

  <script>
    const API = "{api_url}";
    const VNC = "{vnc_url}";
    const STREAM = "{stream_url}";
    let currentView = "interactive";

    // Check QGIS health
    async function checkHealth() {{
      try {{
        const r = await fetch(API + "/health");
        const data = await r.json();
        document.getElementById("statusDot").className = "status-dot";
        document.getElementById("statusText").textContent =
          "QGIS " + (data.qgis?.qgis_version || "connected") +
          " | " + (data.qgis?.layer_count || 0) + " layers";
        return data;
      }} catch(e) {{
        document.getElementById("statusDot").className = "status-dot offline";
        document.getElementById("statusText").textContent = "Disconnected";
        return null;
      }}
    }}

    function switchView(view) {{
      currentView = view;
      const wrapper = document.getElementById("canvasWrapper");
      document.getElementById("btnInteractive").className = view === "interactive" ? "active" : "";
      document.getElementById("btnStream").className = view === "stream" ? "active" : "";
      if (view === "interactive") {{
        wrapper.innerHTML = '<iframe id="vncFrame" src="' + VNC + '" allow="clipboard-write"></iframe>';
      }} else {{
        wrapper.innerHTML = '<img id="streamImg" src="' + STREAM + '" alt="QGIS Live Stream">';
      }}
    }}

    async function takeScreenshot() {{
      try {{
        const r = await fetch(API + "/api/screenshot");
        const data = await r.json();
        if (data.image_base64) {{
          const wrapper = document.getElementById("canvasWrapper");
          wrapper.innerHTML = '<img src="data:image/png;base64,' + data.image_base64 + '" alt="Screenshot">';
          document.getElementById("btnInteractive").className = "";
          document.getElementById("btnStream").className = "";
        }}
      }} catch(e) {{ console.error("Screenshot failed", e); }}
    }}

    async function toggleInfo() {{
      const panel = document.getElementById("infoPanel");
      panel.classList.toggle("visible");
      if (panel.classList.contains("visible")) {{
        const data = await checkHealth();
        document.getElementById("infoContent").textContent = JSON.stringify(data, null, 2);
      }}
    }}

    function openExternal() {{
      // Request host to open VNC URL
      try {{
        window.parent.postMessage({{
          jsonrpc: "2.0", method: "ui/open-link",
          params: {{ url: VNC }}
        }}, "*");
      }} catch(e) {{
        window.open(VNC, "_blank");
      }}
    }}

    // Health check on load + periodic
    checkHealth();
    setInterval(checkHealth, 15000);
  </script>
</body>
</html>"""


@mcp.resource("skill://qgis-status")
def qgis_status() -> str:
    """Current QGIS instance status — version, project, layers, uptime."""
    return json.dumps(qgis_command("health"), indent=2)


# ══════════════════════════════════════════════════════════════════
# PROMPTS
# ══════════════════════════════════════════════════════════════════

@mcp.prompt()
def analyse_territoire(zone: str = "", theme: str = "general") -> str:
    """Template for territory analysis.

    Args:
        zone: Geographic zone (address, commune name, bbox)
        theme: Analysis theme (general, urbanisme, mobilite, environnement)
    """
    return f"""Analyse territoriale pour la zone : {zone or '[à préciser]'}
Thème : {theme}

Étapes recommandées :
1. Géocodez la zone avec execute_python (urllib vers BAN API)
2. Chargez les couches de données pertinentes (BD TOPO, OCS GE, ortho)
3. Effectuez les traitements Processing adaptés au thème
4. Générez un screenshot pour vérification
5. Proposez le VNC à l'utilisateur pour exploration interactive
6. Exportez les résultats (GeoPackage, PDF)

Consultez les skills pertinents :
- skill://data-sources pour les sources de données françaises
- skill://processing pour les algorithmes disponibles
- skill://cartography pour la mise en forme

Services vision disponibles (si déployés) :
- Moondream ({MOONDREAM_URL}) : description et détection d'objets sur images
- SAMGeo3 ({SAMGEO3_URL}) : segmentation géospatiale
- DepthPro ({DEPTHPRO_URL}) : estimation de profondeur
"""


@mcp.prompt()
def audit_passages_pietons(commune: str = "") -> str:
    """Template for pedestrian crossing audit using street-level imagery."""
    return f"""Audit des passages piétons — {commune or '[commune à préciser]'}

Workflow :
1. Géocodez la commune
2. Chargez la BD TOPO (routes, bâtiments)
3. Recherchez les images Panoramax dans le périmètre
4. Pour chaque image, utilisez Moondream pour détecter les passages piétons
5. Géoréférencez les détections
6. Analysez l'état (marquage, accessibilité PMR, visibilité)
7. Créez la couche résultats avec attributs de diagnostic
8. Stylisez par catégorie (bon état / dégradé / absent)
9. Générez le rapport PDF

Services nécessaires :
- Moondream ({MOONDREAM_URL}) pour la détection
- SAMGeo3 ({SAMGEO3_URL}) pour la segmentation fine (optionnel)
"""


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("[BigQgisMCP] Waiting for QGIS bridge...")
    for i in range(90):
        if os.path.exists(SOCKET_PATH):
            print(f"[BigQgisMCP] Bridge found after {i}s")
            break
        time.sleep(1)
    else:
        print("[BigQgisMCP] WARNING: Bridge not found, starting MCP server anyway")

    print(f"[BigQgisMCP] Starting MCP server on :{mcp.settings.port}")
    mcp.run(transport="streamable-http")
