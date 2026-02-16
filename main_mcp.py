"""
BigQgisMCP — MCP Server (Streamable HTTP)
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
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

# ── Configuration ─────────────────────────────────────────────────

SOCKET_PATH = "/tmp/qgis_bridge.sock"
SOCKET_TIMEOUT = 60
SKILLS_DIR = Path("/app/skills")
MCP_PORT = int(os.environ.get("MCP_PORT", "8100"))
VNC_PORT = int(os.environ.get("QGIS_VNC_PORT", "6080"))
VNC_HOST = os.environ.get("VNC_HOST", "localhost")
PROTOCOL_VERSION = "2025-06-18"

MOONDREAM_URL = os.environ.get("MOONDREAM_URL", "http://localhost:8001")
SAMGEO3_URL = os.environ.get("SAMGEO3_URL", "http://localhost:8002")
DEPTHPRO_URL = os.environ.get("DEPTHPRO_URL", "http://localhost:8003")

# MCP Apps
UI_RESOURCE_URI = "ui://bigqgismcp/qgis-desktop"
UI_MIME_TYPE = "text/html;profile=mcp-app"
UI_HTML_CONTENT = ""

# Sessions
sessions: Dict[str, Dict[str, Any]] = {}

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
        return {"error": f"QGIS command timed out after {SOCKET_TIMEOUT}s."}
    except ConnectionRefusedError:
        return {"error": "Cannot connect to QGIS. The application may be restarting."}
    except Exception as e:
        return {"error": f"Bridge error: {str(e)}"}


# ── Load HTML ─────────────────────────────────────────────────────

def load_ui_html():
    global UI_HTML_CONTENT
    html_path = Path(__file__).parent / "qgis_app.html"
    if html_path.exists():
        UI_HTML_CONTENT = html_path.read_text(encoding="utf-8")
        print(f"[BigQgisMCP] Loaded UI HTML: {len(UI_HTML_CONTENT)} bytes")
    else:
        print(f"[BigQgisMCP] Warning: UI HTML not found at {html_path}")
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
        "description": "Execute Python/PyQGIS code inside the running QGIS instance. The script has access to qgis.core.*, iface, processing.run(), project = QgsProject.instance(), canvas = iface.mapCanvas(). Store return values in the `result` dict.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute inside QGIS"}
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
        "description": "Upload a file into the QGIS container (/data/). Accepts base64-encoded content. Use for shapefiles, GeoJSON, GPKG, CSV, TIFF, project files, etc.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Target filename (e.g. 'parcels.geojson')"},
                "content_base64": {"type": "string", "description": "Base64-encoded file content"}
            },
            "required": ["name", "content_base64"]
        }
    },
    {
        "name": "download_file",
        "description": "Download a file from the QGIS container. Returns base64 content for files < 5MB, or a download URL for larger files. Restricted to /data/ and /projects/.",
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
        "description": "List files in the QGIS container's /data/ and /projects/ directories.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (default '*')", "default": "*"},
                "directories": {"type": "array", "items": {"type": "string"}, "description": "Directories to scan", "default": ["/data", "/projects"]}
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
    {"uri": "skill://qgis-status", "name": "QGIS Status", "description": "Current QGIS instance status.", "mimeType": "text/plain"},
]

SKILL_MAP = {
    "skill://pyqgis": "pyqgis",
    "skill://processing": "processing",
    "skill://cartography": "cartography",
    "skill://external-services": "external_services",
    "skill://data-sources": "data_sources",
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

    return [{"type": "text", "text": f"Unknown prompt: {name}"}]


# ══════════════════════════════════════════════════════════════════
# TOOL EXECUTION
# ══════════════════════════════════════════════════════════════════

VISUAL_TOOLS = {
    "execute_python", "new_project", "open_project", "add_layer", "remove_layer",
    "run_processing", "zoom_to", "mouse_click", "mouse_scroll", "key_press", "mouse_drag",
    "add_from_catalog", "set_layer_style", "set_layer_visibility",
}


def _auto_screenshot() -> list:
    """Take a screenshot and return it as an MCP image content block.
    Returns empty list on failure so it can be safely concatenated."""
    time.sleep(0.3)  # let QGIS render
    resp = qgis_command("screenshot", {"width": 1280, "height": 720})
    if "image_base64" in resp:
        return [{"type": "image", "data": resp["image_base64"], "mimeType": "image/png"}]
    return []


def execute_tool(name: str, arguments: dict) -> dict:
    """Execute a tool and return MCP content result.
    Visual-modifying tools automatically append a screenshot."""

    if name == "qgis_desktop_ui":
        vnc_url = f"http://{VNC_HOST}:{VNC_PORT}/vnc.html?autoconnect=true&resize=scale"
        return {"content": [{"type": "text", "text": f"QGIS Desktop interface opened.\nVNC URL: {vnc_url}\nThe interactive view is displayed above."}]}

    elif name == "execute_python":
        code = arguments.get("code", "")
        response = qgis_command("execute_python", {"code": code})
        content = [{"type": "text", "text": json.dumps(response, indent=2, default=str)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "get_screenshot":
        width = arguments.get("width", 1280)
        height = arguments.get("height", 720)
        response = qgis_command("screenshot", {"width": width, "height": height, "format": "png"})
        if "error" in response:
            return {"content": [{"type": "text", "text": json.dumps(response)}]}
        image_b64 = response.get("image_base64", "")
        return {"content": [{"type": "image", "data": image_b64, "mimeType": "image/png"}]}

    elif name == "get_project_info":
        response = qgis_command("get_project_info")
        return {"content": [{"type": "text", "text": json.dumps(response, indent=2, default=str)}]}

    elif name == "new_project":
        title = arguments.get("title", "New Project")
        crs = arguments.get("crs", "EPSG:2154")
        response = qgis_command("new_project", {"title": title, "crs": crs})
        content = [{"type": "text", "text": json.dumps(response)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "open_project":
        response = qgis_command("open_project", {"path": arguments.get("path", "")})
        content = [{"type": "text", "text": json.dumps(response)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "save_project":
        response = qgis_command("save_project", {"path": arguments.get("path", "")})
        return {"content": [{"type": "text", "text": json.dumps(response)}]}

    elif name == "add_layer":
        uri = arguments.get("uri", "")
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
        content = [{"type": "text", "text": json.dumps(response)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "remove_layer":
        response = qgis_command("remove_layer", {"layer_id": arguments.get("layer_id", "")})
        content = [{"type": "text", "text": json.dumps(response)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "get_features":
        response = qgis_command("get_features", {
            "layer_id": arguments.get("layer_id", ""),
            "filter": arguments.get("filter", ""),
            "limit": arguments.get("limit", 100),
            "include_geometry": arguments.get("include_geometry", True),
        })
        return {"content": [{"type": "text", "text": json.dumps(response, indent=2, default=str)}]}

    elif name == "run_processing":
        response = qgis_command("run_processing", {
            "algorithm": arguments.get("algorithm", ""),
            "parameters": arguments.get("parameters", {}),
        })
        content = [{"type": "text", "text": json.dumps(response, indent=2, default=str)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "search_algorithms":
        response = qgis_command("list_algorithms", {
            "search": arguments.get("search", ""),
            "provider": arguments.get("provider", ""),
            "limit": arguments.get("limit", 20),
        })
        return {"content": [{"type": "text", "text": json.dumps(response, indent=2)}]}

    elif name == "zoom_to":
        params = {}
        if arguments.get("extent"):
            params["extent"] = arguments["extent"]
        elif arguments.get("layer_id"):
            params["layer_id"] = arguments["layer_id"]
        response = qgis_command("zoom_to_extent", params)
        content = [{"type": "text", "text": json.dumps(response)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "export_pdf":
        params = {"layout": arguments.get("layout", "")}
        if arguments.get("output_path"):
            params["output_path"] = arguments["output_path"]
        response = qgis_command("export_pdf", params)
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

    elif name == "mouse_click":
        response = qgis_command("mouse_click", {
            "x": arguments.get("x", 0), "y": arguments.get("y", 0),
            "button": arguments.get("button", 1), "double": arguments.get("double", False)
        })
        content = [{"type": "text", "text": json.dumps(response)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "mouse_scroll":
        response = qgis_command("mouse_scroll", {
            "x": arguments.get("x", 0), "y": arguments.get("y", 0),
            "direction": arguments.get("direction", "down"), "clicks": arguments.get("clicks", 3)
        })
        content = [{"type": "text", "text": json.dumps(response)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "key_press":
        response = qgis_command("key_press", {"key": arguments.get("key", "")})
        content = [{"type": "text", "text": json.dumps(response)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "mouse_drag":
        response = qgis_command("mouse_drag", {
            "x1": arguments.get("x1", 0), "y1": arguments.get("y1", 0),
            "x2": arguments.get("x2", 0), "y2": arguments.get("y2", 0),
            "button": arguments.get("button", 1)
        })
        content = [{"type": "text", "text": json.dumps(response)}]
        content += _auto_screenshot()
        return {"content": content}

    # ── File management tools ────────────────────────────────────

    elif name == "upload_file":
        response = qgis_command("write_file", {
            "name": arguments.get("name", ""),
            "content_base64": arguments.get("content_base64", ""),
        })
        return {"content": [{"type": "text", "text": json.dumps(response, default=str)}]}

    elif name == "download_file":
        response = qgis_command("read_file", {"path": arguments.get("path", "")})
        if "error" in response:
            return {"content": [{"type": "text", "text": json.dumps(response)}]}
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

    elif name == "list_files":
        response = qgis_command("list_files", {
            "pattern": arguments.get("pattern", "*"),
            "directories": arguments.get("directories", ["/data", "/projects"]),
        })
        return {"content": [{"type": "text", "text": json.dumps(response, indent=2, default=str)}]}

    elif name == "export_layer":
        response = qgis_command("export_layer", {
            "layer_id": arguments.get("layer_id", ""),
            "format": arguments.get("format", "GPKG"),
            "name": arguments.get("name", ""),
        })
        return {"content": [{"type": "text", "text": json.dumps(response, default=str)}]}

    elif name == "download_project":
        response = qgis_command("download_project", {
            "name": arguments.get("name", "project"),
        })
        return {"content": [{"type": "text", "text": json.dumps(response, default=str)}]}

    # ── Data catalog tools ───────────────────────────────────────

    elif name == "list_datasources":
        response = qgis_command("list_datasources", {
            "category": arguments.get("category", ""),
            "search": arguments.get("search", ""),
        })
        return {"content": [{"type": "text", "text": json.dumps(response, indent=2, default=str)}]}

    elif name == "add_from_catalog":
        params = {"id": arguments.get("id", "")}
        if arguments.get("name"):
            params["name"] = arguments["name"]
        if arguments.get("bbox"):
            params["bbox"] = arguments["bbox"]
        response = qgis_command("add_from_catalog", params)
        content = [{"type": "text", "text": json.dumps(response, default=str)}]
        if response.get("success"):
            content += _auto_screenshot()
        return {"content": content}

    # ── Style tools ──────────────────────────────────────────────

    elif name == "set_layer_style":
        response = qgis_command("set_layer_style", {
            "layer_id": arguments.get("layer_id", ""),
            "style_type": arguments.get("style_type", "single"),
            "color": arguments.get("color", "65,105,225,180"),
            "field": arguments.get("field", ""),
            "categories": arguments.get("categories", {}),
            "ranges": arguments.get("ranges", []),
        })
        content = [{"type": "text", "text": json.dumps(response, default=str)}]
        content += _auto_screenshot()
        return {"content": content}

    elif name == "set_layer_visibility":
        response = qgis_command("set_layer_visibility", {
            "layer_id": arguments.get("layer_id", ""),
            "visible": arguments.get("visible", True),
        })
        content = [{"type": "text", "text": json.dumps(response, default=str)}]
        content += _auto_screenshot()
        return {"content": content}

    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}


# ══════════════════════════════════════════════════════════════════
# MCP JSON-RPC HANDLER
# ══════════════════════════════════════════════════════════════════

SERVER_INFO = {
    "name": "BigQgisMCP",
    "version": "1.0.0",
}

INSTRUCTIONS = f"""You control a live QGIS Desktop instance. Every modifying tool automatically returns a screenshot so you always see the result.

