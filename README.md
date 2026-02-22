# BigQgisMCP

**QGIS Desktop as an MCP Server** — Full GUI via noVNC, PyQGIS scripting, 1000+ Processing algorithms, smart data pipeline with 30+ French national datasets.

An AI assistant controls a live QGIS Desktop — loads data, runs analysis, produces maps — while users interact with the same instance in their browser.

<p align="center">
  <img src="docs/architecture.png" alt="Architecture" width="700">
</p>

## Highlights

- **Smart Data Pipeline** — `set_study_zone("Montpellier")` then `smart_load("bdtopo_batiments")`. Downloads WFS as local GeoPackage with automatic pagination, R-tree spatial index, CRS transform. Processing runs 60-250x faster than live WFS.
- **30+ Pre-configured French Datasets** — BD TOPO, Admin Express, RPG, IGN orthophotos, cadastre, DEM, Corine Land Cover, OSM, Esri. No API key needed.
- **Full QGIS Desktop** — Live GUI via noVNC. AI and user work on the same instance simultaneously.
- **1000+ Processing Algorithms** — Native, GDAL, GRASS, SAGA. All accessible via MCP tools.
- **Automated Recipes** — Pre-built workflows (density analysis, flood risk, land cover, coastal pressure). One command: `run_recipe("risque_inondation", zone="Nimes")`.
- **Multi-format Export** — PDF layouts, interactive Leaflet maps (standard, flood analysis, temporal), QField mobile packages, Grist collaborative documents.
- **MCP App** — Interactive QGIS view embedded directly in the conversation (VNC viewer, file upload, keyboard/mouse forwarding).

## Quick Start

```bash
git clone https://github.com/nic01asFr/BigQgisMCP.git
cd BigQgisMCP
docker compose up -d --build
```

### Endpoints

| Port | Service | URL |
|------|---------|-----|
| 6080 | noVNC (QGIS in browser) | http://localhost:6080 |
| 8100 | MCP Server (Streamable HTTP) | http://localhost:8100/mcp |
| 8080 | REST API | http://localhost:8080/docs |
| 8081 | MJPEG stream | http://localhost:8081/stream |

