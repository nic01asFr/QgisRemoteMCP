"""
QgisRemoteMCP — QGIS Bridge
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

# Host-side API port for download URLs — injected by ContainerManager in multi-user mode
_API_HOST_PORT = os.environ.get("API_HOST_PORT", "8080")

# PyQGIS imports (available because we run inside QGIS)
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer, QgsCoordinateReferenceSystem,
    QgsFeatureRequest, QgsRectangle, QgsMapSettings, QgsMapRendererParallelJob,
    QgsLayoutExporter, QgsApplication, QgsExpression, QgsField, QgsFields,
    QgsCoordinateTransform, QgsPointXY, Qgis, QgsWkbTypes,
    QgsVectorFileWriter, QgsEditorWidgetSetup,
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

# ── Async job runtime ────────────────────────────────────────────
# Each async job keeps its own socket open for the lifetime of the job so the
# watchdog can stream newline-delimited JSON frames (_ack, _heartbeat*, _result).
# _JOB_STATUS_MIRROR is the fallback registry read by status-mode probes — it
# answers even when the Qt main thread is frozen because it never touches Qt.
_ASYNC_LOCK = threading.Lock()
_ACTIVE_JOBS = {}          # job_id -> {conn, lock, action, submitted_at, started_at, stage, cancel_requested}
_JOB_STATUS_MIRROR = {}    # job_id -> {status, stage, qt_lag_ms, heartbeat_at, finished_at?}
_LAST_MAIN_TICK = [0.0]    # updated by main thread via watchdog probe; compared against wall clock
_MIRROR_MAX = 500          # cap _JOB_STATUS_MIRROR growth


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

    # Actions that modify project state — context is appended to their responses
    _MUTATING_ACTIONS = frozenset({
        "execute_python", "new_project", "open_project",
        "add_vector_layer", "add_raster_layer", "add_wfs_layer", "add_wms_layer",
        "remove_layer", "run_processing", "zoom_to_extent",
        "set_layer_style", "set_layer_visibility", "apply_style",
        "add_from_catalog", "set_study_zone", "smart_load",
        "apply_layout_template", "export_web_map", "export_flood_map", "export_temporal_map", "export_qfield", "export_grist",
        "mouse_click", "mouse_scroll", "key_press", "mouse_drag",
    })

    def handle(self, request: dict) -> dict:
        """Route a command to the appropriate handler."""
        action = request.get("action", "")
        params = request.get("params", {})

        try:
            handler = getattr(self, f"_action_{action}", None)
            if handler is None:
                return {"error": f"Unknown action: {action}",
                        "available": self._list_actions()}
            # Lock only mutating actions. All actions already run on the Qt main
            # thread (serialized by the event queue), so the lock is redundant
            # for read-only paths and just blocks concurrent polls/status reads.
            if action in self._MUTATING_ACTIONS:
                with self._lock:
                    response = handler(params)
            else:
                response = handler(params)

            # Append workflow context to mutating actions (no error)
            if action in self._MUTATING_ACTIONS and "error" not in response:
                try:
                    response["_context"] = self._build_context()
                except Exception:
                    pass  # never fail on context building

            return response
        except Exception as e:
            return {"error": str(e), "traceback": traceback.format_exc()}

    def _list_actions(self) -> list:
        return [m.replace("_action_", "") for m in dir(self)
                if m.startswith("_action_")]

    def _resolve_layer(self, layer_id: str, vector_only: bool = False):
        """Resolve a layer_id to a layer object with enriched error messages.
        Returns (layer, None) on success, or (None, error_dict) on failure."""
        project = QgsProject.instance()
        layer = project.mapLayer(layer_id)
        if not layer:
            available = [{"id": lid, "name": l.name()}
                         for lid, l in project.mapLayers().items()]
            return None, {"error": f"Layer not found: {layer_id}",
                          "available_layers": available}
        if vector_only and not isinstance(layer, QgsVectorLayer):
            return None, {"error": f"Not a vector layer: {layer_id} (type: {layer.type().name if hasattr(layer.type(), 'name') else str(layer.type())})"}
        return layer, None

    def _auto_save(self):
        """Auto-save project to /data/.autosave.qgz before risky operations."""
        try:
            project = QgsProject.instance()
            if project.layerTreeRoot().children():  # has content worth saving
                project.write("/data/.autosave.qgz")
        except Exception:
            pass  # never fail on autosave

    # ── Workflow context ──────────────────────────────────────────

    def _build_context(self) -> dict:
        """Lightweight project state snapshot, appended to mutating action responses.
        Inspired by BigLocalApps' _mode_context() pattern."""
        from qgis.core import QgsExpressionContextUtils
        project = QgsProject.instance()
        scope = QgsExpressionContextUtils.projectScope(project)
        zone_name = scope.variable("study_zone_name")

        layers = list(project.mapLayers().values())
        vector_layers = [l for l in layers if isinstance(l, QgsVectorLayer)]
        raster_layers = [l for l in layers if isinstance(l, QgsRasterLayer)]

        has_data = any(l.featureCount() > 0 for l in vector_layers)
        has_styled = any(
            l.renderer() and not isinstance(l.renderer(), QgsSingleSymbolRenderer)
            for l in vector_layers
        )
        has_layouts = bool(project.layoutManager().printLayouts())

        # Phase detection (soft, not strict)
        if has_layouts:
            phase = "export"
        elif has_styled:
            phase = "cartography"
        elif has_data and zone_name:
            phase = "analysis"
        else:
            phase = "setup"

        return {
            "phase": phase,
            "study_zone": zone_name or None,
            "layers": [
                {"name": l.name(), "id": l.id(), "features": l.featureCount()}
                for l in vector_layers
            ],
            "raster_count": len(raster_layers),
            "has_layouts": has_layouts,
            "hint": self._phase_hint(phase, zone_name, len(vector_layers)),
        }

    @staticmethod
    def _phase_hint(phase: str, zone_name, vector_count: int) -> str:
        if phase == "setup" and not zone_name:
            return "Start with set_study_zone to define your area, then smart_load to load data"
        if phase == "setup":
            return "Load data with smart_load (e.g. bdtopo_batiments, osm_xyz). Use list_datasources to browse."
        if phase == "analysis":
            return "Analyze (run_processing, execute_python) or style layers (set_layer_style)"
        if phase == "cartography":
            return "Apply a layout (apply_layout_template) then export (export_pdf, export_web_map)"
        if phase == "export":
            return "Export: export_pdf, export_web_map, download_project, export_layer"
        return ""

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

        # Zone d'étude (variables projet posées par set_study_zone). Indispensable
        # pour que l'agent ré-utilise la bbox au lieu de re-géocoder un string.
        from qgis.core import QgsExpressionContextUtils
        import json as _json
        scope = QgsExpressionContextUtils.projectScope(project)
        study_zone = None
        zname = scope.variable("study_zone_name")
        if zname:
            study_zone = {"name": str(zname), "crs": "EPSG:4326"}
            for var_key, out_key in (("study_zone_bbox_4326", "bbox"),
                                     ("study_zone_bbox_2154", "bbox_2154")):
                raw = scope.variable(var_key)
                if raw is None:
                    continue
                try:
                    if isinstance(raw, str):
                        study_zone[out_key] = _json.loads(raw)
                    else:
                        study_zone[out_key] = list(raw)
                except Exception:
                    pass
            # Centre dérivé de la bbox 4326 si dispo (utile pour le prompt L2)
            bbox4 = study_zone.get("bbox")
            if isinstance(bbox4, list) and len(bbox4) == 4:
                study_zone["center"] = [(bbox4[0] + bbox4[2]) / 2.0,
                                        (bbox4[1] + bbox4[3]) / 2.0]

        return {
            "title": project.title() or project.fileName(),
            "file": project.fileName(),
            "crs": project.crs().authid(),
            "crs_description": project.crs().description(),
            "layers": layers,
            "layer_count": len(layers),
            "print_layouts": layouts,
            "canvas_extent": canvas_extent,
            "study_zone": study_zone,
        }

    # ── Execute Python (the power tool) ──────────────────────────

    def _action_execute_python(self, params: dict) -> dict:
        """Execute arbitrary Python/PyQGIS code.

        The script has access to:
          - qgis.core.*, qgis.utils.iface
          - processing.run()
          - helpers (ready-made functions: geocode, add_wfs, zoom_to, etc.)
          - 'result' dict to store return values
          - 'project' = QgsProject.instance()
          - 'canvas' = iface.mapCanvas()

        Set values in `result` dict to return them to the caller.
        """
        code = params.get("code", "")
        if not code.strip():
            return {"error": "No code provided"}

        # Validate syntax before execution to prevent QGIS crashes
        try:
            compile(code, "<execute_python>", "exec")
        except SyntaxError as e:
            return {
                "success": False,
                "error": f"Syntax error: {e.msg} (line {e.lineno})",
                "lineno": e.lineno,
                "offset": e.offset,
            }

        # Auto-save before executing arbitrary code
        self._auto_save()

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

        # Inject helpers module
        try:
            import qgis_helpers
            exec_globals["helpers"] = qgis_helpers
        except ImportError:
            pass

        proc = _get_processing()
        if proc:
            exec_globals["processing"] = proc

        # Capture stdout
        old_stdout = sys.stdout
        captured = io.StringIO()
        sys.stdout = captured

        # Execute with timeout (threading-based, safe for non-main threads)
        timeout = params.get("timeout", 30)
        exec_thread = threading.current_thread()
        timed_out = False

        def _timeout_killer():
            nonlocal timed_out
            timed_out = True
            import ctypes
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(exec_thread.ident),
                ctypes.py_object(TimeoutError),
            )

        timer = threading.Timer(timeout, _timeout_killer)
        timer.start()
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
        except TimeoutError:
            return {
                "success": False,
                "error": f"Script exceeded {timeout}s timeout",
                "stdout": captured.getvalue(),
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
                "stdout": captured.getvalue(),
            }
        finally:
            timer.cancel()
            sys.stdout = old_stdout

    # ── Project management ────────────────────────────────────────

    def _action_new_project(self, params: dict) -> dict:
        self._auto_save()
        project = QgsProject.instance()
        project.clear()
        crs = params.get("crs", "EPSG:2154")  # Lambert 93 default (France)
        project.setCrs(QgsCoordinateReferenceSystem(crs))
        title = params.get("title", "QgisRemoteMCP Project")
        project.setTitle(title)
        # Enable on-the-fly reprojection so layers in different CRS always display
        from qgis.core import QgsSettings
        QgsSettings().setValue("/Projections/otfTransformAutoEnable", True)
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

    def _auto_adapt_project_crs(self, layer):
        """Auto-adapt project CRS when adding a layer.

        Only changes CRS when project is truly empty (0 layers).
        Otherwise OTF reprojection handles mismatches.
        Changing CRS on raster addition was triggering cascading re-renders.
        """
        project = QgsProject.instance()
        layer_crs = layer.crs()
        if not layer_crs.isValid():
            return
        if len(project.mapLayers()) == 0:
            project.setCrs(layer_crs)
            print(f"[QGISBridge] Project CRS adapted to {layer_crs.authid()} (first layer)")

    def _finalize_add_layer(self, layer) -> dict:
        """Common logic after creating a valid layer: CRS adapt, add to project, zoom if first, build response."""
        project = QgsProject.instance()
        is_first = len(project.mapLayers()) == 0
        self._auto_adapt_project_crs(layer)
        project.addMapLayer(layer)
        # Auto-zoom on first layer
        zoomed = False
        if is_first and self.iface and layer.extent() and not layer.extent().isEmpty():
            self.iface.mapCanvas().setExtent(layer.extent())
            zoomed = True
        # NOTE: canvas.refresh() intentionally NOT called here.
        # Each layer add was triggering a full re-render including
        # WMS GetCapabilities requests, causing crashes with 5+ layers.
        # The canvas refreshes automatically when the event loop resumes.
        # Build enriched response
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
                result["extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
        except Exception:
            pass
        if isinstance(layer, QgsVectorLayer):
            result["feature_count"] = layer.featureCount()
            result["geometry_type"] = layer.geometryType().name if hasattr(layer.geometryType(), 'name') else str(layer.geometryType())
            result["fields"] = [f.name() for f in layer.fields()]
        elif isinstance(layer, QgsRasterLayer):
            result["width"] = layer.width()
            result["height"] = layer.height()
            result["band_count"] = layer.bandCount()
        return result

    def _action_add_vector_layer(self, params: dict) -> dict:
        uri = params.get("uri", "")
        name = params.get("name", "layer")
        provider = params.get("provider", "ogr")  # ogr, WFS, postgres, memory
        layer = QgsVectorLayer(uri, name, provider)
        if not layer.isValid():
            return {"error": f"Invalid layer: {uri}", "provider": provider}
        return self._finalize_add_layer(layer)

    def _action_add_raster_layer(self, params: dict) -> dict:
        uri = params.get("uri", "")
        name = params.get("name", "raster")
        provider = params.get("provider", "gdal")
        layer = QgsRasterLayer(uri, name, provider)
        if not layer.isValid():
            return {"error": f"Invalid raster: {uri}"}
        return self._finalize_add_layer(layer)

    def _canvas_bbox_4326(self):
        """Return current canvas extent as [xmin,ymin,xmax,ymax] in EPSG:4326, or None."""
        try:
            canvas = self.iface.mapCanvas() if self.iface else None
            if not canvas:
                return None
            ext = canvas.extent()
            if ext.isEmpty():
                return None
            proj_crs = QgsProject.instance().crs()
            if proj_crs.authid() != "EPSG:4326":
                xform = QgsCoordinateTransform(
                    proj_crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                    QgsProject.instance())
                ext = xform.transformBoundingBox(ext)
            return [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
        except Exception:
            return None

    def _action_add_wfs_layer(self, params: dict) -> dict:
        url = params.get("url", "")
        typename = params.get("typename", "")
        name = params.get("name", typename.split(":")[-1] if typename else "wfs")
        uri_parts = [f"url='{url}'", f"typename='{typename}'", "pagingEnabled='true'"]

        # Auto-bbox from canvas when none provided
        bbox = params.get("bbox")
        if not bbox:
            bbox = self._canvas_bbox_4326()
        bbox_source = "provided" if params.get("bbox") else "canvas"

        if bbox:
            uri_parts.append(f"bbox='{','.join(str(x) for x in bbox)}'")
        if params.get("srsname"):
            uri_parts.append(f"srsname='{params['srsname']}'")
        if params.get("max_features"):
            uri_parts.append(f"maxNumFeatures='{params['max_features']}'")
        uri = " ".join(uri_parts)
        layer = QgsVectorLayer(uri, name, "WFS")
        if not layer.isValid():
            return {"error": f"Invalid WFS layer: {typename}", "uri": uri}
        if params.get("sql_filter"):
            if not layer.setSubsetString(params["sql_filter"]):
                return {"error": f"Invalid filter: {params['sql_filter']}",
                        "fields": [f.name() for f in layer.fields()]}
        result = self._finalize_add_layer(layer)
        result["bbox_source"] = bbox_source
        if bbox:
            result["bbox_used"] = bbox
        return result

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
        return self._finalize_add_layer(layer)

    def _action_remove_layer(self, params: dict) -> dict:
        self._auto_save()
        layer_id = params.get("layer_id", "")
        layer, err = self._resolve_layer(layer_id)
        if err:
            return err
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
        layer, err = self._resolve_layer(layer_id, vector_only=True)
        if err:
            return err

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
        self._auto_save()
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

        # Serialize result with enriched layer info
        serialized = {}
        for k, v in result.items():
            if isinstance(v, QgsVectorLayer):
                serialized[k] = {"layer_id": v.id(), "name": v.name(),
                                 "feature_count": v.featureCount(),
                                 "crs": v.crs().authid()}
            elif isinstance(v, QgsRasterLayer):
                serialized[k] = {"layer_id": v.id(), "name": v.name(),
                                 "crs": v.crs().authid()}
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
        canvas = self.iface.mapCanvas()
        extent = params.get("extent")  # [xmin, ymin, xmax, ymax]
        crs_param = params.get("crs", "")  # optional CRS of the extent

        if extent:
            rect = QgsRectangle(extent[0], extent[1], extent[2], extent[3])

            # Auto-detect EPSG:4326 coordinates and transform to project CRS
            project_crs = QgsProject.instance().crs()
            source_crs = None
            if crs_param:
                source_crs = QgsCoordinateReferenceSystem(crs_param)
            elif (project_crs.authid() != "EPSG:4326"
                  and -180 <= extent[0] <= 180 and -180 <= extent[2] <= 180
                  and -90 <= extent[1] <= 90 and -90 <= extent[3] <= 90):
                # Heuristic: looks like lon/lat
                source_crs = QgsCoordinateReferenceSystem("EPSG:4326")

            if source_crs and source_crs.isValid() and source_crs != project_crs:
                transform = QgsCoordinateTransform(source_crs, project_crs, QgsProject.instance())
                rect = transform.transformBoundingBox(rect)

            # Buffer for point extents (xmin==xmax or ymin==ymax)
            if rect.width() == 0 or rect.height() == 0:
                # ~500m buffer in projected CRS, ~0.005 deg in geographic CRS
                p_crs = QgsProject.instance().crs()
                buffer = 0.005 if p_crs.isGeographic() else 500
                rect = QgsRectangle(
                    rect.xMinimum() - buffer, rect.yMinimum() - buffer,
                    rect.xMaximum() + buffer, rect.yMaximum() + buffer)

            # Add 5% margin so features don't stick to edges
            rect.scale(1.05)
            canvas.setExtent(rect)
        elif params.get("layer_id"):
            layer, err = self._resolve_layer(params["layer_id"])
            if err:
                return err
            layer_ext = layer.extent()
            if layer_ext.isEmpty():
                return {"error": f"Layer '{layer.name()}' has an empty extent. It may have no features or no spatial data."}
            # Transform layer extent to project CRS if needed
            if layer.crs() != QgsProject.instance().crs():
                transform = QgsCoordinateTransform(layer.crs(), QgsProject.instance().crs(), QgsProject.instance())
                layer_ext = transform.transformBoundingBox(layer_ext)
            # Buffer for point layers
            if layer_ext.width() == 0 or layer_ext.height() == 0:
                p_crs = QgsProject.instance().crs()
                buffer = 0.005 if p_crs.isGeographic() else 500
                layer_ext = QgsRectangle(
                    layer_ext.xMinimum() - buffer, layer_ext.yMinimum() - buffer,
                    layer_ext.xMaximum() + buffer, layer_ext.yMaximum() + buffer)
            layer_ext.scale(1.05)
            canvas.setExtent(layer_ext)
        else:
            canvas.zoomToFullExtent()
        canvas.refresh()
        e = canvas.extent()
        return {"extent": [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()],
                "project_crs": QgsProject.instance().crs().authid()}

    # ── Screenshot ────────────────────────────────────────────────

    def _capture_screenshot(self, width=1920, height=1080):
        """Capture screenshot as JPEG ≤ 1MB. Priority: QgsMapRenderer > Qt screen grab > ffmpeg.

        QgsMapRendererSequentialJob renders directly to a QImage in memory,
        bypassing the X11 framebuffer entirely. This produces correct output
        even when Mesa/llvmpipe doesn't flush the canvas to Xvfb properly.

        Output is always JPEG to keep preview size under 1MB for LLM context.
        Quality is reduced iteratively if the first encode exceeds the limit.
        """
        from qgis.PyQt.QtCore import QBuffer, QIODevice

        PREVIEW_MAX = 1 * 1024 * 1024  # 1MB hard limit for LLM previews

        def _to_jpeg(img_or_pixmap, quality=75):
            """Encode a QImage or QPixmap to JPEG bytes, reducing quality until ≤ 1MB."""
            buf = QBuffer()
            buf.open(QIODevice.WriteOnly)
            img_or_pixmap.save(buf, "JPEG", quality)
            data = bytes(buf.data())
            buf.close()
            # Reduce quality in steps if still too large
            while len(data) > PREVIEW_MAX and quality > 20:
                quality -= 15
                buf = QBuffer()
                buf.open(QIODevice.WriteOnly)
                img_or_pixmap.save(buf, "JPEG", max(quality, 20))
                data = bytes(buf.data())
                buf.close()
            return data

        # Method 1: QgsMapRendererSequentialJob (bypass canvas display)
        # Uses QEventLoop instead of waitForFinished() to avoid blocking
        # the main thread's event loop (which processes network requests
        # for WMS/WMTS tile downloads — would deadlock otherwise).
        try:
            canvas = self.iface.mapCanvas() if self.iface else None
            if canvas and len(QgsProject.instance().mapLayers()) > 0:
                from qgis.core import QgsMapRendererSequentialJob
                settings = canvas.mapSettings()
                settings.setOutputSize(QSize(width, height))
                job = QgsMapRendererSequentialJob(settings)
                job.start()
                # Event loop: processes network events while rendering
                loop = QEventLoop()
                job.finished.connect(loop.quit)
                QTimer.singleShot(15000, loop.quit)  # 15s timeout
                loop.exec_()
                img = job.renderedImage()
                if not img.isNull():
                    data = _to_jpeg(img)
                    if len(data) > 100:
                        return data
        except Exception as e:
            print(f"[QGISBridge] QgsMapRenderer screenshot failed: {e}")

        # Method 2: Qt screen grab (captures full X11 display including UI)
        try:
            from qgis.PyQt.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
            if screen:
                pixmap = screen.grabWindow(0)
                if not pixmap.isNull():
                    if pixmap.width() != width or pixmap.height() != height:
                        from qgis.PyQt.QtCore import Qt
                        pixmap = pixmap.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    data = _to_jpeg(pixmap)
                    if len(data) > 100:
                        return data
        except Exception as e:
            print(f"[QGISBridge] Qt screen grab failed: {e}")

        return None

    def _action_screenshot(self, params: dict) -> dict:
        """Capture the full QGIS desktop."""
        width = params.get("width", 1920)
        height = params.get("height", 1080)

        data = self._capture_screenshot(width, height)

        # Fallback to ffmpeg if Qt fails — capture as JPEG directly
        if not data:
            import subprocess
            display = os.environ.get("DISPLAY", ":99")
            path = f"/tmp/screenshot_{int(time.time())}.jpg"
            try:
                # Capture full display (1920x1080) then scale to requested size, encode as JPEG q=75
                cmd = [
                    "ffmpeg", "-y", "-f", "x11grab",
                    "-video_size", "1920x1080",
                    "-i", display,
                    "-vf", f"scale={width}:{height}",
                    "-frames:v", "1", "-update", "1",
                    "-q:v", "4",  # JPEG quality ~75 in ffmpeg scale (2=best, 31=worst)
                    path
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

        PREVIEW_MAX = 1 * 1024 * 1024  # 1MB
        if len(data) > PREVIEW_MAX:
            return {"error": f"Screenshot too large after compression: {len(data)} bytes"}

        return {
            "image_base64": base64.b64encode(data).decode(),
            "format": "jpeg",
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
                       "download_url": f"http://localhost:{_API_HOST_PORT}/api/files/{Path(output_path).name}"}
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
        layer, err = self._resolve_layer(layer_id)
        if err:
            return err

        qml_path = params.get("qml_path", "")
        if qml_path and os.path.exists(qml_path):
            msg, ok = layer.loadNamedStyle(qml_path)
            layer.triggerRepaint()
            return {"success": ok, "message": msg}

        return {"error": "No QML path provided or file not found"}

    @staticmethod
    def _generate_color_palette(n: int) -> list:
        """Generate n visually distinct colors for categorized/graduated styles."""
        # Curated palette for readability on maps
        base = [
            "228,26,28", "55,126,184", "77,175,74", "152,78,163",
            "255,127,0", "255,255,51", "166,86,40", "247,129,191",
            "153,153,153", "0,128,128", "220,20,60", "70,130,180",
            "34,139,34", "218,112,214", "255,165,0", "64,224,208",
        ]
        if n <= len(base):
            return base[:n]
        # Extend with HSV rotation for more categories
        import colorsys
        colors = list(base)
        for i in range(n - len(base)):
            h = (i * 0.618033988749895) % 1.0  # golden ratio
            r, g, b = colorsys.hsv_to_rgb(h, 0.7, 0.9)
            colors.append(f"{int(r*255)},{int(g*255)},{int(b*255)}")
        return colors

    def _action_set_layer_style(self, params: dict) -> dict:
        """Apply symbology: single, categorized, or graduated.

        Smart defaults:
        - categorized without categories: auto-generates from unique values
        - graduated without ranges: auto-generates equal intervals (5 classes)
        - field validation with available fields in error message
        """
        layer_id = params.get("layer_id", "")
        layer, err = self._resolve_layer(layer_id, vector_only=True)
        if err:
            return err

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
                return {"error": "Categorized style requires 'field' parameter",
                        "available_fields": [f.name() for f in layer.fields()]}
            # Validate field exists
            field_names = [f.name() for f in layer.fields()]
            if field not in field_names:
                return {"error": f"Field '{field}' not found",
                        "available_fields": field_names}

            cats = []
            if categories_def:
                # User-provided categories
                for value, props in categories_def.items():
                    color = props.get("color", "#888888")
                    label = props.get("label", str(value))
                    cats.append(QgsRendererCategory(value, make_symbol(color), label))
            else:
                # Auto-generate from unique values
                idx = layer.fields().indexOf(field)
                unique_values = list(layer.uniqueValues(idx))
                if len(unique_values) > 50:
                    return {"error": f"Field '{field}' has {len(unique_values)} unique values (max 50 for auto-categorize). Provide explicit categories or use graduated style.",
                            "sample_values": [str(v) for v in unique_values[:10]]}
                palette = self._generate_color_palette(len(unique_values))
                for i, value in enumerate(sorted(unique_values, key=lambda x: str(x) if x is not None else "")):
                    cats.append(QgsRendererCategory(value, make_symbol(palette[i]), str(value) if value is not None else "NULL"))
            layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))

        elif style_type == "graduated":
            field = params.get("field", "")
            ranges_def = params.get("ranges", [])
            num_classes = params.get("num_classes", 5)
            if not field:
                return {"error": "Graduated style requires 'field' parameter",
                        "available_fields": [f.name() for f in layer.fields()]}
            # Validate field exists
            field_names = [f.name() for f in layer.fields()]
            if field not in field_names:
                return {"error": f"Field '{field}' not found",
                        "available_fields": field_names}

            ranges = []
            if ranges_def:
                # User-provided ranges
                for r in ranges_def:
                    color = r.get("color", "#888888")
                    label = r.get("label", f"{r['min']}-{r['max']}")
                    ranges.append(QgsRendererRange(r["min"], r["max"], make_symbol(color), label))
            else:
                # Auto-generate equal intervals
                idx = layer.fields().indexOf(field)
                min_val = layer.minimumValue(idx)
                max_val = layer.maximumValue(idx)
                if min_val is None or max_val is None:
                    return {"error": f"Cannot compute min/max for field '{field}'. It may contain only NULL values."}
                try:
                    min_val = float(min_val)
                    max_val = float(max_val)
                except (ValueError, TypeError):
                    return {"error": f"Field '{field}' is not numeric. Use categorized style for text fields.",
                            "field_type": layer.fields().field(idx).typeName()}
                if min_val == max_val:
                    return {"error": f"Field '{field}' has a single value ({min_val}). Cannot create graduated ranges."}
                step = (max_val - min_val) / num_classes
                palette = self._generate_color_palette(num_classes)
                for i in range(num_classes):
                    lo = min_val + i * step
                    hi = min_val + (i + 1) * step
                    label = f"{lo:.1f} - {hi:.1f}"
                    ranges.append(QgsRendererRange(lo, hi, make_symbol(palette[i]), label))
            layer.setRenderer(QgsGraduatedSymbolRenderer(field, ranges))

        else:
            return {"error": f"Unknown style type: {style_type}. Use single, categorized, or graduated."}

        layer.triggerRepaint()
        return {"success": True, "layer_id": layer_id, "style_type": style_type}

    def _action_set_layer_visibility(self, params: dict) -> dict:
        """Toggle layer visibility in the layer tree."""
        layer_id = params.get("layer_id", "")
        visible = params.get("visible", True)
        layer, err = self._resolve_layer(layer_id)
        if err:
            return err
        project = QgsProject.instance()
        node = project.layerTreeRoot().findLayer(layer)
        if not node:
            return {"error": f"Layer not in tree: {layer_id}"}
        node.setItemVisibilityChecked(visible)
        if self.iface:
            self.iface.mapCanvas().refresh()
        return {"success": True, "layer_id": layer_id, "visible": visible}

    # ── Layout Templates ─────────────────────────────────────────

    TEMPLATES_DIR = Path("/app/templates")

    def _action_list_layout_templates(self, params: dict) -> dict:
        """List available print layout templates."""
        templates = []
        if self.TEMPLATES_DIR.is_dir():
            for qpt in sorted(self.TEMPLATES_DIR.glob("*.qpt")):
                templates.append({
                    "id": qpt.stem,
                    "name": qpt.stem.replace("_", " ").title(),
                    "file": str(qpt),
                })
        return {"templates": templates}

    def _action_apply_layout_template(self, params: dict) -> dict:
        """Load a .qpt layout template and configure with project variables."""
        from qgis.PyQt.QtXml import QDomDocument
        from qgis.core import (QgsReadWriteContext, QgsPrintLayout,
                                QgsLayoutItemMap, QgsLayoutItemLegend,
                                QgsLayoutItemScaleBar, QgsExpressionContextUtils)

        template_id = params.get("template_id", params.get("template", ""))
        variables = params.get("variables", {})
        layout_name = params.get("name", "")

        if not template_id:
            return {"error": "Missing 'template_id' parameter (e.g. 'a3_landscape')"}

        template_path = self.TEMPLATES_DIR / f"{template_id}.qpt"
        if not template_path.exists():
            available = [p.stem for p in self.TEMPLATES_DIR.glob("*.qpt")] if self.TEMPLATES_DIR.is_dir() else []
            return {"error": f"Template not found: {template_id}",
                    "available_templates": available}

        # Read template XML
        doc = QDomDocument()
        with open(template_path, encoding="utf-8") as f:
            content = f.read()
        ok, err_msg, err_line, err_col = doc.setContent(content)
        if not ok:
            return {"error": f"Failed to parse template XML: {err_msg} (line {err_line})"}

        project = QgsProject.instance()

        # Set project variables (title, subtitle, etc.)
        for key, value in variables.items():
            QgsExpressionContextUtils.setProjectVariable(project, key, str(value))

        # Create layout
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        context = QgsReadWriteContext()

        items_loaded, ok = layout.loadFromTemplate(doc, context)
        if not ok:
            return {"error": "Failed to load layout from template"}

        # Set layout name
        if not layout_name:
            layout_name = layout.name() or f"Export {template_id.replace('_', ' ').title()}"
        layout.setName(layout_name)

        # Remove existing layout with same name
        existing = project.layoutManager().layoutByName(layout_name)
        if existing:
            project.layoutManager().removeLayout(existing)

        # Link map item to current canvas extent
        canvas = self.iface.mapCanvas() if self.iface else None
        map_items = [item for item in layout.items() if isinstance(item, QgsLayoutItemMap)]
        if map_items and canvas:
            map_item = map_items[0]
            map_item.setExtent(canvas.extent())
            map_item.setCrs(project.crs())

            # Link legend to map
            for item in layout.items():
                if isinstance(item, QgsLayoutItemLegend):
                    item.setLinkedMap(map_item)
                elif isinstance(item, QgsLayoutItemScaleBar):
                    item.setLinkedMap(map_item)

        # Register
        project.layoutManager().addLayout(layout)

        return {
            "success": True,
            "layout_name": layout_name,
            "template": template_id,
            "variables_set": list(variables.keys()),
            "map_items": len(map_items),
        }

    # ── Recipes ───────────────────────────────────────────────────
    # V1.5 Sprint 0 : support multi-sources (system + user).
    # RECIPES_SYSTEM_DIR  : recipes embarquees dans l'image (JSON, backward compat).
    # RECIPES_USER_DIR    : recipes user editables (JSON ou YAML), sur PVC.
    #                       Override via env USER_RECIPES_DIR (injecte par hub
    #                       chart Onyxia avec /data/studies/{sid}/recipes).
    # Priorite : user > system. Si meme id, user gagne (override).
    # Tag `source` dans list_recipes pour distinguer UI.

    RECIPES_SYSTEM_DIR = Path("/app/recipes")
    RECIPES_USER_DIR = Path(os.environ.get(
        "USER_RECIPES_DIR", "/data/studies/recipes"
    ))
    # Legacy alias pour compat code externe qui referencerait RECIPES_DIR
    RECIPES_DIR = RECIPES_SYSTEM_DIR

    @classmethod
    def _load_recipe_file(cls, rpath: "Path") -> dict | None:
        """Charge une recipe depuis un fichier .json ou .yaml. Retourne None si KO."""
        try:
            text = rpath.read_text(encoding="utf-8")
            if rpath.suffix == ".json":
                return json.loads(text)
            elif rpath.suffix in (".yaml", ".yml"):
                try:
                    import yaml  # type: ignore
                    return yaml.safe_load(text)
                except ImportError:
                    # PyYAML non dispo en env QGIS standard -> log + skip
                    print(f"[qgis_bridge] PyYAML manquant, skip {rpath.name}",
                          file=sys.stderr)
                    return None
            return None
        except Exception:
            return None

    @classmethod
    def _iter_recipe_files(cls, directory: "Path") -> "list[Path]":
        """Liste les fichiers recipes (.json + .yaml) tries dans un dossier.

        Skip les fichiers `.archived.<ts>` (soft delete pattern du hub :
        cf. studies.py:delete_recipe_pod_code) : sinon les recipes
        supprimees reapparaissent dans list_recipes apres archive.
        """
        if not directory.is_dir():
            return []
        files = []
        for pattern in ("*.json", "*.yaml", "*.yml"):
            files.extend(directory.glob(pattern))
        # Filtre out les archived
        files = [f for f in files if ".archived." not in f.name]
        return sorted(files)

    def _action_list_recipes(self, params: dict) -> dict:
        """List available workflow recipes (user + system merged, user prioritaire)."""
        recipes = []
        seen_ids = set()
        # 1. User recipes (PVC) prioritaires
        for rpath in self._iter_recipe_files(self.RECIPES_USER_DIR):
            data = self._load_recipe_file(rpath)
            if not data:
                continue
            rid = data.get("id", rpath.stem)
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            recipes.append({
                "id": rid,
                "name": data.get("name", rpath.stem),
                "description": data.get("description", ""),
                "tags": data.get("tags", []),
                "parameters": data.get("parameters", {}),
                "source": "user",
                "format": rpath.suffix.lstrip("."),
            })
        # 2. System recipes (image) en fallback, skip si id deja vu (user override)
        for rpath in self._iter_recipe_files(self.RECIPES_SYSTEM_DIR):
            data = self._load_recipe_file(rpath)
            if not data:
                continue
            rid = data.get("id", rpath.stem)
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            recipes.append({
                "id": rid,
                "name": data.get("name", rpath.stem),
                "description": data.get("description", ""),
                "tags": data.get("tags", []),
                "parameters": data.get("parameters", {}),
                "source": "system",
                "format": rpath.suffix.lstrip("."),
            })
        return {"recipes": recipes}

    def _find_recipe_path(self, recipe_id: str) -> "Path | None":
        """Cherche le fichier d'une recipe (user d'abord, system fallback)."""
        # Essai user (JSON puis YAML)
        for ext in (".json", ".yaml", ".yml"):
            p = self.RECIPES_USER_DIR / f"{recipe_id}{ext}"
            if p.exists():
                return p
        # Essai system (JSON uniquement, backward compat)
        p = self.RECIPES_SYSTEM_DIR / f"{recipe_id}.json"
        if p.exists():
            return p
        return None

    def _action_get_recipe(self, params: dict) -> dict:
        """Get a specific recipe with parameter substitution (user prioritaire)."""
        recipe_id = params.get("id", "")
        if not recipe_id:
            return {"error": "Missing 'id' parameter"}

        recipe_path = self._find_recipe_path(recipe_id)
        if recipe_path is None:
            # Construire la liste des disponibles (user + system)
            all_files = (
                self._iter_recipe_files(self.RECIPES_USER_DIR)
                + self._iter_recipe_files(self.RECIPES_SYSTEM_DIR)
            )
            available = sorted({p.stem for p in all_files})
            return {"error": f"Recipe not found: {recipe_id}",
                    "available_recipes": available}

        data = self._load_recipe_file(recipe_path)
        if data is None:
            return {"error": f"Recipe failed to parse: {recipe_id}",
                    "path": str(recipe_path)}

        # Substitute parameters: $zone → actual value from params
        recipe_params = data.get("parameters", {})
        for key in recipe_params:
            placeholder = f"${key}"
            value = params.get(key, recipe_params[key].get("default", placeholder))
            if value is None:
                value = placeholder
            # Substitute in steps
            data_str = json.dumps(data["steps"])
            data_str = data_str.replace(f'"{placeholder}"', json.dumps(value))
            data_str = data_str.replace(placeholder, str(value))
            data["steps"] = json.loads(data_str)

        return {
            "id": data.get("id"),
            "name": data.get("name"),
            "description": data.get("description"),
            "steps": data.get("steps", []),
            "outputs": data.get("outputs", []),
            "total_steps": len(data.get("steps", [])),
        }

    # ── Web Map Export ────────────────────────────────────────────

    def _action_export_web_map(self, params: dict) -> dict:
        """Export visible vector layers as interactive Leaflet HTML."""
        title = params.get("title", "")
        max_features = params.get("max_features", 5000)
        output_path = params.get("output_path", "")

        project = QgsProject.instance()
        if not title:
            title = project.title() or "Web Map"

        if not output_path:
            output_path = f"/data/webmap_{int(time.time())}.html"

        # Collect visible vector layers
        layer_data = []
        palette = ["#3498db", "#e74c3c", "#2ecc71", "#f39c12", "#9b59b6",
                    "#1abc9c", "#e67e22", "#34495e", "#16a085", "#c0392b"]
        color_idx = 0

        # Canvas extent for initial view
        canvas = self.iface.mapCanvas() if self.iface else None
        center = [0, 0]
        zoom = 13
        if canvas:
            ext = canvas.extent()
            # Transform to 4326 for Leaflet
            tr = QgsCoordinateTransform(project.crs(),
                                         QgsCoordinateReferenceSystem("EPSG:4326"),
                                         project)
            ext_4326 = tr.transformBoundingBox(ext)
            center = [
                (ext_4326.yMinimum() + ext_4326.yMaximum()) / 2,
                (ext_4326.xMinimum() + ext_4326.xMaximum()) / 2,
            ]

        for lid, layer in project.mapLayers().items():
            if not isinstance(layer, QgsVectorLayer):
                continue
            node = project.layerTreeRoot().findLayer(layer)
            if node and not node.isVisible():
                continue
            if layer.featureCount() == 0:
                continue

            # Extract color from renderer (supports single, graduated, categorized)
            color = palette[color_idx % len(palette)]
            color_idx += 1
            renderer = layer.renderer()
            per_feature_colors = {}  # range_key → color for styled renderers
            legend_items = []  # [{label, color}] for graduated/categorized legends
            if renderer:
                if isinstance(renderer, QgsSingleSymbolRenderer):
                    sym = renderer.symbol()
                    if sym:
                        qcolor = sym.color()
                        color = f"#{qcolor.red():02x}{qcolor.green():02x}{qcolor.blue():02x}"
                elif isinstance(renderer, QgsGraduatedSymbolRenderer):
                    field_name = renderer.classAttribute()
                    for rng in renderer.ranges():
                        sym = rng.symbol()
                        if sym:
                            qc = sym.color()
                            hex_color = f"#{qc.red():02x}{qc.green():02x}{qc.blue():02x}"
                            per_feature_colors[f"{rng.lowerValue()}-{rng.upperValue()}"] = hex_color
                            legend_items.append({
                                "label": rng.label() or f"{rng.lowerValue():.0f} - {rng.upperValue():.0f}",
                                "color": hex_color,
                            })
                    ranges_list = renderer.ranges()
                    if ranges_list:
                        mid = ranges_list[len(ranges_list)//2].symbol()
                        if mid:
                            qc = mid.color()
                            color = f"#{qc.red():02x}{qc.green():02x}{qc.blue():02x}"
                elif isinstance(renderer, QgsCategorizedSymbolRenderer):
                    for cat in renderer.categories():
                        sym = cat.symbol()
                        if sym:
                            qc = sym.color()
                            hex_color = f"#{qc.red():02x}{qc.green():02x}{qc.blue():02x}"
                            legend_items.append({
                                "label": str(cat.label()) if cat.label() else str(cat.value()),
                                "color": hex_color,
                            })
                    cats = renderer.categories()
                    if cats:
                        mid = cats[len(cats)//2].symbol()
                        if mid:
                            qc = mid.color()
                            color = f"#{qc.red():02x}{qc.green():02x}{qc.blue():02x}"

            # Export features as GeoJSON
            tr = QgsCoordinateTransform(layer.crs(),
                                         QgsCoordinateReferenceSystem("EPSG:4326"),
                                         project)
            features = []
            for i, feat in enumerate(layer.getFeatures()):
                if i >= max_features:
                    break
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                geom.transform(tr)
                props = {}
                for field in layer.fields():
                    val = feat[field.name()]
                    if val is not None and str(val) != "NULL":
                        props[field.name()] = val
                # Add per-feature color for graduated/categorized renderer
                if renderer:
                    if isinstance(renderer, QgsGraduatedSymbolRenderer):
                        field_name = renderer.classAttribute()
                        val = feat[field_name] if field_name else None
                        if val is not None:
                            try:
                                fval = float(val)
                                for rng in renderer.ranges():
                                    if rng.lowerValue() <= fval <= rng.upperValue():
                                        qc = rng.symbol().color()
                                        props["_color"] = f"#{qc.red():02x}{qc.green():02x}{qc.blue():02x}"
                                        break
                            except (ValueError, TypeError):
                                pass
                    elif isinstance(renderer, QgsCategorizedSymbolRenderer):
                        field_name = renderer.classAttribute()
                        val = feat[field_name] if field_name else None
                        if val is not None:
                            for cat in renderer.categories():
                                if str(cat.value()) == str(val):
                                    qc = cat.symbol().color()
                                    props["_color"] = f"#{qc.red():02x}{qc.green():02x}{qc.blue():02x}"
                                    break
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(geom.asJson()),
                    "properties": props,
                })

            if features:
                ld = {
                    "name": layer.name(),
                    "color": color,
                    "geojson": {"type": "FeatureCollection", "features": features},
                    "feature_count": len(features),
                    "has_feature_colors": bool(per_feature_colors),
                }
                if legend_items:
                    ld["legend"] = legend_items
                layer_data.append(ld)

        if not layer_data:
            return {"error": "No visible vector layers with features to export"}

        # Read template
        template_path = Path("/app/templates/web/leaflet_template.html")
        if not template_path.exists():
            return {"error": "Leaflet template not found at /app/templates/web/leaflet_template.html"}

        template = template_path.read_text(encoding="utf-8")

        # Substitute
        html = template.replace("{{TITLE}}", title)
        html = html.replace("{{CENTER}}", json.dumps(center))
        html = html.replace("{{ZOOM}}", str(zoom))
        html = html.replace("{{LAYERS_JSON}}", json.dumps(layer_data, default=str))

        # Write output
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(html, encoding="utf-8")

        size = Path(output_path).stat().st_size
        fname = Path(output_path).name
        return {
            "success": True,
            "path": output_path,
            "download_url": f"http://localhost:{_API_HOST_PORT}/api/files/{fname}",
            "size_bytes": size,
            "layers_exported": len(layer_data),
            "title": title,
        }

    # ── Interactive Flood Map Export ─────────────────────────────

    def _action_export_flood_map(self, params: dict) -> dict:
        """Export an interactive flood simulation as a Leaflet HTML page.

        Expects ISO_HT (water depth), buildings, and optionally sensitive facilities
        layers already loaded in the project. Pre-computes building exposure by
        spatial intersection with flood polygons, then injects into the flood template.
        """
        title = params.get("title", "")
        output_path = params.get("output_path", "")
        max_features = params.get("max_features", 10000)
        # Optional field filter — list of field names to include in GeoJSON output.
        # When set, only these fields are exported (reduces file size).
        # Extra props (max_water_height, ht_min_exposure, area_ha) are always added.
        include_fields_param = params.get("include_fields")
        include_fields = set(include_fields_param) if include_fields_param else None

        project = QgsProject.instance()

        # Study zone name from project variables
        from qgis.core import QgsExpressionContextUtils
        scope = QgsExpressionContextUtils.projectScope(project)
        zone_name = scope.variable("study_zone_name") or "Zone d'étude"

        if not title:
            title = f"Simulation inondation — {zone_name}"
        if not output_path:
            output_path = f"/data/flood_map_{int(time.time())}.html"

        # ── Find layers by keywords ──
        def find_layers(*keywords):
            results = []
            for l in project.mapLayers().values():
                if not isinstance(l, QgsVectorLayer) or l.featureCount() == 0:
                    continue
                name_lower = l.name().lower()
                if any(k in name_lower for k in keywords):
                    results.append(l)
            return results

        # ISO_HT: flood depth polygons with ht_min/ht_max
        flood_layers = find_layers('hauteur', 'iso_ht', 'depth')
        # Also try the T10/T100/T1000 flood extent layers (ALEA_SYNT)
        extent_layers = find_layers('alea', 'inondab', 'centennale', 'frequente', 'extreme', 't100', 't10', 't1000')
        # Buildings
        building_layers = find_layers('timent')
        # Sensitive facilities
        sensitive_layers = find_layers('sensible', 'enjeux')

        # Determine which layers have ht_min/ht_max fields
        all_flood = flood_layers + [l for l in extent_layers if l not in flood_layers]
        iso_ht_layers = []
        extent_only_layers = []
        for l in all_flood:
            field_names = [f.name().lower() for f in l.fields()]
            if any('ht_min' in fn or 'ht_max' in fn for fn in field_names):
                iso_ht_layers.append(l)
            else:
                extent_only_layers.append(l)

        if not iso_ht_layers and not extent_only_layers:
            return {
                "error": "No flood layers found. Load ISO_HT or ALEA_SYNT layers first "
                         "(e.g. smart_load tri_hauteurs_eau_t100, smart_load tri_inondation_t100)."
            }

        if not building_layers:
            return {"error": "No building layers found. Load buildings first (smart_load bdtopo_batiments)."}

        # ── Transform to EPSG:4326 for Leaflet ──
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")

        # Max realistic water depth — values above this (e.g. 9999) are sentinel "unbounded"
        HT_MAX_CAP = 5.0

        def to_geojson_features(layer, limit, extra_props_fn=None, fields_filter=None, simplify=None):
            """Export layer features as GeoJSON dicts in EPSG:4326.
            fields_filter: optional set of field names to include (None = all).
            simplify: if float > 0, simplify geometry to this tolerance (degrees EPSG:4326)."""
            tr = QgsCoordinateTransform(layer.crs(), crs_4326, project)
            features = []
            for i, feat in enumerate(layer.getFeatures()):
                if i >= limit:
                    break
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                geom.transform(tr)
                if simplify and simplify > 0:
                    simp = geom.simplify(simplify)
                    if simp and not simp.isEmpty():
                        geom = simp
                props = {}
                for field in layer.fields():
                    fname = field.name()
                    if fields_filter and fname not in fields_filter:
                        continue
                    val = feat[fname]
                    if val is not None and str(val) != "NULL":
                        try:
                            json.dumps(val)
                            props[fname] = val
                        except (TypeError, ValueError):
                            props[fname] = str(val)
                if extra_props_fn:
                    extra_props_fn(feat, props)
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(geom.asJson()),
                    "properties": props,
                })
            return features

        # ── Export flood (ISO_HT) data ──
        # Merge all ISO_HT layers into one GeoJSON, computing area_ha
        flood_features = []
        max_height = 0.0

        from qgis.core import QgsDistanceArea
        da = QgsDistanceArea()
        da.setEllipsoid('WGS84')

        for layer in iso_ht_layers:
            da.setSourceCrs(layer.crs(), project.transformContext())
            field_names_lower = {f.name().lower(): f.name() for f in layer.fields()}

            # Find ht_min and ht_max field names (case-insensitive)
            ht_min_field = None
            ht_max_field = None
            for fn_lower, fn_actual in field_names_lower.items():
                if 'ht_min' in fn_lower:
                    ht_min_field = fn_actual
                if 'ht_max' in fn_lower:
                    ht_max_field = fn_actual

            def add_flood_props(feat, props):
                nonlocal max_height
                # Compute area in hectares from original geometry (before transform)
                geom_orig = feat.geometry()
                if geom_orig and not geom_orig.isEmpty() and geom_orig.type() == 2:
                    props["area_ha"] = round(da.measureArea(geom_orig) / 10000, 2)
                else:
                    props["area_ha"] = 0

                # Normalize ht_min / ht_max
                ht_min = 0
                ht_max = 0
                if ht_min_field:
                    try:
                        ht_min = float(feat[ht_min_field] or 0)
                    except (ValueError, TypeError):
                        ht_min = 0
                if ht_max_field:
                    try:
                        ht_max = float(feat[ht_max_field] or 0)
                    except (ValueError, TypeError):
                        ht_max = 0
                # Cap sentinel values (Georisques uses 9999 for "> 2m" class)
                if ht_max > HT_MAX_CAP:
                    ht_max = HT_MAX_CAP
                props["ht_min"] = ht_min
                props["ht_max"] = ht_max
                if ht_max > max_height:
                    max_height = ht_max

            flood_features.extend(to_geojson_features(
                layer, max_features, add_flood_props, fields_filter=include_fields, simplify=0.00005))

        # If no ISO_HT but we have extent layers, create synthetic flood polygons
        if not flood_features and extent_only_layers:
            # Assign approximate water heights based on return period
            for layer in extent_only_layers:
                name_lower = layer.name().lower()
                if any(k in name_lower for k in ['t10', 'frequente', '01_01']):
                    synth_ht_min, synth_ht_max = 0.0, 0.5
                elif any(k in name_lower for k in ['t1000', 'extreme', '01_04']):
                    synth_ht_min, synth_ht_max = 1.0, 3.0
                else:  # T100 / default
                    synth_ht_min, synth_ht_max = 0.5, 2.0

                da.setSourceCrs(layer.crs(), project.transformContext())

                def add_synth_props(feat, props, _min=synth_ht_min, _max=synth_ht_max):
                    nonlocal max_height
                    geom_orig = feat.geometry()
                    if geom_orig and not geom_orig.isEmpty() and geom_orig.type() == 2:
                        props["area_ha"] = round(da.measureArea(geom_orig) / 10000, 2)
                    else:
                        props["area_ha"] = 0
                    props["ht_min"] = _min
                    props["ht_max"] = _max
                    if _max > max_height:
                        max_height = _max

                flood_features.extend(to_geojson_features(layer, max_features, add_synth_props, simplify=0.00005))

        flood_geojson = {"type": "FeatureCollection", "features": flood_features}

        # ── Build spatial index of flood polygons for building intersection ──
        # Use a simple in-memory approach: for each building centroid, find overlapping flood polygons
        from qgis.core import QgsSpatialIndex, QgsGeometry

        # Create a combined flood layer in project CRS for spatial ops
        # We'll work with the original layer features (not the reprojected ones)
        flood_index = QgsSpatialIndex()
        flood_feat_map = {}  # fid → (ht_min, ht_max, geometry)
        global_fid = 0

        for layer in (iso_ht_layers or extent_only_layers):
            field_names_lower = {f.name().lower(): f.name() for f in layer.fields()}
            ht_min_field = None
            ht_max_field = None
            for fn_lower, fn_actual in field_names_lower.items():
                if 'ht_min' in fn_lower:
                    ht_min_field = fn_actual
                if 'ht_max' in fn_lower:
                    ht_max_field = fn_actual

            # Determine synthetic values for extent-only layers
            name_lower = layer.name().lower()
            synth_min, synth_max = 0.5, 2.0
            if any(k in name_lower for k in ['t10', 'frequente', '01_01']):
                synth_min, synth_max = 0.0, 0.5
            elif any(k in name_lower for k in ['t1000', 'extreme', '01_04']):
                synth_min, synth_max = 1.0, 3.0

            # Transform to building layer CRS for intersection
            building_crs = building_layers[0].crs()
            tr_to_bld = QgsCoordinateTransform(layer.crs(), building_crs, project)

            for feat in layer.getFeatures():
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                geom_copy = QgsGeometry(geom)
                geom_copy.transform(tr_to_bld)

                ht_min = synth_min
                ht_max = synth_max
                if ht_min_field:
                    try:
                        ht_min = float(feat[ht_min_field] or 0)
                    except (ValueError, TypeError):
                        pass
                if ht_max_field:
                    try:
                        ht_max = float(feat[ht_max_field] or 0)
                    except (ValueError, TypeError):
                        pass
                # Cap sentinel values
                if ht_max > HT_MAX_CAP:
                    ht_max = HT_MAX_CAP

                from qgis.core import QgsFeature
                idx_feat = QgsFeature(global_fid)
                idx_feat.setGeometry(geom_copy)
                flood_index.addFeature(idx_feat)
                flood_feat_map[global_fid] = (ht_min, ht_max, geom_copy)
                global_fid += 1

        # ── Export buildings with pre-computed exposure ──
        building_layer = building_layers[0]
        building_features = []

        def add_building_exposure(feat, props):
            """For each building, find the max water height from overlapping flood polygons."""
            geom = feat.geometry()
            if geom.isEmpty():
                return
            # Query spatial index
            candidates = flood_index.intersects(geom.boundingBox())
            max_wh = 0.0
            min_exposure = 999.0
            for cand_fid in candidates:
                if cand_fid not in flood_feat_map:
                    continue
                f_ht_min, f_ht_max, f_geom = flood_feat_map[cand_fid]
                if geom.intersects(f_geom):
                    if f_ht_max > max_wh:
                        max_wh = f_ht_max
                    if f_ht_min < min_exposure:
                        min_exposure = f_ht_min
            if max_wh > 0:
                props["max_water_height"] = round(max_wh, 2)
                props["ht_min_exposure"] = round(min_exposure, 2)

        building_features = to_geojson_features(
            building_layer, max_features, add_building_exposure, fields_filter=include_fields)
        buildings_geojson = {"type": "FeatureCollection", "features": building_features}

        # ── Export sensitive facilities with exposure ──
        sensitive_features = []
        if sensitive_layers:
            sens_layer = sensitive_layers[0]
            tr_sens_to_bld = QgsCoordinateTransform(sens_layer.crs(), building_crs, project)

            def add_sens_exposure(feat, props):
                geom = feat.geometry()
                if geom.isEmpty():
                    return
                # Transform to building CRS for flood intersection
                geom_bld = QgsGeometry(geom)
                geom_bld.transform(tr_sens_to_bld)
                candidates = flood_index.intersects(geom_bld.boundingBox())
                max_wh = 0.0
                min_exposure = 999.0
                for cand_fid in candidates:
                    if cand_fid not in flood_feat_map:
                        continue
                    f_ht_min, f_ht_max, f_geom = flood_feat_map[cand_fid]
                    if geom_bld.intersects(f_geom):
                        if f_ht_max > max_wh:
                            max_wh = f_ht_max
                        if f_ht_min < min_exposure:
                            min_exposure = f_ht_min
                if max_wh > 0:
                    props["max_water_height"] = round(max_wh, 2)
                    props["ht_min_exposure"] = round(min_exposure, 2)

            sensitive_features = to_geojson_features(
                sens_layer, 1000, add_sens_exposure, fields_filter=include_fields)

        sensitive_geojson = {"type": "FeatureCollection", "features": sensitive_features}

        # ── Routing zones: per-scenario flood extents for Valhalla exclude_polygons ──
        # Uses ALEA_SYNT binary extent layers (simpler than ISO_HT depth classes).
        # Simplified geometries (~11m tolerance) reduce JSON payload.
        routing_zones = {}

        def _detect_flood_scenario(name_lower):
            """Map layer name to flood scenario key (t10 / t100 / t1000)."""
            # Check most specific first to avoid substring collision (t10 ⊂ t100 ⊂ t1000)
            if 't1000' in name_lower or 'treme' in name_lower or '01_04' in name_lower:
                return 't1000'
            if 't100' in name_lower or 'centennale' in name_lower or '01_02' in name_lower:
                return 't100'
            if 't10' in name_lower or 'quente' in name_lower or '01_01' in name_lower:
                return 't10'
            return None

        for layer in (extent_only_layers if extent_only_layers else iso_ht_layers):
            sc = _detect_flood_scenario(layer.name().lower())
            if sc is None or sc in routing_zones:
                continue
            tr_sc = QgsCoordinateTransform(layer.crs(), crs_4326, project)
            sc_features = []
            for feat in layer.getFeatures():
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                geom_copy = QgsGeometry(geom)
                geom_copy.transform(tr_sc)
                simple = geom_copy.simplify(0.0001)  # ~11m at equator
                target_geom = simple if (simple and not simple.isEmpty()) else geom_copy
                sc_features.append({
                    "type": "Feature",
                    "geometry": json.loads(target_geom.asJson()),
                    "properties": {}
                })
            if sc_features:
                routing_zones[sc] = {"type": "FeatureCollection", "features": sc_features}

        # ── Find and export routes with scenario depth fields ──
        route_layers = find_layers('route', 'troncon')
        route_layer_export = route_layers[0] if route_layers else None
        routes_geojson = {"type": "FeatureCollection", "features": []}

        if route_layer_export:
            route_field_names = {f.name() for f in route_layer_export.fields()}
            # Always export: ht_num fields (all scenarios) + basic road attributes
            route_include = {fn for fn in route_field_names
                             if fn in ('ht_num', 'nature', 'importance', 'sens_de_circulation')
                             or fn.startswith('ht_num_')}
            route_features = to_geojson_features(
                route_layer_export, max_features,
                fields_filter=route_include if route_include else None)
            routes_geojson = {"type": "FeatureCollection", "features": route_features}

        # ── Detect available scenarios from building layer fields ──
        bld_field_names_lower = [f.name().lower() for f in building_layer.fields()]
        available_scenarios = [sc for sc in ('t10', 't100', 't1000')
                               if f'{sc}_ht_max' in bld_field_names_lower]
        if not available_scenarios:
            available_scenarios = ['t100']

        # ── Canvas center for map view ──
        canvas = self.iface.mapCanvas() if self.iface else None
        center = [0, 0]
        zoom = 14
        if canvas:
            ext = canvas.extent()
            tr_view = QgsCoordinateTransform(project.crs(), crs_4326, project)
            ext_4326 = tr_view.transformBoundingBox(ext)
            center = [
                (ext_4326.yMinimum() + ext_4326.yMaximum()) / 2,
                (ext_4326.xMinimum() + ext_4326.xMaximum()) / 2,
            ]

        # Ensure max_height has a sensible value
        if max_height <= 0:
            max_height = 3.0

        # ── Load and fill template ──
        template_path = Path("/app/templates/web/leaflet_flood_template.html")
        if not template_path.exists():
            return {"error": "Flood template not found at /app/templates/web/leaflet_flood_template.html"}

        template = template_path.read_text(encoding="utf-8")

        # V1.1 — Metadata DSFR (branding institutionnel, tracabilite, meta HTML5)
        import datetime, os as _os
        analysis_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
        recipe_id = params.get("recipe_id", "risque_inondation")
        recipe_version = params.get("recipe_version", "1.0")
        recipe_sha = (
            params.get("recipe_sha")
            or _os.environ.get("GIT_COMMIT_SHA", "")[:12]
            or "n/a"
        )
        operator_sub = params.get("operator_sub", "Service géospatial")
        # Description SEO/social — exposes/bldgs si dispo, sinon generique
        meta_desc = params.get(
            "meta_description",
            f"Simulation de risque inondation sur {zone_name}. Données réglementaires "
            f"Directive Inondation (Géorisques TRI). Cartographie interactive des "
            f"zones de crue T10/T100/T1000, bâtiments exposés, routes impactées et "
            f"établissements sensibles. Réalisé par le CEREMA via QGIS Cloud."
        )

        html = template.replace("{{TITLE}}", title)
        html = html.replace("{{CENTER}}", json.dumps(center))
        html = html.replace("{{ZOOM}}", str(zoom))
        html = html.replace("{{FLOOD_JSON}}", json.dumps(flood_geojson, default=str))
        html = html.replace("{{BUILDINGS_JSON}}", json.dumps(buildings_geojson, default=str))
        html = html.replace("{{SENSITIVE_JSON}}", json.dumps(sensitive_geojson, default=str))
        html = html.replace("{{ROUTES_JSON}}", json.dumps(routes_geojson, default=str))
        html = html.replace("{{SCENARIOS}}", json.dumps(available_scenarios))
        html = html.replace("{{MAX_HEIGHT}}", str(round(max_height, 1)))
        html = html.replace("{{ZONE_NAME}}", zone_name)
        html = html.replace("{{ROUTING_ZONES_JSON}}", json.dumps(routing_zones, default=str))
        # V1.1 placeholders
        html = html.replace("{{OPERATOR_SUB}}", operator_sub)
        html = html.replace("{{META_DESCRIPTION}}", meta_desc.replace('"', '&quot;'))
        html = html.replace("{{ANALYSIS_DATE}}", analysis_date)
        html = html.replace("{{RECIPE_ID}}", recipe_id)
        html = html.replace("{{RECIPE_VERSION}}", recipe_version)
        html = html.replace("{{RECIPE_SHA}}", recipe_sha)

        # Write output
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(html, encoding="utf-8")

        size = Path(output_path).stat().st_size
        fname = Path(output_path).name

        # Stats
        exposed_buildings = sum(
            1 for f in building_features
            if f["properties"].get("max_water_height", 0) > 0
        )
        exposed_sensitive = sum(
            1 for f in sensitive_features
            if f["properties"].get("max_water_height", 0) > 0
        )

        return {
            "success": True,
            "path": output_path,
            "download_url": f"http://localhost:{_API_HOST_PORT}/api/files/{fname}",
            "size_bytes": size,
            "title": title,
            "zone": zone_name,
            "scenarios": available_scenarios,
            "max_water_height_m": round(max_height, 1),
            "flood_polygons": len(flood_features),
            "buildings": len(building_features),
            "buildings_exposed": exposed_buildings,
            "buildings_pct": round(exposed_buildings / max(len(building_features), 1) * 100, 1),
            "routes": len(routes_geojson["features"]),
            "sensitive_facilities": len(sensitive_features),
            "sensitive_exposed": exposed_sensitive,
        }

    # ── Temporal Map Export ─────────────────────────────────────

    def _action_export_temporal_map(self, params: dict) -> dict:
        """Export an interactive temporal analysis as a Leaflet HTML page.

        Generic action for any time-series point data with optional spatial bands.
        Produces a year slider, animated playback, color-coded points by value,
        and dynamic statistics per time period and spatial band.
        """
        title = params.get("title", "")
        output_path = params.get("output_path", "")
        max_features = params.get("max_features", 15000)

        # Layer identification keywords (generic — not DVF-specific)
        point_kw = params.get("point_layer_keyword", "dvf")
        band_kw = params.get("band_layer_keyword", "bande")
        extra_kws = params.get("extra_polygon_keywords", ["submersion"])

        # Field configuration
        temporal_field = params.get("temporal_field", "year")
        value_field = params.get("value_field", "price_m2")
        band_field = params.get("band_field", "coastal_band")

        include_fields_param = params.get("include_fields")
        include_fields = set(include_fields_param) if include_fields_param else None

        project = QgsProject.instance()

        # Study zone name
        from qgis.core import QgsExpressionContextUtils
        scope = QgsExpressionContextUtils.projectScope(project)
        zone_name = scope.variable("study_zone_name") or "Zone d'étude"

        if not title:
            title = f"Pression foncière — {zone_name}"
        if not output_path:
            output_path = f"/data/temporal_map_{int(time.time())}.html"

        # ── Find layers ──
        def find_layers(*keywords):
            results = []
            for l in project.mapLayers().values():
                if not isinstance(l, QgsVectorLayer) or l.featureCount() == 0:
                    continue
                name_lower = l.name().lower()
                if any(k in name_lower for k in keywords):
                    results.append(l)
            return results

        point_layers = find_layers(point_kw)
        band_layers = find_layers(band_kw)
        extra_layers = find_layers(*extra_kws) if extra_kws else []

        if not point_layers:
            return {"error": f"No point layer found (keyword: '{point_kw}'). Load data first."}

        # ── Transform to EPSG:4326 ──
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")

        def to_geojson_features(layer, limit, extra_props_fn=None, fields_filter=None):
            tr = QgsCoordinateTransform(layer.crs(), crs_4326, project)
            features = []
            for i, feat in enumerate(layer.getFeatures()):
                if i >= limit:
                    break
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                geom.transform(tr)
                props = {}
                for field in layer.fields():
                    fname = field.name()
                    if fields_filter and fname not in fields_filter:
                        continue
                    val = feat[fname]
                    if val is not None and str(val) != "NULL":
                        try:
                            json.dumps(val)
                            props[fname] = val
                        except (TypeError, ValueError):
                            props[fname] = str(val)
                if extra_props_fn:
                    extra_props_fn(feat, props)
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(geom.asJson()),
                    "properties": props,
                })
            return features

        # ── Export point data ──
        # Always include temporal, value, and band fields
        always_fields = {temporal_field, value_field, band_field,
                         "type_local", "date_mutation", "price", "surface"}
        export_fields = (include_fields | always_fields) if include_fields else None

        point_features = []
        for layer in point_layers:
            point_features.extend(to_geojson_features(layer, max_features, fields_filter=export_fields))

        transactions_geojson = {"type": "FeatureCollection", "features": point_features}

        # ── Export bands ──
        band_features = []
        for layer in band_layers:
            band_features.extend(to_geojson_features(layer, 500))
        bands_geojson = {"type": "FeatureCollection", "features": band_features}

        # ── Export extra polygons (submersion, etc.) ──
        extra_features = []
        for layer in extra_layers:
            extra_features.extend(to_geojson_features(layer, 2000))
        extra_geojson = {"type": "FeatureCollection", "features": extra_features}

        # ── Compute statistics per year × band ──
        from collections import defaultdict
        year_band_values = defaultdict(lambda: defaultdict(list))
        all_years = set()

        for f in point_features:
            p = f.get("properties", {})
            year = p.get(temporal_field)
            val = p.get(value_field)
            band = p.get(band_field, "Hors bande")
            if year is not None and val is not None:
                try:
                    year = int(year)
                    val = float(val)
                    all_years.add(year)
                    year_band_values[year][band].append(val)
                except (ValueError, TypeError):
                    pass

        years_list = sorted(all_years) or [2024]

        stats = {}
        for year in years_list:
            year_stats = {}
            for band, values in year_band_values[year].items():
                values_sorted = sorted(values)
                n = len(values_sorted)
                year_stats[band] = {
                    "count": n,
                    "median_m2": round(values_sorted[n // 2], 0) if n else 0,
                    "mean_m2": round(sum(values) / n, 0) if n else 0,
                    "min_m2": round(min(values), 0) if n else 0,
                    "max_m2": round(max(values), 0) if n else 0,
                }
            stats[str(year)] = year_stats

        # ── Canvas center ──
        canvas = self.iface.mapCanvas() if self.iface else None
        center = [0, 0]
        zoom_level = 13
        if canvas:
            ext = canvas.extent()
            tr_view = QgsCoordinateTransform(project.crs(), crs_4326, project)
            ext_4326 = tr_view.transformBoundingBox(ext)
            center = [
                (ext_4326.yMinimum() + ext_4326.yMaximum()) / 2,
                (ext_4326.xMinimum() + ext_4326.xMaximum()) / 2,
            ]

        # ── Load and fill template ──
        template_path = Path("/app/templates/web/leaflet_temporal_template.html")
        if not template_path.exists():
            return {"error": "Temporal template not found at /app/templates/web/leaflet_temporal_template.html"}

        template = template_path.read_text(encoding="utf-8")

        html = template.replace("{{TITLE}}", title)
        html = html.replace("{{CENTER}}", json.dumps(center))
        html = html.replace("{{ZOOM}}", str(zoom_level))
        html = html.replace("{{TRANSACTIONS_JSON}}", json.dumps(transactions_geojson, default=str))
        html = html.replace("{{BANDS_JSON}}", json.dumps(bands_geojson, default=str))
        html = html.replace("{{SUBMERSION_JSON}}", json.dumps(extra_geojson, default=str))
        html = html.replace("{{YEARS_JSON}}", json.dumps(years_list))
        html = html.replace("{{STATS_JSON}}", json.dumps(stats, default=str))
        html = html.replace("{{ZONE_NAME}}", zone_name)

        # Write output
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(html, encoding="utf-8")

        size = Path(output_path).stat().st_size
        fname = Path(output_path).name

        return {
            "success": True,
            "path": output_path,
            "download_url": f"http://localhost:{_API_HOST_PORT}/api/files/{fname}",
            "size": size,
            "title": title,
            "zone": zone_name,
            "years": years_list,
            "total_points": len(point_features),
            "bands": len(band_features),
            "extra_polygons": len(extra_features),
            "stats_summary": stats,
        }

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
        """List files in /data/ directory."""
        directories = ["/data"]
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
        """Read file content as base64. Restricted to /data/."""
        path = params.get("path", "")
        if not path:
            return {"error": "No path provided"}
        fpath = Path(path)
        allowed = [Path("/data")]
        try:
            resolved = fpath.resolve()
            if not any(str(resolved).startswith(str(d.resolve())) for d in allowed):
                return {"error": f"Access denied: {path} is not in /data/"}
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
            result["download_url"] = f"http://localhost:{_API_HOST_PORT}/api/files/{fpath.name}"
        else:
            result["content_base64"] = base64.b64encode(fpath.read_bytes()).decode()
        return result

    def _action_delete_file(self, params: dict) -> dict:
        """Delete a file. Restricted to /data/."""
        path = params.get("path", "")
        if not path:
            return {"error": "No path provided"}
        fpath = Path(path)
        allowed = [Path("/data")]
        try:
            resolved = fpath.resolve()
            if not any(str(resolved).startswith(str(d.resolve())) for d in allowed):
                return {"error": "Access denied: can only delete files in /data/"}
        except Exception:
            return {"error": f"Invalid path: {path}"}
        if not fpath.exists():
            return {"error": f"File not found: {path}"}
        fpath.unlink()
        return {"success": True, "deleted": fpath.name}

    def _action_export_layer(self, params: dict) -> dict:
        """Export a vector layer to file (GPKG, GeoJSON, Shapefile, CSV)."""
        layer_id = params.get("layer_id", "")
        layer, err = self._resolve_layer(layer_id, vector_only=True)
        if err:
            return err

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
            "download_url": f"http://localhost:{_API_HOST_PORT}/api/files/{name}",
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
            "download_url": f"http://localhost:{_API_HOST_PORT}/api/files/{name}",
        }

    # ── QField Export ────────────────────────────────────────────

    def _action_export_qfield(self, params: dict) -> dict:
        """Export current project as a QField-ready ZIP package.

        Creates a portable package containing:
        - .qgz project with relative GPKG sources
        - All vector layers materialized as individual GPKGs
        - Optional editable Observations layer with form widgets
        """
        import zipfile
        import shutil
        import re as _re

        project = QgsProject.instance()
        project_name = params.get("project_name", project.baseName() or "qfield_project")
        # Sanitize project name
        project_name = _re.sub(r'[^\w\-]', '_', project_name).strip('_') or "qfield_project"
        include_obs = params.get("include_observations_layer", True)
        max_feat = params.get("max_features_per_layer", 50000)

        ts = int(time.time())
        pkg_dir = f"/data/qfield_{project_name}_{ts}"
        data_dir = os.path.join(pkg_dir, "data")
        os.makedirs(data_dir, exist_ok=True)

        # ── 1. Materialize each vector layer → GPKG ──────────────
        exported_layers = {}  # layer_id → {original_name, gpkg_name, features}
        used_names = set()

        for layer in project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.featureCount() == 0:
                continue

            # Generate unique GPKG filename
            base = _re.sub(r'[^\w\-]', '_', layer.name()).strip('_').lower() or "layer"
            gpkg_name = base + ".gpkg"
            counter = 1
            while gpkg_name in used_names:
                gpkg_name = f"{base}_{counter}.gpkg"
                counter += 1
            used_names.add(gpkg_name)
            gpkg_path = os.path.join(data_dir, gpkg_name)

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.fileEncoding = "UTF-8"
            error = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, gpkg_path, project.transformContext(), options
            )
            if error[0] == QgsVectorFileWriter.WriterError.NoError:
                exported_layers[layer.id()] = {
                    "original_name": layer.name(),
                    "gpkg_name": gpkg_name,
                    "features": layer.featureCount(),
                }

        if not exported_layers:
            shutil.rmtree(pkg_dir, ignore_errors=True)
            return {"error": "No vector layers with features to export"}

        # ── 2. Create editable Observations layer ─────────────────
        obs_layer = None
        if include_obs:
            obs_path = os.path.join(data_dir, "observations.gpkg")
            obs_fields = QgsFields()
            obs_fields.append(QgsField("id", QVariant.Int))
            obs_fields.append(QgsField("titre", QVariant.String, len=200))
            obs_fields.append(QgsField("description", QVariant.String, len=2000))
            obs_fields.append(QgsField("categorie", QVariant.String, len=50))
            obs_fields.append(QgsField("date_observation", QVariant.String, len=20))
            obs_fields.append(QgsField("photo", QVariant.String, len=500))
            obs_fields.append(QgsField("priorite", QVariant.String, len=20))

            # Determine CRS — use project CRS or fallback EPSG:4326
            crs = project.crs() if project.crs().isValid() else QgsCoordinateReferenceSystem("EPSG:4326")

            writer = QgsVectorFileWriter(
                obs_path, "UTF-8", obs_fields,
                QgsWkbTypes.Point, crs, "GPKG"
            )
            if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
                include_obs = False
            else:
                del writer  # Flush & close

                # Load the layer back to configure widgets
                obs_layer = QgsVectorLayer(obs_path, "Observations", "ogr")
                if obs_layer.isValid():
                    # titre — TextEdit
                    idx = obs_layer.fields().indexOf("titre")
                    if idx >= 0:
                        obs_layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("TextEdit", {}))

                    # description — TextEdit multiline
                    idx = obs_layer.fields().indexOf("description")
                    if idx >= 0:
                        obs_layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("TextEdit", {"IsMultiline": True}))

                    # categorie — ValueMap dropdown
                    idx = obs_layer.fields().indexOf("categorie")
                    if idx >= 0:
                        obs_layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ValueMap", {
                            "map": [
                                {"Anomalie": "anomalie"},
                                {"Point d'interet": "poi"},
                                {"Mesure terrain": "mesure"},
                                {"Risque identifie": "risque"},
                                {"Autre": "autre"},
                            ]
                        }))

                    # date_observation — DateTime
                    idx = obs_layer.fields().indexOf("date_observation")
                    if idx >= 0:
                        obs_layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("DateTime", {
                            "display_format": "yyyy-MM-dd HH:mm",
                            "field_format": "yyyy-MM-dd HH:mm:ss",
                            "calendar_popup": True,
                        }))

                    # photo — ExternalResource (camera/gallery in QField)
                    idx = obs_layer.fields().indexOf("photo")
                    if idx >= 0:
                        obs_layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ExternalResource", {
                            "DocumentViewer": 1,
                            "RelativeStorage": 1,
                            "StorageMode": 0,
                            "FileWidget": True,
                            "FileWidgetFilter": "Images (*.jpg *.jpeg *.png)",
                        }))

                    # priorite — ValueMap dropdown
                    idx = obs_layer.fields().indexOf("priorite")
                    if idx >= 0:
                        obs_layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ValueMap", {
                            "map": [
                                {"Haute": "haute"},
                                {"Moyenne": "moyenne"},
                                {"Basse": "basse"},
                            ]
                        }))

                else:
                    include_obs = False
                    obs_layer = None

        # ── 3. Write .qgz with rewritten sources ─────────────────
        # Save original sources for restoration
        original_sources = {}
        for lid, info in exported_layers.items():
            layer = project.mapLayer(lid)
            if layer:
                original_sources[lid] = (layer.source(), layer.providerType())

        # Rewrite sources to relative GPKG paths
        for lid, info in exported_layers.items():
            layer = project.mapLayer(lid)
            if layer:
                layer.setDataSource(
                    f"./data/{info['gpkg_name']}",
                    layer.name(), "ogr"
                )

        # Add observations layer to project temporarily
        obs_added = False
        if include_obs and obs_layer and obs_layer.isValid():
            obs_layer.setDataSource("./data/observations.gpkg", "Observations", "ogr")
            project.addMapLayer(obs_layer, True)
            obs_added = True

        # Write the portable project
        qgz_path = os.path.join(pkg_dir, f"{project_name}.qgz")
        project.write(qgz_path)

        # Restore original sources
        for lid, (src, prov) in original_sources.items():
            layer = project.mapLayer(lid)
            if layer:
                layer.setDataSource(src, layer.name(), prov)

        # Remove temporary observations layer
        if obs_added and obs_layer:
            project.removeMapLayer(obs_layer.id())

        # ── 4. Create ZIP archive ─────────────────────────────────
        zip_name = f"{project_name}_qfield.zip"
        zip_path = f"/data/{zip_name}"
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(pkg_dir):
                for f in files:
                    full = os.path.join(root, f)
                    arcname = os.path.relpath(full, pkg_dir)
                    zf.write(full, arcname)

        # Cleanup temp directory
        shutil.rmtree(pkg_dir, ignore_errors=True)

        zip_size = os.path.getsize(zip_path)
        total_features = sum(info["features"] for info in exported_layers.values())

        return {
            "success": True,
            "path": zip_path,
            "download_url": f"http://localhost:{_API_HOST_PORT}/api/files/{zip_name}",
            "size_bytes": zip_size,
            "size_mb": round(zip_size / 1024 / 1024, 1),
            "project_name": project_name,
            "layers_exported": len(exported_layers),
            "total_features": total_features,
            "has_observations_layer": include_obs,
            "layers": {lid: info["original_name"] for lid, info in exported_layers.items()},
        }

    # ── Grist Export ─────────────────────────────────────────────

    def _action_export_grist(self, params: dict) -> dict:
        """Export current project as a .grist file (SQLite) with widgets.

        Creates a complete Grist document with:
        - Tables with typed columns mapped from QGIS fields
        - All vector layer data with lat/lon for map widget
        - Pre-configured Map, Chart, and Form widgets
        - Auto-detected relationships between layers
        - Optional aggregated statistics table

        If html_path is provided, delegates to _action_export_grist_from_html()
        which converts any HTML with inline GeoJSON into a Grist document.
        """
        # ── Dispatch: HTML mode vs QGIS project mode ────────
        html_path = params.get("html_path", "")
        if html_path:
            return self._action_export_grist_from_html(params)

        import sqlite3
        import re as _re
        from collections import defaultdict
        from qgis.core import QgsExpressionContextUtils, QgsRenderContext

        project = QgsProject.instance()
        doc_name = params.get("document_name", project.baseName() or "qgis_export")
        doc_name = _re.sub(r'[^\w\-]', '_', doc_name).strip('_') or "qgis_export"
        max_feat = params.get("max_features_per_layer", 50000)
        include_stats = params.get("include_stats", True)
        detect_rels = params.get("detect_relationships", True)
        tz = params.get("timezone", "Europe/Paris")
        # NEW 2026-06-24 (consumer qgis-sspcloud / Scene Manifest V0.2 :
        # https://github.com/nic01asFr/cerema-offre-de-service/docs/scene-manifest-spec.md) :
        # 2 nouveaux params OPTIONNELS pour la consommation par des
        # plateformes externes. BigQgisMCP reste autonome avec fallback
        # comportement actuel quand ces params sont absents.
        # - output_path : chemin custom (ex: /data/studies/{sid}/projects/{pid}/exports/x.grist)
        # - scene_manifest_json : JSON Scene Manifest V0.2 a embarquer comme
        #   table _custom_SceneManifest dans le .grist (permet aux widgets
        #   atlas Grist de lire le style declarative cross-runtime).
        output_path = (params.get("output_path") or "").strip()
        scene_manifest_json = (params.get("scene_manifest_json") or "").strip()

        if output_path:
            grist_path = output_path
            # S'assurer que le repertoire parent existe (cas qgis-sspcloud :
            # /data/studies/{sid}/projects/{pid}/exports/ pas force pre-cree).
            try:
                Path(grist_path).parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        else:
            grist_path = f"/data/{doc_name}.grist"
        if os.path.exists(grist_path):
            os.remove(grist_path)

        # ── Helper functions ──────────────────────────────────

        def _strip_accents(text):
            import unicodedata
            nfkd = unicodedata.normalize('NFKD', text)
            return ''.join(ch for ch in nfkd if not unicodedata.combining(ch))

        def sanitize_table(name):
            s = _strip_accents(name)
            s = _re.sub(r'[^A-Za-z0-9_]', '_', s).strip('_')
            s = _re.sub(r'_+', '_', s)  # collapse double underscores
            if not s or s[0].isdigit():
                s = 'T' + s
            return s or 'Unnamed'

        def sanitize_col(name):
            s = _strip_accents(name)
            s = _re.sub(r'[^A-Za-z0-9_]', '_', s).strip('_')
            s = _re.sub(r'_+', '_', s)  # collapse double underscores
            if not s or s[0].isdigit():
                s = 'c' + s
            if s.lower() in ('id', 'manualsort'):
                s = s + '_col'
            return s or 'unnamed'

        _TYPE_MAP = {
            'string': 'Text', 'text': 'Text', 'varchar': 'Text',
            'integer': 'Int', 'integer32': 'Int', 'int4': 'Int', 'int': 'Int',
            'integer64': 'Int', 'int8': 'Int', 'bigint': 'Int',
            'real': 'Numeric', 'double': 'Numeric', 'float': 'Numeric',
            'float8': 'Numeric', 'numeric': 'Numeric',
            'boolean': 'Bool', 'bool': 'Bool',
            'date': 'Date', 'datetime': f'DateTime:{tz}',
            'timestamp': f'DateTime:{tz}',
        }
        _QV_MAP = {
            QVariant.Int: 'Int', QVariant.LongLong: 'Int',
            QVariant.Double: 'Numeric', QVariant.Bool: 'Bool',
            QVariant.String: 'Text',
        }

        def qgis_to_grist(field):
            tn = field.typeName().lower()
            return _TYPE_MAP.get(tn, _QV_MAP.get(field.type(), 'Text'))

        def extract_widget(layer, idx):
            setup = layer.editorWidgetSetup(idx)
            wt = setup.type()
            cfg = setup.config()
            if wt == 'ValueMap':
                choices = []
                for entry in cfg.get('map', []):
                    if isinstance(entry, dict):
                        for _disp, val in entry.items():
                            choices.append(str(val))
                if choices:
                    return 'Choice', json.dumps({"choices": choices})
            elif wt == 'DateTime':
                return f'DateTime:{tz}', ''
            elif wt == 'ExternalResource':
                return 'Attachments', ''
            elif wt == 'CheckBox':
                return 'Bool', ''
            return None, ''

        def extract_renderer_info(layer):
            """Extract QGIS renderer info (graduated/categorized/single) as dict."""
            renderer = layer.renderer()
            if not renderer:
                return None
            rtype = renderer.type()
            try:
                if rtype == 'graduatedSymbol':
                    return {
                        'type': 'graduated',
                        'field': renderer.classAttribute(),
                        'classes': [{
                            'min': r.lowerValue(), 'max': r.upperValue(),
                            'color': r.symbol().color().name(),
                            'label': r.label()
                        } for r in renderer.ranges()]
                    }
                elif rtype == 'categorizedSymbol':
                    return {
                        'type': 'categorized',
                        'field': renderer.classAttribute(),
                        'categories': [{
                            'value': str(c.value()) if c.value() != '' else '',
                            'color': c.symbol().color().name(),
                            'label': c.label()
                        } for c in renderer.categories() if c.value() != '']
                    }
                elif rtype == 'singleSymbol':
                    sym = renderer.symbol()
                    return {'type': 'single', 'color': sym.color().name()}
            except Exception:
                pass
            return None

        def get_feature_color(renderer, feat, ctx):
            """Get the renderer color for a single feature.
            Falls back to computing from ranges/categories if symbolsForFeature fails."""
            if not renderer:
                return None
            rtype = renderer.type()
            try:
                # Try symbolsForFeature first (works with full render context)
                symbols = renderer.symbolsForFeature(feat, ctx)
                if symbols:
                    return symbols[0].color().name()
            except Exception:
                pass
            # Fallback: compute from renderer definition directly
            try:
                if rtype == 'graduatedSymbol':
                    field = renderer.classAttribute()
                    val = feat[field]
                    if val is not None:
                        for r in renderer.ranges():
                            if r.lowerValue() <= float(val) <= r.upperValue():
                                return r.symbol().color().name()
                        # Above max → last class
                        ranges = renderer.ranges()
                        if ranges:
                            return ranges[-1].symbol().color().name()
                elif rtype == 'categorizedSymbol':
                    field = renderer.classAttribute()
                    val = feat[field]
                    for cat in renderer.categories():
                        if str(cat.value()) == str(val):
                            return cat.symbol().color().name()
                elif rtype == 'singleSymbol':
                    return renderer.symbol().color().name()
            except Exception:
                pass
            return None

        def coerce(val, gtype):
            if val is None:
                return None
            # Unwrap QVariant and PyQt types early
            type_name = type(val).__name__
            if type_name == 'QVariant' or 'QVariant' in str(type(val)):
                # NULL QVariant
                if hasattr(val, 'isNull') and val.isNull():
                    return None
                if hasattr(val, 'value'):
                    val = val.value()
                    if val is None:
                        return None
                    type_name = type(val).__name__
                else:
                    return None
            if type_name in ('QDate', 'QDateTime', 'QTime'):
                if hasattr(val, 'isNull') and val.isNull():
                    return None
                if hasattr(val, 'toPyDateTime'):
                    val = val.toPyDateTime()
                    return val.isoformat() if val else None
                if hasattr(val, 'toPyDate'):
                    val = val.toPyDate()
                    return val.isoformat() if val else None
                if hasattr(val, 'toString'):
                    fmt = 'yyyy-MM-dd HH:mm:ss' if type_name == 'QDateTime' else 'yyyy-MM-dd' if type_name == 'QDate' else 'HH:mm:ss'
                    s = val.toString(fmt)
                    return s if s else None
                return None
            # Reject any remaining PyQt types
            mod = getattr(type(val), '__module__', '') or ''
            if 'PyQt' in mod or 'sip' in mod:
                return None
            if isinstance(val, str) and val == 'NULL':
                return None
            try:
                if gtype == 'Int':
                    return int(val)
                elif gtype == 'Numeric':
                    return float(val)
                elif gtype == 'Bool':
                    return 1 if val else 0
                else:
                    s = str(val)
                    if s.startswith('PyQt') or s.startswith('sip.'):
                        return None
                    return s
            except (ValueError, TypeError):
                return str(val)

        # ── 1. Introspect layers ──────────────────────────────

        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        layer_specs = []
        all_fields = defaultdict(list)  # field_name → [table_names]
        used_tables = set()
        map_table = None  # first point table for map page
        obs_table = None  # observations table for form page
        temporal_table = None  # table with year field for stats

        for layer in project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            # Include empty layers (observations) for form widget
            tname = sanitize_table(layer.name())
            counter = 1
            while tname in used_tables:
                tname = sanitize_table(layer.name()) + f'_{counter}'
                counter += 1
            used_tables.add(tname)

            geom_type = layer.geometryType()  # 0=Point, 1=Line, 2=Polygon
            is_point = (geom_type == 0)
            is_line = (geom_type == 1)
            is_polygon = (geom_type == 2)

            columns = []  # [{col_id, grist_type, label, widget_options}]
            for idx in range(layer.fields().count()):
                field = layer.fields().field(idx)
                col_id = sanitize_col(field.name())
                gtype = qgis_to_grist(field)
                wopts = ''
                override, w = extract_widget(layer, idx)
                if override:
                    gtype = override
                    wopts = w
                columns.append({
                    'col_id': col_id, 'grist_type': gtype,
                    'label': field.name(), 'widget_options': wopts,
                    'original_name': field.name(),
                })
                all_fields[field.name()].append(tname)

            # Add geometry columns
            geo_cols = []
            if is_point:
                geo_cols = [
                    {'col_id': 'latitude', 'grist_type': 'Numeric', 'label': 'Latitude', 'widget_options': '', 'original_name': ''},
                    {'col_id': 'longitude', 'grist_type': 'Numeric', 'label': 'Longitude', 'widget_options': '', 'original_name': ''},
                ]
            elif is_polygon or is_line:
                geo_cols = [
                    {'col_id': 'centroid_lat', 'grist_type': 'Numeric', 'label': 'Centroid Lat', 'widget_options': '', 'original_name': ''},
                    {'col_id': 'centroid_lon', 'grist_type': 'Numeric', 'label': 'Centroid Lon', 'widget_options': '', 'original_name': ''},
                    {'col_id': '_geojson', 'grist_type': 'Text', 'label': 'GeoJSON', 'widget_options': '', 'original_name': ''},
                ]
            columns.extend(geo_cols)

            # Add _color column if layer has a renderer with colors
            renderer = layer.renderer()
            has_renderer_colors = renderer is not None and renderer.type() in (
                'graduatedSymbol', 'categorizedSymbol', 'singleSymbol')
            if has_renderer_colors:
                columns.append({
                    'col_id': '_color', 'grist_type': 'Text',
                    'label': 'Color', 'widget_options': '', 'original_name': '',
                })

            # Filter columns if too many (Grist has trouble with 90+ columns)
            MAX_GRIST_COLS = 30
            if len(columns) > MAX_GRIST_COLS:
                _geo_ids = {'latitude', 'longitude', 'centroid_lat', 'centroid_lon', '_geojson', '_color'}
                _priority_pats = ['nature', 'nom', 'name', 'type', 'usage', 'importance',
                                  'categorie', 'hauteur', 'date', 'surface', 'titre',
                                  'description', 'label', 'prix', 'price']
                geo_keep = [c for c in columns if c['col_id'] in _geo_ids]
                prio_keep = [c for c in columns if c['col_id'] not in _geo_ids
                             and any(p in c['col_id'].lower() for p in _priority_pats)]
                rest = [c for c in columns if c['col_id'] not in _geo_ids
                        and not any(p in c['col_id'].lower() for p in _priority_pats)]
                budget = MAX_GRIST_COLS - len(geo_keep) - len(prio_keep)
                columns = geo_keep + prio_keep + rest[:max(budget, 0)]

            # Extract features
            export_orignames = {c['original_name'] for c in columns if c['original_name']}
            tr = QgsCoordinateTransform(layer.crs(), crs_4326, project)
            render_ctx = QgsRenderContext() if has_renderer_colors else None
            records = []
            for i, feat in enumerate(layer.getFeatures()):
                if i >= max_feat:
                    break
                rec = {}
                for col in columns:
                    if not col['original_name']:
                        continue
                    if col['original_name'] not in export_orignames:
                        continue
                    val = feat[col['original_name']]
                    c_val = coerce(val, col['grist_type'])
                    if c_val is not None:
                        rec[col['col_id']] = c_val

                # Renderer color per feature
                if has_renderer_colors:
                    color = get_feature_color(renderer, feat, render_ctx)
                    if color:
                        rec['_color'] = color

                geom = feat.geometry()
                if not geom.isEmpty():
                    from qgis.core import QgsGeometry
                    geom_copy = QgsGeometry(geom)
                    geom_copy.transform(tr)
                    if is_point:
                        pt = geom_copy.asPoint()
                        rec['latitude'] = round(pt.y(), 6)
                        rec['longitude'] = round(pt.x(), 6)
                    elif is_polygon or is_line:
                        centroid = geom_copy.centroid().asPoint()
                        rec['centroid_lat'] = round(centroid.y(), 6)
                        rec['centroid_lon'] = round(centroid.x(), 6)
                        try:
                            rec['_geojson'] = geom_copy.asJson()
                        except Exception:
                            pass
                records.append(rec)

            # Determine geom_type string for CONFIG
            geom_type_str = 'point' if is_point else ('line' if is_line else ('polygon' if is_polygon else 'other'))

            spec = {
                'table_name': tname, 'columns': columns,
                'records': records, 'is_point': is_point,
                'is_line': is_line, 'is_polygon': is_polygon,
                'source_layer': layer.name(),
                'has_lat_lon': is_point, 'geom_type': geom_type_str,
                'layer_obj': layer,
            }
            layer_specs.append(spec)

            # Detect special tables
            if is_point and map_table is None and len(records) > 0:
                map_table = spec
            # Form table: detect layers with QField-style form widgets or named "observation"
            name_lower = layer.name().lower()
            if obs_table is None:
                has_form_widgets = False
                for idx in range(layer.fields().count()):
                    wt = layer.editorWidgetSetup(idx).type()
                    if wt in ('ValueMap', 'DateTime', 'ExternalResource'):
                        has_form_widgets = True
                        break
                if has_form_widgets or 'observation' in name_lower:
                    obs_table = spec
            col_ids = [c['col_id'] for c in columns]
            if 'year' in col_ids or 'annee' in col_ids:
                temporal_table = spec

        if not layer_specs:
            return {"error": "No vector layers to export"}

        # Fallback: if no point map_table, use first table with records
        if map_table is None:
            for spec in layer_specs:
                if len(spec['records']) > 0 and spec is not obs_table:
                    map_table = spec
                    break

        # ── 2. Stats table ────────────────────────────────────

        stats_spec = None
        if include_stats and temporal_table:
            t_col = 'year' if 'year' in [c['col_id'] for c in temporal_table['columns']] else 'annee'
            # Find best numeric column
            num_cols = [c for c in temporal_table['columns']
                        if c['grist_type'] == 'Numeric' and c['col_id'] not in
                        ('latitude', 'longitude', 'centroid_lat', 'centroid_lon')]
            band_cols = [c for c in temporal_table['columns']
                         if 'band' in c['col_id'].lower() or 'bande' in c['col_id'].lower()]
            if num_cols:
                v_col = num_cols[0]['col_id']
                g_col = band_cols[0]['col_id'] if band_cols else None
                agg = defaultdict(lambda: defaultdict(list))
                for rec in temporal_table['records']:
                    yr = rec.get(t_col)
                    grp = rec.get(g_col, 'Total') if g_col else 'Total'
                    val = rec.get(v_col)
                    if yr is not None and val is not None:
                        try:
                            agg[int(yr)][str(grp)].append(float(val))
                        except (ValueError, TypeError):
                            pass
                stats_records = []
                for yr in sorted(agg):
                    for grp in sorted(agg[yr]):
                        vals = sorted(agg[yr][grp])
                        n = len(vals)
                        if n == 0:
                            continue
                        stats_records.append({
                            t_col: yr, 'group_name': grp, 'count': n,
                            'median': round(vals[n // 2], 1),
                            'mean': round(sum(vals) / n, 1),
                            'min_val': round(vals[0], 1),
                            'max_val': round(vals[-1], 1),
                        })
                if stats_records:
                    stats_spec = {
                        'table_name': sanitize_table(temporal_table['table_name'] + '_Stats'),
                        'columns': [
                            {'col_id': t_col, 'grist_type': 'Int', 'label': 'Annee', 'widget_options': '', 'original_name': ''},
                            {'col_id': 'group_name', 'grist_type': 'Text', 'label': 'Groupe', 'widget_options': '', 'original_name': ''},
                            {'col_id': 'count', 'grist_type': 'Int', 'label': 'Nombre', 'widget_options': '', 'original_name': ''},
                            {'col_id': 'median', 'grist_type': 'Numeric', 'label': 'Mediane', 'widget_options': '', 'original_name': ''},
                            {'col_id': 'mean', 'grist_type': 'Numeric', 'label': 'Moyenne', 'widget_options': '', 'original_name': ''},
                            {'col_id': 'min_val', 'grist_type': 'Numeric', 'label': 'Min', 'widget_options': '', 'original_name': ''},
                            {'col_id': 'max_val', 'grist_type': 'Numeric', 'label': 'Max', 'widget_options': '', 'original_name': ''},
                        ],
                        'records': stats_records, 'is_point': False, 'is_polygon': False,
                        'source_layer': '(computed)', 'has_lat_lon': False,
                    }
                    layer_specs.append(stats_spec)

        # ── 3. Create SQLite .grist ───────────────────────────

        conn = sqlite3.connect(grist_path)
        cur = conn.cursor()

        # 3a. Meta-tables
        self._grist_create_meta_tables(cur)

        # 3b. DocInfo
        cur.execute("INSERT INTO _grist_DocInfo VALUES (1,'','','',46,?,?)", (tz, '{"locale":"en-US"}'))

        # Counters
        col_id_ctr = 0
        table_id_ctr = 0
        view_id_ctr = 0
        section_id_ctr = 0
        field_id_ctr = 0
        page_id_ctr = 0
        tab_id_ctr = 0

        # Track colRefs for widget mapping
        table_col_refs = {}  # table_name → {col_id → colRef}
        table_ids = {}       # table_name → table_id_ctr value
        raw_section_ids = {} # table_name → section_id for raw view

        # 3c. For each table: meta-records + data
        for spec in layer_specs:
            tname = spec['table_name']
            cols = spec['columns']

            table_id_ctr += 1
            view_id_ctr += 1
            section_id_ctr += 1

            raw_view_id = view_id_ctr
            raw_section_id = section_id_ctr
            table_ids[tname] = table_id_ctr
            raw_section_ids[tname] = raw_section_id
            table_col_refs[tname] = {}

            # _grist_Tables
            cur.execute(
                "INSERT INTO _grist_Tables VALUES (?,?,?,0,0,?,0)",
                (table_id_ctr, tname, raw_view_id, raw_section_id)
            )

            # _grist_Tables_column: manualSort first
            col_id_ctr += 1
            cur.execute(
                "INSERT INTO _grist_Tables_column VALUES (?,?,1.0,'manualSort','ManualSortPos','',0,'','manualSort','',0,0,0,0,NULL,0,NULL)",
                (col_id_ctr, table_id_ctr)
            )

            # User columns
            col_refs_for_fields = []
            for pos_i, col in enumerate(cols):
                col_id_ctr += 1
                table_col_refs[tname][col['col_id']] = col_id_ctr
                cur.execute(
                    "INSERT INTO _grist_Tables_column VALUES (?,?,?,?,?,?,0,'',?,?,1,0,0,0,NULL,0,NULL)",
                    (col_id_ctr, table_id_ctr, float(pos_i + 2),
                     col['col_id'], col['grist_type'],
                     col['widget_options'] or '', col['label'], '')
                )
                col_refs_for_fields.append(col_id_ctr)

            # _grist_Views (raw data view)
            cur.execute(
                "INSERT INTO _grist_Views VALUES (?,?,?,'')",
                (raw_view_id, tname, 'raw_data')
            )

            # _grist_Views_section (raw data grid)
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'','',0,0,'','','','','','',0,0,0,'','','')",
                (raw_section_id, table_id_ctr, raw_view_id, 'record')
            )

            # _grist_Views_section_field
            for pos_i, cr in enumerate(col_refs_for_fields):
                field_id_ctr += 1
                cur.execute(
                    "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                    (field_id_ctr, raw_section_id, float(pos_i + 1), cr)
                )

            # _grist_TabBar + _grist_Pages
            tab_id_ctr += 1
            page_id_ctr += 1
            cur.execute("INSERT INTO _grist_TabBar VALUES (?,?,?)",
                        (tab_id_ctr, raw_view_id, float(tab_id_ctr)))
            cur.execute("INSERT INTO _grist_Pages VALUES (?,?,0,?)",
                        (page_id_ctr, raw_view_id, float(page_id_ctr)))

            # Create data table
            col_defs = ["id INTEGER PRIMARY KEY", "manualSort REAL"]
            for col in cols:
                aff = 'TEXT'
                if col['grist_type'] in ('Int', 'Bool'):
                    aff = 'INTEGER'
                elif col['grist_type'] in ('Numeric', 'Date', f'DateTime:{tz}'):
                    aff = 'REAL'
                col_defs.append(f'"{col["col_id"]}" {aff}')
            cur.execute(f'CREATE TABLE "{tname}" ({", ".join(col_defs)})')

            # Insert data in batches
            if spec['records']:
                col_ids = [c['col_id'] for c in cols]
                placeholders = ', '.join(['?'] * (len(col_ids) + 2))
                quoted_cols = ", ".join(f'"{c}"' for c in col_ids)
                insert_sql = f'INSERT INTO "{tname}" (id, manualSort, {quoted_cols}) VALUES ({placeholders})'
                batch = []
                def _safe(v):
                    """Ensure value is sqlite3-safe (str/int/float/None)."""
                    if v is None:
                        return None
                    if isinstance(v, (int, float, str)):
                        return v
                    if isinstance(v, bool):
                        return 1 if v else 0
                    # Last resort: stringify or None
                    s = str(v)
                    return None if ('PyQt' in s or 'QVariant' in s or 'sip.' in s) else s

                for row_i, rec in enumerate(spec['records']):
                    row = [row_i + 1, float(row_i + 1)]
                    for cid in col_ids:
                        row.append(_safe(rec.get(cid)))
                    batch.append(row)
                    if len(batch) >= 500:
                        cur.executemany(insert_sql, batch)
                        batch = []
                if batch:
                    cur.executemany(insert_sql, batch)

        # ── 4. Widget pages ───────────────────────────────────

        pages_created = {}

        # 4a. Page "Carte" — map + linked table
        if map_table:
            view_id_ctr += 1
            map_view_id = view_id_ctr

            # Map section
            section_id_ctr += 1
            map_section_id = section_id_ctr

            # Table section (linked to map)
            section_id_ctr += 1
            table_section_id = section_id_ctr

            tname = map_table['table_name']
            tid = table_ids[tname]
            crefs = table_col_refs[tname]

            # Find colRefs for Name, Latitude, Longitude
            name_ref = None
            lat_ref = crefs.get('latitude')
            lon_ref = crefs.get('longitude')
            # Use first text column as Name
            for col in map_table['columns']:
                if col['grist_type'] == 'Text' and col['col_id'] in crefs:
                    name_ref = crefs[col['col_id']]
                    break
            if not name_ref:
                name_ref = lat_ref  # fallback

            layout = json.dumps({
                "children": [
                    {"leaf": map_section_id, "size": 60},
                    {"leaf": table_section_id, "size": 40}
                ]
            })

            # Map widget — generic Leaflet Grist widget with CONFIG injection
            # Build CONFIG metadata (no data — widget reads from Grist tables)
            tables_meta = {}
            for spec in layer_specs:
                role = 'primary' if spec is map_table else 'secondary'
                if spec is obs_table:
                    role = 'form'
                tables_meta[spec['table_name']] = {
                    'role': role,
                    'geomType': spec['geom_type'],
                }

            renderer_info = {}
            for spec in layer_specs:
                ri = extract_renderer_info(spec['layer_obj'])
                if ri:
                    renderer_info[spec['table_name']] = ri

            # Canvas center/zoom
            canvas = self.iface.mapCanvas() if self.iface else None
            map_center = [0, 0]
            map_zoom = 13
            if canvas:
                ext = canvas.extent()
                tr_view = QgsCoordinateTransform(project.crs(), crs_4326, project)
                ext_4326 = tr_view.transformBoundingBox(ext)
                map_center = [
                    round((ext_4326.yMinimum() + ext_4326.yMaximum()) / 2, 6),
                    round((ext_4326.xMinimum() + ext_4326.xMaximum()) / 2, 6),
                ]

            scope = QgsExpressionContextUtils.projectScope(project)
            zone_name = scope.variable("study_zone_name") or ""

            config = {
                'center': map_center,
                'zoom': map_zoom,
                'title': doc_name,
                'zoneName': zone_name,
                'tables': tables_meta,
                'primaryTable': map_table['table_name'],
                'rendererInfo': renderer_info,
            }

            # Load generic widget template and inject CONFIG
            grist_template_path = Path("/app/templates/web/leaflet_grist_widget.html")
            if grist_template_path.exists():
                html_content = grist_template_path.read_text(encoding="utf-8")
                html_content = html_content.replace(
                    '{{CONFIG_JSON}}', json.dumps(config, ensure_ascii=False, default=str))
            else:
                html_content = '<!DOCTYPE html><html><body><p>Generic Grist widget template not found.</p></body></html>'

            custom_view_inner = json.dumps({
                "mode": "url",
                "url": None,
                "widgetDef": {
                    "name": "Custom widget builder",
                    "url": "https://gristlabs.github.io/grist-widget/buildwidget/",
                    "widgetId": "@berhalak/custom-widget-builder",
                    "published": True,
                    "accessLevel": "full",
                    "renderAfterReady": True,
                    "description": "Build custom widgets with HTML and JavaScript, right inside Grist.",
                    "isGristLabsMaintained": False,
                },
                "access": "full",
                "pluginId": "",
                "sectionId": "",
                "renderAfterReady": True,
                "widgetId": "@berhalak/custom-widget-builder",
                "widgetOptions": {
                    "_js": "",
                    "_html": html_content
                },
                "columnsMapping": None
            })
            map_options = json.dumps({
                "customView": custom_view_inner
            })

            cur.execute("INSERT INTO _grist_Views VALUES (?,?,'',?)",
                        (map_view_id, 'Carte', layout))

            # Map section (custom widget)
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Carte','',0,0,'',?,'','','','',0,0,0,'','','')",
                (map_section_id, tid, map_view_id, 'custom', map_options)
            )

            # Fields for custom widget section — needed for onRecords to include these columns
            for pos_i, col in enumerate(map_table['columns']):
                if col['col_id'] in crefs:
                    field_id_ctr += 1
                    cur.execute(
                        "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                        (field_id_ctr, map_section_id, float(pos_i + 1), crefs[col['col_id']])
                    )

            # Linked table section (grid below the map)
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Donnees','',0,0,'','','','','','',?,0,0,'','','')",
                (table_section_id, tid, map_view_id, 'record', map_section_id)
            )

            # Fields for table section
            for pos_i, col in enumerate(map_table['columns']):
                if col['col_id'] in crefs:
                    field_id_ctr += 1
                    cur.execute(
                        "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                        (field_id_ctr, table_section_id, float(pos_i + 1), crefs[col['col_id']])
                    )

            tab_id_ctr += 1
            page_id_ctr += 1
            cur.execute("INSERT INTO _grist_TabBar VALUES (?,?,?)",
                        (tab_id_ctr, map_view_id, float(tab_id_ctr)))
            cur.execute("INSERT INTO _grist_Pages VALUES (?,?,0,?)",
                        (page_id_ctr, map_view_id, float(page_id_ctr)))
            pages_created['carte'] = True

        # 4b. Page "Statistiques" — chart + table
        if stats_spec:
            view_id_ctr += 1
            stats_view_id = view_id_ctr

            section_id_ctr += 1
            chart_section_id = section_id_ctr
            section_id_ctr += 1
            stats_table_section_id = section_id_ctr

            sname = stats_spec['table_name']
            stid = table_ids[sname]

            layout = json.dumps({
                "children": [
                    {"leaf": chart_section_id, "size": 50},
                    {"leaf": stats_table_section_id, "size": 50}
                ]
            })

            cur.execute("INSERT INTO _grist_Views VALUES (?,?,'',?)",
                        (stats_view_id, 'Statistiques', layout))

            # Chart section with options
            chart_options = json.dumps({
                "multiseries": True,
                "orientation": "v",
                "invertYAxis": False,
                "logYAxis": False,
                "stacked": False,
                "isXAxisUndefined": False
            })
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Evolution','',0,0,'',?,?,'','','',0,0,0,'','','')",
                (chart_section_id, stid, stats_view_id, 'chart', chart_options, 'bar')
            )

            # Chart fields: 1st = X axis (year), 2nd = group (series), 3rd+ = Y values
            screfs = table_col_refs[sname]
            chart_col_order = [t_col, 'group_name', 'median', 'mean']
            for pos_i, cid in enumerate(chart_col_order):
                if cid in screfs:
                    field_id_ctr += 1
                    cur.execute(
                        "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                        (field_id_ctr, chart_section_id, float(pos_i + 1), screfs[cid])
                    )

            # Stats table section
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Donnees','',0,0,'','','','','','',0,0,0,'','','')",
                (stats_table_section_id, stid, stats_view_id, 'record')
            )

            for pos_i, col in enumerate(stats_spec['columns']):
                if col['col_id'] in screfs:
                    field_id_ctr += 1
                    cur.execute(
                        "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                        (field_id_ctr, stats_table_section_id, float(pos_i + 1), screfs[col['col_id']])
                    )

            tab_id_ctr += 1
            page_id_ctr += 1
            cur.execute("INSERT INTO _grist_TabBar VALUES (?,?,?)",
                        (tab_id_ctr, stats_view_id, float(tab_id_ctr)))
            cur.execute("INSERT INTO _grist_Pages VALUES (?,?,0,?)",
                        (page_id_ctr, stats_view_id, float(page_id_ctr)))
            pages_created['statistiques'] = True

        # 4c. Page "Saisie terrain" — form + map + table
        if obs_table:
            view_id_ctr += 1
            obs_view_id = view_id_ctr

            section_id_ctr += 1
            form_section_id = section_id_ctr
            section_id_ctr += 1
            obs_map_section_id = section_id_ctr
            section_id_ctr += 1
            obs_table_section_id = section_id_ctr

            oname = obs_table['table_name']
            otid = table_ids[oname]
            ocrefs = table_col_refs[oname]

            layout = json.dumps({
                "children": [
                    {"leaf": form_section_id, "size": 40},
                    {"children": [
                        {"leaf": obs_map_section_id, "size": 50},
                        {"leaf": obs_table_section_id, "size": 50}
                    ], "size": 60}
                ]
            })

            # Map for observations (customDef format)
            obs_mapping = {}
            if ocrefs.get('latitude'):
                obs_mapping["Latitude"] = ocrefs['latitude']
            if ocrefs.get('longitude'):
                obs_mapping["Longitude"] = ocrefs['longitude']
            name_col = ocrefs.get('titre') or ocrefs.get('latitude')
            if name_col:
                obs_mapping["Name"] = name_col
            obs_map_opts = json.dumps({
                "customDef": {
                    "mode": "url",
                    "url": "https://gristlabs.github.io/grist-widget/map/",
                    "access": "full",
                    "columnsMapping": obs_mapping,
                    "renderAfterReady": True,
                    "pluginId": "",
                    "sectionId": ""
                }
            })

            cur.execute("INSERT INTO _grist_Views VALUES (?,?,'',?)",
                        (obs_view_id, 'Saisie terrain', layout))

            # Form section
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Nouvelle observation','',0,0,'','','','','','',0,0,0,'','','')",
                (form_section_id, otid, obs_view_id, 'form')
            )
            # Form fields
            for pos_i, col in enumerate(obs_table['columns']):
                if col['col_id'] in ocrefs:
                    field_id_ctr += 1
                    cur.execute(
                        "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                        (field_id_ctr, form_section_id, float(pos_i + 1), ocrefs[col['col_id']])
                    )

            # Obs map section
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Carte observations','',0,0,'',?,'','','','',0,0,0,'','','')",
                (obs_map_section_id, otid, obs_view_id, 'custom', obs_map_opts)
            )

            # Obs table section (linked to map)
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Observations','',0,0,'','','','','','',?,0,0,'','','')",
                (obs_table_section_id, otid, obs_view_id, 'record', obs_map_section_id)
            )
            for pos_i, col in enumerate(obs_table['columns']):
                if col['col_id'] in ocrefs:
                    field_id_ctr += 1
                    cur.execute(
                        "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                        (field_id_ctr, obs_table_section_id, float(pos_i + 1), ocrefs[col['col_id']])
                    )

            tab_id_ctr += 1
            page_id_ctr += 1
            cur.execute("INSERT INTO _grist_TabBar VALUES (?,?,?)",
                        (tab_id_ctr, obs_view_id, float(tab_id_ctr)))
            cur.execute("INSERT INTO _grist_Pages VALUES (?,?,0,?)",
                        (page_id_ctr, obs_view_id, float(page_id_ctr)))
            pages_created['saisie'] = True

        # ── 5. Default ACL + principals ──────────────────────

        cur.execute("INSERT INTO _grist_ACLPrincipals VALUES (1,'group','','','Owners','')")
        cur.execute("INSERT INTO _grist_ACLPrincipals VALUES (2,'group','','','Admins','')")
        cur.execute("INSERT INTO _grist_ACLPrincipals VALUES (3,'group','','','Editors','')")
        cur.execute("INSERT INTO _grist_ACLPrincipals VALUES (4,'group','','','Viewers','')")
        cur.execute("INSERT INTO _grist_ACLResources VALUES (1,'','')")
        cur.execute("INSERT INTO _grist_ACLRules VALUES (1,1,63,'[1]','',0,'','',1e999,'','')")

        # NEW 2026-06-24 : optional embedded Scene Manifest V0.2.
        # Quand un consumer externe (ex: qgis-sspcloud) fournit le JSON,
        # on l'embarque comme table _custom_SceneManifest dans le .grist.
        # Permet aux widgets atlas/Grist de lire le style declarative
        # cross-runtime (cohérence avec la spec V0.2 cerema-offre-de-service).
        # Idempotent : si scene_manifest_json absent, table non creee.
        scene_manifest_embedded = False
        scene_manifest_meta = {}
        if scene_manifest_json:
            try:
                import json as _json
                import hashlib as _hashlib
                from datetime import datetime as _dt
                from datetime import timezone as _timezone
                # Valider que le JSON parse (best-effort, pas de validation
                # Pydantic ici pour ne pas tirer la dep cote BigQgisMCP).
                _parsed = _json.loads(scene_manifest_json)
                _hash = _hashlib.sha256(
                    scene_manifest_json.encode("utf-8")
                ).hexdigest()
                # Creer la table physique (donnees brutes) + entries dans
                # _grist_Tables / _grist_Tables_column pour qu'elle soit
                # visible dans l'UI Grist (sinon table fantome SQL-only).
                # Schema canonique de la table, tel que qgis2grist la cree et
                # qu'Atlas la lit : manifest_json / scene_hash / source_file /
                # created_at. Nous ecrivions `content` et `created_at_iso` :
                # Atlas cherchait `data.manifest_json[i]`, trouvait undefined,
                # et rendait une carte vide en signalant « manifest JSON
                # invalide ». Un .grist produit ici n'etait donc lisible par
                # aucun widget de l'ecosysteme.
                # `n_layers` est en plus du canonique -- lisible dans l'UI Grist
                # sans avoir a ouvrir le JSON.
                cur.execute("""
                    CREATE TABLE SceneManifest (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        manualSort REAL DEFAULT 0,
                        manifest_json TEXT DEFAULT '',
                        scene_hash TEXT DEFAULT '',
                        source_file TEXT DEFAULT '',
                        created_at INTEGER DEFAULT 0,
                        n_layers INTEGER DEFAULT 0
                    )
                """)
                cur.execute(
                    "INSERT INTO SceneManifest "
                    "(manualSort, manifest_json, scene_hash, source_file, "
                    "created_at, n_layers) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        1.0,
                        scene_manifest_json,
                        _hash,
                        doc_name,
                        # Grist stocke les DateTime en secondes epoch, pas en
                        # chaine ISO. Atlas s'en sert pour retenir la ligne la
                        # plus recente.
                        int(_dt.now(_timezone.utc).timestamp()),
                        len(_parsed.get("layers", [])),
                    ),
                )
                # Registry Grist : ajout dans _grist_Tables + colonnes.
                # ID arbitraire mais unique (max_id+1 evite collisions avec
                # tables QGIS deja creees ci-dessus).
                cur.execute("SELECT COALESCE(MAX(id), 0) FROM _grist_Tables")
                sm_tid = cur.fetchone()[0] + 1
                cur.execute(
                    "INSERT INTO _grist_Tables VALUES (?, 'SceneManifest', 0, 0, '', '', '', 0)",
                    (sm_tid,),
                )
                # Colonnes (sans manualSort qui est dejaautocree par Grist).
                _sm_columns = [
                    ("manifest_json", "Text",         1.0),
                    ("scene_hash",    "Text",         2.0),
                    ("source_file",   "Text",         3.0),
                    ("created_at",    "DateTime:UTC", 4.0),
                    ("n_layers",      "Int",          5.0),
                ]
                cur.execute("SELECT COALESCE(MAX(id), 0) FROM _grist_Tables_column")
                sm_col_id = cur.fetchone()[0]
                for col_name, col_type, col_pos in _sm_columns:
                    sm_col_id += 1
                    cur.execute(
                        "INSERT INTO _grist_Tables_column VALUES "
                        "(?,?,?,?,?,'','','','',0,0,'')",
                        (sm_col_id, sm_tid, col_name, col_type, col_pos),
                    )
                scene_manifest_embedded = True
                scene_manifest_meta = {
                    "scene_hash": _hash,
                    # `version` est le champ du contrat publie ; `manifest_version`
                    # est l'ancienne graphie interne de qgis-sspcloud. On lit la
                    # premiere, on retombe sur la seconde tant qu'elle circule.
                    "version": str(
                        _parsed.get("version")
                        or _parsed.get("manifest_version")
                        or "0.2.2"
                    ),
                    "n_layers": len(_parsed.get("layers", [])),
                }
            except Exception as _sm_exc:
                # Best-effort : on n'echoue pas le .grist global si le SM
                # est invalide. Le consumer verra scene_manifest_embedded=False.
                scene_manifest_meta = {"error": str(_sm_exc)}

        conn.commit()
        conn.close()

        total_records = sum(len(s['records']) for s in layer_specs)
        size = os.path.getsize(grist_path)
        fname = os.path.basename(grist_path)

        return {
            "success": True,
            "path": grist_path,
            "download_url": f"http://localhost:{_API_HOST_PORT}/api/files/{fname}",
            "size_bytes": size,
            "size_mb": round(size / 1024 / 1024, 1),
            "document_name": doc_name,
            "tables": len(layer_specs) + (1 if scene_manifest_embedded else 0),
            "total_records": total_records,
            "pages": pages_created,
            "layers": {s['table_name']: len(s['records']) for s in layer_specs},
            # NEW 2026-06-24 : metadata Scene Manifest si embarque.
            "scene_manifest_embedded": scene_manifest_embedded,
            "scene_manifest": scene_manifest_meta,
        }

    # ── Grist from HTML — universal HTML→Grist converter ─────────

    def _action_export_grist_from_html(self, params: dict) -> dict:
        """Convert any HTML with inline GeoJSON into a .grist document.

        Universal: works with export_web_map, export_flood_map, export_temporal_map,
        qgis2web, or any Leaflet HTML containing FeatureCollection data.
        """
        import sqlite3
        import re as _re

        html_path = params.get("html_path", "")
        if not os.path.exists(html_path):
            return {"success": False, "error": f"File not found: {html_path}"}

        html_text = Path(html_path).read_text(encoding="utf-8")
        tz = params.get("timezone", "Europe/Paris")

        # Document name — derive from filename or param
        base_name = params.get("document_name", "")
        if not base_name:
            base_name = Path(html_path).stem
        doc_name = _re.sub(r'[^\w\-]', '_', base_name).strip('_') or "grist_export"

        max_feat = params.get("max_features_per_layer", 50000)

        grist_path = f"/data/{doc_name}.grist"
        if os.path.exists(grist_path):
            os.remove(grist_path)

        # ── 1. Parse HTML for FeatureCollections ─────────────
        parsed_fcs = self._grist_parse_html_geojson(html_text)
        if not parsed_fcs:
            return {"success": False, "error": "No GeoJSON FeatureCollection data found in HTML file."}

        # ── 2. Convert to Grist table specs ──────────────────
        table_specs = []
        wrapper_fcs = [fc for fc in parsed_fcs if fc.get('is_wrapper')]
        individual_fcs = [fc for fc in parsed_fcs if not fc.get('is_wrapper')]

        for fc_info in individual_fcs:
            spec = self._grist_fc_to_spec(fc_info['var_name'], fc_info['data'])
            # Truncate if needed
            if len(spec['records']) > max_feat:
                spec['records'] = spec['records'][:max_feat]
            table_specs.append(spec)

        for fc_info in wrapper_fcs:
            spec = self._grist_fc_to_spec(
                fc_info['var_name'], fc_info['data'],
                element_name=fc_info.get('element_name')
            )
            spec['_is_wrapper'] = True
            if len(spec['records']) > max_feat:
                spec['records'] = spec['records'][:max_feat]
            table_specs.append(spec)

        # Deduplicate table names
        seen_names = {}
        for spec in table_specs:
            tname = spec['table_name']
            if tname in seen_names:
                seen_names[tname] += 1
                spec['table_name'] = f"{tname}_{seen_names[tname]}"
            else:
                seen_names[tname] = 1

        # ── 3. Detect form tables + cross-table references ──
        self._grist_detect_form_tables(table_specs)
        detected_refs = self._grist_detect_refs(table_specs)

        # ── 4. Transform HTML ────────────────────────────────
        transformed_html = self._grist_gristify_html(html_text, parsed_fcs, table_specs)

        # ── 5. Assemble SQLite .grist ────────────────────────
        conn = sqlite3.connect(grist_path)
        cur = conn.cursor()

        # 5a. Meta-tables
        self._grist_create_meta_tables(cur)

        # 5b. DocInfo
        cur.execute("INSERT INTO _grist_DocInfo VALUES (1,'','','',46,?,?)", (tz, '{"locale":"en-US"}'))

        # Counters
        col_id_ctr = 0
        table_id_ctr = 0
        view_id_ctr = 0
        section_id_ctr = 0
        field_id_ctr = 0
        page_id_ctr = 0
        tab_id_ctr = 0

        table_col_refs = {}
        table_ids = {}
        raw_section_ids = {}
        ref_columns_to_process = []  # [(src_table_name, col_spec, col_ref_id)]
        first_table_name = table_specs[0]['table_name'] if table_specs else None

        # 5c. Create tables
        for spec in table_specs:
            tname = spec['table_name']
            cols = spec['columns']

            table_id_ctr += 1
            view_id_ctr += 1
            section_id_ctr += 1

            raw_view_id = view_id_ctr
            raw_section_id = section_id_ctr
            table_ids[tname] = table_id_ctr
            raw_section_ids[tname] = raw_section_id
            table_col_refs[tname] = {}

            # _grist_Tables
            cur.execute(
                "INSERT INTO _grist_Tables VALUES (?,?,?,0,0,?,0)",
                (table_id_ctr, tname, raw_view_id, raw_section_id)
            )

            # manualSort column
            col_id_ctr += 1
            cur.execute(
                "INSERT INTO _grist_Tables_column VALUES (?,?,1.0,'manualSort','ManualSortPos','',0,'','manualSort','',0,0,0,0,NULL,0,NULL)",
                (col_id_ctr, table_id_ctr)
            )

            # User columns
            col_refs_for_fields = []
            for pos_i, col in enumerate(cols):
                col_id_ctr += 1
                table_col_refs[tname][col['col_id']] = col_id_ctr
                cur.execute(
                    "INSERT INTO _grist_Tables_column VALUES (?,?,?,?,?,?,0,'',?,?,1,0,0,0,NULL,0,NULL)",
                    (col_id_ctr, table_id_ctr, float(pos_i + 2),
                     col['col_id'], col['grist_type'],
                     col['widget_options'] or '', col['label'], '')
                )
                col_refs_for_fields.append(col_id_ctr)
                # Track Ref columns for post-processing (helper display columns)
                if col.get('_ref_info'):
                    ref_columns_to_process.append((tname, col, col_id_ctr))

            # Raw data view
            cur.execute(
                "INSERT INTO _grist_Views VALUES (?,?,?,'')",
                (raw_view_id, tname, 'raw_data')
            )

            # Raw data section
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'','',0,0,'','','','','','',0,0,0,'','','')",
                (raw_section_id, table_id_ctr, raw_view_id, 'record')
            )

            # Section fields
            for pos_i, cr in enumerate(col_refs_for_fields):
                field_id_ctr += 1
                cur.execute(
                    "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                    (field_id_ctr, raw_section_id, float(pos_i + 1), cr)
                )

            # TabBar + Pages
            tab_id_ctr += 1
            page_id_ctr += 1
            cur.execute("INSERT INTO _grist_TabBar VALUES (?,?,?)",
                        (tab_id_ctr, raw_view_id, float(tab_id_ctr)))
            cur.execute("INSERT INTO _grist_Pages VALUES (?,?,0,?)",
                        (page_id_ctr, raw_view_id, float(page_id_ctr)))

            # Create data table — handle Choice (TEXT), Ref (INTEGER), standard types
            col_defs = ["id INTEGER PRIMARY KEY", "manualSort REAL"]
            for col in cols:
                aff = 'TEXT'
                if col['grist_type'] in ('Int', 'Bool'):
                    aff = 'INTEGER'
                elif col['grist_type'] in ('Numeric', 'Date', f'DateTime:{tz}'):
                    aff = 'REAL'
                elif col['grist_type'].startswith('Ref:'):
                    aff = 'INTEGER'
                # Choice stays TEXT (default)
                col_defs.append(f'"{col["col_id"]}" {aff}')
            cur.execute(f'CREATE TABLE "{tname}" ({", ".join(col_defs)})')

            # Insert data
            if spec['records']:
                col_ids = [c['col_id'] for c in cols]
                placeholders = ', '.join(['?'] * (len(col_ids) + 2))
                quoted_cols = ", ".join(f'"{c}"' for c in col_ids)
                insert_sql = f'INSERT INTO "{tname}" (id, manualSort, {quoted_cols}) VALUES ({placeholders})'
                batch = []
                for row_i, rec in enumerate(spec['records']):
                    row = [row_i + 1, float(row_i + 1)]
                    for cid in col_ids:
                        v = rec.get(cid)
                        if v is None:
                            row.append(None)
                        elif isinstance(v, (int, float, str)):
                            row.append(v)
                        elif isinstance(v, bool):
                            row.append(1 if v else 0)
                        else:
                            s = str(v)
                            row.append(None if ('PyQt' in s or 'QVariant' in s) else s)
                    batch.append(row)
                    if len(batch) >= 500:
                        cur.executemany(insert_sql, batch)
                        batch = []
                if batch:
                    cur.executemany(insert_sql, batch)

        # ── 5d. Ref column post-processing ────────────────────
        # For each Ref column: add gristHelper_Display formula column + UPDATE displayCol/visibleCol
        if ref_columns_to_process:
            helper_counter = {}  # table_name -> count of helpers
            for src_tname, col, ref_col_id in ref_columns_to_process:
                ref_info = col['_ref_info']
                target_tname = ref_info['target_table']
                target_col_id = ref_info['target_col_id']

                # Get target column ref ID
                target_col_ref = table_col_refs.get(target_tname, {}).get(target_col_id, 0)
                if not target_col_ref:
                    continue

                # Create gristHelper_Display formula column
                helper_n = helper_counter.get(src_tname, 0) + 1
                helper_counter[src_tname] = helper_n
                helper_colId = 'gristHelper_Display' if helper_n == 1 else f'gristHelper_Display{helper_n}'
                formula = f'${col["col_id"]}.{target_col_id}'

                col_id_ctr += 1
                helper_col_ref = col_id_ctr
                cur.execute(
                    "INSERT INTO _grist_Tables_column VALUES (?,?,?,?,?,?,1,?,?,?,1,0,0,0,NULL,0,NULL)",
                    (helper_col_ref, table_ids[src_tname], 999.0,
                     helper_colId, 'Any', '', formula, '', '')
                )

                # Add column to the SQLite data table (cached formula values, NULL initially)
                cur.execute(f'ALTER TABLE "{src_tname}" ADD COLUMN "{helper_colId}" TEXT')

                # UPDATE the Ref column: set displayCol → helper, visibleCol → target col
                cur.execute(
                    "UPDATE _grist_Tables_column SET displayCol=?, visibleCol=? WHERE id=?",
                    (helper_col_ref, target_col_ref, ref_col_id)
                )

        # ── 6. Widget page: "Carte interactive" ──────────────
        if first_table_name:
            view_id_ctr += 1
            map_view_id = view_id_ctr

            section_id_ctr += 1
            map_section_id = section_id_ctr

            section_id_ctr += 1
            table_section_id = section_id_ctr

            tid = table_ids[first_table_name]
            crefs = table_col_refs[first_table_name]

            layout = json.dumps({
                "children": [
                    {"leaf": map_section_id, "size": 65},
                    {"leaf": table_section_id, "size": 35}
                ]
            })

            # Custom widget with transformed HTML
            custom_view_inner = json.dumps({
                "mode": "url",
                "url": None,
                "widgetDef": {
                    "name": "Custom widget builder",
                    "url": "https://gristlabs.github.io/grist-widget/buildwidget/",
                    "widgetId": "@berhalak/custom-widget-builder",
                    "published": True,
                    "accessLevel": "full",
                    "renderAfterReady": True,
                    "description": "Interactive map from HTML export",
                    "isGristLabsMaintained": False,
                },
                "access": "full",
                "pluginId": "",
                "sectionId": "",
                "renderAfterReady": True,
                "widgetId": "@berhalak/custom-widget-builder",
                "widgetOptions": {
                    "_js": "",
                    "_html": transformed_html
                },
                "columnsMapping": None
            })
            map_options = json.dumps({"customView": custom_view_inner})

            cur.execute("INSERT INTO _grist_Views VALUES (?,?,'',?)",
                        (map_view_id, 'Carte interactive', layout))

            # Custom widget section
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Carte','',0,0,'',?,'','','','',0,0,0,'','','')",
                (map_section_id, tid, map_view_id, 'custom', map_options)
            )

            # Fields for custom widget section
            first_spec = table_specs[0]
            for pos_i, col in enumerate(first_spec['columns']):
                if col['col_id'] in crefs:
                    field_id_ctr += 1
                    cur.execute(
                        "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                        (field_id_ctr, map_section_id, float(pos_i + 1), crefs[col['col_id']])
                    )

            # Grid section below
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Donnees','',0,0,'','','','','','',?,0,0,'','','')",
                (table_section_id, tid, map_view_id, 'record', map_section_id)
            )
            for pos_i, col in enumerate(first_spec['columns']):
                if col['col_id'] in crefs:
                    field_id_ctr += 1
                    cur.execute(
                        "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                        (field_id_ctr, table_section_id, float(pos_i + 1), crefs[col['col_id']])
                    )

            tab_id_ctr += 1
            page_id_ctr += 1
            cur.execute("INSERT INTO _grist_TabBar VALUES (?,?,?)",
                        (tab_id_ctr, map_view_id, float(tab_id_ctr)))
            cur.execute("INSERT INTO _grist_Pages VALUES (?,?,0,?)",
                        (page_id_ctr, map_view_id, float(page_id_ctr)))

        # ── 7. Form page (if detected) ──────────────────────
        form_specs = [s for s in table_specs if s.get('has_form')]
        for fspec in form_specs:
            fname = fspec['table_name']
            ftid = table_ids[fname]
            fcrefs = table_col_refs[fname]

            view_id_ctr += 1
            form_view_id = view_id_ctr

            section_id_ctr += 1
            form_section_id = section_id_ctr
            section_id_ctr += 1
            form_grid_section_id = section_id_ctr

            layout = json.dumps({
                "children": [
                    {"leaf": form_section_id, "size": 50},
                    {"leaf": form_grid_section_id, "size": 50}
                ]
            })

            cur.execute("INSERT INTO _grist_Views VALUES (?,?,'',?)",
                        (form_view_id, f'Saisie {fname}', layout))

            # Form section
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Formulaire','',0,0,'','','','','','',0,0,0,'','','')",
                (form_section_id, ftid, form_view_id, 'form')
            )
            for pos_i, col in enumerate(fspec['columns']):
                if col['col_id'] in fcrefs:
                    field_id_ctr += 1
                    cur.execute(
                        "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                        (field_id_ctr, form_section_id, float(pos_i + 1), fcrefs[col['col_id']])
                    )

            # Grid section
            cur.execute(
                "INSERT INTO _grist_Views_section VALUES (?,?,?,?,'Donnees','',0,0,'','','','','','',0,0,0,'','','')",
                (form_grid_section_id, ftid, form_view_id, 'record')
            )
            for pos_i, col in enumerate(fspec['columns']):
                if col['col_id'] in fcrefs:
                    field_id_ctr += 1
                    cur.execute(
                        "INSERT INTO _grist_Views_section_field VALUES (?,?,?,?,100,'',0,0,'','')",
                        (field_id_ctr, form_grid_section_id, float(pos_i + 1), fcrefs[col['col_id']])
                    )

            tab_id_ctr += 1
            page_id_ctr += 1
            cur.execute("INSERT INTO _grist_TabBar VALUES (?,?,?)",
                        (tab_id_ctr, form_view_id, float(tab_id_ctr)))
            cur.execute("INSERT INTO _grist_Pages VALUES (?,?,0,?)",
                        (page_id_ctr, form_view_id, float(page_id_ctr)))

        # ── 8. ACL ───────────────────────────────────────────
        cur.execute("INSERT INTO _grist_ACLPrincipals VALUES (1,'group','','','Owners','')")
        cur.execute("INSERT INTO _grist_ACLPrincipals VALUES (2,'group','','','Admins','')")
        cur.execute("INSERT INTO _grist_ACLPrincipals VALUES (3,'group','','','Editors','')")
        cur.execute("INSERT INTO _grist_ACLPrincipals VALUES (4,'group','','','Viewers','')")
        cur.execute("INSERT INTO _grist_ACLResources VALUES (1,'','')")
        cur.execute("INSERT INTO _grist_ACLRules VALUES (1,1,63,'[1]','',0,'','',1e999,'','')")

        conn.commit()
        conn.close()

        total_records = sum(len(s['records']) for s in table_specs)
        size = os.path.getsize(grist_path)
        fname = os.path.basename(grist_path)

        return {
            "success": True,
            "path": grist_path,
            "download_url": f"http://localhost:{_API_HOST_PORT}/api/files/{fname}",
            "size_bytes": size,
            "size_mb": round(size / 1024 / 1024, 1),
            "document_name": doc_name,
            "source_html": html_path,
            "tables": len(table_specs),
            "total_records": total_records,
            "form_tables": [s['table_name'] for s in form_specs],
            "references": detected_refs if detected_refs else [],
            "layers": {s['table_name']: len(s['records']) for s in table_specs},
        }

    @staticmethod
    def _grist_parse_html_geojson(html_text):
        """Universal scanner: find all GeoJSON FeatureCollections in any HTML.

        Scans for "FeatureCollection" markers, backtracks to opening brace,
        extracts JSON by brace-counting, and identifies the JS variable name.
        Also handles layersData wrapper arrays (web_map template).

        Returns list of dicts:
          [{"var_name": str, "data": dict, "start": int, "end": int}, ...]
        """
        import re as _re

        results = []
        seen_fingerprints = set()
        marker = '"FeatureCollection"'
        search_start = 0

        while True:
            idx = html_text.find(marker, search_start)
            if idx == -1:
                break
            search_start = idx + len(marker)

            # Backtrack to the opening brace of this JSON object
            brace_start = idx
            while brace_start > 0 and html_text[brace_start] != '{':
                brace_start -= 1
            if html_text[brace_start] != '{':
                continue

            # Extract JSON by brace counting (handles nested objects, strings)
            depth = 0
            i = brace_start
            in_string = False
            escape = False
            json_end = -1
            while i < len(html_text):
                ch = html_text[i]
                if escape:
                    escape = False
                    i += 1
                    continue
                if ch == '\\' and in_string:
                    escape = True
                    i += 1
                    continue
                if ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            json_end = i + 1
                            break
                i += 1

            if json_end == -1:
                continue

            json_str = html_text[brace_start:json_end]

            # Parse and validate
            try:
                data = json.loads(json_str)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") != "FeatureCollection":
                continue
            features = data.get("features", [])

            # Deduplicate by fingerprint (property keys + count)
            if features:
                sample_keys = sorted(features[0].get("properties", {}).keys())
                fp = f"{','.join(sample_keys)}:{len(features)}"
            else:
                fp = f"empty:{brace_start}"
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)

            # Find JS variable name in the 200 chars before the brace
            prefix = html_text[max(0, brace_start - 200):brace_start]
            var_match = _re.search(r'(\w+)\s*=\s*$', prefix)
            var_name = var_match.group(1) if var_match else f"data_{len(results)}"

            results.append({
                "var_name": var_name,
                "data": data,
                "start": brace_start,
                "end": json_end,
            })

        # ── Handle layersData wrapper: array of {name, geojson: {FC}, ...}
        # Detect if any variable is an array containing objects with .geojson FCs
        layer_array_re = _re.compile(r'var\s+(\w+)\s*=\s*\[')
        for m in layer_array_re.finditer(html_text):
            var_name = m.group(1)
            arr_start = m.end() - 1  # position of '['

            # Bracket counting to find end of array
            depth = 0
            i = arr_start
            in_str = False
            esc = False
            arr_end = -1
            while i < len(html_text):
                ch = html_text[i]
                if esc:
                    esc = False
                    i += 1
                    continue
                if ch == '\\' and in_str:
                    esc = True
                    i += 1
                    continue
                if ch == '"':
                    in_str = not in_str
                elif not in_str:
                    if ch == '[':
                        depth += 1
                    elif ch == ']':
                        depth -= 1
                        if depth == 0:
                            arr_end = i + 1
                            break
                i += 1

            if arr_end == -1:
                continue

            arr_str = html_text[arr_start:arr_end]
            try:
                arr_data = json.loads(arr_str)
            except (json.JSONDecodeError, ValueError):
                continue

            if not isinstance(arr_data, list) or len(arr_data) == 0:
                continue

            # Check if this is a wrapper: each element has a .geojson key that is a FC
            has_geojson = all(
                isinstance(el, dict) and isinstance(el.get("geojson"), dict)
                and el["geojson"].get("type") == "FeatureCollection"
                for el in arr_data
            )
            if not has_geojson:
                continue

            # Remove any individually-found FCs that are subsets of this array
            results = [r for r in results
                       if not (r["start"] >= arr_start and r["end"] <= arr_end)]

            # Add each element as a separate FC, tagged as wrapper
            for el_idx, el in enumerate(arr_data):
                fc = el["geojson"]
                el_name = el.get("name", f"Layer_{el_idx}")
                fp = f"wrapper:{var_name}:{el_idx}"
                if fp in seen_fingerprints:
                    continue
                seen_fingerprints.add(fp)

                results.append({
                    "var_name": var_name,
                    "data": fc,
                    "start": arr_start,
                    "end": arr_end,
                    "is_wrapper": True,
                    "wrapper_index": el_idx,
                    "wrapper_total": len(arr_data),
                    "wrapper_element": el,  # keep full element (name, color, legend, etc.)
                    "element_name": el_name,
                })

        return results

    @staticmethod
    def _grist_flatten_coords(coords, flat):
        """Recursively flatten GeoJSON coordinates to list of [lon, lat] pairs."""
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            flat.append(coords)
        elif isinstance(coords[0], list):
            if isinstance(coords[0][0], (int, float)):
                flat.extend(coords)
            else:
                for sub in coords:
                    QGISBridge._grist_flatten_coords(sub, flat)

    @staticmethod
    def _grist_fc_to_spec(var_name, fc, element_name=None):
        """Convert a GeoJSON FeatureCollection to a Grist table spec.

        No QGIS dependency — works purely from GeoJSON data.
        Returns a spec dict compatible with the existing SQLite assembly code.
        """
        import re as _re
        import unicodedata

        def _strip(text):
            return ''.join(ch for ch in unicodedata.normalize('NFKD', text)
                           if not unicodedata.combining(ch))

        def _san_table(name):
            s = _strip(name)
            s = _re.sub(r'[^A-Za-z0-9_]', '_', s).strip('_')
            s = _re.sub(r'_+', '_', s)
            # Clean common prefixes: json_, data_, Data, etc.
            s = _re.sub(r'^(json|var|data)_', '', s, flags=_re.IGNORECASE)
            s = _re.sub(r'_?(Data|Json|Geojson)$', '', s, flags=_re.IGNORECASE)
            if not s or s[0].isdigit():
                s = 'T' + s
            # Capitalize first letter
            return (s[0].upper() + s[1:]) if s else 'Unnamed'

        def _san_col(name):
            s = _strip(name)
            s = _re.sub(r'[^A-Za-z0-9_]', '_', s).strip('_')
            s = _re.sub(r'_+', '_', s)
            if not s or s[0].isdigit():
                s = 'c' + s
            if s.lower() in ('id', 'manualsort'):
                s = s + '_col'
            return s or 'unnamed'

        # Table name
        tname = _san_table(element_name or var_name)

        features = fc.get("features", [])
        if not features:
            return {
                'table_name': tname, 'columns': [], 'records': [],
                'geom_type': 'unknown', 'has_form': False,
            }

        # Detect majority geometry type from first 50 features
        geom_counts = {}
        for f in features[:50]:
            g = f.get("geometry")
            if g:
                gt = g.get("type", "")
                base = gt.replace("Multi", "")
                geom_counts[base] = geom_counts.get(base, 0) + 1

        majority_geom = max(geom_counts, key=geom_counts.get) if geom_counts else "Polygon"
        is_point = majority_geom == "Point"

        # Scan properties across first 50 features — collect all keys + infer types
        all_keys = {}  # key → python type name
        for f in features[:50]:
            props = f.get("properties") or {}
            for k, v in props.items():
                if v is None:
                    continue
                if k not in all_keys:
                    if isinstance(v, bool):
                        all_keys[k] = 'Bool'
                    elif isinstance(v, int):
                        all_keys[k] = 'Int'
                    elif isinstance(v, float):
                        all_keys[k] = 'Numeric'
                    else:
                        all_keys[k] = 'Text'

        # Detect date columns: name pattern + ISO value check
        _date_name_pat = _re.compile(r'(^date_|_date$|^date$|^created|^updated)', _re.IGNORECASE)
        _date_val_pat = _re.compile(r'^\d{4}-\d{2}-\d{2}')
        for k in list(all_keys.keys()):
            if all_keys[k] != 'Text':
                continue
            if not _date_name_pat.search(k):
                continue
            # Verify at least one value looks like a date
            for f in features[:10]:
                v = (f.get("properties") or {}).get(k)
                if v and isinstance(v, str) and _date_val_pat.match(v):
                    all_keys[k] = 'Date'
                    break

        # Build columns
        columns = []
        MAX_COLS = 30
        # Priority: keep _color, skip _geojson-like internal columns
        priority_keys = [k for k in all_keys if k == '_color']
        normal_keys = [k for k in all_keys if k != '_color']
        ordered_keys = priority_keys + normal_keys[:MAX_COLS - len(priority_keys)]

        for k in ordered_keys:
            wo = ''
            if all_keys[k] == 'Date':
                wo = '{"dateFormat": "YYYY-MM-DD"}'
            columns.append({
                'col_id': _san_col(k),
                'grist_type': all_keys[k],
                'label': k,
                'widget_options': wo,
                'original_name': k,
            })

        # Add geometry columns
        if is_point:
            columns.append({'col_id': 'latitude', 'grist_type': 'Numeric', 'label': 'Latitude', 'widget_options': '', 'original_name': ''})
            columns.append({'col_id': 'longitude', 'grist_type': 'Numeric', 'label': 'Longitude', 'widget_options': '', 'original_name': ''})
        else:
            columns.append({'col_id': 'centroid_lat', 'grist_type': 'Numeric', 'label': 'Centroid Lat', 'widget_options': '', 'original_name': ''})
            columns.append({'col_id': 'centroid_lon', 'grist_type': 'Numeric', 'label': 'Centroid Lon', 'widget_options': '', 'original_name': ''})
            columns.append({'col_id': 'geojson', 'grist_type': 'Text', 'label': 'GeoJSON', 'widget_options': '', 'original_name': ''})

        # Extract records
        records = []
        for f in features:
            rec = {}
            props = f.get("properties") or {}

            # Property columns
            for col in columns:
                orig = col.get('original_name', '')
                if orig and orig in props:
                    v = props[orig]
                    # Type coercion
                    if col['grist_type'] == 'Int' and v is not None:
                        try:
                            v = int(v)
                        except (ValueError, TypeError):
                            v = None
                    elif col['grist_type'] == 'Numeric' and v is not None:
                        try:
                            v = float(v)
                        except (ValueError, TypeError):
                            v = None
                    elif col['grist_type'] == 'Date' and v is not None:
                        try:
                            import calendar, time
                            v = calendar.timegm(time.strptime(str(v)[:10], "%Y-%m-%d"))
                        except (ValueError, TypeError):
                            v = None
                    rec[col['col_id']] = v

            # Geometry columns
            geom = f.get("geometry")
            if geom:
                gtype = geom.get("type", "")
                coords = geom.get("coordinates", [])
                if is_point and gtype == "Point" and len(coords) >= 2:
                    rec['latitude'] = round(coords[1], 7)
                    rec['longitude'] = round(coords[0], 7)
                elif is_point and gtype == "MultiPoint" and coords:
                    # Use first point
                    pt = coords[0]
                    if len(pt) >= 2:
                        rec['latitude'] = round(pt[1], 7)
                        rec['longitude'] = round(pt[0], 7)
                else:
                    # Polygon/Line — compute centroid (avg of all coords) + store geojson
                    rec['geojson'] = json.dumps(geom, ensure_ascii=False)
                    flat = []
                    QGISBridge._grist_flatten_coords(coords, flat)
                    if flat:
                        avg_lon = sum(c[0] for c in flat) / len(flat)
                        avg_lat = sum(c[1] for c in flat) / len(flat)
                        rec['centroid_lat'] = round(avg_lat, 7)
                        rec['centroid_lon'] = round(avg_lon, 7)

            records.append(rec)

        return {
            'table_name': tname,
            'columns': columns,
            'records': records,
            'geom_type': majority_geom.lower(),
            'has_form': False,  # set later by _grist_detect_form_tables
        }

    @staticmethod
    def _grist_detect_form_tables(specs):
        """Detect tables with QField-style form patterns.

        - Marks spec['has_form'] = True for tables matching >= 2 form patterns
        - Converts detected choice columns to Grist 'Choice' type with dropdown values
        - Populates widgetOptions with choices list + colored choiceOptions
        """
        import json as _json

        form_patterns = {
            'categorie': 'Choice', 'category': 'Choice', 'type': 'Choice',
            'priorite': 'Choice', 'priority': 'Choice',
            'statut': 'Choice', 'status': 'Choice', 'etat': 'Choice',
            'titre': 'Text', 'title': 'Text', 'nom': 'Text', 'name': 'Text',
            'description': 'Text', 'commentaire': 'Text', 'notes': 'Text',
            'photo': 'Attachments', 'image': 'Attachments', 'attachment': 'Attachments',
        }
        date_re_pat = None
        try:
            import re
            date_re_pat = re.compile(r'(^date_|_date$|^date$)', re.IGNORECASE)
        except Exception:
            pass

        # Grist-compatible color palette for Choice pill backgrounds
        _CHOICE_COLORS = [
            '#4285F4', '#EA4335', '#FBBC04', '#34A853', '#FF6D01',
            '#46BDC6', '#7BAAF7', '#F07B72', '#FCD04F', '#57BB8A',
            '#FF9E80', '#80CBC4', '#9FA8DA', '#F48FB1', '#A5D6A7',
        ]

        for spec in specs:
            matches = 0
            choice_cols = []
            for col in spec['columns']:
                label_lower = col.get('label', '').lower()
                col_id_lower = col['col_id'].lower()

                # Check known patterns
                for pat, grist_type in form_patterns.items():
                    if pat in label_lower or pat in col_id_lower:
                        matches += 1
                        if grist_type == 'Choice':
                            choice_cols.append(col)
                        break

                # Check date pattern
                if date_re_pat and (date_re_pat.search(label_lower) or date_re_pat.search(col_id_lower)):
                    matches += 1

            if matches >= 2:
                spec['has_form'] = True

                # Convert choice columns: set type='Choice' + collect unique values for dropdowns
                for col in choice_cols:
                    col_id = col['col_id']
                    unique_vals = []
                    seen = set()
                    for rec in spec['records']:
                        v = rec.get(col_id)
                        if v is not None and str(v).strip() and str(v) not in seen:
                            seen.add(str(v))
                            unique_vals.append(str(v))
                        if len(unique_vals) >= 50:
                            break

                    # Only convert if reasonable number of unique values (1-30)
                    if unique_vals and len(unique_vals) <= 30:
                        sorted_vals = sorted(unique_vals)
                        choice_opts = {}
                        for i, val in enumerate(sorted_vals):
                            choice_opts[val] = {
                                "fillColor": _CHOICE_COLORS[i % len(_CHOICE_COLORS)],
                                "textColor": "#FFFFFF"
                            }
                        col['grist_type'] = 'Choice'
                        col['widget_options'] = _json.dumps({
                            "choices": sorted_vals,
                            "choiceOptions": choice_opts
                        }, ensure_ascii=False)

    @staticmethod
    def _grist_detect_refs(specs):
        """Detect cross-table references based on naming patterns and value matching.

        Conservative: only triggers when a column name clearly matches
        '{OtherTable}_id' or 'id_{OtherTable}' and >=80% of values match.

        Modifies specs in-place:
        - Sets col['_ref_info'] = {'target_table', 'target_col_id', 'value_to_rowid'}
        - Changes col['grist_type'] to 'Ref:TargetTable'
        - Converts record values from original values to Grist row IDs (1-based)

        Returns list of detected refs for logging.
        """
        if len(specs) < 2:
            return []

        import re as _re

        # Build lookup: table_name_lower -> spec
        tname_map = {s['table_name'].lower(): s for s in specs}

        detected = []

        for spec in specs:
            tname = spec['table_name']
            for col in spec['columns']:
                col_id = col['col_id']
                col_lower = col_id.lower()

                # Skip geometry and system columns
                if col_id in ('latitude', 'longitude', 'centroid_lat', 'centroid_lon',
                              'geojson', 'color', 'manualSort'):
                    continue

                # Try naming patterns: {table}_id, id_{table}, {table}Id, {table}_ref
                for other_spec in specs:
                    if other_spec['table_name'] == tname:
                        continue
                    ot_lower = other_spec['table_name'].lower()
                    patterns = [
                        f'{ot_lower}_id', f'id_{ot_lower}',
                        f'{ot_lower}id', f'ref_{ot_lower}',
                        f'{ot_lower}_ref',
                    ]
                    if col_lower not in patterns:
                        continue

                    # Found a naming match — check value overlap
                    src_vals = set()
                    for rec in spec['records'][:200]:
                        v = rec.get(col_id)
                        if v is not None and str(v).strip():
                            src_vals.add(str(v))
                    if not src_vals:
                        continue

                    # Find best matching column in target table
                    best_match = None
                    best_overlap = 0
                    for target_col in other_spec['columns']:
                        target_vals = set()
                        for rec in other_spec['records'][:500]:
                            v = rec.get(target_col['col_id'])
                            if v is not None and str(v).strip():
                                target_vals.add(str(v))
                        if not target_vals:
                            continue
                        overlap = len(src_vals & target_vals)
                        if overlap > best_overlap:
                            best_overlap = overlap
                            best_match = target_col

                    # Require >= 80% of source values to match
                    if best_match and best_overlap >= len(src_vals) * 0.8:
                        target_col_id = best_match['col_id']
                        # Build value → rowId mapping (Grist row IDs are 1-based)
                        value_to_rowid = {}
                        for i, rec in enumerate(other_spec['records']):
                            v = rec.get(target_col_id)
                            if v is not None:
                                value_to_rowid[str(v)] = i + 1

                        # Convert record values to row IDs
                        for rec in spec['records']:
                            v = rec.get(col_id)
                            if v is not None:
                                rec[col_id] = value_to_rowid.get(str(v), 0)
                            else:
                                rec[col_id] = 0

                        col['grist_type'] = f'Ref:{other_spec["table_name"]}'
                        col['_ref_info'] = {
                            'target_table': other_spec['table_name'],
                            'target_col_id': target_col_id,
                        }
                        detected.append({
                            'src_table': tname, 'src_col': col_id,
                            'target_table': other_spec['table_name'],
                            'target_col': target_col_id,
                        })
                        break  # Found match, stop checking other tables

                    if col.get('_ref_info'):
                        break

        return detected

    @staticmethod
    def _grist_gristify_html(html_text, parsed_fcs, table_specs):
        """Transform HTML: remove inline GeoJSON data, inject Grist bootstrap.

        parsed_fcs: output of _grist_parse_html_geojson()
        table_specs: list of spec dicts from _grist_fc_to_spec(), in same order as parsed_fcs

        IMPORTANT: Bootstrap JS is injected as raw code within the existing <script>
        block — never as a new <script>...</script> which would break the DOM.

        Returns transformed HTML string.
        """
        import re as _re

        # ── 1. Inject Grist API in <head> ────────────────────
        head_close = html_text.find('</head>')
        if head_close == -1:
            head_close = html_text.find('<body')
            if head_close == -1:
                head_close = 0
        grist_api_tag = '\n<script src="https://docs.getgrist.com/grist-plugin-api.js"></script>\n'
        html_text = html_text[:head_close] + grist_api_tag + html_text[head_close:]

        # Recompute positions after insertion (shift all start/end by the inserted length)
        shift = len(grist_api_tag)
        for fc in parsed_fcs:
            fc['start'] += shift
            fc['end'] += shift

        # ── 2. Determine if we have wrapper (layersData) or individual vars ──
        wrapper_fcs = [fc for fc in parsed_fcs if fc.get('is_wrapper')]
        individual_fcs = [fc for fc in parsed_fcs if not fc.get('is_wrapper')]

        # ── 3. Replace inline data with window refs ─────────
        # Data vars may be local (inside IIFE → _continueInit), so we must
        # reference window["varName"] instead of null to pick up bootstrap values.
        replacements = []

        if wrapper_fcs:
            arr_start = wrapper_fcs[0]['start']
            arr_end = wrapper_fcs[0]['end']
            wvar = wrapper_fcs[0]['var_name']
            replacements.append((arr_start, arr_end, f'window["{wvar}"]'))

        for fc in individual_fcs:
            replacements.append((fc['start'], fc['end'], f'window["{fc["var_name"]}"]'))

        # Sort descending to preserve positions
        replacements.sort(key=lambda x: x[0], reverse=True)
        for start, end, replacement in replacements:
            html_text = html_text[:start] + replacement + html_text[end:]

        # ── 4. Build the bootstrap JS (raw code, NO <script> tags) ──
        col_to_fc_js = """
function _colToFC(data) {
  if (!data || !data.id) return {type:"FeatureCollection",features:[]};
  var keys = Object.keys(data).filter(function(k){return k!=='id'&&k!=='manualSort';});
  var geoKeys = {latitude:1,longitude:1,centroid_lat:1,centroid_lon:1,geojson:1};
  var features = [];
  for (var i=0;i<data.id.length;i++) {
    var props={}, geom=null;
    keys.forEach(function(k){
      var v = data[k][i];
      if (k==='geojson') { try{geom=JSON.parse(v);}catch(e){} }
      else if (!geoKeys[k]) { if(v!==null&&v!=='') props[k]=v; }
    });
    if (!geom && data.latitude && data.longitude) {
      geom={type:"Point",coordinates:[data.longitude[i],data.latitude[i]]};
    }
    if (!geom && data.centroid_lat && data.centroid_lon) {
      geom={type:"Point",coordinates:[data.centroid_lon[i],data.centroid_lat[i]]};
    }
    if (geom) features.push({type:"Feature",geometry:geom,properties:props});
  }
  return {type:"FeatureCollection",features:features};
}
"""

        if wrapper_fcs:
            var_name = wrapper_fcs[0]['var_name']
            layers_config = []
            for fc_info, spec in zip(wrapper_fcs, [s for s in table_specs if s.get('_is_wrapper')]):
                el = fc_info.get('wrapper_element', {})
                layers_config.append({
                    'table': spec['table_name'],
                    'name': el.get('name', spec['table_name']),
                    'color': el.get('color', '#3498db'),
                    'geomType': spec.get('geom_type', 'polygon'),
                    'legend': el.get('legend', []),
                    'has_feature_colors': el.get('has_feature_colors', False),
                })

            layers_json = json.dumps(layers_config, ensure_ascii=False)
            bootstrap = f"""
// ── Grist Data Bootstrap ──────────────────────────────────
{col_to_fc_js}
var _GRIST_LAYERS = {layers_json};
var _gristPending = _GRIST_LAYERS.length;
var _gristInitDone = false;

grist.ready({{requiredAccess:'full'}});

function _safeInit() {{
  if (_gristInitDone) return;
  _gristInitDone = true;
  try {{ _continueInit(); }} catch(e) {{ console.error('Map init error:', e); }}
}}

var _layerResults = [];
_GRIST_LAYERS.forEach(function(info, idx){{
  grist.docApi.fetchTable(info.table).then(function(d){{
    _layerResults[idx] = {{name:info.name, color:info.color, geojson:_colToFC(d), feature_count:d.id.length, legend:info.legend, has_feature_colors:info.has_feature_colors}};
    if(--_gristPending<=0) {{ {var_name} = _layerResults.filter(Boolean); _safeInit(); }}
  }}).catch(function(e){{
    console.error('Grist fetch error for '+info.table+':',e);
    _layerResults[idx] = {{name:info.name, color:info.color, geojson:{{type:"FeatureCollection",features:[]}}, feature_count:0, legend:info.legend}};
    if(--_gristPending<=0) {{ {var_name} = _layerResults.filter(Boolean); _safeInit(); }}
  }});
}});

"""
        else:
            tables_config = {}
            for fc_info, spec in zip(individual_fcs, [s for s in table_specs if not s.get('_is_wrapper')]):
                tables_config[spec['table_name']] = {
                    'varName': fc_info['var_name'],
                    'geomType': spec.get('geom_type', 'polygon'),
                }

            tables_json = json.dumps(tables_config, ensure_ascii=False)
            bootstrap = f"""
// ── Grist Data Bootstrap ──────────────────────────────────
{col_to_fc_js}
var _GRIST_TABLES = {tables_json};
var _gristPending = Object.keys(_GRIST_TABLES).length;
var _gristInitDone = false;

grist.ready({{requiredAccess:'full'}});

function _safeInit() {{
  if (_gristInitDone) return;
  _gristInitDone = true;
  try {{ _continueInit(); }} catch(e) {{ console.error('Map init error:', e); }}
}}

Object.keys(_GRIST_TABLES).forEach(function(tname){{
  var info = _GRIST_TABLES[tname];
  grist.docApi.fetchTable(tname).then(function(d){{
    window[info.varName] = _colToFC(d);
    if(--_gristPending<=0) _safeInit();
  }}).catch(function(e){{
    console.error('Grist fetch error for '+tname+':',e);
    window[info.varName] = {{type:"FeatureCollection",features:[]}};
    if(--_gristPending<=0) _safeInit();
  }});
}});

"""

        # ── 5. Insert bootstrap + wrap init in _continueInit ─
        # All injection is raw JS within the existing <script> block.
        # Strategy:
        #   - IIFE: replace `(function() {` with bootstrap + `function _continueInit() {`
        #           replace `})();` with `}`
        #   - No IIFE: insert bootstrap + `function _continueInit() {` before init code,
        #              add `}` before the closing `</script>`

        iife_match = _re.search(r'\(function\s*\(\s*\)\s*\{', html_text)

        if iife_match:
            # Replace IIFE opening with bootstrap + _continueInit function
            iife_start = iife_match.start()
            iife_end = iife_match.end()

            html_text = (html_text[:iife_start]
                         + '\n' + bootstrap
                         + 'function _continueInit() {\n'
                         + html_text[iife_end:])

            # Find and replace the IIFE closing: })(); → }
            # Search from after our insertion
            search_from = iife_start + len(bootstrap) + len('function _continueInit() {\n')
            close_iife = _re.search(r'\}\s*\)\s*\(\s*\)\s*;?', html_text[search_from:])
            if close_iife:
                close_start = search_from + close_iife.start()
                close_end = search_from + close_iife.end()
                html_text = html_text[:close_start] + '\n}\n' + html_text[close_end:]
        else:
            # No IIFE — find first executable init code after the last nullified data
            init_patterns = [
                r'var\s+map\s*=',
                r'L\.map\s*\(',
                r'document\s*\.\s*addEventListener',
                r'window\s*\.\s*onload',
            ]
            combined_pattern = '|'.join(init_patterns)

            last_null_pos = 0
            for fc in parsed_fcs:
                end_pos = fc.get('end', 0)
                if end_pos > last_null_pos:
                    last_null_pos = end_pos

            init_match = _re.search(combined_pattern, html_text[last_null_pos:])
            if init_match:
                insert_pos = last_null_pos + init_match.start()
                line_start = html_text.rfind('\n', 0, insert_pos) + 1

                # Insert bootstrap + _continueInit opening as raw JS
                html_text = (html_text[:line_start]
                             + '\n' + bootstrap
                             + 'function _continueInit() {\n'
                             + html_text[line_start:])

                # Find the closing </script> and add } just before it
                close_offset = line_start + len(bootstrap) + len('function _continueInit() {\n') + 50
                script_close = html_text.find('</script>', close_offset)
                if script_close != -1:
                    html_text = html_text[:script_close] + '\n}\n' + html_text[script_close:]
            else:
                # Fallback: inject as a new script block at end of body
                body_close = html_text.rfind('</body>')
                if body_close == -1:
                    body_close = len(html_text)
                html_text = (html_text[:body_close]
                             + '\n<script>\n' + bootstrap
                             + 'function _continueInit() { /* no init code found */ }\n'
                             + '</script>\n'
                             + html_text[body_close:])

        return html_text

    @staticmethod
    def _grist_create_meta_tables(c):
        """Create all 26 required Grist meta-tables (schema version 46)."""
        c.execute("""CREATE TABLE _grist_DocInfo (
            id INTEGER PRIMARY KEY, docId TEXT, peers TEXT, basketId TEXT,
            schemaVersion INTEGER, timezone TEXT, documentSettings TEXT)""")
        c.execute("""CREATE TABLE _grist_Tables (
            id INTEGER PRIMARY KEY, tableId TEXT, primaryViewId INTEGER,
            summarySourceTable INTEGER, onDemand INTEGER,
            rawViewSectionRef INTEGER, recordCardViewSectionRef INTEGER)""")
        c.execute("""CREATE TABLE _grist_Tables_column (
            id INTEGER PRIMARY KEY, parentId INTEGER, parentPos REAL,
            colId TEXT, type TEXT, widgetOptions TEXT, isFormula INTEGER,
            formula TEXT, label TEXT, description TEXT, untieColIdFromLabel INTEGER,
            summarySourceCol INTEGER, displayCol INTEGER, visibleCol INTEGER,
            rules TEXT, recalcWhen INTEGER, recalcDeps TEXT)""")
        c.execute("""CREATE TABLE _grist_Views (
            id INTEGER PRIMARY KEY, name TEXT, type TEXT, layoutSpec TEXT)""")
        c.execute("""CREATE TABLE _grist_Views_section (
            id INTEGER PRIMARY KEY, tableRef INTEGER, parentId INTEGER,
            parentKey TEXT, title TEXT, description TEXT, defaultWidth INTEGER,
            borderWidth INTEGER, theme TEXT, options TEXT, chartType TEXT,
            layoutSpec TEXT, filterSpec TEXT, sortColRefs TEXT,
            linkSrcSectionRef INTEGER, linkSrcColRef INTEGER,
            linkTargetColRef INTEGER, embedId TEXT, rules TEXT, shareOptions TEXT)""")
        c.execute("""CREATE TABLE _grist_Views_section_field (
            id INTEGER PRIMARY KEY, parentId INTEGER, parentPos REAL,
            colRef INTEGER, width INTEGER, widgetOptions TEXT,
            displayCol INTEGER, visibleCol INTEGER, filter TEXT, rules TEXT)""")
        c.execute("""CREATE TABLE _grist_TabBar (
            id INTEGER PRIMARY KEY, viewRef INTEGER, tabPos REAL)""")
        c.execute("""CREATE TABLE _grist_Pages (
            id INTEGER PRIMARY KEY, viewRef INTEGER, indentation INTEGER, pagePos REAL)""")
        c.execute("""CREATE TABLE _grist_ACLResources (
            id INTEGER PRIMARY KEY, tableId TEXT, colIds TEXT)""")
        c.execute("""CREATE TABLE _grist_ACLRules (
            id INTEGER PRIMARY KEY, resource INTEGER, permissions INTEGER,
            principals TEXT, aclFormula TEXT, aclColumn INTEGER,
            aclFormulaParsed TEXT, permissionsText TEXT, rulePos REAL,
            userAttributes TEXT, memo TEXT)""")
        c.execute("""CREATE TABLE _grist_ACLPrincipals (
            id INTEGER PRIMARY KEY, type TEXT, userEmail TEXT,
            userName TEXT, groupName TEXT, instanceId TEXT)""")
        c.execute("""CREATE TABLE _grist_ACLMemberships (
            id INTEGER PRIMARY KEY, parent INTEGER, child INTEGER)""")
        c.execute("""CREATE TABLE _grist_Attachments (
            id INTEGER PRIMARY KEY, fileIdent TEXT, fileName TEXT, fileType TEXT,
            fileSize INTEGER, fileExt TEXT, imageHeight INTEGER, imageWidth INTEGER,
            timeDeleted REAL, timeUploaded REAL)""")
        c.execute("""CREATE TABLE _grist_Filters (
            id INTEGER PRIMARY KEY, viewSectionRef INTEGER, colRef INTEGER,
            filter TEXT, pinned INTEGER)""")
        c.execute("""CREATE TABLE _grist_Shares (
            id INTEGER PRIMARY KEY, linkId TEXT, options TEXT, label TEXT, description TEXT)""")
        c.execute("""CREATE TABLE _grist_Validations (
            id INTEGER PRIMARY KEY, formula TEXT, name TEXT, tableRef INTEGER)""")
        c.execute("""CREATE TABLE _grist_Cells (
            id INTEGER PRIMARY KEY, tableRef INTEGER, colRef INTEGER, rowId INTEGER,
            root INTEGER, parentId INTEGER, type INTEGER, content TEXT,
            userRef TEXT, timeCreated REAL, timeUpdated REAL, resolved INTEGER)""")
        c.execute("""CREATE TABLE _grist_Triggers (
            id INTEGER PRIMARY KEY, tableRef INTEGER, eventTypes TEXT,
            isReadyColRef INTEGER, actions TEXT, label TEXT, memo TEXT,
            enabled INTEGER, watchedColRefList TEXT, options TEXT, condition TEXT)""")
        c.execute("""CREATE TABLE _grist_REPL_Hist (
            id INTEGER PRIMARY KEY, code TEXT, outputText TEXT, errorText TEXT)""")
        c.execute("""CREATE TABLE _grist_Imports (
            id INTEGER PRIMARY KEY, tableRef INTEGER, origFileName TEXT,
            parseFileSchemaId TEXT, delimiter TEXT, doMergeFields TEXT,
            mergeFieldsRaw TEXT, destTableId TEXT, transformSectionRef INTEGER,
            hiddenTableRef INTEGER)""")
        c.execute("""CREATE TABLE _grist_External_database (
            id INTEGER PRIMARY KEY, host TEXT, port INTEGER, database TEXT,
            type TEXT, credentials TEXT)""")
        c.execute("""CREATE TABLE _grist_External_table (
            id INTEGER PRIMARY KEY, tableRef INTEGER, databaseRef INTEGER,
            transformSectionRef INTEGER, queryFormula TEXT, tableName TEXT)""")
        c.execute("""CREATE TABLE _grist_TableViews (
            id INTEGER PRIMARY KEY, tableRef INTEGER, viewRef INTEGER)""")
        c.execute("""CREATE TABLE _grist_Formulas (
            id INTEGER PRIMARY KEY, name TEXT, formula TEXT)""")

        # ── _gristsys_* tables (DocStorage system tables) ────────
        c.execute("""CREATE TABLE _gristsys_Files (
            id INTEGER PRIMARY KEY, ident TEXT UNIQUE, data BLOB, storageId TEXT)""")
        c.execute("""CREATE TABLE _gristsys_Action (
            id INTEGER PRIMARY KEY, "actionNum" BLOB DEFAULT 0,
            "time" BLOB DEFAULT 0, "user" BLOB DEFAULT '',
            "desc" BLOB DEFAULT '', "otherId" BLOB DEFAULT 0,
            "linkId" BLOB DEFAULT 0, "json" BLOB DEFAULT '')""")
        c.execute("""CREATE TABLE _gristsys_Action_step (
            id INTEGER PRIMARY KEY, "parentId" BLOB DEFAULT 0,
            "type" BLOB DEFAULT '', "tableName" BLOB DEFAULT '',
            "colNames" BLOB DEFAULT '', "values" BLOB DEFAULT '',
            "json" BLOB DEFAULT '')""")
        c.execute("""CREATE TABLE _gristsys_ActionHistory (
            id INTEGER PRIMARY KEY, actionHash TEXT UNIQUE,
            parentRef INTEGER, actionNum INTEGER, body BLOB)""")
        c.execute("""CREATE TABLE _gristsys_ActionHistoryBranch (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE, actionRef INTEGER)""")
        c.execute("INSERT INTO _gristsys_ActionHistoryBranch(name) VALUES('shared')")
        c.execute("INSERT INTO _gristsys_ActionHistoryBranch(name) VALUES('local_sent')")
        c.execute("INSERT INTO _gristsys_ActionHistoryBranch(name) VALUES('local_unsent')")
        c.execute("""CREATE TABLE _gristsys_FileInfo (
            id INTEGER PRIMARY KEY CHECK (id = 0),
            docId TEXT DEFAULT '', ownerInstanceId TEXT DEFAULT '')""")
        c.execute("INSERT INTO _gristsys_FileInfo (id) VALUES (0)")
        c.execute("""CREATE TABLE _gristsys_PluginData (
            id INTEGER PRIMARY KEY, pluginId TEXT NOT NULL,
            key TEXT NOT NULL, value BLOB DEFAULT '')""")
        c.execute("""CREATE UNIQUE INDEX _gristsys_PluginData_unique_key
            ON _gristsys_PluginData(pluginId, key)""")

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
            if s.get("id") == source_id:
                source = s
                break
        if not source:
            available = [s["id"] for s in catalog.get("sources", []) if "id" in s]
            return {"error": f"Source not found: {source_id}", "available": available}

        src_type = source["type"]
        name = params.get("name", "") or source["name"]
        src_params = {**source.get("params", {})}
        bbox = params.get("bbox")  # [xmin, ymin, xmax, ymax] in EPSG:4326

        layer = None

        if src_type == "wfs":
            # Auto-derive bbox from canvas if not provided
            if not bbox:
                if self.iface and self.iface.mapCanvas():
                    canvas_ext = self.iface.mapCanvas().extent()
                    if not canvas_ext.isEmpty():
                        transform = QgsCoordinateTransform(
                            QgsProject.instance().crs(),
                            QgsCoordinateReferenceSystem("EPSG:4326"),
                            QgsProject.instance()
                        )
                        ext_4326 = transform.transformBoundingBox(canvas_ext)
                        bbox = [ext_4326.xMinimum(), ext_4326.yMinimum(),
                                ext_4326.xMaximum(), ext_4326.yMaximum()]
                if not bbox:
                    return {"error": "WFS sources require a bbox. Zoom to an area first, or provide bbox [xmin,ymin,xmax,ymax] in EPSG:4326. Example: bbox=[2.3, 48.8, 2.4, 48.9] for central Paris."}
            typename = src_params.get("typename", "")
            srsname = src_params.get("srsname", "EPSG:4326")
            uri = (f"url='{source['url']}' typename='{typename}' "
                   f"srsname='{srsname}' "
                   f"bbox='{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}' "
                   f"pagingEnabled='true'")
            if params.get("max_features"):
                uri += f" maxNumFeatures='{params['max_features']}'"
            layer = QgsVectorLayer(uri, name, "WFS")
            if not layer.isValid():
                return {"error": f"Invalid WFS layer: {typename}", "uri": uri}
            if params.get("sql_filter"):
                if not layer.setSubsetString(params["sql_filter"]):
                    return {"error": f"Invalid filter: {params['sql_filter']}",
                            "fields": [f.name() for f in layer.fields()]}

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

        result = self._finalize_add_layer(layer)
        result["success"] = True
        result["source_id"] = source_id
        result["type"] = src_type
        if bbox:
            result["bbox_used"] = bbox
        return result

    # ── Study Zone & Smart Load ───────────────────────────────────

    def _action_set_study_zone(self, params: dict) -> dict:
        """Define the study zone. Geocodes target, stores bbox, zooms canvas."""
        import qgis_helpers
        target = params.get("target", "")
        if not target:
            return {"error": "target is required (commune name, address, or bbox [xmin,ymin,xmax,ymax])"}
        buffer_km = params.get("buffer_km", 2)
        return qgis_helpers.set_study_zone(target, buffer_km=buffer_km)

    def _action_get_study_zone(self, params: dict) -> dict:
        """Get the current study zone (name, bbox in 4326 and 2154)."""
        import qgis_helpers
        return qgis_helpers.get_study_zone()

    def _action_smart_load(self, params: dict) -> dict:
        """Smart load from catalog: WFS→local GPKG via ogr2ogr, raster→streaming."""
        import qgis_helpers
        source_id = params.get("id", "")
        if not source_id:
            return {"error": "id is required (catalog source ID, e.g. 'bdtopo_batiments')"}

        catalog = self._load_datasources_catalog()
        source = next((s for s in catalog.get("sources", [])
                       if s.get("id") == source_id), None)
        if not source:
            available = [s["id"] for s in catalog.get("sources", []) if "id" in s]
            return {"error": f"Source not found: {source_id}", "available": available}

        src_type = source["type"]
        name = params.get("name") or source["name"]

        # WFS → download via ogr2ogr as local GPKG
        if src_type == "wfs":
            bbox = params.get("bbox")
            native_crs = source.get("native_crs", "EPSG:2154")
            max_features = params.get("max_features", None)  # None = unlimited (bbox is the real filter)
            result = qgis_helpers.download_wfs_ogr(
                url=source["url"],
                typename=source["params"]["typename"],
                bbox_4326=bbox,
                native_crs=native_crs,
                max_features=max_features,
                name=name,
            )
            result["source_id"] = source_id
            result["method"] = "download"
            return result

        # Raster (WMS/WMTS/XYZ) → delegate to existing catalog handler
        return self._action_add_from_catalog({
            "id": source_id,
            "name": name,
            "bbox": params.get("bbox"),
        })

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
# MAIN THREAD DISPATCH — Critical for stability
# ══════════════════════════════════════════════════════════════════
#
# Qt objects (layers, canvas, project) MUST be manipulated from the
# main thread. The socket server runs in a background thread, so we
# use QCoreApplication.postEvent() (thread-safe) to marshal all
# bridge actions onto the main Qt event loop.
#
# Without this, QGIS crashes with SIGSEGV due to:
#   "QObject::setParent: Cannot set parent, new parent is in a different thread"
#   "QObject::startTimer: Timers cannot be started from another thread"

