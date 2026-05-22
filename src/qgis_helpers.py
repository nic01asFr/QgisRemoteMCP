"""
QgisRemoteMCP -- Python Helpers for execute_python
================================================

Ready-made utility functions injected into execute_python's exec_globals
as `helpers`. These run inside the QGIS Python context.

Usage in execute_python:
    result["address"] = helpers.geocode("Mairie de Paris")
    result["layer"] = helpers.add_wfs(
        "https://data.geopf.fr/wfs/ows",
        "BDTOPO_V3:batiment",
        bbox=[2.3, 48.8, 2.4, 48.9]
    )
"""

import json
import os
import subprocess
import hashlib
import time
import math
import urllib.request
import urllib.parse
import urllib.error
import traceback
from pathlib import Path

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsRectangle, QgsPointXY, QgsFeature, QgsGeometry,
    QgsField, QgsFields, QgsExpressionContextUtils,
)
from qgis.PyQt.QtCore import QVariant
import qgis.utils

# ── Configuration ─────────────────────────────────────────────

HTTP_TIMEOUT = 15  # seconds for network requests


# ── Internal utilities ────────────────────────────────────────

def _iface():
    return qgis.utils.iface


def _project():
    return QgsProject.instance()


def _canvas():
    iface = _iface()
    return iface.mapCanvas() if iface else None


def _finalize_layer(layer):
    """Add layer to project with auto CRS adapt, zoom on first layer.
    Replicates QGISBridge._finalize_add_layer pattern."""
    project = _project()
    is_first = len(project.mapLayers()) == 0

    # Auto-adapt project CRS — only when project is truly empty
    layer_crs = layer.crs()
    if layer_crs.isValid() and len(project.mapLayers()) == 0:
        project.setCrs(layer_crs)

    project.addMapLayer(layer)

    # Auto-zoom on first layer (no refresh — caller controls rendering)
    zoomed = False
    canvas = _canvas()
    if is_first and canvas and layer.extent() and not layer.extent().isEmpty():
        canvas.setExtent(layer.extent())
        zoomed = True
    # NOTE: canvas.refresh() intentionally NOT called here.
    # Each layer add was triggering a full re-render including
    # WMS GetCapabilities requests, causing crashes with 5+ layers.
    # The canvas refreshes automatically when the event loop resumes.

    # Build response
    result = {
        "layer_id": layer.id(),
        "name": layer.name(),
        "crs": layer.crs().authid(),
        "project_crs": project.crs().authid(),
        "zoomed": zoomed,
    }
    try:
        ext = layer.extent()
        if not ext.isEmpty():
            result["extent"] = [ext.xMinimum(), ext.yMinimum(),
                                ext.xMaximum(), ext.yMaximum()]
    except Exception:
        pass
    if isinstance(layer, QgsVectorLayer):
        result["feature_count"] = layer.featureCount()
        result["geometry_type"] = (layer.geometryType().name
                                   if hasattr(layer.geometryType(), 'name')
                                   else str(layer.geometryType()))
        result["fields"] = [f.name() for f in layer.fields()]
    elif isinstance(layer, QgsRasterLayer):
        result["width"] = layer.width()
        result["height"] = layer.height()
        result["band_count"] = layer.bandCount()
    return result


# ══════════════════════════════════════════════════════════════
# PUBLIC HELPERS
# ══════════════════════════════════════════════════════════════

# ── Network ───────────────────────────────────────────────────

def fetch_json(url, params=None, timeout=HTTP_TIMEOUT):
    """HTTP GET returning parsed JSON. Returns {"error": "..."} on failure.

    Args:
        url: Base URL
        params: Optional dict of query parameters
        timeout: Request timeout in seconds (default 15)
    """
    try:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "QgisRemoteMCP/1.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        return {"error": f"Network error: {e}"}
    except Exception as e:
        return {"error": f"fetch_json failed: {e}"}


# ── Geocoding ─────────────────────────────────────────────────

def geocode(address, limit=1):
    """Geocode an address. Tries BAN (French API) first, then Nominatim (OSM) as fallback.

    Returns:
        {"lon", "lat", "label", "score", "city", "postcode", "bbox"}
        or {"error": "..."}.

    Example:
        loc = helpers.geocode("Gare de Lyon, Paris")
    """
    # Try BAN (French national geocoder) with short timeout
    data = fetch_json("https://api-adresse.data.gouv.fr/search/",
                      {"q": address, "limit": limit}, timeout=5)
    if "error" not in data:
        features = data.get("features", [])
        if features:
            f = features[0]
            coords = f["geometry"]["coordinates"]
            props = f["properties"]
            buf = 0.005  # ~500m
            return {
                "lon": coords[0],
                "lat": coords[1],
                "label": props.get("label", ""),
                "score": props.get("score", 0),
                "city": props.get("city", ""),
                "postcode": props.get("postcode", ""),
                "bbox": [coords[0] - buf, coords[1] - buf,
                         coords[0] + buf, coords[1] + buf],
            }

    # Fallback: Nominatim (OpenStreetMap) — globally accessible
    nom = fetch_json("https://nominatim.openstreetmap.org/search",
                     {"q": address, "format": "json", "limit": 1,
                      "addressdetails": 1}, timeout=10)
    if "error" not in nom and isinstance(nom, list) and nom:
        r = nom[0]
        lon, lat = float(r["lon"]), float(r["lat"])
        bb = r.get("boundingbox", [])  # [south, north, west, east]
        if len(bb) == 4:
            bbox = [float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])]
        else:
            buf = 0.005
            bbox = [lon - buf, lat - buf, lon + buf, lat + buf]
        addr = r.get("address", {})
        return {
            "lon": lon,
            "lat": lat,
            "label": r.get("display_name", address),
            "score": 0.8,
            "city": addr.get("city", addr.get("town", addr.get("village", ""))),
            "postcode": addr.get("postcode", ""),
            "bbox": bbox,
        }

    return {"error": f"Geocoding failed for '{address}' (BAN and Nominatim unavailable)"}


