# Smart Data Loading Pipeline

## Why Smart Loading?

Direct WFS connections have critical issues for analysis:
- **Pagination**: IGN WFS pages at 5000 features. `max_features=10000` silently returns only the first page.
- **No spatial index**: WFS layers in memory lack R-tree index. Processing algorithms are 60-250x slower.
- **Network during Processing**: Algorithms on live WFS trigger HTTP requests, causing timeouts.
- **Unreliable bbox**: IGN WFS `max_features` returns first N features globally, not within bbox.

**Solution**: `ogr2ogr` downloads WFS to local GeoPackage with automatic pagination, CRS transform, and R-tree spatial index.

## Pipeline

### Step 1: Define Study Zone

```
set_study_zone(target="Montpellier")
```

Or via `execute_python`:
```python
zone = helpers.set_study_zone("Sete")
# Returns: {"name": "Sète", "bbox_4326": [3.55, 43.33, 3.75, 43.43], "bbox_2154": [...], "center_4326": [...]}
```

Accepts:
- Commune name: `"Montpellier"`, `"Sete"`, `"Paris"`
- Address: `"Gare de Lyon, Paris"`, `"Mairie de Nimes"`
- Point dict: `{"lon": 3.87, "lat": 43.62}`
- Explicit bbox: `[3.6, 43.3, 3.8, 43.5]` (EPSG:4326)

The zone is stored as QGIS project variables and survives project save/reload:
- `study_zone_name` — label
- `study_zone_bbox_4326` — JSON `[xmin,ymin,xmax,ymax]`
- `study_zone_bbox_2154` — JSON `[xmin,ymin,xmax,ymax]`

### Step 2: Load Data

```
smart_load(id="bdtopo_batiments")
```

For WFS sources, this:
1. Reads bbox from study zone (or uses provided bbox)
2. Runs `ogr2ogr` to download as GeoPackage in `/data/cache/`
3. Reprojects to EPSG:2154 (Lambert 93)
4. Loads the local GPKG into QGIS as an OGR vector layer
5. Returns layer info with `feature_count`, `path`, `cached` status

For raster sources (WMS/WMTS/XYZ), it adds them as streaming layers (no download needed).

### Step 3: Process

Local GPKG layers work fast with all Processing algorithms:
```python
import processing

# Buffer 50m around buildings (1.8s for 10,000 features)
r = processing.run("native:buffer", {
    "INPUT": project.mapLayersByName("Bâtiments (BD TOPO)")[0],
    "DISTANCE": 50,
    "SEGMENTS": 5,
    "OUTPUT": "memory:"
})

# Dissolve by attribute (2.2s for 10,000 features)
r = processing.run("native:dissolve", {
    "INPUT": project.mapLayersByName("Bâtiments (BD TOPO)")[0],
    "FIELD": ["usage_1"],
    "OUTPUT": "memory:"
})

# Building density grid (0.3s)
bl = project.mapLayersByName("Bâtiments (BD TOPO)")[0]
grid = processing.run("native:creategrid", {
    "TYPE": 2, "EXTENT": bl.extent(),
    "HSPACING": 500, "VSPACING": 500,
    "CRS": bl.crs(), "OUTPUT": "memory:"
})
density = processing.run("native:countpointsinpolygon", {
    "POLYGONS": grid["OUTPUT"],
    "POINTS": centroids_layer,
    "FIELD": "building_count",
    "OUTPUT": "memory:"
})
```

### Step 4: Verify

Use `get_screenshot` and describe what you see. Always verify data is in the correct location.

## Performance Benchmarks

Tested on Montpellier study zone (~10 km bbox):

| Operation | Features | Time | Notes |
|-----------|----------|------|-------|
| Download buildings (WFS) | 10,000 | ~30s | 12 MB GPKG |
| Download roads (WFS) | 5,000 | 6.8s | 9 MB GPKG |
| Cache reload | 10,000 | instant | `cached: true` |
| Buffer 50m | 10,000 | 1.8s | Local GPKG |
| Dissolve by usage | 10,000 | 2.2s | 8 categories |
| Density grid 500m | 440 cells | 0.3s | Grid + count |

Compare with live WFS: the same buffer on a WFS layer would take 60-250x longer due to network + no spatial index.

## CRS Handling

| CRS | Usage |
|-----|-------|
| EPSG:4326 | Bbox input (WGS84 lat/lon), geocoding, web APIs |
| EPSG:2154 | Downloaded WFS data (Lambert 93), Processing, measurements |
| EPSG:3857 | Web Mercator display, basemaps (WMS/WMTS/XYZ) |