from qgis.PyQt.QtCore import QEvent, QCoreApplication, QObject as _QObject


class _InvokeEvent(QEvent):
    """Custom QEvent that carries a callable to execute on the main thread."""
    EVENT_TYPE = QEvent.Type(QEvent.registerEventType())

    def __init__(self, fn, holder, done):
        super().__init__(self.EVENT_TYPE)
        self.fn = fn
        self.holder = holder
        self.done = done


class _MainThreadReceiver(_QObject):
    """QObject living in the main thread that processes _InvokeEvents."""
    def event(self, e):
        if isinstance(e, _InvokeEvent):
            try:
                e.holder['result'] = e.fn()
            except Exception as ex:
                e.holder['error'] = ex
            finally:
                e.done.set()
            return True
        return super().event(e)


# Created at module load time = main thread
_main_receiver = _MainThreadReceiver()


def _run_on_main_thread(fn, timeout=120):
    """Execute fn on the Qt main thread, blocking the calling thread until done.

    Uses QCoreApplication.postEvent() which is explicitly thread-safe.
    The _MainThreadReceiver processes the event on the main event loop.
    """
    holder = {}
    done = threading.Event()
    QCoreApplication.postEvent(_main_receiver, _InvokeEvent(fn, holder, done))
    if not done.wait(timeout):
        return {"error": f"Main thread execution timed out ({timeout}s). "
                "The QGIS main thread may be blocked by a long operation."}
    if 'error' in holder:
        err = holder['error']
        return {"error": str(err), "traceback": traceback.format_exception(type(err), err, err.__traceback__)}
    return holder.get('result', {})


