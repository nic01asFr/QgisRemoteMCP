"""
BigQgisMCP — QGIS Bridge
═══════════════════════════════════════════════════════════════════

This script runs INSIDE QGIS as a startup script. It:
  1. Listens on a UNIX socket for commands from MCP/API servers
  2. Executes PyQGIS code in the main QGIS context
  3. Returns results as JSON

Loaded automatically via:
  ~/.local/share/QGIS/QGIS3/profiles/default/python/startup/qgis_bridge.py

Communication: UNIX socket at /tmp/qgis_bridge.sock
Protocol: JSON request → JSON response (newline-delimited)
"""

import json
import os
import socket
import sys
import threading
import traceback
import time
import io
import base64
from pathlib import Path

# PyQGIS imports (available because we run inside QGIS)
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer, QgsCoordinateReferenceSystem,
    QgsFeatureRequest, QgsRectangle, QgsMapSettings, QgsMapRendererParallelJob,
    QgsLayoutExporter, QgsApplication, QgsExpression, QgsField,
    QgsCoordinateTransform, QgsPointXY, Qgis,
    QgsVectorFileWriter,
    QgsSingleSymbolRenderer, QgsFillSymbol, QgsMarkerSymbol, QgsLineSymbol,
    QgsCategorizedSymbolRenderer, QgsRendererCategory,
    QgsGraduatedSymbolRenderer, QgsRendererRange,
)
from qgis.PyQt.QtCore import QVariant, QSize, QTimer, QEventLoop
from qgis.PyQt.QtGui import QImage, QColor
import qgis.utils

processing = None  # Lazy-loaded (not available at startup, loaded after plugins init)

def _get_processing():
    """Lazy import of processing module (loaded after QGIS plugins init)."""
    global processing
    if processing is None:
        try:
            import processing as _p
            processing = _p
        except ImportError:
            pass
    return processing

SOCKET_PATH = "/tmp/qgis_bridge.sock"
MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_INLINE_FILE = 5 * 1024 * 1024    # 5MB — files above this return a download URL instead of base64
MAX_UPLOAD_SIZE = 50 * 1024 * 1024   # 50MB upload limit


def _guess_mime(suffix: str) -> str:
    """Map file extension to MIME type."""
    mapping = {
        ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".tif": "image/tiff", ".tiff": "image/tiff",
        ".geojson": "application/geo+json", ".json": "application/json",
        ".gpkg": "application/geopackage+sqlite3", ".shp": "application/x-shapefile",
        ".csv": "text/csv", ".qgz": "application/x-qgis-project",
        ".qgs": "application/x-qgis-project", ".qml": "application/x-qgis-style",
        ".zip": "application/zip", ".gml": "application/gml+xml",
    }
    return mapping.get(suffix.lower(), "application/octet-stream")


