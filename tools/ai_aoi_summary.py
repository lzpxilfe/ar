# -*- coding: utf-8 -*-
"""
AOI-centered project summary builder for ArchToolkit AI reporting.

Design goals
- Best-effort and fast: avoid heavy processing providers when possible.
- Only summarize information needed for a narrative report (no raw raster export).
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

try:
    from osgeo import gdal, ogr
except Exception:  # pragma: no cover
    gdal = None
    ogr = None

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMapLayer,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .utils import is_metric_crs, log_message


def _split_qgis_source_path(src: str) -> str:
    try:
        s = str(src or "")
        return (s.split("|", 1)[0] or "").strip()
    except Exception:
        return str(src or "").strip()


def _safe_distance_area(crs) -> QgsDistanceArea:
    da = QgsDistanceArea()
    try:
        da.setSourceCrs(crs, QgsProject.instance().transformContext())
    except Exception:
        pass
    try:
        ellps = QgsProject.instance().ellipsoid()
        if ellps:
            da.setEllipsoid(ellps)
    except Exception:
        pass
    return da


def _unary_union_geoms(layer: QgsVectorLayer, *, selected_only: bool) -> Tuple[Optional[QgsGeometry], int]:
    geoms = []
    count = 0
    feats = layer.selectedFeatures() if selected_only and layer.selectedFeatureCount() > 0 else layer.getFeatures()
    for f in feats:
        try:
            g = f.geometry()
        except Exception:
            continue
        if not g or g.isEmpty():
            continue
        geoms.append(QgsGeometry(g))
        count += 1
    if not geoms:
        return None, 0
    if len(geoms) == 1:
        return geoms[0], count
    try:
        return QgsGeometry.unaryUnion(geoms), count
    except Exception:
        # Fallback: iterative combine
        try:
            out = geoms[0]
            for g in geoms[1:]:
                out = out.combine(g)
            return out, count
        except Exception:
            return None, count


def is_archtoolkit_layer(layer: QgsMapLayer) -> bool:
    """Heuristic: identify layers created by ArchToolkit tools."""
    if layer is None:
        return False
    try:
        name = str(layer.name() or "")
        if name.startswith("Style:") or name.startswith("AOI_"):
            return True
    except Exception:
        pass
    try:
        src = str(layer.source() or "")
        src_l = src.lower()
        if "archtoolkit_" in src_l or "archt_" in src_l or "archtoolkit" in src_l:
            return True
    except Exception:
        pass
    try:
        # Cost tool tags
        if layer.customProperty("archtoolkit/cost_surface/run_id", None) is not None:
            return True
    except Exception:
        pass
    return False


def _layer_group_path(layer_id: str) -> str:
    """Return a best-effort layer tree path 'Group/Sub/Layer'."""
    try:
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer_id)
        if node is None:
            return ""
        parts = []
        cur = node
        while cur is not None:
            try:
                if cur.name():
                    parts.append(cur.name())
            except Exception:
                pass
            cur = cur.parent()
            if cur == root:
                break
        parts.reverse()
        return "/".join(parts)
    except Exception:
        return ""


def _transform_geom(geom: QgsGeometry, src_crs, dst_crs) -> Optional[QgsGeometry]:
    if geom is None or geom.isEmpty():
        return None
    try:
        if src_crs == dst_crs:
            return QgsGeometry(geom)
    except Exception:
        pass
    try:
        tr = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
    except Exception:
        return None
    out = QgsGeometry(geom)
    try:
        out.transform(tr)
    except Exception:
        return None
    if out.isEmpty():
        return None
    return out


def _vector_layer_stats_in_geom(
    layer: QgsVectorLayer,
    geom: QgsGeometry,
    *,
    max_features_scan: int = 20000,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"features": 0}
    if layer is None or geom is None or geom.isEmpty():
        return out

    da = _safe_distance_area(layer.crs())

    bbox: QgsRectangle = geom.boundingBox()
    req = QgsFeatureRequest().setFilterRect(bbox)

    geom_type = layer.geometryType()
    total_len = 0.0
    total_area = 0.0
    n = 0
    scanned = 0

    # Lightweight field-aware summaries (optional)
    field_names = []
    try:
        field_names = [f.name() for f in layer.fields()]
    except Exception:
        field_names = []

    hist = None
    hist_field = None
    for cand in ("class_id", "Layer", "element"):
        if cand in field_names:
            hist_field = cand
            hist = {}
            break

    for feat in layer.getFeatures(req):
        scanned += 1
        if scanned > int(max_features_scan):
            break
        try:
            g = feat.geometry()
        except Exception:
            continue
        if not g or g.isEmpty():
            continue
        try:
            if not g.intersects(geom):
                continue
        except Exception:
            continue

        n += 1
        if geom_type == QgsWkbTypes.LineGeometry:
            try:
                total_len += float(da.measureLength(g.intersection(geom)))
            except Exception:
                pass
        elif geom_type == QgsWkbTypes.PolygonGeometry:
            try:
                total_area += float(da.measureArea(g.intersection(geom)))
            except Exception:
                pass

        if hist is not None and hist_field is not None:
            try:
                v = feat[hist_field]
                k = str(v) if v is not None else "(null)"
                hist[k] = int(hist.get(k, 0)) + 1
            except Exception:
                pass

    out["features"] = int(n)
    out["scanned"] = int(scanned)
    if geom_type == QgsWkbTypes.LineGeometry:
        out["total_length_m"] = float(total_len)
    if geom_type == QgsWkbTypes.PolygonGeometry:
        out["total_area_m2"] = float(total_area)
    if hist is not None:
        # keep top 20
        items = sorted(hist.items(), key=lambda kv: kv[1], reverse=True)[:20]
        out["top_values"] = [{"value": k, "count": int(v)} for k, v in items]
        out["top_field"] = hist_field
    return out


def _raster_stats_in_geom(
    raster_path: str,
    geom: QgsGeometry,
    *,
    max_pixels: int = 4_000_000,  # cap for memory safety
) -> Optional[Dict[str, Any]]:
    if np is None or gdal is None or ogr is None:
        return None
    if not raster_path or not os.path.exists(str(raster_path)):
        return None
    if geom is None or geom.isEmpty():
        return None

    ds = gdal.Open(str(raster_path), gdal.GA_ReadOnly)
    if ds is None:
        return None
    band = ds.GetRasterBand(1)
    if band is None:
        ds = None
        return None

    gt = ds.GetGeoTransform()
    proj = ds.GetProjection() or ""
    nodata = band.GetNoDataValue()

    try:
        inv_gt = gdal.InvGeoTransform(gt)
        if isinstance(inv_gt, tuple) and len(inv_gt) == 2:
            ok, inv_gt = inv_gt
            if not ok:
                ds = None
                return None
    except Exception:
        ds = None
        return None

    bbox = geom.boundingBox()
    try:
        px0, py0 = gdal.ApplyGeoTransform(inv_gt, float(bbox.xMinimum()), float(bbox.yMaximum()))
        px1, py1 = gdal.ApplyGeoTransform(inv_gt, float(bbox.xMaximum()), float(bbox.yMinimum()))
    except Exception:
        ds = None
        return None

    x0 = int(math.floor(min(px0, px1)))
    x1 = int(math.ceil(max(px0, px1)))
    y0 = int(math.floor(min(py0, py1)))
    y1 = int(math.ceil(max(py0, py1)))

    x0 = max(0, min(ds.RasterXSize - 1, x0))
    y0 = max(0, min(ds.RasterYSize - 1, y0))
    x1 = max(0, min(ds.RasterXSize, x1))
    y1 = max(0, min(ds.RasterYSize, y1))

    w = int(max(1, x1 - x0))
    h = int(max(1, y1 - y0))
    if w <= 0 or h <= 0:
        ds = None
        return None

    # Downsample if too big
    step = 1
    try:
        if int(w) * int(h) > int(max_pixels):
            step = int(math.ceil(math.sqrt((w * h) / float(max_pixels))))
            step = max(1, step)
    except Exception:
        step = 1

    try:
        arr = band.ReadAsArray(x0, y0, w, h)
    except Exception:
        ds = None
        return None

    if arr is None:
        ds = None
        return None

    try:
        arr = arr.astype(np.float32, copy=False)
        if step > 1:
            arr = arr[::step, ::step]
    except Exception:
        pass

    # Rasterize polygon mask into the same window
    try:
        win_gt = (
            gt[0] + x0 * gt[1] + y0 * gt[2],
            gt[1] * step,
            gt[2] * step,
            gt[3] + x0 * gt[4] + y0 * gt[5],
            gt[4] * step,
            gt[5] * step,
        )

        rdrv = gdal.GetDriverByName("MEM")
        mds = rdrv.Create("", int(arr.shape[1]), int(arr.shape[0]), 1, gdal.GDT_Byte)
        if mds is None:
            ds = None
            return None
        mds.SetGeoTransform(win_gt)
        mds.SetProjection(str(proj))
        mband = mds.GetRasterBand(1)
        mband.Fill(0)
        mband.SetNoDataValue(0)

        ogr_geom = ogr.CreateGeometryFromWkb(bytes(geom.asWkb()))
        vdrv = ogr.GetDriverByName("Memory")
        vds = vdrv.CreateDataSource("")
        vlyr = vds.CreateLayer("mask", None, ogr.wkbUnknown)
        feat_defn = vlyr.GetLayerDefn()
        feat = ogr.Feature(feat_defn)
        feat.SetGeometry(ogr_geom)
        vlyr.CreateFeature(feat)

        gdal.RasterizeLayer(mds, [1], vlyr, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
        mask = mband.ReadAsArray()
        if mask is None:
            ds = None
            return None
        mask = mask != 0
    except Exception:
        ds = None
        return None

    ds = None

    valid = mask & np.isfinite(arr)
    if nodata is not None:
        try:
            valid &= arr != float(nodata)
        except Exception:
            pass

    if not np.any(valid):
        return None

    vals = arr[valid]
    try:
        out = {
            "count": int(vals.size),
            "min": float(np.nanmin(vals)),
            "max": float(np.nanmax(vals)),
            "mean": float(np.nanmean(vals)),
        }
        # Binary-ish visibility hint
        try:
            vis = float(np.count_nonzero(vals > 0.5)) / float(vals.size) * 100.0
            out["gt_0_5_pct"] = float(vis)
        except Exception:
            pass
        return out
    except Exception:
        return None


def build_aoi_context(
    *,
    aoi_layer: QgsVectorLayer,
    selected_only: bool,
    radius_m: float,
    only_archtoolkit_layers: bool = True,
    max_layers: int = 40,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if aoi_layer is None or aoi_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
        return None, "AOI 레이어는 폴리곤이어야 합니다."

    aoi_crs = aoi_layer.crs()
    if not is_metric_crs(aoi_crs):
        return None, "AOI CRS 단위가 미터가 아닙니다. (투영 CRS 사용 권장)"

    aoi_geom, feat_n = _unary_union_geoms(aoi_layer, selected_only=selected_only)
    if aoi_geom is None or aoi_geom.isEmpty():
        return None, "AOI 지오메트리를 만들 수 없습니다."

    r = float(radius_m)
    if not math.isfinite(r) or r <= 0:
        return None, "반경(m)은 0보다 커야 합니다."

    try:
        buf_geom = aoi_geom.buffer(r, 24)
    except Exception:
        buf_geom = None
    if buf_geom is None or buf_geom.isEmpty():
        return None, "AOI 버퍼를 만들 수 없습니다."

    da = _safe_distance_area(aoi_crs)
    try:
        aoi_area = float(da.measureArea(aoi_geom))
    except Exception:
        aoi_area = None
    try:
        buf_area = float(da.measureArea(buf_geom))
    except Exception:
        buf_area = None

    layers = list(QgsProject.instance().mapLayers().values())
    summaries: List[Dict[str, Any]] = []

    for lyr in layers:
        if lyr is None or lyr.id() == aoi_layer.id():
            continue
        if only_archtoolkit_layers and (not is_archtoolkit_layer(lyr)):
            continue

        # Transform buffer geometry to layer CRS to do intersection tests.
        try:
            g_layer = _transform_geom(buf_geom, aoi_crs, lyr.crs())
        except Exception:
            g_layer = None
        if g_layer is None or g_layer.isEmpty():
            continue

        try:
            if not lyr.extent().intersects(g_layer.boundingBox()):
                continue
        except Exception:
            pass

        item: Dict[str, Any] = {
            "id": lyr.id(),
            "name": lyr.name(),
            "type": "raster" if isinstance(lyr, QgsRasterLayer) else "vector" if isinstance(lyr, QgsVectorLayer) else "other",
            "crs": getattr(lyr.crs(), "authid", lambda: "")() if hasattr(lyr, "crs") else "",
            "group_path": _layer_group_path(lyr.id()),
        }

        if isinstance(lyr, QgsVectorLayer):
            item["geometry_type"] = int(lyr.geometryType())
            try:
                item["wkb"] = QgsWkbTypes.displayString(lyr.wkbType())
            except Exception:
                pass
            try:
                item["provider"] = str(lyr.providerType() or "")
            except Exception:
                pass

            try:
                item["stats"] = _vector_layer_stats_in_geom(lyr, g_layer)
            except Exception:
                item["stats"] = {"features": 0}

        elif isinstance(lyr, QgsRasterLayer):
            try:
                item["provider"] = str(lyr.providerType() or "")
            except Exception:
                pass
            src_path = _split_qgis_source_path(lyr.source())
            item["source"] = os.path.basename(src_path) if src_path else ""
            if src_path and os.path.exists(src_path):
                try:
                    item["stats"] = _raster_stats_in_geom(src_path, g_layer)
                except Exception:
                    item["stats"] = None
            else:
                item["stats"] = None

        summaries.append(item)
        if len(summaries) >= int(max_layers):
            break

    ctx: Dict[str, Any] = {
        "aoi": {
            "layer_name": aoi_layer.name(),
            "feature_count": int(feat_n),
            "crs": aoi_crs.authid(),
            "area_m2": aoi_area,
        },
        "radius_m": float(r),
        "buffer_area_m2": buf_area,
        "layers": summaries,
    }

    try:
        log_message(f"AI AOI summary: layers={len(summaries)} (archtoolkit_only={only_archtoolkit_layers})", level=Qgis.Info)
    except Exception:
        pass

    return ctx, None