# ══════════════════════════════════════════════════════════════════
# SOCKET SERVER (runs in background thread)
# ══════════════════════════════════════════════════════════════════
#
# Three request modes share the same UNIX socket:
#
#   sync (default, backward-compatible):
#     request:  {"action": "...", "params": {...}}
#     response: single JSON blob, then connection close.
#
#   async (new, opt-in via "_async": true):
#     request:  {"action": "...", "params": {...}, "_async": true, "_job_id": "..."?}
#     response: newline-delimited JSON frames on the same connection:
#                 {"_ack":       {"job_id", "submitted_at"}}
#                 {"_heartbeat": {"job_id", "ts", "qt_lag_ms", "stage", "warning"?}}
#                 ...
#                 {"_result":    {"job_id", "success", "result"|"error", "finished_at"}}
#               then connection close.
#
#   status (new, short-circuit):
#     request:  {"action": "_job_status", "params": {"job_id": "..."}}
#     response: single JSON blob — read from _JOB_STATUS_MIRROR, never touches
#               the Qt main thread, so answers even when QGIS is frozen.
#
#   cancel (new, short-circuit, best-effort):
#     request:  {"action": "_cancel_job", "params": {"job_id": "..."}}
#     response: single JSON blob. Flips cancel_requested on the active job.
#               Only effective before dispatch; once Qt main has the job, it
#               returns "already_dispatched_cannot_cancel".
#
# Each incoming connection is handled in its own daemon thread so the listener
# never blocks on a heavy job and status probes can run while work is pending.

