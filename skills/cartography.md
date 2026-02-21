# Cartography & Styling Guide

## Symbology via execute_python

### Single symbol (simple)
```python
from qgis.core import QgsSymbol, QgsSingleSymbolRenderer, QgsFillSymbol

layer = project.mapLayer(layer_id)

# Polygon fill
symbol = QgsFillSymbol.createSimple({
    'color': '65,105,225,180',      # RGBA
    'outline_color': '0,0,139',
    'outline_width': '0.5',
})
layer.setRenderer(QgsSingleSymbolRenderer(symbol))
layer.triggerRepaint()
```

### Categorized (values → colors)
```python
from qgis.core import (QgsCategorizedSymbolRenderer, QgsRendererCategory,
                        QgsMarkerSymbol, QgsFillSymbol)

categories = []
mapping = {
    "bon":      ("#27ae60", "Bon état"),
    "moyen":    ("#f39c12", "État moyen"),
    "mauvais":  ("#e74c3c", "Mauvais état"),
    "inconnu":  ("#95a5a6", "Non évalué"),
}
for value, (color, label) in mapping.items():
    symbol = QgsMarkerSymbol.createSimple({'color': color, 'size': '3.5'})
    categories.append(QgsRendererCategory(value, symbol, label))

renderer = QgsCategorizedSymbolRenderer("etat", categories)
layer.setRenderer(renderer)
layer.triggerRepaint()
```

### Graduated (numeric ranges)
```python
from qgis.core import (QgsGraduatedSymbolRenderer, QgsRendererRange,
                        QgsFillSymbol, QgsGradientColorRamp)
from qgis.PyQt.QtGui import QColor

ranges = []
breaks = [(0, 50, "#2ecc71", "0-50"),
          (50, 100, "#f1c40f", "50-100"),
          (100, 500, "#e67e22", "100-500"),
          (500, 10000, "#e74c3c", "500+")]

for low, high, color, label in breaks:
    symbol = QgsFillSymbol.createSimple({'color': color, 'outline_color': '#333', 'outline_width': '0.3'})
    ranges.append(QgsRendererRange(low, high, symbol, label))

renderer = QgsGraduatedSymbolRenderer("population", ranges)
layer.setRenderer(renderer)
layer.triggerRepaint()
```

### Rule-based (expressions)
```python
from qgis.core import QgsRuleBasedRenderer, QgsFillSymbol

root_rule = QgsRuleBasedRenderer.Rule(None)

rules = [
    ('"surface" > 1000 AND "type" = \'commercial\'', "#3498db", "Grand commercial"),
    ('"surface" > 1000 AND "type" = \'residential\'', "#2ecc71", "Grand résidentiel"),
    ('"surface" <= 1000', "#bdc3c7", "Petit"),
]

for expr, color, label in rules:
    symbol = QgsFillSymbol.createSimple({'color': color, 'outline_color': '#555'})
    rule = QgsRuleBasedRenderer.Rule(symbol)
    rule.setFilterExpression(expr)
    rule.setLabel(label)
    root_rule.appendChild(rule)

renderer = QgsRuleBasedRenderer(root_rule)
layer.setRenderer(renderer)
layer.triggerRepaint()
```

## Labels

```python
from qgis.core import QgsPalLayerSettings, QgsVectorLayerSimpleLabeling, QgsTextFormat
from qgis.PyQt.QtGui import QFont, QColor

settings = QgsPalLayerSettings()
settings.fieldName = "name"
settings.enabled = True

text_format = QgsTextFormat()
text_format.setFont(QFont("Liberation Sans", 9))
text_format.setColor(QColor(30, 30, 30))

# Halo (buffer around text for readability)
buffer = text_format.buffer()
buffer.setEnabled(True)
buffer.setSize(1.5)
buffer.setColor(QColor(255, 255, 255))
text_format.setBuffer(buffer)

settings.setFormat(text_format)
labeling = QgsVectorLayerSimpleLabeling(settings)
layer.setLabeling(labeling)
layer.setLabelsEnabled(True)
layer.triggerRepaint()
```

## Print Layouts

### Using layout templates (recommended)

Pre-configured templates with dynamic labels. Fastest way to get a
professional layout:

