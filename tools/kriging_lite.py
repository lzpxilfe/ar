# -*- coding: utf-8 -*-
"""
Kriging (Lite) implementation for ArchToolkit.

- Goal: provide an Ordinary Kriging workflow without external providers
  (SAGA/GRASS) and without extra Python dependencies beyond numpy.
- Scope: "Lite" = automatic variogram parameters + local neighborhood kriging.

This is intentionally conservative and best-effort:
- Uses nearest N points per grid cell (fast enough for moderate grids).
- Writes prediction + variance GeoTIFF via GDAL Python bindings (usually present
  in QGIS' Python environment). If unavailable, the caller should fail
  gracefully.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .utils import is_metric_crs, log_message


@dataclass(frozen=True)
class KrigingParams:
    model: str
    nugget: float
    partial_sill: float
    range: float


def _as_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except Exception:
        return None


def _auto_value_field(layer: QgsVectorLayer) -> Optional[str]:
    """Pick a likely numeric field name (best-effort)."""
    if layer is None:
        return None

    candidates = [
        "Z_COORD",
        "z_coord",
        "Elevation",
        "ELEVATION",
        "elev",
        "ELEV",
        "height",
        "HEIGHT",
        "z",
        "Z",
    ]
    try:
        for name in candidates:
            idx = layer.fields().indexFromName(name)
            if idx >= 0:
                return name
    except Exception:
        pass

    # Fallback: first numeric field
    try:
        for f in layer.fields():
            try:
                if f.isNumeric():
                    return f.name()
            except Exception:
                continue
    except Exception:
        pass
    return None


def _collect_point_samples(
    layer: QgsVectorLayer,
    *,
    value_field: Optional[str],
    dedup_round: int = 6,
) -> Tuple[List[Tuple[float, float]], List[float], QgsSpatialIndex]:
    """Return unique point samples and a spatial index (IDs are 0..n-1)."""
    if layer is None or not layer.isValid():
        raise ValueError("Invalid layer")

    if layer.geometryType() != QgsWkbTypes.PointGeometry:
        raise ValueError("Kriging requires a point layer")

    field_name = (value_field or "").strip() or _auto_value_field(layer)

    sums: Dict[Tuple[float, float], float] = {}
    counts: Dict[Tuple[float, float], int] = {}
    coords: Dict[Tuple[float, float], Tuple[float, float]] = {}

    # Gather samples (deduplicate by rounded XY)
    for feat in layer.getFeatures():
        try:
            geom = feat.geometry()
        except Exception:
            geom = None
        if geom is None or geom.isEmpty():
            continue

        try:
            pt = geom.asPoint()
        except Exception:
            continue

        x = _as_float(getattr(pt, "x", lambda: None)())
        y = _as_float(getattr(pt, "y", lambda: None)())
        if x is None or y is None:
            continue

        z = None
        if field_name:
            try:
                z = _as_float(feat[field_name])
            except Exception:
                z = None
        if z is None:
            # Fallback: geometry Z (3D points)
            try:
                z = _as_float(pt.z())
            except Exception:
                z = None
        if z is None:
            continue

        key = (round(x, dedup_round), round(y, dedup_round))
        coords[key] = (x, y)
        sums[key] = float(sums.get(key, 0.0)) + float(z)
        counts[key] = int(counts.get(key, 0)) + 1

    points_xy: List[Tuple[float, float]] = []
    values: List[float] = []
    index = QgsSpatialIndex()

    for i, (key, sum_z) in enumerate(sums.items()):
        x, y = coords[key]
        z = float(sum_z) / max(1, int(counts.get(key, 1)))

        points_xy.append((float(x), float(y)))
        values.append(float(z))

        f = QgsFeature()
        f.setId(int(i))
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y))))
        index.addFeature(f)

    if len(points_xy) < 3:
        raise ValueError("Not enough valid points (need >= 3)")

    return points_xy, values, index


def _median_nearest_neighbor_distance(
    points_xy: Sequence[Tuple[float, float]],
    index: QgsSpatialIndex,
) -> Optional[float]:
    """Estimate typical spacing via median NN distance (best-effort)."""
    try:
        import numpy as np
    except Exception:
        return None

    dists = []
    for x, y in points_xy:
        try:
            ids = index.nearestNeighbor(QgsPointXY(float(x), float(y)), 2)
        except Exception:
            ids = []
        if not ids or len(ids) < 2:
            continue

        # The nearest is usually itself; take the second.
        j = int(ids[1])
        if j < 0 or j >= len(points_xy):
            continue
        x2, y2 = points_xy[j]
        dx = float(x2) - float(x)
        dy = float(y2) - float(y)
        d = math.hypot(dx, dy)
        if d > 0:
            dists.append(d)

    if not dists:
        return None
    try:
        return float(np.median(np.array(dists, dtype=float)))
    except Exception:
        return None


def auto_params(
    *,
    points_xy: Sequence[Tuple[float, float]],
    values: Sequence[float],
    extent: QgsRectangle,
    index: QgsSpatialIndex,
) -> KrigingParams:
    """Heuristic "good-enough" parameters for Lite mode."""
    try:
        import numpy as np
    except Exception as e:
        raise RuntimeError(f"numpy is required for Kriging Lite ({e})")

    v = np.array(list(values), dtype=float)
    if v.size < 3:
        raise ValueError("Not enough values")

    # Sample variance (ddof=1). If it collapses (all equal), keep a tiny sill.
    var = float(np.var(v, ddof=1)) if v.size >= 2 else 0.0
    if not (var > 0):
        var = 1e-6

    nn = _median_nearest_neighbor_distance(points_xy, index)
    if nn is None or not (nn > 0):
        # Fallback: approximate spacing from area/points
        try:
            area = float(max(0.0, extent.width() * extent.height()))
            nn = math.sqrt(area / max(1, len(points_xy)))
        except Exception:
            nn = 0.0
    if not (nn > 0):
        nn = 1.0

    # Exponential model tends to be stable for "Lite" defaults.
    # Range is set to a few typical spacings so correlation decays reasonably.
    rng = float(max(1e-6, nn * 3.0))

    nugget = float(max(0.0, var * 0.05))
    partial_sill = float(max(1e-6, var - nugget))

    return KrigingParams(model="exponential", nugget=nugget, partial_sill=partial_sill, range=rng)


def _cov_exponential(dist, *, partial_sill: float, rng: float):
    # C(h) = partial_sill * exp(-h / range)
    try:
        import numpy as np
    except Exception:
        raise RuntimeError("numpy is required")
    rng0 = float(max(1e-12, rng))
    return float(partial_sill) * np.exp(-dist / rng0)


def _write_geotiff(
    *,
    out_path: str,
    array_2d,
    extent: QgsRectangle,
    pixel_size: float,
    crs_wkt: str,
    nodata: float,
) -> None:
    """Write a single-band Float32 GeoTIFF (requires GDAL python bindings)."""
    try:
        import numpy as np
    except Exception as e:
        raise RuntimeError(f"numpy is required to write raster ({e})")

    try:
        from osgeo import gdal  # type: ignore
    except Exception as e:
        raise RuntimeError(f"GDAL Python bindings not available: {e}")

    arr = np.asarray(array_2d, dtype=np.float32)
    if arr.ndim != 2:
        raise ValueError("Expected 2D array")

    height, width = int(arr.shape[0]), int(arr.shape[1])
    if width <= 0 or height <= 0:
        raise ValueError("Invalid raster shape")

    driver = gdal.GetDriverByName("GTiff")
    if driver is None:
        raise RuntimeError("GDAL GTiff driver unavailable")

    ds = driver.Create(
        str(out_path),
        int(width),
        int(height),
        1,
        gdal.GDT_Float32,
        options=["TILED=YES", "COMPRESS=LZW"],
    )
    if ds is None:
        raise RuntimeError("GDAL Create() failed")

    # GeoTransform: top-left corner + pixel sizes (north-up)
    xmin = float(extent.xMinimum())
    ymax = float(extent.yMaximum())
    px = float(pixel_size)
    ds.SetGeoTransform((xmin, px, 0.0, ymax, 0.0, -px))

    if crs_wkt:
        try:
            ds.SetProjection(str(crs_wkt))
        except Exception:
            pass

    band = ds.GetRasterBand(1)
    band.SetNoDataValue(float(nodata))
    band.WriteArray(arr)
    band.FlushCache()
    ds.FlushCache()
    ds = None


def ordinary_kriging_lite_to_geotiff(
    *,
    layer: QgsVectorLayer,
    value_field: Optional[str],
    extent: QgsRectangle,
    pixel_size: float,
    out_path: str,
    variance_path: Optional[str] = None,
    neighbors: int = 16,
    max_cells: int = 250_000,
    progress_cb: Optional[Callable[[int, str], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> Dict[str, object]:
    """Run Ordinary Kriging (Lite) and write GeoTIFF(s).

    Returns a dict with keys:
    - out_path, variance_path
    - params (KrigingParams as dict)
    - ncols, nrows, n_points
    """
    if layer is None or not layer.isValid():
        raise ValueError("Invalid layer")

    if not is_metric_crs(layer.crs()):
        raise ValueError("Layer CRS must be projected in meters for Kriging")

    px = float(pixel_size)
    if not (px > 0):
        raise ValueError("Invalid pixel size")

    # Prepare samples + index
    points_xy, values, index = _collect_point_samples(layer, value_field=value_field)

    # Compute grid size (ceil so we fully cover extent)
    width = float(extent.width())
    height = float(extent.height())
    ncols = int(max(1, math.ceil(width / px)))
    nrows = int(max(1, math.ceil(height / px)))
    ncells = int(ncols * nrows)

    if ncells > int(max_cells):
        area = float(max(0.0, width * height))
        rec_px = math.sqrt(area / float(max_cells)) if area > 0 else px
        raise ValueError(
            f"Grid too large for Kriging Lite: {ncols}x{nrows}={ncells:,} cells. "
            f"Increase pixel size (≈ {rec_px:.2f}m+) or reduce extent."
        )

    params = auto_params(points_xy=points_xy, values=values, extent=extent, index=index)
    log_message(
        f"[kriging] auto params: model={params.model}, nugget={params.nugget:.6g}, "
        f"partial_sill={params.partial_sill:.6g}, range={params.range:.6g}"
    )

    try:
        import numpy as np
    except Exception as e:
        raise RuntimeError(f"numpy is required for Kriging Lite ({e})")

    pts = np.array(points_xy, dtype=float)
    zs = np.array(values, dtype=float)

    neighbor_n = int(max(3, min(int(neighbors), len(points_xy))))
    nodata = -9999.0

    pred = np.full((nrows, ncols), nodata, dtype=np.float32)
    varr = np.full((nrows, ncols), nodata, dtype=np.float32)

    # Cache inverse matrices by neighbor id tuple (local neighborhoods repeat a lot on grids).
    inv_cache: Dict[Tuple[int, ...], np.ndarray] = {}
    inv_cache_max = 5000

    def get_inv(nei_ids: Sequence[int]) -> np.ndarray:
        key = tuple(int(i) for i in nei_ids)
        inv = inv_cache.get(key)
        if inv is not None:
            return inv

        coords = pts[list(key), :]
        dx = coords[:, 0][:, None] - coords[:, 0][None, :]
        dy = coords[:, 1][:, None] - coords[:, 1][None, :]
        dist = np.sqrt(dx * dx + dy * dy)
        C = _cov_exponential(dist, partial_sill=params.partial_sill, rng=params.range)

        # Add nugget on diagonal as measurement noise / stabilization.
        np.fill_diagonal(C, float(params.partial_sill + params.nugget))

        n = int(len(key))
        A = np.empty((n + 1, n + 1), dtype=float)
        A[:n, :n] = C
        A[:n, n] = 1.0
        A[n, :n] = 1.0
        A[n, n] = 0.0

        # Regularize if singular (duplicates / near-duplicates)
        try:
            inv = np.linalg.inv(A)
        except Exception:
            eps = float(max(1e-12, params.partial_sill * 1e-10))
            for i in range(n):
                A[i, i] += eps
            inv = np.linalg.inv(A)

        if len(inv_cache) >= inv_cache_max:
            inv_cache.clear()
        inv_cache[key] = inv
        return inv

    xmin = float(extent.xMinimum())
    ymax = float(extent.yMaximum())

    for r in range(nrows):
        if is_cancelled and is_cancelled():
            raise RuntimeError("Cancelled")

        y = ymax - (float(r) + 0.5) * px
        for c in range(ncols):
            x = xmin + (float(c) + 0.5) * px

            try:
                nei_ids = index.nearestNeighbor(QgsPointXY(x, y), neighbor_n)
            except Exception:
                nei_ids = []
            if not nei_ids or len(nei_ids) < 3:
                continue

            key = [int(i) for i in nei_ids if 0 <= int(i) < len(points_xy)]
            if len(key) < 3:
                continue

            inv = get_inv(key)
            coords = pts[key, :]
            dz = zs[key]

            dx0 = coords[:, 0] - float(x)
            dy0 = coords[:, 1] - float(y)
            dist0 = np.sqrt(dx0 * dx0 + dy0 * dy0)
            cvec = _cov_exponential(dist0, partial_sill=params.partial_sill, rng=params.range)

            b = np.empty((len(key) + 1,), dtype=float)
            b[:-1] = cvec
            b[-1] = 1.0

            w = inv.dot(b)
            lam = w[:-1]
            mu = float(w[-1])

            zhat = float(lam.dot(dz))
            pred[r, c] = float(zhat)

            # OK variance (best-effort)
            vv = float(params.partial_sill + params.nugget) - float(lam.dot(cvec)) + float(mu)
            if vv < 0:
                vv = 0.0
            varr[r, c] = float(vv)

        if progress_cb:
            try:
                pct = int((r + 1) * 100 / max(1, nrows))
                progress_cb(pct, f"Kriging 계산 중… ({r+1}/{nrows})")
            except Exception:
                pass

    crs_wkt = ""
    try:
        crs_wkt = layer.crs().toWkt()
    except Exception:
        crs_wkt = ""

    _write_geotiff(out_path=out_path, array_2d=pred, extent=extent, pixel_size=px, crs_wkt=crs_wkt, nodata=nodata)
    if variance_path:
        _write_geotiff(
            out_path=variance_path,
            array_2d=varr,
            extent=extent,
            pixel_size=px,
            crs_wkt=crs_wkt,
            nodata=nodata,
        )

    return {
        "out_path": out_path,
        "variance_path": variance_path,
        "params": {
            "model": params.model,
            "nugget": params.nugget,
            "partial_sill": params.partial_sill,
            "range": params.range,
            "neighbors": neighbor_n,
        },
        "ncols": ncols,
        "nrows": nrows,
        "n_points": len(points_xy),
    }
