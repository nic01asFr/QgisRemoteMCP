"""
solar_pipeline.py — Configurable solar irradiance pipeline for QgisRemoteMCP.

Usage from execute_python:
    import solar_pipeline
    config = solar_pipeline.get_config("standard")  # or "rapide", "precision"
    config["resolution"] = 2  # override individual params
    solar_pipeline.build_dsm(config)
    solar_pipeline.run_rsun(config)
    solar_pipeline.aggregate_annual()
    solar_pipeline.apply_to_mnt()
    solar_pipeline.analyze_facades(config)
    solar_pipeline.compute_scores()

All outputs go to /data/solar/ (auto-created).
"""

import os
import math
import json
import time
import subprocess
import tempfile
from pathlib import Path

try:
    import numpy as np
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    ogr.UseExceptions()
except ImportError:
    pass  # Will fail at runtime with clear error

# ── Output directory ─────────────────────────────────────────────────
SOLAR_DIR = Path("/data/solar")
SOLAR_DIR.mkdir(exist_ok=True)

# ── Linke turbidity by month (Mediterranean climate) ────────────────
LINKE_MONTHLY = {
    1: 2.5, 2: 2.5, 3: 3.0, 4: 3.5, 5: 3.5, 6: 3.5,
    7: 3.5, 8: 3.5, 9: 3.0, 10: 3.0, 11: 2.5, 12: 2.5,
}

# Representative day of year per month (15th)
DOY_MONTHLY = {
    1: 15, 2: 46, 3: 74, 4: 105, 5: 135, 6: 166,
    7: 196, 8: 227, 9: 258, 10: 288, 11: 319, 12: 349,
}


# ══════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════

PROFILES = {
    "rapide": {
        "resolution": 10,          # DSM resolution (meters)
        "buffer_m": 100,            # Buffer around study zone (meters)
        "months": [1, 4, 7, 10],   # Months to compute
        "time_step": 1.0,           # r.sun step (hours)
        "ray_step": 3.0,            # Ray-marching step (meters)
        "max_shadow_dist": 200,     # Max shadow cast distance (meters)
        "facade_min_length": 5,     # Min facade segment length (meters)
        "nprocs": 2,                # GRASS parallel processes
        "albedo": 0.2,
    },
    "standard": {
        "resolution": 5,
        "buffer_m": 200,
        "months": list(range(1, 13)),
        "time_step": 0.5,
        "ray_step": 1.5,
        "max_shadow_dist": 300,
        "facade_min_length": 3,
        "nprocs": 2,
        "albedo": 0.2,
    },
    "precision": {
        "resolution": 1,
        "buffer_m": 300,
        "months": list(range(1, 13)),
        "time_step": 0.5,
        "ray_step": 1.0,
        "max_shadow_dist": 500,
        "facade_min_length": 2,
        "nprocs": 4,
        "albedo": 0.2,
    },
}


def get_config(profile="standard", **overrides):
    """Get a computation config dict. Override individual params via kwargs."""
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile '{profile}'. Choose from: {list(PROFILES.keys())}")
    config = dict(PROFILES[profile])
    config.update(overrides)
    # Auto-compute derived params
    res = config["resolution"]
    config["_n_steps"] = int(config["max_shadow_dist"] / config["ray_step"])
    config["_estimated_time"] = _estimate_time(config)
    return config


def _estimate_time(config):
    """Rough estimate of total compute time in minutes."""
    res = config["resolution"]
    n_months = len(config["months"])
    # r.sun: ~5s per month at 5m, scales as (5/res)^2
    rsun_min = n_months * 5 * (5 / res) ** 2 / 60
    # Ray-marching: ~1 min per 100k samples at 408 ts
    n_facades_est = 50000 * (5 / res)  # rough
    n_ts = n_months * int(17 / config["time_step"]) * 2
    ray_min = n_facades_est * n_ts / (100000 * 408) * 1.5
    return round(rsun_min + ray_min, 1)


# ══════════════════════════════════════════════════════════════════════
# STEP 1: BUILD DSM HYBRIDE
# ══════════════════════════════════════════════════════════════════════