def reverse_geocode(lon, lat):
    """Reverse geocode coordinates using BAN API.

    Returns:
        {"label", "city", "postcode", "street", ...} or {"error": "..."}.
    """
    data = fetch_json("https://api-adresse.data.gouv.fr/reverse/",
                      {"lon": lon, "lat": lat})
    if "error" in data:
        return data
    features = data.get("features", [])
    if not features:
        return {"error": f"No address found at ({lon}, {lat})"}
    props = features[0]["properties"]
    return {
        "label": props.get("label", ""),
        "city": props.get("city", ""),
        "postcode": props.get("postcode", ""),
        "housenumber": props.get("housenumber", ""),
        "street": props.get("street", ""),
        "context": props.get("context", ""),
    }


def _try_resolve_arrondissement(name: str) -> str | None:
    """Détecte les arrondissements municipaux (Paris/Marseille/Lyon) et
    retourne le code INSEE direct (5 chiffres). `geo.api.gouv.fr/communes?nom=`
    n'indexe pas ces communes ; il faut utiliser `/communes/{insee}` direct.

    Patterns reconnus :
      - "Marseille 4e", "Marseille 4ème arrondissement", "Marseille 04"
      - "Paris 11e", "Paris 11ème arrondissement"
      - "Lyon 3e", "Lyon 3ème"
      - "13004" (CP Marseille → 13204), "75011" (CP Paris → 75111), "69003" (CP Lyon → 69383)

    Codes INSEE : Marseille 13201-16, Paris 75101-20, Lyon 69381-89.
    """
    import re as _re
    s = (name or "").strip().lower()

    # 1) Code postal pur 5 chiffres → arrondissement si CP correspondant
    m = _re.match(r"^(\d{5})$", s)
    if m:
        cp = m.group(1)
        if cp.startswith("130") and 1 <= int(cp[3:5]) <= 16:  # 13001-13016
            return f"132{int(cp[3:5]):02d}"
        if cp.startswith("750") and 1 <= int(cp[3:5]) <= 20:  # 75001-75020
            return f"751{int(cp[3:5]):02d}"
        if cp.startswith("690") and 1 <= int(cp[3:5]) <= 9:   # 69001-69009
            return f"6938{int(cp[3:5])}"
        return None

    # 2) "Marseille|Paris|Lyon Ne arrondissement"
    m = _re.search(
        r"\b(marseille|paris|lyon)\b.*?\b(\d{1,2})\s*(?:e|er|ème|eme|ieme)\b",
        s,
    )
    if m:
        city, n_str = m.group(1), m.group(2)
        n = int(n_str)
        if city == "marseille" and 1 <= n <= 16:
            return f"132{n:02d}"
        if city == "paris" and 1 <= n <= 20:
            return f"751{n:02d}"
        if city == "lyon" and 1 <= n <= 9:
            return f"6938{n}"

    # 3) "CP + ville" : "13004 Marseille", "75011 paris"
    m = _re.search(r"\b(\d{5})\b.*?\b(marseille|paris|lyon)\b", s)
    if m:
        cp, city = m.group(1), m.group(2)
        if city == "marseille" and cp.startswith("130") and 1 <= int(cp[3:5]) <= 16:
            return f"132{int(cp[3:5]):02d}"
        if city == "paris" and cp.startswith("750") and 1 <= int(cp[3:5]) <= 20:
            return f"751{int(cp[3:5]):02d}"
        if city == "lyon" and cp.startswith("690") and 1 <= int(cp[3:5]) <= 9:
            return f"6938{int(cp[3:5])}"

    return None


def _commune_by_insee(insee: str) -> dict:
    """Lookup direct d'une commune par code INSEE (5 chiffres), via
    `geo.api.gouv.fr/communes/{insee}`. Cet endpoint résout aussi les
    arrondissements municipaux que `?nom=` n'indexe pas."""
    data = fetch_json(
        f"https://geo.api.gouv.fr/communes/{insee}",
        {"fields": "nom,code,codesPostaux,population,centre,contour"},
        timeout=5,
    )
    if "error" in data:
        return data
    if not isinstance(data, dict) or "code" not in data:
        return {"error": f"Commune INSEE {insee} introuvable"}
    return _commune_to_result(data)


