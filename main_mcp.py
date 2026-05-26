"""
QgisRemoteMCP — MCP Server (Streamable HTTP)
═══════════════════════════════════════════════════════════════════

Raw Starlette-based MCP server with MCP Apps support.
Implements JSON-RPC 2.0 over Streamable HTTP with ui:// resources
for rendering the QGIS Desktop interface directly in Claude Desktop.
"""

import json
import os
import socket
import sys
import time
import base64
import uuid
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# Multi-user managers (imported lazily — only used when MULTI_USER_MODE=true)
try:
    from src.auth import AuthManager
    from src.container_manager import ContainerManager
    _MULTIUSER_DEPS_OK = True
except ImportError:
    _MULTIUSER_DEPS_OK = False

# ── Configuration ─────────────────────────────────────────────────

SOCKET_PATH = "/tmp/qgis_bridge.sock"
SOCKET_TIMEOUT = 60
SOCKET_TIMEOUT_LONG = 300      # for WFS downloads via ogr2ogr
SOCKET_TIMEOUT_RECIPE = 1200   # for run_recipe (up to 20 min for heavy analyses)
SKILLS_DIR = Path("/app/skills")
MCP_PORT = int(os.environ.get("MCP_PORT", "8100"))
VNC_PORT = int(os.environ.get("QGIS_VNC_PORT", "6080"))
VNC_HOST = os.environ.get("VNC_HOST", "localhost")
PROTOCOL_VERSION = "2025-06-18"

MOONDREAM_URL = os.environ.get("MOONDREAM_URL", "http://localhost:8001")
SAMGEO3_URL = os.environ.get("SAMGEO3_URL", "http://localhost:8002")
DEPTHPRO_URL = os.environ.get("DEPTHPRO_URL", "http://localhost:8003")

# ── Multi-user configuration ───────────────────────────────────────

# Set MULTI_USER_MODE=true to enable per-user Docker containers + optional auth.
MULTI_USER_MODE: bool = os.environ.get("MULTI_USER_MODE", "false").lower() == "true"

# Internal port of the api_server inside each QGIS container.
# In single-user mode this is the local api_server (same container).
# In multi-user mode this is dynamically assigned per user.
_SINGLE_USER_API_PORT: int = int(os.environ.get("API_PORT", "8080"))

# ContextVar: carries current user_id through asyncio.to_thread → tool handlers.
# Python 3.7+ propagates ContextVars automatically into threads started with to_thread.
current_user_id: ContextVar[str] = ContextVar("current_user_id", default="default")

# Global manager instances — initialised in lifespan when MULTI_USER_MODE=true.
container_manager: Optional["ContainerManager"] = None
auth_manager: Optional["AuthManager"] = None

# MCP Apps
UI_RESOURCE_URI = "ui://qgisremotemcp/qgis-desktop"
UI_MIME_TYPE = "text/html;profile=mcp-app"
UI_HTML_CONTENT = ""

# Sessions
SESSION_TTL = 7200  # 2 hours — stale sessions pruned automatically
sessions: Dict[str, Dict[str, Any]] = {}


def _session_touch(session_id: str) -> None:
    """Update last_seen for an existing session; prune stale sessions when dict grows large."""
    now = time.time()
    if session_id in sessions:
        sessions[session_id]["last_seen"] = now
    if len(sessions) > 200:
        stale = [sid for sid, d in list(sessions.items())
                 if now - d.get("last_seen", 0) > SESSION_TTL]
        for sid in stale:
            sessions.pop(sid, None)


async def _ensure_session(user_id: str) -> None:
    """Ensure a QGIS container is running for user_id (multi-user mode only).

    If no ready session exists, starts a new container and waits for it to
    become healthy.  No-op in single-user mode.
    """
    if not MULTI_USER_MODE or container_manager is None:
        return
    session = container_manager.get_session(user_id)
    if session:
        # Container exists and Docker reports it running (get_session checks that).
        # If marked "unhealthy" by our wait loop, re-probe — QGIS may be ready now.
        if session.status == "ready":
            container_manager.touch_session(user_id)
            return
        if session.status == "unhealthy":
            # Re-check health before giving up and spawning a new container
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(f"{session.internal_api_url}/health")
                    if r.status_code == 200:
                        session.status = "ready"
                        container_manager.touch_session(user_id)
                        print(f"[QgisRemoteMCP] Container for user={user_id} recovered → ready")
                        return
            except Exception:
                pass
            # Still unhealthy — stop the old container before spawning a new one
            print(f"[QgisRemoteMCP] Container for user={user_id} still unhealthy, replacing…")
            await container_manager.stop_session(user_id)
    # Start a new container — this blocks for ~30-60 s on first call
    print(f"[QgisRemoteMCP] Starting QGIS container for user={user_id} …")
    await container_manager.start_session(user_id)


# ── QGIS Bridge client ───────────────────────────────────────────

def _get_user_api_base_url() -> str:
    """Return the base URL for the current user's api_server.

    Single-user mode  → http://localhost:<API_PORT>
    Multi-user mode   → http://<container_ip>:8080 (internal Docker network)
    Falls back to the single-user URL if no session is found.
    """
    if MULTI_USER_MODE and container_manager is not None:
        uid = current_user_id.get("default")
        session = container_manager.sessions.get(uid)
        if session:
            return session.internal_api_url
    return f"http://localhost:{_SINGLE_USER_API_PORT}"


def _get_user_api_port() -> int:
    """Return the host-mapped api_server port (for external-facing URLs like download_url).
    In multi-user mode returns the host port, not the internal container port.
    """
    if MULTI_USER_MODE and container_manager is not None:
        uid = current_user_id.get("default")
        session = container_manager.sessions.get(uid)
        if session and session.api_port:
            return session.api_port
    return _SINGLE_USER_API_PORT


def qgis_command(action: str, params: dict = None, timeout: int = None) -> dict:
    """Send a command to the QGIS api_server via HTTP POST /api/command.

    Replaces the former UNIX-socket transport so that each user's request
    is routed to the correct container port (multi-user) or the local
    api_server (single-user).  The api_server in every container exposes
    POST /api/command → UNIX socket → bridge — no bridge changes needed.
    """
    effective_timeout = timeout or SOCKET_TIMEOUT
    base_url = _get_user_api_base_url()
    url = f"{base_url}/api/command"
    try:
        with httpx.Client(timeout=effective_timeout) as client:
            resp = client.post(url, json={"action": action, "params": params or {}})
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        return {"error": f"QGIS command timed out after {effective_timeout}s."}
    except httpx.ConnectError:
        return {"error": f"Cannot connect to QGIS api_server at port {port}. QGIS may still be starting up."}
    except Exception as e:
        return {"error": f"Bridge HTTP error: {str(e)}"}


# ── Load HTML ─────────────────────────────────────────────────────

def load_ui_html():
    global UI_HTML_CONTENT
    html_path = Path(__file__).parent / "qgis_app.html"
    if html_path.exists():
        UI_HTML_CONTENT = html_path.read_text(encoding="utf-8")
        print(f"[QgisRemoteMCP] Loaded UI HTML: {len(UI_HTML_CONTENT)} bytes")
    else:
        print(f"[QgisRemoteMCP] Warning: UI HTML not found at {html_path}")
        UI_HTML_CONTENT = "<html><body><h1>QGIS App UI not found</h1></body></html>"

load_ui_html()


# ── Load skill files ──────────────────────────────────────────────

def _load_skill(name: str) -> str:
    path = SKILLS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text()
    return f"Skill '{name}' not found at {path}"


# ══════════════════════════════════════════════════════════════════
# TOOLS DEFINITION
# ══════════════════════════════════════════════════════════════════