def build_dsm(config):
    """Build hybrid DSM from RGE ALTI (MNT) + BD TOPO building heights.

    Requires:
        - /data/cache/ with RGE ALTI raster (from helpers.download_rge_alti)
        - BD TOPO batiments layer loaded in QGIS project

    Returns dict with paths to generated rasters.
    """
    t0 = time.time()
    res = config["resolution"]
    buffer_m = config["buffer_m"]

    # Find MNT raster
    mnt_path = _find_file("rge_alti", ["/data/solar/rge_alti.tif"] +
                          list(Path("/data/cache").glob("*alti*.tif")) +
                          list(Path("/data").glob("*rge_alti*.tif")))
    if not mnt_path:
        return {"error": "RGE ALTI not found. Run helpers.download_rge_alti(bbox) first."}

    # Find BD TOPO batiments GPKG
    bati_path = _find_file("batiments", ["/data/solar/batiments.gpkg"] +
                           list(Path("/data/cache").glob("*batiment*.gpkg")) +
                           list(Path("/data").glob("*batiments*.gpkg")))
    if not bati_path:
        return {"error": "BD TOPO batiments not found. Run smart_load('bdtopo_batiments') first."}

    # Get study zone bbox + buffer
    zone_file = Path("/data/solar/zone.json")
    if zone_file.exists():
        zone = json.load(open(zone_file))
    else:
        # Read from QGIS project variables
        try:
            from qgis.core import QgsProject
            prj = QgsProject.instance()
            bbox_str = prj.readEntry("study_zone", "bbox_2154", "")[0]
            if bbox_str:
                parts = [float(x) for x in bbox_str.split(",")]
                zone = {"bbox_2154": parts}
            else:
                return {"error": "No study zone set. Call set_study_zone() first."}
        except Exception:
            return {"error": "Cannot read study zone."}

    bbox = zone["bbox_2154"]
    xmin = bbox[0] - buffer_m
    ymin = bbox[1] - buffer_m
    xmax = bbox[2] + buffer_m
    ymax = bbox[3] + buffer_m

    # Resample MNT to target resolution
    mnt_res = str(SOLAR_DIR / f"mnt_{res}m.tif")
    _run_gdal(f"gdalwarp -overwrite -tr {res} {res} -r bilinear "
              f"-te {xmin} {ymin} {xmax} {ymax} "
              f"-t_srs EPSG:2154 {mnt_path} {mnt_res}")

    # Read MNT
    ds = gdal.Open(mnt_res)
    gt = ds.GetGeoTransform()
    mnt = ds.ReadAsArray().astype(np.float32)
    H, W = mnt.shape
    ds = None

    # Rasterize buildings onto MNT
    dsm = mnt.copy()
    src = ogr.Open(str(bati_path))
    layer = src.GetLayer(0)
    for feat in layer:
        h = feat.GetField("hauteur") or 0
        if h <= 0:
            n_et = feat.GetField("nombre_d_etages") or 1
            h = max(n_et * 3.0, 3.0)
        z_sol = feat.GetField("altitude_minimale_sol") or 0
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        geom = ogr.ForceToMultiPolygon(geom) if geom.GetGeometryName() in ("MULTISURFACE", "CURVEPOLYGON") else geom
        env = geom.GetEnvelope()  # xmin, xmax, ymin, ymax
        # Pixel range
        c0 = max(0, int((env[0] - gt[0]) / gt[1]))
        c1 = min(W, int((env[1] - gt[0]) / gt[1]) + 1)
        r0 = max(0, int((gt[3] - env[3]) / (-gt[5])))
        r1 = min(H, int((gt[3] - env[2]) / (-gt[5])) + 1)
        # Simple rasterization: fill bounding box pixels with max(mnt, z_sol + h)
        # (Proper rasterization via GDAL would be better for complex shapes)
        roof_z = (z_sol if z_sol > 0 else float(mnt[max(0, (r0+r1)//2), max(0, (c0+c1)//2)])) + h
        dsm[r0:r1, c0:c1] = np.maximum(dsm[r0:r1, c0:c1], roof_z)
    src = None

    # Write DSM
    dsm_path = str(SOLAR_DIR / f"dsm_hybride_{res}m.tif")
    _write_raster(dsm_path, dsm, gt, 2154)

    # Compute slope + aspect for r.sun
    slope_path = str(SOLAR_DIR / f"slope_{res}m.tif")
    aspect_path = str(SOLAR_DIR / f"aspect_{res}m.tif")
    _run_gdal(f"gdaldem slope {dsm_path} {slope_path} -compute_edges")
    _run_gdal(f"gdaldem aspect {dsm_path} {aspect_path} -compute_edges")

    elapsed = time.time() - t0
    return {
        "success": True,
        "dsm_path": dsm_path,
        "mnt_path": mnt_res,
        "slope_path": slope_path,
        "aspect_path": aspect_path,
        "size_px": f"{W}x{H}",
        "resolution_m": res,
        "bbox_l93": [xmin, ymin, xmax, ymax],
        "elapsed_s": round(elapsed, 1),
    }


# ══════════════════════════════════════════════════════════════════════
# STEP 2: RUN r.sun (GRASS GIS)
# ══════════════════════════════════════════════════════════════════════

def run_rsun(config):
    """Run GRASS r.sun for configured months. Returns dict with output paths."""
    t0 = time.time()
    res = config["resolution"]
    months = config["months"]
    step = config["time_step"]
    nprocs = config["nprocs"]
    albedo = config["albedo"]

    dsm_path = str(SOLAR_DIR / f"dsm_hybride_{res}m.tif")
    slope_path = str(SOLAR_DIR / f"slope_{res}m.tif")
    aspect_path = str(SOLAR_DIR / f"aspect_{res}m.tif")

    for p in [dsm_path, slope_path, aspect_path]:
        if not os.path.exists(p):
            return {"error": f"Missing: {p}. Run build_dsm() first."}

    # Build GRASS script
    lines = ["#!/bin/bash", "set -e"]
    lines.append(f"r.in.gdal input={dsm_path} output=dsm --overwrite")
    lines.append(f"r.in.gdal input={slope_path} output=slope --overwrite")
    lines.append(f"r.in.gdal input={aspect_path} output=aspect --overwrite")
    lines.append("g.region raster=dsm")

    outputs = {}
    for mo in months:
        day = DOY_MONTHLY[mo]
        linke = LINKE_MONTHLY[mo]
        gname = f"g{mo:02d}"
        iname = f"i{mo:02d}"
        glob_out = str(SOLAR_DIR / f"glob_rad_{mo:02d}.tif")
        insol_out = str(SOLAR_DIR / f"insol_time_{mo:02d}.tif")
        lines.append(
            f"r.sun elevation=dsm slope=slope aspect=aspect "
            f"day={day} linke_value={linke} albedo_value={albedo} "
            f"step={step} glob_rad={gname} insol_time={iname} "
            f"nprocs={nprocs} --overwrite"
        )
        lines.append(
            f'r.out.gdal input={gname} output={glob_out} format=GTiff '
            f'type=Float32 createopt="COMPRESS=LZW" --overwrite'
        )
        lines.append(
            f'r.out.gdal input={iname} output={insol_out} format=GTiff '
            f'type=Float32 createopt="COMPRESS=LZW" --overwrite'
        )
        outputs[f"glob_rad_{mo:02d}"] = glob_out
        outputs[f"insol_time_{mo:02d}"] = insol_out

    # Write and execute
    script_path = str(SOLAR_DIR / "run_rsun.sh")
    with open(script_path, "w", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(script_path, 0o755)

    result = subprocess.run(
        ["grass", "--tmp-project", "EPSG:2154", "--exec", "bash", script_path],
        capture_output=True, text=True, timeout=3600,
    )

    elapsed = time.time() - t0
    if result.returncode != 0:
        return {"error": f"r.sun failed: {result.stderr[-500:]}", "elapsed_s": round(elapsed, 1)}

    return {
        "success": True,
        "months_computed": months,
        "outputs": outputs,
        "elapsed_s": round(elapsed, 1),
    }


# ══════════════════════════════════════════════════════════════════════
# STEP 3: AGGREGATE ANNUAL
# ══════════════════════════════════════════════════════════════════════

def aggregate_annual():
    """Sum monthly irradiance → annual kWh/m²/an. Returns path."""
    t0 = time.time()
    files = sorted(SOLAR_DIR.glob("glob_rad_*.tif"))
    if not files:
        return {"error": "No glob_rad_*.tif found. Run run_rsun() first."}

    # Read first to get shape
    ds0 = gdal.Open(str(files[0]))
    gt = ds0.GetGeoTransform()
    total = np.zeros_like(ds0.ReadAsArray(), dtype=np.float64)
    ds0 = None

    # Weight: each month represents ~30 days, r.sun gives Wh/m²/day → kWh/m²/month = val * 30 / 1000
    for f in files:
        ds = gdal.Open(str(f))
        band = ds.ReadAsArray().astype(np.float64)
        total += band * 30.0 / 1000.0  # Wh/m²/day → kWh/m²/month
        ds = None

    # If only 4 months computed, extrapolate to 12
    n_months = len(files)
    if n_months < 12:
        total *= (12.0 / n_months)

    out_path = str(SOLAR_DIR / "irradiance_annuelle_kwh.tif")
    _write_raster(out_path, total.astype(np.float32), gt, 2154)

    # Also sum insol_time
    insol_files = sorted(SOLAR_DIR.glob("insol_time_*.tif"))
    if insol_files:
        insol_total = np.zeros_like(total, dtype=np.float64)
        for f in insol_files:
            ds = gdal.Open(str(f))
            insol_total += ds.ReadAsArray().astype(np.float64) * 30.0  # h/day → h/month
            ds = None
        if len(insol_files) < 12:
            insol_total *= (12.0 / len(insol_files))
        insol_path = str(SOLAR_DIR / "ensoleillement_annuel_h.tif")
        _write_raster(insol_path, insol_total.astype(np.float32), gt, 2154)

    elapsed = time.time() - t0
    return {
        "success": True,
        "path": out_path,
        "months_aggregated": n_months,
        "min_kwh": round(float(total[total > 0].min()), 1) if (total > 0).any() else 0,
        "max_kwh": round(float(total.max()), 1),
        "mean_kwh": round(float(total[total > 0].mean()), 1) if (total > 0).any() else 0,
        "elapsed_s": round(elapsed, 1),
    }


# ══════════════════════════════════════════════════════════════════════
# STEP 4: APPLY RASTER TO MNT IN QGIS
# ══════════════════════════════════════════════════════════════════════

def apply_to_mnt():
    """Load irradiance raster in QGIS and apply graduated color ramp over MNT.

    Creates a singleband pseudocolor style on the irradiance layer.
    """
    try:
        from qgis.core import (QgsProject, QgsRasterLayer, QgsStyle,
                               QgsColorRampShader, QgsRasterShader,
                               QgsSingleBandPseudoColorRenderer)
        from qgis.PyQt.QtGui import QColor
    except ImportError:
        return {"error": "Must run inside QGIS (via execute_python)"}

    irr_path = str(SOLAR_DIR / "irradiance_annuelle_kwh.tif")
    mnt_files = sorted(SOLAR_DIR.glob("mnt_*.tif"))

    if not os.path.exists(irr_path):
        return {"error": f"Missing {irr_path}. Run aggregate_annual() first."}

    project = QgsProject.instance()

    # Load MNT if not already loaded
    if mnt_files:
        mnt_name = "MNT"
        if not project.mapLayersByName(mnt_name):
            mnt_layer = QgsRasterLayer(str(mnt_files[0]), mnt_name)
            if mnt_layer.isValid():
                project.addMapLayer(mnt_layer)

    # Load irradiance raster
    layer_name = "Irradiance annuelle (kWh/m²/an)"
    existing = project.mapLayersByName(layer_name)
    if existing:
        project.removeMapLayer(existing[0].id())

    layer = QgsRasterLayer(irr_path, layer_name)
    if not layer.isValid():
        return {"error": f"Cannot load {irr_path}"}

    # Apply color ramp: blue → cyan → yellow → orange → red
    ramp_items = [
        QgsColorRampShader.ColorRampItem(0, QColor(30, 58, 95), "0"),
        QgsColorRampShader.ColorRampItem(200, QColor(30, 58, 95), "< 200"),
        QgsColorRampShader.ColorRampItem(400, QColor(74, 111, 165), "400"),
        QgsColorRampShader.ColorRampItem(600, QColor(136, 176, 212), "600"),
        QgsColorRampShader.ColorRampItem(800, QColor(253, 230, 138), "800"),
        QgsColorRampShader.ColorRampItem(1000, QColor(251, 146, 60), "1000"),
        QgsColorRampShader.ColorRampItem(1100, QColor(234, 88, 12), "1100"),
        QgsColorRampShader.ColorRampItem(1200, QColor(220, 38, 38), "> 1200"),
    ]

    shader = QgsRasterShader()
    color_ramp = QgsColorRampShader()
    color_ramp.setColorRampType(QgsColorRampShader.Interpolated)
    color_ramp.setColorRampItemList(ramp_items)
    shader.setRasterShaderFunction(color_ramp)

    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    layer.setRenderer(renderer)
    layer.setOpacity(0.7)  # Semi-transparent to show MNT relief underneath

    project.addMapLayer(layer)

    return {
        "success": True,
        "layer_name": layer_name,
        "opacity": 0.7,
        "note": "Irradiance layer added over MNT with graduated blue→red color ramp",
    }


# ══════════════════════════════════════════════════════════════════════
# STEP 5: FACADE ANALYSIS (RAY-MARCHING)
# ══════════════════════════════════════════════════════════════════════

def analyze_facades(config):
    """Ray-march from facade samples to detect solar visibility per timestep.

    Requires DSM and BD TOPO batiments. Outputs:
        /data/solar/facades.npz  — sample positions + irradiance
        /data/solar/bits.npy     — temporal bitsets
        /data/solar/ts_info.json — timestep solar positions
    """
    t0 = time.time()
    res = config["resolution"]
    ray_step = config["ray_step"]
    max_dist = config["max_shadow_dist"]
    n_steps = int(max_dist / ray_step)
    facade_min = config["facade_min_length"]
    months = config["months"]

    # Load DSM
    dsm_path = str(SOLAR_DIR / f"dsm_hybride_{res}m.tif")
    if not os.path.exists(dsm_path):
        return {"error": f"Missing {dsm_path}. Run build_dsm() first."}

    ds = gdal.Open(dsm_path)
    gt = ds.GetGeoTransform()
    DSM = ds.ReadAsArray().astype(np.float32)
    H, W = DSM.shape
    XMIN, YMAX, RES = gt[0], gt[3], gt[1]
    ds = None

    # Load irradiance for annual values
    irr_path = str(SOLAR_DIR / "irradiance_annuelle_kwh.tif")
    if os.path.exists(irr_path):
        ds = gdal.Open(irr_path)
        IRR = ds.ReadAsArray().astype(np.float32)
        IRR_GT = ds.GetGeoTransform()
        ds = None
    else:
        IRR = None

    # Load batiments
    bati_path = _find_file("batiments", list(Path("/data/cache").glob("*batiment*.gpkg")) +
                           list(Path("/data").glob("*batiments*.gpkg")))
    if not bati_path:
        return {"error": "BD TOPO batiments not found."}

    # Build facade samples
    print(f"[solar] Building facade samples (min_length={facade_min}m)...", flush=True)
    samples = []  # list of (cx, cy, cz, normal_az, bid, etage)
    src = ogr.Open(str(bati_path))
    layer = src.GetLayer(0)
    for feat in layer:
        h = feat.GetField("hauteur") or 0
        n_et = feat.GetField("nombre_d_etages") or 1
        if h <= 0:
            h = max(n_et * 3.0, 3.0)
        if n_et <= 0:
            n_et = max(1, int(round(h / 3.0)))
        et_h = h / n_et
        bid = feat.GetFID()
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        geom = ogr.ForceToMultiPolygon(geom)
        if geom is None or geom.GetGeometryCount() == 0:
            continue
        poly = max([geom.GetGeometryRef(k) for k in range(geom.GetGeometryCount())],
                   key=lambda p: p.GetArea() if p else 0)
        ring = poly.GetGeometryRef(0)
        if ring is None or ring.GetPointCount() < 4:
            continue
        centroid = poly.Centroid()
        z0_px_c = int((YMAX - centroid.GetY()) / RES)
        z0_px_r = int((centroid.GetX() - XMIN) / RES)
        z0 = float(DSM[max(0, min(H-1, z0_px_c)), max(0, min(W-1, z0_px_r))]) - h  # ground ≈ dsm - building height

        for i in range(ring.GetPointCount() - 1):
            ax, ay = ring.GetX(i), ring.GetY(i)
            bx, by = ring.GetX(i+1), ring.GetY(i+1)
            seg_len = math.hypot(bx - ax, by - ay)
            if seg_len < facade_min:
                continue
            dx, dy = bx - ax, by - ay
            nx, ny = dy / seg_len, -dx / seg_len  # outward normal (CCW)
            az = (math.degrees(math.atan2(nx, ny)) + 360) % 360
            mx, my = (ax + bx) / 2, (ay + by) / 2
            for et in range(n_et):
                cz = z0 + (et + 0.5) * et_h
                # Offset 1m along normal
                sx = mx + nx * 1.0
                sy = my + ny * 1.0
                samples.append((sx, sy, cz, az, bid, et))
    src = None

    N = len(samples)
    print(f"[solar] {N} facade samples generated", flush=True)
    if N == 0:
        return {"error": "No facade samples generated. Check batiments layer."}

    SX = np.array([s[0] for s in samples], dtype=np.float64)
    SY = np.array([s[1] for s in samples], dtype=np.float64)
    SZ = np.array([s[2] for s in samples], dtype=np.float32)
    SNORM = np.array([s[3] for s in samples], dtype=np.float32)
    SBID = np.array([s[4] for s in samples], dtype=np.int32)
    SETAGE = np.array([s[5] for s in samples], dtype=np.int32)

    # Sample irradiance at facade position
    SIRR = np.zeros(N, dtype=np.float32)
    if IRR is not None:
        for i in range(N):
            c = int((SX[i] - IRR_GT[0]) / IRR_GT[1])
            r = int((IRR_GT[3] - SY[i]) / (-IRR_GT[5]))
            if 0 <= c < IRR.shape[1] and 0 <= r < IRR.shape[0]:
                SIRR[i] = IRR[r, c]

    # Build timestep table
    LAT, LON = 43.306, 5.4  # TODO: read from study zone
    ts_info = []
    for mo in months:
        for h in range(5, 22):
            for m in [0, 30]:
                if config["time_step"] >= 1.0 and m == 30:
                    continue
                az, el = _sun_pos(2026, mo, 15, h, m, LAT, LON)
                ts_info.append([mo, h, m, round(az, 1), round(el, 1)])
    N_TS = len(ts_info)

    # Ray-marching
    print(f"[solar] Ray-marching {N} samples × {N_TS} timesteps × {n_steps} steps...", flush=True)
    N_BYTES = (N_TS + 7) // 8
    BITS = np.zeros((N, N_BYTES), dtype=np.uint8)

    SCOL = ((SX - XMIN) / RES).astype(np.float32)
    SROW = ((YMAX - SY) / RES).astype(np.float32)
    distances = (np.arange(n_steps, dtype=np.float32) + 0.5) * ray_step

    last_log = time.time()
    for ti in range(N_TS):
        mo, h, m, az, el = ts_info[ti]
        if el <= 0:
            continue
        az_r = math.radians(az)
        el_r = math.radians(el)
        sx_d = math.sin(az_r) * math.cos(el_r)
        sy_d = math.cos(az_r) * math.cos(el_r)
        sz_d = math.sin(el_r)
        dcol = sx_d / RES
        drow = -sy_d / RES

        sun_visible = np.ones(N, dtype=bool)
        for s in range(n_steps):
            d = distances[s]
            cols = (SCOL + dcol * d).astype(np.int32)
            rows = (SROW + drow * d).astype(np.int32)
            valid = (cols >= 0) & (cols < W) & (rows >= 0) & (rows < H)
            if not valid.any():
                break
            ray_z = SZ + sz_d * d
            check = sun_visible & valid
            if check.any():
                dsm_z = DSM[np.where(check, rows, 0), np.where(check, cols, 0)]
                sun_visible[check & (dsm_z > ray_z)] = False
            if not sun_visible.any():
                break

        byte_idx = ti // 8
        bit_idx = ti % 8
        BITS[sun_visible, byte_idx] |= (1 << bit_idx)

        if time.time() - last_log > 5:
            last_log = time.time()
            n_vis = sun_visible.sum()
            print(f"[solar]   ts {ti+1}/{N_TS} ({mo}/{h}h{m:02d} el={el:.0f}) — {n_vis}/{N} visible", flush=True)

    # Save
    np.savez(str(SOLAR_DIR / "facades.npz"),
             cx=SX, cy=SY, z=SZ, norm=SNORM, bid=SBID, etage=SETAGE, irr=SIRR)
    np.save(str(SOLAR_DIR / "bits.npy"), BITS)
    with open(str(SOLAR_DIR / "ts_info.json"), "w") as f:
        json.dump(ts_info, f)

    elapsed = time.time() - t0
    return {
        "success": True,
        "n_samples": N,
        "n_timesteps": N_TS,
        "bits_size_kb": BITS.nbytes // 1024,
        "elapsed_s": round(elapsed, 1),
    }


# ══════════════════════════════════════════════════════════════════════
# STEP 6: COMPUTE SCORES
# ══════════════════════════════════════════════════════════════════════

def compute_scores():
    """Compute annual scores (sun, hot, comfort) + z-score local.

    Outputs /data/solar/scores.npz
    """
    t0 = time.time()
    arr = np.load(str(SOLAR_DIR / "facades.npz"))
    SIRR = arr["irr"]
    N = len(SIRR)
    SX, SY = arr["cx"], arr["cy"]

    BITS = np.load(str(SOLAR_DIR / "bits.npy"))
    ts_info = json.load(open(str(SOLAR_DIR / "ts_info.json")))
    N_TS = len(ts_info)

    # Count sun hours per category
    total = np.zeros(N, dtype=np.int32)
    hiv = np.zeros(N, dtype=np.int32)
    ete = np.zeros(N, dtype=np.int32)
    ete_noon = np.zeros(N, dtype=np.int32)

    for ti in range(N_TS):
        bit = (BITS[:, ti // 8] >> (ti % 8)) & 1
        mo, h, m, az, el = ts_info[ti]
        total += bit
        if mo in (12, 1, 2):
            hiv += bit
        if mo in (6, 7, 8):
            ete += bit
            if 11 <= h < 15:
                ete_noon += bit

    # Normalize to hours/year (each timestep = 0.5h × 30 days/month)
    SH_YEAR = (total * 0.5 * 30).astype(np.int32)
    SH_HIV = (hiv * 0.5 * 30).astype(np.int32)
    SH_ETE = (ete * 0.5 * 30).astype(np.int32)
    SH_ETENOON = (ete_noon * 0.5 * 30).astype(np.int32)

    # Scores
    def pct(a):
        p5, p95 = np.percentile(a, 5), np.percentile(a, 95)
        if p95 - p5 < 1:
            return np.zeros_like(a, dtype=np.float32)
        return np.clip((a - p5) / (p95 - p5) * 100, 0, 100).astype(np.float32)

    SC_SUN = pct(SIRR.astype(np.float32))
    SC_HOT = pct(-SH_ETENOON.astype(np.float32))
    SC_COM = pct((SH_HIV * 1.5 - SH_ETENOON * 1.0).astype(np.float32))

    # Z-score local (50m convolution)
    from scipy.ndimage import uniform_filter
    # Build a mini raster of irradiance
    res_z = 5  # 5m for z-score computation
    x0, x1 = SX.min() - 50, SX.max() + 50
    y0, y1 = SY.min() - 50, SY.max() + 50
    zw = int((x1 - x0) / res_z) + 1
    zh = int((y1 - y0) / res_z) + 1
    grid_sum = np.zeros((zh, zw), dtype=np.float64)
    grid_cnt = np.zeros((zh, zw), dtype=np.int32)
    for i in range(N):
        c = int((SX[i] - x0) / res_z)
        r = int((y1 - SY[i]) / res_z)
        if 0 <= c < zw and 0 <= r < zh:
            grid_sum[r, c] += SIRR[i]
            grid_cnt[r, c] += 1
    grid_mean = np.where(grid_cnt > 0, grid_sum / grid_cnt, 0)
    # 50m window = 10 pixels at 5m
    local_mean = uniform_filter(grid_mean, size=10, mode='nearest')
    local_sq = uniform_filter(grid_mean ** 2, size=10, mode='nearest')
    local_std = np.sqrt(np.maximum(local_sq - local_mean ** 2, 0))
    local_std = np.maximum(local_std, 1.0)  # avoid div-by-0
    # Sample z-score per facade
    Z = np.zeros(N, dtype=np.float32)
    for i in range(N):
        c = int((SX[i] - x0) / res_z)
        r = int((y1 - SY[i]) / res_z)
        if 0 <= c < zw and 0 <= r < zh:
            Z[i] = (SIRR[i] - local_mean[r, c]) / local_std[r, c]

    np.savez(str(SOLAR_DIR / "scores.npz"),
             sh_year=SH_YEAR, sh_hiv=SH_HIV, sh_ete=SH_ETE, sh_etenoon=SH_ETENOON,
             sc_sun=SC_SUN, sc_hot=SC_HOT, sc_com=SC_COM, z_scores=Z)

    elapsed = time.time() - t0
    return {
        "success": True,
        "n_samples": N,
        "sh_year_mean": int(SH_YEAR.mean()),
        "sc_sun_mean": round(float(SC_SUN.mean()), 1),
        "z_score_max": round(float(Z.max()), 2),
        "n_remarkable": int((Z > 1.5).sum()),
        "elapsed_s": round(elapsed, 1),
    }


# ══════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════

def _find_file(keyword, candidates):
    """Find first existing file from candidates list."""
    for c in candidates:
        c = str(c)
        if os.path.exists(c):
            return c
    return None


def _run_gdal(cmd):
    """Run a GDAL command-line tool."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"GDAL failed: {result.stderr[:300]}")


def _write_raster(path, arr, geotransform, epsg):
    """Write a float32 raster to GeoTIFF."""
    driver = gdal.GetDriverByName("GTiff")
    H, W = arr.shape
    ds = driver.Create(path, W, H, 1, gdal.GDT_Float32, ["COMPRESS=LZW"])
    ds.SetGeoTransform(geotransform)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(arr)
    ds.FlushCache()
    ds = None


def _sun_pos(year, month, day, hour, minute, lat, lon):
    """Simple solar position (NOAA-like). Returns (azimuth_deg, elevation_deg)."""
    from datetime import datetime
    dt = datetime(year, month, day, hour, minute)
    n = dt.timetuple().tm_yday
    H_local = hour + minute / 60.0
    B = math.radians(360 / 365 * (n - 81))
    EoT = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (n - 81))))
    LSTM = 15 * 1  # UTC+1
    TC = 4 * (lon - LSTM) + EoT
    LST = H_local + TC / 60.0
    HRA = math.radians(15 * (LST - 12))
    lat_r = math.radians(lat)
    sin_el = math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(HRA)
    elev = math.degrees(math.asin(max(-1, min(1, sin_el))))
    cos_el = math.cos(math.radians(elev))
    if cos_el < 1e-6:
        return 180.0, elev
    cos_az = (math.sin(decl) - math.sin(math.radians(elev)) * math.sin(lat_r)) / (cos_el * math.cos(lat_r))
    cos_az = max(-1, min(1, cos_az))
    az = math.degrees(math.acos(cos_az))
    if HRA > 0:
        az = 360 - az
    return az, elev
