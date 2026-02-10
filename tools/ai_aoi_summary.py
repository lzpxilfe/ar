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
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .utils import get_archtoolkit_layer_metadata, is_metric_crs, log_message


_NUMERIC_FIELD_CANDIDATES = (
    # Generic stats-like fields
    "value",
    "val",
    "min",
    "max",
    "mean",
    "avg",
    # Viewshed AOI stats (common)
    "vis_pct",
    "vis_m2",
    "tot_m2",
    "feat_n",
    # Terrain profile (line attributes)
    "distance",
    "min_elev",
    "max_elev",
    # Cost/LCP outputs
    "dist_m",
    "time_min",
    "energy_kcal",
    # GeoChem polygonize outputs
    "val_min",
    "val_max",
    "v_min",
    "v_max",
    # Cadastral overlap outputs
    "parcel_m2",
    "in_aoi_m2",
    "in_aoi_pct",
)


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
        meta = get_archtoolkit_layer_metadata(layer)
        if meta and (meta.get("tool_id") or meta.get("run_id")):
            return True
    except Exception:
        pass
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
    try:
        # Many tools place outputs under an "ArchToolkit - ..." layer tree group.
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer.id())
        cur = node.parent() if node is not None else None
        while cur is not None and cur != root:
            try:
                if str(cur.name() or "").startswith("ArchToolkit -"):
                    return True
            except Exception:
                pass
            try:
                cur = cur.parent()
            except Exception:
                break
    except Exception:
        pass
    return False


def _layer_archtoolkit_meta(layer: QgsMapLayer) -> Dict[str, Any]:
    """Return ArchToolkit metadata dict for this layer (best-effort)."""
    if layer is None:
        return {}
    try:
        meta = get_archtoolkit_layer_metadata(layer) or {}
        if meta:
            return meta
    except Exception:
        meta = {}

    # Backward-compat: older builds tagged cost surface layers under a different key.
    try:
        rid = layer.customProperty("archtoolkit/cost_surface/run_id", None)
        if rid:
            out = {"tool_id": "cost_surface", "run_id": str(rid)}
            k = layer.customProperty("archtoolkit/cost_surface/kind", None)
            if k:
                out["kind"] = str(k)
            return out
    except Exception:
        pass
    return {}


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
    origin_point: Optional[QgsPointXY] = None,
    origin_crs=None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"features": 0}
    if layer is None or geom is None or geom.isEmpty():
        return out

    da = _safe_distance_area(layer.crs())
    da_origin = _safe_distance_area(origin_crs) if origin_point is not None and origin_crs is not None else None
    ct_to_origin = None
    if da_origin is not None:
        try:
            if layer.crs() != origin_crs:
                ct_to_origin = QgsCoordinateTransform(layer.crs(), origin_crs, QgsProject.instance())
        except Exception:
            ct_to_origin = None

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

    num_fields = [c for c in _NUMERIC_FIELD_CANDIDATES if c in field_names]
    num_acc: Dict[str, Dict[str, Any]] = {}
    for f in num_fields:
        num_acc[str(f)] = {"sum": 0.0, "min": float("inf"), "max": float("-inf"), "n": 0}

    dist_acc = None
    if geom_type == QgsWkbTypes.PointGeometry and da_origin is not None and origin_point is not None:
        dist_acc = {"sum": 0.0, "min": float("inf"), "max": float("-inf"), "n": 0}

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

        for f, acc in num_acc.items():
            try:
                v = feat[f]
                if v is None:
                    continue
                x = float(v)
                if not math.isfinite(x):
                    continue
                acc["sum"] = float(acc["sum"]) + float(x)
                acc["min"] = float(min(float(acc["min"]), float(x)))
                acc["max"] = float(max(float(acc["max"]), float(x)))
                acc["n"] = int(acc["n"]) + 1
            except Exception:
                continue

        if dist_acc is not None:
            try:
                pt = g.centroid().asPoint()
                pt_xy = QgsPointXY(pt)
                if ct_to_origin is not None:
                    pt_xy = ct_to_origin.transform(pt_xy)
                dist = float(da_origin.measureLine(origin_point, pt_xy))
                if math.isfinite(dist):
                    dist_acc["sum"] = float(dist_acc["sum"]) + dist
                    dist_acc["min"] = float(min(float(dist_acc["min"]), dist))
                    dist_acc["max"] = float(max(float(dist_acc["max"]), dist))
                    dist_acc["n"] = int(dist_acc["n"]) + 1
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

    numeric_out = {}
    for f, acc in num_acc.items():
        try:
            n0 = int(acc.get("n") or 0)
            if n0 <= 0:
                continue
            s0 = float(acc.get("sum") or 0.0)
            numeric_out[f] = {
                "n": n0,
                "min": float(acc.get("min")),
                "max": float(acc.get("max")),
                "mean": float(s0 / float(n0)),
            }
        except Exception:
            continue
    if numeric_out:
        out["numeric_fields"] = numeric_out

    if dist_acc is not None:
        try:
            dn = int(dist_acc.get("n") or 0)
            if dn > 0:
                ds = float(dist_acc.get("sum") or 0.0)
                out["dist_to_aoi_centroid_m"] = {
                    "n": dn,
                    "min": float(dist_acc.get("min")),
                    "max": float(dist_acc.get("max")),
                    "mean": float(ds / float(dn)),
                }
        except Exception:
            pass
    return out


