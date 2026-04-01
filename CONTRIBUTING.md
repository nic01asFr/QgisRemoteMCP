# Contributing to QgisRemoteMCP

Thank you for your interest in contributing! This document explains how the project is structured and how to add features.

## Development setup

```bash
git clone https://github.com/nic01asFr/BigQgisMCP.git
cd BigQgisMCP
cp .env.example .env
docker compose up -d --build
```

Source files are mounted as volumes — edit locally, restart to apply:

```bash
docker compose restart qgisremotemcp
docker compose logs -f qgisremotemcp
```

## Code map

### `main_mcp.py` — MCP Server (~2300 lines)

The MCP entry point. Contains:
- **`TOOLS[]`** list — tool definitions (name, description, inputSchema)
- **`_tool_*()` functions** — one per tool, dispatches to `qgis_command()`
- **`TOOL_HANDLERS`** dict — maps tool names to handler functions
- **`handle_mcp_message()`** — JSON-RPC dispatch
- **`handle_mcp()`** — Starlette route, auth, session management
- Multi-user routes (`/api/auth/*`, `/api/session*`)

### `src/qgis_bridge.py` — QGIS Bridge (~5300 lines)

Runs inside QGIS as a startup script. Contains:
- **`QGISBridge`** class with `_action_*()` methods (one per bridge action)
- Socket server thread (reads JSON commands, dispatches to main thread)
- `_build_context()` — workflow context appended to mutating responses
- Export functions (PDF, web maps, QField, Grist)

### `src/qgis_helpers.py` — Python helpers

Injected into `execute_python` as `helpers` module. Functions for geocoding, WFS download, layer creation, study zone management.

### `src/api_server.py` — REST API

FastAPI server bridging HTTP to the QGIS socket. File upload/download, screenshot, command proxy.

### `src/container_manager.py` — Multi-user (optional)

Docker SDK-based container lifecycle. Per-user QGIS containers with GPU passthrough.

## How to add a new tool

1. **Define the tool** in `TOOLS[]` (main_mcp.py):
   ```python
   {
       "name": "my_tool",
       "description": "What it does.",
       "inputSchema": {
           "type": "object",
           "properties": { ... },
           "required": [...]
       }
   }
   ```

2. **Add the handler** function:
   ```python
   def _tool_my_tool(arguments: dict) -> dict:
       response = qgis_command("my_action", {...})
       return {"content": _text(response) + _auto_screenshot()}
   ```

3. **Register** in `TOOL_HANDLERS`:
   ```python
   "my_tool": _tool_my_tool,
   ```

4. **Add the bridge action** in `src/qgis_bridge.py`:
   ```python
   def _action_my_action(self, params: dict) -> dict:
       # PyQGIS code here
       return {"success": True, ...}
   ```

5. **Register** the action name in the bridge's `MUTATING_ACTIONS` set if it modifies the project (triggers `_build_context()`).

## How to add a data source

Edit `datasources.json`:

```json
{
  "id": "my_source",
  "name": "Human-readable name",
  "category": "topography",
  "type": "wfs",
  "url": "https://...",
  "typename": "namespace:LayerName",
  "description": "What this source contains"
}
```

## How to add a recipe

Create `recipes/my_recipe.json`:

```json
{
  "id": "my_recipe",
  "name": "My Analysis",
  "description": "What it does",
  "steps": [
    {"action": "set_study_zone", "params": {"target": "{zone}"}},
    {"action": "smart_load", "params": {"id": "bdtopo_batiments"}},
    {"action": "run_processing", "params": {"algorithm": "native:buffer", ...}}
  ]
}
```

## How to add a skill resource

Create `skills/my_skill.md` with the skill content. Register it in `main_mcp.py`:
- Add to the `RESOURCES` list
- Add a handler in `_read_resource()`

## Code conventions

- `main_mcp.py` and `qgis_bridge.py` are single files by design — do not split
- Use `_text()` + `_auto_screenshot()` for tool responses that modify the project
- All bridge actions run on Qt main thread — never create Qt objects in threads
- Screenshots are JPEG ≤1MB — the bridge handles compression automatically
- Test with a real QGIS container, not unit tests (PyQGIS requires a running QGIS instance)

## Submitting changes

1. Fork the repo
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Test with `docker compose up -d`
4. Commit with descriptive messages
5. Submit a merge request

For questions, open an issue on [GitHub](https://github.com/nic01asFr/BigQgisMCP/issues) or [GitLab CEREMA](https://gitlab.cerema.fr/mcp/QgisRemoteMCP/-/issues).
