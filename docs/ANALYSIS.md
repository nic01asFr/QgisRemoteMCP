# QgisRemoteMCP — Architecture Analysis & Evolution Plan

## 1. Current Architecture

```
Container (single Docker)
  supervisord
  ├── Xvfb :99 (virtual display 1920x1080)
  ├── fluxbox (window manager, maximizes QGIS)
  ├── QGIS Desktop (GUI, PyQGIS bridge via PYQGIS_STARTUP)
  ├── x11vnc → websockify/noVNC (:6080)
  ├── api_server.py (FastAPI REST :8080)
  ├── main_mcp.py (MCP Server :8100, Streamable HTTP)
  └── stream_server.py (MJPEG :8081)
```

### Communication Flow

```
User (Claude Desktop)
  │
  ├── sees MCP App (qgis_app.html) with screenshots
  ├── can open noVNC (:6080) for manual interaction
  │
  ▼
AI Assistant (Claude)
  │ calls MCP tools via JSON-RPC / Streamable HTTP
  ▼
main_mcp.py (:8100) — MCP Server
  │ UNIX socket /tmp/qgis_bridge.sock
  ▼
qgis_bridge.py — runs INSIDE QGIS process
  │ direct access to iface, QgsProject, processing, etc.
  ▼
QGIS Desktop (Xvfb :99)
```

### Current MCP Tools (16 tools)

| Category | Tool | Description |
|----------|------|-------------|
| **UI** | `qgis_desktop_ui` | Opens MCP App view |
| **Exec** | `execute_python` | Run arbitrary PyQGIS code |
| **Visual** | `get_screenshot` | Capture QGIS desktop as PNG |
| **Project** | `get_project_info` | Project state (layers, CRS, layouts) |
| **Project** | `new_project` | Create empty project |
| **Project** | `open_project` | Open .qgz/.qgs file |
| **Project** | `save_project` | Save current project |
| **Layer** | `add_layer` | Add vector/raster/WFS/WMS |
| **Layer** | `remove_layer` | Remove a layer |
| **Layer** | `get_features` | Query vector features |
| **Analysis** | `run_processing` | Execute Processing algorithm |
| **Analysis** | `search_algorithms` | Search available algorithms |
| **Navigation** | `zoom_to` | Zoom to extent or layer |
| **Export** | `export_pdf` | Export print layout to PDF |
| **GUI** | `mouse_click` | Click at (x,y) via xdotool |
| **GUI** | `mouse_scroll` | Scroll at (x,y) |
| **GUI** | `key_press` | Send key combo |
| **GUI** | `mouse_drag` | Drag from (x1,y1) to (x2,y2) |

### Current MCP Resources (7 resources)

| URI | Description |
|-----|-------------|
| `ui://qgisremotemcp/qgis-desktop` | MCP App HTML (interactive view) |
| `skill://pyqgis` | PyQGIS scripting reference |
| `skill://processing` | Processing algorithms guide |
| `skill://cartography` | Symbology, labels, layouts |
| `skill://external-services` | Vision services (Moondream, SAMGeo3, DepthPro) |
| `skill://data-sources` | French national datasets reference |
| `skill://qgis-status` | Live QGIS instance status |

### Bridge Actions (qgis_bridge.py)

Actions exposed via UNIX socket but NOT all mapped to MCP tools:

| Bridge Action | MCP Tool | Status |
|---------------|----------|--------|
| `health` | — | Only used by resource `skill://qgis-status` |
| `get_project_info` | `get_project_info` | ✓ |
| `execute_python` | `execute_python` | ✓ |
| `new_project` | `new_project` | ✓ |
| `open_project` | `open_project` | ✓ |
| `save_project` | `save_project` | ✓ |
| `add_vector_layer` | `add_layer` (type=vector) | ✓ |
| `add_raster_layer` | `add_layer` (type=raster) | ✓ |
| `add_wfs_layer` | `add_layer` (type=wfs) | ✓ |
| `add_wms_layer` | `add_layer` (type=wms) | ✓ |
| `remove_layer` | `remove_layer` | ✓ |
| `list_layers` | — | Not exposed |
| `get_features` | `get_features` | ✓ |
| `run_processing` | `run_processing` | ✓ |
| `list_algorithms` | `search_algorithms` | ✓ |
| `zoom_to_extent` | `zoom_to` | ✓ |
| `screenshot` | `get_screenshot` | ✓ |
| `export_pdf` | `export_pdf` | ✓ |
| `export_image` | — | Not exposed |
| `apply_style` | — | Not exposed |
| `mouse_click` | `mouse_click` | ✓ |
| `mouse_scroll` | `mouse_scroll` | ✓ |
| `key_press` | `key_press` | ✓ |
| `mouse_drag` | `mouse_drag` | ✓ |

