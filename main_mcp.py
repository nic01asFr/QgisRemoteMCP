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
SOCKET_TIMEOUT_LONG = 300  # for WFS downloads via ogr2ogr
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

def qgis_command(action: str, params: dict = None, timeout: int = None) -> dict:
    """Send a command to QGIS bridge via UNIX socket."""
    if not os.path.exists(SOCKET_PATH):
        return {"error": "QGIS bridge not ready. QGIS may still be starting up."}
    effective_timeout = timeout or SOCKET_TIMEOUT
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(effective_timeout)
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
        return {"error": f"QGIS command timed out after {effective_timeout}s."}
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
        "description": "Execute Python/PyQGIS code inside the running QGIS instance. The script has access to qgis.core.*, iface, processing.run(), project = QgsProject.instance(), canvas = iface.mapCanvas(). A `helpers` module is available with ready-made functions: helpers.geocode(addr), helpers.add_wfs(url, typename, bbox), helpers.add_wms(url, layers), helpers.add_wmts(url, layers), helpers.add_xyz(url, name), helpers.zoom_to(target), helpers.create_point_layer(name, points), helpers.load_catalog_source(id), helpers.bbox_from_canvas(), helpers.search_commune(name), helpers.get_elevation(lon, lat). Store return values in the `result` dict. Read skill://helpers for full reference.",
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
        "description": "Download a file from the QGIS container. Returns base64 content for files < 5MB, or a download URL for larger files. Restricted to /data/.",
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
# TOOL EXECUTION
# ══════════════════════════════════════════════════════════════════

def _auto_screenshot() -> list:
    """Take a screenshot and return it as an MCP image content block.
    Returns empty list on failure so it can be safely concatenated."""
    time.sleep(0.3)  # let QGIS render
    resp = qgis_command("screenshot", {"width": 1280, "height": 720})
    if "image_base64" in resp:
        return [{"type": "image", "data": resp["image_base64"], "mimeType": "image/png"}]
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
    """Return error message if any required field is missing/empty, else None."""
    missing = [f for f in fields if not arguments.get(f)]
    if missing:
        return f"Missing required parameter(s): {', '.join(missing)}"
    return None


# ── Tool handlers ─────────────────────────────────────────────
# Each function takes (arguments: dict) and returns an MCP result dict.

def _tool_qgis_desktop_ui(arguments: dict) -> dict:
    vnc_url = f"http://{VNC_HOST}:{VNC_PORT}/vnc.html?autoconnect=true&resize=scale"
    return {"content": [{"type": "text", "text": f"QGIS Desktop interface opened.\nVNC URL: {vnc_url}\nThe interactive view is displayed above."}]}


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
    response = qgis_command("screenshot", {"width": width, "height": height, "format": "png"})
    if "error" in response:
        return {"content": _text(response)}
    return {"content": [{"type": "image", "data": response.get("image_base64", ""), "mimeType": "image/png"}]}


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
    err = _validate_required(arguments, "name", "content_base64")
    if err:
        return _error(err)
    response = qgis_command("write_file", {
        "name": arguments["name"],
        "content_base64": arguments["content_base64"],
    })
    return {"content": _text(response)}


def _tool_download_file(arguments: dict) -> dict:
    err = _validate_required(arguments, "path")
    if err:
        return _error(err)
    response = qgis_command("read_file", {"path": arguments["path"]})
    if "error" in response:
        return {"content": _text(response)}
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


# ── Run recipe (automated execution) ─────────────────────────

# Actions that need longer timeouts (WFS downloads, heavy exports)
_LONG_TIMEOUT_ACTIONS = frozenset({
    "smart_load", "export_flood_map", "export_web_map", "export_temporal_map", "export_qfield", "execute_python",
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

        # Determine timeout
        timeout = SOCKET_TIMEOUT_LONG if action in _LONG_TIMEOUT_ACTIONS else SOCKET_TIMEOUT

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
    "list_recipes": _tool_list_recipes,
    "get_recipe": _tool_get_recipe,
    "run_recipe": _tool_run_recipe,
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
    "name": "BigQgisMCP",
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
- Files in /data/ are accessible via REST API at http://localhost:8080/api/files/{{filename}}.
- Python code is syntax-validated before execution — malformed code returns a clean error instead of crashing QGIS.
- The project is auto-saved to /data/.autosave.qgz before risky operations (execute_python, run_processing, remove_layer, new_project).
- execute_python has a 30s timeout by default. Pass `timeout` param to adjust.

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
