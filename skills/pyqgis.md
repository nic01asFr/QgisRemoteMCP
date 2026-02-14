# PyQGIS Scripting Reference

## Context

When you use `execute_python`, your code runs inside a live QGIS instance.
You have full access to all PyQGIS APIs and the current project.

## Pre-loaded variables

```python
project     # QgsProject.instance()
canvas      # iface.mapCanvas()
iface       # QGIS interface
processing  # Processing framework
result      # Dict — store values here to return them
```

## Common Patterns

### Load a GeoJSON file
```python
layer = QgsVectorLayer("/data/points.geojson", "My Points", "ogr")
project.addMapLayer(layer)
result['layer_id'] = layer.id()
result['features'] = layer.featureCount()
```

### Load a GeoJSON from a string
```python
import tempfile, json

geojson = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [4.35, 43.84]},
         "properties": {"name": "Nîmes"}}
    ]
}

path = "/tmp/temp_layer.geojson"
with open(path, "w") as f:
    json.dump(geojson, f)

layer = QgsVectorLayer(path, "My Points", "ogr")
project.addMapLayer(layer)
result['layer_id'] = layer.id()
```

### Add a WFS layer (IGN Géoplateforme)
```python
uri = (
    "url='https://data.geopf.fr/wfs/ows' "
    "typename='BDTOPO_V3:batiment' "
    "srsname='EPSG:4326' "
    "bbox='43.8,4.3,43.9,4.4' "
    "pagingEnabled='true'"
)
layer = QgsVectorLayer(uri, "Bâtiments BD TOPO", "WFS")
project.addMapLayer(layer)
result['count'] = layer.featureCount()
```

### Add a WMS layer (IGN orthophoto)
```python
uri = (
    "url=https://data.geopf.fr/wms-r"
    "&layers=ORTHOIMAGERY.ORTHOPHOTOS"
    "&format=image/jpeg"
    "&crs=EPSG:3857"
    "&styles="
)
layer = QgsRasterLayer(uri, "Orthophoto IGN", "wms")
project.addMapLayer(layer)
```

### Run a Processing algorithm
```python
params = {
    'INPUT': layer_id,
    'DISTANCE': 50,
    'OUTPUT': 'memory:'
}
res = processing.run("native:buffer", params)
buffer_layer = res['OUTPUT']
project.addMapLayer(buffer_layer)
result['buffer_id'] = buffer_layer.id()
```

### Filter features
```python
layer = project.mapLayer("some_layer_id")
request = QgsFeatureRequest().setFilterExpression("type = 'residential'")
request.setLimit(10)
features = []
for f in layer.getFeatures(request):
    features.append({
        "id": f.id(),
        "name": f["name"],
        "area": f.geometry().area()
    })
result['features'] = features
```

### Set layer style (categorized)
```python
from qgis.core import (QgsCategorizedSymbolRenderer, QgsRendererCategory,
                        QgsSymbol, QgsMarkerSymbol)

layer = project.mapLayer(layer_id)
categories = []
for value, color, label in [
    ("good", "#2ecc71", "Bon état"),
    ("degraded", "#f39c12", "Dégradé"),
    ("missing", "#e74c3c", "Absent"),
]:
    symbol = QgsMarkerSymbol.createSimple({'color': color, 'size': '4'})
    categories.append(QgsRendererCategory(value, symbol, label))

renderer = QgsCategorizedSymbolRenderer("status", categories)
layer.setRenderer(renderer)
layer.triggerRepaint()
```

### Call an external HTTP service from QGIS
```python
import urllib.request, json

# Example: geocode with BAN API
url = "https://api-adresse.data.gouv.fr/search/?q=gare+de+nimes&limit=1"
resp = urllib.request.urlopen(url)
data = json.loads(resp.read())
coords = data['features'][0]['geometry']['coordinates']
result['coordinates'] = coords  # [lon, lat]
```

### Zoom to coordinates
```python
from qgis.core import QgsRectangle
lon, lat = 4.35, 43.84
buffer = 0.01  # ~1km
canvas.setExtent(QgsRectangle(lon - buffer, lat - buffer, lon + buffer, lat + buffer))
canvas.refresh()
```

### Create a new memory layer
```python
layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string&field=score:double",
                       "Detections", "memory")
provider = layer.dataProvider()
features = []
for det in detections:
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(det['lon'], det['lat'])))
    f.setAttributes([det['name'], det['score']])
    features.append(f)
provider.addFeatures(features)
layer.updateExtents()
project.addMapLayer(layer)
result['layer_id'] = layer.id()
```

## Important notes

- Always store return values in `result` dict
- Use `print()` for debug output (captured in `stdout`)
- `processing.run()` is available for all QGIS Processing algorithms
- External HTTP calls work (the container has network access)
- Files in `/data/` are shared with the host
- Projects in `/projects/` persist across restarts
