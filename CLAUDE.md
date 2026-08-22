# CLAUDE.md — QgisRemoteMCP

## Project

QgisRemoteMCP exposes a full QGIS Desktop instance as an MCP Server.
Single Docker container with QGIS GUI + Xvfb + noVNC + MCP Server.

Supports two modes:
- **Single-user** (default, `MULTI_USER_MODE=false`) — one QGIS instance, no auth required
- **Multi-user** (`MULTI_USER_MODE=true`) — one Docker container per authenticated user, isolated QGIS sessions

## Architecture

```
Container (single-user mode)
  supervisord
  ├── Xvfb :99 (virtual display)
  ├── fluxbox (window manager)
  ├── QGIS Desktop (GUI, PyQGIS bridge via startup script)
  ├── x11vnc → websockify/noVNC (:6080)
  ├── api_server.py (FastAPI REST :8080)
  ├── main_mcp.py (MCP Server :8100, 52 tools)
  └── stream_server.py (MJPEG :8081)
```

Communication (single-user): MCP Server → HTTP → `api_server.py:8080` → QGIS Bridge (UNIX socket)

Multi-user gateway spawns per-user worker containers via Docker SDK.
Each worker exposes the same REST API on dynamic ports (9000+/9100+/9200+).

## Key files

- `main_mcp.py` — MCP Server with 52 tools, 10 resources, 3 prompts
- `src/qgis_bridge.py` — Runs inside QGIS, UNIX socket listener, 47 actions
- `src/qgis_helpers.py` — Python helpers injected into execute_python
- `src/api_server.py` — FastAPI REST wrapper
- `src/stream_server.py` — MJPEG stream
- `src/auth.py` — API key + JWT authentication (multi-user mode)
- `src/container_manager.py` — Docker-per-user container lifecycle (multi-user mode)
- `skills/*.md` — MCP Resources (PyQGIS, Processing, cartography, smart loading, recipes, etc.)
- `templates/*.qpt` — Print layout templates (A3 landscape, A4 portrait)
- `templates/web/` — Leaflet HTML templates (standard, flood, temporal)
- `recipes/*.json` — Workflow recipes (6 pre-built analyses)
- `datasources.json` — 47 pre-configured French data sources (IGN, OSM, BD TOPO, Georisques)
- `Dockerfile` — Single container build
- `supervisord.conf` — Process orchestration
- `entrypoint.sh` — Container startup

## MCP Tools (52)

### Core
- `execute_python` — Run PyQGIS code with `helpers` module
- `get_screenshot` — Capture QGIS canvas
- `get_project_info` / `new_project` / `open_project` / `save_project`
- `qgis_desktop_ui` — Open interactive QGIS view in conversation

### Data loading
- `set_study_zone` — Define study area (geocodes, stores bbox)
- `get_study_zone` — Read stored zone
- `smart_load` — Load from catalog (WFS → local GPKG, rasters stream)
- `list_datasources` / `add_from_catalog` — Browse and add data sources
- `add_layer` / `remove_layer` — Manual layer management

### Analysis
- `run_processing` — 1000+ Processing algorithms
- `search_algorithms` — Search algorithms
- `get_features` — Query features from vector layers

### Cartography & export
- `set_layer_style` / `set_layer_visibility` — Symbology and visibility
- `apply_layout_template` / `list_layout_templates` — Print layout templates
- `export_pdf` — Export layout to PDF
- `export_web_map` — Export visible layers as interactive Leaflet HTML
- `export_flood_map` — Interactive flood analysis HTML (water height slider, building exposure stats)
- `export_temporal_map` — Interactive temporal analysis HTML (year slider, animated playback, per-band stats)
- `export_layer` — Export layer to GPKG/GeoJSON/Shapefile/CSV
- `export_qfield` — QField-ready ZIP package (.qgz + GPKGs + editable Observations layer with form widgets)
- `export_grist` — Grist document (.grist SQLite) from QGIS project or any HTML with GeoJSON (Choice/Date/Ref columns, custom map widget, form pages)

### Recipes (workflow automation)
- `list_recipes` — Browse workflow recipes
- `get_recipe` — Get recipe with parameter substitution
- `run_recipe` — Execute a complete recipe automatically (all steps in one shot)