TOOLS = [
    {
        "name": "qgis_desktop_ui",
        "description": "Open the interactive QGIS Desktop view. Shows the live QGIS map canvas directly in the conversation. Use this when the user wants to see or interact with the map, validate results visually, or make manual adjustments.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "_meta": {
            "ui": {
                "resourceUri": UI_RESOURCE_URI
            }
        }
    },
    {
        "name": "execute_python",
        "description": "Execute Python/PyQGIS code inside the running QGIS instance. The script has access to qgis.core.*, iface, processing.run(), project = QgsProject.instance(), canvas = iface.mapCanvas(). A `helpers` module is available with ready-made functions: helpers.geocode(addr), helpers.add_wfs(url, typename, bbox), helpers.add_wms(url, layers), helpers.add_wmts(url, layers), helpers.add_xyz(url, name), helpers.zoom_to(target), helpers.create_point_layer(name, points), helpers.load_catalog_source(id), helpers.bbox_from_canvas(), helpers.search_commune(name), helpers.get_elevation(lon, lat). Store return values in the `result` dict. Read skill://helpers for full reference.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute inside QGIS"},
                "timeout": {"type": "integer", "description": "Execution timeout in seconds (default 60). Increase for long-running spatial operations.", "default": 60}
            },
            "required": ["code"]
        }
    },
    {
        "name": "get_screenshot",
        "description": "Capture the current QGIS map canvas as a PNG image. Returns the screenshot inline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "description": "Image width (default 800)", "default": 800},
                "height": {"type": "integer", "description": "Image height (default 600)", "default": 600}
            },
            "required": []
        }
    },
    {
        "name": "get_project_info",
        "description": "Get information about the current QGIS project: title, CRS, layers, print layouts.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "new_project",
        "description": "Create a new empty QGIS project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "default": "New Project"},
                "crs": {"type": "string", "description": "CRS (default EPSG:2154 Lambert 93)", "default": "EPSG:2154"}
            },
            "required": []
        }
    },
    {
        "name": "open_project",
        "description": "Open an existing QGIS project file (.qgz or .qgs).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to project file"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "save_project",
        "description": "Save the current QGIS project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path (empty = current location)", "default": ""}
            },
            "required": []
        }
    },
    {
        "name": "add_layer",
        "description": "Add a layer to the QGIS project. Supports vector (GeoJSON, SHP, GPKG), raster (GeoTIFF, COG), WFS, WMS.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Data source URI"},
                "name": {"type": "string", "description": "Display name", "default": "layer"},
                "layer_type": {"type": "string", "enum": ["vector", "raster", "wfs", "wms"], "default": "vector"},
                "provider": {"type": "string", "description": "Data provider override", "default": ""}
            },
            "required": ["uri"]
        }
    },
    {
        "name": "remove_layer",
        "description": "Remove a layer from the project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer_id": {"type": "string", "description": "Layer ID"}
            },
            "required": ["layer_id"]
        }
    },
    {
        "name": "get_features",
        "description": "Query features from a vector layer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer_id": {"type": "string"},
                "filter": {"type": "string", "description": "QGIS expression filter", "default": ""},
                "limit": {"type": "integer", "default": 100},
                "include_geometry": {"type": "boolean", "default": True}
            },
            "required": ["layer_id"]
        }
    },
    {
        "name": "run_processing",
        "description": "Execute a QGIS Processing algorithm. 1000+ algorithms from native, GDAL, GRASS, SAGA.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "algorithm": {"type": "string", "description": "Algorithm ID (e.g. native:buffer)"},
                "parameters": {"type": "object", "description": "Algorithm parameters"}
            },
            "required": ["algorithm", "parameters"]
        }
    },
    {
        "name": "search_algorithms",
        "description": "Search available QGIS Processing algorithms.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "default": ""},
                "provider": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 20}
            },
            "required": []
        }
    },
    {
        "name": "zoom_to",
        "description": "Zoom the map canvas to an extent or layer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "extent": {"type": "array", "items": {"type": "number"}, "description": "[xmin, ymin, xmax, ymax]"},
                "layer_id": {"type": "string", "default": ""}
            },
            "required": []
        }
    },
    {
        "name": "export_pdf",
        "description": "Export a QGIS print layout to PDF.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layout": {"type": "string", "description": "Print layout name"},
                "output_path": {"type": "string", "default": ""}
            },
            "required": ["layout"]
        }
    },
    {
        "name": "mouse_click",
        "description": "Click at (x, y) on the QGIS desktop. Coordinates are in display pixels (1920x1080).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
                "button": {"type": "integer", "description": "1=left, 2=middle, 3=right", "default": 1},
                "double": {"type": "boolean", "description": "Double-click", "default": False}
            },
            "required": ["x", "y"]
        }
    },
    {
        "name": "mouse_scroll",
        "description": "Scroll the mouse wheel at (x, y).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
                "clicks": {"type": "integer", "default": 3}
            },
            "required": ["x", "y"]
        }
    },
    {
        "name": "key_press",
        "description": "Send a key press to QGIS. Examples: 'Return', 'ctrl+z', 'ctrl+shift+s', 'Delete'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key combo (xdotool syntax)"}
            },
            "required": ["key"]
        }
    },
    {
        "name": "mouse_drag",
        "description": "Drag from (x1,y1) to (x2,y2) on the QGIS desktop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x1": {"type": "integer"}, "y1": {"type": "integer"},
                "x2": {"type": "integer"}, "y2": {"type": "integer"},
                "button": {"type": "integer", "default": 1}
            },
            "required": ["x1", "y1", "x2", "y2"]
        }
    },
    # ── File management tools ────────────────────────────────────
    {
        "name": "upload_file",
        "description": "Upload a file into the QGIS container (/data/). Call with just 'name' to get the direct upload endpoint (multipart POST, any size up to 50MB). The user can then upload via: curl -F 'file=@local_file' <endpoint>. Alternatively provide 'url' for the server to fetch the file itself, or 'content_base64' for tiny inline files (<1MB).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Target filename in /data/ (e.g. 'parcels.geojson')"},
                "url": {"type": "string", "description": "URL the server will fetch directly. Any public URL, or http://host.docker.internal:<port>/file for local Docker setups."},
                "content_base64": {"type": "string", "description": "Base64-encoded content — only for tiny files (<1MB). Prefer url or multipart instead."}
            },
            "required": ["name"]
        }
    },
    {
        "name": "download_file",
        "description": "Download a file from the QGIS container (/data/). Returns a download_url you can give to the user — they open it in their browser to save locally. Small files (<5MB) also include base64 inline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (e.g. '/data/export.gpkg')"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_files",
        "description": "List files in the QGIS container's /data/ directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (default '*')", "default": "*"}
            },
            "required": []
        }
    },
    {
        "name": "export_layer",
        "description": "Export a vector layer to file (GPKG, GeoJSON, Shapefile, CSV). The file is saved to /data/ and can be downloaded.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer_id": {"type": "string", "description": "Layer ID to export"},
                "format": {"type": "string", "enum": ["GPKG", "GeoJSON", "ESRI Shapefile", "CSV"], "default": "GPKG"},
                "name": {"type": "string", "description": "Output filename (without extension)", "default": ""}
            },
            "required": ["layer_id"]
        }
    },
    {
        "name": "download_project",
        "description": "Save the current QGIS project as .qgz and make it available for download.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project filename (without .qgz)", "default": "project"}
            },
            "required": []
        }
    },
    {
        "name": "delete_file",
        "description": "Delete a file from the QGIS container. Restricted to /data/.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (e.g. '/data/export.gpkg')"}
            },
            "required": ["path"]
        }
    },
    # ── Data catalog tools ───────────────────────────────────────
    {
        "name": "list_datasources",
        "description": "List available pre-configured data sources (IGN, OSM, BD TOPO, etc.). All French national sources are free, no API key needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category: basemap, imagery, topography, administrative, elevation, environment, api", "default": ""},
                "search": {"type": "string", "description": "Search in name/description", "default": ""}
            },
            "required": []
        }
    },
    {
        "name": "add_from_catalog",
        "description": "Add a data source from the catalog by ID. WFS sources require a bbox. Use list_datasources to see available IDs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Source ID from catalog (e.g. 'osm_xyz', 'bdtopo_batiments')"},
                "name": {"type": "string", "description": "Override display name", "default": ""},
                "bbox": {"type": "array", "items": {"type": "number"}, "description": "[xmin, ymin, xmax, ymax] in EPSG:4326 — required for WFS sources"}
            },
            "required": ["id"]
        }
    },
    # ── Study zone & smart load ─────────────────────────────────
    {
        "name": "set_study_zone",
        "description": "Define the geographic study area. CALL THIS FIRST before loading WFS data. Geocodes the target, stores bbox in project variables (EPSG:4326 + EPSG:2154), and zooms the canvas. Subsequent smart_load calls auto-use this zone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Place name, address, or commune. Examples: 'Montpellier', 'Gare de Lyon, Paris', 'Sete'"},
                "buffer_km": {"type": "number", "description": "Buffer around point in km (default 2)", "default": 2}
            },
            "required": ["target"]
        }
    },
    {
        "name": "get_study_zone",
        "description": "Get the current study zone (name, bbox in EPSG:4326 and EPSG:2154). Returns the zone set by set_study_zone.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "smart_load",
        "description": "Load data from the catalog. WFS sources are downloaded as local GeoPackage via ogr2ogr (automatic pagination, R-tree spatial index, fast for Processing). Raster sources (WMS/WMTS/XYZ) stream as usual. Use set_study_zone first to define the area, or provide a bbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Catalog source ID (e.g. 'bdtopo_batiments', 'osm_xyz'). Use list_datasources to see available IDs."},
                "bbox": {"type": "array", "items": {"type": "number"}, "description": "Optional [xmin,ymin,xmax,ymax] in EPSG:4326. Auto from study zone if not provided."},
                "max_features": {"type": "integer", "description": "Max features for WFS download (default 10000)", "default": 10000},
                "name": {"type": "string", "description": "Override display name", "default": ""}
            },
            "required": ["id"]
        }
    },
    # ── Style tools ──────────────────────────────────────────────
    {
        "name": "set_layer_style",
        "description": "Apply symbology to a vector layer. Supports single color, categorized (by field values), or graduated (numeric ranges).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer_id": {"type": "string", "description": "Layer ID"},
                "style_type": {"type": "string", "enum": ["single", "categorized", "graduated"], "default": "single"},
                "color": {"type": "string", "description": "Color as 'R,G,B,A' (e.g. '255,0,0,180') — for single style", "default": "65,105,225,180"},
                "field": {"type": "string", "description": "Attribute field — for categorized/graduated", "default": ""},
                "categories": {"type": "object", "description": "Map of value → {color, label} — for categorized style", "default": {}},
                "ranges": {"type": "array", "description": "List of {min, max, color, label} — for graduated style", "default": []}
            },
            "required": ["layer_id"]
        }
    },
    {
        "name": "set_layer_visibility",
        "description": "Toggle a layer's visibility in the layer tree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer_id": {"type": "string", "description": "Layer ID"},
                "visible": {"type": "boolean", "description": "Show (true) or hide (false)", "default": True}
            },
            "required": ["layer_id"]
        }
    },
    # ── Layout templates ──────────────────────────────────────────
    {
        "name": "list_layout_templates",
        "description": "List available print layout templates (A3 landscape, A4 portrait, etc.).",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "apply_layout_template",
        "description": "Apply a pre-configured print layout template (.qpt) with dynamic labels. Variables like title, subtitle are set as project variables and resolved via QGIS expressions [% @title %]. Use export_pdf after this to generate the PDF.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template": {"type": "string", "description": "Template ID: 'a3_landscape' or 'a4_portrait'"},
                "variables": {"type": "object", "description": "Variables to set: {title, subtitle, ...}. 'study_zone_name' is auto-set by set_study_zone.", "default": {}},
                "name": {"type": "string", "description": "Layout name override", "default": ""}
            },
            "required": ["template"]
        }
    },
    # ── Web map export ────────────────────────────────────────────
    {
        "name": "export_web_map",
        "description": "Export visible vector layers as an interactive Leaflet HTML page. GeoJSON inline, popup attributes, legend with toggle. Returns a download URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Map title (default: project title)", "default": ""},
                "max_features": {"type": "integer", "description": "Max features per layer (default 5000)", "default": 5000},
                "output_path": {"type": "string", "description": "Output path (default: /data/webmap_<timestamp>.html)", "default": ""}
            },
            "required": []
        }
    },
    # ── Interactive flood map ─────────────────────────────────────
    {
        "name": "export_flood_map",
        "description": "Export an interactive flood simulation as a standalone Leaflet HTML page. Requires ISO_HT (water depth) and building layers loaded. Pre-computes building exposure by spatial intersection. The HTML includes a water height slider with play/pause animation, dynamic statistics, and graduated color legends. Best used after running the risque_inondation recipe.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Map title (default: 'Simulation inondation — <zone>')", "default": ""},
                "max_features": {"type": "integer", "description": "Max features per layer (default 10000)", "default": 10000},
                "output_path": {"type": "string", "description": "Output path (default: /data/flood_map_<timestamp>.html)", "default": ""},
                "include_fields": {"type": "array", "items": {"type": "string"}, "description": "Field names to include in GeoJSON (reduces file size). Omit to include all fields.", "default": []}
            },
            "required": []
        }
    },
    {
        "name": "export_temporal_map",
        "description": "Export an interactive temporal analysis as a standalone Leaflet HTML page. Shows point data (e.g. property transactions) with a year slider, color-coded by value, with optional spatial bands (e.g. coastal proximity) and animated playback. Pre-computes per-year statistics. Best used after running a temporal recipe (e.g. pression_fonciere_cotiere).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Map title", "default": ""},
                "max_features": {"type": "integer", "description": "Max point features to export", "default": 15000},
                "output_path": {"type": "string", "description": "Output file path (auto-generated if empty)", "default": ""},
                "point_layer_keyword": {"type": "string", "description": "Keyword to find point layer", "default": "dvf"},
                "band_layer_keyword": {"type": "string", "description": "Keyword to find band polygons", "default": "bande"},
                "extra_polygon_keywords": {"type": "array", "items": {"type": "string"}, "description": "Keywords for extra polygon layers", "default": ["submersion"]},
                "temporal_field": {"type": "string", "description": "Field name for time dimension", "default": "year"},
                "value_field": {"type": "string", "description": "Field name for the value to color-code", "default": "price_m2"},
                "band_field": {"type": "string", "description": "Field name for spatial band assignment", "default": "coastal_band"},
                "include_fields": {"type": "array", "items": {"type": "string"}, "description": "Field names to include in GeoJSON output", "default": []}
            },
            "required": []
        }
    },
    # ── QField Export ──────────────────────────────────────────────
    {
        "name": "export_qfield",
        "description": "Export the current QGIS project as a QField-ready package (ZIP). Contains .qgz with relative GPKG sources + all vector layers materialized as individual GPKGs. Optionally includes an editable Observations layer with QField-compatible form widgets (dropdowns, date picker, camera/photo) for field data collection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Name for the exported project (default: current project name)"},
                "include_observations_layer": {"type": "boolean", "description": "Add an editable Observations layer for field data collection (default: true)", "default": True},
                "max_features_per_layer": {"type": "integer", "description": "Max features per exported layer (default: 50000)", "default": 50000}
            },
            "required": []
        }
    },
    # ── Grist Export ─────────────────────────────────────────────
    {
        "name": "export_grist",
        "description": "Export as a .grist file (SQLite). Two modes: (1) From QGIS project layers (default) — creates tables, typed columns, map widget, stats, form. (2) From HTML file (html_path) — takes any HTML containing GeoJSON (export_web_map, export_flood_map, export_temporal_map, qgis2web, or any Leaflet HTML), extracts data into Grist tables, and transforms the original map into a Grist custom widget reading from those tables. Same interactive map, but data lives in Grist.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "html_path": {"type": "string", "description": "Path to any HTML file containing GeoJSON data (from export_web_map, export_flood_map, export_temporal_map, qgis2web, or any Leaflet HTML with inline FeatureCollections). Converts it into a .grist document with data in tables and the original map as a Grist custom widget."},
                "document_name": {"type": "string", "description": "Document name (default: derived from html filename or project name)"},
                "max_features_per_layer": {"type": "integer", "description": "Max features per layer (default: 50000)", "default": 50000},
                "include_stats": {"type": "boolean", "description": "Generate stats summary table (default: true)", "default": True},
                "detect_relationships": {"type": "boolean", "description": "Auto-detect Ref columns between tables (default: true)", "default": True},
                "timezone": {"type": "string", "description": "Timezone for DateTime columns (default: Europe/Paris)", "default": "Europe/Paris"}
            },
            "required": []
        }
    },
    # ── Recipes ───────────────────────────────────────────────────
    {
        "name": "list_recipes",
        "description": "List available workflow recipes. Recipes are step-by-step guides for common GIS analyses (building density, urban analysis, flood risk, land cover). Execute them by calling get_recipe then following each step.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_recipe",
        "description": "Get a specific recipe with parameters resolved. Returns ordered steps to execute using existing tools (set_study_zone, smart_load, run_processing, etc.). Follow each step sequentially.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Recipe ID (e.g. 'densite_bati')"},
                "zone": {"type": "string", "description": "Study area (commune name or address)", "default": ""},
                "grid_size": {"type": "number", "description": "Grid cell size in meters (for density recipes)", "default": 500}
            },
            "required": ["id"]
        }
    },
    {
        "name": "run_recipe",
        "description": "Execute a complete recipe automatically in one shot. Runs all steps sequentially (zone setup → data loading → analysis → styling → layout → export). Much faster than executing steps manually. Use list_recipes to see available recipes. Returns per-step results and a final screenshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Recipe ID (e.g. 'risque_inondation', 'densite_bati')"},
                "zone": {"type": "string", "description": "Study area (commune name or address, e.g. 'Béziers', 'Montpellier')"},
                "grid_size": {"type": "number", "description": "Grid cell size in meters (for density recipes)", "default": 500},
                "new_project": {"type": "boolean", "description": "Start a fresh project before running (default true)", "default": True}
            },
            "required": ["id", "zone"]
        }
    },
    {
        "name": "publish_artifact",
        "description": "Publie un livrable (storymap, flux, recipe, dataset, pdf) sur S3 via le hub. POST {HUB_URL}/publish/{kind}/{slug}. Retourne hub_url public stable à donner à l'user. Le fichier doit déjà exister sur le workspace au chemin par défaut /data/studies/{sid}/exports/{kind}/{slug}.{ext} (ou /data/exports/{kind}/{slug}.{ext} hors étude), sauf si tu passes source explicite. Requiert HUB_URL + HUB_API_KEY env. Préfère ce tool plutôt qu'un urllib brut en execute_python.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["storymap", "flux", "recipe", "dataset", "pdf"], "description": "Type de livrable"},
                "slug": {"type": "string", "description": "Identifiant URL-safe (ex: 'inondation_marseille4'). Sans accents ni espaces."},
                "source": {"type": "string", "description": "Chemin explicite sous /data/ (optionnel). Si omis, utilise le défaut selon kind + étude active."},
                "hub_url": {"type": "string", "description": "Override HUB_URL (test seulement)"},
                "api_key": {"type": "string", "description": "Override HUB_API_KEY (test seulement)"}
            },
            "required": ["kind", "slug"]
        }
    },
]