```
# Via MCP tools:
apply_layout_template(template="a3_landscape", variables={"title": "Densité bâti — Montpellier", "subtitle": "Grille 500m"})
export_pdf(layout="Export A3 Landscape")
```

Available templates:
- `a3_landscape` — A3 paysage (420x297mm): carte, titre, légende, échelle, flèche nord, sources
- `a4_portrait` — A4 portrait (210x297mm): carte, titre, légende en bas

Dynamic labels use QGIS expressions:
- `[% @title %]` — from `variables.title`
- `[% @subtitle %]` — from `variables.subtitle`
- `[% @study_zone_name %]` — auto-set by `set_study_zone`
- `[% format_date(now(), 'dd/MM/yyyy') %]` — current date

### Create a layout programmatically

For custom layouts beyond templates:

```python
from qgis.core import (QgsLayoutItemMap, QgsLayoutItemLabel, QgsLayoutItemLegend,
                        QgsLayoutItemScaleBar, QgsPrintLayout, QgsLayoutPoint,
                        QgsLayoutSize, QgsUnitTypes)

layout = QgsPrintLayout(project)
layout.initializeDefaults()
layout.setName("Export A3")

# Page size A3 landscape
page = layout.pageCollection().page(0)
page.setPageSize("A3", QgsLayoutItemPage.Landscape)

# Map item
map_item = QgsLayoutItemMap(layout)
map_item.attemptMove(QgsLayoutPoint(10, 10, QgsUnitTypes.LayoutMillimeters))
map_item.attemptResize(QgsLayoutSize(380, 260, QgsUnitTypes.LayoutMillimeters))
map_item.setExtent(canvas.extent())
layout.addLayoutItem(map_item)

# Title
title = QgsLayoutItemLabel(layout)
title.setText("Analyse territoriale — Nîmes")
title.setFont(QFont("Liberation Sans", 18))
title.attemptMove(QgsLayoutPoint(10, 275, QgsUnitTypes.LayoutMillimeters))
layout.addLayoutItem(title)

# Legend
legend = QgsLayoutItemLegend(layout)
legend.setLinkedMap(map_item)
legend.attemptMove(QgsLayoutPoint(300, 10, QgsUnitTypes.LayoutMillimeters))
layout.addLayoutItem(legend)

# Scale bar
scalebar = QgsLayoutItemScaleBar(layout)
scalebar.setLinkedMap(map_item)
scalebar.attemptMove(QgsLayoutPoint(10, 260, QgsUnitTypes.LayoutMillimeters))
layout.addLayoutItem(scalebar)

# Register layout
project.layoutManager().addLayout(layout)
result['layout'] = layout.name()
```

## Color palettes (CEREMA-friendly)

```python
# Professional palettes
CEREMA_BLUE = "#005B96"
CEREMA_GREEN = "#00A651"
CEREMA_ORANGE = "#F7941D"
CEREMA_RED = "#ED1C24"
CEREMA_GREY = "#58585A"

# Sequential (light to dark)
SEQ_GREEN = ["#edf8e9", "#bae4b3", "#74c476", "#31a354", "#006d2c"]
SEQ_BLUE  = ["#eff3ff", "#bdd7e7", "#6baed6", "#3182bd", "#08519c"]
SEQ_RED   = ["#fee5d9", "#fcae91", "#fb6a4a", "#de2d26", "#a50f15"]

# Diverging (good-neutral-bad)
DIV_RYG = ["#d73027", "#fc8d59", "#fee08b", "#d9ef8b", "#91cf60", "#1a9850"]
```

## Web Map Export

Export visible vector layers as an interactive Leaflet HTML page:

```
# Via MCP tool:
export_web_map(title="Analyse urbaine — Montpellier")
# → {"path": "/data/webmap_1234.html", "download_url": "http://localhost:8080/api/files/webmap_1234.html", "layers_exported": 4}
```

The exported HTML is standalone (no server required):
- Leaflet 1.9 via CDN
- GeoJSON data inline (per layer, up to 5000 features each)
- OSM basemap
- Popups with feature attributes
- Interactive legend with layer toggle
- Colors extracted from QGIS renderer

Useful for sharing analysis results via email or embedding in reports.