def _pick_reference_name_field(layer: QgsVectorLayer, preferred: str = "") -> str:
    if layer is None or not isinstance(layer, QgsVectorLayer):
        return ""
    pref = str(preferred or "").strip()
    try:
        if pref and layer.fields().indexFromName(pref) >= 0:
            return pref
    except Exception:
        pass

    try:
        fields = [f.name() for f in layer.fields()]
    except Exception:
        fields = []
    if not fields:
        return ""

    want = (
        "name",
        "site_name",
        "title",
        "label",
        "유적명",
        "명칭",
        "문화재명",
        "대상명",
        "지명",
    )
    lower_map = {str(n).lower(): str(n) for n in fields}
    for cand in want:
        key = str(cand).lower()
        if key in lower_map:
            return lower_map[key]

    try:
        for f in layer.fields():
            if int(f.type()) == int(QVariant.String):
                return str(f.name() or "")
    except Exception:
        pass
    return ""


def _feature_ref_name(feature, *, name_field: str) -> str:
    if feature is None:
        return ""
    try:
        if name_field:
            v = feature[name_field]
            if v is not None:
                s = str(v).strip()
                if s:
                    return s
    except Exception:
        pass
    try:
        return f"FID {int(feature.id())}"
    except Exception:
        return "feature"


def _extract_representative_point(geom: QgsGeometry) -> Optional[QgsPointXY]:
    if geom is None or geom.isEmpty():
        return None
    try:
        if geom.type() == QgsWkbTypes.PointGeometry:
            if geom.isMultipart():
                pts = geom.asMultiPoint()
                if pts:
                    return QgsPointXY(pts[0])
            else:
                return QgsPointXY(geom.asPoint())
    except Exception:
        pass

    try:
        p = geom.pointOnSurface()
        if p is not None and (not p.isEmpty()):
            return QgsPointXY(p.asPoint())
    except Exception:
        pass
    try:
        c = geom.centroid()
        if c is not None and (not c.isEmpty()):
            return QgsPointXY(c.asPoint())
    except Exception:
        pass
    return None