---

## 2. Identified Problems

### P1: No file transfer (CRITICAL)

Files created inside the container (exports, PDFs, project files) cannot be retrieved
by the user or returned by the assistant. The delivery flow is completely broken:

```
Assistant creates export ──→ File in /data/ ──→ ??? User gets nothing
```

The MCP protocol supports:
- `type: "resource"` with `blob` field (base64) for binary files in tool results
- `type: "image"` with `data` field (base64) — already used for screenshots
- `type: "resource_link"` for references to downloadable resources

### P2: No file upload

Users cannot load local files (shapefiles, GeoJSON, etc.) into the QGIS instance.
The noVNC file dialog shows the container filesystem, not the user's machine.

### P3: Missing convenience tools

Common operations require `execute_python` with raw PyQGIS:
- Setting layer style (categorized, graduated, simple)
- Toggling layer visibility
- Renaming layers
- Getting layer extent

### P4: No data source catalog

The `skills/data_sources.md` resource provides code patterns as text, but:
- The assistant must read and parse it manually
- No structured catalog the assistant can query
- No pre-configured QGIS connections (user must know URLs)
- No quick "add from catalog" capability

### P5: MCP App is limited

The `qgis_app.html` currently:
- Shows static screenshots via MCP tool calls (slow roundtrip)
- Links to noVNC in a separate window
- No file upload/download UI
- No live view (MJPEG available but unused)

The Blender_servers implementation embeds noVNC RFB directly in the iframe with
keyboard lock, making the GUI fully interactive within the conversation.

### P6: REST API lacks file endpoints

`api_server.py` has no file serving or upload endpoints. Needed for large files
that exceed base64 MCP response limits.

---

## 3. Reference: Blender_servers Patterns

### File Management Pattern

All file operations go through the bridge `execute` action with inline Python:

```python
# Upload: base64 → container filesystem
result = await _call_blender(user_id, "/api/execute", "POST", {
    "code": f"""
import os, base64
content = base64.b64decode({repr(content_b64)})
with open('/projects/{filename}', 'wb') as f:
    f.write(content)
result = {{'success': True, 'size': len(content)}}
"""
})

# Download: container filesystem → base64
result = await _call_blender(user_id, "/api/execute", "POST", {
    "code": f"""
import os, base64
with open('/projects/{filename}', 'rb') as f:
    content = f.read()
result = {{'content': base64.b64encode(content).decode()}}
"""
})
```

Security: path traversal prevention with `".." in filename or "/" in filename`.

### noVNC RFB Embed Pattern

Direct RFB library import in iframe (not an iframe-to-noVNC page):

```javascript
import RFB from '/static/novnc/core/rfb.js';
rfb = new RFB(container, wsUrl, {
    scaleViewport: true, resizeSession: true, focusOnClick: true,
    qualityLevel: 6, compressionLevel: 2
});
```

With keyboard lock on fullscreen:
```javascript
await navigator.keyboard.lock(['Escape', 'Tab', 'AltLeft', ...]);
```

### Bridge Startup Configuration

Blender's `startup.py` auto-configures the application on launch:
- Disable splash screen
- Set resolution
- Configure input for web canvas
- Set viewport mode

For QGIS equivalent: pre-configure connections, default CRS, disable tips dialog.

---

## 4. IGN Géoplateforme Data Sources (2025-2026)

All free, no API key required since July 2021.

### WMTS (tiled rasters, fast)
| Layer | Name | URL |
|-------|------|-----|
| `ORTHOIMAGERY.ORTHOPHOTOS` | Orthophotos | `https://data.geopf.fr/wmts` |
| `GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2` | Plan IGN v2 | `https://data.geopf.fr/wmts` |
| `CADASTRALPARCELS.PARCELLAIRE_EXPRESS` | Cadastre | `https://data.geopf.fr/wmts` |

### WFS (vector features)
| Typename | Description | URL |
|----------|-------------|-----|
| `BDTOPO_V3:batiment` | Buildings | `https://data.geopf.fr/wfs/ows` |
| `BDTOPO_V3:troncon_de_route` | Roads | `https://data.geopf.fr/wfs/ows` |
| `BDTOPO_V3:commune` | Municipalities | `https://data.geopf.fr/wfs/ows` |
| `BDTOPO_V3:surface_hydrographique` | Water | `https://data.geopf.fr/wfs/ows` |
| `BDTOPO_V3:zone_de_vegetation` | Vegetation | `https://data.geopf.fr/wfs/ows` |

### WMS (rendered rasters)
| Layer | Description | URL |
|-------|-------------|-----|
| `ORTHOIMAGERY.ORTHOPHOTOS` | Orthophotos | `https://data.geopf.fr/wms-r` |
| `ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES` | DEM | `https://data.geopf.fr/wms-r` |

