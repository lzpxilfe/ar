# -*- coding: utf-8 -*-

"""
비용표면/최소비용경로 (Cost Surface / LCP) dialog for ArchToolkit.

Notes
- No external processing providers (GRASS/SAGA/Whitebox).
- Uses GDAL + NumPy (shipped with QGIS) for least-cost computation.
"""

import heapq
import math
import os
import threading
import tempfile
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from osgeo import gdal, ogr, osr

import re

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPen
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsColorRampShader,
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsFillSymbol,
    QgsField,
    QgsGeometry,
    QgsLineSymbol,
    QgsMapLayerProxyModel,
    QgsMapLayer,
    QgsMarkerSymbol,
    QgsPointLocator,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsRendererCategory,
    QgsPalLayerSettings,
    QgsSingleBandPseudoColorRenderer,
    QgsSingleSymbolRenderer,
    QgsTask,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand, QgsSnapIndicator

from .utils import (
    cleanup_files,
    is_metric_crs,
    log_message,
    push_message,
    restore_ui_focus,
    set_archtoolkit_layer_metadata,
    transform_point,
)
from .live_log_dialog import ensure_live_log_dialog
from .help_dialog import show_help_dialog


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "cost_surface_dialog_base.ui")
)


MODEL_TOBLER = "tobler_time"
MODEL_NAISMITH = "naismith_time"
MODEL_HERZOG_METABOLIC = "herzog_metabolic_time"
MODEL_CONOLLY_LAKE = "conolly_lake_time"
MODEL_HERZOG_WHEELED = "herzog_wheeled_time"
MODEL_PANDOLF = "pandolf_energy"


@dataclass
class CostTaskResult:
    ok: bool
    message: str = ""
    cost_raster_path: Optional[str] = None
    cost_min: Optional[float] = None
    cost_max: Optional[float] = None
    energy_raster_path: Optional[str] = None
    energy_min: Optional[float] = None
    energy_max: Optional[float] = None
    path_coords: Optional[List[Tuple[float, float]]] = None  # in DEM CRS
    start_xy: Optional[Tuple[float, float]] = None  # in DEM CRS
    end_xy: Optional[Tuple[float, float]] = None  # in DEM CRS
    dem_authid: Optional[str] = None
    dem_source: Optional[str] = None
    model_key: Optional[str] = None
    model_params: Optional[dict] = None
    model_label: Optional[str] = None
    total_cost_s: Optional[float] = None
    total_energy_kcal: Optional[float] = None
    straight_time_s: Optional[float] = None
    straight_energy_kcal: Optional[float] = None
    straight_dist_m: Optional[float] = None
    lcp_dist_m: Optional[float] = None
    lcp_time_s: Optional[float] = None
    isochrones_vector_path: Optional[str] = None
    isoenergy_vector_path: Optional[str] = None
    corridor_raster_path: Optional[str] = None
    corridor_vector_path: Optional[str] = None
    corridor_percent: Optional[float] = None


def _inv_geotransform(gt):
    """
    Return inverse geotransform in a GDAL-version-safe way.

    Some GDAL builds return `(success, inv_gt)` while others return `inv_gt` directly.
    """
    inv = gdal.InvGeoTransform(gt)

    # Variant A: (success, inv_gt)
    if isinstance(inv, (list, tuple)) and len(inv) == 2:
        ok, inv_gt = inv
        if not ok:
            raise Exception("geotransform inverse failed")
        return inv_gt

    # Variant B: inv_gt (6-tuple)
    if isinstance(inv, (list, tuple)) and len(inv) == 6:
        return inv

    raise Exception("geotransform inverse failed")


def _clamp_int(v, lo, hi):
    return max(lo, min(hi, v))


def _cell_center(gt, col, row):
    x, y = gdal.ApplyGeoTransform(gt, col + 0.5, row + 0.5)
    return float(x), float(y)


def _window_geotransform(gt, xoff, yoff):
    return (
        gt[0] + xoff * gt[1] + yoff * gt[2],
        gt[1],
        gt[2],
        gt[3] + xoff * gt[4] + yoff * gt[5],
        gt[4],
        gt[5],
    )


def _split_qgis_source_path(source: str) -> str:
    """Best-effort: strip QGIS URI options (e.g., `|layername=...`) for GDAL/OGR."""
    s = (str(source or "")).strip()
    if not s:
        return ""
    return (s.split("|", 1)[0] or "").strip()


def _split_qgis_ogr_uri(source: str):
    """
    Parse common QGIS OGR-style source URIs like:
    - path.gpkg|layername=name
    - path.gpkg|layerid=0
    Returns (dataset_path, layer_name, layer_id).
    """
    s = (str(source or "")).strip()
    if not s:
        return "", None, None
    parts = s.split("|")
    dataset_path = (parts[0] or "").strip()
    layer_name = None
    layer_id = None
    for p in parts[1:]:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = (k or "").strip().lower()
        v = (v or "").strip()
        if k == "layername" and v:
            layer_name = v
        elif k == "layerid":
            try:
                layer_id = int(v)
            except Exception:
                layer_id = None
    return dataset_path, layer_name, layer_id


def _window_bounds(win_gt, cols: int, rows: int):
    """Return outputBounds=(minx, miny, maxx, maxy) for a window geotransform."""
    c = int(cols)
    r = int(rows)
    pts = [
        gdal.ApplyGeoTransform(win_gt, 0, 0),
        gdal.ApplyGeoTransform(win_gt, c, 0),
        gdal.ApplyGeoTransform(win_gt, 0, r),
        gdal.ApplyGeoTransform(win_gt, c, r),
    ]
    xs = [float(x) for x, _y in pts]
    ys = [float(y) for _x, y in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _resample_raster_to_window(raster_source: str, *, win_gt, win_proj_wkt: str, cols: int, rows: int):
    """Resample the raster to the given window grid (returns float32 array with NaN for nodata)."""
    path = _split_qgis_source_path(raster_source)
    if not path:
        return None
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        return None

    minx, miny, maxx, maxy = _window_bounds(win_gt, cols, rows)
    try:
        warp_opts = gdal.WarpOptions(
            format="MEM",
            outputBounds=(minx, miny, maxx, maxy),
            width=int(cols),
            height=int(rows),
            dstSRS=str(win_proj_wkt or ""),
            resampleAlg="near",
            multithread=False,
            outputType=gdal.GDT_Float32,
        )
        out_ds = gdal.Warp("", ds, options=warp_opts)
    except Exception:
        out_ds = None
    if out_ds is None:
        return None

    band = out_ds.GetRasterBand(1)
    arr = band.ReadAsArray()
    if arr is None:
        return None

    arr = arr.astype(np.float32, copy=False)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        try:
            arr[arr == float(nodata)] = np.nan
        except Exception:
            pass
    arr[~np.isfinite(arr)] = np.nan
    return arr


def _rasterize_vector_mask(vector_source: str, *, win_gt, win_proj_wkt: str, cols: int, rows: int):
    """Rasterize a vector layer into a boolean mask for the given window grid."""
    dataset_path, layer_name, layer_id = _split_qgis_ogr_uri(vector_source)
    if not dataset_path:
        return None
    vds = ogr.Open(dataset_path)
    if vds is None:
        return None

    layer = None
    if layer_name:
        try:
            layer = vds.GetLayerByName(str(layer_name))
        except Exception:
            layer = None
    if layer is None and layer_id is not None:
        try:
            layer = vds.GetLayer(int(layer_id))
        except Exception:
            layer = None
    if layer is None:
        try:
            layer = vds.GetLayer(0)
        except Exception:
            layer = None
    if layer is None:
        return None

    drv = gdal.GetDriverByName("MEM")
    if drv is None:
        return None
    out_ds = drv.Create("", int(cols), int(rows), 1, gdal.GDT_Byte)
    if out_ds is None:
        return None
    out_ds.SetGeoTransform(win_gt)
    out_ds.SetProjection(str(win_proj_wkt or ""))
    band = out_ds.GetRasterBand(1)
    band.Fill(0)
    band.SetNoDataValue(0)

    try:
        layer.ResetReading()
    except Exception:
        pass
    try:
        gdal.RasterizeLayer(out_ds, [1], layer, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    except Exception:
        return None

    mask = band.ReadAsArray()
    if mask is None:
        return None
    return (mask != 0)


def _bilinear_elevation(dem, nodata_mask, inv_gt, x, y):
    """Sample DEM elevation at x,y using bilinear interpolation (returns None if unavailable)."""
    rows, cols = dem.shape
    px, py = gdal.ApplyGeoTransform(inv_gt, float(x), float(y))

    # Convert GDAL pixel coords (top-left origin, center at +0.5) into array indices.
    col_f = float(px) - 0.5
    row_f = float(py) - 0.5

    if col_f < 0 or row_f < 0 or col_f > (cols - 1) or row_f > (rows - 1):
        return None

    x0 = int(math.floor(col_f))
    y0 = int(math.floor(row_f))
    x1 = min(x0 + 1, cols - 1)
    y1 = min(y0 + 1, rows - 1)
    dx = col_f - x0
    dy = row_f - y0

    # If any neighbor is nodata, fall back to nearest neighbor (more robust on edges/masks).
    if nodata_mask[y0, x0] or nodata_mask[y0, x1] or nodata_mask[y1, x0] or nodata_mask[y1, x1]:
        rn = int(round(row_f))
        cn = int(round(col_f))
        rn = _clamp_int(rn, 0, rows - 1)
        cn = _clamp_int(cn, 0, cols - 1)
        if nodata_mask[rn, cn]:
            return None
        return float(dem[rn, cn])

    v00 = float(dem[y0, x0])
    v01 = float(dem[y0, x1])
    v10 = float(dem[y1, x0])
    v11 = float(dem[y1, x1])

    v0 = (v00 * (1.0 - dx)) + (v01 * dx)
    v1 = (v10 * (1.0 - dx)) + (v11 * dx)
    return (v0 * (1.0 - dy)) + (v1 * dy)


def _estimate_straight_line_cost(
    model_key,
    model_params,
    start_xy,
    end_xy,
    dem,
    nodata_mask,
    win_gt,
    step_m,
    cost_mode="time_s",
):
    """Estimate cumulative cost along a straight line by DEM sampling."""
    sx, sy = start_xy
    ex, ey = end_xy
    straight_dist = math.hypot(ex - sx, ey - sy)
    if straight_dist <= 0:
        return 0.0, 0.0

    step_m = max(0.001, float(step_m))
    n_steps = max(1, int(math.ceil(straight_dist / step_m)))
    inv_win_gt = _inv_geotransform(win_gt)

    z_prev = _bilinear_elevation(dem, nodata_mask, inv_win_gt, sx, sy)
    if z_prev is None:
        return None, straight_dist

    total_cost = 0.0
    x_prev, y_prev = float(sx), float(sy)

    for i in range(1, n_steps + 1):
        t = float(i) / float(n_steps)
        x = (sx * (1.0 - t)) + (ex * t)
        y = (sy * (1.0 - t)) + (ey * t)
        z = _bilinear_elevation(dem, nodata_mask, inv_win_gt, x, y)
        if z is None:
            return None, straight_dist
        horiz = math.hypot(x - x_prev, y - y_prev)
        dz = float(z) - float(z_prev)
        total_cost += _edge_cost(model_key, horiz, dz, model_params, cost_mode=cost_mode)
        x_prev, y_prev, z_prev = float(x), float(y), float(z)

    return total_cost, straight_dist


def _polyline_length(coords):
    if not coords or len(coords) < 2:
        return 0.0
    total = 0.0
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        total += math.hypot(float(x1) - float(x0), float(y1) - float(y0))
    return total


def _default_isochrone_levels_minutes(max_minutes):
    """Generate isochrone levels (minutes) with coarse spacing as time increases.

    - Up to 60 min: every 15 min
    - 60~180 min: every 30 min
    - 180+ min: every 60 min
    """
    try:
        max_minutes = float(max_minutes)
    except Exception:
        return []
    if not math.isfinite(max_minutes) or max_minutes <= 0:
        return []

    levels = []

    for v in (15, 30, 45, 60):
        if v <= max_minutes + 1e-6:
            levels.append(float(v))

    v = 90
    while v <= 180 and v <= max_minutes + 1e-6:
        levels.append(float(v))
        v += 30

    v = 240
    # Safety cap to avoid producing an excessive number of contours on huge rasters.
    max_levels = 60
    while v <= max_minutes + 1e-6 and len(levels) < max_levels:
        levels.append(float(v))
        v += 60

    # Ensure sorted unique values
    uniq = []
    for t in sorted(set(levels)):
        if t > 0:
            uniq.append(t)
    return uniq


def _default_isoenergy_levels_kcal(max_kcal):
    """Generate iso-energy levels (kcal) with coarser spacing as energy increases."""
    try:
        max_kcal = float(max_kcal)
    except Exception:
        return []
    if not math.isfinite(max_kcal) or max_kcal <= 0:
        return []

    levels = []

    # Up to 600 kcal: 50-kcal steps
    step = 50.0
    v = step
    while v <= min(600.0, max_kcal + 1e-6):
        levels.append(float(v))
        v += step

    # 600~2000 kcal: 200-kcal steps
    v = 800.0
    while v <= min(2000.0, max_kcal + 1e-6):
        levels.append(float(v))
        v += 200.0

    # 2000+ kcal: 500-kcal steps (cap count)
    v = 2500.0
    max_levels = 80
    while v <= max_kcal + 1e-6 and len(levels) < max_levels:
        levels.append(float(v))
        v += 500.0

    uniq = []
    for t in sorted(set(levels)):
        if t > 0:
            uniq.append(t)
    return uniq


def _safe_layer_name_fragment(text):
    """Sanitize user-visible fragments used in layer/group names."""
    s = (text or "").strip()
    if not s:
        return ""
    # Prefer the part before the first '(' to avoid overly long mixed labels.
    s = s.split("(")[0].strip()
    s = re.sub(r"[\\\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\\s+", " ", s).strip()
    # Keep it short-ish for layer tree readability.
    return s[:40]


def _create_isochrones_gpkg(cost_raster_path, output_gpkg_path, levels_minutes, nodata_value=-9999.0):
    """Create an isochrone contour GeoPackage from the cost raster (values in minutes)."""
    return _create_fixed_contours_gpkg(
        cost_raster_path,
        output_gpkg_path,
        layer_name="isochrones",
        value_field_name="minutes",
        fixed_levels=levels_minutes,
        nodata_value=nodata_value,
    )


def _create_isoenergy_gpkg(energy_raster_path, output_gpkg_path, levels_kcal, nodata_value=-9999.0):
    """Create an iso-energy contour GeoPackage from the energy raster (values in kcal)."""
    return _create_fixed_contours_gpkg(
        energy_raster_path,
        output_gpkg_path,
        layer_name="isoenergy",
        value_field_name="kcal",
        fixed_levels=levels_kcal,
        nodata_value=nodata_value,
    )


def _create_corridor_gpkg(corridor_raster_path, output_gpkg_path):
    """Polygonize a corridor mask raster (1=corridor, 0=outside) into a GeoPackage."""
    if not corridor_raster_path or not os.path.exists(str(corridor_raster_path)):
        return None
    try:
        ds = gdal.Open(str(corridor_raster_path), gdal.GA_ReadOnly)
        if ds is None:
            return None
        band = ds.GetRasterBand(1)
        proj_wkt = ds.GetProjection() or ""

        drv = ogr.GetDriverByName("GPKG")
        if drv is None:
            return None
        if os.path.exists(output_gpkg_path):
            try:
                drv.DeleteDataSource(output_gpkg_path)
            except Exception:
                pass

        vds = drv.CreateDataSource(output_gpkg_path)
        if vds is None:
            return None

        srs = None
        if proj_wkt:
            try:
                srs = osr.SpatialReference()
                srs.ImportFromWkt(proj_wkt)
            except Exception:
                srs = None

        layer = vds.CreateLayer("corridor", srs, ogr.wkbPolygon)
        if layer is None:
            vds = None
            return None

        layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))

        # Use the corridor band itself as a mask so only non-zero pixels are polygonized.
        try:
            gdal.Polygonize(band, band, layer, 0, [], callback=None)
        except TypeError:
            gdal.Polygonize(band, band, layer, 0, [], None)

        try:
            vds.FlushCache()
        except Exception:
            pass
        vds = None
        ds = None
        return output_gpkg_path
    except Exception:
        try:
            if os.path.exists(output_gpkg_path):
                os.remove(output_gpkg_path)
        except Exception:
            pass
        return None


