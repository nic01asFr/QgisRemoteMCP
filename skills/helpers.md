# Python Helpers Reference

The `helpers` module is available in `execute_python` scripts. It provides
ready-made functions for common GIS operations.

## Quick Reference

| Function | Description |
|----------|-------------|
| `helpers.geocode(address)` | Geocode French address (BAN API) |
| `helpers.reverse_geocode(lon, lat)` | Reverse geocode coordinates |
| `helpers.search_commune(name)` | Search commune info (Geo API) |
| `helpers.get_elevation(lon, lat)` | Get altitude (IGN Altimetrie) |
| `helpers.fetch_json(url, params)` | HTTP GET returning JSON |
| `helpers.add_wfs(url, typename, bbox)` | Add WFS layer |
| `helpers.add_wms(url, layers, name)` | Add WMS layer |
| `helpers.add_wmts(url, layers, name)` | Add WMTS tiled layer |
| `helpers.add_xyz(url, name)` | Add XYZ tile layer |
| `helpers.add_postgis(host, db, table)` | Add PostGIS layer |
| `helpers.add_spatialite(path, table)` | Add SpatiaLite layer |
| `helpers.create_point_layer(name, pts)` | Memory layer from list of dicts |
| `helpers.bbox_from_canvas()` | Get canvas extent in EPSG:4326 |
| `helpers.zoom_to(extent_or_address)` | Zoom (bbox, point dict, or address) |
| `helpers.load_catalog_source(id, bbox)` | Load from datasources.json by ID |
| `helpers.overpass_query(tags, bbox)` | Query OpenStreetMap via Overpass API |

## Common Patterns

### Geocode and zoom to a place
```python
loc = helpers.geocode("Cathedrale de Nimes")
helpers.zoom_to(loc["bbox"])
result["location"] = loc
```

### Load BD TOPO buildings around a place
```python
loc = helpers.geocode("Place Bellecour, Lyon")
layer = helpers.add_wfs(
    "https://data.geopf.fr/wfs/ows",
    "BDTOPO_V3:batiment",
    bbox=loc["bbox"],
    name="Batiments"
)
result["buildings"] = layer
```

### Add basemap + WFS data combo
```python
helpers.add_xyz("https://tile.openstreetmap.org/{z}/{x}/{y}.png", "OSM")
helpers.zoom_to("Montpellier")
batiments = helpers.add_wfs(
    "https://data.geopf.fr/wfs/ows",
    "BDTOPO_V3:batiment",
    name="Batiments"
)
result["batiments"] = batiments
```

### Create a layer from geocoded addresses
```python
addresses = ["Mairie de Paris", "Mairie de Lyon", "Mairie de Marseille"]
points = []
for addr in addresses:
    loc = helpers.geocode(addr)
    if "error" not in loc:
        points.append({"lon": loc["lon"], "lat": loc["lat"],
                       "name": loc["label"], "score": loc["score"]})
layer = helpers.create_point_layer("Mairies", points)
result["mairies"] = layer
```

### Load from catalog by ID
```python
helpers.load_catalog_source("osm_xyz")
helpers.zoom_to("Bordeaux")
communes = helpers.load_catalog_source("admin_express_communes")
result["communes"] = communes
```

### Connect to PostGIS
```python
layer = helpers.add_postgis(
    host="my-postgres-server",
    dbname="gis_data",
    table="parcels",
    geom_col="geom",
    user="postgres",
    password="secret",
    schema="public"
)
result["parcels"] = layer
```

### Get elevation profile
```python
points = [(2.35, 48.85), (2.36, 48.86), (2.37, 48.87)]
profile = []
for lon, lat in points:
    elev = helpers.get_elevation(lon, lat)
    if "error" not in elev:
        profile.append(elev)
result["profile"] = profile
```

### Query OpenStreetMap (Overpass API)
```python
# By tag dict (amenity=school within study zone)
schools = helpers.overpass_query({"amenity": "school"})
result["schools"] = schools

# By string key=value
supermarkets = helpers.overpass_query("shop=supermarket", name="Supermarchés")
result["supermarkets"] = supermarkets

# Multiple tags
pharmacies = helpers.overpass_query({"amenity": "pharmacy"})

# With explicit bbox (default: uses study zone)
cycleways = helpers.overpass_query(
    {"highway": "cycleway"},
    bbox_4326=[3.8, 43.5, 4.0, 43.7],
    name="Pistes cyclables"
)

# Common OSM tags:
# amenity: school, hospital, pharmacy, restaurant, cafe, parking
# shop: supermarket, bakery, convenience
# highway: cycleway, footway, primary, secondary
# building: yes, residential, commercial
# leisure: park, playground, sports_centre
# tourism: hotel, museum, viewpoint
```

Returns: `{"layer_id", "name", "feature_count", "osm_elements", "path"}`

Overpass downloads data from OpenStreetMap. Auto-uses the study zone bbox
if not provided. Data is saved as GeoJSON in `/data/cache/`.

## Error Handling

All helpers return `{"error": "..."}` on failure instead of raising exceptions.
Check for errors:

```python
loc = helpers.geocode("adresse inconnue xyz")
if "error" in loc:
    result["error"] = loc["error"]
else:
    helpers.zoom_to(loc["bbox"])
```

Network requests timeout after 15 seconds. The BAN API and Geo API are
generally reliable for French addresses. Overpass API has a 60s timeout.

## Notes

- All layer-adding helpers auto-adapt the project CRS (first layer sets CRS)
- WFS helpers auto-derive bbox from canvas if not provided
- `zoom_to()` accepts a bbox list, a point dict, or an address string
- `create_point_layer()` auto-detects field types from the first point
- `overpass_query()` auto-uses the study zone bbox if not provided
- Helpers are available only in `execute_python`, not in other tools