def _reference_sites_summary(
    *,
    layer: QgsVectorLayer,
    aoi_geom: QgsGeometry,
    buffer_geom: QgsGeometry,
    aoi_centroid_pt: Optional[QgsPointXY],
    aoi_crs,
    selected_only: bool,
    preferred_name_field: str = "",
    max_features: int = 120,
) -> Optional[Dict[str, Any]]:
    if layer is None or not isinstance(layer, QgsVectorLayer):
        return None
    if aoi_geom is None or aoi_geom.isEmpty() or buffer_geom is None or buffer_geom.isEmpty():
        return None

    try:
        g_aoi_on_layer = _transform_geom(aoi_geom, aoi_crs, layer.crs())
        g_buf_on_layer = _transform_geom(buffer_geom, aoi_crs, layer.crs())
    except Exception:
        g_aoi_on_layer = None
        g_buf_on_layer = None
    if g_aoi_on_layer is None or g_aoi_on_layer.isEmpty() or g_buf_on_layer is None or g_buf_on_layer.isEmpty():
        return None

    # Small bbox pre-filter in source CRS.
    try:
        req = QgsFeatureRequest().setFilterRect(g_buf_on_layer.boundingBox())
    except Exception:
        req = QgsFeatureRequest()

    selected_count = 0
    try:
        if selected_only:
            selected_count = int(layer.selectedFeatureCount())
            if selected_count > 0:
                features = layer.selectedFeatures()
            else:
                # Respect "selected only": empty selection means empty result.
                features = []
        else:
            features = layer.getFeatures(req)
    except Exception:
        try:
            features = layer.getFeatures(req) if not selected_only else []
        except Exception:
            features = []

    max_items = int(max(1, min(int(max_features), 2000)))
    max_scan = int(max(max_items * 20, 500))

    name_field = _pick_reference_name_field(layer, preferred=preferred_name_field)
    da = _safe_distance_area(aoi_crs)

    scanned = 0
    kept = 0
    counts = {
        "inside_or_overlap_aoi": 0,
        "inside_buffer_only": 0,
        "outside_buffer": 0,
        "inside_aoi": 0,
        "crosses_aoi_boundary": 0,
        "crosses_buffer_boundary": 0,
    }
    items: List[Dict[str, Any]] = []

    for ft in features:
        scanned += 1
        if scanned > max_scan:
            break

        try:
            g0 = ft.geometry()
        except Exception:
            continue
        if g0 is None or g0.isEmpty():
            continue

        if not selected_only:
            try:
                if not g0.intersects(g_buf_on_layer):
                    # When not using explicit selection, keep the scan focused around AOI.
                    continue
            except Exception:
                pass

        g = _transform_geom(g0, layer.crs(), aoi_crs)
        if g is None or g.isEmpty():
            continue

        try:
            intersects_aoi = bool(g.intersects(aoi_geom))
        except Exception:
            intersects_aoi = False
        try:
            within_aoi = bool(g.within(aoi_geom))
        except Exception:
            within_aoi = False
        try:
            intersects_buf = bool(g.intersects(buffer_geom))
        except Exception:
            intersects_buf = False
        try:
            within_buf = bool(g.within(buffer_geom))
        except Exception:
            within_buf = False

        crosses_aoi_boundary = bool(intersects_aoi and (not within_aoi))
        crosses_buffer_boundary = bool(intersects_buf and (not within_buf))

        if within_aoi:
            relation = "inside_aoi"
            counts["inside_aoi"] += 1
            counts["inside_or_overlap_aoi"] += 1
        elif intersects_aoi:
            relation = "crosses_aoi_boundary"
            counts["crosses_aoi_boundary"] += 1
            counts["inside_or_overlap_aoi"] += 1
        elif within_buf:
            relation = "inside_buffer_only"
            counts["inside_buffer_only"] += 1
        elif intersects_buf:
            relation = "crosses_buffer_boundary"
            counts["crosses_buffer_boundary"] += 1
        else:
            relation = "outside_buffer"
            counts["outside_buffer"] += 1

        try:
            dist_to_aoi = 0.0 if intersects_aoi else float(g.distance(aoi_geom))
            if not math.isfinite(dist_to_aoi):
                dist_to_aoi = None
        except Exception:
            dist_to_aoi = None
        try:
            dist_to_buffer = 0.0 if intersects_buf else float(g.distance(buffer_geom))
            if not math.isfinite(dist_to_buffer):
                dist_to_buffer = None
        except Exception:
            dist_to_buffer = None

        dist_to_centroid = None
        if aoi_centroid_pt is not None:
            try:
                rp = _extract_representative_point(g)
                if rp is not None:
                    d0 = float(da.measureLine(aoi_centroid_pt, rp))
                    if math.isfinite(d0):
                        dist_to_centroid = d0
            except Exception:
                dist_to_centroid = None

        feature_area_m2 = None
        overlap_aoi_area_m2 = None
        overlap_buffer_area_m2 = None
        outside_aoi_area_m2 = None
        outside_buffer_area_m2 = None
        inside_aoi_area_pct = None
        outside_aoi_area_pct = None
        inside_buffer_area_pct = None
        outside_buffer_area_pct = None

        feature_length_m = None
        overlap_aoi_length_m = None
        overlap_buffer_length_m = None
        outside_aoi_length_m = None
        outside_buffer_length_m = None
        inside_aoi_length_pct = None
        outside_aoi_length_pct = None
        inside_buffer_length_pct = None
        outside_buffer_length_pct = None
        try:
            gt = int(g.type())
        except Exception:
            gt = -1
        if gt == int(QgsWkbTypes.PolygonGeometry):
            try:
                feature_area_m2 = float(da.measureArea(g))
            except Exception:
                feature_area_m2 = None
            try:
                inter = g.intersection(aoi_geom)
                if inter is not None and (not inter.isEmpty()):
                    overlap_aoi_area_m2 = float(da.measureArea(inter))
            except Exception:
                overlap_aoi_area_m2 = None
            try:
                interb = g.intersection(buffer_geom)
                if interb is not None and (not interb.isEmpty()):
                    overlap_buffer_area_m2 = float(da.measureArea(interb))
            except Exception:
                overlap_buffer_area_m2 = None

            try:
                if feature_area_m2 is not None and feature_area_m2 > 0:
                    in_aoi = float(overlap_aoi_area_m2 or 0.0)
                    in_buf = float(overlap_buffer_area_m2 or 0.0)
                    outside_aoi_area_m2 = max(0.0, float(feature_area_m2) - in_aoi)
                    outside_buffer_area_m2 = max(0.0, float(feature_area_m2) - in_buf)
                    inside_aoi_area_pct = max(0.0, min(100.0, (in_aoi / float(feature_area_m2)) * 100.0))
                    outside_aoi_area_pct = max(0.0, min(100.0, (outside_aoi_area_m2 / float(feature_area_m2)) * 100.0))
                    inside_buffer_area_pct = max(0.0, min(100.0, (in_buf / float(feature_area_m2)) * 100.0))
                    outside_buffer_area_pct = max(0.0, min(100.0, (outside_buffer_area_m2 / float(feature_area_m2)) * 100.0))
            except Exception:
                pass
        elif gt == int(QgsWkbTypes.LineGeometry):
            try:
                feature_length_m = float(da.measureLength(g))
            except Exception:
                feature_length_m = None
            try:
                inter = g.intersection(aoi_geom)
                if inter is not None and (not inter.isEmpty()):
                    overlap_aoi_length_m = float(da.measureLength(inter))
            except Exception:
                overlap_aoi_length_m = None
            try:
                interb = g.intersection(buffer_geom)
                if interb is not None and (not interb.isEmpty()):
                    overlap_buffer_length_m = float(da.measureLength(interb))
            except Exception:
                overlap_buffer_length_m = None

            try:
                if feature_length_m is not None and feature_length_m > 0:
                    in_aoi_l = float(overlap_aoi_length_m or 0.0)
                    in_buf_l = float(overlap_buffer_length_m or 0.0)
                    outside_aoi_length_m = max(0.0, float(feature_length_m) - in_aoi_l)
                    outside_buffer_length_m = max(0.0, float(feature_length_m) - in_buf_l)
                    inside_aoi_length_pct = max(0.0, min(100.0, (in_aoi_l / float(feature_length_m)) * 100.0))
                    outside_aoi_length_pct = max(0.0, min(100.0, (outside_aoi_length_m / float(feature_length_m)) * 100.0))
                    inside_buffer_length_pct = max(0.0, min(100.0, (in_buf_l / float(feature_length_m)) * 100.0))
                    outside_buffer_length_pct = max(0.0, min(100.0, (outside_buffer_length_m / float(feature_length_m)) * 100.0))
            except Exception:
                pass

        item = {
            "fid": int(ft.id()) if hasattr(ft, "id") else None,
            "name": _feature_ref_name(ft, name_field=name_field),
            "relation": relation,
            "crosses_aoi_boundary": crosses_aoi_boundary,
            "crosses_buffer_boundary": crosses_buffer_boundary,
            "inside_or_overlap_aoi": bool(intersects_aoi or within_aoi),
            "inside_or_overlap_buffer": bool(intersects_buf or within_buf),
            "distance_to_aoi_m": dist_to_aoi,
            "distance_to_buffer_m": dist_to_buffer,
            "distance_to_aoi_centroid_m": dist_to_centroid,
        }
        try:
            item["wkb"] = QgsWkbTypes.displayString(layer.wkbType())
        except Exception:
            pass
        if overlap_aoi_area_m2 is not None:
            item["overlap_aoi_area_m2"] = overlap_aoi_area_m2
        if overlap_buffer_area_m2 is not None:
            item["overlap_buffer_area_m2"] = overlap_buffer_area_m2
        if feature_area_m2 is not None:
            item["feature_area_m2"] = feature_area_m2
        if outside_aoi_area_m2 is not None:
            item["outside_aoi_area_m2"] = outside_aoi_area_m2
        if outside_buffer_area_m2 is not None:
            item["outside_buffer_area_m2"] = outside_buffer_area_m2
        if inside_aoi_area_pct is not None:
            item["inside_aoi_area_pct"] = inside_aoi_area_pct
        if outside_aoi_area_pct is not None:
            item["outside_aoi_area_pct"] = outside_aoi_area_pct
        if inside_buffer_area_pct is not None:
            item["inside_buffer_area_pct"] = inside_buffer_area_pct
        if outside_buffer_area_pct is not None:
            item["outside_buffer_area_pct"] = outside_buffer_area_pct
        if overlap_aoi_length_m is not None:
            item["overlap_aoi_length_m"] = overlap_aoi_length_m
        if overlap_buffer_length_m is not None:
            item["overlap_buffer_length_m"] = overlap_buffer_length_m
        if feature_length_m is not None:
            item["feature_length_m"] = feature_length_m
        if outside_aoi_length_m is not None:
            item["outside_aoi_length_m"] = outside_aoi_length_m
        if outside_buffer_length_m is not None:
            item["outside_buffer_length_m"] = outside_buffer_length_m
        if inside_aoi_length_pct is not None:
            item["inside_aoi_length_pct"] = inside_aoi_length_pct
        if outside_aoi_length_pct is not None:
            item["outside_aoi_length_pct"] = outside_aoi_length_pct
        if inside_buffer_length_pct is not None:
            item["inside_buffer_length_pct"] = inside_buffer_length_pct
        if outside_buffer_length_pct is not None:
            item["outside_buffer_length_pct"] = outside_buffer_length_pct

        items.append(item)
        kept += 1
        if kept >= max_items:
            break

    try:
        items.sort(
            key=lambda d: (
                float(d.get("distance_to_aoi_m")) if d.get("distance_to_aoi_m") is not None else float("inf"),
                str(d.get("name") or ""),
            )
        )
    except Exception:
        pass

    return {
        "layer_id": str(layer.id() or ""),
        "layer_name": str(layer.name() or ""),
        "selected_only": bool(selected_only),
        "selected_feature_count": int(selected_count),
        "name_field": str(name_field or ""),
        "feature_count": int(kept),
        "scanned": int(scanned),
        "counts": counts,
        "items": items,
        "max_features": int(max_items),
        "truncated": bool(kept >= max_items),
    }


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
        # Binary-ish mask hint (e.g., viewshed/corridor): only compute when data looks 0/Max-ish.
        try:
            sample = vals
            if int(sample.size) > 200_000:
                step0 = int(max(1, sample.size // 200_000))
                sample = sample[::step0]
            vmin = float(out.get("min"))
            vmax = float(out.get("max"))
            if math.isfinite(vmin) and math.isfinite(vmax) and vmax > 0 and vmin >= -1e-6 and sample.size > 0:
                near0 = float(np.count_nonzero(np.isclose(sample, 0.0, atol=1e-6)))
                nearMax = float(np.count_nonzero(np.isclose(sample, vmax, atol=1e-6)))
                frac = (near0 + nearMax) / float(sample.size)
                if frac >= 0.98:
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
    exclude_styling_layers: bool = False,
    layer_ids: Optional[List[str]] = None,
    group_path_prefix: Optional[str] = None,
    reference_layer: Optional[QgsVectorLayer] = None,
    reference_selected_only: bool = False,
    reference_name_field: str = "",
    reference_max_features: int = 120,
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

    aoi_centroid_pt = None
    try:
        c = aoi_geom.centroid()
        if c and (not c.isEmpty()):
            aoi_centroid_pt = QgsPointXY(c.asPoint())
    except Exception:
        aoi_centroid_pt = None

    group_prefix = str(group_path_prefix or "").strip().strip("/")
    if layer_ids:
        map_layers = QgsProject.instance().mapLayers()
        ordered = []
        seen = set()
        for lid in layer_ids:
            lid0 = str(lid or "").strip()
            if not lid0:
                continue
            lyr = map_layers.get(lid0)
            if lyr is None:
                continue
            if lyr.id() in seen:
                continue
            ordered.append(lyr)
            seen.add(lyr.id())
        layers = ordered
    else:
        layers = list(QgsProject.instance().mapLayers().values())
    summaries: List[Dict[str, Any]] = []

    for lyr in layers:
        if lyr is None or lyr.id() == aoi_layer.id():
            continue

        group_path = None
        if group_prefix:
            try:
                group_path = str(_layer_group_path(lyr.id()) or "")
            except Exception:
                group_path = ""
            if group_path != group_prefix and (not group_path.startswith(group_prefix + "/")):
                continue

        meta = _layer_archtoolkit_meta(lyr)

        if exclude_styling_layers:
            try:
                if str(lyr.name() or "").startswith("Style:"):
                    continue
            except Exception:
                pass
            try:
                if (meta or {}).get("tool_id") == "map_styling":
                    continue
            except Exception:
                pass

        if only_archtoolkit_layers and (not meta) and (not is_archtoolkit_layer(lyr)):
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
            "group_path": group_path if group_path is not None else _layer_group_path(lyr.id()),
        }
        if meta:
            item["archtoolkit"] = meta

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
                item["stats"] = _vector_layer_stats_in_geom(
                    lyr,
                    g_layer,
                    origin_point=aoi_centroid_pt,
                    origin_crs=aoi_crs,
                )
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

    reference_sites = None
    if reference_layer is not None and isinstance(reference_layer, QgsVectorLayer):
        try:
            reference_sites = _reference_sites_summary(
                layer=reference_layer,
                aoi_geom=aoi_geom,
                buffer_geom=buf_geom,
                aoi_centroid_pt=aoi_centroid_pt,
                aoi_crs=aoi_crs,
                selected_only=bool(reference_selected_only),
                preferred_name_field=str(reference_name_field or ""),
                max_features=int(reference_max_features),
            )
        except Exception:
            reference_sites = None

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
        "options": {
            "selected_only": bool(selected_only),
            "archtoolkit_only": bool(only_archtoolkit_layers),
            "exclude_styling_layers": bool(exclude_styling_layers),
            "layer_scope": "layers" if layer_ids else "group" if group_prefix else "auto",
            "group_path_prefix": group_prefix or None,
            "layer_ids_count": int(len(layer_ids)) if layer_ids else None,
            "reference_sites_enabled": bool(reference_sites is not None),
            "reference_selected_only": bool(reference_selected_only),
            "reference_name_field": str(reference_name_field or ""),
            "reference_max_features": int(reference_max_features),
            "max_layers": int(max_layers),
        },
    }
    if reference_sites is not None:
        ctx["reference_sites"] = reference_sites

    try:
        log_message(f"AI AOI summary: layers={len(summaries)} (archtoolkit_only={only_archtoolkit_layers})", level=Qgis.Info)
    except Exception:
        pass

    return ctx, None


