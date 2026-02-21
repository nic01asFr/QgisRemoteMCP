# French National Data Sources

## IGN Géoplateforme (data.geopf.fr)

### WFS — Vector data
Base URL: `https://data.geopf.fr/wfs/ows`

```python
# Pattern pour charger une couche WFS
uri = (
    "url='https://data.geopf.fr/wfs/ows' "
    "typename='{TYPENAME}' "
    "srsname='EPSG:4326' "
    "bbox='{ymin},{xmin},{ymax},{xmax}' "
    "pagingEnabled='true'"
)
layer = QgsVectorLayer(uri.format(...), "name", "WFS")
```

Key typenames (BD TOPO V3):
| Typename | Description |
|----------|-------------|
| `BDTOPO_V3:batiment` | Buildings |
| `BDTOPO_V3:troncon_de_route` | Road segments |
| `BDTOPO_V3:troncon_de_voie_ferree` | Railway |
| `BDTOPO_V3:surface_hydrographique` | Water surfaces |
| `BDTOPO_V3:cours_d_eau` | Rivers |
| `BDTOPO_V3:zone_de_vegetation` | Vegetation |
| `BDTOPO_V3:equipement_de_transport` | Transport equipment |
| `BDTOPO_V3:commune` | Municipalities |
| `BDTOPO_V3:arrondissement` | Districts |
| `BDTOPO_V3:lieu_dit_non_habite` | Named places |
| `BDTOPO_V3:point_de_repere` | Landmarks |

### WMS — Raster imagery
Base URL: `https://data.geopf.fr/wms-r`

```python
uri = "url=https://data.geopf.fr/wms-r&layers={LAYER}&format=image/jpeg&crs=EPSG:3857&styles="
layer = QgsRasterLayer(uri.format(...), "name", "wms")
```

Key layers:
| Layer | Description |
|-------|-------------|
| `ORTHOIMAGERY.ORTHOPHOTOS` | Latest orthophoto |
| `ORTHOIMAGERY.ORTHOPHOTOS.IRC` | Infrared orthophoto |
| `GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2` | Plan IGN v2 |
| `GEOGRAPHICALGRIDSYSTEMS.MAPS` | Scan 25 (topo maps) |
| `ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES` | MNT (DEM) |
| `CADASTRALPARCELS.PARCELLAIRE_EXPRESS` | Cadastral parcels |

### WMTS (tiled, faster)
Base URL: `https://data.geopf.fr/wmts`

```python
uri = (
    "url=https://data.geopf.fr/wmts"
    "&layers=ORTHOIMAGERY.ORTHOPHOTOS"
    "&format=image/jpeg"
    "&tilematrixset=PM"
    "&crs=EPSG:3857"
    "&styles=normal"
    "&type=xyz"
)
```

## BAN (Base Adresse Nationale)

Geocoding API: `https://api-adresse.data.gouv.fr`

```python
import urllib.request, json, urllib.parse

def geocode(query):
    """Geocode an address using BAN."""
    q = urllib.parse.quote(query)
    url = f"https://api-adresse.data.gouv.fr/search/?q={q}&limit=1"
    resp = urllib.request.urlopen(url)
    data = json.loads(resp.read())
    if data['features']:
        f = data['features'][0]
        return {
            'lon': f['geometry']['coordinates'][0],
            'lat': f['geometry']['coordinates'][1],
            'label': f['properties']['label'],
            'score': f['properties']['score'],
            'city': f['properties'].get('city', ''),
            'postcode': f['properties'].get('postcode', ''),
        }
    return None

def reverse_geocode(lon, lat):
    """Reverse geocode coordinates."""
    url = f"https://api-adresse.data.gouv.fr/reverse/?lon={lon}&lat={lat}"
    resp = urllib.request.urlopen(url)
    data = json.loads(resp.read())
    if data['features']:
        return data['features'][0]['properties']['label']
    return None
```

## Panoramax (street-level imagery)

API: `https://api.panoramax.xyz/api`

```python
import urllib.request, json

PANORAMAX = "https://api.panoramax.xyz/api"

def search_panoramax(bbox, limit=50):
    """Search for street-level images in a bounding box.
    bbox: [west, south, east, north] in EPSG:4326
    """
    url = (f"{PANORAMAX}/search?bbox={','.join(str(x) for x in bbox)}"
           f"&limit={limit}")
    resp = urllib.request.urlopen(url)
    return json.loads(resp.read())

def download_image(image_id, output_path):
    """Download a Panoramax image."""
    url = f"{PANORAMAX}/pictures/{image_id}/sd.jpg"
    urllib.request.urlretrieve(url, output_path)
    return output_path

# Example: search and create QGIS layer
bbox = [4.3, 43.8, 4.4, 43.9]  # around Nîmes
data = search_panoramax(bbox)

# Create GeoJSON from results
features = []
for item in data.get('features', []):
    features.append({
        "type": "Feature",
        "geometry": item['geometry'],
        "properties": {
            "id": item['id'],
            "date": item['properties'].get('datetime', ''),
            "provider": item['properties'].get('providers', [{}])[0].get('name', ''),
        }
    })

geojson = {"type": "FeatureCollection", "features": features}
with open("/tmp/panoramax.geojson", "w") as f:
    json.dump(geojson, f)

layer = QgsVectorLayer("/tmp/panoramax.geojson", "Photos Panoramax", "ogr")
project.addMapLayer(layer)
```

## Cartofriches (CEREMA)

API: `https://cartofriches.cerema.fr/api`

```python
def search_friches(bbox):
    """Search for brownfield sites."""
    url = (f"https://cartofriches.cerema.fr/api/v1/friches/"
           f"?bbox={','.join(str(x) for x in bbox)}")
    resp = urllib.request.urlopen(url)
    return json.loads(resp.read())
```