## Core tools
- **execute_python** — Run PyQGIS code. Access: iface, project, canvas, processing, QgsProject, QgsVectorLayer, etc. Store outputs in `result` dict.
- **get_screenshot** — Capture current QGIS desktop (1280x720 PNG). Already included automatically after modifying tools.
- **add_layer** — Add vector/raster/WFS/WMS layers by URI.
- **run_processing** — Execute any of 1000+ Processing algorithms (native:buffer, gdal:warp, grass7:v.clean, etc.).
- **zoom_to** — Zoom to extent or layer.
- **mouse_click / mouse_scroll / key_press / mouse_drag** — Direct GUI interaction via xdotool (coordinates in 1920x1080 display pixels).
- **qgis_desktop_ui** — Open interactive QGIS view in conversation.

## Data catalog (pre-configured French national sources — free, no API key)
- **list_datasources** — Browse available sources: IGN orthophotos, Plan IGN, BD TOPO (buildings, roads, rivers, communes...), OSM, cadastre, DEM, BAN geocoding, Panoramax. Filter by category or search.
- **add_from_catalog** — Add a source by ID (e.g. `osm_xyz`, `bdtopo_batiments`). WFS requires a `bbox` [xmin,ymin,xmax,ymax] in EPSG:4326. Raster sources (WMS/WMTS/XYZ) work without bbox.