### Connect to Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "qgis": {
      "url": "http://localhost:8100/mcp"
    }
  }
}
```

## Smart Data Loading

The core innovation: a structured pipeline that replaces unreliable live WFS connections with fast local GeoPackage files.

### Why?

| Problem | Live WFS | Smart Loading |
|---------|----------|---------------|
| Pagination | IGN silently truncates at 5000 | ogr2ogr handles all pages automatically |
| Spatial index | None (in-memory) | R-tree in GeoPackage |
| Processing speed | 60-250x slower (network + no index) | Fast local file |
| Network during analysis | HTTP requests per feature | Zero network |
| CRS confusion | Mixed 4326/3857/2154 | Standardized EPSG:2154 |

### Pipeline

```
1. set_study_zone(target="Montpellier")    # Geocode, store bbox, zoom
2. smart_load(id="osm_xyz")                # Basemap (streaming)
3. smart_load(id="bdtopo_batiments")       # Buildings (download GPKG)
4. smart_load(id="bdtopo_routes")          # Roads (download GPKG)
5. get_screenshot                           # Verify
6. run_processing / execute_python          # Analyze
7. export_pdf / export_web_map / ...        # Deliver (PDF, HTML, QField, Grist)
```

### Performance (Montpellier, ~10 km bbox)

| Operation | Features | Time |
|-----------|----------|------|
| Download buildings | 10,000 | ~30s |
| Download roads | 5,000 | 6.8s |
| Cache reload (2nd call) | 10,000 | instant |
| Buffer 50m | 10,000 | 1.8s |
| Dissolve by usage | 10,000 | 2.2s |
| Density grid 500m | 440 cells | 0.3s |

### Caching

Downloads are cached in `/data/cache/` with bbox hash. Same area = instant reload for 24h.

## Available Data Sources

All sources are free (IGN open data since July 2021). No API key needed.

### Vector Data (WFS, downloaded as GPKG)

| ID | Name | Key Attributes |
|----|------|----------------|
| `bdtopo_batiments` | Buildings | nature, usage, height, floors, materials |
| `bdtopo_routes` | Roads | nature, importance, width, lanes, speed |
| `bdtopo_hydrographie` | Rivers | name, class, width |
| `bdtopo_vegetation` | Vegetation zones | nature |
| `bdtopo_voie_ferree` | Railways | nature, nb_voies |
| `bdtopo_hydro_surfaces` | Water bodies | nature, name |
| `bdtopo_communes` | Communes (BD TOPO) | name, code INSEE, population |
| `admin_express_communes` | Communes (Admin Express) | name, code, population |
| `admin_express_departements` | Departments | name, code |
| `admin_express_regions` | Regions | name, code |
| `rpg` | Agricultural parcels | crop type, area |
| `bdtopo_poi` | Points of interest | nature |
| `bdtopo_lieu_dit` | Place names | name |
| `bdtopo_surface_activite` | Activity zones | nature |
| `bdtopo_equipement_transport` | Transport facilities | nature |

### Basemaps (streaming)

| ID | Name |
|----|------|
| `osm_xyz` | OpenStreetMap |
| `ign_planign` | Plan IGN v2 |
| `ign_scan25` | Cartes topo IGN 1:25000 |
| `cartodb_positron` | Light basemap |
| `cartodb_dark` | Dark basemap |
| `esri_world_topo` | Esri World Topographic |
| `stamen_terrain` | Stamen Terrain (relief) |

### Imagery (streaming)

| ID | Name |
|----|------|
| `ign_ortho_wmts` | IGN orthophotos (WMTS, fast) |
| `ign_ortho_wms` | IGN orthophotos (WMS) |
| `ign_ortho_irc` | IGN infrared photos |
| `esri_world_imagery` | Esri satellite |

### Other (WMS/API)

| ID | Name |
|----|------|
| `ign_cadastre` | Cadastral parcels |
| `ign_dem` | High-resolution DEM |
| `corine_land_cover` | Land cover 2018 |
| `ban_geocode` | Address geocoding API |
| `geo_api_communes` | Commune info API |
| `dvf_api` | Property transactions API |
| `panoramax` | Street-level imagery API |
| `ign_altimetrie` | Elevation API |

## Recipes

Pre-built workflow templates that automate complete analyses — from data loading to styled map export.

| ID | Name | Description |
|----|------|-------------|
| `densite_bati` | Building Density | Hex grid density analysis with graduated symbology |
| `urbanisme_general` | Urban Overview | Buildings, roads, vegetation, hydrology with categorized styles |
| `risque_inondation` | Flood Risk | Flood zones, building exposure, buffer analysis + interactive web map |
| `occupation_sol` | Land Cover | Corine Land Cover with categorized symbology |
| `pression_fonciere_cotiere` | Coastal Land Pressure | DVF transactions 2020-2024, coastal bands + temporal web map |

### Usage

```
# Automated (all steps in one shot)
run_recipe(id="risque_inondation", zone="Nimes")

# Manual (follow steps one by one)
get_recipe(id="densite_bati", zone="Montpellier")
→ Returns step-by-step instructions to execute individually
```

## Export Formats

### PDF Layout

Print-ready PDF via QGIS print layouts with pre-built templates (A3 landscape, A4 portrait). Includes title, legend, scalebar, north arrow, and data sources.

```
apply_layout_template(template="a3_landscape", title="Flood Risk — Nimes")
export_pdf(layout="a3_landscape")
```

### Interactive Web Map

Leaflet HTML files with embedded GeoJSON data. Three specialized templates:

| Template | Use case | Features |
|----------|----------|----------|
| **Standard** | General map | Layer toggle, popup, legend, basemap selector |
| **Flood** | Flood risk analysis | Water height slider, building exposure stats, animation |
| **Temporal** | Time series | Year slider, per-band statistics, trend arrows, animated playback |

### QField Mobile Package

Portable ZIP ready for [QField](https://qfield.org/) mobile data collection:

- `.qgz` project with relative GPKG sources
- All vector layers materialized as individual GeoPackages
- Editable **Observations** layer with form widgets (dropdowns, date picker, camera, free text)

```
export_qfield(project_name="terrain_survey")
→ /data/terrain_survey_qfield.zip
```

### Grist Document

Converts QGIS project layers or any HTML map into a [Grist](https://www.getgrist.com/) collaborative document (`.grist`):

- **From project** — Exports visible vector layers as Grist tables with a custom map widget
- **From HTML** — Universal converter: takes any HTML file containing GeoJSON (flood maps, temporal maps, qgis2web exports) and creates a Grist document with data in tables and the original interactive map as a Grist custom widget

Detected column types: `Choice` (colored dropdowns), `Date` (epoch timestamps), `Ref` (cross-table references). Form-like tables automatically get a Grist Form page.

```
# From QGIS project
export_grist(title="Urban Analysis")

