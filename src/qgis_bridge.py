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
        "apply_layout_template", "export_web_map", "export_flood_map", "export_temporal_map", "export_qfield",
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
            with self._lock:
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

        return {
            "title": project.title() or project.fileName(),
            "file": project.fileName(),
            "crs": project.crs().authid(),
            "crs_description": project.crs().description(),
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
        title = params.get("title", "BigQgisMCP Project")
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
        """Capture screenshot. Priority: QgsMapRenderer > Qt screen grab > ffmpeg.

        QgsMapRendererSequentialJob renders directly to a QImage in memory,
        bypassing the X11 framebuffer entirely. This produces correct output
        even when Mesa/llvmpipe doesn't flush the canvas to Xvfb properly.
        """
        from qgis.PyQt.QtCore import QBuffer, QIODevice

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
                    buf = QBuffer()
                    buf.open(QIODevice.WriteOnly)
                    img.save(buf, "PNG", 85)
                    data = bytes(buf.data())
                    buf.close()
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
                    buf = QBuffer()
                    buf.open(QIODevice.WriteOnly)
                    pixmap.save(buf, "PNG", 80)
                    data = bytes(buf.data())
                    buf.close()
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

    RECIPES_DIR = Path("/app/recipes")

    def _action_list_recipes(self, params: dict) -> dict:
        """List available workflow recipes."""
        recipes = []
        if self.RECIPES_DIR.is_dir():
            for rpath in sorted(self.RECIPES_DIR.glob("*.json")):
                try:
                    data = json.loads(rpath.read_text(encoding="utf-8"))
                    recipes.append({
                        "id": data.get("id", rpath.stem),
                        "name": data.get("name", rpath.stem),
                        "description": data.get("description", ""),
                        "tags": data.get("tags", []),
                        "parameters": data.get("parameters", {}),
                    })
                except Exception:
                    pass
        return {"recipes": recipes}

    def _action_get_recipe(self, params: dict) -> dict:
        """Get a specific recipe with parameter substitution."""
        recipe_id = params.get("id", "")
        if not recipe_id:
            return {"error": "Missing 'id' parameter"}

        recipe_path = self.RECIPES_DIR / f"{recipe_id}.json"
        if not recipe_path.exists():
            available = [p.stem for p in self.RECIPES_DIR.glob("*.json")] if self.RECIPES_DIR.is_dir() else []
            return {"error": f"Recipe not found: {recipe_id}",
                    "available_recipes": available}

        data = json.loads(recipe_path.read_text(encoding="utf-8"))

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
            "download_url": f"http://localhost:8080/api/files/{fname}",
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

        def to_geojson_features(layer, limit, extra_props_fn=None, fields_filter=None):
            """Export layer features as GeoJSON dicts in EPSG:4326.
            fields_filter: optional set of field names to include (None = all)."""
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
                layer, max_features, add_flood_props, fields_filter=include_fields))

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

                flood_features.extend(to_geojson_features(layer, max_features, add_synth_props))

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

        html = template.replace("{{TITLE}}", title)
        html = html.replace("{{CENTER}}", json.dumps(center))
        html = html.replace("{{ZOOM}}", str(zoom))
        html = html.replace("{{FLOOD_JSON}}", json.dumps(flood_geojson, default=str))
        html = html.replace("{{BUILDINGS_JSON}}", json.dumps(buildings_geojson, default=str))
        html = html.replace("{{SENSITIVE_JSON}}", json.dumps(sensitive_geojson, default=str))
        html = html.replace("{{MAX_HEIGHT}}", str(round(max_height, 1)))
        html = html.replace("{{ZONE_NAME}}", zone_name)

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
            "download_url": f"http://localhost:8080/api/files/{fname}",
            "size_bytes": size,
            "title": title,
            "zone": zone_name,
            "max_water_height_m": round(max_height, 1),
            "flood_polygons": len(flood_features),
            "buildings": len(building_features),
            "buildings_exposed": exposed_buildings,
            "buildings_pct": round(exposed_buildings / max(len(building_features), 1) * 100, 1),
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
            "download_url": f"http://localhost:8080/api/files/{fname}",
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
            result["download_url"] = f"http://localhost:8080/api/files/{fpath.name}"
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
            "download_url": f"http://localhost:8080/api/files/{zip_name}",
            "size_bytes": zip_size,
            "size_mb": round(zip_size / 1024 / 1024, 1),
            "project_name": project_name,
            "layers_exported": len(exported_layers),
            "total_features": total_features,
            "has_observations_layer": include_obs,
            "layers": {lid: info["original_name"] for lid, info in exported_layers.items()},
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
            max_features = params.get("max_features", 10000)
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
                # Compute timeout: user timeout + margin for main thread dispatch
                user_timeout = request.get("params", {}).get("timeout", 30)
                dispatch_timeout = max(user_timeout + 30, 120)
                # Dispatch to main thread — all Qt/QGIS ops must happen there
                response = _run_on_main_thread(
                    lambda req=request: bridge.handle(req),
                    timeout=dispatch_timeout,
                )
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


def _open_startup_project():
    """Open a project on startup so QGIS never shows the empty welcome screen.

    Behavior:
    - If QGIS_PROJECT env var is set and the file exists → open it
    - Otherwise → create a new default project (EPSG:2154, OTF enabled)
    """
    try:
        project = QgsProject.instance()
        iface = qgis.utils.iface
        project_path = os.environ.get("QGIS_PROJECT", "").strip()

        if project_path and os.path.isfile(project_path):
            # Open existing project
            ok = project.read(project_path)
            if ok:
                print(f"[QGISBridge] Opened startup project: {project_path}")
                if iface and iface.mapCanvas():
                    iface.mapCanvas().refresh()
                return
            else:
                print(f"[QGISBridge] WARNING: Failed to open {project_path}, creating default project")
        elif project_path:
            print(f"[QGISBridge] WARNING: QGIS_PROJECT={project_path} not found, creating default project")

        # Create a fresh default project
        project.clear()
        project.setCrs(QgsCoordinateReferenceSystem("EPSG:2154"))
        project.setTitle("BigQgisMCP Project")

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
server_thread = threading.Thread(target=socket_server, args=(bridge,), daemon=True)
server_thread.start()
print("[QGISBridge] Started successfully")