def _commune_to_result(c: dict) -> dict:
    """Transforme une réponse /communes en dict normalisé avec lon/lat/bbox."""
    result = {
        "nom": c.get("nom", ""),
        "code": c.get("code", ""),
        "codesPostaux": c.get("codesPostaux", []),
        "population": c.get("population", 0),
    }
    centre = (c.get("centre") or {}).get("coordinates") or []
    if centre:
        result["lon"] = centre[0]
        result["lat"] = centre[1]
    contour = c.get("contour") or {}
    geo_type = contour.get("type", "")
    raw_coords = contour.get("coordinates") or []
    all_points = []
    if geo_type == "MultiPolygon":
        for polygon in raw_coords:
            for ring in polygon:
                all_points.extend(ring)
    elif geo_type == "Polygon":
        for ring in raw_coords:
            all_points.extend(ring)
    elif raw_coords and raw_coords[0]:
        for ring in raw_coords:
            if isinstance(ring, list) and ring and isinstance(ring[0], (int, float)):
                all_points.append(ring)
            elif isinstance(ring, list):
                for pt in ring:
                    if isinstance(pt, list) and len(pt) >= 2 and isinstance(pt[0], (int, float)):
                        all_points.append(pt)
    if all_points:
        lons = [p[0] for p in all_points]
        lats = [p[1] for p in all_points]
        result["bbox"] = [min(lons), min(lats), max(lons), max(lats)]
    return result


def search_commune(name):
    """Search for a French commune using the Geo API.

    Stratégie en 2 niveaux pour gérer correctement les arrondissements
    municipaux Paris/Marseille/Lyon (que `?nom=` n'indexe pas) :

    1. Détection regex → INSEE direct (ex: "Marseille 4e" → 13204)
    2. Fallback `?nom=...&limit=1` pour les communes "normales"

    Returns:
        {"nom", "code", "codesPostaux", "population", "lon", "lat", "bbox"}
        or {"error": "..."}.

    Example:
        commune = helpers.search_commune("Nimes")
        commune = helpers.search_commune("Marseille 4e arrondissement")
        commune = helpers.search_commune("13004 Marseille")
    """
    # 1) Détection arrondissement municipal → lookup direct par INSEE
    insee = _try_resolve_arrondissement(name)
    if insee:
        r = _commune_by_insee(insee)
        if "error" not in r:
            return r
        # Si lookup INSEE échoue, on tente quand même le fallback nom

    # 2) Fallback : recherche par nom (cas commun)
    data = fetch_json("https://geo.api.gouv.fr/communes",
                      {"nom": name, "fields": "nom,code,codesPostaux,"
                       "population,centre,contour", "limit": 1}, timeout=5)
    if "error" in data:
        return data
    if not data or not isinstance(data, list) or len(data) == 0:
        return {"error": f"Commune not found: {name}"}
    return _commune_to_result(data[0])


def get_elevation(lon, lat):
    """Get elevation at a point using the IGN Altimetrie API.

    Returns:
        {"elevation": float, "lon": float, "lat": float} or {"error": "..."}.
    """
    data = fetch_json(
        "https://data.geopf.fr/altimetrie/1.0/calcul/alti/rest/elevation.json",
        {"lon": str(lon), "lat": str(lat), "zonly": "false"})
    if "error" in data:
        return data
    elevations = data.get("elevations", [])
    if not elevations:
        return {"error": "No elevation data returned"}
    return {"elevation": elevations[0].get("z"), "lon": lon, "lat": lat}


# ── Layer helpers ─────────────────────────────────────────────

def add_wfs(url, typename, bbox=None, name=None, srsname="EPSG:4326",
            sql_filter=None, max_features=None):
    """Add a WFS layer. Auto-derives bbox from canvas if not provided.

    Args:
        url: WFS server URL
        typename: e.g. "BDTOPO_V3:batiment"
        bbox: [xmin, ymin, xmax, ymax] in EPSG:4326
        name: Display name (default: short typename)
        srsname: SRS (default EPSG:4326)
        sql_filter: OGC filter expression, e.g. "nature = 'Religieux'"
        max_features: Limit number of features returned

    Examples:
        # Basic with bbox
        layer = helpers.add_wfs("https://data.geopf.fr/wfs/ows",
                                "BDTOPO_V3:batiment",
                                bbox=[2.3, 48.8, 2.4, 48.9])
        # With attribute filter
        layer = helpers.add_wfs("https://data.geopf.fr/wfs/ows",
                                "BDTOPO_V3:batiment",
                                bbox=[2.3, 48.8, 2.4, 48.9],
                                sql_filter="nature = 'Religieux'")
        # With max features
        layer = helpers.add_wfs("https://data.geopf.fr/wfs/ows",
                                "BDTOPO_V3:troncon_de_route",
                                bbox=[2.3, 48.8, 2.4, 48.9],
                                sql_filter="importance = '1'",
                                max_features=5000)
    """
    if name is None:
        name = typename.split(":")[-1] if ":" in typename else typename

    if bbox is None:
        bbox = bbox_from_canvas()
        if isinstance(bbox, dict) and "error" in bbox:
            return {"error": "No bbox provided and canvas has no extent. "
                    "Provide bbox=[xmin,ymin,xmax,ymax] in EPSG:4326."}

    # WFS bbox order for IGN: lat,lon (ymin,xmin,ymax,xmax)
    uri = (f"url='{url}' typename='{typename}' "
           f"srsname='{srsname}' "
           f"bbox='{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}' "
           f"pagingEnabled='true'")
    if max_features:
        uri += f" maxNumFeatures='{max_features}'"
    layer = QgsVectorLayer(uri, name, "WFS")
    if not layer.isValid():
        return {"error": f"Invalid WFS layer: {typename}", "uri": uri}
    # Apply attribute filter via setSubsetString (sends OGC filter to server)
    if sql_filter:
        if not layer.setSubsetString(sql_filter):
            return {"error": f"Invalid filter: {sql_filter}",
                    "fields": [f.name() for f in layer.fields()]}
    result = _finalize_layer(layer)
    result["bbox_used"] = bbox
    if sql_filter:
        result["sql_filter"] = sql_filter
    return result