# ══════════════════════════════════════════════════════════════════
# RESOURCES DEFINITION
# ══════════════════════════════════════════════════════════════════

RESOURCES = [
    {"uri": UI_RESOURCE_URI, "name": "QGIS Desktop", "description": "Interactive QGIS Desktop — live map canvas with full GUI access.", "mimeType": UI_MIME_TYPE},
    {"uri": "skill://pyqgis", "name": "PyQGIS Reference", "description": "PyQGIS scripting patterns and API usage.", "mimeType": "text/plain"},
    {"uri": "skill://processing", "name": "Processing Algorithms", "description": "QGIS Processing algorithms guide.", "mimeType": "text/plain"},
    {"uri": "skill://cartography", "name": "Cartography Guide", "description": "Symbology, labels, print layouts.", "mimeType": "text/plain"},
    {"uri": "skill://external-services", "name": "External Services", "description": "Vision services integration (Moondream, SAMGeo3, DepthPro).", "mimeType": "text/plain"},
    {"uri": "skill://data-sources", "name": "Data Sources", "description": "French national datasets reference.", "mimeType": "text/plain"},
    {"uri": "skill://helpers", "name": "Python Helpers", "description": "Ready-made Python functions for execute_python (geocode, add_wfs, zoom_to, etc.).", "mimeType": "text/plain"},
    {"uri": "skill://smart-loading", "name": "Smart Loading Pipeline", "description": "Guided data loading: set_study_zone + smart_load (ogr2ogr + GeoPackage). CRS handling, caching, best practices.", "mimeType": "text/plain"},
    {"uri": "skill://recipes", "name": "Recipes Guide", "description": "Workflow recipes: reproducible step-by-step GIS analyses. Use list_recipes + get_recipe.", "mimeType": "text/plain"},
    {"uri": "skill://solar", "name": "Solar Pipeline", "description": "Cadastre solaire: irradiance, r.sun, ray-marching, facade analysis, z-scores. Configurable profiles (rapide/standard/precision).", "mimeType": "text/plain"},
    {"uri": "skill://qgis-status", "name": "QGIS Status", "description": "Current QGIS instance status.", "mimeType": "text/plain"},
]

SKILL_MAP = {
    "skill://pyqgis": "pyqgis",
    "skill://processing": "processing",
    "skill://cartography": "cartography",
    "skill://external-services": "external_services",
    "skill://data-sources": "data_sources",
    "skill://helpers": "helpers",
    "skill://smart-loading": "smart_loading",
    "skill://recipes": "recipes",
    "skill://solar": "solar",
}

# ══════════════════════════════════════════════════════════════════
# PROMPTS DEFINITION
# ══════════════════════════════════════════════════════════════════

PROMPTS = [
    {
        "name": "analyse_territoire",
        "description": "Template for territory analysis.",
        "arguments": [
            {"name": "zone", "description": "Geographic zone", "required": False},
            {"name": "theme", "description": "Analysis theme", "required": False},
        ]
    },
    {
        "name": "audit_passages_pietons",
        "description": "Template for pedestrian crossing audit using street-level imagery.",
        "arguments": [
            {"name": "commune", "description": "Commune name", "required": False},
        ]
    },
    {
        "name": "workflow_donnees",
        "description": "Guided workflow for loading and analyzing French geospatial data. Uses smart pipeline (set_study_zone + smart_load) for reliable, fast data loading.",
        "arguments": [
            {"name": "zone", "description": "Study area (commune, address, or region)", "required": True},
            {"name": "theme", "description": "Analysis theme: urbanisme, environnement, transport, agriculture, risques", "required": False},
        ]
    },
]


def get_prompt_content(name: str, arguments: dict) -> list:
    if name == "analyse_territoire":
        zone = arguments.get("zone", "[à préciser]")
        theme = arguments.get("theme", "general")
        return [{"type": "text", "text": f"""Analyse territoriale pour : {zone}
Thème : {theme}

1. Géocodez la zone (execute_python + BAN API)
2. Chargez les données (BD TOPO, OCS GE, ortho)
3. Traitements Processing adaptés
4. Screenshot pour vérification
5. VNC pour exploration interactive
6. Export (GeoPackage, PDF)

Skills : skill://data-sources, skill://processing, skill://cartography
Services vision : Moondream ({MOONDREAM_URL}), SAMGeo3 ({SAMGEO3_URL}), DepthPro ({DEPTHPRO_URL})"""}]

    elif name == "audit_passages_pietons":
        commune = arguments.get("commune", "[à préciser]")
        return [{"type": "text", "text": f"""Audit passages piétons — {commune}

1. Géocodez la commune
2. BD TOPO (routes, bâtiments)
3. Images Panoramax
4. Moondream ({MOONDREAM_URL}) pour détection
5. Géoréférencement
6. Diagnostic (marquage, PMR, visibilité)
7. Couche résultats + style
8. Rapport PDF"""}]

    elif name == "workflow_donnees":
        zone = arguments.get("zone", "[à préciser]")
        theme = arguments.get("theme", "general")
        theme_layers = {
            "urbanisme": "bdtopo_batiments, bdtopo_routes, ign_cadastre",
            "environnement": "bdtopo_hydrographie, bdtopo_vegetation, corine_land_cover",
            "transport": "bdtopo_routes, bdtopo_voie_ferree, bdtopo_equipement_transport",
            "agriculture": "rpg, bdtopo_hydrographie, corine_land_cover",
            "risques": "bdtopo_hydro_surfaces, bdtopo_batiments, ign_dem",
            "general": "bdtopo_batiments, bdtopo_routes, bdtopo_communes",
        }
        layers = theme_layers.get(theme, theme_layers["general"])
        return [{"type": "text", "text": f"""Workflow données — {zone} ({theme})

Utilise le pipeline smart_load pour charger les données de manière fiable.
Les WFS sont téléchargés en GeoPackage local (index spatial, Processing rapide).

1. set_study_zone(target="{zone}")
   → Géocode, stocke bbox 4326+2154, zoom canvas

2. smart_load(id="osm_xyz") — fond de carte

3. Données thématiques ({theme}):
   {chr(10).join(f'   smart_load(id="{lid.strip()}")' for lid in layers.split(','))}

4. get_screenshot — vérifier que les données sont au bon endroit

5. Analyse Processing adaptée au thème :
   - urbanisme : densité bâti (creategrid + countpointsinpolygon), distances routes
   - environnement : buffer cours d'eau, intersection végétation
   - transport : réseau routier (v.clean), zones de desserte (service area)
   - agriculture : surfaces par culture (dissolve + area), proximité eau
   - risques : zones inondables (buffer hydro), bâtiments exposés (intersection)

6. Mise en forme : set_layer_style (graduated/categorized), labels

7. Export : print layout (titre, légende, échelle, sources) → export_pdf

Skills : skill://smart-loading, skill://processing, skill://cartography, skill://data-sources"""}]

    return [{"type": "text", "text": f"Unknown prompt: {name}"}]


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def _accepts_sse(accept_header: str) -> bool:
    """Return True if the Accept header includes text/event-stream as a media type.

    Parses properly so that 'text/event-stream-custom' does not match.
    """
    for part in accept_header.split(","):
        media_type = part.strip().split(";")[0].strip()
        if media_type == "text/event-stream":
            return True
    return False


