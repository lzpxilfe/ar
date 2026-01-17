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
import tempfile
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from osgeo import gdal, ogr, osr

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsColorRampShader,
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsLineSymbol,
    QgsMapLayerProxyModel,
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

from .utils import is_metric_crs, log_message, push_message, restore_ui_focus, transform_point


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "cost_surface_dialog_base.ui")
)


MODEL_TOBLER = "tobler_time"
MODEL_NAISMITH = "naismith_time"
MODEL_HERZOG_METABOLIC = "herzog_metabolic_time"
MODEL_CONOLLY_LAKE = "conolly_lake_time"
MODEL_HERZOG_WHEELED = "herzog_wheeled_time"


@dataclass
class CostTaskResult:
    ok: bool
    message: str = ""
    cost_raster_path: Optional[str] = None
    cost_min: Optional[float] = None
    cost_max: Optional[float] = None
    path_coords: Optional[List[Tuple[float, float]]] = None  # in DEM CRS
    start_xy: Optional[Tuple[float, float]] = None  # in DEM CRS
    end_xy: Optional[Tuple[float, float]] = None  # in DEM CRS
    dem_authid: Optional[str] = None
    model_label: Optional[str] = None
    total_cost_s: Optional[float] = None
    straight_time_s: Optional[float] = None
    straight_dist_m: Optional[float] = None
    lcp_dist_m: Optional[float] = None
    isochrones_vector_path: Optional[str] = None


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


def _estimate_straight_line_time_s(
    model_key,
    model_params,
    start_xy,
    end_xy,
    dem,
    nodata_mask,
    win_gt,
    step_m,
):
    """Estimate travel time along the straight line segment by DEM sampling."""
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

    total_s = 0.0
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
        total_s += _edge_cost(model_key, horiz, dz, model_params)
        x_prev, y_prev, z_prev = float(x), float(y), float(z)

    return total_s, straight_dist


def _polyline_length(coords):
    if not coords or len(coords) < 2:
        return 0.0
    total = 0.0
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        total += math.hypot(float(x1) - float(x0), float(y1) - float(y0))
    return total


def _create_isochrones_gpkg(cost_raster_path, output_gpkg_path, levels_minutes, nodata_value=-9999.0):
    """Create an isochrone contour GeoPackage from the cost raster (values in minutes)."""
    if not cost_raster_path or not os.path.exists(cost_raster_path):
        return None

    try:
        ds = gdal.Open(cost_raster_path, gdal.GA_ReadOnly)
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

        layer = vds.CreateLayer("isochrones", srs, ogr.wkbLineString)
        if layer is None:
            vds = None
            return None

        layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
        layer.CreateField(ogr.FieldDefn("minutes", ogr.OFTReal))

        # Create only fixed contour levels (minutes).
        levels = [float(v) for v in levels_minutes]
        try:
            # Newer GDAL python bindings accept fixedLevels list directly.
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
            # Fallback signature: fixedLevelCount + fixedLevels
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