### XYZ Tiles
| Name | URL |
|------|-----|
| OpenStreetMap | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` |
| CartoDB Positron | `https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png` |
| CartoDB Dark | `https://cartodb-basemaps-a.global.ssl.fastly.net/dark_all/{z}/{x}/{y}.png` |

### APIs
| Service | URL | Type |
|---------|-----|------|
| BAN Geocoding | `https://api-adresse.data.gouv.fr/search/` | REST |
| BAN Reverse | `https://api-adresse.data.gouv.fr/reverse/` | REST |
| Panoramax | `https://api.panoramax.xyz/api` | REST |
| IGN Altimetry | `https://data.geopf.fr/altimetrie/` | REST |

---

## 5. PyQGIS Connection Management Patterns

### Pre-configure XYZ Tiles
```python
from qgis.core import QgsSettings
settings = QgsSettings()
settings.setValue(f"qgis/connections-xyz/{name}/url", url)
settings.setValue(f"qgis/connections-xyz/{name}/zmin", "0")
settings.setValue(f"qgis/connections-xyz/{name}/zmax", "19")
iface.reloadConnections()
```

### Pre-configure WMS/WFS/WMTS
```python
settings.setValue(f"qgis/connections-wms/{name}/url", url)
settings.setValue(f"qgis/connections-wfs/{name}/url", url)
```

### Spatial Bookmarks
```python
from qgis.core import QgsBookmark, QgsRectangle, QgsCoordinateReferenceSystem
bookmark = QgsBookmark()
bookmark.setName("My Area")
bookmark.setExtent(QgsRectangle(xmin, ymin, xmax, ymax))
bookmark.setCrs(QgsCoordinateReferenceSystem("EPSG:2154"))
QgsProject.instance().bookmarkManager().addBookmark(bookmark)
```

### Layer Visibility
```python
root = QgsProject.instance().layerTreeRoot()
node = root.findLayer(layer.id())
node.setItemVisibilityChecked(True/False)
```

---

## 6. Target Architecture

### New tools to add (10 tools)

| Tool | Category | Bridge Action | Returns |
|------|----------|---------------|---------|
| `upload_file` | File | `upload_file` | success, path, size |
| `download_file` | File | `download_file` | base64 blob or URL |
| `list_files` | File | `list_files` | file list with metadata |
| `export_layer` | Delivery | `export_layer` | base64 blob (GPKG/GeoJSON/SHP) |
| `download_project` | Delivery | `download_project` | base64 blob (.qgz) |
| `list_datasources` | Catalog | (reads datasources.json) | structured catalog |
| `add_from_catalog` | Catalog | `add_from_catalog` | layer_id + screenshot |
| `set_layer_style` | Action | `set_layer_style` | success + screenshot |
| `set_layer_visibility` | Action | `set_layer_visibility` | success + screenshot |
| `manage_connections` | Config | `manage_connections` | connection list |

### Tools to modify (2 tools)

| Tool | Change |
|------|--------|
| `export_pdf` | Return PDF content as base64 blob, not just path |
| `get_project_info` | Add layer extents, style info, source paths |

### New bridge actions (10 actions)

| Action | What it does |
|--------|-------------|
| `upload_file` | Write base64 content to path |
| `download_file` | Read file as base64 |
| `list_files` | Scan /data/ and /projects/ |
| `export_layer` | QgsVectorFileWriter → base64 |
| `download_project` | Save project to temp → base64 |
| `add_from_catalog` | Load catalog entry + configure URI + add layer |
| `set_layer_style` | Apply symbol/categorized/graduated |
| `set_layer_visibility` | Toggle layer tree node |
| `manage_connections` | List/add/remove QGIS connections |
| `setup_catalog` | Pre-configure connections at startup |

### New files to create

| File | Purpose |
|------|---------|
| `datasources.json` | Structured data source catalog |
| `docs/ANALYSIS.md` | This document |

### Files to modify

| File | Changes |
|------|---------|
| `main_mcp.py` | Add 10 tools, modify 2, add REST file proxy |
| `src/qgis_bridge.py` | Add 10 actions, startup configuration |
| `src/api_server.py` | Add file endpoints (list, upload, download) |
| `qgis_app.html` | Add upload button, download list, optional RFB embed |
| `supervisord.conf` | No changes needed |
| `Dockerfile` | Copy datasources.json |
| `docker-compose.yml` | No changes needed (volumes already correct) |

### File transfer strategy

```
Small files (< 5 MB):
  Tool result → type: "resource", blob: base64
  User gets file directly in conversation

Large files (> 5 MB):
  Saved to /data/ (mounted as ./data/ on host)
  Tool result → text with download URL: http://localhost:8080/api/files/{name}
  User downloads via browser
```