def add_wms(url, layers, name=None, crs="EPSG:3857", fmt="image/png"):
    """Add a WMS layer.

    Example:
        layer = helpers.add_wms("https://data.geopf.fr/wms-r",
                                "ORTHOIMAGERY.ORTHOPHOTOS",
                                name="Orthophoto IGN")
    """
    if name is None:
        name = layers
    uri = f"url={url}&layers={layers}&format={fmt}&crs={crs}&styles="
    layer = QgsRasterLayer(uri, name, "wms")
    if not layer.isValid():
        return {"error": f"Invalid WMS layer: {layers}"}
    return _finalize_layer(layer)


def add_wmts(url, layers, name=None, tilematrixset="PM",
             crs="EPSG:3857", fmt="image/jpeg", styles="normal"):
    """Add a WMTS tiled layer.

    Example:
        layer = helpers.add_wmts("https://data.geopf.fr/wmts",
                                 "ORTHOIMAGERY.ORTHOPHOTOS")
    """
    if name is None:
        name = layers
    uri = (f"url={url}&layers={layers}&format={fmt}"
           f"&tilematrixset={tilematrixset}&crs={crs}"
           f"&styles={styles}&type=xyz")
    layer = QgsRasterLayer(uri, name, "wms")
    if not layer.isValid():
        return {"error": f"Invalid WMTS layer: {layers}"}
    return _finalize_layer(layer)


def add_xyz(url, name=None, zmin=0, zmax=19):
    """Add an XYZ tile layer.

    Example:
        layer = helpers.add_xyz(
            "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            name="OpenStreetMap")
    """
    if name is None:
        name = "XYZ Tiles"
    uri = f"type=xyz&url={url}&zmin={zmin}&zmax={zmax}"
    layer = QgsRasterLayer(uri, name, "wms")
    if not layer.isValid():
        return {"error": f"Invalid XYZ layer: {url}"}
    return _finalize_layer(layer)


def add_postgis(host, dbname, table, geom_col="geom", user="",
                password="", port=5432, schema="public", sql=""):
    """Add a PostGIS layer.

    Example:
        layer = helpers.add_postgis("myhost", "mydb", "buildings",
                                    user="postgres", password="pwd")
    """
    uri = (f"dbname='{dbname}' host={host} port={port} "
           f"user='{user}' password='{password}' "
           f"sslmode=disable key='id' srid=4326 type=auto "
           f'table="{schema}"."{table}" ({geom_col})')
    if sql:
        uri += f" sql={sql}"
    layer = QgsVectorLayer(uri, table, "postgres")
    if not layer.isValid():
        return {"error": f"Invalid PostGIS layer: {schema}.{table}@{host}"}
    return _finalize_layer(layer)


def add_spatialite(path, table):
    """Add a SpatiaLite layer.

    Example:
        layer = helpers.add_spatialite("/data/mydb.sqlite", "buildings")
    """
    uri = f"dbname='{path}' table='{table}'"
    layer = QgsVectorLayer(uri, table, "spatialite")
    if not layer.isValid():
        return {"error": f"Invalid SpatiaLite layer: {table} in {path}"}
    return _finalize_layer(layer)


def create_point_layer(name, points, fields=None, crs="EPSG:4326"):
    """Create an in-memory point layer from a list of dicts.

    Args:
        name: Layer name
        points: List of dicts with "lon"/"lat" (or "x"/"y") + attributes.
        fields: Optional {field_name: type_str}. Types: "string","int","double".
                If None, auto-detected from first point.
        crs: CRS (default EPSG:4326)

    Example:
        layer = helpers.create_point_layer("Cities", [
            {"lon": 2.35, "lat": 48.85, "name": "Paris", "pop": 2161000},
            {"lon": 4.83, "lat": 45.76, "name": "Lyon", "pop": 516092},
        ])
    """
    if not points:
        return {"error": "No points provided"}

    reserved = {"lon", "lat", "x", "y"}
    if fields is None:
        fields = {}
        sample = points[0]
        for key, val in sample.items():
            if key.lower() in reserved:
                continue
            if isinstance(val, int):
                fields[key] = "int"
            elif isinstance(val, float):
                fields[key] = "double"
            else:
                fields[key] = "string"

    type_map = {"string": "string", "int": "integer", "double": "double",
                "date": "date"}
    field_parts = "&".join(f"field={k}:{type_map.get(v, 'string')}"
                          for k, v in fields.items())
    uri = f"Point?crs={crs}"
    if field_parts:
        uri += "&" + field_parts

    layer = QgsVectorLayer(uri, name, "memory")
    if not layer.isValid():
        return {"error": "Failed to create memory layer"}

    provider = layer.dataProvider()
    field_names = list(fields.keys())
    feats = []
    for pt in points:
        lon = pt.get("lon", pt.get("x", 0))
        lat = pt.get("lat", pt.get("y", 0))
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(lon), float(lat))))
        attrs = [pt.get(fn) for fn in field_names]
        f.setAttributes(attrs)
        feats.append(f)

    provider.addFeatures(feats)
    layer.updateExtents()
    return _finalize_layer(layer)


# ── Navigation ────────────────────────────────────────────────

