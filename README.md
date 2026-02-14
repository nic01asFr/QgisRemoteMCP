# BigQgisMCP

**QGIS Desktop as an MCP Server** — Full GUI via noVNC + PyQGIS scripting + 1000+ Processing algorithms.

An AI assistant writes and executes QGIS scripts directly, while users interact with the same QGIS instance in their browser.

<p align="center">
  <img src="docs/architecture.png" alt="Architecture" width="700">
</p>

## What is this?

BigQgisMCP puts a complete QGIS Desktop inside a Docker container and exposes it as an [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server. This means:

- **AI assistants** (Claude, etc.) can write and execute PyQGIS scripts
- **Users** see QGIS running live in their browser via noVNC
- **Both** work on the same QGIS instance simultaneously
- **1000+ Processing algorithms** available (native, GDAL, GRASS, SAGA)
- **Skills** guide the AI with PyQGIS patterns, data sources, cartography

Part of the [BigApp](https://github.com/nic01asFr/BigBlenderMCP) family (BigBlenderMCP pattern).

## Quick Start

```bash
# Clone
git clone https://github.com/nic01asFr/BigQgisMCP.git
cd BigQgisMCP

# Configure (optional)
cp .env.example .env
# Edit .env to set vision service URLs if needed

# Build & start
docker compose up -d --build

# Open QGIS in browser
open http://localhost:6080
```

### Endpoints

| Port | Service | URL |
|------|---------|-----|
| 6080 | noVNC (QGIS in browser) | http://localhost:6080 |
| 8100 | MCP Server | http://localhost:8100/mcp |
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

## MCP Tools

| Tool | Description |
|------|-------------|
| `execute_python` | **The power tool.** Write and run PyQGIS code. Full access to qgis.core, iface, processing. |
| `get_screenshot` | Capture current map canvas as PNG |
| `get_project_info` | Current project state (layers, CRS, layouts) |
| `add_layer` | Add vector/raster/WFS/WMS layers |
| `remove_layer` | Remove a layer |
| `get_features` | Query features with filters |
| `run_processing` | Execute Processing algorithms |
| `search_algorithms` | Find algorithms |
| `zoom_to` | Navigate the map |
| `export_pdf` | Export print layout to PDF |
| `get_vnc_url` | Get URL for interactive access |
| `new_project` / `open_project` / `save_project` | Project management |

## MCP Skills (Resources)

Skills are reference documents that guide the AI assistant:

| Resource URI | Content |
|-------------|---------|
| `skill://pyqgis` | PyQGIS scripting patterns & examples |
| `skill://processing` | Processing algorithms guide |
| `skill://cartography` | Styling, symbology, print layouts |
| `skill://external-services` | Calling Moondream, SAMGeo3, DepthPro |
| `skill://data-sources` | French national datasets (IGN, BAN, Panoramax, ...) |
| `skill://qgis-status` | Live QGIS instance status |

## External Vision Services

BigQgisMCP doesn't embed vision models. They run as separate services accessible via HTTP from PyQGIS scripts:

| Service | Default URL | Purpose |
|---------|-------------|---------|
| Moondream | http://localhost:8001 | Image captioning, object detection, VQA |
| SAMGeo3 | http://localhost:8002 | Geospatial segmentation |
| DepthPro | http://localhost:8003 | Monocular depth estimation |

Configure via environment variables (`MOONDREAM_URL`, `SAMGEO3_URL`, `DEPTHPRO_URL`).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                 BigQgisMCP Container                     │
│                                                           │
│  supervisord                                              │
│  ├── Xvfb :99              (virtual display)             │
│  ├── fluxbox               (window manager)              │
│  ├── QGIS Desktop ◄───────────────────┐                 │
│  │   └── qgis_bridge.py   (startup)   │ UNIX socket     │
│  ├── x11vnc → noVNC       (:6080)     │                 │
│  ├── api_server.py         (:8080) ────┘                 │
│  ├── main_mcp.py           (:8100) ────┘                 │
│  └── stream_server.py      (:8081)                       │
└──────────┬──────────────────────────────────────────────┘
           │ HTTP (env vars)
    ┌──────┼──────────┐
    ▼      ▼          ▼
Moondream SAMGeo3  DepthPro     (external, optional)
```

## Example Conversation

```
User: "Analyse les bâtiments autour de la gare de Nîmes"

AI: [reads skill://data-sources, skill://pyqgis]
    [execute_python → geocode "gare de Nîmes" via BAN API]
    [execute_python → add WFS layers (buildings, roads) from BD TOPO]
    [execute_python → style buildings by height]
    [get_screenshot → shows the map to user]
    [get_vnc_url → "You can explore the map here: http://..."]

User: [clicks around in QGIS via noVNC, adjusts view]
      "Generate a PDF report"

AI: [execute_python → create print layout with title, legend, scalebar]
    [export_pdf → /data/analyse_nimes.pdf]
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
  -d '{"code": "result[\"v\"] = Qgis.version()"}'
```

## Project Structure

```
BigQgisMCP/
├── main_mcp.py           # MCP Server (tools, resources, prompts)
├── src/
│   ├── qgis_bridge.py    # Runs inside QGIS (UNIX socket bridge)
│   ├── api_server.py     # FastAPI REST API
│   └── stream_server.py  # MJPEG stream
├── skills/
│   ├── pyqgis.md         # PyQGIS scripting reference
│   ├── processing.md     # Processing algorithms guide
│   ├── cartography.md    # Styling & print layouts
│   ├── external_services.md  # Vision services integration
│   └── data_sources.md   # French national datasets
├── projects/             # QGIS project files (persisted)
├── Dockerfile            # Single container build
├── docker-compose.yml    # One service
├── supervisord.conf      # Process orchestration
├── entrypoint.sh         # Container startup
├── requirements.txt      # Python dependencies
├── CLAUDE.md             # Claude Code instructions
└── .env.example          # Environment template
```

## License

MIT

## Credits

- **BigApp pattern** from [BigBlenderMCP](https://github.com/nic01asFr/BigBlenderMCP)
- **QGIS** — https://qgis.org
- **noVNC** — https://novnc.com
- **MCP** — https://modelcontextprotocol.io
