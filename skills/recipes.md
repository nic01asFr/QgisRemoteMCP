# Recipes — Reproducible GIS Workflows

## Concept

Recipes are step-by-step workflow templates stored as JSON files. They describe
complete GIS analyses using existing MCP tools. The AI reads a recipe via
`get_recipe`, then executes each step sequentially.

**Recipes are guides, not auto-execution.** The AI adapts to errors, describes
results, and can modify steps if needed.

## Usage

### 1. List available recipes
```
list_recipes()
→ [{id, name, description, tags, parameters}, ...]
```

### 2. Get a recipe with parameters
```
get_recipe(id="densite_bati", zone="Montpellier")
→ {steps: [{tool, params, description}, ...], outputs: [...]}
```

### 3. Execute each step
Follow the steps in order. Each step specifies:
- `tool`: which MCP tool to call
- `params`: parameters (already substituted)
- `description`: what this step does
- `code`: (for execute_python steps) the Python code to run

## Available Recipes

### `densite_bati` — Building Density Map
- **Parameters**: zone (required), grid_size (default: 500m)
- **Steps**: study zone → basemap → buildings → hex grid → centroids → count → graduated style → layout → PDF
- **Output**: PDF A3 with graduated color map, density grid layer

### `urbanisme_general` — Urban Overview
- **Parameters**: zone (required)
- **Steps**: study zone → basemap → buildings + roads + vegetation + hydro → themed styles → layout → PDF
- **Output**: PDF A3 with complete urban view

### `risque_inondation` — Flood Risk Analysis
- **Parameters**: zone (required), buffer_m (default: 100m)
- **Steps**: study zone → hydro + buildings → buffer → intersection → risk styling → layout → PDF
- **Output**: PDF with flood risk zones, exposed buildings count & percentage

### `occupation_sol` — Land Cover Analysis
- **Parameters**: zone (required)
- **Steps**: study zone → CLC + vegetation + hydro + buildings → surface stats → themed colors → layout → PDF
- **Output**: PDF with land cover, area statistics by category

## Adapting Recipes

The AI should adapt recipes when needed:
- **Layer names may vary**: use fuzzy matching (e.g., "bâtiment" or "BATIMENT")
- **Processing may fail**: check results, retry with different parameters
- **Zones may be large**: adjust max_features or grid_size
- **Custom styling**: the user may want different colors or ranges

## Creating New Recipes

Recipe JSON schema:
```json
{
  "id": "unique_id",
  "name": "Human-readable name",
  "description": "What this recipe produces",
  "tags": ["theme1", "theme2"],
  "parameters": {
    "zone": {"type": "string", "required": true, "description": "..."},
    "param2": {"type": "number", "default": 500, "description": "..."}
  },
  "steps": [
    {"tool": "tool_name", "params": {...}, "description": "..."},
    {"tool": "execute_python", "code": "...", "description": "..."}
  ],
  "outputs": ["Description of outputs"]
}
```

Parameters use `$name` placeholders that are substituted by `get_recipe`.
