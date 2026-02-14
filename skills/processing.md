# QGIS Processing Algorithms Reference

## How to use Processing

```python
# From execute_python
result_dict = processing.run("algorithm_id", {parameters})
```

Output layers are automatically added to the project when using `"OUTPUT": "memory:"`.

## Most useful algorithms

### Geometry operations
| Algorithm | Description | Key params |
|-----------|-------------|------------|
| `native:buffer` | Buffer around features | INPUT, DISTANCE, SEGMENTS, OUTPUT |
| `native:intersection` | Geometric intersection | INPUT, OVERLAY, OUTPUT |
| `native:union` | Merge geometries | INPUT, OVERLAY, OUTPUT |
| `native:difference` | Subtract geometries | INPUT, OVERLAY, OUTPUT |
| `native:dissolve` | Dissolve by attribute | INPUT, FIELD, OUTPUT |
| `native:centroids` | Feature centroids | INPUT, OUTPUT |
| `native:convexhull` | Convex hull | INPUT, OUTPUT |
| `native:voronoipolygons` | Voronoi polygons | INPUT, OUTPUT |
| `native:delaunaytriangulation` | Delaunay triangulation | INPUT, OUTPUT |
| `native:simplifygeometries` | Simplify | INPUT, TOLERANCE, OUTPUT |
| `native:clip` | Clip by mask | INPUT, OVERLAY, OUTPUT |

### Analysis
| Algorithm | Description | Key params |
|-----------|-------------|------------|
| `native:countpointsinpolygon` | Count points in polygons | POLYGONS, POINTS, OUTPUT |
| `native:joinattributesbylocation` | Spatial join | INPUT, JOIN, PREDICATE, OUTPUT |
| `native:nearestneighbouranalysis` | Nearest neighbour | INPUT, OUTPUT_HTML_FILE |
| `native:distancematrix` | Distance matrix | INPUT, INPUT_FIELD, TARGET, OUTPUT |
| `qgis:statisticsbycategories` | Stats by category | INPUT, VALUES_FIELD_NAME, CATEGORIES_FIELD_NAME |
| `native:zonalstatisticsfb` | Zonal stats (raster→vector) | INPUT, INPUT_RASTER, OUTPUT |

### Data management
| Algorithm | Description | Key params |
|-----------|-------------|------------|
| `native:reprojectlayer` | Reproject CRS | INPUT, TARGET_CRS, OUTPUT |
| `native:mergevectorlayers` | Merge layers | LAYERS, OUTPUT |
| `native:extractbyexpression` | Filter features | INPUT, EXPRESSION, OUTPUT |
| `native:addautoincrementalfield` | Add ID field | INPUT, FIELD_NAME, OUTPUT |
| `native:fieldcalculator` | Calculate field | INPUT, FIELD_NAME, FORMULA, OUTPUT |
| `native:saveselectedfeatures` | Export selection | INPUT, OUTPUT |
| `native:creategrid` | Create grid | TYPE, EXTENT, HSPACING, VSPACING, OUTPUT |

### GDAL (raster)
| Algorithm | Description | Key params |
|-----------|-------------|------------|
| `gdal:cliprasterbyextent` | Clip raster by bbox | INPUT, EXTENT, OUTPUT |
| `gdal:cliprasterbymasklayer` | Clip raster by mask | INPUT, MASK, OUTPUT |
| `gdal:contour` | Contour lines | INPUT, INTERVAL, OUTPUT |
| `gdal:hillshade` | Hillshade | INPUT, OUTPUT |
| `gdal:slope` | Slope | INPUT, OUTPUT |
| `gdal:warpreproject` | Reproject raster | INPUT, TARGET_CRS, OUTPUT |
| `gdal:merge` | Merge rasters | INPUT, OUTPUT |

## Parameter patterns

### Layer reference
```python
# By layer ID
{"INPUT": "layer_id_from_project"}

# By file path
{"INPUT": "/data/my_file.geojson"}

# Memory output (auto-added to project)
{"OUTPUT": "memory:"}

# File output
{"OUTPUT": "/data/result.gpkg"}
```

### Expressions in parameters
```python
# Field calculator
processing.run("native:fieldcalculator", {
    "INPUT": layer_id,
    "FIELD_NAME": "area_ha",
    "FIELD_TYPE": 0,  # 0=float, 1=int, 2=string
    "FORMULA": "$area / 10000",
    "OUTPUT": "memory:"
})
```

### Chaining algorithms
```python
# Buffer then dissolve
buf = processing.run("native:buffer", {
    "INPUT": layer_id, "DISTANCE": 100, "OUTPUT": "memory:"
})

dissolved = processing.run("native:dissolve", {
    "INPUT": buf["OUTPUT"], "OUTPUT": "memory:"
})

project.addMapLayer(dissolved["OUTPUT"])
result['layer_id'] = dissolved["OUTPUT"].id()
```

## Search for algorithms

Use the `search_algorithms` tool:
```
search_algorithms(search="buffer")
search_algorithms(provider="native", search="join")
search_algorithms(provider="gdal")
```