# From any HTML with GeoJSON
export_grist(html_path="/data/flood_map_nimes.html")
```

## MCP Tools

### Smart Loading
| Tool | Description |
|------|-------------|
| `set_study_zone` | Define study area (commune, address, bbox). Geocodes, stores bbox, zooms canvas. |
| `get_study_zone` | Get current study zone (name, bbox in 4326 + 2154). |
| `smart_load` | Load data by catalog ID. WFS → local GPKG with spatial index. Rasters stream. |

### Core
| Tool | Description |
|------|-------------|
| `execute_python` | Run PyQGIS code with full access to iface, project, processing, `helpers` module. |
| `get_screenshot` | Capture QGIS canvas as PNG. Auto-included after modifying tools. |
| `get_project_info` | Current project state (layers, CRS, layouts, extents). |
| `run_processing` | Execute any of 1000+ Processing algorithms. |
| `search_algorithms` | Find Processing algorithms by keyword. |
| `zoom_to` | Navigate to extent, layer, or point. |

### Data & Layers
| Tool | Description |
|------|-------------|
| `add_layer` | Add vector/raster/WFS/WMS by URI. |
| `remove_layer` | Remove a layer. |
| `get_features` | Query features with attribute/spatial filters. |
| `list_datasources` | Browse the pre-configured data catalog. |
| `add_from_catalog` | Add a source by catalog ID. |

### Styling
| Tool | Description |
|------|-------------|
| `set_layer_style` | Apply single color, categorized, or graduated symbology. |
| `set_layer_visibility` | Show/hide layers. |
| `apply_layout_template` | Apply a print layout template (A3 landscape, A4 portrait). |
| `list_layout_templates` | List available layout templates. |

### Recipes
| Tool | Description |
|------|-------------|
| `list_recipes` | Browse available workflow recipes. |
| `get_recipe` | Get recipe details with parameter substitution. |
| `run_recipe` | Execute a complete recipe automatically (all steps in one shot). |

### Export
| Tool | Description |
|------|-------------|
| `export_pdf` | Export print layout to PDF. |
| `export_web_map` | Export visible layers as interactive Leaflet HTML. |
| `export_flood_map` | Interactive flood analysis HTML (water height slider, building exposure). |
| `export_temporal_map` | Interactive temporal analysis HTML (year slider, animated playback). |
| `export_qfield` | QField-ready ZIP package (.qgz + GPKGs + editable Observations layer). |
| `export_grist` | Grist document from project layers or from any HTML with GeoJSON. |
| `export_layer` | Export vector layer to GPKG, GeoJSON, Shapefile, CSV. |

### Files
| Tool | Description |
|------|-------------|
| `upload_file` | Upload file (shapefile, GeoJSON, GPKG, CSV, TIFF, project). |
| `download_file` | Download file from /data/. |
| `list_files` | List files in /data/. |
| `delete_file` | Delete file from /data/. |
| `download_project` | Save project as .qgz. |

### GUI Interaction
| Tool | Description |
|------|-------------|
| `qgis_desktop_ui` | Open interactive QGIS MCP App in conversation. |
| `mouse_click` / `mouse_scroll` / `mouse_drag` / `key_press` | Direct GUI interaction via xdotool. |

### Projects
| Tool | Description |
|------|-------------|
| `new_project` | Create empty project. |
| `open_project` | Open a .qgz project. |
| `save_project` | Save current project. |

## MCP Skills (Resources)

Reference documents that guide the AI assistant's expertise:

| Resource URI | Content |
|-------------|---------|
| `skill://smart-loading` | Smart Loading Pipeline — set_study_zone + smart_load, CRS handling, caching |
| `skill://pyqgis` | PyQGIS scripting patterns & API usage |
| `skill://processing` | Processing algorithms guide (native, GDAL, GRASS) |
| `skill://cartography` | Symbology, labels, print layouts, PDF export |
| `skill://helpers` | Ready-made Python helpers (geocode, add_wfs, zoom_to, create_point_layer...) |
| `skill://data-sources` | French national datasets reference |
| `skill://recipes` | Workflow recipes reference |
| `skill://external-services` | Vision services integration (Moondream, SAMGeo3, DepthPro) |
| `skill://qgis-status` | Live QGIS instance status |