# ══════════════════════════════════════════════════════════════════
# TOOL EXECUTION
# ══════════════════════════════════════════════════════════════════

def _auto_screenshot() -> list:
    """Take a screenshot and return it as an MCP image content block (JPEG ≤ 1MB).
    Returns empty list on failure so it can be safely concatenated."""
    time.sleep(0.3)  # let QGIS render
    resp = qgis_command("screenshot", {"width": 1280, "height": 720})
    if "image_base64" in resp:
        return [{"type": "image", "data": resp["image_base64"], "mimeType": "image/jpeg"}]
    return []


def _extract_context(response: dict) -> list:
    """Extract _context from bridge response and format as a compact text block.
    Returns a list with one text content item, or empty list if no context."""
    ctx = response.pop("_context", None)
    if not ctx:
        return []
    zone = ctx.get("study_zone") or "none"
    phase = ctx.get("phase", "?")
    layers = ctx.get("layers", [])
    rasters = ctx.get("raster_count", 0)
    hint = ctx.get("hint", "")
    vec_count = len(layers)
    total = vec_count + rasters
    parts = [f"phase={phase}", f"zone={zone}", f"{total} layers ({vec_count} vector, {rasters} raster)"]
    if ctx.get("has_layouts"):
        parts.append("layouts=yes")
    line = " | ".join(parts)
    text = f"\n--- Context: {line}"
    if hint:
        text += f"\n    Hint: {hint}"
    return [{"type": "text", "text": text}]


def _text(response, **kwargs) -> list:
    """Format a bridge response as MCP text content, extracting _context if present."""
    ctx_content = _extract_context(response)
    return [{"type": "text", "text": json.dumps(response, default=str, **kwargs)}] + ctx_content