### Files & interaction
- `upload_file` / `download_file` / `list_files` / `delete_file`
- `download_project` — Save project as .qgz
- `mouse_click` / `mouse_scroll` / `key_press` / `mouse_drag` — GUI control
- `zoom_to` — Zoom canvas

## Workflow context

Every mutating tool response includes a `_context` with:
- **phase**: setup / analysis / cartography / export (auto-detected)
- **study_zone**: current zone name
- **layers**: loaded vector layers with feature counts
- **hint**: suggested next action

This guides the AI through structured workflows without hard mode restrictions.

## Recipes

Pre-built workflow templates in `recipes/*.json`:
- `densite_bati` — Building density (hex grid + graduated symbology)
- `urbanisme_general` — Complete urban overview
- `risque_inondation` — Flood risk analysis (buffer zones + building exposure + interactive flood map)
- `occupation_sol` — Land cover analysis
- `pression_fonciere_cotiere` — Coastal land pressure evolution (DVF 2020-2024 + coastal bands + temporal web map)

Usage: `list_recipes()` → `run_recipe(id="risque_inondation", zone="Nimes")` (automated)
Or manual: `get_recipe(id="densite_bati", zone="Montpellier")` → follow steps one by one

## Interactive web exports

Three specialized Leaflet HTML templates for interactive deliverables:
- **Standard** (`leaflet_template.html`) — Vector layers + basemap
- **Flood** (`leaflet_flood_template.html`) — Water height slider, building exposure stats, animation
- **Temporal** (`leaflet_temporal_template.html`) — Year slider, per-band statistics, trend arrows, animated playback

## Grist export

`export_grist` converts geographic data into a complete `.grist` document (SQLite).

### Two modes

1. **From QGIS project** (default) — exports loaded layers with typed columns, map widget, stats, forms
2. **From HTML file** (`html_path`) — universal converter for any HTML containing GeoJSON

### HTML→Grist pipeline

Any Leaflet HTML (from `export_web_map`, `export_flood_map`, `export_temporal_map`, qgis2web, or custom) is converted:

1. **Parse**: Universal FeatureCollection scanner (brace-counting, handles individual vars + `layersData` wrappers)
2. **Extract**: GeoJSON → Grist table specs with typed columns
3. **Detect**: Choice columns (auto-dropdown from unique values), Date columns (ISO→epoch), Ref columns (cross-table)
4. **Transform**: Original map becomes a Grist custom widget reading data from tables via `grist.docApi.fetchTable()`
5. **Assemble**: SQLite `.grist` with 26 meta-tables, data tables, pages (raw data + Carte interactive + Saisie forms)

### Column types

- **Choice**: Auto-detected from form patterns (`categorie`, `priorite`, `statut`...). Collects unique values (≤30), generates colored pills via `choiceOptions`
- **Date**: Detected by name pattern (`date_*`, `*_date`) + ISO value validation. Strings converted to epoch timestamps, `dateFormat: "YYYY-MM-DD"`
- **Ref**: Cross-table references detected by naming (`{table}_id`) + 80% value match. Creates `Ref:TableName` + `gristHelper_Display` formula column

### Key technical details

- Bootstrap JS uses `window["varName"]` refs (not `null`) to handle IIFE→function conversion where local vars shadow globals
- `_safeInit()` guard prevents double-invocation if `_continueInit()` throws
- `_colToFC()` JS function reconstructs GeoJSON FeatureCollections from Grist columnar format
- Widget uses Custom Widget Builder (`@berhalak/custom-widget-builder`) with `accessLevel: 'full'`

### Usage

```bash
# From HTML (flood map, web map, temporal map, any Leaflet HTML)
export_grist(html_path="/data/flood_map.html", document_name="flood_analysis")

# From QGIS project
export_grist(document_name="my_project")
```

## QField export

`export_qfield` produces a portable ZIP ready for QField mobile:
- `.qgz` project with relative GPKG sources
- All vector layers materialized as individual GPKGs
- Editable **Observations** layer with QField-compatible form widgets:
  - Dropdowns (categorie, priorite)
  - Date picker (date_observation)
  - Camera/photo (photo via ExternalResource)
  - Free text (titre, description)

## Multi-user mode

Set `MULTI_USER_MODE=true` in `.env` to enable per-user isolated QGIS containers.

```bash
# .env
MULTI_USER_MODE=true
JWT_SECRET=your-secret-key
IDLE_TIMEOUT_MINUTES=30
BASE_API_PORT=9000
```