def _edge_cost(model_key, horiz_m, dz_m, model_params):
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
    cancel_check=None,
):
    rows, cols = dem.shape
    sr, sc = start_rc
    er, ec = end_rc
    start_idx = sr * cols + sc
    end_idx = er * cols + ec

    gscore = np.full(rows * cols, np.inf, dtype=np.float64)
    prev = np.full(rows * cols, -1, dtype=np.int32)
    gscore[start_idx] = 0.0

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
    else:
        vmax = float(model_params.get("naismith_horizontal_kmh", 5.0)) * 1000.0 / 3600.0
    vmax = max(0.05, vmax)

    def hfun(r, c):
        return math.hypot((ec - c) * dx, (er - r) * dy) / vmax

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
            w = _edge_cost(model_key, horiz, dz, model_params)

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
    cancel_check=None,
    progress_cb=None,
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
            w = _edge_cost(model_key, horiz, dz, model_params)
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
        create_path,
        on_done,
    ):
        super().__init__("비용표면/최소비용경로 (Cost Surface / LCP)", QgsTask.CanCancel)
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
        self.create_path = bool(create_path)
        self.on_done = on_done
        self.result_obj = CostTaskResult(ok=False)

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
            return self.isCanceled()

        def progress_cb(p):
            try:
                self.setProgress(float(p))
            except Exception:
                pass

        dist = None
        prev = None
        end_cost = None

        if self.create_cost_raster:
            dist, prev = _dijkstra_full(
                dem,
                nodata_mask,
                start_rc,
                dx,
                dy,
                self.allow_diagonal,
                self.model_key,
                self.model_params,
                cancel_check=cancel_check,
                progress_cb=progress_cb,
            )
            if dist is None or prev is None:
                return CostTaskResult(ok=False, message="작업이 취소되었습니다.")
            if has_end:
                end_cost = float(dist[end_rc[0] * cols + end_rc[1]])
        else:
            if not has_end:
                return CostTaskResult(ok=False, message="최소비용경로를 생성하려면 도착점이 필요합니다.")
            prev, end_cost = _astar_path(
                dem,
                nodata_mask,
                start_rc,
                end_rc,
                dx,
                dy,
                self.allow_diagonal,
                self.model_key,
                self.model_params,
                cancel_check=cancel_check,
            )
            if prev is None:
                return CostTaskResult(ok=False, message="작업이 취소되었습니다.")

        win_gt = _window_geotransform(gt, xoff, yoff)

        cost_raster_path = None
        cost_min = None
        cost_max = None
        isochrones_vector_path = None
        if self.create_cost_raster and dist is not None:
            dist2d_s = dist.reshape((rows, cols))
            valid = np.isfinite(dist2d_s) & (~nodata_mask)
            if np.any(valid):
                dist2d_min = dist2d_s[valid] / 60.0
                cost_min = float(np.nanmin(dist2d_min))
                cost_max = float(np.nanmax(dist2d_min))

            out = np.full(dist2d_s.shape, -9999.0, dtype=np.float32)
            out[valid] = (dist2d_s[valid] / 60.0).astype(np.float32, copy=False)

            run_id = uuid.uuid4().hex[:8]
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

            # Isochrones (0/15/30/45/60 minutes) for easier interpretation.
            try:
                if cost_max is not None and math.isfinite(cost_max):
                    levels = [0.0, 15.0, 30.0, 45.0, 60.0]
                    usable = [v for v in levels if float(v) <= float(cost_max) + 1e-6]
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

        path_coords = None
        if self.create_path and prev is not None and has_end:
            path_idx = _reconstruct_path(prev, start_rc, end_rc, cols, rows)
            if path_idx:
                coords = []
                for idx in path_idx:
                    r = idx // cols
                    c = idx % cols
                    coords.append(_cell_center(win_gt, c, r))
                path_coords = coords
        lcp_dist_m = _polyline_length(path_coords) if path_coords else None

        straight_time_s = None
        straight_dist_m = None
        if has_end:
            straight_time_s, straight_dist_m = _estimate_straight_line_time_s(
                self.model_key,
                self.model_params,
                (float(sx), float(sy)),
                (float(ex), float(ey)),
                dem,
                nodata_mask,
                win_gt,
                step_m=min(dx, dy),
            )

        msg = "완료"
        if self.create_path:
            if end_cost is None or not math.isfinite(end_cost):
                msg = "도착점까지 경로를 찾지 못했습니다."
                end_cost = None

        return CostTaskResult(
            ok=True,
            message=msg,
            cost_raster_path=cost_raster_path,
            cost_min=cost_min,
            cost_max=cost_max,
            path_coords=path_coords,
            start_xy=(float(sx), float(sy)),
            end_xy=(float(ex), float(ey)) if has_end else None,
            dem_authid=self.dem_authid,
            model_label=self.model_label,
            total_cost_s=end_cost,
            straight_time_s=straight_time_s,
            straight_dist_m=straight_dist_m,
            lcp_dist_m=lcp_dist_m,
            isochrones_vector_path=isochrones_vector_path,
        )


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

        # Ensure no lingering preview graphics on startup
        self._reset_preview()

        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self._init_models()
        self.cmbModel.currentIndexChanged.connect(self._on_model_changed)
        self._on_model_changed()

        self.btnPickPoints.clicked.connect(self.pick_points_on_map)
        self.btnClearPoints.clicked.connect(self.clear_points)
        self.btnRun.clicked.connect(self.run_analysis)
        self.btnClose.clicked.connect(self.reject)
        self.chkCreatePath.toggled.connect(self._on_create_path_toggled)

        self.cmbDemLayer.layerChanged.connect(self._on_dem_changed)
        self._on_dem_changed()
        self._update_labels()
        self._update_point_help()
        self._on_create_path_toggled(bool(self.chkCreatePath.isChecked()))

    def _init_models(self):
        self.cmbModel.clear()
        self.cmbModel.addItem("토블러 보행함수 (Tobler Hiking Function)", MODEL_TOBLER)
        self.cmbModel.addItem("나이스미스 규칙 (Naismith's Rule)", MODEL_NAISMITH)
        self.cmbModel.addItem("허조그 메타볼릭 (Herzog metabolic, via Čučković)", MODEL_HERZOG_METABOLIC)
        self.cmbModel.addItem("코놀리&레이크 경사비용 (Conolly & Lake, 2006)", MODEL_CONOLLY_LAKE)
        self.cmbModel.addItem("허조그 차량/수레 (Herzog wheeled vehicle, via Čučković)", MODEL_HERZOG_WHEELED)

    def _is_path_required(self):
        try:
            return bool(self.chkCreatePath.isChecked())
        except Exception:
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
            if not checked:
                self._end_canvas = None
                self._update_preview()
                self._update_labels()
            self._update_point_help()
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
                    "<br><b>참고</b>: 수식은 Zoran Čučković의 QGIS 'Movement Analysis' 플러그인(slope_cost) 구현을 참고했습니다.<br>"
                    "<br><b>누적 비용</b>: 출발점→각 셀 최소 이동시간(분)"
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
        create_path = bool(self.chkCreatePath.isChecked())
        if not create_cost_raster and not create_path:
            push_message(self.iface, "오류", "최소 1개 출력(누적 비용/경로)을 선택하세요.", level=2)
            restore_ui_focus(self)
            return
        if create_path and not self._end_canvas:
            push_message(self.iface, "오류", "최소비용경로를 생성하려면 도착점이 필요합니다.", level=2)
            restore_ui_focus(self)
            return

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
            create_path=create_path,
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
        if res.total_cost_s is not None and math.isfinite(res.total_cost_s):
            summary = f"{summary} | LCP {res.total_cost_s/60.0:.1f}분"

        if (
            res.straight_time_s is not None
            and math.isfinite(res.straight_time_s)
            and res.total_cost_s is not None
            and math.isfinite(res.total_cost_s)
        ):
            lcp_min = float(res.total_cost_s) / 60.0
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
        group_name = f"비용표면_{run_id}"
        run_group = parent_group.insertGroup(0, group_name)
        run_group.setExpanded(False)

        bottom_to_top = []

        if res.cost_raster_path:
            layer_name = f"누적 비용(분) (Cumulative Cost, min) - {(res.model_label or '').strip()}"
            cost_layer = QgsRasterLayer(res.cost_raster_path, layer_name)
            if cost_layer.isValid():
                self._apply_cost_raster_style(cost_layer, res.cost_min, res.cost_max)
                bottom_to_top.append(cost_layer)

        if res.isochrones_vector_path:
            iso_layer = QgsVectorLayer(
                f"{res.isochrones_vector_path}|layername=isochrones",
                "등시간선 (Isochrones)",
                "ogr",
            )
            if iso_layer.isValid():
                self._apply_isochrone_style(iso_layer)
                bottom_to_top.append(iso_layer)

        if res.start_xy and res.dem_authid:
            pt_layer = QgsVectorLayer(f"Point?crs={res.dem_authid}", "시작/도착점 (Start/End)", "memory")
            pr = pt_layer.dataProvider()
            pr.addAttributes([QgsField("role", QVariant.String)])
            pt_layer.updateFields()

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
            path_layer = QgsVectorLayer(
                f"LineString?crs={res.dem_authid}", "경로 비교 (Straight vs LCP)", "memory"
            )
            pr = path_layer.dataProvider()
            pr.addAttributes(
                [
                    QgsField("kind", QVariant.String),
                    QgsField("model", QVariant.String),
                    QgsField("dist_m", QVariant.Double),
                    QgsField("time_min", QVariant.Double),
                ]
            )
            path_layer.updateFields()

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
                        (float(res.total_cost_s) / 60.0) if res.total_cost_s is not None else None,
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
            bottom_to_top.append(path_layer)

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

    def _apply_isochrone_style(self, layer: QgsVectorLayer):
        try:
            symbol = QgsLineSymbol.createSimple(
                {"color": "20,20,20,200", "width": "0.9", "line_style": "dash"}
            )
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))

            pal = QgsPalLayerSettings()
            pal.isExpression = True
            pal.fieldName = "round(\"minutes\", 0) || '분'"
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

    def reject(self):
        self._cleanup()
        super().reject()

    def closeEvent(self, event):
        self._cleanup()
        event.accept()

    def _cleanup(self):
        try:
            if self._task_running and self._task is not None:
                try:
                    self._task.cancel()
                except Exception:
                    pass
            self._task_running = False
            self._task = None
            self._reset_preview()

            # Remove temporary rubber bands from canvas scene (prevents lingering graphics)
            try:
                if self.canvas and self.canvas.scene():
                    for rb in (self._rb_start, self._rb_end, self._rb_line):
                        try:
                            self.canvas.scene().removeItem(rb)
                        except Exception:
                            pass
            except Exception:
                pass

            if self.original_tool:
                self.canvas.setMapTool(self.original_tool)
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
