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
    QgsCoordinateTransform, QgsPointXY, Qgis
)
from qgis.PyQt.QtCore import QVariant, QSize, QTimer, QEventLoop
from qgis.PyQt.QtGui import QImage, QColor
from qgis.utils import iface

try:
    import processing
    PROCESSING_AVAILABLE = True
except ImportError:
    PROCESSING_AVAILABLE = False

SOCKET_PATH = "/tmp/qgis_bridge.sock"
MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10MB


class QGISBridge:
    """Bridge between external processes and the running QGIS instance."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.time()

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
            "processing_available": PROCESSING_AVAILABLE,
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
            if isinstance(layer, QgsVectorLayer):
                info["feature_count"] = layer.featureCount()
                info["geometry_type"] = layer.geometryType().name if hasattr(layer.geometryType(), 'name') else str(layer.geometryType())
                info["fields"] = [{"name": f.name(), "type": f.typeName()}
                                  for f in layer.fields()]
            elif isinstance(layer, QgsRasterLayer):
                info["width"] = layer.width()
                info["height"] = layer.height()
                info["band_count"] = layer.bandCount()
            layers.append(info)

        layouts = [layout.name() for layout in
                   project.layoutManager().printLayouts()]

        return {
            "title": project.title() or project.fileName(),
            "file": project.fileName(),
            "crs": project.crs().authid(),
            "layers": layers,
            "layer_count": len(layers),
            "print_layouts": layouts,
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
            "QVariant": QVariant,
            "json": json,
            "os": os,
            "Path": Path,
        }

        if PROCESSING_AVAILABLE:
            exec_globals["processing"] = processing

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
        if iface:
            iface.mapCanvas().refresh()
        return {"success": True, "title": title, "crs": crs}

    def _action_open_project(self, params: dict) -> dict:
        path = params.get("path", "")
        if not path or not os.path.exists(path):
            return {"error": f"Project file not found: {path}"}
        ok = QgsProject.instance().read(path)
        if iface:
            iface.mapCanvas().refresh()
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
        if iface:
            iface.mapCanvas().refresh()
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
        if iface:
            iface.mapCanvas().refresh()
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
        if iface:
            iface.mapCanvas().refresh()
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
        if iface:
            iface.mapCanvas().refresh()
        return {"layer_id": layer.id(), "name": layer.name()}

    def _action_remove_layer(self, params: dict) -> dict:
        layer_id = params.get("layer_id", "")
        QgsProject.instance().removeMapLayer(layer_id)
        if iface:
            iface.mapCanvas().refresh()
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
        if not PROCESSING_AVAILABLE:
            return {"error": "Processing not available"}
        algorithm = params.get("algorithm", "")
        parameters = params.get("parameters", {})
        feedback = params.get("feedback", False)

        result = processing.run(algorithm, parameters)

        # Auto-add output layers to project
        output_layers = {}
        for key, value in result.items():
            if isinstance(value, QgsVectorLayer) or isinstance(value, QgsRasterLayer):
                QgsProject.instance().addMapLayer(value)
                output_layers[key] = value.id()

        if iface:
            iface.mapCanvas().refresh()

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
        if not PROCESSING_AVAILABLE:
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
        if not iface:
            return {"error": "No iface available"}
        extent = params.get("extent")  # [xmin, ymin, xmax, ymax]
        if extent:
            rect = QgsRectangle(extent[0], extent[1], extent[2], extent[3])
            iface.mapCanvas().setExtent(rect)
        elif params.get("layer_id"):
            layer = QgsProject.instance().mapLayer(params["layer_id"])
            if layer:
                iface.mapCanvas().setExtent(layer.extent())
        else:
            iface.mapCanvas().zoomToFullExtent()
        iface.mapCanvas().refresh()
        e = iface.mapCanvas().extent()
        return {"extent": [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()]}

    # ── Screenshot ────────────────────────────────────────────────

    def _action_screenshot(self, params: dict) -> dict:
        if not iface:
            return {"error": "No iface available"}
        width = params.get("width", 800)
        height = params.get("height", 600)
        fmt = params.get("format", "png")

        path = f"/tmp/screenshot_{int(time.time())}.{fmt}"
        iface.mapCanvas().saveAsImage(path)

        # Read and encode
        with open(path, "rb") as f:
            data = f.read()
        os.unlink(path)

        if len(data) > MAX_MESSAGE_SIZE:
            return {"error": f"Screenshot too large: {len(data)} bytes"}

        return {
            "image_base64": base64.b64encode(data).decode(),
            "format": fmt,
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

        return {"success": True, "path": output_path,
                "size": os.path.getsize(output_path)}

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


# ── Start bridge ──────────────────────────────────────────────────
bridge = QGISBridge()
server_thread = threading.Thread(target=socket_server, args=(bridge,), daemon=True)
server_thread.start()
print("[QGISBridge] Started successfully")