def _create_fixed_contours_gpkg(
    raster_path,
    output_gpkg_path,
    *,
    layer_name,
    value_field_name,
    fixed_levels,
    nodata_value=-9999.0,
):
    if not raster_path or not os.path.exists(raster_path):
        return None

    try:
        ds = gdal.Open(raster_path, gdal.GA_ReadOnly)
        if ds is None:
            return None
        band = ds.GetRasterBand(1)
        proj_wkt = ds.GetProjection() or ""

        drv = ogr.GetDriverByName("GPKG")
        if drv is None:
            return None
        if os.path.exists(output_gpkg_path):
            try:
                drv.DeleteDataSource(output_gpkg_path)
            except Exception:
                pass

        vds = drv.CreateDataSource(output_gpkg_path)
        if vds is None:
            return None

        srs = None
        if proj_wkt:
            try:
                srs = osr.SpatialReference()
                srs.ImportFromWkt(proj_wkt)
            except Exception:
                srs = None

        layer = vds.CreateLayer(str(layer_name), srs, ogr.wkbLineString)
        if layer is None:
            vds = None
            return None

        layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
        layer.CreateField(ogr.FieldDefn(str(value_field_name), ogr.OFTReal))

        levels = [float(v) for v in (fixed_levels or [])]
        if not levels:
            vds = None
            ds = None
            return None

        try:
            gdal.ContourGenerate(
                band,
                0.0,
                0.0,
                levels,
                1,
                float(nodata_value),
                layer,
                0,
                1,
            )
        except TypeError:
            gdal.ContourGenerate(
                band,
                0.0,
                0.0,
                len(levels),
                levels,
                1,
                float(nodata_value),
                layer,
                0,
                1,
            )

        try:
            vds.FlushCache()
        except Exception:
            pass
        vds = None
        ds = None
        return output_gpkg_path
    except Exception:
        try:
            if os.path.exists(output_gpkg_path):
                os.remove(output_gpkg_path)
        except Exception:
            pass
        return None


def _bbox_window(gt, xsize, ysize, minx, miny, maxx, maxy):
    inv = _inv_geotransform(gt)
    px0, py0 = gdal.ApplyGeoTransform(inv, minx, maxy)
    px1, py1 = gdal.ApplyGeoTransform(inv, maxx, miny)

    x0 = int(math.floor(min(px0, px1)))
    x1 = int(math.ceil(max(px0, px1)))
    y0 = int(math.floor(min(py0, py1)))
    y1 = int(math.ceil(max(py0, py1)))

    x0 = _clamp_int(x0, 0, xsize - 1)
    y0 = _clamp_int(y0, 0, ysize - 1)
    x1 = _clamp_int(x1, 0, xsize - 1)
    y1 = _clamp_int(y1, 0, ysize - 1)

    return x0, y0, max(1, x1 - x0 + 1), max(1, y1 - y0 + 1)


def _tobler_speed_mps(slope, base_speed_kmh, slope_factor, slope_offset, min_speed_mps):
    # Tobler (1993): W = a * exp(-b * abs(slope + c))  [km/h]
    speed_kmh = float(base_speed_kmh) * math.exp(
        -float(slope_factor) * abs(float(slope) + float(slope_offset))
    )
    return max(float(min_speed_mps), speed_kmh * 1000.0 / 3600.0)


def _naismith_time_s(horizontal_m, dz_m, horizontal_kmh, ascent_m_per_h):
    # Classic Naismith (1892): time = distance / speed + ascent / ascent_rate
    horizontal_kmh = max(0.0001, float(horizontal_kmh))
    ascent_m_per_h = max(0.0001, float(ascent_m_per_h))
    time_h = (float(horizontal_m) / (horizontal_kmh * 1000.0)) + (
        max(0.0, float(dz_m)) / ascent_m_per_h
    )
    return time_h * 3600.0


def _neighbors(allow_diagonal, dx, dy):
    moves = [(-1, 0, dy), (1, 0, dy), (0, -1, dx), (0, 1, dx)]
    if allow_diagonal:
        dxy = math.hypot(dx, dy)
        moves.extend([(-1, -1, dxy), (-1, 1, dxy), (1, -1, dxy), (1, 1, dxy)])
    return moves


def _edge_cost(model_key, horiz_m, dz_m, model_params, *, cost_mode="time_s"):
    if horiz_m <= 0:
        return 0.0

    if model_key == MODEL_TOBLER:
        slope = dz_m / horiz_m if horiz_m > 0 else 0.0
        return horiz_m / _tobler_speed_mps(
            slope,
            model_params.get("tobler_base_kmh", 6.0),
            model_params.get("tobler_slope_factor", 3.5),
            model_params.get("tobler_slope_offset", 0.05),
            model_params.get("tobler_min_speed_mps", 0.05),
        )
    if model_key == MODEL_NAISMITH:
        return _naismith_time_s(
            horiz_m,
            dz_m,
            model_params.get("naismith_horizontal_kmh", 5.0),
            model_params.get("naismith_ascent_m_per_h", 600.0),
        )

    if model_key == MODEL_PANDOLF:
        # Pandolf et al. (1977) load carriage equation (energy-based).
        #
        # M(W) = 1.5W + 2.0(W+L)(L/W)^2 + η(W+L)(1.5V^2 + 0.35VG)
        # where:
        #   W: body weight (kg)
        #   L: load weight (kg)
        #   V: speed (m/s)
        #   G: grade (%)  (signed)
        #   η: terrain factor (dimensionless)
        #
        # Edge energy (J) = M * (distance / V)
        # Edge time (s)   = distance / V
        W = max(1.0, float(model_params.get("pandolf_body_kg", 70.0)))
        L = max(0.0, float(model_params.get("pandolf_load_kg", 0.0)))
        eta = max(0.1, float(model_params.get("pandolf_terrain_factor", 1.0)))
        V = max(0.05, float(model_params.get("pandolf_speed_mps", 5.0 * 1000.0 / 3600.0)))

        if cost_mode == "time_s":
            return float(horiz_m) / V

        grade_percent = (float(dz_m) / float(horiz_m)) * 100.0
        load_ratio = (L / W) if W > 0 else 0.0
        M = (1.5 * W) + (2.0 * (W + L) * (load_ratio**2)) + (
            eta * (W + L) * (1.5 * V * V + 0.35 * V * grade_percent)
        )
        # Ensure strictly positive to keep the path solver stable.
        M = max(1.0, float(M))
        return (float(M) * float(horiz_m)) / V

    # Isotropic slope-based models (use absolute slope magnitude)
    slope_abs = abs(float(dz_m)) / float(horiz_m) if horiz_m > 0 else 0.0  # tan(theta)
    min_speed_mps = float(model_params.get("min_speed_mps", 0.05))

    if model_key == MODEL_HERZOG_METABOLIC:
        # Based on the slope_cost implementation in Zoran Čučković's "Movement Analysis" QGIS plugin.
        # We normalize the factor so that slope=0 keeps the base speed.
        den = (
            1337.8 * slope_abs**6
            + 278.19 * slope_abs**5
            - 517.39 * slope_abs**4
            - 78.199 * slope_abs**3
            + 93.419 * slope_abs**2
            + 19.825 * slope_abs
            + 1.64
        )
        rel = 1.0 / max(1e-9, float(den))
        rel0 = 1.0 / 1.64
        rel_norm = rel / rel0
        base_mps = max(min_speed_mps, float(model_params.get("herzog_base_kmh", 5.0)) * 1000.0 / 3600.0)
        speed_mps = max(min_speed_mps, base_mps * rel_norm)
        return float(horiz_m) / speed_mps

    if model_key == MODEL_CONOLLY_LAKE:
        # Conolly & Lake: relative slope penalty anchored at a reference slope.
        # We clamp the factor to >=1 so gentle slopes do not become "faster than flat".
        ref_deg = max(0.1, float(model_params.get("conolly_ref_slope_deg", 1.0)))
        ref_tan = math.tan(math.radians(ref_deg))
        factor = max(1.0, slope_abs / max(1e-9, ref_tan))
        base_mps = max(min_speed_mps, float(model_params.get("conolly_base_kmh", 5.0)) * 1000.0 / 3600.0)
        return (float(horiz_m) / base_mps) * factor

    if model_key == MODEL_HERZOG_WHEELED:
        # Optional "hard" slope limit for wheeled traffic (beyond this, effectively impassable).
        max_deg = float(model_params.get("wheeled_max_slope_deg", 45.0))
        max_deg = max(1.0, min(89.0, max_deg))
        slope_deg = math.degrees(math.atan(slope_abs))
        if slope_deg > max_deg + 1e-9:
            # Treat as unreachable instead of producing extreme finite costs (keeps raster ranges readable).
            return math.inf

        critical_deg = max(1.0, float(model_params.get("wheeled_critical_slope_deg", 12.0)))
        critical_percent = math.tan(math.radians(critical_deg)) * 100.0
        slope_percent = slope_abs * 100.0
        speed_factor = 1.0 / (1.0 + (slope_percent / max(1e-9, critical_percent)) ** 2)
        base_mps = max(min_speed_mps, float(model_params.get("wheeled_base_kmh", 4.0)) * 1000.0 / 3600.0)
        speed_mps = max(min_speed_mps, base_mps * speed_factor)
        return float(horiz_m) / speed_mps

    # Fallback
    return _naismith_time_s(
        horiz_m,
        dz_m,
        model_params.get("naismith_horizontal_kmh", 5.0),
        model_params.get("naismith_ascent_m_per_h", 600.0),
    )


def _astar_path(
    dem,
    nodata_mask,
    start_rc,
    end_rc,
    dx,
    dy,
    allow_diagonal,
    model_key,
    model_params,
    cost_mode="time_s",
    cancel_check=None,
    friction=None,
    friction_min=1.0,
):
    rows, cols = dem.shape
    sr, sc = start_rc
    er, ec = end_rc
    start_idx = sr * cols + sc
    end_idx = er * cols + ec

    gscore = np.full(rows * cols, np.inf, dtype=np.float64)
    prev = np.full(rows * cols, -1, dtype=np.int32)
    gscore[start_idx] = 0.0

    if cost_mode != "time_s":
        # Keep heuristic admissible (0) for non-time costs (e.g. energy).
        def hfun(_r, _c):
            return 0.0
    else:
        if model_key == MODEL_TOBLER:
            vmax = float(model_params.get("tobler_base_kmh", 6.0)) * 1000.0 / 3600.0
        elif model_key == MODEL_NAISMITH:
            vmax = float(model_params.get("naismith_horizontal_kmh", 5.0)) * 1000.0 / 3600.0
        elif model_key == MODEL_HERZOG_METABOLIC:
            vmax = float(model_params.get("herzog_base_kmh", 5.0)) * 1000.0 / 3600.0
        elif model_key == MODEL_CONOLLY_LAKE:
            vmax = float(model_params.get("conolly_base_kmh", 5.0)) * 1000.0 / 3600.0
        elif model_key == MODEL_HERZOG_WHEELED:
            vmax = float(model_params.get("wheeled_base_kmh", 4.0)) * 1000.0 / 3600.0
        elif model_key == MODEL_PANDOLF:
            vmax = float(model_params.get("pandolf_speed_mps", 5.0 * 1000.0 / 3600.0))
        else:
            vmax = float(model_params.get("naismith_horizontal_kmh", 5.0)) * 1000.0 / 3600.0
        vmax = max(0.05, vmax)

        fmin = 1.0
        try:
            fmin = max(0.0, float(friction_min))
        except Exception:
            fmin = 1.0

        def hfun(r, c):
            return (math.hypot((ec - c) * dx, (er - r) * dy) / vmax) * fmin

    heap = [(hfun(sr, sc), 0.0, start_idx)]
    moves = _neighbors(allow_diagonal, dx, dy)

    while heap:
        if cancel_check and cancel_check():
            return None, None
        f, g, idx = heapq.heappop(heap)
        if g != gscore[idx]:
            continue
        if idx == end_idx:
            return prev, float(g)

        r = idx // cols
        c = idx % cols
        if nodata_mask[r, c]:
            continue

        z0 = float(dem[r, c])
        for dr, dc, horiz in moves:
            nr = r + dr
            nc = c + dc
            if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                continue
            if nodata_mask[nr, nc]:
                continue
            dz = float(dem[nr, nc]) - z0
            w = _edge_cost(model_key, horiz, dz, model_params, cost_mode=cost_mode)
            if friction is not None and math.isfinite(w):
                try:
                    f0 = float(friction[r, c])
                    f1 = float(friction[nr, nc])
                    w *= 0.5 * (f0 + f1)
                except Exception:
                    pass

            nidx = nr * cols + nc
            ng = g + w
            if ng < gscore[nidx]:
                gscore[nidx] = ng
                prev[nidx] = idx
                heapq.heappush(heap, (ng + hfun(nr, nc), ng, nidx))

    return prev, None


def _dijkstra_full(
    dem,
    nodata_mask,
    start_rc,
    dx,
    dy,
    allow_diagonal,
    model_key,
    model_params,
    cost_mode="time_s",
    cancel_check=None,
    progress_cb=None,
    friction=None,
):
    rows, cols = dem.shape
    sr, sc = start_rc
    start_idx = sr * cols + sc

    dist = np.full(rows * cols, np.inf, dtype=np.float64)
    prev = np.full(rows * cols, -1, dtype=np.int32)
    dist[start_idx] = 0.0

    heap = [(0.0, start_idx)]
    moves = _neighbors(allow_diagonal, dx, dy)
    total = rows * cols
    popped = 0

    while heap:
        if cancel_check and cancel_check():
            return None, None
        d, idx = heapq.heappop(heap)
        if d != dist[idx]:
            continue

        popped += 1
        if progress_cb and popped % 5000 == 0:
            progress_cb(min(99.0, 100.0 * popped / max(1, total)))

        r = idx // cols
        c = idx % cols
        if nodata_mask[r, c]:
            continue

        z0 = float(dem[r, c])
        for dr, dc, horiz in moves:
            nr = r + dr
            nc = c + dc
            if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                continue
            if nodata_mask[nr, nc]:
                continue

            dz = float(dem[nr, nc]) - z0
            w = _edge_cost(model_key, horiz, dz, model_params, cost_mode=cost_mode)
            if friction is not None and math.isfinite(w):
                try:
                    f0 = float(friction[r, c])
                    f1 = float(friction[nr, nc])
                    w *= 0.5 * (f0 + f1)
                except Exception:
                    pass
            nidx = nr * cols + nc
            nd = d + w
            if nd < dist[nidx]:
                dist[nidx] = nd
                prev[nidx] = idx
                heapq.heappush(heap, (nd, nidx))

    if progress_cb:
        progress_cb(100.0)
    return dist, prev


def _reconstruct_path(prev, start_rc, end_rc, cols, rows):
    start_idx = start_rc[0] * cols + start_rc[1]
    end_idx = end_rc[0] * cols + end_rc[1]
    if start_idx == end_idx:
        return [start_idx]
    if prev[end_idx] == -1:
        return []

    path = []
    cur = end_idx
    max_steps = rows * cols + 1
    steps = 0
    while cur != -1 and steps < max_steps:
        path.append(cur)
        if cur == start_idx:
            break
        cur = int(prev[cur])
        steps += 1
    if not path or path[-1] != start_idx:
        return []
    path.reverse()
    return path