def _generate_job_id():
    import uuid
    return uuid.uuid4().hex[:16]


def _mirror_set(job_id: str, **fields):
    """Update the status mirror, pruning oldest entries if over cap."""
    _JOB_STATUS_MIRROR[job_id] = {**_JOB_STATUS_MIRROR.get(job_id, {}), **fields}
    if len(_JOB_STATUS_MIRROR) > _MIRROR_MAX:
        # Drop oldest by heartbeat_at — cheap approximation of LRU
        victims = sorted(
            _JOB_STATUS_MIRROR.items(),
            key=lambda kv: kv[1].get("heartbeat_at", 0),
        )[: max(1, _MIRROR_MAX // 10)]
        for jid, _ in victims:
            _JOB_STATUS_MIRROR.pop(jid, None)


def _serve_conn(conn, bridge):
    """Handle a single client connection: sync, async, status, or cancel mode.

    Sync mode preserves the old byte-for-byte protocol (single JSON + close).
    Async mode streams newline-delimited frames.
    Status and cancel modes short-circuit before Qt dispatch.
    """
    try:
        # ── Read request (until JSON parses, bounded by MAX_MESSAGE_SIZE) ──
        data = b""
        parsed = None
        while True:
            try:
                chunk = conn.recv(65536)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_MESSAGE_SIZE:
                try:
                    conn.sendall(json.dumps({
                        "error": f"request exceeds MAX_MESSAGE_SIZE ({MAX_MESSAGE_SIZE} bytes)"
                    }).encode())
                except Exception:
                    pass
                return
            try:
                parsed = json.loads(data.decode())
                break
            except UnicodeDecodeError:
                # Wait for more bytes — UTF-8 multi-byte boundary
                continue
            except json.JSONDecodeError:
                continue

        if parsed is None:
            if data:
                try:
                    conn.sendall(json.dumps({
                        "error": "invalid JSON or incomplete request",
                    }).encode())
                except Exception:
                    pass
            return

        request = parsed
        action = request.get("action", "")
        params = request.get("params", {}) or {}
        is_async = bool(request.get("_async"))

        # ── Status-mode short-circuit: never touches Qt ──
        if action == "_job_status":
            job_id = params.get("job_id", "")
            mirror = _JOB_STATUS_MIRROR.get(job_id)
            if mirror is None:
                resp = {
                    "error": f"unknown job_id: {job_id}",
                    "known_jobs_sample": list(_JOB_STATUS_MIRROR.keys())[:20],
                }
            else:
                resp = {"success": True, "job": mirror}
            try:
                conn.sendall(json.dumps(resp).encode())
            except Exception:
                pass
            return

        # ── Cancel-mode short-circuit ──
        if action == "_cancel_job":
            job_id = params.get("job_id", "")
            with _ASYNC_LOCK:
                info = _ACTIVE_JOBS.get(job_id)
                if info is None:
                    resp = {"success": False, "error": "unknown or already-finished job"}
                elif info.get("stage") in ("queued",):
                    info["cancel_requested"] = True
                    resp = {"success": True, "status": "cancel_pending"}
                else:
                    info["cancel_requested"] = True
                    resp = {"success": False, "status": "already_dispatched_cannot_cancel",
                            "stage": info.get("stage")}
            try:
                conn.sendall(json.dumps(resp).encode())
            except Exception:
                pass
            return

        # ── Async mode: keep conn open, stream frames ──
        if is_async:
            job_id = request.get("_job_id") or _generate_job_id()
            conn_lock = threading.Lock()
            now = time.time()
            with _ASYNC_LOCK:
                _ACTIVE_JOBS[job_id] = {
                    "conn": conn,
                    "lock": conn_lock,
                    "action": action,
                    "submitted_at": now,
                    "started_at": None,
                    "stage": "queued",
                    "cancel_requested": False,
                }
            _mirror_set(job_id,
                        status="queued", stage="queued",
                        submitted_at=now, heartbeat_at=now, action=action)

            # _ack frame
            ack = {"_ack": {"job_id": job_id, "submitted_at": now, "action": action}}
            try:
                with conn_lock:
                    conn.sendall((json.dumps(ack) + "\n").encode())
            except Exception:
                with _ASYNC_LOCK:
                    _ACTIVE_JOBS.pop(job_id, None)
                return

            # Cancel window before dispatch
            with _ASYNC_LOCK:
                info = _ACTIVE_JOBS.get(job_id, {})
                if info.get("cancel_requested"):
                    info["stage"] = "cancelled"
                    cancelled = True
                else:
                    info["stage"] = "dispatched"
                    info["started_at"] = time.time()
                    cancelled = False

            if cancelled:
                _mirror_set(job_id, status="cancelled", stage="cancelled",
                            finished_at=time.time(), heartbeat_at=time.time())
                frame = {"_result": {"job_id": job_id, "success": False,
                                     "error": "cancelled before dispatch",
                                     "finished_at": time.time()}}
                try:
                    with conn_lock:
                        conn.sendall((json.dumps(frame) + "\n").encode())
                except Exception:
                    pass
                with _ASYNC_LOCK:
                    _ACTIVE_JOBS.pop(job_id, None)
                return

            _mirror_set(job_id, status="running", stage="dispatched",
                        started_at=time.time(), heartbeat_at=time.time())

            # Dispatch to Qt main thread (blocks THIS worker thread, not listener)
            user_timeout = params.get("timeout", 600)
            dispatch_timeout = max(int(user_timeout) + 30, 120)
            try:
                response = _run_on_main_thread(
                    lambda req=request: bridge.handle(req),
                    timeout=dispatch_timeout,
                )
            except Exception as e:
                response = {"error": str(e), "traceback": traceback.format_exc()}

            with _ASYNC_LOCK:
                info = _ACTIVE_JOBS.pop(job_id, None)

            finished_at = time.time()
            success = isinstance(response, dict) and "error" not in response
            result_frame = {"_result": {
                "job_id": job_id,
                "success": success,
                "result": response if success else None,
                "error": response.get("error") if (isinstance(response, dict) and not success) else None,
                "finished_at": finished_at,
            }}
            try:
                lock = info["lock"] if info else threading.Lock()
                with lock:
                    conn.sendall((json.dumps(result_frame, default=str) + "\n").encode())
            except Exception:
                pass
            _mirror_set(job_id,
                        status="done" if success else "error",
                        stage="finished",
                        heartbeat_at=finished_at,
                        finished_at=finished_at)
            return

        # ── Sync mode (backward-compatible) ──
        user_timeout = params.get("timeout", 30)
        dispatch_timeout = max(int(user_timeout) + 30, 120)
        response = _run_on_main_thread(
            lambda req=request: bridge.handle(req),
            timeout=dispatch_timeout,
        )
        try:
            conn.sendall(json.dumps(response, default=str).encode())
        except Exception:
            pass

    except Exception as e:
        try:
            conn.sendall(json.dumps({
                "error": str(e),
                "traceback": traceback.format_exc(),
            }).encode())
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def socket_server(bridge: QGISBridge):
    """Listen on UNIX socket, spawn one worker thread per connection.

    The listener itself never blocks on request processing, so a 10-minute
    execute_python on one connection does not prevent a status poll on another.
    """
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(20)
    os.chmod(SOCKET_PATH, 0o777)

    print(f"[QGISBridge] Listening on {SOCKET_PATH}")
    print(f"[QGISBridge] Available actions: {bridge._list_actions()}")

    while True:
        try:
            conn, _ = server.accept()
        except Exception as e:
            print(f"[QGISBridge] accept() failed: {e}", file=sys.stderr)
            time.sleep(0.1)
            continue
        threading.Thread(
            target=_serve_conn, args=(conn, bridge), daemon=True,
            name="bridge-conn",
        ).start()


def _watchdog():
    """Background daemon. Responsibilities:

    1. Probe Qt main thread latency via postEvent → self-timestamp.
       If the main thread is frozen, _LAST_MAIN_TICK stays old and qt_lag_ms
       grows, letting clients detect a frozen QGIS without waiting for timeout.
    2. Stream a _heartbeat frame every ~1s to every active async job's socket,
       so the api_server reader sees progress and the client can poll.
    3. Update _JOB_STATUS_MIRROR so status-mode probes work even if the job's
       own socket has died.
    """
    last_scheduled = 0.0
    # Warm _LAST_MAIN_TICK so the first few iterations don't report huge lag
    _LAST_MAIN_TICK[0] = time.time()
    while True:
        try:
            time.sleep(1.0)
            now = time.time()

            # Compute lag against the previous probe's main-thread tick
            if _LAST_MAIN_TICK[0] > 0:
                qt_lag_ms = max(0, int((now - _LAST_MAIN_TICK[0]) * 1000))
            else:
                qt_lag_ms = 0
            frozen = qt_lag_ms > 5000

            # Schedule next probe (do not wait on done — just fire-and-forget)
            last_scheduled = now
            def _probe():
                _LAST_MAIN_TICK[0] = time.time()
                return None
            try:
                holder = {}
                done = threading.Event()
                QCoreApplication.postEvent(_main_receiver, _InvokeEvent(_probe, holder, done))
            except Exception:
                pass  # Qt may be shutting down

            # Fan out heartbeats
            with _ASYNC_LOCK:
                active = list(_ACTIVE_JOBS.items())

            for job_id, info in active:
                frame = {"_heartbeat": {
                    "job_id": job_id,
                    "ts": now,
                    "qt_lag_ms": qt_lag_ms,
                    "stage": info.get("stage", "running"),
                }}
                if frozen:
                    frame["_heartbeat"]["warning"] = "qt_frozen"
                payload = (json.dumps(frame) + "\n").encode()
                try:
                    with info["lock"]:
                        info["conn"].sendall(payload)
                    _mirror_set(job_id,
                                status="qt_frozen" if frozen else "running",
                                stage=info.get("stage", "running"),
                                qt_lag_ms=qt_lag_ms,
                                heartbeat_at=now)
                except (BrokenPipeError, ConnectionError, OSError):
                    with _ASYNC_LOCK:
                        _ACTIVE_JOBS.pop(job_id, None)
                    _mirror_set(job_id,
                                status="client_disconnected",
                                heartbeat_at=now)
                except Exception:
                    # One bad conn must not kill the watchdog loop
                    pass
        except Exception as e:
            try:
                print(f"[QGISBridge.watchdog] {e}", file=sys.stderr)
            except Exception:
                pass
            # Keep alive — the watchdog is the safety net, it cannot die


# ── Optimize rendering (GPU-aware + CPU fallback) ────────────────
def _optimize_rendering():
    """Apply rendering optimizations after QGIS is fully loaded.

    Reads RENDERING_MODE env var set by entrypoint.sh:
      - "gpu"  → full quality (anti-aliasing, preview jobs, fast refresh)
      - "cpu"  → optimized for llvmpipe (no AA, no previews, slower refresh)
    Common optimizations always applied: caching, parallel rendering,
    geometry simplification for heavy layers.
    """
    try:
        import os
        from qgis.core import QgsSettings
        mode = os.environ.get("RENDERING_MODE", "cpu")
        s = QgsSettings()

        iface = qgis.utils.iface
        if not iface:
            return
        canvas = iface.mapCanvas()

        # ── Common optimizations (always applied) ────────────────
        s.setValue("qgis/enable_render_caching", True)
        s.setValue("qgis/parallel_rendering", True)
        s.setValue("cache/size", 512)  # 512 MB tile cache
        s.setValue("cache/directory", "/tmp/qgis/cache")

        # Geometry simplification — critical for heavy vector layers
        s.setValue("qgis/simplifyDrawingHints", 1)
        s.setValue("qgis/simplifyMaxScale", 1)
        s.setValue("qgis/simplifyDrawingTol", 1.0)
        s.setValue("qgis/simplifyAlgorithm", 0)   # Distance-based
        s.setValue("qgis/simplifyLocal", True)     # Provider-side when possible

        canvas.setParallelRenderingEnabled(True)
        canvas.setCachingEnabled(True)

        # ── Adaptive settings (GPU vs CPU) ───────────────────────
        if mode == "gpu":
            s.setValue("qgis/enable_anti_aliasing", True)
            s.setValue("qgis/main_canvas_preview_jobs", True)
            s.setValue("Map/updateInterval", 100)
            canvas.enableAntiAliasing(True)
            canvas.setMapUpdateInterval(100)
            print("[QGISBridge] Rendering: GPU mode (full quality)")
        else:
            s.setValue("qgis/enable_anti_aliasing", False)
            s.setValue("qgis/main_canvas_preview_jobs", False)
            s.setValue("Map/updateInterval", 500)
            canvas.enableAntiAliasing(False)
            canvas.setMapUpdateInterval(500)
            print("[QGISBridge] Rendering: CPU mode (optimized for llvmpipe)")

    except Exception as e:
        print(f"[QGISBridge] Rendering optimization warning: {e}")

# ── Configure QGIS for container environment ─────────────────────
def _configure_environment():
    """Adapt QGIS runtime: hidden browser paths, rendering, startup project.

    Data source connections (WMS/WFS/XYZ) are pre-seeded by
    setup_qgis_connections.py BEFORE QGIS starts — so they're
    already in the settings file when QGIS initializes its browser.

    This function handles runtime-only settings that need the
    running QGIS instance (browser panel, canvas, project).
    """
    try:
        from qgis.core import QgsSettings
        s = QgsSettings()

        # ── Disable news feed and version check on welcome page ─────
        # The welcome page is replaced by the startup project (see _open_startup_project),
        # but in case it briefly shows, disable non-essential elements.
        s.setValue("qgis/allowVersionCheck", False)
        s.setValue("qgis/checkVersion", False)

        # ── Browser panel: hide filesystem root entirely ──────────────
        # Hide "/" to remove the entire filesystem tree from browser.
        # Also hide Home (/root is useless in container context).
        s.setValue("browser/hiddenPaths", ["/", "/root"])

        # ── Set browser Home to /data/ ───────────────────────────────
        s.setValue("browser/homePath", "/data")

        # ── All file dialogs point to /data/ ─────────────────────────
        s.setValue("UI/lastProjectDir", "/data")
        s.setValue("UI/lastVectorFileFilterDir", "/data")
        s.setValue("UI/lastRasterFileFilterDir", "/data")
        s.setValue("UI/lastFileNameWidgetDir", "/data")

        s.sync()

        # ── Add /data as Favorite + refresh browser ──────────────────
        iface = qgis.utils.iface
        if iface:
            try:
                model = iface.browserModel()
                if model:
                    model.addFavoriteDirectory("/data", "Data")
                    model.reload()
                    print("[QGISBridge] Browser: / hidden, /data set as Home + Favorite")
            except Exception as e:
                print(f"[QGISBridge] Browser refresh: {e}")

            # ── Close Browser panel to prevent crashes ─────────────────
            # Pre-configured WFS/WMS connections trigger simultaneous
            # GetCapabilities requests when the panel is open, crashing QGIS.
            try:
                from qgis.PyQt.QtWidgets import QDockWidget
                mw = iface.mainWindow()
                for dock in mw.findChildren(QDockWidget):
                    if dock.objectName() in ('Browser', 'Browser2'):
                        dock.close()
                print("[QGISBridge] Browser panel closed (prevents connection flood)")
            except Exception as e:
                print(f"[QGISBridge] Browser panel close: {e}")

        # ── Startup project: open existing or create new default ─────
        _open_startup_project()

        print("[QGISBridge] Environment configured for container")
    except Exception as e:
        print(f"[QGISBridge] Environment config warning: {e}")
        traceback.print_exc()


def _zoom_to_valid_layers(iface):
    """Frame the canvas on layers that actually have an extent.

    zoomToFullExtent() aggregates every layer, including those reporting an
    empty or undefined extent — typically a WMS layer the provider could not
    query, which reports a 0x0 size. A single such layer produces an absurd
    extent: real data collapses to an invisible dot and the canvas looks
    blank even though valid layers are loaded. Observed in production with an
    elevation WMS serving 32-bit TIFF, which QGIS cannot decode.

    Falls back to zoomToFullExtent when no layer qualifies.
    """
    extent = None
    skipped = []
    try:
        for layer in QgsProject.instance().mapLayers().values():
            try:
                if not layer.isValid():
                    skipped.append(layer.name())
                    continue
                ext = layer.extent()
                if ext is None or ext.isEmpty() or ext.isNull():
                    skipped.append(layer.name())
                    continue
                if extent is None:
                    extent = ext
                else:
                    # combineExtentWith mutates in place and returns nothing.
                    extent.combineExtentWith(ext)
            except Exception:
                skipped.append(getattr(layer, "name", lambda: "?")())
    except Exception as exc:
        print(f"[QGISBridge] Extent computation failed: {exc}")
        extent = None

    try:
        if extent is not None and not extent.isEmpty():
            extent.scale(1.05)  # breathing room around the data
            iface.mapCanvas().setExtent(extent)
        else:
            iface.mapCanvas().zoomToFullExtent()
    except Exception:
        try:
            iface.mapCanvas().zoomToFullExtent()
        except Exception:
            pass
    if skipped:
        print(f"[QGISBridge] Layers ignored when framing (no usable extent): {skipped}")


def _open_startup_project():
    """Open a project on startup so QGIS never shows the empty welcome screen.

    Behavior:
    - If a study is marked active, reopen that study's project
    - Else if QGIS_PROJECT env var is set and the file exists → open it
    - Otherwise → create a new default project (EPSG:2154, OTF enabled)

    The active-study lookup was added on 2026-08-22. QGIS can restart on its
    own inside the pod: supervisord respawns it after a crash, which was
    observed in production (`exited: qgis (exit status 1)` followed by
    `spawned: 'qgis'`). The pod itself never restarts, so the hub still sees
    a healthy workspace and never replays the activation — QGIS came back on
    an empty project while the UI kept showing the study as open.

    Reading the sentinel written by the hub (`/data/.active_study`) makes the
    recovery self-contained: whatever killed QGIS, it comes back on the right
    project. Covers a spontaneous crash as well as POST /api/restart_qgis.
    """
    try:
        project = QgsProject.instance()
        iface = qgis.utils.iface
        project_path = os.environ.get("QGIS_PROJECT", "").strip()

        # Active study takes precedence over the env var: it reflects what the
        # user is actually working on, the env var only a boot-time default.
        try:
            sentinel = "/data/.active_study"
            if os.path.isfile(sentinel):
                with open(sentinel, encoding="utf-8") as fh:
                    sid = fh.read().strip()
                candidate = f"/data/studies/{sid}/project.qgz"
                if sid and os.path.isfile(candidate):
                    project_path = candidate
                    print(f"[QGISBridge] Active study {sid} -> {candidate}")
        except Exception as exc:
            print(f"[QGISBridge] Could not read active study sentinel: {exc}")

        if project_path and os.path.isfile(project_path):
            # Open existing project
            ok = project.read(project_path)
            if ok:
                print(f"[QGISBridge] Opened startup project: {project_path}")
                if iface and iface.mapCanvas():
                    _zoom_to_valid_layers(iface)
                    iface.mapCanvas().refresh()
                return
            else:
                print(f"[QGISBridge] WARNING: Failed to open {project_path}, creating default project")
        elif project_path:
            print(f"[QGISBridge] WARNING: QGIS_PROJECT={project_path} not found, creating default project")

        # Create a fresh default project
        project.clear()
        project.setCrs(QgsCoordinateReferenceSystem("EPSG:2154"))
        project.setTitle("QgisRemoteMCP Project")

        # Enable OTF reprojection
        from qgis.core import QgsSettings
        QgsSettings().setValue("/Projections/otfTransformAutoEnable", True)

        if iface and iface.mapCanvas():
            iface.mapCanvas().refresh()

        print("[QGISBridge] Created default project (EPSG:2154, OTF enabled)")

    except Exception as e:
        print(f"[QGISBridge] Startup project warning: {e}")
        traceback.print_exc()


# Apply after QGIS is fully loaded
QTimer.singleShot(3000, _optimize_rendering)
QTimer.singleShot(5000, _configure_environment)


# ── Start bridge ──────────────────────────────────────────────────
bridge = QGISBridge()
server_thread = threading.Thread(target=socket_server, args=(bridge,), daemon=True,
                                 name="bridge-listener")
server_thread.start()
watchdog_thread = threading.Thread(target=_watchdog, daemon=True, name="bridge-watchdog")
watchdog_thread.start()
print("[QGISBridge] Started successfully (listener + watchdog)")