## OCS GE (Occupation du Sol Grande Échelle)

Available via WFS on Géoplateforme:
```python
uri = (
    "url='https://data.geopf.fr/wfs/ows' "
    "typename='OCSGE:occupation_du_sol' "
    "srsname='EPSG:4326' "
    f"bbox='{ymin},{xmin},{ymax},{xmax}'"
)
```

## DVF (Demandes de Valeurs Foncières)

Open API: `https://api.cquest.org/dvf`

```python
def search_dvf(code_commune, year=2023):
    """Search property transactions."""
    url = f"https://api.cquest.org/dvf?code_commune={code_commune}&annee_mutation={year}"
    resp = urllib.request.urlopen(url)
    return json.loads(resp.read())
```

## Environment & Protected Areas

### Corine Land Cover (WMS)
```python
helpers.load_catalog_source("corine_land_cover")
```
Layer: `LANDCOVER.CLC18_FR` — land use classification 2018.

### Natura 2000 / ZNIEFF
> **Unavailable** — Natura 2000 (SIC/ZPS) and ZNIEFF (Type 1/2) WFS sources were removed from the catalog. The typenames appear in Géoplateforme GetCapabilities but GetFeature returns "Feature type unknown" (INPN migration + cyberattaque 2025).

## Administrative Boundaries

### Admin Express (WFS)
```python
helpers.load_catalog_source("admin_express_communes", bbox=[4.3, 43.6, 4.5, 43.8])
helpers.load_catalog_source("admin_express_departements", bbox=[2.0, 43.0, 5.0, 45.0])
helpers.load_catalog_source("admin_express_regions")
```

### RPG — Agricultural Parcels (WFS)
```python
helpers.load_catalog_source("rpg", bbox=[4.3, 43.6, 4.5, 43.8])
```

## Additional BD TOPO layers

| Catalog ID | Typename | Description |
|------------|----------|-------------|
| `bdtopo_poi` | `BDTOPO_V3:construction_ponctuelle` | Points of interest |
| `bdtopo_lieu_dit` | `BDTOPO_V3:lieu_dit_non_habite` | Named places |
| `bdtopo_surface_activite` | `BDTOPO_V3:zone_d_activite_ou_d_interet` | Activity zones |
| `bdtopo_equipement_transport` | `BDTOPO_V3:equipement_de_transport` | Transport facilities |

## International Basemaps

```python
# Esri World Imagery (satellite)
helpers.load_catalog_source("esri_world_imagery")
# Esri World Topo
helpers.load_catalog_source("esri_world_topo")
# Stadia Stamen Terrain
helpers.load_catalog_source("stamen_terrain")
# IGN Scan 25 (topographic maps)
helpers.load_catalog_source("ign_scan25")
```

## APIs

### Geo API Communes
```python
commune = helpers.search_commune("Nimes")
# Returns: {nom, code, population, departement, region, bbox}
```

### DVF (Property Transactions)
API: `https://api.cquest.org/dvf`
```python
data = helpers.fetch_json("https://api.cquest.org/dvf", {"code_commune": "30189", "annee_mutation": "2023"})
```

## Typical workflow: load base data for a zone

### Using helpers (recommended)
```python
# 1. Geocode + zoom
loc = helpers.geocode("Gare de Nimes")
helpers.zoom_to(loc["bbox"])

# 2. Add orthophoto + buildings + roads
helpers.load_catalog_source("ign_ortho_wmts")
helpers.load_catalog_source("bdtopo_batiments", bbox=loc["bbox"])
helpers.load_catalog_source("bdtopo_routes", bbox=loc["bbox"])

result["center"] = [loc["lon"], loc["lat"]]
result["bbox"] = loc["bbox"]
```

### Manual approach (for custom URIs)
```python
import urllib.request, json, urllib.parse

# 1. Geocode
q = urllib.parse.quote("Gare de Nimes")
resp = urllib.request.urlopen(f"https://api-adresse.data.gouv.fr/search/?q={q}&limit=1")
data = json.loads(resp.read())
lon = data['features'][0]['geometry']['coordinates'][0]
lat = data['features'][0]['geometry']['coordinates'][1]

# 2. Compute bbox (500m around point)
buffer = 0.005  # ~500m
bbox = [lon - buffer, lat - buffer, lon + buffer, lat + buffer]

# 3. Add orthophoto
ortho_uri = "url=https://data.geopf.fr/wms-r&layers=ORTHOIMAGERY.ORTHOPHOTOS&format=image/jpeg&crs=EPSG:3857&styles="
project.addMapLayer(QgsRasterLayer(ortho_uri, "Orthophoto", "wms"))

# 4. Add buildings
bat_uri = f"url='https://data.geopf.fr/wfs/ows' typename='BDTOPO_V3:batiment' srsname='EPSG:4326' bbox='{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}' pagingEnabled='true'"
project.addMapLayer(QgsVectorLayer(bat_uri, "Batiments", "WFS"))

# 5. Add roads
route_uri = f"url='https://data.geopf.fr/wfs/ows' typename='BDTOPO_V3:troncon_de_route' srsname='EPSG:4326' bbox='{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}' pagingEnabled='true'"
project.addMapLayer(QgsVectorLayer(route_uri, "Routes", "WFS"))

# 6. Zoom
from qgis.core import QgsRectangle
canvas.setExtent(QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3]))
canvas.refresh()

result['center'] = [lon, lat]
result['bbox'] = bbox
result['layers'] = [l.name() for l in project.mapLayers().values()]
```