class CostSurfaceWorker(QgsTask):
    def __init__(
        self,
        *,
        dem_source,
        dem_authid,
        start_xy,
        end_xy,
        buffer_m,
        allow_diagonal,
        model_key,
        model_params,
        model_label,
        create_cost_raster,
        create_energy_raster,
        create_path,
        create_corridor=False,
        corridor_percent=5.0,
        corridor_polygonize=True,
        friction_raster_source=None,
        friction_raster_scale=1.0,
        friction_vector_source=None,
        friction_vector_multiplier=1.0,
        on_done,
    ):
        super().__init__("비용표면/최소비용경로 (Cost Surface / LCP)", QgsTask.CanCancel)
        self._cancel_event = threading.Event()
        self.dem_source = dem_source
        self.dem_authid = dem_authid
        self.start_xy = start_xy
        self.end_xy = end_xy
        self.buffer_m = float(buffer_m)
        self.allow_diagonal = bool(allow_diagonal)
        self.model_key = model_key
        self.model_params = dict(model_params or {})
        self.model_label = model_label
        self.create_cost_raster = bool(create_cost_raster)
        self.create_energy_raster = bool(create_energy_raster)
        self.create_path = bool(create_path)
        self.create_corridor = bool(create_corridor)
        self.corridor_percent = float(corridor_percent) if corridor_percent is not None else 0.0
        self.corridor_polygonize = bool(corridor_polygonize)
        self.friction_raster_source = friction_raster_source
        self.friction_raster_scale = float(friction_raster_scale) if friction_raster_scale is not None else 1.0
        self.friction_vector_source = friction_vector_source
        self.friction_vector_multiplier = (
            float(friction_vector_multiplier) if friction_vector_multiplier is not None else 1.0
        )
        self.on_done = on_done
        self.result_obj = CostTaskResult(ok=False)

    def cancel(self):
        # Avoid calling QgsTask.isCanceled() from worker thread (can be unstable on some setups).
        try:
            self._cancel_event.set()
        except Exception:
            pass
        try:
            return super().cancel()
        except Exception:
            return True

    def _is_cancelled(self) -> bool:
        try:
            return bool(self._cancel_event.is_set())
        except Exception:
            return False

    def run(self):
        try:
            self.result_obj = self._run_impl()
            return bool(self.result_obj.ok)
        except Exception as e:
            self.result_obj = CostTaskResult(ok=False, message=str(e))
            return False

    def finished(self, result):
        try:
            if self.on_done:
                self.on_done(self.result_obj)
        except Exception as e:
            log_message(f"Cost task finished callback error: {e}", level=Qgis.Warning)

    def _run_impl(self):
        log_message(
            f"CostSurface: start (model={self.model_label or self.model_key}, diagonal={self.allow_diagonal}, buffer_m={self.buffer_m})",
            level=Qgis.Info,
        )
        ds = gdal.Open(self.dem_source, gdal.GA_ReadOnly)
        if ds is None:
            return CostTaskResult(ok=False, message="DEM을 GDAL로 열 수 없습니다.")

        band = ds.GetRasterBand(1)
        xsize = ds.RasterXSize
        ysize = ds.RasterYSize
        gt = ds.GetGeoTransform()
        proj = ds.GetProjection()
        nodata = band.GetNoDataValue()

        dx = abs(float(gt[1]))
        dy = abs(float(gt[5]))
        if dx <= 0 or dy <= 0:
            return CostTaskResult(ok=False, message="DEM 픽셀 크기를 확인할 수 없습니다.")

        sx, sy = self.start_xy
        has_end = self.end_xy is not None
        if has_end:
            ex, ey = self.end_xy
        else:
            ex, ey = sx, sy

        # Analysis extent
        # - buffer_m == 0 : full DEM
        # - buffer_m > 0  : window around start/end (faster)
        if self.buffer_m <= 0:
            xoff, yoff, win_xsize, win_ysize = 0, 0, xsize, ysize
        else:
            if has_end:
                minx = min(sx, ex) - self.buffer_m
                maxx = max(sx, ex) + self.buffer_m
                miny = min(sy, ey) - self.buffer_m
                maxy = max(sy, ey) + self.buffer_m
            else:
                minx = sx - self.buffer_m
                maxx = sx + self.buffer_m
                miny = sy - self.buffer_m
                maxy = sy + self.buffer_m

            xoff, yoff, win_xsize, win_ysize = _bbox_window(
                gt, xsize, ysize, minx, miny, maxx, maxy
            )
        cell_count = int(win_xsize * win_ysize)
        if cell_count > 4_000_000:
            return CostTaskResult(
                ok=False,
                message=(
                    f"분석 영역이 너무 큽니다(약 {cell_count:,} cells). "
                    "분석 제한(m)을 0보다 크게 설정해 영역을 줄이거나 DEM을 클립하세요."
                ),
            )

        log_message(
            f"CostSurface: DEM window {win_xsize}x{win_ysize} ({cell_count:,} cells)",
            level=Qgis.Info,
        )
        dem = band.ReadAsArray(xoff, yoff, win_xsize, win_ysize)
        if dem is None:
            return CostTaskResult(ok=False, message="DEM 값을 읽을 수 없습니다.")
        dem = dem.astype(np.float32, copy=False)

        nodata_mask = np.zeros(dem.shape, dtype=bool)
        if nodata is not None:
            nodata_mask |= dem == nodata
        nodata_mask |= np.isnan(dem)

        inv = _inv_geotransform(gt)
        s_px, s_py = gdal.ApplyGeoTransform(inv, sx, sy)
        e_px, e_py = gdal.ApplyGeoTransform(inv, ex, ey)
        s_col = int(math.floor(s_px)) - xoff
        s_row = int(math.floor(s_py)) - yoff
        e_col = int(math.floor(e_px)) - xoff
        e_row = int(math.floor(e_py)) - yoff

        rows, cols = dem.shape
        if not (0 <= s_row < rows and 0 <= s_col < cols and 0 <= e_row < rows and 0 <= e_col < cols):
            return CostTaskResult(ok=False, message="시작/도착점이 DEM 분석 범위를 벗어났습니다.")
        if nodata_mask[s_row, s_col] or nodata_mask[e_row, e_col]:
            return CostTaskResult(
                ok=False,
                message="시작점이 NoData 영역에 있습니다." if not has_end else "시작/도착점이 NoData 영역에 있습니다.",
            )

        start_rc = (s_row, s_col)
        end_rc = (e_row, e_col) if has_end else None

        def cancel_check():
            return self._is_cancelled()

        def progress_cb(p):
            try:
                self.setProgress(float(p))
            except Exception:
                pass

        def make_progress_cb(stage: str, *, offset: float, scale: float):
            last_bucket = -1

            def _cb(p):
                nonlocal last_bucket
                overall = float(offset) + float(p) * float(scale)
                progress_cb(overall)
                bucket = int(overall // 10.0)
                if bucket != last_bucket:
                    last_bucket = bucket
                    log_message(f"CostSurface: {stage}… {bucket * 10}%", level=Qgis.Info)

            return _cb

        is_energy_model = self.model_key == MODEL_PANDOLF
        path_cost_mode = "energy_j" if is_energy_model else "time_s"

        create_time_raster = bool(self.create_cost_raster)
        create_energy_raster = bool(self.create_energy_raster and is_energy_model)
        create_path = bool(self.create_path and has_end)

        create_corridor = bool(self.create_corridor and has_end)
        try:
            corridor_percent = float(self.corridor_percent)
        except Exception:
            corridor_percent = 0.0
        corridor_percent = max(0.0, min(100.0, corridor_percent))
        corridor_polygonize = bool(self.corridor_polygonize)

        win_gt = _window_geotransform(gt, xoff, yoff)
        run_id = uuid.uuid4().hex[:8]

        # Optional friction multiplier grid (penalty factors). 1.0 = no effect.
        friction = None
        friction_min = 1.0
        if self.friction_raster_source or self.friction_vector_source:
            friction = np.ones(dem.shape, dtype=np.float32)

            if self.friction_raster_source:
                fr = _resample_raster_to_window(
                    self.friction_raster_source,
                    win_gt=win_gt,
                    win_proj_wkt=proj,
                    cols=cols,
                    rows=rows,
                )
                if fr is None:
                    return CostTaskResult(ok=False, message="추가 마찰(래스터)을 읽을 수 없습니다.")
                try:
                    scale = float(self.friction_raster_scale)
                except Exception:
                    scale = 1.0
                if not math.isfinite(scale) or scale <= 0:
                    scale = 1.0

                mult = fr.astype(np.float32, copy=False)
                mult[~np.isfinite(mult)] = 1.0
                mult = mult * float(scale)
                mult = np.clip(mult, 0.0001, 1.0e6).astype(np.float32, copy=False)
                friction *= mult

            if self.friction_vector_source:
                mask = _rasterize_vector_mask(
                    self.friction_vector_source,
                    win_gt=win_gt,
                    win_proj_wkt=proj,
                    cols=cols,
                    rows=rows,
                )
                if mask is None:
                    return CostTaskResult(ok=False, message="추가 마찰(벡터)을 래스터화할 수 없습니다.")
                try:
                    mult = float(self.friction_vector_multiplier)
                except Exception:
                    mult = 1.0
                if not math.isfinite(mult) or mult <= 0:
                    mult = 1.0
                try:
                    friction[mask] *= float(mult)
                except Exception:
                    pass

            try:
                friction[nodata_mask] = 1.0
            except Exception:
                pass

            try:
                finite = np.isfinite(friction)
                if np.any(finite):
                    friction_min = float(np.nanmin(friction[finite]))
                else:
                    friction_min = 1.0
            except Exception:
                friction_min = 1.0
            if not math.isfinite(friction_min) or friction_min <= 0:
                friction_min = 0.0001

        dist_time = None
        prev_time = None
        end_time_s = None

        dist_energy = None
        prev_energy = None
        end_energy_j = None

        dist_end_for_corridor = None

        need_time_surface = bool(create_time_raster or (create_corridor and path_cost_mode == "time_s"))
        need_energy_surface = bool(create_energy_raster or (create_corridor and path_cost_mode == "energy_j"))

        stages = []
        if need_time_surface:
            stages.append(("time surface", "start", "time_s"))
        if need_energy_surface:
            stages.append(("energy surface", "start", "energy_j"))
        if create_corridor:
            stages.append(("corridor surface (from end)", "end", path_cost_mode))

        stage_share = 100.0 / float(len(stages) or 1)

        for i, (stage_label, which, mode) in enumerate(stages):
            cb = make_progress_cb(stage_label, offset=float(i) * stage_share, scale=stage_share / 100.0)
            if which == "start":
                dist, prev = _dijkstra_full(
                    dem,
                    nodata_mask,
                    start_rc,
                    dx,
                    dy,
                    self.allow_diagonal,
                    self.model_key,
                    self.model_params,
                    cost_mode=mode,
                    cancel_check=cancel_check,
                    progress_cb=cb,
                    friction=friction,
                )
                if dist is None or prev is None:
                    return CostTaskResult(ok=False, message="작업이 취소되었습니다.")
                if mode == "time_s":
                    dist_time, prev_time = dist, prev
                    if has_end:
                        end_time_s = float(dist_time[end_rc[0] * cols + end_rc[1]])
                else:
                    dist_energy, prev_energy = dist, prev
                    if has_end:
                        end_energy_j = float(dist_energy[end_rc[0] * cols + end_rc[1]])
            else:
                dist, prev = _dijkstra_full(
                    dem,
                    nodata_mask,
                    end_rc,
                    dx,
                    dy,
                    self.allow_diagonal,
                    self.model_key,
                    self.model_params,
                    cost_mode=mode,
                    cancel_check=cancel_check,
                    progress_cb=cb,
                    friction=friction,
                )
                if dist is None or prev is None:
                    return CostTaskResult(ok=False, message="작업이 취소되었습니다.")
                dist_end_for_corridor = dist

        # Path: optimize by the model's primary cost (energy for Pandolf, time otherwise).
        prev_for_path = None
        end_cost_for_path = None
        if create_path:
            log_message("CostSurface: computing least-cost path (A*)…", level=Qgis.Info)
            if path_cost_mode == "energy_j":
                if prev_energy is None:
                    prev_energy, end_energy_j = _astar_path(
                        dem,
                        nodata_mask,
                        start_rc,
                        end_rc,
                        dx,
                        dy,
                        self.allow_diagonal,
                        self.model_key,
                        self.model_params,
                        cost_mode="energy_j",
                        cancel_check=cancel_check,
                        friction=friction,
                        friction_min=friction_min,
                    )
                prev_for_path = prev_energy
                end_cost_for_path = end_energy_j
            else:
                if prev_time is None:
                    prev_time, end_time_s = _astar_path(
                        dem,
                        nodata_mask,
                        start_rc,
                        end_rc,
                        dx,
                        dy,
                        self.allow_diagonal,
                        self.model_key,
                        self.model_params,
                        cost_mode="time_s",
                        cancel_check=cancel_check,
                        friction=friction,
                        friction_min=friction_min,
                    )
                prev_for_path = prev_time
                end_cost_for_path = end_time_s

            if prev_for_path is None:
                return CostTaskResult(ok=False, message="작업이 취소되었습니다.")

        if not stages:
            progress_cb(100.0)

        cost_raster_path = None
        cost_min = None
        cost_max = None
        isochrones_vector_path = None
        isoenergy_vector_path = None
        corridor_raster_path = None
        corridor_vector_path = None
        log_message("CostSurface: writing outputs…", level=Qgis.Info)
        if create_time_raster and dist_time is not None:
            dist2d_s = dist_time.reshape((rows, cols))
            valid = np.isfinite(dist2d_s) & (~nodata_mask)
            if np.any(valid):
                dist2d_min = dist2d_s[valid] / 60.0
                cost_min = float(np.nanmin(dist2d_min))
                cost_max = float(np.nanmax(dist2d_min))

            out = np.full(dist2d_s.shape, -9999.0, dtype=np.float32)
            out[valid] = (dist2d_s[valid] / 60.0).astype(np.float32, copy=False)

            cost_raster_path = os.path.join(
                tempfile.gettempdir(), f"archt_cost_{self.model_key}_{run_id}.tif"
            )
            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(
                cost_raster_path,
                cols,
                rows,
                1,
                gdal.GDT_Float32,
                options=["TILED=YES", "COMPRESS=LZW"],
            )
            if out_ds is None:
                return CostTaskResult(ok=False, message="누적 비용 래스터를 생성할 수 없습니다.")
            out_ds.SetGeoTransform(win_gt)
            out_ds.SetProjection(proj)
            out_band = out_ds.GetRasterBand(1)
            out_band.SetNoDataValue(-9999.0)
            out_band.WriteArray(out)
            out_band.FlushCache()
            out_ds.FlushCache()
            out_ds = None

            # Isochrones (0/15/30/45/60/...) for easier interpretation.
            try:
                if cost_max is not None and math.isfinite(cost_max):
                    usable = _default_isochrone_levels_minutes(cost_max)
                    if usable:
                        iso_path = os.path.join(
                            tempfile.gettempdir(),
                            f"archt_iso_{self.model_key}_{run_id}.gpkg",
                        )
                        isochrones_vector_path = _create_isochrones_gpkg(
                            cost_raster_path,
                            iso_path,
                            usable,
                            nodata_value=-9999.0,
                        )
            except Exception:
                isochrones_vector_path = None

        energy_raster_path = None
        energy_min = None
        energy_max = None
        if create_energy_raster and dist_energy is not None:
            dist2d_j = dist_energy.reshape((rows, cols))
            valid = np.isfinite(dist2d_j) & (~nodata_mask)
            if np.any(valid):
                dist2d_kcal = dist2d_j[valid] / 4184.0
                energy_min = float(np.nanmin(dist2d_kcal))
                energy_max = float(np.nanmax(dist2d_kcal))

            out = np.full(dist2d_j.shape, -9999.0, dtype=np.float32)
            out[valid] = (dist2d_j[valid] / 4184.0).astype(np.float32, copy=False)

            energy_raster_path = os.path.join(
                tempfile.gettempdir(), f"archt_energy_{self.model_key}_{run_id}.tif"
            )
            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(
                energy_raster_path,
                cols,
                rows,
                1,
                gdal.GDT_Float32,
                options=["TILED=YES", "COMPRESS=LZW"],
            )
            if out_ds is None:
                return CostTaskResult(ok=False, message="누적 에너지 래스터를 생성할 수 없습니다.")
            out_ds.SetGeoTransform(win_gt)
            out_ds.SetProjection(proj)
            out_band = out_ds.GetRasterBand(1)
            out_band.SetNoDataValue(-9999.0)
            out_band.WriteArray(out)
            out_band.FlushCache()
            out_ds.FlushCache()
            out_ds = None

            # Iso-energy contours (kcal) for interpretation (same polygonal artifacts as grid-based movement).
            try:
                if energy_max is not None and math.isfinite(energy_max):
                    usable = _default_isoenergy_levels_kcal(energy_max)
                    if usable:
                        iso_path = os.path.join(
                            tempfile.gettempdir(),
                            f"archt_isoenergy_{self.model_key}_{run_id}.gpkg",
                        )
                        isoenergy_vector_path = _create_isoenergy_gpkg(
                            energy_raster_path,
                            iso_path,
                            usable,
                            nodata_value=-9999.0,
                        )
            except Exception:
                isoenergy_vector_path = None

        if create_corridor:
            dist_start = dist_energy if path_cost_mode == "energy_j" else dist_time
            if dist_start is not None and dist_end_for_corridor is not None:
                try:
                    best_cost = float(dist_start[end_rc[0] * cols + end_rc[1]])
                except Exception:
                    best_cost = None

                if best_cost is not None and math.isfinite(best_cost):
                    thr = float(best_cost) * (1.0 + float(corridor_percent) / 100.0)
                    try:
                        start2d = dist_start.reshape((rows, cols))
                        end2d = dist_end_for_corridor.reshape((rows, cols))
                        valid = np.isfinite(start2d) & np.isfinite(end2d) & (~nodata_mask)
                        corridor_mask = np.zeros((rows, cols), dtype=np.uint8)
                        corridor_mask[valid & ((start2d + end2d) <= thr)] = 1
                    except Exception:
                        corridor_mask = None

                    if corridor_mask is not None:
                        corridor_raster_path = os.path.join(
                            tempfile.gettempdir(), f"archt_corridor_{self.model_key}_{run_id}.tif"
                        )
                        driver = gdal.GetDriverByName("GTiff")
                        out_ds = driver.Create(
                            corridor_raster_path,
                            cols,
                            rows,
                            1,
                            gdal.GDT_Byte,
                            options=["TILED=YES", "COMPRESS=LZW"],
                        )
                        if out_ds is not None:
                            out_ds.SetGeoTransform(win_gt)
                            out_ds.SetProjection(proj)
                            out_band = out_ds.GetRasterBand(1)
                            out_band.SetNoDataValue(0)
                            out_band.WriteArray(corridor_mask)
                            out_band.FlushCache()
                            out_ds.FlushCache()
                            out_ds = None

                            if corridor_polygonize:
                                corridor_gpkg_path = os.path.join(
                                    tempfile.gettempdir(),
                                    f"archt_corridor_{self.model_key}_{run_id}.gpkg",
                                )
                                corridor_vector_path = _create_corridor_gpkg(
                                    corridor_raster_path, corridor_gpkg_path
                                )
                        else:
                            corridor_raster_path = None

        path_coords = None
        if create_path and prev_for_path is not None:
            path_idx = _reconstruct_path(prev_for_path, start_rc, end_rc, cols, rows)
            if path_idx:
                coords = []
                for idx in path_idx:
                    r = idx // cols
                    c = idx % cols
                    coords.append(_cell_center(win_gt, c, r))
                path_coords = coords
        lcp_dist_m = _polyline_length(path_coords) if path_coords else None

        straight_time_s = None
        straight_energy_kcal = None
        straight_dist_m = None
        if has_end:
            straight_dist_m = math.hypot(float(ex) - float(sx), float(ey) - float(sy))
            if is_energy_model:
                v = max(
                    0.05,
                    float(self.model_params.get("pandolf_speed_mps", 5.0 * 1000.0 / 3600.0)),
                )
                straight_time_s = float(straight_dist_m) / float(v)

                straight_energy_j, _ = _estimate_straight_line_cost(
                    self.model_key,
                    self.model_params,
                    (float(sx), float(sy)),
                    (float(ex), float(ey)),
                    dem,
                    nodata_mask,
                    win_gt,
                    step_m=min(dx, dy),
                    cost_mode="energy_j",
                )
                if straight_energy_j is not None and math.isfinite(straight_energy_j):
                    straight_energy_kcal = float(straight_energy_j) / 4184.0
            else:
                straight_time_s, straight_dist_m = _estimate_straight_line_cost(
                    self.model_key,
                    self.model_params,
                    (float(sx), float(sy)),
                    (float(ex), float(ey)),
                    dem,
                    nodata_mask,
                    win_gt,
                    step_m=min(dx, dy),
                    cost_mode="time_s",
                )

        lcp_time_s = None
        if has_end:
            if is_energy_model:
                if lcp_dist_m is not None and math.isfinite(lcp_dist_m):
                    v = max(
                        0.05,
                        float(self.model_params.get("pandolf_speed_mps", 5.0 * 1000.0 / 3600.0)),
                    )
                    lcp_time_s = float(lcp_dist_m) / float(v)
            else:
                if end_time_s is not None and math.isfinite(end_time_s):
                    lcp_time_s = float(end_time_s)

        total_energy_kcal = None
        if end_energy_j is not None and math.isfinite(end_energy_j):
            total_energy_kcal = float(end_energy_j) / 4184.0

        msg_parts = []

        if create_path and (end_cost_for_path is None or not math.isfinite(end_cost_for_path)):
            msg_parts.append("도착점까지 경로를 찾지 못했습니다.")
            if path_cost_mode == "energy_j":
                end_energy_j = None
                total_energy_kcal = None
            else:
                end_time_s = None
                lcp_time_s = None

        if create_corridor and not corridor_raster_path:
            msg_parts.append("Least-cost corridor 생성 실패")

        msg = "완료" if not msg_parts else " | ".join(msg_parts)

        if create_corridor and not corridor_raster_path and (not create_time_raster) and (not create_energy_raster) and (not create_path):
            return CostTaskResult(ok=False, message=msg)

        return CostTaskResult(
            ok=True,
            message=msg,
            cost_raster_path=cost_raster_path,
            cost_min=cost_min,
            cost_max=cost_max,
            energy_raster_path=energy_raster_path,
            energy_min=energy_min,
            energy_max=energy_max,
            path_coords=path_coords,
            start_xy=(float(sx), float(sy)),
            end_xy=(float(ex), float(ey)) if has_end else None,
            dem_authid=self.dem_authid,
            dem_source=self.dem_source,
            model_key=self.model_key,
            model_params=dict(self.model_params or {}),
            model_label=self.model_label,
            total_cost_s=end_time_s,
            total_energy_kcal=total_energy_kcal,
            straight_time_s=straight_time_s,
            straight_energy_kcal=straight_energy_kcal,
            straight_dist_m=straight_dist_m,
            lcp_dist_m=lcp_dist_m,
            lcp_time_s=lcp_time_s,
            isochrones_vector_path=isochrones_vector_path,
            isoenergy_vector_path=isoenergy_vector_path,
            corridor_raster_path=corridor_raster_path,
            corridor_vector_path=corridor_vector_path,
            corridor_percent=(float(corridor_percent) if create_corridor else None),
        )


class MultiLineChartWidget(QtWidgets.QWidget):
    """Lightweight multi-line profile widget (QPainter)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.series = []  # [{name,color,dash,points:[(d,val),...]}]
        self.unit = ""
        self.title = ""
        self.on_hover_distance = None  # callback(distance_m|None)
        self.zoom_level = 1.0
        self.pan_offset = 0.0  # distance units
        self.is_dragging = False
        self.drag_start_x = 0
        self.drag_start_offset = 0.0

        self.margin_left = 60
        self.margin_right = 20
        self.margin_top = 28
        self.margin_bottom = 34
        self.setMinimumHeight(160)
        self.setMouseTracking(True)

    def set_series(self, *, title: str, unit: str, series: list):
        self.title = title or ""
        self.unit = unit or ""
        self.series = series or []
        self.zoom_level = 1.0
        self.pan_offset = 0.0
        self.update()

    def _get_extent(self):
        all_pts = []
        for s in self.series:
            pts = s.get("points") or []
            all_pts.extend(pts)
        if not all_pts:
            return None
        max_d = max(float(d) for d, _v in all_pts)
        vals = [float(v) for _d, v in all_pts]
        vmin = min(vals)
        vmax = max(vals)
        if not math.isfinite(max_d) or max_d <= 0:
            return None
        if not math.isfinite(vmin) or not math.isfinite(vmax):
            return None
        if vmax == vmin:
            vmax = vmin + 1.0
        pad = (vmax - vmin) * 0.08
        return max_d, vmin - pad, vmax + pad

    def wheelEvent(self, event):
        if not self.series:
            return
        delta = event.angleDelta().y()
        if delta > 0:
            self.zoom_level = min(12.0, self.zoom_level * 1.2)
        else:
            self.zoom_level = max(1.0, self.zoom_level / 1.2)
        if self.zoom_level == 1.0:
            self.pan_offset = 0.0
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.zoom_level > 1.0:
            self.is_dragging = True
            self.drag_start_x = event.x()
            self.drag_start_offset = self.pan_offset
            self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.is_dragging = False
            self.setCursor(Qt.ArrowCursor)

    def mouseMoveEvent(self, event):
        if not self.series:
            return
        ext = self._get_extent()
        if not ext:
            return
        max_d, _vmin, _vmax = ext

        w = self.width() - self.margin_left - self.margin_right
        if w <= 0:
            return

        visible_range = max_d / max(1.0, float(self.zoom_level))
        max_offset = max(0.0, max_d - visible_range)

        if self.is_dragging and self.zoom_level > 1.0:
            delta_x = self.drag_start_x - event.x()
            delta_d = (float(delta_x) / float(w)) * visible_range
            self.pan_offset = max(0.0, min(max_offset, self.drag_start_offset + delta_d))
            self.update()
            return

        # Tooltip: nearest point values for each series
        if not (self.margin_left <= event.x() <= self.margin_left + w):
            self.setToolTip("")
            if self.on_hover_distance:
                try:
                    self.on_hover_distance(None)
                except Exception:
                    pass
            return
        rel = float(event.x() - self.margin_left) / float(w)
        d = self.pan_offset + rel * visible_range
        if d < 0 or d > max_d:
            self.setToolTip("")
            if self.on_hover_distance:
                try:
                    self.on_hover_distance(None)
                except Exception:
                    pass
            return
        lines = [f"{d:.0f} m"]
        for s in self.series:
            pts = s.get("points") or []
            if not pts:
                continue
            nearest = min(pts, key=lambda p: abs(float(p[0]) - d))
            val = float(nearest[1])
            name = s.get("name") or ""
            if self.unit:
                lines.append(f"{name}: {val:.2f} {self.unit}".rstrip())
            else:
                lines.append(f"{name}: {val:.2f}".rstrip())
        self.setToolTip("\n".join(lines))
        if self.on_hover_distance:
            try:
                self.on_hover_distance(float(d))
            except Exception:
                pass

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        rect_w = self.width()
        rect_h = self.height()
        p.fillRect(0, 0, rect_w, rect_h, QColor(250, 250, 250))

        ext = self._get_extent()
        if not ext:
            p.setPen(QPen(QColor(120, 120, 120), 1))
            p.drawText(self.margin_left, self.margin_top + 20, "데이터 없음")
            return
        max_d, vmin, vmax = ext

        plot_w = rect_w - self.margin_left - self.margin_right
        plot_h = rect_h - self.margin_top - self.margin_bottom
        if plot_w <= 10 or plot_h <= 10:
            return

        visible_range = max_d / max(1.0, float(self.zoom_level))
        visible_range = max(1e-6, visible_range)
        max_offset = max(0.0, max_d - visible_range)
        self.pan_offset = max(0.0, min(max_offset, float(self.pan_offset)))
        view_start = float(self.pan_offset)
        view_end = view_start + visible_range

        # Axes
        p.setPen(QPen(QColor(0, 0, 0, 180), 1))
        x0 = self.margin_left
        y0 = self.margin_top + plot_h
        p.drawLine(x0, self.margin_top, x0, y0)
        p.drawLine(x0, y0, x0 + plot_w, y0)
        if self.title:
            p.drawText(x0, 18, self.title)

        # Helper transforms
        def tx(d):
            return x0 + (float(d) - view_start) * plot_w / visible_range

        def ty(v):
            return self.margin_top + (vmax - float(v)) * plot_h / (vmax - vmin)

        # Draw each series
        for s in self.series:
            pts = s.get("points") or []
            if len(pts) < 2:
                continue
            color = s.get("color") or QColor(0, 120, 255, 220)
            pen = QPen(color, 2)
            if s.get("dash"):
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)

            path_started = False
            prev = None
            for d, v in pts:
                d = float(d)
                if d < view_start - 1e-6 or d > view_end + 1e-6:
                    prev = None
                    continue
                px = tx(d)
                py = ty(v)
                if prev is None:
                    prev = (px, py)
                    path_started = True
                    continue
                if path_started:
                    p.drawLine(int(prev[0]), int(prev[1]), int(px), int(py))
                prev = (px, py)

        # Simple x labels
        p.setPen(QPen(QColor(0, 0, 0, 160), 1))
        p.drawText(x0, y0 + 22, f"{view_start:.0f}m")
        p.drawText(x0 + plot_w - 40, y0 + 22, f"{view_end:.0f}m")


class CostSurfaceDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self.canvas = iface.mapCanvas()

        # Window icon (uses plugin root icon file)
        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            icon_path = os.path.join(plugin_dir, "cost_icon.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except Exception:
            pass

        self._setup_help_button()

        self.original_tool = None
        self.map_tool = None

        self._start_canvas = None
        self._end_canvas = None

        self._rb_start = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        self._rb_start.setColor(QColor(0, 180, 0, 220))
        self._rb_start.setWidth(3)
        self._rb_start.setIcon(QgsRubberBand.ICON_CIRCLE)
        self._rb_start.setIconSize(7)

        self._rb_end = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        self._rb_end.setColor(QColor(220, 0, 0, 220))
        self._rb_end.setWidth(3)
        self._rb_end.setIcon(QgsRubberBand.ICON_CIRCLE)
        self._rb_end.setIconSize(7)

        self._rb_line = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self._rb_line.setColor(QColor(0, 120, 255, 200))
        self._rb_line.setWidth(2)

        self._task = None
        self._task_running = False
        self._layer_temp_outputs = {}  # layer_id -> [temporary file paths]
        self._profile_payloads = {}  # path_layer_id -> payload dict
        self._profile_dialogs = {}  # path_layer_id -> dialog
        self._profile_selection_handlers = {}  # path_layer_id -> handler
        QgsProject.instance().layersWillBeRemoved.connect(self._on_project_layers_removed)

        # Ensure no lingering preview graphics on startup
        self._reset_preview()

        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)
        try:
            if hasattr(self, "cmbFrictionRaster"):
                self.cmbFrictionRaster.setFilters(QgsMapLayerProxyModel.RasterLayer)
            if hasattr(self, "cmbFrictionVector"):
                self.cmbFrictionVector.setFilters(QgsMapLayerProxyModel.VectorLayer)
        except Exception:
            pass
        self._init_models()
        self.cmbModel.currentIndexChanged.connect(self._on_model_changed)
        self._on_model_changed()

        self.btnPickPoints.clicked.connect(self.pick_points_on_map)
        self.btnClearPoints.clicked.connect(self.clear_points)
        self.btnRun.clicked.connect(self.run_analysis)
        self.btnClose.clicked.connect(self.reject)
        self.chkCreatePath.toggled.connect(self._on_create_path_toggled)
        try:
            if hasattr(self, "chkCreateCorridor"):
                self.chkCreateCorridor.toggled.connect(self._on_create_corridor_toggled)
            if hasattr(self, "chkUseFrictionRaster"):
                self.chkUseFrictionRaster.toggled.connect(self._on_friction_raster_toggled)
            if hasattr(self, "chkUseFrictionVector"):
                self.chkUseFrictionVector.toggled.connect(self._on_friction_vector_toggled)
        except Exception:
            pass

        self.cmbDemLayer.layerChanged.connect(self._on_dem_changed)
        self._on_dem_changed()
        self._update_labels()
        self._update_point_help()
        self._on_create_path_toggled(bool(self.chkCreatePath.isChecked()))
        try:
            if hasattr(self, "chkCreateCorridor"):
                self._on_create_corridor_toggled(bool(self.chkCreateCorridor.isChecked()))
            if hasattr(self, "chkUseFrictionRaster"):
                self._on_friction_raster_toggled(bool(self.chkUseFrictionRaster.isChecked()))
            if hasattr(self, "chkUseFrictionVector"):
                self._on_friction_vector_toggled(bool(self.chkUseFrictionVector.isChecked()))
        except Exception:
            pass

    def _setup_help_button(self):
        try:
            self.btnHelp = QtWidgets.QPushButton("도움말", self)
            self.btnHelp.setToolTip("도구 사용법/주의사항을 봅니다.")
            self.btnHelp.clicked.connect(self._on_help)
            if hasattr(self, "horizontalLayout_Buttons"):
                try:
                    idx = self.horizontalLayout_Buttons.indexOf(self.btnClose)
                    if idx >= 0:
                        self.horizontalLayout_Buttons.insertWidget(idx, self.btnHelp)
                    else:
                        self.horizontalLayout_Buttons.addWidget(self.btnHelp)
                except Exception:
                    self.horizontalLayout_Buttons.addWidget(self.btnHelp)
        except Exception:
            pass

    def _on_help(self):
        html = """
<h3>비용표면 / 최소비용경로(Cost Surface / LCP) 도움말</h3>
<p>DEM을 기반으로 경사(및 선택한 모델)에 따라 이동 비용을 계산하고, 시작점→종료점의 최소비용경로를 생성합니다.</p>

<h4>주요 출력</h4>
<ul>
  <li><b>누적 비용 래스터</b>(시간/에너지 등)</li>
  <li><b>최소비용경로</b> 라인(옵션)</li>
  <li>(옵션) 등비용면(isochrone/isoenergy), 회랑(corridor)</li>
</ul>

<h4>사용 순서</h4>
<ol>
  <li>DEM 레이어를 선택하고(권장: 미터 단위 투영좌표계), 모델을 선택합니다.</li>
  <li>지도에서 시작/종료점을 지정합니다.</li>
  <li>필요하면 마찰요인(추가 비용) 옵션을 켠 뒤 실행합니다.</li>
</ol>

<h4>팁</h4>
<ul>
  <li>DEM NoData/해상도/CRS가 결과 품질에 크게 영향합니다.</li>
  <li>회랑(corridor)은 “최소경로 주변의 저비용 통로”를 표현하는 옵션입니다.</li>
</ul>
"""
        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            show_help_dialog(parent=self, title="Cost Surface / LCP 도움말", html=html, plugin_dir=plugin_dir)
        except Exception:
            pass

    def cleanup_for_unload(self):
        """Best-effort cleanup for plugin unload/reload (disconnect global signals, cancel tasks)."""
        self._cleanup_for_unload()

    def _init_models(self):
        self.cmbModel.clear()
        self.cmbModel.addItem("토블러 보행함수 (Tobler Hiking Function)", MODEL_TOBLER)
        self.cmbModel.addItem("나이스미스 규칙 (Naismith's Rule)", MODEL_NAISMITH)
        self.cmbModel.addItem("허조그 메타볼릭 (Herzog metabolic, via Čučković)", MODEL_HERZOG_METABOLIC)
        self.cmbModel.addItem("코놀리&레이크 경사비용 (Conolly & Lake, 2006)", MODEL_CONOLLY_LAKE)
        self.cmbModel.addItem("허조그 차량/수레 (Herzog wheeled vehicle, via Čučković)", MODEL_HERZOG_WHEELED)
        self.cmbModel.addItem("판돌프 운반 에너지 (Pandolf load carriage, 1977)", MODEL_PANDOLF)

    def _is_path_required(self):
        try:
            if bool(self.chkCreatePath.isChecked()):
                return True
        except Exception:
            pass
        try:
            if hasattr(self, "chkCreateCorridor") and bool(self.chkCreateCorridor.isChecked()):
                return True
        except Exception:
            pass
        return False

    def _update_point_help(self):
        try:
            if self._is_path_required():
                self.lblPointHelp.setText("왼쪽 클릭 2번(시작→도착), 우클릭/ESC: 종료")
            else:
                self.lblPointHelp.setText("왼쪽 클릭 1번(시작), 우클릭/ESC: 종료")
        except Exception:
            pass

    def _on_create_path_toggled(self, checked):
        # When LCP output is disabled, drop any previously-selected end point to reduce confusion.
        try:
            if not self._is_path_required():
                self._end_canvas = None
                self._update_preview()
                self._update_labels()
            self._update_point_help()
        except Exception:
            pass

    def _on_create_corridor_toggled(self, checked):
        try:
            if hasattr(self, "spinCorridorPercent"):
                self.spinCorridorPercent.setEnabled(bool(checked))
            if hasattr(self, "chkCorridorPolygon"):
                self.chkCorridorPolygon.setEnabled(bool(checked))

            if not self._is_path_required():
                self._end_canvas = None
                self._update_preview()
                self._update_labels()
            self._update_point_help()
        except Exception:
            pass

    def _on_friction_raster_toggled(self, checked):
        try:
            if hasattr(self, "cmbFrictionRaster"):
                self.cmbFrictionRaster.setEnabled(bool(checked))
            if hasattr(self, "spinFrictionRasterScale"):
                self.spinFrictionRasterScale.setEnabled(bool(checked))
        except Exception:
            pass

    def _on_friction_vector_toggled(self, checked):
        try:
            if hasattr(self, "cmbFrictionVector"):
                self.cmbFrictionVector.setEnabled(bool(checked))
            if hasattr(self, "spinFrictionVectorMult"):
                self.spinFrictionVectorMult.setEnabled(bool(checked))
        except Exception:
            pass

    def _on_model_changed(self):
        try:
            model_key = self.cmbModel.currentData()
            # Reset all param panels first
            self.groupToblerParams.setVisible(False)
            self.groupNaismithParams.setVisible(False)
            self.groupHerzogMetabolicParams.setVisible(False)
            self.groupConollyLakeParams.setVisible(False)
            self.groupHerzogWheeledParams.setVisible(False)
            self.groupPandolfParams.setVisible(False)

            # Energy raster output is only meaningful for energy-based models (Pandolf).
            try:
                if model_key == MODEL_PANDOLF:
                    self.chkCreateEnergyRaster.setEnabled(True)
                    if not self.chkCreateEnergyRaster.isChecked():
                        self.chkCreateEnergyRaster.setChecked(True)
                else:
                    self.chkCreateEnergyRaster.setChecked(False)
                    self.chkCreateEnergyRaster.setEnabled(False)
            except Exception:
                pass

            if model_key == MODEL_TOBLER:
                self.groupToblerParams.setVisible(True)
                self.lblModelHelp.setText(
                    "<b>토블러 보행함수 (Tobler, 1993)</b><br>"
                    "속도(km/h)=a·exp(-b·|slope+c|), slope=Δz/Δd (예: 0.1=10%)<br>"
                    "<br><b>변수 해석</b><br>"
                    "• 기본속도(a): 평지 기준 속도. 값↑ → 전체 시간이↓<br>"
                    "• 경사 민감도(b): 값↑ → 경사에 따른 속도 감소가 더 급격<br>"
                    "• 최적 경사(c): 속도가 가장 빠른 경사(대략 -c가 최적). c=0.05는 약 -5% 내리막에서 최적<br>"
                    "<br><b>분석 제한</b>: 0=DEM 전체(느릴 수 있음), 값&gt;0=주변만 계산(빠름)<br>"
                    "<b>누적 비용</b>: 출발점→각 셀 최소 이동시간(분)<br>"
                    "<b>최소비용경로</b>: 출발점→도착점 경로(도착점 필요)"
                )
            elif model_key == MODEL_NAISMITH:
                self.groupNaismithParams.setVisible(True)
                self.lblModelHelp.setText(
                    "<b>나이스미스 규칙 (Naismith, 1892)</b><br>"
                    "시간=수평거리/속도 + 상승고도/상승페널티(하강은 페널티 없음)<br>"
                    "<br><b>변수 해석</b><br>"
                    "• 수평 속도: 값↑ → 전체 시간이↓<br>"
                    "• 상승 페널티(m/h): 값↓ → 오르막에 더 불리(상승에 더 많은 시간 부여)<br>"
                    "<br><b>분석 제한</b>: 0=DEM 전체(느릴 수 있음), 값&gt;0=주변만 계산(빠름)<br>"
                    "<b>누적 비용</b>: 출발점→각 셀 최소 이동시간(분)<br>"
                    "<b>최소비용경로</b>: 출발점→도착점 경로(도착점 필요)"
                )
            elif model_key == MODEL_HERZOG_METABOLIC:
                self.groupHerzogMetabolicParams.setVisible(True)
                self.lblModelHelp.setText(
                    "<b>허조그 메타볼릭(상대속도) (Herzog metabolic, via Čučković)</b><br>"
                    "경사(절대값)에 따른 이동 저항을 다항식으로 표현한 모델입니다. (상·하행 동일하게 취급)<br>"
                    "<br><b>변수 해석</b><br>"
                    "• 기본속도: 평지 기준 속도. 값↑ → 전체 시간이↓<br>"
                    "<br><b>참고</b>: 수식은 Zoran Čučković의 QGIS 'Movement Analysis' 플러그인(slope_cost) 구현을 따릅니다.<br>"
                    "<br><b>누적 비용</b>: 출발점→각 셀 최소 이동시간(분)"
                )
            elif model_key == MODEL_CONOLLY_LAKE:
                self.groupConollyLakeParams.setVisible(True)
                self.lblModelHelp.setText(
                    "<b>코놀리&레이크 경사비용 (Conolly & Lake, 2006)</b><br>"
                    "경사(절대값)에 비례한 상대 비용을 적용합니다. (상·하행 동일하게 취급)<br>"
                    "<br><b>변수 해석</b><br>"
                    "• 기본속도: 평지 기준 속도. 값↑ → 전체 시간이↓<br>"
                    "• 기준경사(°): 값↓ → 약한 경사에도 페널티가 빨리 커짐(민감). 값↑ → 완만한 지형에서는 차이가 줄어듦<br>"
                    "<br><b>주의</b>: 완만한 지형이 '더 빠르게' 나오지 않도록, 기준경사 이하에서는 페널티를 1로 고정합니다.<br>"
                    "<br><b>누적 비용</b>: 출발점→각 셀 최소 이동시간(분)"
                )
            elif model_key == MODEL_HERZOG_WHEELED:
                self.groupHerzogWheeledParams.setVisible(True)
                self.lblModelHelp.setText(
                    "<b>허조그 차량/수레 모델 (Herzog wheeled vehicle, via Čučković)</b><br>"
                    "경사가 커질수록 속도가 비선형으로 급격히 감소하는 차량/수레 모델입니다. (상·하행 동일)<br>"
                    "<br><b>변수 해석</b><br>"
                    "• 기본속도: 평지 기준 속도. 값↑ → 전체 시간이↓<br>"
                    "• 임계경사(°): 값↓ → 경사에 더 취약(조금만 경사져도 속도 급감). 값↑ → 경사 영향이 완만<br>"
                    "• 통행한계(°): 이 각도를 넘는 경사는 사실상 '불통'으로 간주해 강하게 회피합니다(차량/수레에 현실적).<br>"
                    "<br><b>참고</b>: 수식은 Zoran Čučković의 QGIS 'Movement Analysis' 플러그인(slope_cost) 구현을 참고했습니다.<br>"
                    "<br><b>주의</b>: 이 도구는 '도로/길' 정보를 모르므로, 평지 우회로가 너무 길면 산을 가로지르는 경로가 선택될 수 있습니다. "
                    "차량/수레라면 임계경사를 낮추거나 통행한계를 설정해 완만한 경로를 더 선호하게 하세요.<br>"
                    "<br><b>누적 비용</b>: 출발점→각 셀 최소 이동시간(분)"
                )
            elif model_key == MODEL_PANDOLF:
                self.groupPandolfParams.setVisible(True)
                self.lblModelHelp.setText(
                    "<b>판돌프 운반 에너지 (Pandolf load carriage, 1977)</b><br>"
                    "운반(체중/짐)과 지면계수(η), 경사(%)를 고려해 에너지 소모를 계산합니다.<br>"
                    "<br><b>핵심</b>: 이 모델은 <u>시간</u>이 아니라 <u>에너지(소모)</u>를 최소화하는 경로를 찾는 데 적합합니다.<br>"
                    "<br><b>변수 해석</b><br>"
                    "• 체중/짐: 값↑ → 에너지 비용↑ (특히 짐/체중 비율 영향)<br>"
                    "• 속도: 시간은 거리/속도이지만, 에너지(수식)도 속도에 따라 변합니다<br>"
                    "• 지면계수 η: 1.0=단단한 지면, 값↑ → 같은 경사에서도 에너지 비용↑<br>"
                    "<br><b>출력</b><br>"
                    "• 누적 에너지(kcal): 출발점→각 셀 최소 누적 에너지 (체크 시 생성)<br>"
                    "• 누적 시간(분): (체크 시) 속도 기반 이동시간을 별도로 출력할 수 있습니다<br>"
                    "<br><b>참고</b>: 에너지(kcal)=J/4184 로 변환하여 저장합니다."
                )
        except Exception:
            pass

    def _on_dem_changed(self):
        dem_layer = self.cmbDemLayer.currentLayer()
        if not dem_layer:
            return
        if not is_metric_crs(dem_layer.crs()):
            push_message(
                self.iface,
                "주의",
                "DEM CRS가 미터 단위가 아닙니다. (권장: 투영좌표계/미터)",
                level=1,
                duration=5,
            )

    def pick_points_on_map(self):
        dem_layer = self.cmbDemLayer.currentLayer()
        if not dem_layer:
            push_message(self.iface, "오류", "먼저 DEM을 선택하세요.", level=2)
            restore_ui_focus(self)
            return

        self.original_tool = self.canvas.mapTool()
        if self.map_tool is None:
            self.map_tool = CostPathPointTool(self.canvas, self)
        self.canvas.setMapTool(self.map_tool)
        self.hide()
        msg = "지도에서 시작점을 클릭하세요. (우클릭/ESC 종료)"
        if self._is_path_required():
            msg = "지도에서 시작점→도착점을 순서대로 클릭하세요. (우클릭/ESC 종료)"
        push_message(
            self.iface,
            "비용표면/최소비용경로",
            msg,
            level=0,
            duration=6,
        )

    def set_start_point(self, point_canvas):
        self._start_canvas = point_canvas
        self._end_canvas = None
        self._update_preview()
        self._update_labels()

    def set_end_point(self, point_canvas):
        self._end_canvas = point_canvas
        self._update_preview()
        self._update_labels()

    def finish_map_selection(self):
        try:
            if self.original_tool:
                self.canvas.setMapTool(self.original_tool)
        except Exception:
            pass
        restore_ui_focus(self)

    def clear_points(self):
        self._start_canvas = None
        self._end_canvas = None
        self._reset_preview()
        self._update_labels()

    def _reset_preview(self):
        try:
            self._rb_start.reset(QgsWkbTypes.PointGeometry)
            self._rb_start.hide()
            self._rb_end.reset(QgsWkbTypes.PointGeometry)
            self._rb_end.hide()
            self._rb_line.reset(QgsWkbTypes.LineGeometry)
            self._rb_line.hide()
        except Exception:
            pass

    def _update_preview(self):
        self._reset_preview()
        if self._start_canvas:
            self._rb_start.show()
            self._rb_start.addPoint(self._start_canvas)
        if self._end_canvas:
            self._rb_end.show()
            self._rb_end.addPoint(self._end_canvas)
        if self._start_canvas and self._end_canvas:
            self._rb_line.show()
            self._rb_line.addPoint(self._start_canvas)
            self._rb_line.addPoint(self._end_canvas)

    def _update_labels(self):
        if not self._start_canvas:
            self.lblStart.setText("시작점: (미설정)")
        else:
            self.lblStart.setText(
                f"시작점: {self._start_canvas.x():.3f}, {self._start_canvas.y():.3f}"
            )

        if not self._end_canvas:
            self.lblEnd.setText("도착점: (선택)")
        else:
            self.lblEnd.setText(
                f"도착점: {self._end_canvas.x():.3f}, {self._end_canvas.y():.3f}"
            )

        if self._start_canvas and self._end_canvas:
            d = math.hypot(
                self._end_canvas.x() - self._start_canvas.x(),
                self._end_canvas.y() - self._start_canvas.y(),
            )
            self.lblDistance.setText(f"직선거리: {d:.1f} (지도 CRS 단위)")
        else:
            self.lblDistance.setText("직선거리: -")

    def run_analysis(self):
        if self._task_running:
            push_message(self.iface, "비용표면/최소비용경로", "이미 작업이 실행 중입니다.", level=1)
            return

        dem_layer = self.cmbDemLayer.currentLayer()
        if not dem_layer:
            push_message(self.iface, "오류", "DEM을 선택하세요.", level=2)
            restore_ui_focus(self)
            return
        if not is_metric_crs(dem_layer.crs()):
            push_message(self.iface, "오류", "DEM CRS가 미터 단위가 아닙니다. (권장: 투영좌표계/미터)", level=2)
            restore_ui_focus(self)
            return
        if not self._start_canvas:
            push_message(self.iface, "오류", "시작점을 먼저 지정하세요.", level=2)
            restore_ui_focus(self)
            return

        model_key = self.cmbModel.currentData()
        if not model_key:
            push_message(self.iface, "오류", "모델을 선택하세요.", level=2)
            restore_ui_focus(self)
            return

        create_cost_raster = bool(self.chkCreateCostRaster.isChecked())
        create_energy_raster = bool(getattr(self, "chkCreateEnergyRaster", None) and self.chkCreateEnergyRaster.isChecked())
        create_path = bool(self.chkCreatePath.isChecked())
        create_corridor = bool(getattr(self, "chkCreateCorridor", None) and self.chkCreateCorridor.isChecked())

        if not create_cost_raster and not create_energy_raster and not create_path and not create_corridor:
            push_message(self.iface, "오류", "최소 1개 출력(누적 비용/경로)을 선택하세요.", level=2)
            restore_ui_focus(self)
            return
        if (create_path or create_corridor) and not self._end_canvas:
            push_message(self.iface, "오류", "경로/회랑을 생성하려면 도착점이 필요합니다.", level=2)
            restore_ui_focus(self)
            return

        use_friction_raster = bool(
            getattr(self, "chkUseFrictionRaster", None) and self.chkUseFrictionRaster.isChecked()
        )
        friction_raster_layer = self.cmbFrictionRaster.currentLayer() if use_friction_raster and hasattr(self, "cmbFrictionRaster") else None
        friction_raster_scale = float(self.spinFrictionRasterScale.value()) if use_friction_raster and hasattr(self, "spinFrictionRasterScale") else 1.0
        if use_friction_raster and not friction_raster_layer:
            push_message(self.iface, "오류", "추가 마찰(래스터) 레이어를 선택하세요.", level=2)
            restore_ui_focus(self)
            return

        use_friction_vector = bool(
            getattr(self, "chkUseFrictionVector", None) and self.chkUseFrictionVector.isChecked()
        )
        friction_vector_layer = self.cmbFrictionVector.currentLayer() if use_friction_vector and hasattr(self, "cmbFrictionVector") else None
        friction_vector_multiplier = float(self.spinFrictionVectorMult.value()) if use_friction_vector and hasattr(self, "spinFrictionVectorMult") else 1.0
        if use_friction_vector and not friction_vector_layer:
            push_message(self.iface, "오류", "추가 마찰(벡터) 레이어를 선택하세요.", level=2)
            restore_ui_focus(self)
            return
        if use_friction_vector and friction_vector_layer and (friction_vector_layer.crs() != dem_layer.crs()):
            push_message(self.iface, "오류", "추가 마찰(벡터) 레이어 CRS는 DEM CRS와 동일해야 합니다.", level=2)
            restore_ui_focus(self)
            return

        corridor_percent = float(self.spinCorridorPercent.value()) if create_corridor and hasattr(self, "spinCorridorPercent") else 5.0
        corridor_polygonize = bool(self.chkCorridorPolygon.isChecked()) if create_corridor and hasattr(self, "chkCorridorPolygon") else True

        # Live log window (non-modal) so users can see progress in real time.
        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)

        buffer_m = float(self.spinBuffer.value())
        allow_diagonal = bool(self.chkDiagonal.isChecked())
        model_label = self.cmbModel.currentText()

        canvas_crs = self.canvas.mapSettings().destinationCrs()
        start_dem = transform_point(self._start_canvas, canvas_crs, dem_layer.crs())
        end_dem = (
            transform_point(self._end_canvas, canvas_crs, dem_layer.crs())
            if self._end_canvas
            else None
        )

        model_params = {
            "tobler_base_kmh": float(self.spinToblerBaseKmh.value()),
            "tobler_slope_factor": float(self.spinToblerSlopeFactor.value()),
            "tobler_slope_offset": float(self.spinToblerOffset.value()),
            "tobler_min_speed_mps": 0.05,
            "naismith_horizontal_kmh": float(self.spinNaismithSpeedKmh.value()),
            "naismith_ascent_m_per_h": float(self.spinNaismithAscentMph.value()),
            "min_speed_mps": 0.05,
            "herzog_base_kmh": float(self.spinHerzogBaseKmh.value()),
            "conolly_base_kmh": float(self.spinConollyBaseKmh.value()),
            "conolly_ref_slope_deg": float(self.spinConollyRefSlopeDeg.value()),
            "wheeled_base_kmh": float(self.spinWheeledBaseKmh.value()),
            "wheeled_critical_slope_deg": float(self.spinWheeledCriticalSlopeDeg.value()),
            "wheeled_max_slope_deg": float(getattr(self, "spinWheeledMaxSlopeDeg", None).value()) if hasattr(self, "spinWheeledMaxSlopeDeg") else 45.0,
            "pandolf_body_kg": float(self.spinPandolfBodyKg.value()),
            "pandolf_load_kg": float(self.spinPandolfLoadKg.value()),
            "pandolf_speed_mps": float(self.spinPandolfSpeedKmh.value()) * 1000.0 / 3600.0,
            "pandolf_terrain_factor": float(self.spinPandolfTerrainFactor.value()),
        }

        self._set_running_ui(True)

        def on_done(res):
            self._task_running = False
            self._task = None
            self._set_running_ui(False)
            self._handle_task_result(res)

        task = CostSurfaceWorker(
            dem_source=dem_layer.source(),
            dem_authid=dem_layer.crs().authid(),
            start_xy=(float(start_dem.x()), float(start_dem.y())),
            end_xy=(float(end_dem.x()), float(end_dem.y())) if end_dem else None,
            buffer_m=buffer_m,
            allow_diagonal=allow_diagonal,
            model_key=model_key,
            model_params=model_params,
            model_label=model_label,
            create_cost_raster=create_cost_raster,
            create_energy_raster=create_energy_raster,
            create_path=create_path,
            create_corridor=create_corridor,
            corridor_percent=corridor_percent,
            corridor_polygonize=corridor_polygonize,
            friction_raster_source=(friction_raster_layer.source() if use_friction_raster and friction_raster_layer else None),
            friction_raster_scale=friction_raster_scale,
            friction_vector_source=(friction_vector_layer.source() if use_friction_vector and friction_vector_layer else None),
            friction_vector_multiplier=friction_vector_multiplier,
            on_done=on_done,
        )
        self._task = task
        self._task_running = True
        QgsApplication.taskManager().addTask(task)
        push_message(self.iface, "비용표면/최소비용경로", "분석을 시작했습니다. (QGIS 작업 관리자 확인)", level=0, duration=6)

    def _set_running_ui(self, running: bool):
        self.btnRun.setEnabled(not running)
        self.btnPickPoints.setEnabled(not running)
        self.btnClearPoints.setEnabled(not running)
        self.btnClose.setEnabled(not running)

    def _handle_task_result(self, res: CostTaskResult):
        if not isinstance(res, CostTaskResult) or not res.ok:
            msg = getattr(res, "message", "") or "분석 실패"
            push_message(self.iface, "오류", msg, level=2, duration=8)
            return

        try:
            self._add_result_layers(res)
        except Exception as e:
            log_message(f"Add cost result layers error: {e}", level=Qgis.Critical)
            push_message(self.iface, "오류", f"결과 레이어 추가 실패: {e}", level=2, duration=8)
            return

        summary = res.message or "완료"

        if res.total_energy_kcal is not None and math.isfinite(res.total_energy_kcal):
            lcp_kcal = float(res.total_energy_kcal)
            lcp_detail = []
            if res.lcp_dist_m is not None and math.isfinite(res.lcp_dist_m):
                lcp_detail.append(f"{res.lcp_dist_m/1000.0:.2f}km")
            if res.lcp_time_s is not None and math.isfinite(res.lcp_time_s):
                lcp_detail.append(f"{res.lcp_time_s/60.0:.1f}분")
            lcp_txt = f"LCP {lcp_kcal:.0f}kcal"
            if lcp_detail:
                lcp_txt = f"{lcp_txt}({', '.join(lcp_detail)})"

            if res.straight_energy_kcal is not None and math.isfinite(res.straight_energy_kcal):
                straight_kcal = float(res.straight_energy_kcal)
                straight_detail = []
                if res.straight_dist_m is not None and math.isfinite(res.straight_dist_m):
                    straight_detail.append(f"{res.straight_dist_m/1000.0:.2f}km")
                if res.straight_time_s is not None and math.isfinite(res.straight_time_s):
                    straight_detail.append(f"{res.straight_time_s/60.0:.1f}분")
                straight_txt = f"직선 {straight_kcal:.0f}kcal"
                if straight_detail:
                    straight_txt = f"{straight_txt}({', '.join(straight_detail)})"

                delta_kcal = straight_kcal - lcp_kcal
                sign = "+" if delta_kcal >= 0 else "-"
                summary = f"{summary} | {lcp_txt} / {straight_txt} (Δ {sign}{abs(delta_kcal):.0f}kcal)"
            else:
                summary = f"{summary} | {lcp_txt}"
        else:
            lcp_time_s = res.lcp_time_s if res.lcp_time_s is not None else res.total_cost_s
            if lcp_time_s is not None and math.isfinite(lcp_time_s):
                summary = f"{summary} | LCP {float(lcp_time_s)/60.0:.1f}분"

            if (
                res.straight_time_s is not None
                and math.isfinite(res.straight_time_s)
                and lcp_time_s is not None
                and math.isfinite(lcp_time_s)
            ):
                lcp_min = float(lcp_time_s) / 60.0
                straight_min = float(res.straight_time_s) / 60.0
                delta_min = straight_min - lcp_min
                sign = "+" if delta_min >= 0 else "-"
                if (
                    res.straight_dist_m is not None
                    and math.isfinite(res.straight_dist_m)
                    and res.lcp_dist_m is not None
                    and math.isfinite(res.lcp_dist_m)
                ):
                    summary = (
                        f"{summary}({res.lcp_dist_m/1000.0:.2f}km)"
                        f" / 직선 {straight_min:.1f}분({res.straight_dist_m/1000.0:.2f}km)"
                        f" (Δ {sign}{abs(delta_min):.1f}분)"
                    )
                else:
                    summary = f"{summary} / 직선 {straight_min:.1f}분 (Δ {sign}{abs(delta_min):.1f}분)"
        push_message(self.iface, "비용표면/최소비용경로", summary, level=0, duration=7)

    def _add_result_layers(self, res: CostTaskResult):
        project = QgsProject.instance()
        root = project.layerTreeRoot()

        parent_name = "ArchToolkit - 비용표면/최소비용경로 (Cost Surface / LCP)"
        parent_group = root.findGroup(parent_name)
        if parent_group is None:
            parent_group = root.insertGroup(0, parent_name)

        run_id = uuid.uuid4().hex[:6]
        model_tag = _safe_layer_name_fragment(res.model_label or "")
        group_name = f"비용표면_{model_tag}_{run_id}" if model_tag else f"비용표면_{run_id}"
        run_group = parent_group.insertGroup(0, group_name)
        run_group.setExpanded(False)

        bottom_to_top = []

        if res.cost_raster_path:
            layer_name = f"누적 비용(분) (Cumulative Cost, min) - {(res.model_label or '').strip()}"
            cost_layer = QgsRasterLayer(res.cost_raster_path, layer_name)
            if cost_layer.isValid():
                self._tag_cost_surface_layer(cost_layer, run_id, "cost_raster")
                self._apply_cost_raster_style(cost_layer, res.cost_min, res.cost_max)
                self._track_layer_output(cost_layer, res.cost_raster_path)
                bottom_to_top.append(cost_layer)

        if res.energy_raster_path:
            layer_name = f"누적 에너지(kcal) (Cumulative Energy, kcal) - {(res.model_label or '').strip()}"
            energy_layer = QgsRasterLayer(res.energy_raster_path, layer_name)
            if energy_layer.isValid():
                self._tag_cost_surface_layer(energy_layer, run_id, "energy_raster")
                self._apply_energy_raster_style(
                    energy_layer, res.energy_min, res.energy_max
                )
                self._track_layer_output(energy_layer, res.energy_raster_path)
                bottom_to_top.append(energy_layer)

        if res.corridor_raster_path:
            pct = res.corridor_percent
            pct_txt = f"{float(pct):.1f}%" if pct is not None and math.isfinite(float(pct)) else ""
            layer_name = "Least-cost corridor"
            if pct_txt:
                layer_name = f"{layer_name} ({pct_txt})"
            if model_tag:
                layer_name = f"{layer_name} - {model_tag}"
            corridor_layer = QgsRasterLayer(res.corridor_raster_path, layer_name)
            if corridor_layer.isValid():
                self._tag_cost_surface_layer(corridor_layer, run_id, "corridor_raster")
                self._apply_corridor_raster_style(corridor_layer)
                self._track_layer_output(corridor_layer, res.corridor_raster_path)
                bottom_to_top.append(corridor_layer)

        if res.corridor_vector_path:
            pct = res.corridor_percent
            pct_txt = f"{float(pct):.1f}%" if pct is not None and math.isfinite(float(pct)) else ""
            layer_name = "Least-cost corridor (polygon)"
            if pct_txt:
                layer_name = f"{layer_name} ({pct_txt})"
            if model_tag:
                layer_name = f"{layer_name} - {model_tag}"
            corridor_poly = QgsVectorLayer(
                f"{res.corridor_vector_path}|layername=corridor",
                layer_name,
                "ogr",
            )
            if corridor_poly.isValid():
                self._tag_cost_surface_layer(corridor_poly, run_id, "corridor_polygon")
                self._apply_corridor_polygon_style(corridor_poly)
                self._track_layer_output(corridor_poly, res.corridor_vector_path)
                bottom_to_top.append(corridor_poly)

        if res.isochrones_vector_path:
            iso_name = "등시간선 (Isochrones)"
            if model_tag:
                iso_name = f"{iso_name} - {model_tag}"
            iso_layer = QgsVectorLayer(
                f"{res.isochrones_vector_path}|layername=isochrones",
                iso_name,
                "ogr",
            )
            if iso_layer.isValid():
                self._tag_cost_surface_layer(iso_layer, run_id, "isochrones")
                self._apply_isochrone_style(iso_layer)
                self._track_layer_output(iso_layer, res.isochrones_vector_path)
                bottom_to_top.append(iso_layer)

        if res.isoenergy_vector_path:
            iso_name = "등에너지선 (Iso-energy)"
            if model_tag:
                iso_name = f"{iso_name} - {model_tag}"
            iso_layer = QgsVectorLayer(
                f"{res.isoenergy_vector_path}|layername=isoenergy",
                iso_name,
                "ogr",
            )
            if iso_layer.isValid():
                self._tag_cost_surface_layer(iso_layer, run_id, "isoenergy")
                self._apply_isoenergy_style(iso_layer)
                self._track_layer_output(iso_layer, res.isoenergy_vector_path)
                bottom_to_top.append(iso_layer)

        if res.start_xy and res.dem_authid:
            pt_layer = QgsVectorLayer(f"Point?crs={res.dem_authid}", "시작/도착점 (Start/End)", "memory")
            pr = pt_layer.dataProvider()
            pr.addAttributes([QgsField("role", QVariant.String)])
            pt_layer.updateFields()
            self._tag_cost_surface_layer(pt_layer, run_id, "start_end_points")

            f_start = QgsFeature(pt_layer.fields())
            f_start.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*res.start_xy)))
            f_start.setAttributes(["start"])

            feats = [f_start]
            if res.end_xy:
                f_end = QgsFeature(pt_layer.fields())
                f_end.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*res.end_xy)))
                f_end.setAttributes(["end"])
                feats.append(f_end)

            pr.addFeatures(feats)
            pt_layer.updateExtents()

            symbol = QgsMarkerSymbol.createSimple(
                {
                    "name": "circle",
                    "color": "255,0,0,220",
                    "size": "2.5",
                    "outline_width": "0.2",
                }
            )
            pt_layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            bottom_to_top.append(pt_layer)

        if res.end_xy and res.start_xy and res.dem_authid:
            path_name = "경로 비교 (Straight vs LCP)"
            if model_tag:
                path_name = f"{path_name} - {model_tag}"
            path_layer = QgsVectorLayer(
                f"LineString?crs={res.dem_authid}", path_name, "memory"
            )
            pr = path_layer.dataProvider()
            pr.addAttributes(
                [
                    QgsField("kind", QVariant.String),
                    QgsField("model", QVariant.String),
                    QgsField("dist_m", QVariant.Double),
                    QgsField("time_min", QVariant.Double),
                    QgsField("energy_kcal", QVariant.Double),
                ]
            )
            path_layer.updateFields()
            self._tag_cost_surface_layer(path_layer, run_id, "path_compare")

            feats = []

            # Straight line (shortest distance)
            straight_pts = [QgsPointXY(*res.start_xy), QgsPointXY(*res.end_xy)]
            feat_straight = QgsFeature(path_layer.fields())
            feat_straight.setGeometry(QgsGeometry.fromPolylineXY(straight_pts))
            feat_straight.setAttributes(
                [
                    "straight",
                    res.model_label or "",
                    float(res.straight_dist_m or 0.0),
                    (float(res.straight_time_s) / 60.0) if res.straight_time_s is not None else None,
                    float(res.straight_energy_kcal) if res.straight_energy_kcal is not None else None,
                ]
            )
            feats.append(feat_straight)

            # Least-cost path (if available)
            if res.path_coords and len(res.path_coords) >= 2:
                lcp_pts = [QgsPointXY(x, y) for x, y in res.path_coords]
                feat_lcp = QgsFeature(path_layer.fields())
                feat_lcp.setGeometry(QgsGeometry.fromPolylineXY(lcp_pts))
                feat_lcp.setAttributes(
                    [
                        "lcp",
                        res.model_label or "",
                        float(res.lcp_dist_m or 0.0),
                        (float(res.lcp_time_s) / 60.0) if res.lcp_time_s is not None else None,
                        float(res.total_energy_kcal) if res.total_energy_kcal is not None else None,
                    ]
                )
                feats.append(feat_lcp)

            pr.addFeatures(feats)
            path_layer.updateExtents()

            # Categorized renderer: straight (dashed) vs lcp (solid)
            sym_straight = QgsLineSymbol.createSimple(
                {"color": "90,90,90,220", "width": "1.4", "line_style": "dash"}
            )
            sym_lcp = QgsLineSymbol.createSimple({"color": "0,180,0,220", "width": "1.8"})
            renderer = QgsCategorizedSymbolRenderer(
                "kind",
                [
                    QgsRendererCategory("straight", sym_straight, "직선 (Straight)"),
                    QgsRendererCategory("lcp", sym_lcp, "최소비용경로 (LCP)"),
                ],
            )
            path_layer.setRenderer(renderer)

            # Label only the LCP feature (map-friendly)
            try:
                pal = QgsPalLayerSettings()
                pal.isExpression = True
                pal.fieldName = (
                    "case "
                    "when \"kind\"='lcp' then "
                    "  'LCP ' || round(\"dist_m\"/1000.0, 2) || 'km'"
                    "  || coalesce(' / ' || round(\"time_min\", 1) || '분', '')"
                    "  || coalesce(' / ' || round(\"energy_kcal\", 0) || 'kcal', '')"
                    "end"
                )
                pal.placement = QgsPalLayerSettings.Curved
                fmt = QgsTextFormat()
                fmt.setSize(10.0)
                fmt.setColor(QColor(10, 10, 10))
                buf = QgsTextBufferSettings()
                buf.setEnabled(True)
                buf.setColor(QColor(255, 255, 255, 220))
                buf.setSize(1.2)
                fmt.setBuffer(buf)
                pal.setFormat(fmt)
                path_layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
                path_layer.setLabelsEnabled(True)
            except Exception:
                pass

            # Store payload so selecting the line can reopen a profile.
            try:
                path_layer.setCustomProperty("archtoolkit/dem_source", res.dem_source or "")
                path_layer.setCustomProperty("archtoolkit/model_key", res.model_key or "")
            except Exception:
                pass
            self._profile_payloads[path_layer.id()] = {
                "dem_source": res.dem_source,
                "dem_authid": res.dem_authid,
                "model_key": res.model_key,
                "model_params": res.model_params or {},
                "model_label": res.model_label or "",
                "start_xy": res.start_xy,
                "end_xy": res.end_xy,
                "path_coords": res.path_coords,
            }
            try:
                handler = lambda *_args, lid=path_layer.id(): self._on_path_layer_selection_changed(lid)
                self._profile_selection_handlers[path_layer.id()] = handler
                path_layer.selectionChanged.connect(handler)
            except Exception:
                pass

            bottom_to_top.append(path_layer)

            # Milestones along LCP for map-friendly reading (every 500m).
            try:
                if res.path_coords and len(res.path_coords) >= 2 and res.dem_source:
                    milestone_layer = self._create_lcp_milestones_layer(
                        dem_source=res.dem_source,
                        crs_authid=res.dem_authid,
                        model_key=res.model_key,
                        model_params=res.model_params or {},
                        path_coords=res.path_coords,
                        interval_m=500.0,
                        layer_name=f"LCP 마일스톤 (500m) - {model_tag}" if model_tag else "LCP 마일스톤 (500m)",
                    )
                    if milestone_layer is not None and milestone_layer.isValid():
                        self._tag_cost_surface_layer(milestone_layer, run_id, "milestones")
                        bottom_to_top.append(milestone_layer)
            except Exception as e:
                log_message(f"Milestone layer error: {e}", level=Qgis.Warning)

        for lyr in bottom_to_top:
            project.addMapLayer(lyr, False)
            run_group.insertLayer(0, lyr)

        try:
            # Keep results visible even when rasters are added later.
            if parent_group.parent() == root:
                idx = root.children().index(parent_group)
                if idx != 0:
                    root.removeChildNode(parent_group)
                    root.insertChildNode(0, parent_group)
        except Exception:
            pass

    def _tag_cost_surface_layer(self, layer: QgsMapLayer, run_id: str, kind: str):
        """Attach metadata to result layers for later cleanup (e.g., transient rubberbands)."""
        if layer is None:
            return
        try:
            layer.setCustomProperty("archtoolkit/cost_surface/run_id", str(run_id))
            layer.setCustomProperty("archtoolkit/cost_surface/kind", str(kind))
        except Exception:
            pass
        try:
            units = ""
            if str(kind) == "cost_raster":
                units = "min"
            elif str(kind) == "energy_raster":
                units = "kcal"
            set_archtoolkit_layer_metadata(
                layer,
                tool_id="cost_surface",
                run_id=str(run_id),
                kind=str(kind or ""),
                units=units,
            )
        except Exception:
            pass

    def _track_layer_output(self, layer: QgsMapLayer, path: Optional[str]):
        if not layer or not path:
            return
        self._layer_temp_outputs.setdefault(layer.id(), []).append(path)

    def _cleanup_layer_outputs(self, layer_ids):
        remove_preview = False

        # Close profile dialogs / disconnect handlers for removed layers
        try:
            for lid in layer_ids:
                layer = None
                try:
                    layer = QgsProject.instance().mapLayer(lid)
                    if layer and layer.customProperty("archtoolkit/cost_surface/run_id", None) is not None:
                        remove_preview = True
                except Exception:
                    pass
                try:
                    handler = self._profile_selection_handlers.pop(lid, None)
                    if layer and handler:
                        layer.selectionChanged.disconnect(handler)
                except Exception:
                    pass
                try:
                    dlg = self._profile_dialogs.pop(lid, None)
                    if dlg:
                        dlg.close()
                        try:
                            dlg.deleteLater()
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    self._profile_payloads.pop(lid, None)
                except Exception:
                    pass
        except Exception:
            pass

        if remove_preview:
            self._reset_preview()

        paths = []
        for lid in layer_ids:
            outputs = self._layer_temp_outputs.pop(lid, [])
            if outputs:
                paths.extend(outputs)
        if paths:
            cleanup_files(paths)

    def _on_project_layers_removed(self, layer_ids):
        self._cleanup_layer_outputs(layer_ids)

    def _apply_isochrone_style(self, layer: QgsVectorLayer):
        try:
            symbol = QgsLineSymbol.createSimple(
                {"color": "20,20,20,200", "width": "0.9", "line_style": "dash"}
            )
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))

            pal = QgsPalLayerSettings()
            pal.isExpression = True
            # Minutes -> show hours for large values to improve readability.
            pal.fieldName = (
                "case "
                "when \"minutes\" >= 120 then round(\"minutes\"/60.0, 1) || 'h' "
                "else round(\"minutes\", 0) || '분' "
                "end"
            )
            pal.placement = QgsPalLayerSettings.Curved

            fmt = QgsTextFormat()
            fmt.setSize(10.0)
            fmt.setColor(QColor(10, 10, 10))

            buf = QgsTextBufferSettings()
            buf.setEnabled(True)
            buf.setColor(QColor(255, 255, 255, 220))
            buf.setSize(1.2)
            fmt.setBuffer(buf)
            pal.setFormat(fmt)

            layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
            layer.setLabelsEnabled(True)
            layer.triggerRepaint()
        except Exception as e:
            log_message(f"Isochrone style error: {e}", level=Qgis.Warning)

    def _apply_isoenergy_style(self, layer: QgsVectorLayer):
        try:
            symbol = QgsLineSymbol.createSimple(
                {"color": "0,70,200,200", "width": "0.9", "line_style": "dash"}
            )
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))

            pal = QgsPalLayerSettings()
            pal.isExpression = True
            pal.fieldName = "round(\"kcal\", 0) || ' kcal'"
            pal.placement = QgsPalLayerSettings.Curved

            fmt = QgsTextFormat()
            fmt.setSize(10.0)
            fmt.setColor(QColor(10, 10, 10))

            buf = QgsTextBufferSettings()
            buf.setEnabled(True)
            buf.setColor(QColor(255, 255, 255, 220))
            buf.setSize(1.2)
            fmt.setBuffer(buf)
            pal.setFormat(fmt)

            layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
            layer.setLabelsEnabled(True)
            layer.triggerRepaint()
        except Exception as e:
            log_message(f"Iso-energy style error: {e}", level=Qgis.Warning)

    def _apply_corridor_raster_style(self, layer: QgsRasterLayer):
        try:
            nodata_value = 0.0
            layer.dataProvider().setNoDataValue(1, nodata_value)

            shader = QgsRasterShader()
            ramp = QgsColorRampShader()
            ramp.setColorRampType(QgsColorRampShader.Discrete)
            items = [
                QgsColorRampShader.ColorRampItem(nodata_value, QColor(0, 0, 0, 0), "Outside"),
                QgsColorRampShader.ColorRampItem(1.0, QColor(0, 170, 255, 210), "Corridor"),
            ]
            ramp.setColorRampItemList(items)
            shader.setRasterShaderFunction(ramp)

            renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
            layer.setRenderer(renderer)
            layer.setOpacity(0.65)
            layer.triggerRepaint()
        except Exception as e:
            log_message(f"Corridor raster style error: {e}", level=Qgis.Warning)

    def _apply_corridor_polygon_style(self, layer: QgsVectorLayer):
        try:
            symbol = QgsFillSymbol.createSimple(
                {
                    "color": "0,0,0,0",
                    "outline_color": "0,170,255,220",
                    "outline_width": "0.9",
                }
            )
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            layer.triggerRepaint()
        except Exception as e:
            log_message(f"Corridor polygon style error: {e}", level=Qgis.Warning)

    def _apply_cost_raster_style(self, layer: QgsRasterLayer, vmin, vmax):
        try:
            nodata_value = -9999.0
            layer.dataProvider().setNoDataValue(1, nodata_value)

            if vmin is None or vmax is None or not math.isfinite(vmin) or not math.isfinite(vmax):
                vmin = 0.0
                vmax = 1.0
            vmin = float(vmin)
            vmax = float(vmax)
            if vmax <= 0:
                vmax = 1.0
            if vmin < 0:
                vmin = 0.0

            def fmt_minutes(m):
                m = float(m)
                if m < 1.0:
                    return f"{m*60.0:.0f}s"
                if m < 120.0:
                    return f"{m:.0f}min"
                return f"{m/60.0:.1f}h"

            # Legend ticks in minutes (cost raster is stored in minutes)
            ticks = [0.0, vmax * 0.25, vmax * 0.5, vmax * 0.75, vmax]
            # ensure strictly increasing unique ticks
            uniq = []
            for t in ticks:
                t = float(t)
                if not uniq or t > uniq[-1] + 1e-9:
                    uniq.append(t)
            ticks = uniq

            shader = QgsRasterShader()
            ramp = QgsColorRampShader()
            ramp.setColorRampType(QgsColorRampShader.Interpolated)

            colors = [
                QColor("#2c7bb6"),
                QColor("#abd9e9"),
                QColor("#ffffbf"),
                QColor("#fdae61"),
                QColor("#d7191c"),
            ]
            # Match color list length to ticks (keep endpoints stable)
            if len(ticks) <= 2:
                ticks = [0.0, vmax]
                colors = [QColor("#2c7bb6"), QColor("#d7191c")]
            else:
                # truncate/extend colors to tick count
                if len(colors) > len(ticks):
                    colors = colors[: len(ticks)]
                elif len(colors) < len(ticks):
                    colors = (colors + [colors[-1]] * len(ticks))[: len(ticks)]

            items = [QgsColorRampShader.ColorRampItem(nodata_value, QColor(0, 0, 0, 0), "NoData")]
            for i, t in enumerate(ticks):
                label = fmt_minutes(t)
                if i == 0:
                    label = f"{label} (출발점)"
                items.append(QgsColorRampShader.ColorRampItem(float(t), colors[i], label))
            ramp.setColorRampItemList(items)
            shader.setRasterShaderFunction(ramp)

            renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
            try:
                renderer.setClassificationMin(float(vmin))
                renderer.setClassificationMax(float(vmax))
            except Exception:
                pass
            layer.setRenderer(renderer)
            layer.setOpacity(0.7)
            layer.triggerRepaint()
        except Exception as e:
            log_message(f"Cost raster style error: {e}", level=Qgis.Warning)

    def _apply_energy_raster_style(self, layer: QgsRasterLayer, vmin, vmax):
        try:
            nodata_value = -9999.0
            layer.dataProvider().setNoDataValue(1, nodata_value)

            if vmin is None or vmax is None or not math.isfinite(vmin) or not math.isfinite(vmax):
                vmin = 0.0
                vmax = 1.0
            vmin = float(vmin)
            vmax = float(vmax)
            if vmax <= 0:
                vmax = 1.0
            if vmin < 0:
                vmin = 0.0

            def fmt_kcal(v):
                v = float(v)
                if v < 1.0:
                    return f"{v:.2f}kcal"
                return f"{v:.0f}kcal"

            ticks = [0.0, vmax * 0.25, vmax * 0.5, vmax * 0.75, vmax]
            uniq = []
            for t in ticks:
                t = float(t)
                if not uniq or t > uniq[-1] + 1e-9:
                    uniq.append(t)
            ticks = uniq

            shader = QgsRasterShader()
            ramp = QgsColorRampShader()
            ramp.setColorRampType(QgsColorRampShader.Interpolated)

            colors = [
                QColor("#2c7bb6"),
                QColor("#abd9e9"),
                QColor("#ffffbf"),
                QColor("#fdae61"),
                QColor("#d7191c"),
            ]
            if len(ticks) <= 2:
                ticks = [0.0, vmax]
                colors = [QColor("#2c7bb6"), QColor("#d7191c")]
            else:
                if len(colors) > len(ticks):
                    colors = colors[: len(ticks)]
                elif len(colors) < len(ticks):
                    colors = (colors + [colors[-1]] * len(ticks))[: len(ticks)]

            items = [
                QgsColorRampShader.ColorRampItem(nodata_value, QColor(0, 0, 0, 0), "NoData")
            ]
            for i, t in enumerate(ticks):
                label = fmt_kcal(t)
                if i == 0:
                    label = f"{label} (출발점)"
                items.append(QgsColorRampShader.ColorRampItem(float(t), colors[i], label))
            ramp.setColorRampItemList(items)
            shader.setRasterShaderFunction(ramp)

            renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
            try:
                renderer.setClassificationMin(float(vmin))
                renderer.setClassificationMax(float(vmax))
            except Exception:
                pass
            layer.setRenderer(renderer)
            layer.setOpacity(0.7)
            layer.triggerRepaint()
        except Exception as e:
            log_message(f"Energy raster style error: {e}", level=Qgis.Warning)

    def _create_lcp_milestones_layer(
        self,
        *,
        dem_source: str,
        crs_authid: str,
        model_key: str,
        model_params: dict,
        path_coords: list,
        interval_m: float,
        layer_name: str,
    ):
        if not dem_source or not os.path.exists(str(dem_source)):
            return None
        if not path_coords or len(path_coords) < 2:
            return None
        interval_m = float(interval_m)
        if interval_m <= 0:
            return None

        ds = gdal.Open(str(dem_source), gdal.GA_ReadOnly)
        if ds is None:
            return None
        band = ds.GetRasterBand(1)
        gt = ds.GetGeoTransform()
        nodata = band.GetNoDataValue()
        dx = abs(float(gt[1]))
        dy = abs(float(gt[5]))
        step_m = max(0.1, min(dx, dy))

        def densify_line(coords, step):
            out = [coords[0]]
            for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                seg_len = math.hypot(float(x1) - float(x0), float(y1) - float(y0))
                if seg_len <= 0:
                    continue
                n = max(1, int(math.ceil(seg_len / float(step))))
                for i in range(1, n + 1):
                    t = float(i) / float(n)
                    out.append(((x0 * (1.0 - t)) + (x1 * t), (y0 * (1.0 - t)) + (y1 * t)))
            return out

        coords_dense = densify_line(path_coords, step_m)
        minx = min(float(x) for x, _y in coords_dense)
        maxx = max(float(x) for x, _y in coords_dense)
        miny = min(float(y) for _x, y in coords_dense)
        maxy = max(float(y) for _x, y in coords_dense)
        inv = _inv_geotransform(gt)
        px0, py0 = gdal.ApplyGeoTransform(inv, minx, maxy)
        px1, py1 = gdal.ApplyGeoTransform(inv, maxx, miny)
        x0 = int(math.floor(min(px0, px1))) - 2
        x1 = int(math.ceil(max(px0, px1))) + 2
        y0 = int(math.floor(min(py0, py1))) - 2
        y1 = int(math.ceil(max(py0, py1))) + 2
        x0 = _clamp_int(x0, 0, ds.RasterXSize - 1)
        y0 = _clamp_int(y0, 0, ds.RasterYSize - 1)
        x1 = _clamp_int(x1, 0, ds.RasterXSize - 1)
        y1 = _clamp_int(y1, 0, ds.RasterYSize - 1)
        win_xsize = max(1, x1 - x0 + 1)
        win_ysize = max(1, y1 - y0 + 1)
        dem = band.ReadAsArray(x0, y0, win_xsize, win_ysize).astype(np.float32, copy=False)
        nodata_mask = np.zeros(dem.shape, dtype=bool)
        if nodata is not None:
            nodata_mask |= dem == nodata
        nodata_mask |= np.isnan(dem)
        win_gt = _window_geotransform(gt, x0, y0)
        inv_win_gt = _inv_geotransform(win_gt)
        ds = None

        profile = []
        dist = 0.0
        cum_time_s = 0.0
        cum_energy_j = 0.0
        z_prev = None
        x_prev = None
        y_prev = None
        for (x, y) in coords_dense:
            z = _bilinear_elevation(dem, nodata_mask, inv_win_gt, float(x), float(y))
            if z is None:
                continue
            if x_prev is not None:
                horiz = math.hypot(float(x) - float(x_prev), float(y) - float(y_prev))
                dz = float(z) - float(z_prev)
                dist += horiz
                cum_time_s += _edge_cost(model_key, horiz, dz, model_params, cost_mode="time_s")
                if model_key == MODEL_PANDOLF:
                    cum_energy_j += _edge_cost(model_key, horiz, dz, model_params, cost_mode="energy_j")
            profile.append((float(dist), float(x), float(y), float(cum_time_s) / 60.0, (float(cum_energy_j) / 4184.0) if model_key == MODEL_PANDOLF else None))
            x_prev, y_prev, z_prev = float(x), float(y), float(z)

        if not profile:
            return None

        total_d = profile[-1][0]
        if not math.isfinite(total_d) or total_d <= interval_m:
            return None

        layer = QgsVectorLayer(f"Point?crs={crs_authid}", layer_name, "memory")
        pr = layer.dataProvider()
        pr.addAttributes(
            [
                QgsField("dist_m", QVariant.Double),
                QgsField("time_min", QVariant.Double),
                QgsField("energy_kcal", QVariant.Double),
                QgsField("label", QVariant.String),
            ]
        )
        layer.updateFields()

        feats = []
        n = int(math.floor(total_d / interval_m))
        for i in range(1, n + 1):
            target_d = float(i) * float(interval_m)
            nearest = min(profile, key=lambda p: abs(float(p[0]) - target_d))
            d_m, x, y, t_min, e_kcal = nearest
            parts = [f"{d_m/1000.0:.1f}km", f"{t_min:.1f}분"]
            if e_kcal is not None:
                parts.append(f"{e_kcal:.0f}kcal")
            label = " / ".join(parts)

            f = QgsFeature(layer.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y))))
            f.setAttributes([float(d_m), float(t_min), float(e_kcal) if e_kcal is not None else None, label])
            feats.append(f)

        pr.addFeatures(feats)
        layer.updateExtents()

        symbol = QgsMarkerSymbol.createSimple(
            {"name": "circle", "color": "0,0,0,0", "outline_color": "0,0,0,200", "size": "2.0", "outline_width": "0.4"}
        )
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        pal = QgsPalLayerSettings()
        pal.fieldName = "label"
        pal.placement = QgsPalLayerSettings.AroundPoint
        fmt = QgsTextFormat()
        fmt.setSize(9.5)
        fmt.setColor(QColor(10, 10, 10))
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setColor(QColor(255, 255, 255, 220))
        buf.setSize(1.2)
        fmt.setBuffer(buf)
        pal.setFormat(fmt)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        layer.setLabelsEnabled(True)
        layer.triggerRepaint()

        return layer

    def _on_path_layer_selection_changed(self, layer_id: str):
        try:
            layer = QgsProject.instance().mapLayer(layer_id)
            if not layer or layer.selectedFeatureCount() <= 0:
                return
            self.open_cost_profile(layer_id)
        except Exception as e:
            log_message(f"Cost profile selection handler error: {e}", level=Qgis.Warning)

    def open_cost_profile(self, layer_id: str):
        payload = self._profile_payloads.get(layer_id)
        if not payload:
            return

        if layer_id in self._profile_dialogs:
            dlg = self._profile_dialogs.get(layer_id)
            if dlg:
                try:
                    dlg.show()
                    dlg.raise_()
                    dlg.activateWindow()
                except Exception:
                    pass
                return

        dem_source = payload.get("dem_source")
        start_xy = payload.get("start_xy")
        end_xy = payload.get("end_xy")
        lcp_coords = payload.get("path_coords") or []
        model_key = payload.get("model_key")
        model_params = payload.get("model_params") or {}
        model_label = payload.get("model_label") or ""

        if not dem_source or not os.path.exists(str(dem_source)):
            push_message(self.iface, "오류", "프로파일을 위해 DEM 소스를 찾을 수 없습니다.", level=2, duration=6)
            return
        if not start_xy or not end_xy:
            return

        try:
            ds = gdal.Open(str(dem_source), gdal.GA_ReadOnly)
            if ds is None:
                raise Exception("GDAL open failed")
            band = ds.GetRasterBand(1)
            gt = ds.GetGeoTransform()
            nodata = band.GetNoDataValue()
            dx = abs(float(gt[1]))
            dy = abs(float(gt[5]))
            step_m = max(0.1, min(dx, dy))

            def densify_line(coords, step):
                if not coords or len(coords) < 2:
                    return coords
                out = [coords[0]]
                for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                    seg_len = math.hypot(float(x1) - float(x0), float(y1) - float(y0))
                    if seg_len <= 0:
                        continue
                    n = max(1, int(math.ceil(seg_len / float(step))))
                    for i in range(1, n + 1):
                        t = float(i) / float(n)
                        out.append(((x0 * (1.0 - t)) + (x1 * t), (y0 * (1.0 - t)) + (y1 * t)))
                return out

            straight_coords = densify_line([start_xy, end_xy], step_m)
            lcp_coords_dense = densify_line(lcp_coords, step_m) if lcp_coords else []

            # Read minimal DEM window for both paths
            all_pts = straight_coords + lcp_coords_dense
            minx = min(float(x) for x, _y in all_pts)
            maxx = max(float(x) for x, _y in all_pts)
            miny = min(float(y) for _x, y in all_pts)
            maxy = max(float(y) for _x, y in all_pts)
            inv = _inv_geotransform(gt)
            px0, py0 = gdal.ApplyGeoTransform(inv, minx, maxy)
            px1, py1 = gdal.ApplyGeoTransform(inv, maxx, miny)
            x0 = int(math.floor(min(px0, px1))) - 2
            x1 = int(math.ceil(max(px0, px1))) + 2
            y0 = int(math.floor(min(py0, py1))) - 2
            y1 = int(math.ceil(max(py0, py1))) + 2
            x0 = _clamp_int(x0, 0, ds.RasterXSize - 1)
            y0 = _clamp_int(y0, 0, ds.RasterYSize - 1)
            x1 = _clamp_int(x1, 0, ds.RasterXSize - 1)
            y1 = _clamp_int(y1, 0, ds.RasterYSize - 1)
            win_xsize = max(1, x1 - x0 + 1)
            win_ysize = max(1, y1 - y0 + 1)
            dem = band.ReadAsArray(x0, y0, win_xsize, win_ysize).astype(np.float32, copy=False)
            nodata_mask = np.zeros(dem.shape, dtype=bool)
            if nodata is not None:
                nodata_mask |= dem == nodata
            nodata_mask |= np.isnan(dem)
            win_gt = _window_geotransform(gt, x0, y0)
            inv_win_gt = _inv_geotransform(win_gt)

            def sample_profile(coords):
                pts = []
                dist = 0.0
                cum_time_s = 0.0
                cum_energy_j = 0.0
                z_prev = None
                x_prev = None
                y_prev = None
                for (x, y) in coords:
                    z = _bilinear_elevation(dem, nodata_mask, inv_win_gt, float(x), float(y))
                    if z is None:
                        continue
                    if x_prev is not None:
                        horiz = math.hypot(float(x) - float(x_prev), float(y) - float(y_prev))
                        dz = float(z) - float(z_prev)
                        dist += horiz
                        if model_key:
                            cum_time_s += _edge_cost(model_key, horiz, dz, model_params, cost_mode="time_s")
                            if model_key == MODEL_PANDOLF:
                                cum_energy_j += _edge_cost(model_key, horiz, dz, model_params, cost_mode="energy_j")
                    pts.append(
                        (
                            float(dist),
                            float(z),
                            float(cum_time_s) / 60.0,
                            (float(cum_energy_j) / 4184.0) if model_key == MODEL_PANDOLF else None,
                        )
                    )
                    x_prev, y_prev, z_prev = float(x), float(y), float(z)
                return pts

            straight_pts = sample_profile(straight_coords)
            lcp_pts = sample_profile(lcp_coords_dense) if lcp_coords_dense else []
            ds = None

        except Exception as e:
            push_message(self.iface, "오류", f"프로파일 계산 실패: {e}", level=2, duration=7)
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"최소비용경로 프로파일 (LCP Profile) - {model_label}".strip())
        dlg.setModal(False)
        try:
            dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        except Exception:
            pass
        layout = QtWidgets.QVBoxLayout(dlg)

        summary_parts = []
        if lcp_pts:
            total_d = lcp_pts[-1][0] / 1000.0
            total_t = lcp_pts[-1][2]
            summary_parts.append(f"LCP {total_d:.2f}km / {total_t:.1f}분")
            if lcp_pts[-1][3] is not None:
                summary_parts.append(f"{lcp_pts[-1][3]:.0f}kcal")
        if straight_pts:
            sd = straight_pts[-1][0] / 1000.0
            st = straight_pts[-1][2]
            summary_parts.append(f"직선 {sd:.2f}km / {st:.1f}분")
            if straight_pts[-1][3] is not None:
                summary_parts.append(f"{straight_pts[-1][3]:.0f}kcal")
        lbl = QtWidgets.QLabel(" | ".join(summary_parts))
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        elev_chart = MultiLineChartWidget()
        elev_chart.set_series(
            title="고도 (Elevation)",
            unit="m",
            series=[
                {"name": "LCP", "color": QColor(0, 160, 0, 220), "dash": False, "points": [(d, z) for d, z, _t, _e in lcp_pts]},
                {"name": "직선", "color": QColor(90, 90, 90, 220), "dash": True, "points": [(d, z) for d, z, _t, _e in straight_pts]},
            ],
        )
        layout.addWidget(elev_chart)

        time_chart = MultiLineChartWidget()
        time_chart.set_series(
            title="누적 시간 (Cumulative Time)",
            unit="분",
            series=[
                {"name": "LCP", "color": QColor(0, 120, 255, 220), "dash": False, "points": [(d, t) for d, _z, t, _e in lcp_pts]},
                {"name": "직선", "color": QColor(90, 90, 90, 220), "dash": True, "points": [(d, t) for d, _z, t, _e in straight_pts]},
            ],
        )
        layout.addWidget(time_chart)

        if model_key == MODEL_PANDOLF:
            energy_chart = MultiLineChartWidget()
            energy_chart.set_series(
                title="누적 에너지 (Cumulative Energy)",
                unit="kcal",
                series=[
                    {"name": "LCP", "color": QColor(120, 0, 200, 220), "dash": False, "points": [(d, e) for d, _z, _t, e in lcp_pts if e is not None]},
                    {"name": "직선", "color": QColor(90, 90, 90, 220), "dash": True, "points": [(d, e) for d, _z, _t, e in straight_pts if e is not None]},
                ],
            )
            layout.addWidget(energy_chart)

        # Map synchronization: hover over profile → show position marker on map (along LCP when available).
        rb = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        rb.setColor(QColor(0, 120, 255, 220))
        rb.setWidth(4)
        rb.setIcon(QgsRubberBand.ICON_CIRCLE)
        rb.setIconSize(10)
        rb.hide()

        def point_at_distance(coords, dist_m):
            if not coords or len(coords) < 2:
                return None
            remaining = float(dist_m)
            for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                seg = math.hypot(float(x1) - float(x0), float(y1) - float(y0))
                if seg <= 0:
                    continue
                if remaining <= seg:
                    t = remaining / seg
                    return (float(x0) * (1.0 - t) + float(x1) * t, float(y0) * (1.0 - t) + float(y1) * t)
                remaining -= seg
            return coords[-1]

        base_coords = lcp_coords_dense if lcp_coords_dense else straight_coords

        def on_hover(d):
            try:
                if d is None:
                    rb.hide()
                    rb.reset(QgsWkbTypes.PointGeometry)
                    return
                pt = point_at_distance(base_coords, float(d))
                if not pt:
                    return
                rb.reset(QgsWkbTypes.PointGeometry)
                rb.addPoint(QgsPointXY(pt[0], pt[1]))
                rb.show()
            except Exception:
                pass

        elev_chart.on_hover_distance = on_hover
        time_chart.on_hover_distance = on_hover
        if model_key == MODEL_PANDOLF:
            energy_chart.on_hover_distance = on_hover

        btn_row = QtWidgets.QHBoxLayout()
        btn_close = QtWidgets.QPushButton("닫기")
        btn_close.clicked.connect(dlg.close)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        self._profile_dialogs[layer_id] = dlg
        try:
            dlg.destroyed.connect(lambda *_a, lid=layer_id: self._profile_dialogs.pop(lid, None))
        except Exception:
            pass
        try:
            dlg.destroyed.connect(lambda *_a: on_hover(None))
            dlg.destroyed.connect(
                lambda *_a: (self.canvas.scene().removeItem(rb) if self.canvas and self.canvas.scene() else None)
            )
        except Exception:
            pass
        dlg.resize(820, 720 if model_key == MODEL_PANDOLF else 560)
        dlg.show()

    def reject(self):
        self._cleanup_for_close()
        super().reject()

    def closeEvent(self, event):
        self._cleanup_for_close()
        event.accept()

    def _cleanup_for_close(self):
        """Cleanup when the dialog closes (keep project signals for later layer/temp cleanup)."""
        try:
            if self._task_running and self._task is not None:
                try:
                    self._task.cancel()
                except Exception:
                    pass
            self._task_running = False
            self._task = None
            self._reset_preview()

            try:
                if self.map_tool and hasattr(self.map_tool, "snap_indicator"):
                    try:
                        self.map_tool.snap_indicator.setMatch(QgsPointLocator.Match())
                    except Exception:
                        pass
            except Exception:
                pass

            if self.original_tool:
                self.canvas.setMapTool(self.original_tool)
        except Exception:
            pass

    def _cleanup_for_unload(self):
        """Full cleanup for plugin unload/reload (disconnect signals, release handlers, clear temp tracking)."""
        try:
            if self._task_running and self._task is not None:
                try:
                    self._task.cancel()
                except Exception:
                    pass
            self._task_running = False
            self._task = None
        except Exception:
            pass

        try:
            self._reset_preview()
        except Exception:
            pass

        # Disconnect selection handlers for profile reopen to avoid stale callbacks after reload.
        try:
            for lid, handler in list(self._profile_selection_handlers.items()):
                try:
                    layer = QgsProject.instance().mapLayer(lid)
                    if layer and handler:
                        layer.selectionChanged.disconnect(handler)
                except Exception:
                    pass
            self._profile_selection_handlers.clear()
        except Exception:
            pass

        # Close any open profile dialogs (best-effort).
        try:
            for _lid, dlg in list(self._profile_dialogs.items()):
                try:
                    dlg.close()
                    try:
                        dlg.deleteLater()
                    except Exception:
                        pass
                except Exception:
                    pass
            self._profile_dialogs.clear()
            self._profile_payloads.clear()
        except Exception:
            pass

        try:
            if self.original_tool:
                self.canvas.setMapTool(self.original_tool)
        except Exception:
            pass

        try:
            QgsProject.instance().layersWillBeRemoved.disconnect(self._on_project_layers_removed)
        except Exception:
            pass

        try:
            self._layer_temp_outputs.clear()
        except Exception:
            pass


class CostPathPointTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, dialog: CostSurfaceDialog):
        super().__init__(canvas)
        self.dialog = dialog
        self.snap_indicator = QgsSnapIndicator(canvas)
        self._has_start = False

    def activate(self):
        super().activate()
        try:
            needs_end = bool(self.dialog._is_path_required())
            # If LCP is enabled and start is already set, allow selecting only the end point.
            self._has_start = needs_end and self.dialog._start_canvas is not None and self.dialog._end_canvas is None
        except Exception:
            self._has_start = False

        if self._has_start:
            push_message(self.dialog.iface, "비용표면/최소비용경로", "도착점을 클릭하세요. (우클릭/ESC 종료)", level=0, duration=4)
        else:
            push_message(self.dialog.iface, "비용표면/최소비용경로", "시작점을 클릭하세요. (우클릭/ESC 종료)", level=0, duration=4)

    def canvasMoveEvent(self, event):
        res = self.canvas().snappingUtils().snapToMap(event.pos())
        if res.isValid():
            self.snap_indicator.setMatch(res)
        else:
            self.snap_indicator.setMatch(QgsPointLocator.Match())

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self.finish_selection()
            return

        res = self.canvas().snappingUtils().snapToMap(event.pos())
        point = res.point() if res.isValid() else self.toMapCoordinates(event.pos())

        if not self._has_start:
            self._has_start = True
            self.dialog.set_start_point(point)
            if not self.dialog._is_path_required():
                self.finish_selection()
                return
            push_message(self.dialog.iface, "비용표면/최소비용경로", "도착점을 클릭하세요. (또는 우클릭/ESC로 종료)", level=0, duration=4)
            return

        self.dialog.set_end_point(point)
        self.finish_selection()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.finish_selection()

    def finish_selection(self):
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        self._has_start = False
        self.dialog.finish_map_selection()

    def deactivate(self):
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()