- `set_study_zone` stores bbox in both 4326 and 2154
- `smart_load` downloads WFS in 4326, then reprojects to the source's native CRS (typically 2154)
- The QGIS project CRS is typically 2154 for France
- Basemaps auto-reproject to match the project CRS
- Processing distances are in meters (Lambert 93 = metric CRS)

## Caching

Downloaded files are cached in `/data/cache/` with a hash of the bbox:
- Cache validity: 24 hours
- Same source + same bbox = instant reload from cache
- Different bbox = new download
- To force re-download, delete the cache file or wait 24h

```python
# Clear all cache
import glob, os
for f in glob.glob("/data/cache/*"):
    os.remove(f)
```

## Available Catalog Sources

### Basemaps (streaming, no download)
- `osm_xyz` — OpenStreetMap standard tiles
- `ign_planign` — Plan IGN v2 (carte topographique)
- `ign_scan25` — Cartes topo IGN 1:25000
- `cartodb_positron` — Light basemap (minimalist)
- `cartodb_dark` — Dark basemap
- `esri_world_topo` — Esri World Topographic
- `stamen_terrain` — Stadia/Stamen Terrain (relief)

### Imagery (streaming, no download)
- `ign_ortho_wmts` — IGN orthophotos (fast, tiled WMTS)
- `ign_ortho_wms` — IGN orthophotos (WMS, supports GetFeatureInfo)
- `ign_ortho_irc` — IGN infrared orthophotos (vegetation/hydrology)
- `esri_world_imagery` — Esri satellite imagery

### Topography (WFS, downloaded as GPKG)
- `bdtopo_batiments` — Buildings (nature, usage, hauteur, nb_logements, nb_etages)
- `bdtopo_routes` — Roads (nature, importance, largeur, nb_voies, vitesse)
- `bdtopo_hydrographie` — Rivers and streams
- `bdtopo_vegetation` — Vegetation zones
- `bdtopo_voie_ferree` — Railways
- `bdtopo_hydro_surfaces` — Water bodies (lakes, ponds)
- `bdtopo_poi` — Points of interest (bell towers, water towers, etc.)
- `bdtopo_lieu_dit` — Place names (uninhabited)
- `bdtopo_surface_activite` — Activity zones (ZI, ZAC, campus)
- `bdtopo_equipement_transport` — Transport facilities (stations, airports, ports)

### Administrative (WFS, downloaded as GPKG)
- `bdtopo_communes` — Communes (BD TOPO)
- `admin_express_communes` — Communes (Admin Express COG)
- `admin_express_departements` — Departments
- `admin_express_regions` — Regions
- `rpg` — Agricultural parcels (RPG, with crop types)

### Administrative (WMS, streaming)
- `ign_cadastre` — Cadastral parcels

### Elevation (WMS, streaming)
- `ign_dem` — High-resolution DEM (MNT)

### Environment (WMS, streaming)
- `corine_land_cover` — Land cover 2018 (Corine)

### APIs (not loaded as layers)
- `ban_geocode` — French address geocoding (BAN)
- `geo_api_communes` — Commune information API
- `dvf_api` — Property transaction data (DVF)
- `panoramax` — Street-level imagery
- `ign_altimetrie` — Altitude/elevation API

## Theme-Based Loading

### Urbanisme
```
set_study_zone(target="Montpellier")
smart_load(id="osm_xyz")
smart_load(id="bdtopo_batiments")
smart_load(id="bdtopo_routes")
smart_load(id="ign_cadastre")
```
Analysis: building density, distances to roads, land use patterns, building heights

### Environnement
```
set_study_zone(target="Camargue")
smart_load(id="ign_ortho_wmts")
smart_load(id="bdtopo_hydrographie")
smart_load(id="bdtopo_vegetation")
smart_load(id="bdtopo_hydro_surfaces")
smart_load(id="corine_land_cover")
```
Analysis: buffer rivers, vegetation proximity, water surface area, land cover change

### Transport
```
set_study_zone(target="Lyon")
smart_load(id="osm_xyz")
smart_load(id="bdtopo_routes")
smart_load(id="bdtopo_voie_ferree")
smart_load(id="bdtopo_equipement_transport")
```
Analysis: road network topology, service areas, accessibility, multimodal connections

### Agriculture
```
set_study_zone(target="Beauce")
smart_load(id="ign_ortho_wmts")
smart_load(id="rpg")
smart_load(id="bdtopo_hydrographie")
smart_load(id="corine_land_cover")
```
Analysis: crop type distribution (dissolve by CODE_GROUP), parcel sizes, water proximity

