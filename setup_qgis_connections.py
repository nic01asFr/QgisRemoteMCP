#!/usr/bin/env python3
"""
QgisStreamMCP — Pre-seed QGIS data source connections
═══════════════════════════════════════════════════════════════════

Runs BEFORE QGIS starts. Writes WMS/WMTS/WFS/XYZ connections
to the QGIS profile settings file so they appear immediately
in the QGIS browser panel on startup.

Connections are deduplicated: one per server URL (not per layer).
"""

import json
import os
import sys
from pathlib import Path

# No display needed for QSettings
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt5.QtCore import QSettings, QCoreApplication

# QCoreApplication required for QSettings to work
app = QCoreApplication(sys.argv)

# ── QGIS profile settings file ──────────────────────────────────
# QgsSettings writes to: <profile>/QGIS/QGIS3.ini using IniFormat
PROFILE_DIR = Path.home() / ".local" / "share" / "QGIS" / "QGIS3" / "profiles" / "default" / "QGIS"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
INI_PATH = str(PROFILE_DIR / "QGIS3.ini")

CATALOG_PATH = Path("/app/datasources.json")


def main():
    if not CATALOG_PATH.exists():
        print("[setup_connections] No datasources.json found, skipping")
        return

    catalog = json.loads(CATALOG_PATH.read_text())
    sources = catalog.get("sources", [])
    if not sources:
        print("[setup_connections] Empty catalog, skipping")
        return

    s = QSettings(INI_PATH, QSettings.IniFormat)
    count = 0

    # ── Collect unique server URLs per connection type ────────────
    # WMS/WMTS: deduplicate by URL (one connection shows all layers via GetCapabilities)
    wms_servers = {}  # url -> connection_name
    wfs_servers = {}  # url -> connection_name

    for src in sources:
        src_type = src.get("type", "")
        url = src.get("url", "")
        name = src.get("name", "")
        params = src.get("params", {})

        if not url:
            continue

        if src_type == "xyz":
            # XYZ: each tile URL is unique, register individually
            prefix = f"qgis/connections-xyz/{name}"
            s.setValue(f"{prefix}/url", url)
            s.setValue(f"{prefix}/zmin", int(params.get("zmin", 0)))
            s.setValue(f"{prefix}/zmax", int(params.get("zmax", 19)))
            s.setValue(f"{prefix}/authcfg", "")
            s.setValue(f"{prefix}/username", "")
            s.setValue(f"{prefix}/password", "")
            s.setValue(f"{prefix}/referer", "")
            count += 1

        elif src_type in ("wms", "wmts"):
            if url not in wms_servers:
                if "wmts" in url.lower():
                    conn_name = "IGN Geoplateforme WMTS"
                else:
                    conn_name = "IGN Geoplateforme WMS"
                wms_servers[url] = conn_name

        elif src_type == "wfs":
            if url not in wfs_servers:
                wfs_servers[url] = "IGN Geoplateforme WFS"

    # ── Write WMS/WMTS connections (one per server) ──────────────
    for url, conn_name in wms_servers.items():
        prefix = f"qgis/connections-wms/{conn_name}"
        s.setValue(f"{prefix}/url", url)
        s.setValue(f"{prefix}/authcfg", "")
        s.setValue(f"{prefix}/username", "")
        s.setValue(f"{prefix}/password", "")
        s.setValue(f"{prefix}/referer", "")
        s.setValue(f"{prefix}/ignoreGetMapURI", False)
        s.setValue(f"{prefix}/ignoreGetFeatureInfoURI", False)
        s.setValue(f"{prefix}/ignoreAxisOrientation", False)
        s.setValue(f"{prefix}/invertAxisOrientation", False)
        s.setValue(f"{prefix}/smoothPixmapTransform", False)
        s.setValue(f"{prefix}/dpiMode", 7)
        count += 1

    # ── Write WFS connections (one per server) ───────────────────
    for url, conn_name in wfs_servers.items():
        prefix = f"qgis/connections-wfs/{conn_name}"
        s.setValue(f"{prefix}/url", url)
        s.setValue(f"{prefix}/version", "auto")
        s.setValue(f"{prefix}/maxnumfeatures", "")
        s.setValue(f"{prefix}/pagesize", "")
        s.setValue(f"{prefix}/pagingenabled", True)
        s.setValue(f"{prefix}/authcfg", "")
        s.setValue(f"{prefix}/username", "")
        s.setValue(f"{prefix}/password", "")
        s.setValue(f"{prefix}/referer", "")
        count += 1

    # ── General QGIS configuration ──────────────────────────────
    # All file dialogs default to /data/ (single workspace)
    s.setValue("UI/lastProjectDir", "/data")
    s.setValue("UI/lastVectorFileFilterDir", "/data")
    s.setValue("UI/lastRasterFileFilterDir", "/data")
    s.setValue("UI/lastFileNameWidgetDir", "/data")

    # Browser panel: hide filesystem root, set Home to /data
    s.setValue("browser/hiddenPaths", ["/", "/root"])
    s.setValue("browser/homePath", "/data")

    # Default CRS: use project CRS for new layers (auto-adapted on basemap add)
    s.setValue("Projections/defaultBehavior", "useProject")
    s.setValue("Projections/layerDefaultCrs", "EPSG:2154")

    # OTF reprojection always on
    s.setValue("Projections/otfTransformEnabled", True)

    # Disable dialogs
    s.setValue("qgis/showTips", False)
    s.setValue("qgis/checkVersion", False)
    s.setValue("qgis/nullValue", "NULL")
    s.setValue("qgis/askToSaveProjectChanges", False)
    s.setValue("qgis/askToDeleteLayers", False)

    # Flush to disk
    s.sync()

    print(f"[setup_connections] Configured {count} connections in {INI_PATH}")


if __name__ == "__main__":
    main()