def bbox_from_canvas():
    """Get current canvas extent as [xmin, ymin, xmax, ymax] in EPSG:4326.

    Returns list or {"error": "..."}.
    """
    canvas = _canvas()
    if not canvas:
        return {"error": "No canvas available"}
    ext = canvas.extent()
    if ext.isEmpty():
        return {"error": "Canvas extent is empty"}

    project_crs = _project().crs()
    if project_crs.authid() != "EPSG:4326":
        transform = QgsCoordinateTransform(
            project_crs,
            QgsCoordinateReferenceSystem("EPSG:4326"),
            _project()
        )
        ext = transform.transformBoundingBox(ext)

    return [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]


def zoom_to(extent_4326):
    """Zoom canvas to an extent in EPSG:4326, a point dict, or an address string.

    Args:
        extent_4326: [xmin,ymin,xmax,ymax], {"lon":..,"lat":..}, or "address"

    Examples:
        helpers.zoom_to([2.3, 48.8, 2.4, 48.9])
        helpers.zoom_to({"lon": 2.35, "lat": 48.85})
        helpers.zoom_to("Gare de Lyon, Paris")
    """
    canvas = _canvas()
    if not canvas:
        return {"error": "No canvas available"}

    # Handle string address
    if isinstance(extent_4326, str):
        geo = geocode(extent_4326)
        if "error" in geo:
            return geo
        extent_4326 = geo["bbox"]

    # Handle point dict
    if isinstance(extent_4326, dict):
        lon = extent_4326.get("lon", extent_4326.get("x", 0))
        lat = extent_4326.get("lat", extent_4326.get("y", 0))
        buf = 0.005
        extent_4326 = [lon - buf, lat - buf, lon + buf, lat + buf]

    if not isinstance(extent_4326, (list, tuple)) or len(extent_4326) != 4:
        return {"error": "extent must be [xmin, ymin, xmax, ymax]"}

    rect = QgsRectangle(extent_4326[0], extent_4326[1],
                        extent_4326[2], extent_4326[3])

    project_crs = _project().crs()
    if project_crs.authid() != "EPSG:4326":
        source_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(source_crs, project_crs, _project())
        rect = transform.transformBoundingBox(rect)

    if rect.width() == 0 or rect.height() == 0:
        buf = 0.005 if project_crs.isGeographic() else 500
        rect = QgsRectangle(rect.xMinimum() - buf, rect.yMinimum() - buf,
                            rect.xMaximum() + buf, rect.yMaximum() + buf)

    rect.scale(1.05)
    canvas.setExtent(rect)
    canvas.refresh()

    e = canvas.extent()
    return {
        "extent": [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()],
        "project_crs": project_crs.authid(),
    }


# ── Catalog ───────────────────────────────────────────────────

def load_catalog_source(source_id, bbox=None, name=None, sql_filter=None,
                        max_features=None):
    """Load a data source from datasources.json by its ID.

    Args:
        source_id: Catalog source ID (e.g. "bdtopo_batiments")
        bbox: [xmin, ymin, xmax, ymax] in EPSG:4326 (required for WFS)
        name: Override display name
        sql_filter: OGC filter for WFS, e.g. "nature = 'Religieux'"
        max_features: Limit features for WFS

    Examples:
        layer = helpers.load_catalog_source("bdtopo_batiments",
                                            bbox=[2.3, 48.8, 2.4, 48.9])
        layer = helpers.load_catalog_source("bdtopo_batiments",
                                            bbox=[2.3, 48.8, 2.4, 48.9],
                                            sql_filter="nature = 'Religieux'")
    """
    catalog_path = Path("/app/datasources.json")
    if not catalog_path.exists():
        return {"error": "datasources.json not found"}

    catalog = json.loads(catalog_path.read_text())
    source = None
    for s in catalog.get("sources", []):
        if s.get("id") == source_id:
            source = s
            break
    if not source:
        available = [s["id"] for s in catalog.get("sources", []) if "id" in s]
        return {"error": f"Source not found: {source_id}", "available": available}

    src_type = source["type"]
    display_name = name or source["name"]
    params = source.get("params", {})

    if src_type == "wfs":
        return add_wfs(source["url"], params.get("typename", ""),
                       bbox=bbox, name=display_name,
                       srsname=params.get("srsname", "EPSG:4326"),
                       sql_filter=sql_filter, max_features=max_features)
    elif src_type == "wms":
        return add_wms(source["url"], params.get("layers", ""),
                       name=display_name, crs=params.get("crs", "EPSG:3857"),
                       fmt=params.get("format", "image/png"))
    elif src_type == "wmts":
        return add_wmts(source["url"], params.get("layers", ""),
                        name=display_name,
                        tilematrixset=params.get("tilematrixset", "PM"),
                        crs=params.get("crs", "EPSG:3857"),
                        fmt=params.get("format", "image/jpeg"),
                        styles=params.get("styles", "normal"))
    elif src_type == "xyz":
        return add_xyz(source["url"], name=display_name,
                       zmin=int(params.get("zmin", 0)),
                       zmax=int(params.get("zmax", 19)))
    elif src_type == "api":
        return {"info": f"{display_name} is an API, not a map layer.",
                "url": source["url"],
                "description": source.get("description", "")}
    else:
        return {"error": f"Unsupported source type: {src_type}"}


# ── Study Zone ───────────────────────────────────────────────