## File management
- **upload_file** — Upload a file (base64) into the QGIS container /data/. Supports shapefiles, GeoJSON, GPKG, CSV, TIFF, project files.
- **download_file** — Download a file from /data/ or /projects/. Returns base64 for files <5MB, or a download URL for larger files.
- **list_files** — List files in /data/ and /projects/.
- **export_layer** — Export a vector layer to GPKG, GeoJSON, Shapefile, or CSV. Saved to /data/.
- **download_project** — Save the current project as .qgz to /data/.
- **export_pdf** — Export a print layout to PDF. Returns base64 for files <5MB.

## Styling
- **set_layer_style** — Apply single color, categorized (by field), or graduated (ranges) symbology.
- **set_layer_visibility** — Show/hide a layer in the layer tree.

## Workflow pattern
1. **get_project_info** → understand current layers, CRS, layouts, extents
2. **Add data** — use add_from_catalog for French national data, add_layer for custom URIs, upload_file for user files
3. **Act** — run_processing, execute_python, zoom_to → each returns screenshot
4. **Style** — set_layer_style, set_layer_visibility
5. **Verify** the screenshot — describe what you see
6. **Deliver** — export_layer, download_project, export_pdf, download_file

## Important
- Screenshots are 1280x720 of the full QGIS desktop (menus, panels, map canvas, layer tree).
- The user sees every screenshot in the MCP App panel. Describe what you observe so they can follow along.
- For GUI interactions, reference pixel coordinates based on the screenshot layout.
- Default CRS is EPSG:2154 (Lambert 93, France). Change via new_project or execute_python if needed.
- Use skill:// resources for PyQGIS patterns, Processing algorithms, cartography best practices, and data source reference.
- Files in /data/ are accessible via REST API at http://localhost:8080/api/files/{{filename}}.