## MCP Prompts

| Prompt | Description |
|--------|-------------|
| `analyse_territoire` | Template for territory analysis (zone + question). |
| `workflow_donnees` | **Guided workflow** — theme-based loading (urbanisme, environnement, transport, agriculture, risques) with step-by-step instructions. |

## MCP App

An interactive QGIS view embedded directly in the Claude conversation:

- **Live VNC viewer** — See and interact with QGIS in-conversation
- **File upload** — Drag & drop files directly into the QGIS container
- **Keyboard/mouse forwarding** — Full interaction without leaving the chat
- **MJPEG fallback** — Lightweight stream for quick visual feedback

## Example Workflows

### Manual: Urban Analysis

```
User: "Analyse l'urbanisation autour de Montpellier"

AI: [set_study_zone("Montpellier")]
    → Geocodes, stores bbox, zooms canvas

    [smart_load("osm_xyz")]              → OpenStreetMap basemap
    [smart_load("bdtopo_batiments")]     → 10,000 buildings as GPKG
    [smart_load("bdtopo_routes")]        → 5,000 road segments

    [execute_python → density grid]      → 500m hex grid, graduated symbology
    [apply_layout_template("a3_landscape", title="Densité bâtie — Montpellier")]
    [export_pdf]                         → /data/densite_montpellier.pdf
```

### Automated: Flood Risk with Recipe

```
User: "Analyse le risque inondation à Nîmes"

AI: [run_recipe("risque_inondation", zone="Nimes")]
    → Executes all steps automatically:
      1. set_study_zone("Nimes")
      2. smart_load basemap + buildings + flood zones
      3. Buffer analysis (50m, 100m, 200m from flood zones)
      4. Building exposure classification
      5. Graduated symbology
    → Returns screenshot + statistics

    [export_flood_map(include_fields=["nature","usage","height"])]
    → Interactive HTML with water height slider

    [export_grist(html_path="/data/flood_map_nimes.html")]
    → Grist document with editable tables + embedded map widget
```

### Field Survey: QField Export

```
User: "Prépare un relevé terrain pour la commune de Sète"

AI: [set_study_zone("Sète")]
    [smart_load("bdtopo_batiments")]
    [smart_load("bdtopo_routes")]
    [set_layer_style("Batiments", type="categorized", field="usage")]

    [export_qfield(project_name="releve_sete")]
    → ZIP with .qgz + GPKGs + Observations layer (camera, dropdowns, date picker)
    → Ready to load on QField mobile app
```

## External Vision Services

Optional services accessible from PyQGIS scripts via HTTP:

| Service | Default URL | Purpose |
|---------|-------------|---------|
| Moondream | http://localhost:8001 | Image captioning, object detection, VQA |
| SAMGeo3 | http://localhost:8002 | Geospatial segmentation |
| DepthPro | http://localhost:8003 | Monocular depth estimation |