def set_study_zone(target, buffer_km=2):
    """Define the study zone. Stores bbox in project variables and zooms canvas.

    Args:
        target: commune name, address string, {"lon":..,"lat":..}, or [xmin,ymin,xmax,ymax] in 4326
        buffer_km: buffer around point/geocode result in km (default 2)

    Returns:
        {"name", "bbox_4326", "bbox_2154", "center_4326"} or {"error": ...}

    Examples:
        zone = helpers.set_study_zone("Sete")
        zone = helpers.set_study_zone("Gare de Lyon, Paris")
        zone = helpers.set_study_zone([3.6, 43.3, 3.8, 43.5])
    """
    project = _project()
    zone_name = ""
    bbox_4326 = None

    if isinstance(target, (list, tuple)) and len(target) == 4:
        # Explicit bbox
        bbox_4326 = [float(x) for x in target]
        zone_name = f"Zone [{bbox_4326[0]:.2f},{bbox_4326[1]:.2f}]"

    elif isinstance(target, dict):
        # Point dict
        lon = float(target.get("lon", target.get("x", 0)))
        lat = float(target.get("lat", target.get("y", 0)))
        buf_deg_lat = buffer_km * 0.009  # ~0.009 deg per km in latitude
        buf_deg_lon = buffer_km * 0.009 / max(math.cos(math.radians(lat)), 0.1)
        bbox_4326 = [lon - buf_deg_lon, lat - buf_deg_lat,
                     lon + buf_deg_lon, lat + buf_deg_lat]
        zone_name = f"Point ({lon:.4f}, {lat:.4f})"

    elif isinstance(target, str):
        # Try commune first (precise bbox), then geocode
        commune = search_commune(target)
        commune_bbox = commune.get("bbox") if "error" not in commune else None
        # Validate bbox is a flat list of 4 numbers
        if (commune_bbox and isinstance(commune_bbox, list) and len(commune_bbox) == 4
                and all(isinstance(v, (int, float)) for v in commune_bbox)):
            bbox_4326 = [float(v) for v in commune_bbox]
            zone_name = commune.get("nom", target)
        elif "error" not in commune and "lon" in commune:
            # Use commune center with buffer
            lon, lat = float(commune["lon"]), float(commune["lat"])
            buf_deg_lat = buffer_km * 0.009
            buf_deg_lon = buffer_km * 0.009 / max(math.cos(math.radians(lat)), 0.1)
            bbox_4326 = [lon - buf_deg_lon, lat - buf_deg_lat,
                         lon + buf_deg_lon, lat + buf_deg_lat]
            zone_name = commune.get("nom", target)
        else:
            geo = geocode(target)
            if "error" in geo:
                return geo
            lon, lat = geo["lon"], geo["lat"]
            buf_deg_lat = buffer_km * 0.009
            buf_deg_lon = buffer_km * 0.009 / max(math.cos(math.radians(lat)), 0.1)
            bbox_4326 = [lon - buf_deg_lon, lat - buf_deg_lat,
                         lon + buf_deg_lon, lat + buf_deg_lat]
            zone_name = geo.get("label", target)
    else:
        return {"error": f"Invalid target type: {type(target).__name__}. "
                "Use string, list [xmin,ymin,xmax,ymax], or dict {{lon, lat}}."}

    if not bbox_4326:
        return {"error": "Could not determine bbox from target"}

    # Reproject to EPSG:2154
    try:
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        crs_2154 = QgsCoordinateReferenceSystem("EPSG:2154")
        xform = QgsCoordinateTransform(crs_4326, crs_2154, project)
        rect_4326 = QgsRectangle(bbox_4326[0], bbox_4326[1],
                                 bbox_4326[2], bbox_4326[3])
        rect_2154 = xform.transformBoundingBox(rect_4326)
        bbox_2154 = [rect_2154.xMinimum(), rect_2154.yMinimum(),
                     rect_2154.xMaximum(), rect_2154.yMaximum()]
    except Exception as e:
        return {"error": f"CRS transform failed: {e}"}

    # Store in project variables
    QgsExpressionContextUtils.setProjectVariable(project, "study_zone_name", zone_name)
    QgsExpressionContextUtils.setProjectVariable(project, "study_zone_bbox_4326", json.dumps(bbox_4326))
    QgsExpressionContextUtils.setProjectVariable(project, "study_zone_bbox_2154", json.dumps(bbox_2154))
    QgsExpressionContextUtils.setProjectVariable(project, "study_zone_timestamp", str(int(time.time())))

    # Zoom canvas
    zoom_to(bbox_4326)

    center_4326 = [(bbox_4326[0] + bbox_4326[2]) / 2,
                   (bbox_4326[1] + bbox_4326[3]) / 2]

    return {
        "name": zone_name,
        "bbox_4326": bbox_4326,
        "bbox_2154": bbox_2154,
        "center_4326": center_4326,
    }


def get_study_zone():
    """Retrieve the stored study zone from project variables.

    Returns:
        {"name", "bbox_4326", "bbox_2154"} or {"error": "No study zone set"}
    """
    project = _project()
    scope = QgsExpressionContextUtils.projectScope(project)
    name = scope.variable("study_zone_name")
    if not name:
        return {"error": "No study zone set. Call set_study_zone() first."}

    bbox_4326_str = scope.variable("study_zone_bbox_4326")
    bbox_2154_str = scope.variable("study_zone_bbox_2154")

    try:
        bbox_4326 = json.loads(bbox_4326_str) if bbox_4326_str else None
        bbox_2154 = json.loads(bbox_2154_str) if bbox_2154_str else None
    except (json.JSONDecodeError, TypeError):
        return {"error": "Study zone variables are corrupted. Call set_study_zone() again."}

    return {
        "name": name,
        "bbox_4326": bbox_4326,
        "bbox_2154": bbox_2154,
    }