## External vision services
- Moondream (image understanding): {MOONDREAM_URL}
- SAMGeo3 (segmentation): {SAMGEO3_URL}
- DepthPro (depth estimation): {DEPTHPRO_URL}
"""


def handle_mcp_message(method: str, params: dict, msg_id: Any, session_id: str):
    """Handle a single MCP JSON-RPC message. Returns (response_dict, notification_dict_or_None)."""

    if method == "initialize":
        sessions[session_id] = {"initialized": True}
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
        }, None

    elif method == "notifications/initialized":
        return None, None

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}, None

    # ── Tools ──────────────────────────────────────────────────

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS}
        }, None

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            result = execute_tool(tool_name, arguments)
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}, None
        except Exception as e:
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}
            }, None

    # ── Resources ──────────────────────────────────────────────

    elif method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"resources": RESOURCES}
        }, None

    elif method == "resources/read":
        uri = params.get("uri", "")

        if uri == UI_RESOURCE_URI:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "contents": [{
                        "uri": uri,
                        "mimeType": UI_MIME_TYPE,
                        "text": UI_HTML_CONTENT,
                        "_meta": {
                            "ui": {
                                "csp": {
                                    "connectDomains": ["self", "http://localhost:6080", "ws://localhost:6080", "http://localhost:8080", "http://localhost:8081"],
                                    "frameDomains": ["http://localhost:6080"],
                                    "imgDomains": ["http://localhost:8081"]
                                }
                            }
                        }
                    }]
                }
            }, None

        elif uri == "skill://qgis-status":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"contents": [{"uri": uri, "mimeType": "text/plain", "text": json.dumps(qgis_command("health"), indent=2)}]}
            }, None

        elif uri in SKILL_MAP:
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
            }, None

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32602, "message": f"Unknown resource: {uri}"}
        }, None

    # ── Prompts ────────────────────────────────────────────────

    elif method == "prompts/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"prompts": PROMPTS}
        }, None

    elif method == "prompts/get":
        prompt_name = params.get("name", "")
        arguments = params.get("arguments", {})
        messages = get_prompt_content(prompt_name, arguments)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"messages": [{"role": "user", "content": messages}]}
        }, None

    # ── Unknown ────────────────────────────────────────────────

    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }, None


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

    # GET = SSE stream (not used in our case, but acknowledge)
    if request.method == "GET":
        return Response(status_code=405, content="Use POST for MCP requests")

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

    # Get or create session
    session_id = request.headers.get("mcp-session-id", "")
    if not session_id:
        session_id = uuid.uuid4().hex

    method = body.get("method", "")
    params = body.get("params", {})
    msg_id = body.get("id")

    # Notifications (no id) — just acknowledge
    if msg_id is None:
        if method == "notifications/initialized":
            return Response(status_code=202, headers={"mcp-session-id": session_id})
        return Response(status_code=202, headers={"mcp-session-id": session_id})

    # Handle the message
    response, notification = handle_mcp_message(method, params, msg_id, session_id)

    if response is None:
        return Response(status_code=202, headers={"mcp-session-id": session_id})

    # Check Accept header — prefer SSE if supported
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        return make_sse_response(response, session_id)
    else:
        return JSONResponse(response, headers={"mcp-session-id": session_id})


async def handle_health(request: Request) -> JSONResponse:
    """Health check endpoint."""
    bridge_ok = os.path.exists(SOCKET_PATH)
    return JSONResponse({"status": "ok", "bridge": bridge_ok, "server": "BigQgisMCP"})


# ── Create Starlette app ──────────────────────────────────────────

app = Starlette(
    routes=[
        Route("/mcp", handle_mcp, methods=["GET", "POST", "DELETE"]),
        Route("/health", handle_health, methods=["GET"]),
    ]
)


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    load_ui_html()

    print("[BigQgisMCP] Waiting for QGIS bridge...")
    for i in range(90):
        if os.path.exists(SOCKET_PATH):
            print(f"[BigQgisMCP] Bridge found after {i}s")
            break
        time.sleep(1)
    else:
        print("[BigQgisMCP] WARNING: Bridge not found, starting MCP server anyway")

    print(f"[BigQgisMCP] Starting MCP server on :{MCP_PORT}")
    print(f"[BigQgisMCP] MCP Apps UI: {UI_RESOURCE_URI}")
    print(f"[BigQgisMCP] Endpoint: http://0.0.0.0:{MCP_PORT}/mcp")

    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="info")