Authentication endpoints:
- `POST /api/auth/register` — `{"email": "...", "password": "..."}` → `{"api_key": "qgis_...", "token": "..."}`
- `POST /api/auth/login` — same
- `GET  /api/session` — current user's container info (ports, status)
- `GET  /api/sessions` — list all active sessions (admin)

MCP calls with auth: `Authorization: Bearer qgis_xxxxx`

Docker socket must be mounted (see `docker-compose.yml` comments).

## Build & Run

```bash
docker compose up -d --build
# MCP: http://localhost:8100/mcp
# VNC: http://localhost:6080
# API: http://localhost:8080
# MJPEG: http://localhost:8081/stream
```

## Development

Source files are mounted as volumes in dev. Edit locally, restart container:
```bash
docker compose restart qgisremotemcp
```

QGIS bridge changes require full restart (loaded at QGIS startup).

## Publishing — read this before changing anything shipped in the image

`.github/workflows/build.yml` rebuilds and pushes
`ghcr.io/nic01asfr/qgisremotemcp` on every push to `main` **that touches a
file baked into the image** (`Dockerfile`, `src/`, `skills/`, `recipes/`,
`templates/`, `datasources.json`, `requirements.txt`, the startup scripts).
Documentation-only commits do not trigger it — the build takes ~30 min.
`workflow_dispatch` is available for a manual run.

That image is consumed by a **separate** project, `qgis-sspcloud`, whose
Helm chart pins `workspace.image.repository: ghcr.io/nic01asfr/qgisremotemcp`.
It runs as the `qgis-workspace-*` pod on SSPCloud.

Two consequences worth remembering:

- **A merged fix does not reach production on its own.** The image is
  rebuilt automatically, but running pods keep the image they started with:
  restart the workspace pod to pull (`imagePullPolicy: Always`). Nothing
  reports that a pod is running an older image.
- Nothing under `datasources.json`, `recipes/`, `skills/`, `templates/` or
  `src/` is mounted at runtime — it is all baked in, so none of it can be
  hot-fixed on a running pod.

Existing QGIS projects (`.qgz`) keep the layer definitions they were saved
with. A corrected entry in `datasources.json` only reaches a study once the
layer is loaded again.

GHCR note: the package was originally created by hand, so this repository
must stay listed with *Write* access under the package's *Manage Actions
access* settings — otherwise the push fails with
`denied: permission_denied: write_package`.

## Data source pitfall — display vs compute

WMS layers are not interchangeable. Some serve *pixels* (renderable), others
serve *values* (not renderable by QGIS).

`ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES` in `image/tiff` returns 32-bit
float elevations. QGIS cannot decode those samples — every tile logs

```
foo: Sorry, can not handle images with 32-bit samples.
```

The layer then shows as loaded but paints nothing, reports a `0x0` extent
(which throws off `zoomToFullExtent`), and the flood of render errors has
been observed crashing the QGIS process in production.

The format cannot simply be swapped — that layer only accepts TIFF:

| request | result |
|---|---|
| `HIGHRES` + `image/tiff` | 200, unusable for display |
| `HIGHRES` + `image/png` / `image/jpeg` | 400 |
| `…GRIDCOVERAGE.SHADOW` + `image/png` | 200, renders |

Hence the split in `datasources.json`: `ign_dem` (hillshade, display) and
`ign_dem_altitudes` (raw values, `display: false`, compute only).

When adding an elevation or coverage source, check what the service actually
returns before exposing it to `smart_load`.

## External services

Vision backends (Moondream, SAMGeo3, DepthPro) run separately.
Configured via env vars, called from PyQGIS scripts via HTTP.

## Testing

```bash
# Health
curl http://localhost:8080/health

# Screenshot
curl http://localhost:8080/api/screenshot

# Execute Python
curl -X POST http://localhost:8080/api/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "result[\"version\"] = Qgis.version()"}'

# Run a recipe
curl -X POST http://localhost:8100/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"run_recipe","arguments":{"id":"risque_inondation","zone":"Nimes"}}}'

# Export QField package
curl -X POST http://localhost:8100/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"export_qfield","arguments":{"project_name":"my_project"}}}'

# Export HTML→Grist (universal converter)
curl -X POST http://localhost:8100/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"export_grist","arguments":{"html_path":"/data/flood_map.html","document_name":"flood_analysis"}}}'
```