def _error(message: str) -> dict:
    """Return an MCP error response."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _validate_required(arguments: dict, *fields) -> Optional[str]:
    """Return error message if any required field is absent or None, else None.

    Uses `is None` (not falsiness) so that 0, False and {} are accepted —
    e.g. mouse_click(x=0, y=0) or run_processing(parameters={}) must not fail.
    """
    missing = [f for f in fields if arguments.get(f) is None]
    if missing:
        return f"Missing required parameter(s): {', '.join(missing)}"
    return None


# ── Tool handlers ─────────────────────────────────────────────
# Each function takes (arguments: dict) and returns an MCP result dict.

def _tool_qgis_desktop_ui(arguments: dict) -> dict:
    novnc_port = VNC_PORT  # default single-user
    if MULTI_USER_MODE and container_manager:
        uid = current_user_id.get(None)
        if uid:
            session = container_manager.get_session(uid)
            if session:
                novnc_port = session.novnc_port
    novnc_url = f"http://localhost:{novnc_port}/vnc.html?autoconnect=true&resize=scale"
    return {"content": [{"type": "text", "text": f"QGIS Desktop UI opened. Direct noVNC access: {novnc_url}"}]}


def _tool_execute_python(arguments: dict) -> dict:
    err = _validate_required(arguments, "code")
    if err:
        return _error(err)
    user_timeout = arguments.get("timeout", 60)
    response = qgis_command("execute_python",
                            {"code": arguments["code"], "timeout": user_timeout},
                            timeout=user_timeout + 30)
    return {"content": _text(response, indent=2) + _auto_screenshot()}


def _tool_get_screenshot(arguments: dict) -> dict:
    width = arguments.get("width", 1280)
    height = arguments.get("height", 720)
    response = qgis_command("screenshot", {"width": width, "height": height})
    if "error" in response:
        return {"content": _text(response)}
    return {"content": [{"type": "image", "data": response.get("image_base64", ""), "mimeType": "image/jpeg"}]}


def _tool_get_project_info(arguments: dict) -> dict:
    response = qgis_command("get_project_info")
    return {"content": _text(response, indent=2)}


def _tool_new_project(arguments: dict) -> dict:
    title = arguments.get("title", "New Project")
    crs = arguments.get("crs", "EPSG:2154")
    response = qgis_command("new_project", {"title": title, "crs": crs})
    return {"content": _text(response) + _auto_screenshot()}


def _tool_open_project(arguments: dict) -> dict:
    err = _validate_required(arguments, "path")
    if err:
        return _error(err)
    response = qgis_command("open_project", {"path": arguments["path"]})
    return {"content": _text(response) + _auto_screenshot()}


def _tool_save_project(arguments: dict) -> dict:
    response = qgis_command("save_project", {"path": arguments.get("path", "")})
    return {"content": _text(response)}


def _tool_add_layer(arguments: dict) -> dict:
    err = _validate_required(arguments, "uri")
    if err:
        return _error(err)
    uri = arguments["uri"]
    layer_name = arguments.get("name", "layer")
    layer_type = arguments.get("layer_type", "vector")
    provider = arguments.get("provider", "")
    if layer_type == "wfs":
        response = qgis_command("add_wfs_layer", {"url": uri, "typename": layer_name, "name": layer_name})
    elif layer_type == "wms":
        response = qgis_command("add_wms_layer", {"url": uri, "layers": layer_name, "name": layer_name})
    elif layer_type == "raster":
        response = qgis_command("add_raster_layer", {"uri": uri, "name": layer_name, "provider": provider or "gdal"})
    else:
        response = qgis_command("add_vector_layer", {"uri": uri, "name": layer_name, "provider": provider or "ogr"})
    return {"content": _text(response) + _auto_screenshot()}


def _tool_remove_layer(arguments: dict) -> dict:
    err = _validate_required(arguments, "layer_id")
    if err:
        return _error(err)
    response = qgis_command("remove_layer", {"layer_id": arguments["layer_id"]})
    return {"content": _text(response) + _auto_screenshot()}


def _tool_get_features(arguments: dict) -> dict:
    err = _validate_required(arguments, "layer_id")
    if err:
        return _error(err)
    response = qgis_command("get_features", {
        "layer_id": arguments["layer_id"],
        "filter": arguments.get("filter", ""),
        "limit": arguments.get("limit", 100),
        "include_geometry": arguments.get("include_geometry", True),
    })
    return {"content": _text(response, indent=2)}


def _tool_run_processing(arguments: dict) -> dict:
    err = _validate_required(arguments, "algorithm", "parameters")
    if err:
        return _error(err)
    response = qgis_command("run_processing", {
        "algorithm": arguments["algorithm"],
        "parameters": arguments["parameters"],
    })
    return {"content": _text(response, indent=2) + _auto_screenshot()}


def _tool_search_algorithms(arguments: dict) -> dict:
    response = qgis_command("list_algorithms", {
        "search": arguments.get("search", ""),
        "provider": arguments.get("provider", ""),
        "limit": arguments.get("limit", 20),
    })
    return {"content": _text(response, indent=2)}


def _tool_zoom_to(arguments: dict) -> dict:
    params = {}
    if arguments.get("extent"):
        params["extent"] = arguments["extent"]
    elif arguments.get("layer_id"):
        params["layer_id"] = arguments["layer_id"]
    response = qgis_command("zoom_to_extent", params)
    return {"content": _text(response) + _auto_screenshot()}


def _tool_export_pdf(arguments: dict) -> dict:
    err = _validate_required(arguments, "layout")
    if err:
        return _error(err)
    params = {"layout": arguments["layout"]}
    if arguments.get("output_path"):
        params["output_path"] = arguments["output_path"]
    response = qgis_command("export_pdf", params)
    # Bridge already caps inline content at MAX_INLINE_FILE (5MB).
    # Files above that have no content_base64, only a download_url.
    content = [{"type": "text", "text": json.dumps({k: v for k, v in response.items() if k != "content_base64"}, default=str)}]
    if response.get("content_base64"):
        content.append({
            "type": "resource",
            "resource": {
                "uri": f"file://{response.get('path', 'export.pdf')}",
                "mimeType": response.get("mime_type", "application/pdf"),
                "blob": response["content_base64"],
            }
        })
    return {"content": content}


def _tool_mouse_click(arguments: dict) -> dict:
    err = _validate_required(arguments, "x", "y")
    if err:
        return _error(err)
    response = qgis_command("mouse_click", {
        "x": arguments["x"], "y": arguments["y"],
        "button": arguments.get("button", 1), "double": arguments.get("double", False)
    })
    return {"content": _text(response) + _auto_screenshot()}


def _tool_mouse_scroll(arguments: dict) -> dict:
    err = _validate_required(arguments, "x", "y")
    if err:
        return _error(err)
    response = qgis_command("mouse_scroll", {
        "x": arguments["x"], "y": arguments["y"],
        "direction": arguments.get("direction", "down"), "clicks": arguments.get("clicks", 3)
    })
    return {"content": _text(response) + _auto_screenshot()}


def _tool_key_press(arguments: dict) -> dict:
    err = _validate_required(arguments, "key")
    if err:
        return _error(err)
    response = qgis_command("key_press", {"key": arguments["key"]})
    return {"content": _text(response) + _auto_screenshot()}


def _tool_mouse_drag(arguments: dict) -> dict:
    err = _validate_required(arguments, "x1", "y1", "x2", "y2")
    if err:
        return _error(err)
    response = qgis_command("mouse_drag", {
        "x1": arguments["x1"], "y1": arguments["y1"],
        "x2": arguments["x2"], "y2": arguments["y2"],
        "button": arguments.get("button", 1)
    })
    return {"content": _text(response) + _auto_screenshot()}


def _tool_upload_file(arguments: dict) -> dict:
    err = _validate_required(arguments, "name")
    if err:
        return _error(err)
    name = arguments["name"]
    url = arguments.get("url")
    content_base64 = arguments.get("content_base64")
    base_url = _get_user_api_base_url()
    api_port = _get_user_api_port()

    # Public endpoint for external clients (host-mapped port)
    upload_endpoint = f"http://localhost:{api_port}/api/upload"

    if url:
        # Mode URL: server fetches the file directly — no size limit from MCP
        try:
            with httpx.Client(timeout=120) as client:
                with client.stream("GET", url) as dl:
                    dl.raise_for_status()
                    data = dl.read()
                files = {"file": (name, data, "application/octet-stream")}
                resp = client.post(f"{base_url}/api/upload", files=files)
                resp.raise_for_status()
                result = resp.json()
                result["upload_endpoint"] = upload_endpoint
                return {"content": _text(result)}
        except httpx.HTTPStatusError as e:
            return _error(f"Upload failed: HTTP {e.response.status_code}")
        except Exception as e:
            return _error(f"Upload failed: {e}")
    elif content_base64:
        # Mode base64: legacy, for small files
        response = qgis_command("write_file", {
            "name": name,
            "content_base64": content_base64,
        })
        return {"content": _text(response)}
    else:
        # No data provided — return the multipart endpoint for direct upload
        # The client can POST multipart/form-data with field "file" to this URL
        return {"content": _text({
            "action": "upload_ready",
            "upload_endpoint": upload_endpoint,
            "method": "POST",
            "content_type": "multipart/form-data",
            "field_name": "file",
            "max_size_mb": 50,
            "instructions": f"POST your file as multipart/form-data (field='file') to the endpoint above. "
                           f"Example: curl -F 'file=@{name}' {upload_endpoint}"
        })}


def _tool_download_file(arguments: dict) -> dict:
    err = _validate_required(arguments, "path")
    if err:
        return _error(err)
    response = qgis_command("read_file", {"path": arguments["path"]})
    if "error" in response:
        return {"content": _text(response)}
    # Bridge already caps inline content at MAX_INLINE_FILE (5MB).
    content = [{"type": "text", "text": json.dumps({k: v for k, v in response.items() if k != "content_base64"}, default=str)}]
    if response.get("content_base64"):
        content.append({
            "type": "resource",
            "resource": {
                "uri": f"file://{response.get('path', '')}",
                "mimeType": response.get("mime_type", "application/octet-stream"),
                "blob": response["content_base64"],
            }
        })
    return {"content": content}


def _tool_list_files(arguments: dict) -> dict:
    response = qgis_command("list_files", {"pattern": arguments.get("pattern", "*")})
    return {"content": _text(response, indent=2)}


def _tool_export_layer(arguments: dict) -> dict:
    err = _validate_required(arguments, "layer_id")
    if err:
        return _error(err)
    response = qgis_command("export_layer", {
        "layer_id": arguments["layer_id"],
        "format": arguments.get("format", "GPKG"),
        "name": arguments.get("name", ""),
    })
    return {"content": _text(response)}


def _tool_download_project(arguments: dict) -> dict:
    response = qgis_command("download_project", {"name": arguments.get("name", "project")})
    return {"content": _text(response)}


def _tool_delete_file(arguments: dict) -> dict:
    err = _validate_required(arguments, "path")
    if err:
        return _error(err)
    response = qgis_command("delete_file", {"path": arguments["path"]})
    return {"content": _text(response)}


def _tool_list_datasources(arguments: dict) -> dict:
    response = qgis_command("list_datasources", {
        "category": arguments.get("category", ""),
        "search": arguments.get("search", ""),
    })
    return {"content": _text(response, indent=2)}


def _tool_add_from_catalog(arguments: dict) -> dict:
    err = _validate_required(arguments, "id")
    if err:
        return _error(err)
    params = {"id": arguments["id"]}
    if arguments.get("name"):
        params["name"] = arguments["name"]
    if arguments.get("bbox"):
        params["bbox"] = arguments["bbox"]
    response = qgis_command("add_from_catalog", params)
    content = _text(response)
    if response.get("success"):
        content += _auto_screenshot()
    return {"content": content}


def _tool_set_study_zone(arguments: dict) -> dict:
    err = _validate_required(arguments, "target")
    if err:
        return _error(err)
    params = {"target": arguments["target"]}
    if arguments.get("buffer_km"):
        params["buffer_km"] = arguments["buffer_km"]
    response = qgis_command("set_study_zone", params)
    return {"content": _text(response, indent=2) + _auto_screenshot()}


def _tool_get_study_zone(arguments: dict) -> dict:
    response = qgis_command("get_study_zone", {})
    return {"content": _text(response, indent=2)}


def _tool_smart_load(arguments: dict) -> dict:
    err = _validate_required(arguments, "id")
    if err:
        return _error(err)
    params = {"id": arguments["id"]}
    if arguments.get("bbox"):
        params["bbox"] = arguments["bbox"]
    if arguments.get("max_features"):
        params["max_features"] = arguments["max_features"]
    if arguments.get("name"):
        params["name"] = arguments["name"]
    response = qgis_command("smart_load", params, timeout=SOCKET_TIMEOUT_LONG)
    content = _text(response, indent=2)
    if not response.get("error"):
        content += _auto_screenshot()
    return {"content": content}


def _tool_set_layer_style(arguments: dict) -> dict:
    err = _validate_required(arguments, "layer_id")
    if err:
        return _error(err)
    style_type = arguments.get("style_type", "single")
    if style_type in ("categorized", "graduated") and not arguments.get("field"):
        return _error(f"'{style_type}' style requires a 'field' parameter")
    response = qgis_command("set_layer_style", {
        "layer_id": arguments["layer_id"],
        "style_type": style_type,
        "color": arguments.get("color", "65,105,225,180"),
        "field": arguments.get("field", ""),
        "categories": arguments.get("categories", {}),
        "ranges": arguments.get("ranges", []),
    })
    return {"content": _text(response) + _auto_screenshot()}


def _tool_set_layer_visibility(arguments: dict) -> dict:
    err = _validate_required(arguments, "layer_id")
    if err:
        return _error(err)
    response = qgis_command("set_layer_visibility", {
        "layer_id": arguments["layer_id"],
        "visible": arguments.get("visible", True),
    })
    return {"content": _text(response) + _auto_screenshot()}


# ── Layout templates ──────────────────────────────────────────

def _tool_list_layout_templates(arguments: dict) -> dict:
    response = qgis_command("list_layout_templates", {})
    return {"content": _text(response, indent=2)}


def _tool_apply_layout_template(arguments: dict) -> dict:
    err = _validate_required(arguments, "template")
    if err:
        return _error(err)
    params = {"template_id": arguments["template"]}
    if arguments.get("variables"):
        params["variables"] = arguments["variables"]
    if arguments.get("name"):
        params["name"] = arguments["name"]
    response = qgis_command("apply_layout_template", params)
    return {"content": _text(response, indent=2) + _auto_screenshot()}


# ── Web map export ────────────────────────────────────────────

def _tool_export_web_map(arguments: dict) -> dict:
    params = {}
    if arguments.get("title"):
        params["title"] = arguments["title"]
    if arguments.get("max_features"):
        params["max_features"] = arguments["max_features"]
    if arguments.get("output_path"):
        params["output_path"] = arguments["output_path"]
    response = qgis_command("export_web_map", params)
    return {"content": _text(response, indent=2)}


# ── Interactive flood map ─────────────────────────────────────

def _tool_export_flood_map(arguments: dict) -> dict:
    params = {}
    if arguments.get("title"):
        params["title"] = arguments["title"]
    if arguments.get("max_features"):
        params["max_features"] = arguments["max_features"]
    if arguments.get("output_path"):
        params["output_path"] = arguments["output_path"]
    if arguments.get("include_fields"):
        params["include_fields"] = arguments["include_fields"]
    response = qgis_command("export_flood_map", params, timeout=SOCKET_TIMEOUT_LONG)
    return {"content": _text(response, indent=2)}


def _tool_export_temporal_map(arguments: dict) -> dict:
    params = {}
    for key in ("title", "max_features", "output_path", "point_layer_keyword",
                "band_layer_keyword", "extra_polygon_keywords", "temporal_field",
                "value_field", "band_field", "include_fields"):
        if arguments.get(key):
            params[key] = arguments[key]
    response = qgis_command("export_temporal_map", params, timeout=SOCKET_TIMEOUT_LONG)
    return {"content": _text(response, indent=2)}


def _tool_export_qfield(arguments: dict) -> dict:
    params = {}
    for key in ("project_name", "include_observations_layer", "max_features_per_layer"):
        if key in arguments:
            params[key] = arguments[key]
    response = qgis_command("export_qfield", params, timeout=SOCKET_TIMEOUT_LONG)
    return {"content": _text(response, indent=2)}


def _tool_export_grist(arguments: dict) -> dict:
    params = {}
    for key in ("html_path", "document_name", "max_features_per_layer", "include_stats",
                "detect_relationships", "timezone"):
        if key in arguments:
            params[key] = arguments[key]
    response = qgis_command("export_grist", params, timeout=SOCKET_TIMEOUT_LONG)
    return {"content": _text(response, indent=2)}


# ── Recipes ───────────────────────────────────────────────────

def _tool_list_recipes(arguments: dict) -> dict:
    response = qgis_command("list_recipes", {})
    return {"content": _text(response, indent=2)}


def _tool_get_recipe(arguments: dict) -> dict:
    err = _validate_required(arguments, "id")
    if err:
        return _error(err)
    params = {"id": arguments["id"]}
    # Forward all extra params for recipe substitution
    for key in ("zone", "grid_size", "max_features"):
        if arguments.get(key):
            params[key] = arguments[key]
    response = qgis_command("get_recipe", params)
    return {"content": _text(response, indent=2)}


# ── Publish artifact to hub S3 (storymap, flux, recipe, dataset, pdf) ─

_PUBLISH_KINDS = frozenset({"storymap", "flux", "recipe", "dataset", "pdf"})


def _tool_publish_artifact(arguments: dict) -> dict:
    """Publie un livrable (storymap, flux, recipe, dataset, pdf) sur S3 via le hub.

    Lit le fichier sur le workspace pod et POST sur `{HUB_URL}/publish/{kind}/{slug}`
    avec authentification Bearer. Retourne `hub_url` (URL publique stable, proxy hub
    masquant MinIO) à fournir directement à l'user.

    Requiert dans l'env du conteneur :
      - HUB_URL  : URL du hub (ex: https://user-xxx-qgis-mcp-bridge.user.lab.sspcloud.fr)
      - HUB_API_KEY : clé API émise par le hub pour ce user
    """
    err = _validate_required(arguments, "kind", "slug")
    if err:
        return _error(err)

    kind = arguments["kind"]
    slug = arguments["slug"]
    if kind not in _PUBLISH_KINDS:
        return _error(f"kind invalide : {kind}. Attendu : {sorted(_PUBLISH_KINDS)}")

    import os as _os
    hub_url = arguments.get("hub_url") or _os.environ.get("HUB_URL", "")
    api_key = arguments.get("api_key") or _os.environ.get("HUB_API_KEY", "")
    if not hub_url:
        return _error("HUB_URL absent (env var ou arg) — impossible de joindre le hub")
    if not api_key:
        return _error("HUB_API_KEY absent (env var ou arg) — auth hub impossible. "
                      "Le hub doit injecter cette clé dans le pod workspace au scale-up.")

    body = {}
    if arguments.get("source"):
        body["source"] = arguments["source"]

    import json as _json
    import urllib.request as _ur
    import urllib.error as _ue
    url = f"{hub_url.rstrip('/')}/publish/{kind}/{slug}"
    payload = _json.dumps(body).encode() if body else b"{}"
    req = _ur.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with _ur.urlopen(req, timeout=120) as r:
            data = _json.loads(r.read().decode("utf-8", errors="replace"))
        return {"content": _text({
            "success": True,
            "kind": kind,
            "slug": slug,
            "hub_url": data.get("hub_url"),
            "key": data.get("key"),
            "size": data.get("size"),
            "published_at": data.get("published_at"),
            "study_id": data.get("study_id"),
        }, indent=2)}
    except _ue.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")[:500]
        return _error(f"Hub HTTP {e.code} sur {url} : {body_txt}")
    except Exception as e:
        return _error(f"Publish failed ({type(e).__name__}): {e}")


# ── Run recipe (automated execution) ─────────────────────────

# Actions that need longer timeouts (WFS downloads, heavy exports)
_LONG_TIMEOUT_ACTIONS = frozenset({
    "smart_load", "export_flood_map", "export_web_map", "export_temporal_map", "export_qfield", "export_grist", "execute_python",
})


def _tool_run_recipe(arguments: dict) -> dict:
    """Execute a complete recipe in one shot — all steps sequentially."""
    err = _validate_required(arguments, "id", "zone")
    if err:
        return _error(err)

    recipe_id = arguments["id"]
    zone = arguments["zone"]

    # 1. Optionally start a new project
    if arguments.get("new_project", True):
        qgis_command("new_project", {"title": f"{recipe_id} — {zone}"})

    # 2. Get the resolved recipe (with $zone substituted)
    recipe_params = {"id": recipe_id, "zone": zone}
    if arguments.get("grid_size"):
        recipe_params["grid_size"] = arguments["grid_size"]

    recipe_resp = qgis_command("get_recipe", recipe_params)
    if "error" in recipe_resp:
        return _error(f"Recipe not found: {recipe_resp['error']}")

    steps = recipe_resp.get("steps", [])
    total = len(steps)

    # 3. Execute each step sequentially
    step_results = []
    stopped = False

    for i, step in enumerate(steps):
        step_id = step.get("id", f"step_{i}")
        action = step.get("tool", "")
        description = step.get("description", "")
        params = dict(step.get("params", {}))

        # Special handling: execute_python has 'code' at step level
        if action == "execute_python":
            params["code"] = step.get("code", "")
            params.setdefault("timeout", 180)

        # Special handling: apply_layout_template uses template_id in bridge
        if action == "apply_layout_template" and "template" in params:
            params["template_id"] = params.pop("template")

        # Determine timeout — execute_python in recipes can be very heavy (spatial joins on 100k+
        # features), so use the recipe timeout (1200s) instead of the standard long timeout (300s).
        if action == "execute_python":
            timeout = SOCKET_TIMEOUT_RECIPE
        elif action in _LONG_TIMEOUT_ACTIONS:
            timeout = SOCKET_TIMEOUT_LONG
        else:
            timeout = SOCKET_TIMEOUT

        # Execute
        resp = qgis_command(action, params, timeout=timeout)
        success = "error" not in resp

        step_result = {
            "step": f"{i + 1}/{total}",
            "id": step_id,
            "tool": action,
            "description": description,
            "success": success,
        }

        if not success:
            step_result["error"] = resp.get("error", "Unknown error")
        else:
            # Include key metrics from response (keep it compact)
            for key in ("feature_count", "layer_id", "name", "path",
                        "download_url", "size", "stats"):
                if key in resp:
                    step_result[key] = resp[key]
            # For execute_python, include the result dict
            if action == "execute_python" and "result" in resp:
                step_result["result"] = resp["result"]

        step_results.append(step_result)

        # Stop on critical failure (zone setup must succeed)
        if not success and action == "set_study_zone":
            stopped = True
            break

    # 4. Build summary
    succeeded = sum(1 for r in step_results if r["success"])
    failed = sum(1 for r in step_results if not r["success"])

    response = {
        "recipe": recipe_id,
        "zone": zone,
        "total_steps": total,
        "executed": len(step_results),
        "succeeded": succeeded,
        "failed": failed,
        "stopped_early": stopped,
        "steps": step_results,
        "outputs": recipe_resp.get("outputs", []),
    }

    return {"content": _text(response, indent=2) + _auto_screenshot()}


async def stream_run_recipe(arguments: dict, msg_id: Any, session_id: str,
                            progress_token: Any, user_id: str = "default"):
    """Async generator — streams run_recipe progress via SSE (MCP notifications/progress).

    Yields SSE events:
      - notifications/progress  for each step (keeps connection alive, shows progress)
      - final tools/call result as the last event
    """
    # Set ContextVar here (not in handle_mcp) because the generator runs after
    # the handler returns, so any ContextVar set in handle_mcp would already be reset.
    _cv_token = current_user_id.set(user_id)

    def sse(data: dict) -> str:
        return f"event: message\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def progress(step: int, total: int, message: str) -> str:
        if progress_token is not None:
            return sse({
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": progress_token, "progress": step,
                           "total": total, "message": message},
            })
        # No token — SSE comment keeps the TCP connection alive without a JSON event
        return f": {step}/{total} {message}\n\n"

    err = _validate_required(arguments, "id", "zone")
    if err:
        yield sse({"jsonrpc": "2.0", "id": msg_id,
                   "error": {"code": -32602, "message": err}})
        return

    recipe_id = arguments["id"]
    zone      = arguments["zone"]

    yield progress(0, 1, f"Démarrage : {recipe_id} — {zone}")

    # 1. New project
    if arguments.get("new_project", True):
        await asyncio.to_thread(qgis_command, "new_project",
                                {"title": f"{recipe_id} — {zone}"})

    # 2. Resolve recipe
    recipe_params = {"id": recipe_id, "zone": zone}
    if arguments.get("grid_size"):
        recipe_params["grid_size"] = arguments["grid_size"]

    recipe_resp = await asyncio.to_thread(qgis_command, "get_recipe", recipe_params)
    if "error" in recipe_resp:
        yield sse({"jsonrpc": "2.0", "id": msg_id,
                   "error": {"code": -32602,
                             "message": f"Recipe not found: {recipe_resp['error']}"}})
        return

    steps   = recipe_resp.get("steps", [])
    total   = len(steps)
    results = []
    stopped = False

    # 3. Execute each step, yielding progress before each
    for i, step in enumerate(steps):
        step_id     = step.get("id", f"step_{i}")
        action      = step.get("tool", "")
        description = step.get("description", "")
        params      = dict(step.get("params", {}))

        if action == "execute_python":
            params["code"] = step.get("code", "")
            params.setdefault("timeout", 180)
        if action == "apply_layout_template" and "template" in params:
            params["template_id"] = params.pop("template")

        if action == "execute_python":
            sock_timeout = SOCKET_TIMEOUT_RECIPE
        elif action in _LONG_TIMEOUT_ACTIONS:
            sock_timeout = SOCKET_TIMEOUT_LONG
        else:
            sock_timeout = SOCKET_TIMEOUT

        yield progress(i, total, f"Étape {i + 1}/{total} : {description}")

        # Run the bridge call in a thread and send SSE keepalive comments every 25s so the
        # client connection never times out during long operations (smart_load can take 300s).
        task = asyncio.ensure_future(asyncio.to_thread(qgis_command, action, params, sock_timeout))
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=25)
            if not done:
                yield f": keepalive\n\n"
        resp = task.result()
        success = "error" not in resp

        step_result = {
            "step": f"{i + 1}/{total}",
            "id": step_id,
            "tool": action,
            "description": description,
            "success": success,
        }
        if not success:
            step_result["error"] = resp.get("error", "Unknown error")
        else:
            for key in ("feature_count", "layer_id", "name", "path",
                        "download_url", "size", "stats"):
                if key in resp:
                    step_result[key] = resp[key]
            if action == "execute_python" and "result" in resp:
                step_result["result"] = resp["result"]

        results.append(step_result)

        if not success and action == "set_study_zone":
            stopped = True
            break

    # 4. Final result
    succeeded = sum(1 for r in results if r["success"])
    failed    = sum(1 for r in results if not r["success"])

    response_data = {
        "recipe": recipe_id,
        "zone": zone,
        "total_steps": total,
        "executed": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "stopped_early": stopped,
        "steps": results,
        "outputs": recipe_resp.get("outputs", []),
    }

    yield progress(total, total, f"Terminé — {succeeded}/{total} étapes réussies")

    # Screenshot (async, non-blocking)
    await asyncio.sleep(0.3)  # let QGIS render
    ss_resp  = await asyncio.to_thread(qgis_command, "screenshot", {"width": 1280, "height": 720})
    content  = [{"type": "text", "text": json.dumps(response_data, ensure_ascii=False, indent=2)}]
    if "image_base64" in ss_resp:
        content.append({"type": "image", "data": ss_resp["image_base64"], "mimeType": "image/jpeg"})

    yield sse({"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}})
    current_user_id.reset(_cv_token)


# ── Dispatch table ────────────────────────────────────────────

TOOL_HANDLERS = {
    "qgis_desktop_ui": _tool_qgis_desktop_ui,
    "execute_python": _tool_execute_python,
    "get_screenshot": _tool_get_screenshot,
    "get_project_info": _tool_get_project_info,
    "new_project": _tool_new_project,
    "open_project": _tool_open_project,
    "save_project": _tool_save_project,
    "add_layer": _tool_add_layer,
    "remove_layer": _tool_remove_layer,
    "get_features": _tool_get_features,
    "run_processing": _tool_run_processing,
    "search_algorithms": _tool_search_algorithms,
    "zoom_to": _tool_zoom_to,
    "export_pdf": _tool_export_pdf,
    "mouse_click": _tool_mouse_click,
    "mouse_scroll": _tool_mouse_scroll,
    "key_press": _tool_key_press,
    "mouse_drag": _tool_mouse_drag,
    "upload_file": _tool_upload_file,
    "download_file": _tool_download_file,
    "list_files": _tool_list_files,
    "export_layer": _tool_export_layer,
    "download_project": _tool_download_project,
    "delete_file": _tool_delete_file,
    "list_datasources": _tool_list_datasources,
    "add_from_catalog": _tool_add_from_catalog,
    "set_study_zone": _tool_set_study_zone,
    "get_study_zone": _tool_get_study_zone,
    "smart_load": _tool_smart_load,
    "set_layer_style": _tool_set_layer_style,
    "set_layer_visibility": _tool_set_layer_visibility,
    "list_layout_templates": _tool_list_layout_templates,
    "apply_layout_template": _tool_apply_layout_template,
    "export_web_map": _tool_export_web_map,
    "export_flood_map": _tool_export_flood_map,
    "export_temporal_map": _tool_export_temporal_map,
    "export_qfield": _tool_export_qfield,
    "export_grist": _tool_export_grist,
    "list_recipes": _tool_list_recipes,
    "get_recipe": _tool_get_recipe,
    "run_recipe": _tool_run_recipe,
    "publish_artifact": _tool_publish_artifact,
}


def execute_tool(name: str, arguments: dict) -> dict:
    """Execute a tool via dispatch table. Returns MCP content result."""
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return _error(f"Unknown tool: {name}")
    return handler(arguments)


# ══════════════════════════════════════════════════════════════════
# MCP JSON-RPC HANDLER
# ══════════════════════════════════════════════════════════════════

SERVER_INFO = {
    "name": "QgisRemoteMCP",
    "version": "1.0.0",
}

INSTRUCTIONS = f"""You control a live QGIS Desktop instance. Every modifying tool automatically returns a screenshot so you always see the result.

## Recommended workflow for data analysis
1. **set_study_zone** — Define where: "Montpellier", "Sete", "Gare de Lyon, Paris". Stores bbox in project variables.
2. **smart_load** — Load data by catalog ID (e.g. 'bdtopo_batiments'). WFS data is downloaded as local GeoPackage with spatial index (fast for Processing). Rasters stream as usual.
3. **Act** — run_processing, execute_python on local layers (no network delays)
4. **Verify** — get_screenshot, describe what you see
5. **Deliver** — export_layer, export_pdf, download_project

IMPORTANT: Always call set_study_zone BEFORE smart_load for WFS sources. Downloaded WFS layers are in EPSG:2154 (Lambert 93) with R-tree spatial index. Results are cached 24h in /data/cache/.

## Core tools
- **execute_python** — Run PyQGIS code. Access: iface, project, canvas, processing, QgsProject, QgsVectorLayer, etc. Store outputs in `result` dict. A `helpers` module is injected with ready-made functions (see below).
- **get_screenshot** — Capture current QGIS desktop (1280x720 PNG). Already included automatically after modifying tools.
- **add_layer** — Add vector/raster/WFS/WMS layers by URI.
- **run_processing** — Execute any of 1000+ Processing algorithms (native:buffer, gdal:warp, grass7:v.clean, etc.).
- **zoom_to** — Zoom to extent or layer.
- **mouse_click / mouse_scroll / key_press / mouse_drag** — Direct GUI interaction via xdotool (coordinates in 1920x1080 display pixels).
- **qgis_desktop_ui** — Open interactive QGIS view in conversation.

## Python helpers (available in execute_python as `helpers`)
Use these instead of writing boilerplate. Read skill://helpers for full docs and examples.
- `helpers.geocode(address)` — Geocode French address (BAN API) → {{lon, lat, label, score, bbox}}
- `helpers.reverse_geocode(lon, lat)` — Reverse geocode
- `helpers.search_commune(name)` — Search commune info (Geo API)
- `helpers.get_elevation(lon, lat)` — Altitude from IGN
- `helpers.add_wfs(url, typename, bbox, name)` — Add WFS layer (auto bbox from canvas if omitted)
- `helpers.add_wms(url, layers, name)` — Add WMS layer
- `helpers.add_wmts(url, layers, name)` — Add WMTS tiled layer
- `helpers.add_xyz(url, name)` — Add XYZ tile layer
- `helpers.create_point_layer(name, points)` — Memory layer from list of dicts
- `helpers.zoom_to(target)` — Zoom to bbox, point dict, or address string
- `helpers.load_catalog_source(id, bbox)` — Load source from datasources.json by ID
- `helpers.bbox_from_canvas()` — Get current canvas extent in EPSG:4326
- `helpers.set_study_zone(target, buffer_km)` — Define study zone, store in project variables
- `helpers.get_study_zone()` — Read stored study zone (name, bbox_4326, bbox_2154)
- `helpers.download_wfs_ogr(url, typename, bbox_4326)` — Download WFS as local GPKG via ogr2ogr
- `helpers.overpass_query(tags, bbox_4326)` — Query OpenStreetMap via Overpass API. tags: dict like {{"amenity": "school"}} or string "amenity=school". Auto-uses study zone bbox.

## Data catalog (pre-configured French national sources — free, no API key)
- **list_datasources** — Browse available sources: IGN orthophotos, Plan IGN, BD TOPO (buildings, roads, rivers, communes...), OSM, cadastre, DEM, BAN geocoding, Panoramax. Filter by category or search.
- **add_from_catalog** — Add a source by ID (e.g. `osm_xyz`, `bdtopo_batiments`). WFS requires a `bbox` [xmin,ymin,xmax,ymax] in EPSG:4326. Raster sources (WMS/WMTS/XYZ) work without bbox.

## File management
- **upload_file** — Upload a file (base64) into the QGIS container /data/. Supports shapefiles, GeoJSON, GPKG, CSV, TIFF, project files.
- **download_file** — Download a file from /data/. Returns base64 for files <5MB, or a download URL for larger files.
- **list_files** — List files in /data/.
- **export_layer** — Export a vector layer to GPKG, GeoJSON, Shapefile, or CSV. Saved to /data/.
- **download_project** — Save the current project as .qgz to /data/.
- **delete_file** — Delete a file from /data/.
- **export_pdf** — Export a print layout to PDF. Returns base64 for files <5MB.

## Styling
- **set_layer_style** — Apply single color, categorized (by field), or graduated (ranges) symbology.
- **set_layer_visibility** — Show/hide a layer in the layer tree.

## Layout templates & export
- **list_layout_templates** — List available print layout templates.
- **apply_layout_template** — Apply a pre-configured template (a3_landscape, a4_portrait) with dynamic labels. Variables (title, subtitle) are set as QGIS project variables resolved via expressions. Then use export_pdf to generate the PDF.
- **export_web_map** — Export visible vector layers as interactive Leaflet HTML. GeoJSON inline, popups, toggle legend. Returns download URL.
- **export_flood_map** — Export an interactive flood simulation HTML. Water height slider, play/pause animation, building exposure by color, dynamic stats. Requires ISO_HT + building layers loaded (use risque_inondation recipe first).

## Recipes (reproducible workflows)
- **list_recipes** — Browse workflow recipes: building density, urban analysis, flood risk, land cover.
- **get_recipe** — Get a recipe with parameters resolved. Returns step-by-step instructions to follow manually.
- **run_recipe** — Execute a complete recipe automatically in one shot! Runs all steps (zone → data → analysis → style → export) without manual intervention. Much faster than step-by-step.
- When a user asks for a common analysis, check recipes first! Prefer run_recipe(id=..., zone="...") for fully automated execution. Use get_recipe only when you need to inspect or customize individual steps.

## Workflow pattern
1. **get_project_info** → understand current layers, CRS, layouts, extents
2. **Add data** — set_study_zone + smart_load for French data, add_layer for custom URIs, upload_file for user files
3. **Act** — run_processing, execute_python, zoom_to → each returns screenshot
4. **Style** — set_layer_style, set_layer_visibility
5. **Layout** — apply_layout_template (a3_landscape, a4_portrait)
6. **Verify** the screenshot — describe what you see
7. **Deliver** — export_pdf, export_web_map, export_flood_map, export_layer, download_project

## Workflow context
Every mutating tool response includes a context line with: current phase (setup/analysis/cartography/export), study zone, layer count, and a hint for the next action. Use this to stay oriented.

## Important
- Screenshots are 1280x720 of the full QGIS desktop (menus, panels, map canvas, layer tree).
- The user sees every screenshot in the MCP App panel. Describe what you observe so they can follow along.
- For GUI interactions, reference pixel coordinates based on the screenshot layout.
- Default CRS is EPSG:2154 (Lambert 93, France). Change via new_project or execute_python if needed.
- Use skill:// resources for PyQGIS patterns, Processing algorithms, cartography best practices, and data source reference.
- Files in /data/ are accessible via the REST API (port depends on mode: 8080 single-user, session-specific in multi-user).
- Python code is syntax-validated before execution — malformed code returns a clean error instead of crashing QGIS.
- The project is auto-saved to /data/.autosave.qgz before risky operations (execute_python, run_processing, remove_layer, new_project).
- execute_python has a 60s timeout by default. Pass `timeout` param to adjust (e.g. timeout=180 for heavy spatial joins).

## External vision services
- Moondream (image understanding): {MOONDREAM_URL}
- SAMGeo3 (segmentation): {SAMGEO3_URL}
- DepthPro (depth estimation): {DEPTHPRO_URL}
"""


def handle_mcp_message(method: str, params: dict, msg_id: Any, session_id: str) -> Optional[dict]:
    """Handle a single MCP JSON-RPC message. Returns a response dict, or None for notifications."""

    if method == "initialize":
        sessions[session_id] = {"initialized": True, "last_seen": time.time()}
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                    "extensions": {
                        "io.modelcontextprotocol/ui": {}
                    },
                },
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            }
        }

    if method == "notifications/initialized":
        return None

    if method == "ping":
        _session_touch(session_id)
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    _session_touch(session_id)

    # ── Tools ──────────────────────────────────────────────────

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS}
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            result = execute_tool(tool_name, arguments)
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except Exception as e:
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}
            }

    # ── Resources ──────────────────────────────────────────────

    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"resources": RESOURCES}
        }

    if method == "resources/read":
        uri = params.get("uri", "")

        if uri == UI_RESOURCE_URI:
            # Resolve session-specific ports (multi-user) or defaults (single-user)
            novnc_port  = VNC_PORT   # default: 6080
            stream_port = 8081       # default: 8081
            api_port    = 8080       # default: 8080
            if MULTI_USER_MODE and container_manager:
                uid = current_user_id.get(None)
                if uid:
                    session = container_manager.get_session(uid)
                    if session:
                        novnc_port  = session.novnc_port
                        stream_port = session.stream_port
                        api_port    = session.api_port

            ui_html = (UI_HTML_CONTENT
                       .replace("__NOVNC_PORT__",  str(novnc_port))
                       .replace("__STREAM_PORT__", str(stream_port))
                       .replace("__API_PORT__",    str(api_port)))

            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "contents": [{
                        "uri": uri,
                        "mimeType": UI_MIME_TYPE,
                        "text": ui_html,
                        "_meta": {
                            "ui": {
                                "csp": {
                                    "connectDomains": [
                                        "self",
                                        f"http://localhost:{novnc_port}",
                                        f"ws://localhost:{novnc_port}",
                                        f"http://localhost:{api_port}",
                                        f"http://localhost:{stream_port}",
                                    ],
                                    "frameDomains": [f"http://localhost:{novnc_port}"],
                                    "imgDomains":   [f"http://localhost:{stream_port}"],
                                }
                            }
                        }
                    }]
                }
            }

        if uri == "skill://qgis-status":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"contents": [{"uri": uri, "mimeType": "text/plain", "text": json.dumps(qgis_command("health"), indent=2)}]}
            }

        if uri in SKILL_MAP:
            content = _load_skill(SKILL_MAP[uri])
            if uri == "skill://data-sources":
                catalog_path = Path("/app/datasources.json")
                if catalog_path.exists():
                    catalog_json = catalog_path.read_text()
                    content = f"# Data Source Catalog (JSON)\n\n```json\n{catalog_json}\n```\n\n---\n\n{content}"
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"contents": [{"uri": uri, "mimeType": "text/plain", "text": content}]}
            }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32602, "message": f"Unknown resource: {uri}"}
        }

    # ── Prompts ────────────────────────────────────────────────

    if method == "prompts/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"prompts": PROMPTS}
        }

    if method == "prompts/get":
        prompt_name = params.get("name", "")
        arguments = params.get("arguments", {})
        messages = get_prompt_content(prompt_name, arguments)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"messages": [{"role": "user", "content": messages}]}
        }

    # ── Unknown ────────────────────────────────────────────────

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }


# ══════════════════════════════════════════════════════════════════
# STARLETTE APP
# ══════════════════════════════════════════════════════════════════

def make_sse_response(data: dict, session_id: str) -> Response:
    """Create an SSE response with a single event."""
    body = f"event: message\ndata: {json.dumps(data)}\n\n"
    return Response(
        content=body,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "mcp-session-id": session_id,
        }
    )


async def handle_mcp(request: Request) -> Response:
    """Main MCP endpoint — handles Streamable HTTP."""

    # GET = MCP 2024-11-05 SSE transport (Claude Desktop, older clients)
    # Open persistent SSE connection, send endpoint event, then keepalives.
    # Clients using this protocol will POST tool calls to /mcp normally.
    if request.method == "GET":
        session_id = request.headers.get("mcp-session-id", uuid.uuid4().hex)

        async def _sse_keepalive():
            # Announce where to POST requests
            yield f"event: endpoint\ndata: /mcp\n\n"
            # Keep connection alive; Claude Desktop reconnects automatically if it drops
            while True:
                await asyncio.sleep(30)
                yield f": keepalive\n\n"

        return StreamingResponse(
            _sse_keepalive(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "mcp-session-id": session_id,
            },
        )

    # DELETE = close session
    if request.method == "DELETE":
        session_id = request.headers.get("mcp-session-id", "")
        sessions.pop(session_id, None)
        return Response(status_code=204)

    # POST = JSON-RPC request
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}, status_code=400)

    # Batch requests are not supported (JSON-RPC 2.0 §6)
    if isinstance(body, list):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Batch requests are not supported"}},
            status_code=400,
        )

    # Get or create session
    # Keep the raw header value to distinguish "client sent a known ID" from "auto-generated"
    session_id_header = request.headers.get("mcp-session-id", "")
    session_id = session_id_header or uuid.uuid4().hex

    method = body.get("method", "")
    params = body.get("params", {})
    msg_id = body.get("id")

    # Session validation (P3 — lenient): only enforce when the client explicitly sent a
    # mcp-session-id header that we don't recognise. Auto-generated IDs (no header sent)
    # are allowed through for backward-compat with session-less clients and proxies.
    _STATEFUL_METHODS = frozenset({
        "tools/list", "tools/call",
        "resources/list", "resources/read",
        "prompts/list", "prompts/get",
    })
    if (sessions and  # only enforce when server has active sessions (prevents blocking reconnects after restart)
            session_id_header and
            method in _STATEFUL_METHODS and
            msg_id is not None and
            session_id_header not in sessions):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": msg_id,
             "error": {"code": -32600, "message": "Session not initialized or expired. Send 'initialize' first."}},
            headers={"mcp-session-id": session_id_header},
            status_code=400,
        )

    # ── Multi-user: resolve user_id from auth header ──────────────
    # Auth is optional: if no Bearer token is provided, use "anonymous"
    # (which gets its own isolated container).
    if MULTI_USER_MODE:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and auth_manager is not None:
            api_key = auth_header[7:].strip()
            user = auth_manager.verify_api_key(api_key)
            user_id = user.id if user else "anonymous"
        else:
            user_id = "anonymous"
        # Ensure the user's QGIS container is running before we dispatch
        # Skip for lifecycle methods that don't need QGIS (initialize, ping, notifications)
        _NEEDS_QGIS = frozenset({"tools/call", "resources/read"})
        if method in _NEEDS_QGIS:
            await _ensure_session(user_id)
    else:
        user_id = "default"

    # Notifications (no id) — just acknowledge
    if msg_id is None:
        if method == "notifications/initialized":
            return Response(status_code=202, headers={"mcp-session-id": session_id})
        return Response(status_code=202, headers={"mcp-session-id": session_id})

    # run_recipe: stream via SSE only if client supports it (e.g. direct Claude.ai).
    # mcp-remote proxies don't send Accept: text/event-stream and can't parse SSE results
    # → they would return null → "No result received from client-side tool execution".
    # For non-SSE clients, fall through to the normal synchronous handler below.
    accept = request.headers.get("accept", "")
    if (method == "tools/call" and
            params.get("name") == "run_recipe" and
            _accepts_sse(accept)):
        progress_token = params.get("_meta", {}).get("progressToken")
        # Pass user_id into the generator — it sets its own ContextVar token
        # because the generator runs after this handler returns.
        return StreamingResponse(
            stream_run_recipe(params.get("arguments", {}), msg_id, session_id,
                              progress_token, user_id=user_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "mcp-session-id": session_id},
        )

    # Handle the message — run in thread pool to avoid blocking the asyncio event loop.
    # ContextVar is propagated automatically by asyncio.to_thread (Python 3.7+).
    token = current_user_id.set(user_id)
    try:
        response = await asyncio.to_thread(handle_mcp_message, method, params, msg_id, session_id)
    finally:
        current_user_id.reset(token)

    if response is None:
        return Response(status_code=202, headers={"mcp-session-id": session_id})

    # Check Accept header — prefer SSE if supported
    if _accepts_sse(accept):
        return make_sse_response(response, session_id)
    else:
        return JSONResponse(response, headers={"mcp-session-id": session_id})


async def handle_health(request: Request) -> JSONResponse:
    """Health check endpoint."""
    if MULTI_USER_MODE:
        bridge_ok = True  # bridge runs per-container; gateway itself is always up
    else:
        # Quick HTTP probe to the local api_server
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(f"http://localhost:{_SINGLE_USER_API_PORT}/health")
                bridge_ok = r.status_code == 200
        except Exception:
            bridge_ok = os.path.exists(SOCKET_PATH)  # fallback: legacy check
    return JSONResponse({
        "status": "ok",
        "bridge": bridge_ok,
        "server": "QgisRemoteMCP",
        "multi_user": MULTI_USER_MODE,
    })


# ── Auth endpoints (active when MULTI_USER_MODE=true) ─────────────

async def handle_auth_register(request: Request) -> JSONResponse:
    """POST /api/auth/register — {"email": "...", "password": "..."}"""
    if not MULTI_USER_MODE or auth_manager is None:
        return JSONResponse({"error": "Auth not enabled (MULTI_USER_MODE=false)"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    try:
        result = auth_manager.register(body.get("email", ""), body.get("password", ""))
        return JSONResponse(result, status_code=201)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def handle_auth_login(request: Request) -> JSONResponse:
    """POST /api/auth/login — {"email": "...", "password": "..."}"""
    if not MULTI_USER_MODE or auth_manager is None:
        return JSONResponse({"error": "Auth not enabled (MULTI_USER_MODE=false)"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    try:
        result = auth_manager.login(body.get("email", ""), body.get("password", ""))
        return JSONResponse(result)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=401)


async def handle_auth_me(request: Request) -> JSONResponse:
    """GET /api/auth/me — returns current user info."""
    if not MULTI_USER_MODE or auth_manager is None:
        return JSONResponse({"error": "Auth not enabled"}, status_code=404)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "Missing Bearer token"}, status_code=401)
    api_key = auth_header[7:].strip()
    user = auth_manager.verify_api_key(api_key)
    if not user:
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    return JSONResponse(auth_manager.get_user_info(user.id))


async def handle_auth_regenerate(request: Request) -> JSONResponse:
    """POST /api/auth/regenerate-key"""
    if not MULTI_USER_MODE or auth_manager is None:
        return JSONResponse({"error": "Auth not enabled"}, status_code=404)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "Missing Bearer token"}, status_code=401)
    api_key = auth_header[7:].strip()
    user = auth_manager.verify_api_key(api_key)
    if not user:
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    new_key = auth_manager.regenerate_api_key(user.id)
    return JSONResponse({"api_key": new_key})


async def handle_session_info(request: Request) -> JSONResponse:
    """GET /api/session — returns the caller's container session info."""
    if not MULTI_USER_MODE or container_manager is None:
        return JSONResponse({"error": "Multi-user mode not enabled"}, status_code=404)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and auth_manager is not None:
        api_key = auth_header[7:].strip()
        user = auth_manager.verify_api_key(api_key)
        user_id = user.id if user else "anonymous"
    else:
        user_id = "anonymous"
    session = container_manager.get_session(user_id)
    if not session:
        return JSONResponse({"status": "not_started", "user_id": user_id})
    return JSONResponse(session.to_dict())


async def handle_sessions_list(request: Request) -> JSONResponse:
    """GET /api/sessions — list all active sessions (admin, no auth required for MVP)."""
    if not MULTI_USER_MODE or container_manager is None:
        return JSONResponse({"error": "Multi-user mode not enabled"}, status_code=404)
    return JSONResponse({"sessions": container_manager.list_sessions()})


# ── Lifespan: initialise / shutdown managers ──────────────────────

@asynccontextmanager
async def lifespan(app):
    global container_manager, auth_manager

    if MULTI_USER_MODE:
        if not _MULTIUSER_DEPS_OK:
            raise RuntimeError(
                "MULTI_USER_MODE=true but src/auth.py or src/container_manager.py "
                "could not be imported. Check that docker>=7.0.0 is installed."
            )

        idle_timeout = os.environ.get("IDLE_TIMEOUT_MINUTES")
        if not idle_timeout:
            raise RuntimeError(
                "IDLE_TIMEOUT_MINUTES must be set when MULTI_USER_MODE=true "
                "(e.g. IDLE_TIMEOUT_MINUTES=30)."
            )

        data_dir = os.environ.get("DATA_DIR", "data")

        auth_manager = AuthManager(data_dir=data_dir)
        await auth_manager.initialize()
        print(f"[QgisRemoteMCP] Auth manager ready — data_dir={data_dir}")

        container_manager = ContainerManager(
            image_name=os.environ.get("QGIS_IMAGE", "qgisremotemcp:latest"),
            base_api_port=int(os.environ.get("BASE_API_PORT", "9000")),
            base_stream_port=int(os.environ.get("BASE_STREAM_PORT", "9100")),
            base_novnc_port=int(os.environ.get("BASE_NOVNC_PORT", "9200")),
            max_containers=int(os.environ.get("MAX_CONTAINERS", "50")),
            idle_timeout_minutes=int(idle_timeout),
            data_dir=data_dir,
        )
        await container_manager.initialize()
        print(f"[QgisRemoteMCP] Multi-user mode: ON — idle_timeout={idle_timeout}min")
    else:
        print("[QgisRemoteMCP] Single-user mode (MULTI_USER_MODE=false)")

    yield

    if container_manager is not None:
        await container_manager.shutdown()
        print("[QgisRemoteMCP] Container manager stopped")


# ── Create Starlette app ──────────────────────────────────────────

app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/mcp", handle_mcp, methods=["GET", "POST", "DELETE"]),
        Route("/health", handle_health, methods=["GET"]),
        Route("/api/auth/register", handle_auth_register, methods=["POST"]),
        Route("/api/auth/login", handle_auth_login, methods=["POST"]),
        Route("/api/auth/me", handle_auth_me, methods=["GET"]),
        Route("/api/auth/regenerate-key", handle_auth_regenerate, methods=["POST"]),
        Route("/api/session", handle_session_info, methods=["GET"]),
        Route("/api/sessions", handle_sessions_list, methods=["GET"]),
    ]
)


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if MULTI_USER_MODE:
        print("[QgisRemoteMCP] Multi-user mode — skipping QGIS bridge wait (per-user containers)")
    else:
        print("[QgisRemoteMCP] Waiting for local QGIS api_server…")
        for i in range(90):
            try:
                import urllib.request
                urllib.request.urlopen(
                    f"http://localhost:{_SINGLE_USER_API_PORT}/health", timeout=2
                )
                print(f"[QgisRemoteMCP] api_server ready after {i}s")
                break
            except Exception:
                time.sleep(1)
        else:
            print("[QgisRemoteMCP] WARNING: api_server not responding, starting MCP server anyway")

    print(f"[QgisRemoteMCP] Starting MCP server on :{MCP_PORT}")
    print(f"[QgisRemoteMCP] MCP Apps UI: {UI_RESOURCE_URI}")
    print(f"[QgisRemoteMCP] Endpoint: http://0.0.0.0:{MCP_PORT}/mcp")

    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="info")