# ── Smart Download ───────────────────────────────────────────

def download_wfs_ogr(url, typename, bbox_4326=None, output_path=None,
                     native_crs="EPSG:2154", max_features=None, name=None):
    """Download WFS features as local GeoPackage via ogr2ogr.

    Uses GDAL's WFS driver which handles pagination automatically (IGN pages
    at 5000 features), bbox filtering, CRS transformation, and produces GPKG
    with R-tree spatial index. Results are cached for 24h.

    Args:
        url: WFS server URL (e.g. "https://data.geopf.fr/wfs/ows")
        typename: Layer typename (e.g. "BDTOPO_V3:batiment")
        bbox_4326: [xmin,ymin,xmax,ymax] in EPSG:4326 (auto from study zone if None)
        output_path: Save path (default /data/cache/{typename_short}_{hash}.gpkg)
        native_crs: Server's native CRS for output (default EPSG:2154)
        max_features: Max features to download (None = unlimited, uses bbox + pagination)
        name: Display name

    Returns:
        {"layer_id", "name", "feature_count", "path", "cached", ...} or {"error": ...}

    Examples:
        r = helpers.download_wfs_ogr(
            "https://data.geopf.fr/wfs/ows",
            "BDTOPO_V3:batiment")  # uses study zone bbox

        r = helpers.download_wfs_ogr(
            "https://data.geopf.fr/wfs/ows",
            "BDTOPO_V3:batiment",
            bbox_4326=[3.6, 43.3, 3.8, 43.5])
    """
    # 1. Resolve bbox
    if bbox_4326 is None:
        zone = get_study_zone()
        if "error" in zone:
            return {"error": "No bbox provided and no study zone set. "
                    "Call set_study_zone() first or provide bbox_4326."}
        bbox_4326 = zone["bbox_4326"]

    # 2. Determine output path with cache
    typename_short = typename.split(":")[-1] if ":" in typename else typename
    if name is None:
        name = typename_short
    bbox_hash = hashlib.md5(json.dumps(bbox_4326).encode()).hexdigest()[:8]
    if output_path is None:
        os.makedirs("/data/cache", exist_ok=True)
        output_path = f"/data/cache/{typename_short}_{bbox_hash}.gpkg"

    # 3. Check cache (< 24h)
    if os.path.exists(output_path):
        age_hours = (time.time() - os.path.getmtime(output_path)) / 3600
        if age_hours < 24:
            layer = QgsVectorLayer(output_path + f"|layername={typename_short}", name, "ogr")
            if layer.isValid():
                result = _finalize_layer(layer)
                result["path"] = output_path
                result["cached"] = True
                result["cache_age_hours"] = round(age_hours, 1)
                return result

    # 4. Build ogr2ogr command (two-step: download in 4326, then reproject)
    # GDAL 3.8 bug: -spat + -t_srs in same command produces 0 features.
    # Workaround: download with spatial filter only, then reproject separately.
    need_reproject = native_crs and native_crs.upper() != "EPSG:4326"
    dl_path = output_path + ".tmp.gpkg" if need_reproject else output_path

    cmd = [
        "ogr2ogr", "-f", "GPKG", dl_path,
        f"WFS:{url}", typename,
        "-nln", typename_short,  # strip namespace prefix (e.g. ms:LAYER → LAYER)
        "-forceNullable",  # allow NULL gml_id (Georisques WFS omits it)
        "-spat", str(bbox_4326[0]), str(bbox_4326[1]),
                str(bbox_4326[2]), str(bbox_4326[3]),
        "-spat_srs", "EPSG:4326",
        "--config", "OGR_WFS_PAGING_ALLOWED", "ON",
        "--config", "OGR_WFS_PAGE_SIZE", "5000",
        "-overwrite",
    ]
    if max_features:
        cmd += ["-limit", str(max_features)]

    # 5. Execute download
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"error": "ogr2ogr download timed out after 180s. Try a smaller bbox or fewer features."}
    except FileNotFoundError:
        return {"error": "ogr2ogr not found. GDAL may not be installed in the container."}

    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace")[:500]
        return {"error": f"ogr2ogr download failed (exit {proc.returncode}): {stderr}",
                "command": " ".join(cmd)}

    if not os.path.exists(dl_path):
        return {"error": f"ogr2ogr completed but output file not found: {dl_path}"}

    # 5b. Reproject to native CRS if needed
    if need_reproject:
        cmd2 = [
            "ogr2ogr", "-f", "GPKG", output_path, dl_path,
            "-t_srs", native_crs, "-overwrite",
        ]
        try:
            proc2 = subprocess.run(cmd2, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            return {"error": "ogr2ogr reproject timed out after 60s."}
        finally:
            # Clean up temp file
            if os.path.exists(dl_path):
                os.remove(dl_path)

        if proc2.returncode != 0:
            stderr = proc2.stderr.decode(errors="replace")[:500]
            return {"error": f"ogr2ogr reproject failed (exit {proc2.returncode}): {stderr}"}

    # 6. Verify output exists
    if not os.path.exists(output_path):
        return {"error": f"ogr2ogr completed but output file not found: {output_path}"}

    # 7. Load GPKG as OGR vector layer
    # Must use |layername= to avoid QGIS reading stale feature count statistics
    # Without it, featureCount() returns ~10k (cached) instead of actual count,
    # and native:clip processes only those ~10k features — not the full dataset.
    layer = QgsVectorLayer(output_path + f"|layername={typename_short}", name, "ogr")
    if not layer.isValid():
        return {"error": f"Downloaded GPKG is invalid: {output_path}"}

    result = _finalize_layer(layer)
    result["path"] = output_path
    result["cached"] = False
    result["download_size_mb"] = round(os.path.getsize(output_path) / 1048576, 2)
    return result


# ── Overpass API (OpenStreetMap data) ─────────────────────────

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT = 60


def overpass_query(tags, bbox_4326=None, name=None):
    """Query OpenStreetMap data via the Overpass API.

    Args:
        tags: dict like {"amenity": "school"} or string like "amenity=school"
              or raw Overpass QL if it starts with '[' or 'node' or 'way'
        bbox_4326: [xmin, ymin, xmax, ymax] in EPSG:4326. Auto from study zone if omitted.
        name: Display name for the resulting layer (auto-generated if omitted)

    Returns:
        dict with layer_id, name, feature_count, etc. or {"error": ...}

    Example:
        helpers.overpass_query({"amenity": "school"})
        helpers.overpass_query({"shop": "supermarket"}, name="Supermarchés")
        helpers.overpass_query("highway=cycleway", name="Pistes cyclables")
    """
    # Parse tags
    if isinstance(tags, str):
        if tags.startswith("[") or tags.startswith("node") or tags.startswith("way"):
            # Raw Overpass QL — use as-is
            raw_query = tags
        else:
            # Simple key=value format
            parts = tags.split("=", 1)
            if len(parts) == 2:
                tags = {parts[0].strip(): parts[1].strip()}
            else:
                return {"error": f"Cannot parse tags: {tags}. Use dict or 'key=value' format."}

    # Get bbox
    if bbox_4326 is None:
        zone = get_study_zone()
        if zone.get("bbox_4326"):
            bbox_4326 = zone["bbox_4326"]
        else:
            # Fallback: canvas extent
            bbox_4326 = bbox_from_canvas()
            if not bbox_4326:
                return {"error": "No bbox provided and no study zone set. Call set_study_zone first or provide bbox_4326."}

    # Build Overpass QL
    if isinstance(tags, dict):
        # Build from dict
        tag_filters = "".join(f'["{k}"="{v}"]' for k, v in tags.items())
        bbox_str = f"{bbox_4326[1]},{bbox_4326[0]},{bbox_4326[3]},{bbox_4326[2]}"
        raw_query = f"""[out:json][timeout:{OVERPASS_TIMEOUT}];
(
  node{tag_filters}({bbox_str});
  way{tag_filters}({bbox_str});
  relation{tag_filters}({bbox_str});
);
out body;
>;
out skel qt;"""
        if not name:
            name = " + ".join(f"{k}={v}" for k, v in tags.items())

    if not name:
        name = "OSM Query"

    # Execute query
    try:
        data = urllib.parse.urlencode({"data": raw_query}).encode()
        req = urllib.request.Request(OVERPASS_URL, data=data, method="POST")
        req.add_header("User-Agent", "QgisRemoteMCP/1.0")
        resp = urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT + 10)
        result_json = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"Overpass API error: {e.code} {e.reason}"}
    except Exception as e:
        return {"error": f"Overpass query failed: {e}"}

    elements = result_json.get("elements", [])
    if not elements:
        return {"error": "No results from Overpass API for this query and area.",
                "query_preview": raw_query[:200]}

    # Convert to GeoJSON
    nodes = {e["id"]: e for e in elements if e["type"] == "node"}
    features = []

    for el in elements:
        props = el.get("tags", {})
        props["osm_id"] = el["id"]
        props["osm_type"] = el["type"]

        if el["type"] == "node" and "lat" in el:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
                "properties": props,
            })
        elif el["type"] == "way" and "nodes" in el:
            coords = []
            for nid in el["nodes"]:
                n = nodes.get(nid)
                if n and "lat" in n:
                    coords.append([n["lon"], n["lat"]])
            if len(coords) >= 2:
                # Closed way = polygon
                if coords[0] == coords[-1] and len(coords) >= 4:
                    geom = {"type": "Polygon", "coordinates": [coords]}
                else:
                    geom = {"type": "LineString", "coordinates": coords}
                features.append({
                    "type": "Feature",
                    "geometry": geom,
                    "properties": props,
                })

    if not features:
        return {"error": "Overpass returned elements but none could be converted to GeoJSON.",
                "element_count": len(elements)}

    # Write GeoJSON to temp file
    geojson = {"type": "FeatureCollection", "features": features}
    cache_dir = Path("/data/cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_name = name.replace(" ", "_").replace("/", "_")[:50]
    output_path = str(cache_dir / f"osm_{safe_name}_{int(time.time())}.geojson")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f)

    # Load as QGIS layer
    layer = QgsVectorLayer(output_path, name, "ogr")
    if not layer.isValid():
        return {"error": f"Failed to load GeoJSON: {output_path}"}

    result = _finalize_layer(layer)
    result["path"] = output_path
    result["osm_elements"] = len(elements)
    result["features_converted"] = len(features)
    return result