def export_aoi_context_csv(
    ctx: Dict[str, Any],
    *,
    layers_csv_path: str,
    numeric_fields_csv_path: str,
) -> Optional[str]:
    """Export aoi context stats to CSV files (best-effort).

    This is designed to work without any AI provider so users can bundle
    standard stats into a report folder.
    Returns an error message on failure, else None.
    """
    if not ctx:
        return "context is empty"

    try:
        import csv
        import json
    except Exception as e:  # pragma: no cover
        return str(e)

    try:
        layers = ctx.get("layers") or []
    except Exception:
        layers = []

    try:
        out_dir = os.path.dirname(str(layers_csv_path or ""))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
    except Exception:
        pass

    try:
        out_dir = os.path.dirname(str(numeric_fields_csv_path or ""))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
    except Exception:
        pass

    def _as_text(v: Any) -> str:
        try:
            if v is None:
                return ""
            if isinstance(v, (dict, list)):
                return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            return str(v)
        except Exception:
            return ""

    # 1) Layer summary CSV (one row per layer)
    try:
        fieldnames = [
            "layer_id",
            "layer_name",
            "layer_type",
            "group_path",
            "crs",
            "provider",
            "wkb",
            "source",
            # ArchToolkit metadata
            "tool_id",
            "run_id",
            "kind",
            "units",
            "created_at",
            # Common vector stats
            "features",
            "scanned",
            "total_length_m",
            "total_area_m2",
            "top_field",
            "top_values_preview",
            "dist_to_aoi_centroid_mean_m",
            "dist_to_aoi_centroid_min_m",
            "dist_to_aoi_centroid_max_m",
            "dist_to_aoi_centroid_n",
            # Common raster stats
            "pixel_count",
            "raster_min",
            "raster_mean",
            "raster_max",
            "gt_0_5_pct",
            # Raw JSON
            "stats_json",
        ]
        with open(str(layers_csv_path), "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for lyr in layers:
                if not isinstance(lyr, dict):
                    continue
                meta = lyr.get("archtoolkit") or {}
                if not isinstance(meta, dict):
                    meta = {}
                stats = lyr.get("stats") or {}
                if not isinstance(stats, dict):
                    stats = {}

                top_preview = ""
                try:
                    tv = stats.get("top_values") or []
                    if isinstance(tv, list) and tv:
                        top_preview = ";".join([f"{d.get('value')}={d.get('count')}" for d in tv[:10] if isinstance(d, dict)])
                except Exception:
                    top_preview = ""

                dist = stats.get("dist_to_aoi_centroid_m") or {}
                if not isinstance(dist, dict):
                    dist = {}

                row = {
                    "layer_id": _as_text(lyr.get("id")),
                    "layer_name": _as_text(lyr.get("name")),
                    "layer_type": _as_text(lyr.get("type")),
                    "group_path": _as_text(lyr.get("group_path")),
                    "crs": _as_text(lyr.get("crs")),
                    "provider": _as_text(lyr.get("provider")),
                    "wkb": _as_text(lyr.get("wkb")),
                    "source": _as_text(lyr.get("source")),
                    # metadata
                    "tool_id": _as_text(meta.get("tool_id")),
                    "run_id": _as_text(meta.get("run_id")),
                    "kind": _as_text(meta.get("kind")),
                    "units": _as_text(meta.get("units")),
                    "created_at": _as_text(meta.get("created_at")),
                    # vector stats
                    "features": _as_text(stats.get("features")),
                    "scanned": _as_text(stats.get("scanned")),
                    "total_length_m": _as_text(stats.get("total_length_m")),
                    "total_area_m2": _as_text(stats.get("total_area_m2")),
                    "top_field": _as_text(stats.get("top_field")),
                    "top_values_preview": _as_text(top_preview),
                    "dist_to_aoi_centroid_mean_m": _as_text(dist.get("mean")),
                    "dist_to_aoi_centroid_min_m": _as_text(dist.get("min")),
                    "dist_to_aoi_centroid_max_m": _as_text(dist.get("max")),
                    "dist_to_aoi_centroid_n": _as_text(dist.get("n")),
                    # raster stats
                    "pixel_count": _as_text(stats.get("count")),
                    "raster_min": _as_text(stats.get("min")),
                    "raster_mean": _as_text(stats.get("mean")),
                    "raster_max": _as_text(stats.get("max")),
                    "gt_0_5_pct": _as_text(stats.get("gt_0_5_pct")),
                    # raw
                    "stats_json": _as_text(lyr.get("stats")),
                }
                w.writerow(row)
    except Exception as e:
        return str(e)

    # 2) Numeric fields CSV (long format, one row per layer-field)
    try:
        fieldnames = [
            "layer_id",
            "layer_name",
            "group_path",
            "layer_type",
            "tool_id",
            "run_id",
            "kind",
            "units",
            "field",
            "n",
            "min",
            "mean",
            "max",
        ]
        with open(str(numeric_fields_csv_path), "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for lyr in layers:
                if not isinstance(lyr, dict):
                    continue
                stats = lyr.get("stats") or {}
                if not isinstance(stats, dict):
                    continue
                num = stats.get("numeric_fields") or {}
                if not isinstance(num, dict) or not num:
                    continue

                meta = lyr.get("archtoolkit") or {}
                if not isinstance(meta, dict):
                    meta = {}

                for fname, d in num.items():
                    if not isinstance(d, dict):
                        continue
                    w.writerow(
                        {
                            "layer_id": _as_text(lyr.get("id")),
                            "layer_name": _as_text(lyr.get("name")),
                            "group_path": _as_text(lyr.get("group_path")),
                            "layer_type": _as_text(lyr.get("type")),
                            "tool_id": _as_text(meta.get("tool_id")),
                            "run_id": _as_text(meta.get("run_id")),
                            "kind": _as_text(meta.get("kind")),
                            "units": _as_text(meta.get("units")),
                            "field": _as_text(fname),
                            "n": _as_text(d.get("n")),
                            "min": _as_text(d.get("min")),
                            "mean": _as_text(d.get("mean")),
                            "max": _as_text(d.get("max")),
                        }
                    )
    except Exception as e:
        return str(e)

    return None