class QGISBridge:
    """Bridge between external processes and the running QGIS instance."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.time()

    @property
    def iface(self):
        """Lazy access to iface — resolves at call time, not import time."""
        return qgis.utils.iface

    # ── Command dispatcher ────────────────────────────────────────

    def handle(self, request: dict) -> dict:
        """Route a command to the appropriate handler."""
        action = request.get("action", "")
        params = request.get("params", {})

        try:
            handler = getattr(self, f"_action_{action}", None)
            if handler is None:
                return {"error": f"Unknown action: {action}",
                        "available": self._list_actions()}
            with self._lock:
                return handler(params)
        except Exception as e:
            return {"error": str(e), "traceback": traceback.format_exc()}

    def _list_actions(self) -> list:
        return [m.replace("_action_", "") for m in dir(self)
                if m.startswith("_action_")]

    # ══════════════════════════════════════════════════════════════
    # ACTIONS
    # ══════════════════════════════════════════════════════════════

    # ── Health / Info ─────────────────────────────────────────────

    def _action_health(self, params: dict) -> dict:
        project = QgsProject.instance()
        return {
            "status": "ok",
            "qgis_version": Qgis.version(),
            "project": project.fileName() or "(no project)",
            "layer_count": len(project.mapLayers()),
            "crs": project.crs().authid(),
            "uptime_seconds": int(time.time() - self._started),
            "processing_available": _get_processing() is not None,
        }

    def _action_get_project_info(self, params: dict) -> dict:
        project = QgsProject.instance()
        layers = []
        for lid, layer in project.mapLayers().items():
            info = {
                "id": lid,
                "name": layer.name(),
                "type": layer.type().name if hasattr(layer.type(), 'name') else str(layer.type()),
                "crs": layer.crs().authid(),
                "visible": project.layerTreeRoot().findLayer(layer).isVisible()
                           if project.layerTreeRoot().findLayer(layer) else True,
            }
            try:
                ext = layer.extent()
                if not ext.isEmpty():
                    info["extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
            except Exception:
                pass
            info["source"] = layer.source()[:200]
            if isinstance(layer, QgsVectorLayer):
                info["feature_count"] = layer.featureCount()
                info["geometry_type"] = layer.geometryType().name if hasattr(layer.geometryType(), 'name') else str(layer.geometryType())
                info["fields"] = [{"name": f.name(), "type": f.typeName()}
                                  for f in layer.fields()]
                renderer = layer.renderer()
                if renderer:
                    info["style_type"] = type(renderer).__name__
            elif isinstance(layer, QgsRasterLayer):
                info["width"] = layer.width()
                info["height"] = layer.height()
                info["band_count"] = layer.bandCount()
            layers.append(info)

        layouts = [layout.name() for layout in
                   project.layoutManager().printLayouts()]

        canvas_extent = None
        if self.iface and self.iface.mapCanvas():
            e = self.iface.mapCanvas().extent()
            canvas_extent = [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()]

        return {
            "title": project.title() or project.fileName(),
            "file": project.fileName(),
            "crs": project.crs().authid(),
            "layers": layers,
            "layer_count": len(layers),
            "print_layouts": layouts,
            "canvas_extent": canvas_extent,
        }

    # ── Execute Python (the power tool) ──────────────────────────

    def _action_execute_python(self, params: dict) -> dict:
        """Execute arbitrary Python/PyQGIS code.

        The script has access to:
          - qgis.core.*, qgis.utils.iface
          - processing.run()
          - 'result' dict to store return values
          - 'project' = QgsProject.instance()
          - 'canvas' = iface.mapCanvas()

        Set values in `result` dict to return them to the caller.
        """
        code = params.get("code", "")
        if not code.strip():
            return {"error": "No code provided"}

        # Execution context
        iface = qgis.utils.iface
        result = {}
        exec_globals = {
            "__builtins__": __builtins__,
            "result": result,
            "project": QgsProject.instance(),
            "canvas": iface.mapCanvas() if iface else None,
            "iface": iface,
            # Re-export common modules so scripts don't need to import
            "QgsProject": QgsProject,
            "QgsVectorLayer": QgsVectorLayer,
            "QgsRasterLayer": QgsRasterLayer,
            "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem,
            "QgsFeatureRequest": QgsFeatureRequest,
            "QgsRectangle": QgsRectangle,
            "QgsExpression": QgsExpression,
            "QgsField": QgsField,
            "QgsCoordinateTransform": QgsCoordinateTransform,
            "QgsPointXY": QgsPointXY,
            "QgsApplication": QgsApplication,
            "QVariant": QVariant,
            "json": json,
            "os": os,
            "Path": Path,
        }

        proc = _get_processing()
        if proc:
            exec_globals["processing"] = proc

        # Capture stdout
        old_stdout = sys.stdout
        captured = io.StringIO()
        sys.stdout = captured

        try:
            exec(code, exec_globals)
            stdout = captured.getvalue()

            # Serialize result (handle non-JSON-serializable objects)
            serialized = {}
            for k, v in result.items():
                try:
                    json.dumps(v)
                    serialized[k] = v
                except (TypeError, ValueError):
                    serialized[k] = str(v)

            return {
                "success": True,
                "result": serialized,
                "stdout": stdout,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
                "stdout": captured.getvalue(),
            }
        finally:
            sys.stdout = old_stdout

    # ── Project management ────────────────────────────────────────

    def _action_new_project(self, params: dict) -> dict:
        project = QgsProject.instance()
        project.clear()
        crs = params.get("crs", "EPSG:2154")  # Lambert 93 default (France)
        project.setCrs(QgsCoordinateReferenceSystem(crs))
        title = params.get("title", "BigQgisMCP Project")
        project.setTitle(title)
        if self.iface:
            self.iface.mapCanvas().refresh()
        return {"success": True, "title": title, "crs": crs}

    def _action_open_project(self, params: dict) -> dict:
        path = params.get("path", "")
        if not path or not os.path.exists(path):
            return {"error": f"Project file not found: {path}"}
        ok = QgsProject.instance().read(path)
        if self.iface:
            self.iface.mapCanvas().refresh()
        return {"success": ok, "path": path}

    def _action_save_project(self, params: dict) -> dict:
        path = params.get("path", "")
        project = QgsProject.instance()
        if path:
            ok = project.write(path)
        else:
            ok = project.write()
        return {"success": ok, "path": project.fileName()}

    # ── Layer management ──────────────────────────────────────────

    def _action_add_vector_layer(self, params: dict) -> dict:
        uri = params.get("uri", "")
        name = params.get("name", "layer")
        provider = params.get("provider", "ogr")  # ogr, WFS, postgres, memory
        layer = QgsVectorLayer(uri, name, provider)
        if not layer.isValid():
            return {"error": f"Invalid layer: {uri}", "provider": provider}
        QgsProject.instance().addMapLayer(layer)
        if self.iface:
            self.iface.mapCanvas().refresh()
        return {
            "layer_id": layer.id(),
            "name": layer.name(),
            "feature_count": layer.featureCount(),
            "crs": layer.crs().authid(),
        }

    def _action_add_raster_layer(self, params: dict) -> dict:
        uri = params.get("uri", "")
        name = params.get("name", "raster")
        provider = params.get("provider", "gdal")
        layer = QgsRasterLayer(uri, name, provider)
        if not layer.isValid():
            return {"error": f"Invalid raster: {uri}"}
        QgsProject.instance().addMapLayer(layer)
        if self.iface:
            self.iface.mapCanvas().refresh()
        return {
            "layer_id": layer.id(),
            "name": layer.name(),
            "width": layer.width(),
            "height": layer.height(),
            "crs": layer.crs().authid(),
        }

    def _action_add_wfs_layer(self, params: dict) -> dict:
        url = params.get("url", "")
        typename = params.get("typename", "")
        name = params.get("name", typename.split(":")[-1] if typename else "wfs")
        uri_parts = [f"url='{url}'", f"typename='{typename}'", "pagingEnabled='true'"]
        if params.get("bbox"):
            bbox = params["bbox"]
            uri_parts.append(f"bbox='{','.join(str(x) for x in bbox)}'")
        if params.get("srsname"):
            uri_parts.append(f"srsname='{params['srsname']}'")
        uri = " ".join(uri_parts)
        layer = QgsVectorLayer(uri, name, "WFS")
        if not layer.isValid():
            return {"error": f"Invalid WFS layer: {typename}", "uri": uri}
        QgsProject.instance().addMapLayer(layer)
        if self.iface:
            self.iface.mapCanvas().refresh()
        return {
            "layer_id": layer.id(),
            "name": layer.name(),
            "feature_count": layer.featureCount(),
        }

    def _action_add_wms_layer(self, params: dict) -> dict:
        url = params.get("url", "")
        layers = params.get("layers", "")
        name = params.get("name", layers)
        fmt = params.get("format", "image/png")
        crs = params.get("crs", "EPSG:3857")
        uri = f"url={url}&layers={layers}&format={fmt}&crs={crs}&styles="
        layer = QgsRasterLayer(uri, name, "wms")
        if not layer.isValid():
            return {"error": f"Invalid WMS layer: {layers}"}
        QgsProject.instance().addMapLayer(layer)
        if self.iface:
            self.iface.mapCanvas().refresh()
        return {"layer_id": layer.id(), "name": layer.name()}

    def _action_remove_layer(self, params: dict) -> dict:
        layer_id = params.get("layer_id", "")
        QgsProject.instance().removeMapLayer(layer_id)
        if self.iface:
            self.iface.mapCanvas().refresh()
        return {"success": True, "removed": layer_id}

    def _action_list_layers(self, params: dict) -> dict:
        layers = []
        for lid, layer in QgsProject.instance().mapLayers().items():
            layers.append({
                "id": lid,
                "name": layer.name(),
                "type": str(layer.type()),
            })
        return {"layers": layers, "count": len(layers)}

    def _action_get_features(self, params: dict) -> dict:
        layer_id = params.get("layer_id", "")
        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer or not isinstance(layer, QgsVectorLayer):
            return {"error": f"Vector layer not found: {layer_id}"}

        request = QgsFeatureRequest()
        if params.get("filter"):
            request.setFilterExpression(params["filter"])
        if params.get("limit"):
            request.setLimit(params["limit"])
        if params.get("bbox"):
            b = params["bbox"]
            request.setFilterRect(QgsRectangle(b[0], b[1], b[2], b[3]))

        features = []
        field_names = [f.name() for f in layer.fields()]
        for feat in layer.getFeatures(request):
            f = {
                "id": feat.id(),
                "attributes": dict(zip(field_names, [
                    v if isinstance(v, (str, int, float, bool, type(None)))
                    else str(v) for v in feat.attributes()
                ])),
            }
            if feat.hasGeometry() and params.get("include_geometry", True):
                f["geometry_wkt"] = feat.geometry().asWkt()
            features.append(f)

        return {"features": features, "count": len(features)}

    # ── Processing ────────────────────────────────────────────────

    def _action_run_processing(self, params: dict) -> dict:
        proc = _get_processing()
        if not proc:
            return {"error": "Processing not available"}
        algorithm = params.get("algorithm", "")
        parameters = params.get("parameters", {})

        result = proc.run(algorithm, parameters)

        # Auto-add output layers to project
        output_layers = {}
        for key, value in result.items():
            if isinstance(value, QgsVectorLayer) or isinstance(value, QgsRasterLayer):
                QgsProject.instance().addMapLayer(value)
                output_layers[key] = value.id()

        if self.iface:
            self.iface.mapCanvas().refresh()

        # Serialize result
        serialized = {}
        for k, v in result.items():
            if isinstance(v, (QgsVectorLayer, QgsRasterLayer)):
                serialized[k] = {"layer_id": v.id(), "name": v.name()}
            else:
                try:
                    json.dumps(v)
                    serialized[k] = v
                except (TypeError, ValueError):
                    serialized[k] = str(v)

        return {"result": serialized, "output_layers": output_layers}

    def _action_list_algorithms(self, params: dict) -> dict:
        if not _get_processing():
            return {"error": "Processing not available"}
        search = params.get("search", "").lower()
        provider = params.get("provider", "")

        algos = []
        for alg in QgsApplication.processingRegistry().algorithms():
            if provider and not alg.id().startswith(provider):
                continue
            if search and search not in alg.id().lower() and search not in alg.displayName().lower():
                continue
            algos.append({
                "id": alg.id(),
                "name": alg.displayName(),
                "group": alg.group(),
                "short_help": alg.shortHelpString()[:200] if alg.shortHelpString() else "",
            })
            if len(algos) >= params.get("limit", 50):
                break

        return {"algorithms": algos, "count": len(algos)}

    # ── Zoom / Navigation ─────────────────────────────────────────

    def _action_zoom_to_extent(self, params: dict) -> dict:
        if not self.iface:
            return {"error": "No iface available"}
        extent = params.get("extent")  # [xmin, ymin, xmax, ymax]
        if extent:
            rect = QgsRectangle(extent[0], extent[1], extent[2], extent[3])
            self.iface.mapCanvas().setExtent(rect)
        elif params.get("layer_id"):
            layer = QgsProject.instance().mapLayer(params["layer_id"])
            if layer:
                self.iface.mapCanvas().setExtent(layer.extent())
        else:
            self.iface.mapCanvas().zoomToFullExtent()
        self.iface.mapCanvas().refresh()
        e = self.iface.mapCanvas().extent()
        return {"extent": [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()]}

    # ── Screenshot ────────────────────────────────────────────────

    def _capture_screenshot(self, width=1920, height=1080):
        """Fast screenshot via Qt screen grab (no subprocess)."""
        from qgis.PyQt.QtWidgets import QApplication
        from qgis.PyQt.QtCore import QBuffer, QIODevice

        screen = QApplication.primaryScreen()
        if not screen:
            return None
        pixmap = screen.grabWindow(0)  # 0 = root window = full X11 display
        if pixmap.isNull():
            return None
        # Scale if needed
        if pixmap.width() != width or pixmap.height() != height:
            from qgis.PyQt.QtCore import Qt
            pixmap = pixmap.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # Encode to PNG in memory
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        pixmap.save(buf, "PNG", 80)
        data = bytes(buf.data())
        buf.close()
        return data

    def _action_screenshot(self, params: dict) -> dict:
        """Capture the full QGIS desktop."""
        width = params.get("width", 1920)
        height = params.get("height", 1080)

        data = self._capture_screenshot(width, height)

        # Fallback to ffmpeg if Qt fails
        if not data:
            import subprocess
            display = os.environ.get("DISPLAY", ":99")
            path = f"/tmp/screenshot_{int(time.time())}.png"
            try:
                # Capture full display (1920x1080) then scale to requested size
                cmd = [
                    "ffmpeg", "-y", "-f", "x11grab",
                    "-video_size", "1920x1080",
                    "-i", display,
                    "-vf", f"scale={width}:{height}",
                    "-frames:v", "1", "-update", "1", path
                ]
                subprocess.run(cmd, capture_output=True, timeout=10)
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        data = f.read()
                    os.unlink(path)
            except Exception:
                return {"error": "Screenshot failed (both Qt and ffmpeg)"}

        if not data or len(data) < 100:
            return {"error": "Screenshot produced empty image"}
        if len(data) > MAX_MESSAGE_SIZE:
            return {"error": f"Screenshot too large: {len(data)} bytes"}

        return {
            "image_base64": base64.b64encode(data).decode(),
            "format": "png",
            "size": len(data),
        }

    # ── Print / Export ────────────────────────────────────────────

    def _action_export_pdf(self, params: dict) -> dict:
        layout_name = params.get("layout", "")
        output_path = params.get("output_path", f"/data/export_{int(time.time())}.pdf")

        manager = QgsProject.instance().layoutManager()
        layout = manager.layoutByName(layout_name)
        if not layout:
            available = [l.name() for l in manager.printLayouts()]
            return {"error": f"Layout not found: {layout_name}", "available": available}

        exporter = QgsLayoutExporter(layout)
        settings = QgsLayoutExporter.PdfExportSettings()
        result = exporter.exportToPdf(output_path, settings)

        if result != QgsLayoutExporter.ExportResult.Success:
            return {"error": f"PDF export failed: {result}"}

        size = os.path.getsize(output_path)
        result_dict = {"success": True, "path": output_path, "size": size,
                       "download_url": f"http://localhost:8080/api/files/{Path(output_path).name}"}
        if size <= MAX_INLINE_FILE:
            with open(output_path, "rb") as f:
                result_dict["content_base64"] = base64.b64encode(f.read()).decode()
            result_dict["mime_type"] = "application/pdf"
        return result_dict

    def _action_export_image(self, params: dict) -> dict:
        layout_name = params.get("layout", "")
        output_path = params.get("output_path", f"/data/export_{int(time.time())}.png")
        dpi = params.get("dpi", 150)

        manager = QgsProject.instance().layoutManager()
        layout = manager.layoutByName(layout_name)
        if not layout:
            available = [l.name() for l in manager.printLayouts()]
            return {"error": f"Layout not found: {layout_name}", "available": available}

        exporter = QgsLayoutExporter(layout)
        settings = QgsLayoutExporter.ImageExportSettings()
        settings.dpi = dpi
        result = exporter.exportToImage(output_path, settings)

        if result != QgsLayoutExporter.ExportResult.Success:
            return {"error": f"Image export failed: {result}"}

        return {"success": True, "path": output_path,
                "size": os.path.getsize(output_path)}

    # ── Style ─────────────────────────────────────────────────────

    def _action_apply_style(self, params: dict) -> dict:
        layer_id = params.get("layer_id", "")
        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer:
            return {"error": f"Layer not found: {layer_id}"}

        qml_path = params.get("qml_path", "")
        if qml_path and os.path.exists(qml_path):
            msg, ok = layer.loadNamedStyle(qml_path)
            layer.triggerRepaint()
            return {"success": ok, "message": msg}

        return {"error": "No QML path provided or file not found"}

    def _action_set_layer_style(self, params: dict) -> dict:
        """Apply symbology: single, categorized, or graduated."""
        layer_id = params.get("layer_id", "")
        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer or not isinstance(layer, QgsVectorLayer):
            return {"error": f"Vector layer not found: {layer_id}"}

        style_type = params.get("style_type", "single")
        geom = layer.geometryType()  # 0=Point, 1=Line, 2=Polygon

        def make_symbol(color, size="2.5", width="0.5"):
            if geom == 0:  # Point
                return QgsMarkerSymbol.createSimple({"color": color, "size": size})
            elif geom == 1:  # Line
                return QgsLineSymbol.createSimple({"color": color, "width": width})
            else:  # Polygon
                return QgsFillSymbol.createSimple({
                    "color": color, "outline_color": "#333333", "outline_width": "0.3"})

        if style_type == "single":
            color = params.get("color", "65,105,225,180")
            layer.setRenderer(QgsSingleSymbolRenderer(make_symbol(color)))

        elif style_type == "categorized":
            field = params.get("field", "")
            categories_def = params.get("categories", {})
            if not field:
                return {"error": "Categorized style requires 'field' parameter"}
            cats = []
            for value, props in categories_def.items():
                color = props.get("color", "#888888")
                label = props.get("label", str(value))
                cats.append(QgsRendererCategory(value, make_symbol(color), label))
            layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))

        elif style_type == "graduated":
            field = params.get("field", "")
            ranges_def = params.get("ranges", [])
            if not field:
                return {"error": "Graduated style requires 'field' parameter"}
            ranges = []
            for r in ranges_def:
                color = r.get("color", "#888888")
                label = r.get("label", f"{r['min']}-{r['max']}")
                ranges.append(QgsRendererRange(r["min"], r["max"], make_symbol(color), label))
            layer.setRenderer(QgsGraduatedSymbolRenderer(field, ranges))

        else:
            return {"error": f"Unknown style type: {style_type}. Use single, categorized, or graduated."}

        layer.triggerRepaint()
        return {"success": True, "layer_id": layer_id, "style_type": style_type}

    def _action_set_layer_visibility(self, params: dict) -> dict:
        """Toggle layer visibility in the layer tree."""
        layer_id = params.get("layer_id", "")
        visible = params.get("visible", True)
        project = QgsProject.instance()
        layer = project.mapLayer(layer_id)
        if not layer:
            return {"error": f"Layer not found: {layer_id}"}
        node = project.layerTreeRoot().findLayer(layer)
        if not node:
            return {"error": f"Layer not in tree: {layer_id}"}
        node.setItemVisibilityChecked(visible)
        if self.iface:
            self.iface.mapCanvas().refresh()
        return {"success": True, "layer_id": layer_id, "visible": visible}

    # ── File Management ──────────────────────────────────────────

    @staticmethod
    def _safe_filename(name: str) -> str:
        """Validate and sanitize filename. Raises ValueError on unsafe input."""
        import re
        if not name or not name.strip():
            raise ValueError("Empty filename")
        name = name.replace("\\", "/")
        basename = name.split("/")[-1]
        if ".." in basename or basename.startswith("."):
            raise ValueError(f"Unsafe filename: {basename}")
        if not re.match(r'^[\w\-. ()\[\]]+$', basename):
            raise ValueError(f"Invalid characters in filename: {basename}")
        return basename

    def _action_list_files(self, params: dict) -> dict:
        """List files in /data/ and /projects/ directories."""
        directories = params.get("directories", ["/data", "/projects"])
        pattern = params.get("pattern", "*")
        results = []
        for d in directories:
            if not os.path.isdir(d):
                continue
            for fpath in sorted(Path(d).glob(pattern)):
                if fpath.is_file():
                    stat = fpath.stat()
                    results.append({
                        "path": str(fpath),
                        "name": fpath.name,
                        "size": stat.st_size,
                        "modified": int(stat.st_mtime),
                        "suffix": fpath.suffix,
                        "directory": str(fpath.parent),
                    })
        return {"files": results, "count": len(results)}

    def _action_write_file(self, params: dict) -> dict:
        """Write base64-encoded content to /data/. Used by upload_file tool."""
        try:
            name = self._safe_filename(params.get("name", ""))
        except ValueError as e:
            return {"error": str(e)}
        content_b64 = params.get("content_base64", "")
        if not content_b64:
            return {"error": "No content provided"}
        try:
            data = base64.b64decode(content_b64)
        except Exception as e:
            return {"error": f"Invalid base64: {e}"}
        if len(data) > MAX_UPLOAD_SIZE:
            return {"error": f"File too large: {len(data)} bytes (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)"}
        dest = Path("/data") / name
        dest.write_bytes(data)
        return {"success": True, "path": str(dest), "size": len(data), "name": name}

    def _action_read_file(self, params: dict) -> dict:
        """Read file content as base64. Restricted to /data/ and /projects/."""
        path = params.get("path", "")
        if not path:
            return {"error": "No path provided"}
        fpath = Path(path)
        allowed = [Path("/data"), Path("/projects")]
        try:
            resolved = fpath.resolve()
            if not any(str(resolved).startswith(str(d.resolve())) for d in allowed):
                return {"error": f"Access denied: {path} is not in /data/ or /projects/"}
        except Exception:
            return {"error": f"Invalid path: {path}"}
        if not fpath.exists():
            return {"error": f"File not found: {path}"}
        size = fpath.stat().st_size
        result = {
            "path": str(fpath),
            "name": fpath.name,
            "size": size,
            "mime_type": _guess_mime(fpath.suffix),
        }
        if size > MAX_INLINE_FILE:
            result["too_large_for_inline"] = True
            result["download_url"] = f"http://localhost:8080/api/files/{fpath.name}"
        else:
            result["content_base64"] = base64.b64encode(fpath.read_bytes()).decode()
        return result

    def _action_export_layer(self, params: dict) -> dict:
        """Export a vector layer to file (GPKG, GeoJSON, Shapefile, CSV)."""
        layer_id = params.get("layer_id", "")
        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer or not isinstance(layer, QgsVectorLayer):
            return {"error": f"Vector layer not found: {layer_id}"}

        fmt = params.get("format", "GPKG")
        name = params.get("name", "") or layer.name().replace(" ", "_")
        try:
            name = self._safe_filename(name)
        except ValueError as e:
            return {"error": str(e)}

        ext_map = {"GPKG": ".gpkg", "GeoJSON": ".geojson",
                    "ESRI Shapefile": ".shp", "CSV": ".csv"}
        ext = ext_map.get(fmt, ".gpkg")
        if not name.endswith(ext):
            name = name + ext
        output_path = f"/data/{name}"

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = fmt
        options.fileEncoding = "UTF-8"
        error = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, output_path, QgsProject.instance().transformContext(), options
        )
        if error[0] != QgsVectorFileWriter.WriterError.NoError:
            return {"error": f"Export failed: {error[1]}"}

        size = os.path.getsize(output_path)
        return {
            "success": True, "path": output_path, "name": name,
            "format": fmt, "size": size,
            "download_url": f"http://localhost:8080/api/files/{name}",
        }

    def _action_download_project(self, params: dict) -> dict:
        """Save current project as .qgz and return path + metadata."""
        project = QgsProject.instance()
        name = params.get("name", "project")
        try:
            name = self._safe_filename(name)
        except ValueError as e:
            return {"error": str(e)}
        if not name.endswith(".qgz"):
            name += ".qgz"
        output_path = f"/data/{name}"
        ok = project.write(output_path)
        if not ok:
            return {"error": "Failed to save project"}
        size = os.path.getsize(output_path)
        return {
            "success": True, "path": output_path, "name": name, "size": size,
            "download_url": f"http://localhost:8080/api/files/{name}",
        }

    # ── Data Source Catalog ──────────────────────────────────────

    def _load_datasources_catalog(self):
        """Load catalog from JSON file (cached)."""
        if not hasattr(self, '_catalog_cache'):
            catalog_path = Path("/app/datasources.json")
            if catalog_path.exists():
                self._catalog_cache = json.loads(catalog_path.read_text())
            else:
                self._catalog_cache = {"sources": [], "categories": []}
        return self._catalog_cache

    def _action_list_datasources(self, params: dict) -> dict:
        """List available data sources from catalog, optionally filtered."""
        catalog = self._load_datasources_catalog()
        category = params.get("category", "")
        search = params.get("search", "").lower()
        sources = catalog.get("sources", [])
        if category:
            sources = [s for s in sources if s.get("category") == category]
        if search:
            sources = [s for s in sources if search in s.get("name", "").lower()
                       or search in s.get("description", "").lower()
                       or search in s.get("id", "").lower()]
        return {
            "sources": sources, "count": len(sources),
            "categories": catalog.get("categories", []),
        }

    def _action_add_from_catalog(self, params: dict) -> dict:
        """Add a data source from the catalog by its ID."""
        source_id = params.get("id", "")
        catalog = self._load_datasources_catalog()
        source = None
        for s in catalog.get("sources", []):
            if s["id"] == source_id:
                source = s
                break
        if not source:
            available = [s["id"] for s in catalog.get("sources", [])]
            return {"error": f"Source not found: {source_id}", "available": available}

        src_type = source["type"]
        name = params.get("name", "") or source["name"]
        src_params = {**source.get("params", {})}
        bbox = params.get("bbox")  # [xmin, ymin, xmax, ymax] in EPSG:4326

        layer = None

        if src_type == "wfs":
            if not bbox:
                return {"error": "WFS sources require a bbox [xmin, ymin, xmax, ymax] in EPSG:4326"}
            typename = src_params.get("typename", "")
            srsname = src_params.get("srsname", "EPSG:4326")
            uri = (f"url='{source['url']}' typename='{typename}' "
                   f"srsname='{srsname}' "
                   f"bbox='{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}' "
                   f"pagingEnabled='true'")
            layer = QgsVectorLayer(uri, name, "WFS")
            if not layer.isValid():
                return {"error": f"Invalid WFS layer: {typename}", "uri": uri}

        elif src_type == "wms":
            layers_param = src_params.get("layers", "")
            fmt = src_params.get("format", "image/png")
            crs = src_params.get("crs", "EPSG:3857")
            uri = f"url={source['url']}&layers={layers_param}&format={fmt}&crs={crs}&styles="
            layer = QgsRasterLayer(uri, name, "wms")
            if not layer.isValid():
                return {"error": f"Invalid WMS layer: {layers_param}"}

        elif src_type == "wmts":
            layers_param = src_params.get("layers", "")
            fmt = src_params.get("format", "image/jpeg")
            tms = src_params.get("tilematrixset", "PM")
            crs = src_params.get("crs", "EPSG:3857")
            styles = src_params.get("styles", "normal")
            uri = (f"url={source['url']}&layers={layers_param}&format={fmt}"
                   f"&tilematrixset={tms}&crs={crs}&styles={styles}&type=xyz")
            layer = QgsRasterLayer(uri, name, "wms")
            if not layer.isValid():
                return {"error": f"Invalid WMTS layer: {layers_param}"}

        elif src_type == "xyz":
            url = source["url"]
            zmin = src_params.get("zmin", "0")
            zmax = src_params.get("zmax", "19")
            uri = f"type=xyz&url={url}&zmin={zmin}&zmax={zmax}"
            layer = QgsRasterLayer(uri, name, "wms")
            if not layer.isValid():
                return {"error": f"Invalid XYZ layer: {url}"}

        elif src_type == "api":
            return {
                "info": f"{source['name']} is an API, not a map layer. Use execute_python to interact with it.",
                "url": source["url"], "description": source.get("description", ""),
            }
        else:
            return {"error": f"Unsupported source type: {src_type}"}

        QgsProject.instance().addMapLayer(layer)
        if self.iface:
            self.iface.mapCanvas().refresh()
        return {"success": True, "layer_id": layer.id(), "name": layer.name(),
                "source_id": source_id, "type": src_type}

    # ── X11 Interaction (xdotool) ─────────────────────────────────

    def _action_mouse_click(self, params: dict) -> dict:
        """Click at (x, y) on the X11 display using xdotool."""
        import subprocess
        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        button = int(params.get("button", 1))  # 1=left, 2=middle, 3=right
        double = params.get("double", False)
        display = os.environ.get("DISPLAY", ":99")

        cmd = ["xdotool", "mousemove", "--screen", "0", str(x), str(y)]
        if double:
            cmd += ["click", "--repeat", "2", "--delay", "100", str(button)]
        else:
            cmd += ["click", str(button)]

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=5,
                                    env={**os.environ, "DISPLAY": display})
            if result.returncode != 0:
                return {"error": f"xdotool failed: {result.stderr.decode()}"}
            return {"success": True, "x": x, "y": y, "button": button}
        except Exception as e:
            return {"error": str(e)}

    def _action_mouse_scroll(self, params: dict) -> dict:
        """Scroll at (x, y) on the X11 display."""
        import subprocess
        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        direction = params.get("direction", "down")  # up or down
        clicks = int(params.get("clicks", 3))
        display = os.environ.get("DISPLAY", ":99")

        # Move then scroll
        button = 4 if direction == "up" else 5
        cmd = ["xdotool", "mousemove", "--screen", "0", str(x), str(y),
               "click", "--repeat", str(clicks), "--delay", "50", str(button)]

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=5,
                                    env={**os.environ, "DISPLAY": display})
            if result.returncode != 0:
                return {"error": f"xdotool failed: {result.stderr.decode()}"}
            return {"success": True, "x": x, "y": y, "direction": direction}
        except Exception as e:
            return {"error": str(e)}

    def _action_key_press(self, params: dict) -> dict:
        """Send a key press to the X11 display."""
        import subprocess
        key = params.get("key", "")  # e.g. "Return", "ctrl+z", "ctrl+shift+s"
        display = os.environ.get("DISPLAY", ":99")

        if not key:
            return {"error": "No key specified"}

        try:
            result = subprocess.run(
                ["xdotool", "key", key],
                capture_output=True, timeout=5,
                env={**os.environ, "DISPLAY": display}
            )
            if result.returncode != 0:
                return {"error": f"xdotool failed: {result.stderr.decode()}"}
            return {"success": True, "key": key}
        except Exception as e:
            return {"error": str(e)}

    def _action_mouse_drag(self, params: dict) -> dict:
        """Drag from (x1,y1) to (x2,y2)."""
        import subprocess
        x1, y1 = int(params.get("x1", 0)), int(params.get("y1", 0))
        x2, y2 = int(params.get("x2", 0)), int(params.get("y2", 0))
        button = int(params.get("button", 1))
        display = os.environ.get("DISPLAY", ":99")

        try:
            subprocess.run(["xdotool", "mousemove", "--screen", "0", str(x1), str(y1)],
                           capture_output=True, timeout=5,
                           env={**os.environ, "DISPLAY": display})
            subprocess.run(["xdotool", "mousedown", str(button)],
                           capture_output=True, timeout=5,
                           env={**os.environ, "DISPLAY": display})
            subprocess.run(["xdotool", "mousemove", "--screen", "0", str(x2), str(y2)],
                           capture_output=True, timeout=5,
                           env={**os.environ, "DISPLAY": display})
            subprocess.run(["xdotool", "mouseup", str(button)],
                           capture_output=True, timeout=5,
                           env={**os.environ, "DISPLAY": display})
            return {"success": True, "from": [x1, y1], "to": [x2, y2]}
        except Exception as e:
            return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════
# SOCKET SERVER (runs in background thread)
# ══════════════════════════════════════════════════════════════════

def socket_server(bridge: QGISBridge):
    """Listen on UNIX socket for JSON commands."""
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(10)
    os.chmod(SOCKET_PATH, 0o777)

    print(f"[QGISBridge] Listening on {SOCKET_PATH}")
    print(f"[QGISBridge] Available actions: {bridge._list_actions()}")

    while True:
        conn, _ = server.accept()
        try:
            # Read until we get complete JSON
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                # Try to parse — if valid JSON, we're done
                try:
                    json.loads(data.decode())
                    break
                except json.JSONDecodeError:
                    continue

            if data:
                request = json.loads(data.decode())
                response = bridge.handle(request)
                conn.sendall(json.dumps(response, default=str).encode())
        except Exception as e:
            try:
                conn.sendall(json.dumps({
                    "error": str(e),
                    "traceback": traceback.format_exc()
                }).encode())
            except:
                pass
        finally:
            conn.close()


# ── Optimize rendering for software OpenGL (llvmpipe) ────────────
def _optimize_rendering():
    """Apply rendering optimizations after QGIS is fully loaded."""
    try:
        from qgis.core import QgsSettings
        s = QgsSettings()
        s.setValue("qgis/enable_render_caching", True)
        s.setValue("qgis/parallel_rendering", True)
        s.setValue("qgis/enable_anti_aliasing", True)
        s.setValue("Map/updateInterval", 100)
        s.setValue("cache/size", 256)  # 256 MB tile cache
        s.setValue("cache/directory", "/tmp/qgis/cache")

        iface = qgis.utils.iface
        if iface:
            canvas = iface.mapCanvas()
            canvas.enableAntiAliasing(True)
            canvas.setParallelRenderingEnabled(True)
            canvas.setMapUpdateInterval(100)
            canvas.setCachingEnabled(True)
            print("[QGISBridge] Rendering optimized for software renderer")
    except Exception as e:
        print(f"[QGISBridge] Rendering optimization warning: {e}")

# ── Configure QGIS for container environment ─────────────────────
def _configure_environment():
    """Adapt QGIS to the container: paths, CRS, dialogs, connections."""
    try:
        from qgis.core import QgsSettings
        s = QgsSettings()

        # ── File paths: restrict to /data/ and /projects/ ────────
        s.setValue("UI/lastProjectDir", "/projects")
        s.setValue("UI/lastVectorFileFilterDir", "/data")
        s.setValue("UI/lastRasterFileFilterDir", "/data")
        s.setValue("UI/lastFileNameWidgetDir", "/data")

        # Browser panel: hide system paths, show only useful ones
        s.setValue("browser/hiddenPaths", [
            "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64",
            "/media", "/mnt", "/opt", "/proc", "/root", "/run",
            "/sbin", "/srv", "/sys", "/usr", "/var", "/tmp",
            "/app", "/snap",
        ])

        # ── Default CRS ─────────────────────────────────────────
        s.setValue("Projections/defaultBehavior", "useProject")
        s.setValue("Projections/layerDefaultCrs", "EPSG:2154")

        # ── Disable dialogs and tips ─────────────────────────────
        s.setValue("qgis/showTips", False)
        s.setValue("qgis/checkVersion", False)
        s.setValue("qgis/nullValue", "NULL")

        # ── Pre-configure data source connections ────────────────
        catalog_path = Path("/app/datasources.json")
        if catalog_path.exists():
            catalog = json.loads(catalog_path.read_text())
            for src in catalog.get("sources", []):
                name = src.get("name", "")
                url = src.get("url", "")
                src_type = src.get("type", "")

                if src_type == "xyz":
                    s.setValue(f"qgis/connections-xyz/{name}/url", url)
                    s.setValue(f"qgis/connections-xyz/{name}/zmin", src.get("params", {}).get("zmin", "0"))
                    s.setValue(f"qgis/connections-xyz/{name}/zmax", src.get("params", {}).get("zmax", "19"))

                elif src_type in ("wms", "wmts"):
                    s.setValue(f"qgis/connections-wms/{name}/url", url)

                elif src_type == "wfs":
                    s.setValue(f"qgis/connections-wfs/{name}/url", url)

            # Reload browser to show new connections
            iface = qgis.utils.iface
            if iface:
                iface.reloadConnections()

            print(f"[QGISBridge] Configured {len(catalog.get('sources', []))} data source connections")

        print("[QGISBridge] Environment configured for container")
    except Exception as e:
        print(f"[QGISBridge] Environment config warning: {e}")


# Apply after QGIS is fully loaded
QTimer.singleShot(3000, _optimize_rendering)
QTimer.singleShot(5000, _configure_environment)


# ── Start bridge ──────────────────────────────────────────────────
bridge = QGISBridge()
server_thread = threading.Thread(target=socket_server, args=(bridge,), daemon=True)
server_thread.start()
print("[QGISBridge] Started successfully")