Configure via environment variables (`MOONDREAM_URL`, `SAMGEO3_URL`, `DEPTHPRO_URL`).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    BigQgisMCP Container                      │
│                                                              │
│  supervisord                                                 │
│  ├── Xvfb :99                (virtual display 1920x1080)    │
│  ├── fluxbox                 (window manager)               │
│  ├── QGIS Desktop ◄─────────────────────┐                  │
│  │   └── qgis_bridge.py    (startup)    │ UNIX socket      │
│  ├── x11vnc → noVNC         (:6080)     │                  │
│  ├── api_server.py           (:8080) ───┘                   │
│  ├── main_mcp.py             (:8100) ───┘                   │
│  └── stream_server.py        (:8081)                        │
│                                                              │
│  /data/          (user files, projects)                     │
│  /data/cache/    (smart_load GPKG cache, 24h)               │
│  /app/skills/    (MCP skill documents)                      │
│  /app/datasources.json  (30+ pre-configured sources)        │
└──────────┬──────────────────────────────────────────────────┘
           │ HTTP (optional)
    ┌──────┼──────────┐
    ▼      ▼          ▼
Moondream SAMGeo3  DepthPro     (external vision services)
```

### Communication Flow

```
Claude Desktop / MCP Client
  │ JSON-RPC over Streamable HTTP (:8100)
  ▼
main_mcp.py (MCP Server)
  │ UNIX socket /tmp/qgis_bridge.sock
  ▼
qgis_bridge.py (runs inside QGIS, main thread)
  │ PyQGIS API (iface, QgsProject, processing)
  ▼
QGIS Desktop (Xvfb display :99)
  │ X11
  ▼
x11vnc → websockify → noVNC (:6080)
  │ WebSocket
  ▼
User's browser
```

## Development

```bash
# Source files are mounted as volumes — edit locally
# Restart to apply changes:
docker compose restart bigqgismcp

# View logs
docker compose logs -f bigqgismcp

# Test API
curl http://localhost:8080/health
curl -X POST http://localhost:8080/api/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "result[\"v\"] = Qgis.version()"}'

# Test smart loading
curl -X POST http://localhost:8080/api/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "result.update(helpers.set_study_zone(\"Montpellier\"))", "timeout": 30}'
```

## Project Structure

```
BigQgisMCP/
├── main_mcp.py             # MCP Server (40 tools, 10 resources, 3 prompts)
├── datasources.json        # 30+ pre-configured data sources catalog
├── qgis_app.html           # MCP App (interactive QGIS in conversation)
├── src/
│   ├── qgis_bridge.py      # Runs inside QGIS (UNIX socket bridge, 45 actions)
│   ├── qgis_helpers.py     # Python helpers (geocode, smart loading, etc.)
│   ├── api_server.py       # FastAPI REST API
│   └── stream_server.py    # MJPEG stream
├── skills/                 # MCP Resources (AI skill documents)
│   ├── smart_loading.md
│   ├── pyqgis.md
│   ├── processing.md
│   ├── cartography.md
│   ├── helpers.md
│   ├── data_sources.md
│   └── external_services.md
├── recipes/                # Workflow recipes (JSON)
│   ├── densite_bati.json
│   ├── urbanisme_general.json
│   ├── risque_inondation.json
│   ├── occupation_sol.json
│   └── pression_fonciere_cotiere.json
├── templates/              # Print layout templates (.qpt)
│   ├── a3_landscape.qpt
│   ├── a4_portrait.qpt
│   └── web/                # Leaflet HTML templates
│       ├── leaflet_template.html
│       ├── leaflet_flood_template.html
│       └── leaflet_temporal_template.html
├── projects/               # QGIS project files (persisted)
├── docs/                   # Architecture diagrams
├── Dockerfile
├── docker-compose.yml
├── supervisord.conf
├── entrypoint.sh
├── requirements.txt
└── CLAUDE.md
```

## License

MIT

## Credits

- **BigApp pattern** from [BigBlenderMCP](https://github.com/nic01asFr/BigBlenderMCP)
- **QGIS** — https://qgis.org
- **noVNC** — https://novnc.com
- **MCP** — https://modelcontextprotocol.io
- **IGN Geoplateforme** — https://data.geopf.fr (free French national geodata)
- **GDAL/OGR** — https://gdal.org (data conversion & download engine)