### Risques naturels
```
set_study_zone(target="Nimes")
smart_load(id="osm_xyz")
smart_load(id="bdtopo_hydro_surfaces")
smart_load(id="bdtopo_hydrographie")
smart_load(id="bdtopo_batiments")
smart_load(id="ign_dem")
```
Analysis: flood buffer zones, exposed buildings (intersection), elevation profiles

## Processing Examples by Theme

### Urbanisme: building density map
```python
import processing
bl = project.mapLayersByName("Bâtiments (BD TOPO)")[0]

# Create 500m grid
grid = processing.run("native:creategrid", {
    "TYPE": 2, "EXTENT": bl.extent(),
    "HSPACING": 500, "VSPACING": 500,
    "CRS": bl.crs(), "OUTPUT": "memory:"
})

# Compute centroids for counting
centroids = processing.run("native:centroids", {
    "INPUT": bl, "OUTPUT": "memory:"
})

# Count buildings per cell
density = processing.run("native:countpointsinpolygon", {
    "POLYGONS": grid["OUTPUT"],
    "POINTS": centroids["OUTPUT"],
    "FIELD": "nb_batiments",
    "OUTPUT": "memory:"
})
project.addMapLayer(density["OUTPUT"])
```

### Environnement: riparian buffer analysis
```python
import processing
rivers = project.mapLayersByName("cours_d_eau")[0]
vegetation = project.mapLayersByName("zone_de_vegetation")[0]

# 100m buffer around rivers
buffer = processing.run("native:buffer", {
    "INPUT": rivers, "DISTANCE": 100, "OUTPUT": "memory:"
})

# Intersection: vegetation within 100m of rivers
riparian = processing.run("native:intersection", {
    "INPUT": vegetation,
    "OVERLAY": buffer["OUTPUT"],
    "OUTPUT": "memory:"
})
project.addMapLayer(riparian["OUTPUT"])
```

### Risques: buildings in flood zone
```python
import processing
rivers = project.mapLayersByName("cours_d_eau")[0]
buildings = project.mapLayersByName("Bâtiments (BD TOPO)")[0]

# Flood zone: 200m buffer
flood = processing.run("native:buffer", {
    "INPUT": rivers, "DISTANCE": 200, "OUTPUT": "memory:"
})

# Buildings in flood zone
exposed = processing.run("native:extractbylocation", {
    "INPUT": buildings,
    "PREDICATE": [0],  # intersects
    "INTERSECT": flood["OUTPUT"],
    "OUTPUT": "memory:"
})
result["exposed_buildings"] = exposed["OUTPUT"].featureCount()
project.addMapLayer(exposed["OUTPUT"])
```

## Recipes (Pre-Built Workflows)

Instead of building workflows from scratch, use recipes for common analyses:

```
list_recipes()
→ densite_bati, urbanisme_general, risque_inondation, occupation_sol

get_recipe(id="densite_bati", zone="Montpellier")
→ Step-by-step instructions using set_study_zone, smart_load, run_processing, etc.
```

Recipes handle the full pipeline: data loading → analysis → styling → layout → PDF export.
See `skill://recipes` for details.

## OpenStreetMap Data (Overpass)

For data not in BD TOPO (shops, schools, cycleways, etc.), use the Overpass helper:

```python
# In execute_python:
schools = helpers.overpass_query({"amenity": "school"})
cycleways = helpers.overpass_query("highway=cycleway", name="Pistes cyclables")
```

Auto-uses the study zone bbox. See `skill://helpers` for full docs.

## Common Pitfalls

1. **Forgot set_study_zone**: smart_load returns "No study zone set" error. Always define the zone first.
2. **Too many features**: Default limit is 10000. For large cities, consider a smaller bbox or reduce max_features.
3. **Wrong CRS in Processing**: Downloaded layers are in EPSG:2154. When using extent parameters in Processing, ensure they match.
4. **Cache confusion**: If data seems stale, check `cached: true` in the response. Delete `/data/cache/` to force fresh downloads.
5. **Layer naming**: smart_load uses the catalog display name (e.g. "Bâtiments (BD TOPO)"), not the typename. Use `project.mapLayersByName()` with the display name.
6. **Large downloads**: The default timeout is 300s. For very large areas with max_features=10000, the download may take up to 3 minutes. Consider reducing the area or feature count.
7. **Raster sources need no study zone**: WMS/WMTS/XYZ layers stream tiles on demand. Only WFS sources require set_study_zone.
