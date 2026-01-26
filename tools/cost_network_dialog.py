# -*- coding: utf-8 -*-

"""
최소비용 네트워크 (Least-cost Network) - ArchToolkit

DEM 기반 이동 비용 모델을 이용해 유적(포인트/폴리곤) 간 최소비용경로(LCP)를 계산하고,
다음 네트워크를 생성합니다.
- A) MST (Minimum Spanning Tree): 전체 연결망의 총 비용 최소
- B) k-NN (k Nearest Neighbors by cost): 각 노드에서 비용 기준 상위 k개 연결
- C) Hub: 허브(왕성/산성/봉수 등) 지정 → 비허브는 가장 가까운 허브에 연결 (+허브 MST 옵션)

주의: 실제 도로/하천/토지피복을 알지 못하며, DEM 경사 기반 이동 비용만 고려합니다.
"""

import math
import os
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from osgeo import gdal

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsLineSymbol,
    QgsMapLayerProxyModel,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsRendererCategory,
    QgsTask,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)

from .cost_surface_dialog import (
    MODEL_CONOLLY_LAKE,
    MODEL_HERZOG_METABOLIC,
    MODEL_HERZOG_WHEELED,
    MODEL_NAISMITH,
    MODEL_PANDOLF,
    MODEL_TOBLER,
    _astar_path,
    _bbox_window,
    _cell_center,
    _inv_geotransform,
    _polyline_length,
    _reconstruct_path,
    _safe_layer_name_fragment,
    _window_geotransform,
)
from .utils import (
    ensure_log_panel_visible,
    is_metric_crs,
    log_message,
    push_message,
    restore_ui_focus,
    transform_point,
)


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "cost_network_dialog_base.ui")
)


NETWORK_MST = "mst"
NETWORK_KNN = "knn"
NETWORK_HUB = "hub"
NETWORK_ALL = "all"

COST_TIME = "time"
COST_ENERGY = "energy"

SYMMETRY_AVG = "avg"
SYMMETRY_MIN = "min"
SYMMETRY_MAX = "max"


def _sign(v: float, eps: float = 1e-12) -> int:
    if abs(float(v)) <= eps:
        return 0
    return 1 if v > 0 else -1


def _simplify_turn_points(coords: Optional[List[Tuple[float, float]]]) -> Optional[List[Tuple[float, float]]]:
    """Grid 기반 경로는 직선 구간이 길기 때문에 방향 전환점만 남겨서 가볍게 만듭니다."""
    if not coords or len(coords) < 3:
        return coords
    out = [coords[0]]
    prev_dir = (_sign(coords[1][0] - coords[0][0]), _sign(coords[1][1] - coords[0][1]))
    for i in range(1, len(coords) - 1):
        cur_dir = (_sign(coords[i + 1][0] - coords[i][0]), _sign(coords[i + 1][1] - coords[i][1]))
        if cur_dir != prev_dir:
            out.append(coords[i])
            prev_dir = cur_dir
    out.append(coords[-1])
    return out


def _parse_csv_values(txt: str) -> List[str]:
    vals: List[str] = []
    for raw in (txt or "").split(","):
        v = raw.strip()
        if v:
            vals.append(v)
    return vals


@dataclass
class NetworkNode:
    fid: str
    name: str
    x: float
    y: float
    is_hub: bool = False
    rank: int = 0


@dataclass
class NetworkEdge:
    a: int
    b: int
    kind: str
    coords: List[Tuple[float, float]]
    dist_m: float
    time_min_ab: Optional[float] = None
    time_min_ba: Optional[float] = None
    time_min_sym: Optional[float] = None
    energy_kcal_ab: Optional[float] = None
    energy_kcal_ba: Optional[float] = None
    energy_kcal_sym: Optional[float] = None


@dataclass
class NetworkTaskResult:
    ok: bool
    message: str = ""
    dem_authid: Optional[str] = None
    model_key: Optional[str] = None
    model_label: Optional[str] = None
    cost_mode: str = COST_TIME
    network_mode: str = NETWORK_MST
    nodes: Optional[List[NetworkNode]] = None
    edges: Optional[List[NetworkEdge]] = None


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(int(n)))
        self.rank = [0] * int(n)

    def find(self, a: int) -> int:
        a = int(a)
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a: int, b: int) -> bool:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1
        return True


class CostNetworkWorker(QgsTask):
    def __init__(
        self,
        *,
        dem_source: str,
        dem_authid: str,
        nodes: Sequence[NetworkNode],
        allow_diagonal: bool,
        pair_buffer_m: float,
        candidate_k: int,
        network_mode: str,
        knn_k: int,
        hub_connect_mst: bool,
        hierarchy_enabled: bool = False,
        sym_method: str,
        model_key: str,
        model_params: dict,
        model_label: str,
        cost_mode: str,
        on_done,
    ):
        super().__init__("최소비용 네트워크 (Least-cost Network)", QgsTask.CanCancel)
        self.dem_source = dem_source
        self.dem_authid = dem_authid
        self.nodes = list(nodes)
        self.allow_diagonal = bool(allow_diagonal)
        self.pair_buffer_m = float(pair_buffer_m)
        self.candidate_k = int(candidate_k)
        self.network_mode = str(network_mode)
        self.knn_k = int(knn_k)
        self.hub_connect_mst = bool(hub_connect_mst)
        self.hierarchy_enabled = bool(hierarchy_enabled)
        self.sym_method = str(sym_method)
        self.model_key = str(model_key)
        self.model_params = dict(model_params or {})
        self.model_label = str(model_label or "")
        self.cost_mode = str(cost_mode or COST_TIME)
        self.on_done = on_done
        self.result_obj = NetworkTaskResult(ok=False)

    def run(self):
        try:
            self.result_obj = self._run_impl()
            return bool(self.result_obj.ok)
        except Exception as e:
            self.result_obj = NetworkTaskResult(ok=False, message=str(e))
            return False

    def finished(self, result):
        try:
            if self.on_done:
                self.on_done(self.result_obj)
        except Exception as e:
            log_message(f"Network task finished callback error: {e}", level=Qgis.Warning)

    def _run_impl(self) -> NetworkTaskResult:
        log_message(
            (
                "CostNetwork: start "
                f"(mode={self.network_mode}, cost={self.cost_mode}, model={self.model_label or self.model_key}, "
                f"candidate_k={self.candidate_k}, pair_buffer_m={self.pair_buffer_m}, diagonal={self.allow_diagonal})"
            ),
            level=Qgis.Info,
        )
        ds = gdal.Open(self.dem_source, gdal.GA_ReadOnly)
        if ds is None:
            return NetworkTaskResult(ok=False, message="DEM을 GDAL로 열 수 없습니다.")

        band = ds.GetRasterBand(1)
        xsize = ds.RasterXSize
        ysize = ds.RasterYSize
        gt = ds.GetGeoTransform()
        nodata = band.GetNoDataValue()

        dx = abs(float(gt[1]))
        dy = abs(float(gt[5]))
        if dx <= 0 or dy <= 0:
            return NetworkTaskResult(ok=False, message="DEM 픽셀 크기를 확인할 수 없습니다.")

        inv = _inv_geotransform(gt)

        # Filter nodes outside DEM (or clearly on NoData)
        valid_nodes: List[NetworkNode] = []
        removed = 0
        for n in self.nodes:
            if self.isCanceled():
                return NetworkTaskResult(ok=False, message="취소됨")
            try:
                px, py = gdal.ApplyGeoTransform(inv, float(n.x), float(n.y))
                col = int(math.floor(px))
                row = int(math.floor(py))
                if not (0 <= col < xsize and 0 <= row < ysize):
                    removed += 1
                    continue
                try:
                    v = band.ReadAsArray(col, row, 1, 1)
                    if v is not None:
                        z = float(v[0, 0])
                        if (nodata is not None and z == float(nodata)) or math.isnan(z):
                            removed += 1
                            continue
                except Exception:
                    pass
                valid_nodes.append(n)
            except Exception:
                removed += 1

        if len(valid_nodes) < 2:
            return NetworkTaskResult(ok=False, message="유효한 유적 포인트가 2개 이상 필요합니다.")

        nodes = valid_nodes
        if removed:
            log_message(f"CostNetwork: filtered out {removed} node(s) outside DEM/NoData", level=Qgis.Info)
        log_message(f"CostNetwork: using {len(nodes)} node(s)", level=Qgis.Info)
        coords = np.array([(float(n.x), float(n.y)) for n in nodes], dtype=np.float64)
        n_nodes = int(coords.shape[0])

        # Candidate undirected pairs
        pair_set = set()
        candidate_pairs: List[Tuple[int, int]] = []

        def add_pair(i: int, j: int):
            a = int(i)
            b = int(j)
            if a == b:
                return
            if a > b:
                a, b = b, a
            key = (a, b)
            if key in pair_set:
                return
            pair_set.add(key)
            candidate_pairs.append(key)

        k = max(1, int(self.candidate_k))

        if self.network_mode in (NETWORK_MST, NETWORK_KNN, NETWORK_ALL):
            for i in range(n_nodes):
                if self.isCanceled():
                    return NetworkTaskResult(ok=False, message="취소됨")
                dxs = coords[:, 0] - coords[i, 0]
                dys = coords[:, 1] - coords[i, 1]
                d2 = dxs * dxs + dys * dys
                d2[i] = np.inf
                nn = np.argsort(d2)[: min(n_nodes - 1, k)]
                for j in nn:
                    add_pair(i, int(j))

        if self.network_mode in (NETWORK_HUB, NETWORK_ALL):
            hubs = [idx for idx, n in enumerate(nodes) if bool(n.is_hub)]
            if not hubs and self.network_mode == NETWORK_HUB:
                return NetworkTaskResult(ok=False, message="허브가 없습니다. 허브 필드/값을 확인하세요.")
            if not hubs:
                hubs = []

            hub_coords = coords[hubs, :] if hubs else None

            # Non-hub -> nearest hubs
            if hubs:
                for i in range(n_nodes):
                    if self.isCanceled():
                        return NetworkTaskResult(ok=False, message="취소됨")
                    if i in hubs:
                        continue
                    dxs = hub_coords[:, 0] - coords[i, 0]
                    dys = hub_coords[:, 1] - coords[i, 1]
                    d2 = dxs * dxs + dys * dys
                    nn = np.argsort(d2)[: min(len(hubs), k)]
                    for jj in nn:
                        add_pair(i, hubs[int(jj)])

            # Hub -> hub candidates (for optional MST among hubs)
            if hubs and self.hub_connect_mst and len(hubs) >= 2:
                for ii, i in enumerate(hubs):
                    dxs = hub_coords[:, 0] - hub_coords[ii, 0]
                    dys = hub_coords[:, 1] - hub_coords[ii, 1]
                    d2 = dxs * dxs + dys * dys
                    d2[ii] = np.inf
                    nn = np.argsort(d2)[: min(len(hubs) - 1, k)]
                    for jj in nn:
                        add_pair(i, hubs[int(jj)])

        if not candidate_pairs:
            return NetworkTaskResult(ok=False, message="후보 간선이 없습니다. 후보 간선(k)을 늘려주세요.")

        log_message(
            f"CostNetwork: candidate pairs={len(candidate_pairs)} (directed paths={len(candidate_pairs) * 2})",
            level=Qgis.Info,
        )

        # Internal solver cost mode
        solver_cost_mode = "energy_j" if self.cost_mode == COST_ENERGY else "time_s"
        want_paths_for_pairs = self.network_mode in (NETWORK_KNN, NETWORK_HUB, NETWORK_ALL)

        # Directed results keyed by (i, j)
        cost_dir: Dict[Tuple[int, int], float] = {}
        path_dir: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}

        max_cells = 4_000_000
        total_dir = len(candidate_pairs) * 2
        done_dir = 0
        last_bucket = -1

        def update_progress():
            nonlocal last_bucket
            try:
                pct = 90.0 * done_dir / max(1, total_dir)
                self.setProgress(pct)
                bucket = int(pct // 10.0)
                if bucket != last_bucket:
                    last_bucket = bucket
                    log_message(
                        f"CostNetwork: computing pair costs… {bucket * 10}% ({done_dir}/{total_dir})",
                        level=Qgis.Info,
                    )
            except Exception:
                pass

        for a, b in candidate_pairs:
            if self.isCanceled():
                return NetworkTaskResult(ok=False, message="취소됨")

            ax, ay = coords[a, 0], coords[a, 1]
            bx, by = coords[b, 0], coords[b, 1]
            minx = min(ax, bx) - self.pair_buffer_m
            maxx = max(ax, bx) + self.pair_buffer_m
            miny = min(ay, by) - self.pair_buffer_m
            maxy = max(ay, by) + self.pair_buffer_m

            xoff, yoff, win_xsize, win_ysize = _bbox_window(gt, xsize, ysize, minx, miny, maxx, maxy)
            cell_count = int(win_xsize * win_ysize)
            if cell_count > max_cells:
                return NetworkTaskResult(
                    ok=False,
                    message=(
                        f"후보 쌍 중 일부의 분석 창이 너무 큽니다 ({cell_count:,} cells). "
                        "경로 버퍼(m)를 줄이거나 후보 간선(k)를 줄이세요."
                    ),
                )

            dem = band.ReadAsArray(xoff, yoff, win_xsize, win_ysize)
            if dem is None:
                continue
            dem = dem.astype(np.float32, copy=False)

            nodata_mask = np.zeros(dem.shape, dtype=bool)
            if nodata is not None:
                nodata_mask |= dem == nodata
            nodata_mask |= np.isnan(dem)

            win_gt = _window_geotransform(gt, xoff, yoff)
            inv_win = _inv_geotransform(win_gt)

            a_px, a_py = gdal.ApplyGeoTransform(inv_win, float(ax), float(ay))
            b_px, b_py = gdal.ApplyGeoTransform(inv_win, float(bx), float(by))
            a_col = int(math.floor(a_px))
            a_row = int(math.floor(a_py))
            b_col = int(math.floor(b_px))
            b_row = int(math.floor(b_py))

            rows, cols = dem.shape
            if not (0 <= a_row < rows and 0 <= a_col < cols and 0 <= b_row < rows and 0 <= b_col < cols):
                continue
            if nodata_mask[a_row, a_col] or nodata_mask[b_row, b_col]:
                continue

            start_rc = (a_row, a_col)
            end_rc = (b_row, b_col)

            def cancel_check():
                return self.isCanceled()

            # A -> B
            prev_ab, cost_ab = _astar_path(
                dem,
                nodata_mask,
                start_rc,
                end_rc,
                dx,
                dy,
                self.allow_diagonal,
                self.model_key,
                self.model_params,
                cost_mode=solver_cost_mode,
                cancel_check=cancel_check,
            )
            if prev_ab is not None and cost_ab is not None and math.isfinite(float(cost_ab)):
                cost_dir[(a, b)] = float(cost_ab)
                if want_paths_for_pairs:
                    idxs = _reconstruct_path(prev_ab, start_rc, end_rc, cols, rows)
                    if idxs:
                        pts = [_cell_center(win_gt, (idx % cols), (idx // cols)) for idx in idxs]
                        pts = _simplify_turn_points(pts) or pts
                        path_dir[(a, b)] = pts
            done_dir += 1
            update_progress()

            # B -> A
            prev_ba, cost_ba = _astar_path(
                dem,
                nodata_mask,
                end_rc,
                start_rc,
                dx,
                dy,
                self.allow_diagonal,
                self.model_key,
                self.model_params,
                cost_mode=solver_cost_mode,
                cancel_check=cancel_check,
            )
            if prev_ba is not None and cost_ba is not None and math.isfinite(float(cost_ba)):
                cost_dir[(b, a)] = float(cost_ba)
                if want_paths_for_pairs:
                    idxs = _reconstruct_path(prev_ba, end_rc, start_rc, cols, rows)
                    if idxs:
                        pts = [_cell_center(win_gt, (idx % cols), (idx // cols)) for idx in idxs]
                        pts = _simplify_turn_points(pts) or pts
                        path_dir[(b, a)] = pts
            done_dir += 1
            update_progress()

        def sym_cost(a: int, b: int) -> Optional[float]:
            cab = cost_dir.get((a, b))
            cba = cost_dir.get((b, a))
            if cab is None or cba is None:
                return None
            if not math.isfinite(float(cab)) or not math.isfinite(float(cba)):
                return None
            if self.sym_method == SYMMETRY_MIN:
                return float(min(cab, cba))
            if self.sym_method == SYMMETRY_MAX:
                return float(max(cab, cba))
            return float(0.5 * (float(cab) + float(cba)))

        edges_out: List[NetworkEdge] = []

        def add_edge(kind: str, a: int, b: int, geom: List[Tuple[float, float]]):
            if not geom or len(geom) < 2:
                return
            dist_m = float(_polyline_length(geom) or 0.0)
            cab = cost_dir.get((a, b))
            cba = cost_dir.get((b, a))
            sym = sym_cost(a, b)
            if solver_cost_mode == "time_s":
                edges_out.append(
                    NetworkEdge(
                        a=a,
                        b=b,
                        kind=kind,
                        coords=geom,
                        dist_m=dist_m,
                        time_min_ab=(float(cab) / 60.0) if cab is not None else None,
                        time_min_ba=(float(cba) / 60.0) if cba is not None else None,
                        time_min_sym=(float(sym) / 60.0) if sym is not None else None,
                    )
                )
            else:
                v = max(0.05, float(self.model_params.get("pandolf_speed_mps", 5.0 * 1000.0 / 3600.0)))
                edges_out.append(
                    NetworkEdge(
                        a=a,
                        b=b,
                        kind=kind,
                        coords=geom,
                        dist_m=dist_m,
                        time_min_ab=float(dist_m) / v / 60.0,
                        time_min_ba=float(dist_m) / v / 60.0,
                        time_min_sym=float(dist_m) / v / 60.0,
                        energy_kcal_ab=(float(cab) / 4184.0) if cab is not None else None,
                        energy_kcal_ba=(float(cba) / 4184.0) if cba is not None else None,
                        energy_kcal_sym=(float(sym) / 4184.0) if sym is not None else None,
                    )
                )

        # --- A) MST ---
        if self.network_mode == NETWORK_MST:
            weighted = []
            for a, b in candidate_pairs:
                w = sym_cost(a, b)
                if w is None:
                    continue
                weighted.append((w, a, b))
            weighted.sort(key=lambda t: t[0])

            uf = _UnionFind(n_nodes)
            chosen: List[Tuple[int, int]] = []
            for w, a, b in weighted:
                if uf.union(a, b):
                    chosen.append((a, b))
                    if len(chosen) >= n_nodes - 1:
                        break
            if len(chosen) < n_nodes - 1:
                return NetworkTaskResult(
                    ok=False,
                    message=(
                        "MST를 구성할 수 없습니다(그래프가 끊겨 있음). "
                        "후보 간선(k)를 늘리거나 경로 버퍼(m)를 늘려주세요."
                    ),
                )

            log_message(
                f"CostNetwork: MST selected {len(chosen)} edge(s); computing detailed paths…",
                level=Qgis.Info,
            )
            mst_done = 0
            mst_total = max(1, len(chosen))
            mst_last_bucket = -1

            # MST는 선택된 간선만 경로를 다시 계산(메모리 절약)
            for a, b in chosen:
                if self.isCanceled():
                    return NetworkTaskResult(ok=False, message="취소됨")

                mst_done += 1
                try:
                    pct = 90.0 + 10.0 * (mst_done / mst_total)
                    self.setProgress(pct)
                    bucket = int((100.0 * mst_done / mst_total) // 25.0)
                    if bucket != mst_last_bucket:
                        mst_last_bucket = bucket
                        log_message(
                            f"CostNetwork: MST paths… {int(100.0 * mst_done / mst_total)}% ({mst_done}/{mst_total})",
                            level=Qgis.Info,
                        )
                except Exception:
                    pass

                ax, ay = coords[a, 0], coords[a, 1]
                bx, by = coords[b, 0], coords[b, 1]
                minx = min(ax, bx) - self.pair_buffer_m
                maxx = max(ax, bx) + self.pair_buffer_m
                miny = min(ay, by) - self.pair_buffer_m
                maxy = max(ay, by) + self.pair_buffer_m
                xoff, yoff, win_xsize, win_ysize = _bbox_window(gt, xsize, ysize, minx, miny, maxx, maxy)
                dem = band.ReadAsArray(xoff, yoff, win_xsize, win_ysize)
                if dem is None:
                    continue
                dem = dem.astype(np.float32, copy=False)
                nodata_mask = np.zeros(dem.shape, dtype=bool)
                if nodata is not None:
                    nodata_mask |= dem == nodata
                nodata_mask |= np.isnan(dem)
                win_gt = _window_geotransform(gt, xoff, yoff)
                inv_win = _inv_geotransform(win_gt)
                a_px, a_py = gdal.ApplyGeoTransform(inv_win, float(ax), float(ay))
                b_px, b_py = gdal.ApplyGeoTransform(inv_win, float(bx), float(by))
                a_col = int(math.floor(a_px))
                a_row = int(math.floor(a_py))
                b_col = int(math.floor(b_px))
                b_row = int(math.floor(b_py))
                rows, cols = dem.shape
                if not (0 <= a_row < rows and 0 <= a_col < cols and 0 <= b_row < rows and 0 <= b_col < cols):
                    continue
                if nodata_mask[a_row, a_col] or nodata_mask[b_row, b_col]:
                    continue
                start_rc = (a_row, a_col)
                end_rc = (b_row, b_col)
                prev, cost_ab = _astar_path(
                    dem,
                    nodata_mask,
                    start_rc,
                    end_rc,
                    dx,
                    dy,
                    self.allow_diagonal,
                    self.model_key,
                    self.model_params,
                    cost_mode=solver_cost_mode,
                    cancel_check=lambda: self.isCanceled(),
                )
                if prev is None or cost_ab is None:
                    continue
                idxs = _reconstruct_path(prev, start_rc, end_rc, cols, rows)
                if not idxs:
                    continue
                pts = [_cell_center(win_gt, (idx % cols), (idx // cols)) for idx in idxs]
                pts = _simplify_turn_points(pts) or pts
                dist_m = float(_polyline_length(pts) or 0.0)

                cab = cost_dir.get((a, b))
                cba = cost_dir.get((b, a))
                sym = sym_cost(a, b)

                if solver_cost_mode == "time_s":
                    edges_out.append(
                        NetworkEdge(
                            a=a,
                            b=b,
                            kind="mst",
                            coords=pts,
                            dist_m=dist_m,
                            time_min_ab=(float(cab) / 60.0) if cab is not None else None,
                            time_min_ba=(float(cba) / 60.0) if cba is not None else None,
                            time_min_sym=(float(sym) / 60.0) if sym is not None else None,
                        )
                    )
                else:
                    v = max(
                        0.05,
                        float(self.model_params.get("pandolf_speed_mps", 5.0 * 1000.0 / 3600.0)),
                    )
                    edges_out.append(
                        NetworkEdge(
                            a=a,
                            b=b,
                            kind="mst",
                            coords=pts,
                            dist_m=dist_m,
                            time_min_ab=float(dist_m) / v / 60.0,
                            time_min_ba=float(dist_m) / v / 60.0,
                            time_min_sym=float(dist_m) / v / 60.0,
                            energy_kcal_ab=(float(cab) / 4184.0) if cab is not None else None,
                            energy_kcal_ba=(float(cba) / 4184.0) if cba is not None else None,
                            energy_kcal_sym=(float(sym) / 4184.0) if sym is not None else None,
                        )
                    )

        # --- B) k-NN ---
        elif self.network_mode == NETWORK_KNN:
            k_sel = max(1, int(self.knn_k))
            selected_dir = set()
            for i in range(n_nodes):
                outs = []
                for a, b in candidate_pairs:
                    if a == i:
                        outs.append(b)
                    elif b == i:
                        outs.append(a)
                cand = []
                for j in outs:
                    c = cost_dir.get((i, j))
                    if c is None or not math.isfinite(float(c)):
                        continue
                    cand.append((float(c), j))
                cand.sort(key=lambda t: t[0])
                for _, j in cand[:k_sel]:
                    selected_dir.add((i, j))

            undirected = set()
            for i, j in selected_dir:
                a, b = (i, j) if i < j else (j, i)
                undirected.add((a, b))

            for a, b in sorted(undirected):
                geom = path_dir.get((a, b)) or path_dir.get((b, a))
                if not geom:
                    continue
                dist_m = float(_polyline_length(geom) or 0.0)
                cab = cost_dir.get((a, b))
                cba = cost_dir.get((b, a))
                sym = sym_cost(a, b)
                if solver_cost_mode == "time_s":
                    edges_out.append(
                        NetworkEdge(
                            a=a,
                            b=b,
                            kind="knn",
                            coords=geom,
                            dist_m=dist_m,
                            time_min_ab=(float(cab) / 60.0) if cab is not None else None,
                            time_min_ba=(float(cba) / 60.0) if cba is not None else None,
                            time_min_sym=(float(sym) / 60.0) if sym is not None else None,
                        )
                    )
                else:
                    v = max(
                        0.05,
                        float(self.model_params.get("pandolf_speed_mps", 5.0 * 1000.0 / 3600.0)),
                    )
                    edges_out.append(
                        NetworkEdge(
                            a=a,
                            b=b,
                            kind="knn",
                            coords=geom,
                            dist_m=dist_m,
                            time_min_ab=float(dist_m) / v / 60.0,
                            time_min_ba=float(dist_m) / v / 60.0,
                            time_min_sym=float(dist_m) / v / 60.0,
                            energy_kcal_ab=(float(cab) / 4184.0) if cab is not None else None,
                            energy_kcal_ba=(float(cba) / 4184.0) if cba is not None else None,
                            energy_kcal_sym=(float(sym) / 4184.0) if sym is not None else None,
                        )
                    )

        # --- A+B+C (All) ---
        elif self.network_mode == NETWORK_ALL:
            # A) MST
            weighted = []
            for a, b in candidate_pairs:
                w = sym_cost(a, b)
                if w is None:
                    continue
                weighted.append((w, a, b))
            weighted.sort(key=lambda t: t[0])

            uf = _UnionFind(n_nodes)
            chosen: List[Tuple[int, int]] = []
            for w, a, b in weighted:
                if uf.union(a, b):
                    chosen.append((a, b))
                    if len(chosen) >= n_nodes - 1:
                        break
            if len(chosen) < n_nodes - 1:
                return NetworkTaskResult(
                    ok=False,
                    message=(
                        "MST를 구성할 수 없습니다(그래프가 끊겨 있음). "
                        "후보 간선(k)를 늘리거나 경로 버퍼(m)를 늘려주세요."
                    ),
                )

            for a, b in chosen:
                geom = path_dir.get((a, b)) or path_dir.get((b, a))
                if geom:
                    add_edge("mst", a, b, geom)

            # B) k-NN
            k_sel = max(1, int(self.knn_k))
            selected_dir = set()
            for i in range(n_nodes):
                outs = []
                for a, b in candidate_pairs:
                    if a == i:
                        outs.append(b)
                    elif b == i:
                        outs.append(a)
                cand = []
                for j in outs:
                    c = cost_dir.get((i, j))
                    if c is None or not math.isfinite(float(c)):
                        continue
                    cand.append((float(c), j))
                cand.sort(key=lambda t: t[0])
                for _, j in cand[:k_sel]:
                    selected_dir.add((i, j))

            undirected = set()
            for i, j in selected_dir:
                a, b = (i, j) if i < j else (j, i)
                undirected.add((a, b))

            for a, b in sorted(undirected):
                geom = path_dir.get((a, b)) or path_dir.get((b, a))
                if geom:
                    add_edge("knn", a, b, geom)

            # C) Hub (optional)
            hubs = {idx for idx, n in enumerate(nodes) if bool(n.is_hub)}
            if hubs:
                hub_links = set()
                for i in range(n_nodes):
                    if i in hubs:
                        continue
                    best = None
                    best_h = None
                    for h in hubs:
                        c = cost_dir.get((i, h))
                        if c is None or not math.isfinite(float(c)):
                            continue
                        if best is None or float(c) < best:
                            best = float(c)
                            best_h = int(h)
                    if best_h is None:
                        continue
                    a, b = (i, best_h) if i < best_h else (best_h, i)
                    hub_links.add((a, b))

                hub_mst = set()
                if self.hub_connect_mst and len(hubs) >= 2:
                    hub_list = sorted(hubs)
                    hub_index = {h: idx for idx, h in enumerate(hub_list)}
                    weighted = []
                    for a, b in candidate_pairs:
                        if a in hubs and b in hubs:
                            w = sym_cost(a, b)
                            if w is not None:
                                weighted.append((w, a, b))
                    weighted.sort(key=lambda t: t[0])
                    uf = _UnionFind(len(hub_list))
                    for w, a, b in weighted:
                        if uf.union(hub_index[a], hub_index[b]):
                            aa, bb = (a, b) if a < b else (b, a)
                            hub_mst.add((aa, bb))
                            if len(hub_mst) >= len(hub_list) - 1:
                                break

                for a, b in sorted(hub_links):
                    geom = path_dir.get((a, b)) or path_dir.get((b, a))
                    if geom:
                        add_edge("hub_link", a, b, geom)
                for a, b in sorted(hub_mst):
                    geom = path_dir.get((a, b)) or path_dir.get((b, a))
                    if geom:
                        add_edge("hub_mst", a, b, geom)

        # --- C) Hub ---
        else:
            hubs = {idx for idx, n in enumerate(nodes) if bool(n.is_hub)}
            if not hubs:
                return NetworkTaskResult(ok=False, message="허브가 없습니다. 허브 필드/값을 확인하세요.")

            hub_links = set()
            for i in range(n_nodes):
                if i in hubs:
                    continue
                best = None
                best_h = None
                for h in hubs:
                    c = cost_dir.get((i, h))
                    if c is None or not math.isfinite(float(c)):
                        continue
                    if best is None or float(c) < best:
                        best = float(c)
                        best_h = int(h)
                if best_h is None:
                    continue
                a, b = (i, best_h) if i < best_h else (best_h, i)
                hub_links.add((a, b))

            hub_mst = set()
            if self.hub_connect_mst and len(hubs) >= 2:
                hub_list = sorted(hubs)
                hub_index = {h: idx for idx, h in enumerate(hub_list)}
                weighted = []
                for a, b in candidate_pairs:
                    if a in hubs and b in hubs:
                        w = sym_cost(a, b)
                        if w is not None:
                            weighted.append((w, a, b))
                weighted.sort(key=lambda t: t[0])
                uf = _UnionFind(len(hub_list))
                for w, a, b in weighted:
                    if uf.union(hub_index[a], hub_index[b]):
                        aa, bb = (a, b) if a < b else (b, a)
                        hub_mst.add((aa, bb))
                        if len(hub_mst) >= len(hub_list) - 1:
                            break

            all_edges = []
            for a, b in sorted(hub_links):
                all_edges.append((a, b, "hub_link"))
            for a, b in sorted(hub_mst):
                all_edges.append((a, b, "hub_mst"))

            for a, b, kind in all_edges:
                geom = path_dir.get((a, b)) or path_dir.get((b, a))
                if not geom:
                    continue
                dist_m = float(_polyline_length(geom) or 0.0)
                cab = cost_dir.get((a, b))
                cba = cost_dir.get((b, a))
                sym = sym_cost(a, b)
                if solver_cost_mode == "time_s":
                    edges_out.append(
                        NetworkEdge(
                            a=a,
                            b=b,
                            kind=kind,
                            coords=geom,
                            dist_m=dist_m,
                            time_min_ab=(float(cab) / 60.0) if cab is not None else None,
                            time_min_ba=(float(cba) / 60.0) if cba is not None else None,
                            time_min_sym=(float(sym) / 60.0) if sym is not None else None,
                        )
                    )
                else:
                    v = max(
                        0.05,
                        float(self.model_params.get("pandolf_speed_mps", 5.0 * 1000.0 / 3600.0)),
                    )
                    edges_out.append(
                        NetworkEdge(
                            a=a,
                            b=b,
                            kind=kind,
                            coords=geom,
                            dist_m=dist_m,
                            time_min_ab=float(dist_m) / v / 60.0,
                            time_min_ba=float(dist_m) / v / 60.0,
                            time_min_sym=float(dist_m) / v / 60.0,
                            energy_kcal_ab=(float(cab) / 4184.0) if cab is not None else None,
                            energy_kcal_ba=(float(cba) / 4184.0) if cba is not None else None,
                            energy_kcal_sym=(float(sym) / 4184.0) if sym is not None else None,
                        )
                    )

        msg = f"노드 {len(nodes)}개 / 간선 {len(edges_out)}개 생성"
        if removed:
            msg = f"{msg} (DEM 범위/NoData로 {removed}개 제외)"

        try:
            self.setProgress(100.0)
        except Exception:
            pass
        log_message(f"CostNetwork: done ({msg})", level=Qgis.Info)

        return NetworkTaskResult(
            ok=True,
            message=msg,
            dem_authid=self.dem_authid,
            model_key=self.model_key,
            model_label=self.model_label,
            cost_mode=self.cost_mode,
            network_mode=self.network_mode,
            nodes=nodes,
            edges=edges_out,
        )


class CostNetworkDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.iface = iface

        try:
            self._wrap_in_scroll_area()
        except Exception:
            pass

        # Make DEM selector a bit more compact.
        try:
            dem_layout = getattr(self, "verticalLayout_Dem", None)
            if dem_layout is not None:
                dem_layout.setContentsMargins(6, 2, 6, 2)
                dem_layout.setSpacing(2)
        except Exception:
            pass

        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            network_icon = None
            for icon_name in ("network_icon.png", "network_icon.jpg", "network_icon.jpeg"):
                p = os.path.join(plugin_dir, icon_name)
                if os.path.exists(p):
                    network_icon = p
                    break
            fallback_icon = os.path.join(plugin_dir, "cost_icon.png")
            if os.path.exists(network_icon or fallback_icon):
                self.setWindowIcon(QIcon(network_icon or fallback_icon))
        except Exception:
            pass

        self._task = None
        self._task_running = False

        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.cmbSiteLayer.setFilters(QgsMapLayerProxyModel.VectorLayer)

        # Populate combos
        self.cmbPolyPointMode.clear()
        self.cmbPolyPointMode.addItem("표면상 점 (Point on surface, 권장)", "surface")
        self.cmbPolyPointMode.addItem("중심점 (Centroid)", "centroid")

        self.cmbNetworkMode.clear()

        mode_items = [
            (
                "A. 최소 신장 트리 (MST)",
                NETWORK_MST,
                (
                    "최소 신장 트리 (MST)\n"
                    "- 목적: 모든 유적을 '총 비용 합'이 최소가 되도록 연결\n"
                    "- 간선 수: N-1 (딱 필요한 만큼)\n"
                    "- 해석: 최소 골격(backbone) 네트워크\n"
                    "Ref: Kruskal (1956); Prim (1957)"
                ),
            ),
            (
                "B. k-최근접 네트워크 (k-NN)",
                NETWORK_KNN,
                (
                    "k-최근접 네트워크 (k-NN graph)\n"
                    "- 목적: 각 노드에서 비용이 작은 상위 k개와 연결\n"
                    "- 특징: 복수 경로/연락망 형태, k가 작으면 끊길 수 있음\n"
                    "Ref: k-nearest neighbor graph (graph theory / computational geometry)"
                ),
            ),
            (
                "C. 허브 기반 네트워크 (Hub)",
                NETWORK_HUB,
                (
                    "허브 기반 (Hub)\n"
                    "- 목적: 특정 거점(허브) 중심으로 연결\n"
                    "- 방법: 비허브 → 비용이 가장 작은 허브에 연결 (+허브 MST 옵션)\n"
                    "- 활용: 왕성/산성/봉수 등 계층형 네트워크 가정\n"
                    "Ref: hub-and-spoke network (transport network design)"
                ),
            ),
            (
                "A+B+C. 한번에 생성 (All: MST + k-NN + Hub)",
                NETWORK_ALL,
                (
                    "A+B+C (All)\n"
                    "- MST + k-NN + Hub를 한 번에 생성\n"
                    "- 같은 비용모델/파라미터에서 결과를 바로 비교할 때 유용\n"
                    "- Hub는 허브 필드/값을 지정했을 때만 생성"
                ),
            ),
        ]
        for label, key, tip in mode_items:
            self.cmbNetworkMode.addItem(label, key)
            idx = self.cmbNetworkMode.count() - 1
            self.cmbNetworkMode.setItemData(idx, tip, Qt.ToolTipRole)

        # Item tooltips are stored in Qt.ToolTipRole; let Qt handle showing them.

        self.cmbCostMode.clear()
        self.cmbCostMode.addItem("시간(분) (Time, min)", COST_TIME)
        self.cmbCostMode.addItem("에너지(kcal) (Energy, kcal) - Pandolf만", COST_ENERGY)

        self.cmbSymmetrize.clear()
        self.cmbSymmetrize.addItem("MST 대칭화: 왕복 평균 (Round-trip mean)", SYMMETRY_AVG)
        self.cmbSymmetrize.addItem("MST 대칭화: 편도 최소 (One-way min)", SYMMETRY_MIN)
        self.cmbSymmetrize.addItem("MST 대칭화: 편도 최대 (One-way max)", SYMMETRY_MAX)

        self._init_models()
        self.cmbModel.currentIndexChanged.connect(self._on_model_changed)
        self._on_model_changed()

        self.cmbNetworkMode.currentIndexChanged.connect(self._on_mode_changed)
        self._on_mode_changed()

        self.cmbSiteLayer.layerChanged.connect(self._on_site_layer_changed)
        self._on_site_layer_changed()

        try:
            self.btnPickHubValues.clicked.connect(self._pick_hub_values)
        except Exception:
            pass

        self.btnRun.clicked.connect(self.run_analysis)
        self.btnClose.clicked.connect(self.reject)

        try:
            self._apply_help_texts()
        except Exception:
            pass

    def reject(self):
        self._cleanup_for_close()
        super().reject()

    def closeEvent(self, event):
        self._cleanup_for_close()
        event.accept()

    def _cleanup_for_close(self):
        # Best-effort: cancel background task so it can't outlive the dialog.
        try:
            if self._task_running and self._task is not None:
                try:
                    self._task.cancel()
                except Exception:
                    pass
        except Exception:
            pass
        self._task_running = False
        self._task = None

    def _wrap_in_scroll_area(self):
        # Keep bottom buttons always visible, and make the long form scrollable.
        layout = getattr(self, "verticalLayout", None)
        if layout is None:
            return

        content = []
        for name in ("groupInfo", "groupDem", "groupInput", "groupNetwork", "groupModel"):
            w = getattr(self, name, None)
            if w is not None:
                content.append(w)
        if not content:
            return

        scroll = QtWidgets.QScrollArea(self)
        scroll.setObjectName("scrollArea_Main")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QtWidgets.QWidget(scroll)
        container_layout = QtWidgets.QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(3)

        for w in content:
            try:
                layout.removeWidget(w)
            except Exception:
                pass
            container_layout.addWidget(w)

        container_layout.addStretch(1)
        scroll.setWidget(container)

        try:
            layout.insertWidget(0, scroll, 1)
        except Exception:
            layout.insertWidget(0, scroll)

    def _apply_help_texts(self):
        # High-level description (keep it short; details go into tooltips)
        self.lblDescription.setText(
            "<html><head/><body>"
            "<p><b>최소비용 네트워크(Least-cost Network)</b>는 DEM 경사 기반 이동비용(시간/에너지)으로 "
            "유적 간 <b>최소비용경로(LCP)</b>를 계산하고, 그 결과로 네트워크를 만듭니다.</p>"
            "<p>네트워크 방식: <b>MST</b>(전체 연결망 최소), <b>k-NN</b>(복수 경로), <b>Hub</b>(거점 기반), "
            "<b>A+B+C(All)</b>(한 번에 생성).</p>"
            "<p style='color:#444;'>유형/위계(예: 왕성·빈전·고분군)별 비교는 ‘선택된 피처만’으로 원하는 조합을 선택해 "
            "여러 번 실행하면 해석이 쉽습니다.</p>"
            "</body></html>"
        )
        try:
            ss = self.lblDescription.styleSheet() or ""
            if "font-size" not in ss:
                self.lblDescription.setStyleSheet(ss + " font-size: 8pt;")
        except Exception:
            pass

        self.lblInputHelp.setText(
            "<html>"
            "• <b>포인트</b>: 그대로 노드로 사용합니다.<br/>"
            "• <b>폴리곤</b>: 대표점(권장: Point on surface)으로 노드를 만듭니다.<br/>"
            "• <b>선택 피처만</b> 체크 시: 선택된 피처만 네트워크에 포함됩니다."
            "</html>"
        )
        self.chkSelectedOnly.setToolTip(
            "선택된 피처만 사용\n"
            "- 유형/위계(예: 왕성·빈전·고분군) 비교 분석에 유용합니다.\n"
            "  1) 속성테이블에서 원하는 유형(값) 조합을 선택\n"
            "  2) 이 옵션을 체크\n"
            "  3) MST 또는 A+B+C(All) 실행\n"
            "- 조합을 바꿔 여러 번 실행하면 층간/층내 연결망을 비교할 수 있습니다."
        )

        self.lblNetworkHelp.setText(
            "<html>"
            "• <b>후보 간선(k)</b>: 유클리드 기준으로 후보를 줄여 계산을 빠르게 합니다. "
            "너무 작으면 연결이 끊겨 MST가 실패할 수 있습니다.<br/>"
            "• <b>경로 버퍼(m)</b>: 각 후보쌍 LCP 계산창(bbox)에 여유를 줍니다. "
            "너무 작으면 최적 경로가 창 밖으로 나가 실패할 수 있습니다.<br/>"
            "• <b>대칭화(MST)</b>: 오르막/내리막 차이로 A→B와 B→A 비용이 다를 수 있어, "
            "MST는 (평균/최소/최대)로 한 값으로 만듭니다.<br/>"
            "• <b>A+B+C(All)</b>: MST/k-NN/Hub를 한 번에 생성합니다(Hub는 허브 값 설정 시)."
            "</html>"
        )

        # Network parameters tooltips
        self.spinCandidateK.setToolTip(
            "후보 간선(k)\n"
            "- 각 노드에서 유클리드 거리로 가까운 k개만 후보로 잡고 LCP를 계산합니다.\n"
            "- 값이 작을수록 빠르지만, 그래프가 끊겨 MST가 실패할 수 있습니다.\n"
            "- 200개+ 노드에서는 8~20부터 시도 후, 실패하면 k를 늘려보세요."
        )
        self.spinPairBuffer.setToolTip(
            "경로 버퍼(m)\n"
            "- 후보쌍 두 점을 감싸는 bbox에 추가로 여유를 주는 값입니다.\n"
            "- 값이 너무 작으면 '진짜 최적 경로'가 창 밖으로 나가 경로가 끊길 수 있습니다.\n"
            "- 0은 DEM 전체를 사용(매우 느림)하므로 권장하지 않습니다."
        )
        self.chkDiagonal.setToolTip(
            "대각 이동 허용(8방향)\n"
            "- 격자 기반 경로에서 '계단 현상'을 줄이고 더 자연스러운 경로가 나올 수 있습니다.\n"
            "- 필요하면 꺼서(4방향) 비교해보세요."
        )
        self.cmbSymmetrize.setToolTip(
            "MST 대칭화\n"
            "- 경사 때문에 A→B와 B→A 비용이 달라질 수 있습니다.\n"
            "- MST는 무방향 그래프가 필요하므로 한 값으로 합칩니다.\n"
            "  • 왕복 평균: (A→B + B→A)/2\n"
            "  • 편도 최소: min(A→B, B→A)\n"
            "  • 편도 최대: max(A→B, B→A)"
        )
        self.spinKnnK.setToolTip(
            "k‑NN의 k\n"
            "- 각 노드에서 비용이 작은 상위 k개 노드로 연결합니다.\n"
            "- k가 작으면 네트워크가 끊길 수 있고, k가 크면 선이 많아집니다."
        )
        self.cmbNetworkMode.setToolTip("네트워크 방식(드롭다운 항목에 마우스를 올리면 설명/레퍼런스가 표시됩니다).")
        self.cmbCostMode.setToolTip(
            "비용 기준\n"
            "- 시간(분): 대부분 모델에서 사용\n"
            "- 에너지(kcal): Pandolf 모델에서만 의미가 있습니다."
        )

        # Hub UI tooltips
        self.cmbHubField.setToolTip(
            "허브 필드\n"
            "- 허브를 구분할 필드(예: 유형/등급/분류)를 선택합니다.\n"
            "- 예: '유형' 필드에서 값이 '왕성'인 피처만 허브로 지정"
        )
        self.txtHubValues.setToolTip(
            "허브 값(쉼표로 구분)\n"
            "- 예: 왕성, 산성, 봉수\n"
            "- 오른쪽 '선택…' 버튼으로 필드의 실제 값 목록에서 고를 수 있습니다."
        )
        try:
            self.btnPickHubValues.setToolTip("허브 필드의 고유 값을 목록에서 선택합니다.")
        except Exception:
            pass

        # Model parameter tooltips (hover on both label and spinbox)
        def tt(w, text: str):
            try:
                w.setToolTip(text)
            except Exception:
                pass

        tt(self.cmbModel, "모델을 선택하면 아래 변수들이 해당 모델에 맞게 적용됩니다.")

        # Tobler
        tt(
            self.lblToblerBaseSpeed,
            "기본속도(km/h)\n- 평지 기준 속도입니다.\n- 값↑ → 전체 이동 시간이 감소합니다.",
        )
        tt(
            self.spinToblerBaseKmh,
            "기본속도(km/h)\n- 평지 기준 속도입니다.\n- 값↑ → 전체 이동 시간이 감소합니다.",
        )
        tt(
            self.lblToblerSlopeFactor,
            "경사 민감도\n- 경사가 변할 때 속도가 얼마나 빨리 감소하는지 결정합니다.\n- 값↑ → 가파를수록 더 느려집니다.",
        )
        tt(
            self.spinToblerSlopeFactor,
            "경사 민감도\n- 경사가 변할 때 속도가 얼마나 빨리 감소하는지 결정합니다.\n- 값↑ → 가파를수록 더 느려집니다.",
        )
        tt(
            self.lblToblerOffset,
            "오프셋(+)\n- Tobler 식의 상수(기본값 0.05)로, 최적 경사 위치를 미세 조정합니다.\n- 값 변화는 결과에 미세하게 반영됩니다.",
        )
        tt(
            self.spinToblerOffset,
            "오프셋(+)\n- Tobler 식의 상수(기본값 0.05)로, 최적 경사 위치를 미세 조정합니다.\n- 값 변화는 결과에 미세하게 반영됩니다.",
        )

        # Naismith
        tt(
            self.lblNaismithSpeed,
            "수평 속도(km/h)\n- 평지 기준 보행 속도입니다.\n- 값↑ → 전체 이동 시간이 감소합니다.",
        )
        tt(
            self.spinNaismithSpeedKmh,
            "수평 속도(km/h)\n- 평지 기준 보행 속도입니다.\n- 값↑ → 전체 이동 시간이 감소합니다.",
        )
        tt(
            self.lblNaismithAscent,
            "상승(m/h)\n- 상승 속도(오르막 보정)입니다.\n- 값↑ → 오르막 페널티가 감소(더 빨리 오름)합니다.",
        )
        tt(
            self.spinNaismithAscentMph,
            "상승(m/h)\n- 상승 속도(오르막 보정)입니다.\n- 값↑ → 오르막 페널티가 감소(더 빨리 오름)합니다.",
        )

        # Herzog metabolic (via Cuckovic)
        tt(
            self.lblHerzogBaseSpeed,
            "기본 속도(km/h)\n- 평지 기준 속도입니다.\n- 값↑ → 전체 이동 시간이 감소합니다.",
        )
        tt(
            self.spinHerzogBaseKmh,
            "기본 속도(km/h)\n- 평지 기준 속도입니다.\n- 값↑ → 전체 이동 시간이 감소합니다.",
        )

        # Conolly & Lake
        tt(
            self.lblConollyBaseSpeed,
            "기본 속도(km/h)\n- 평지(경사 0) 기준 속도입니다.\n- 값↑ → 전체 이동 시간이 감소합니다.",
        )
        tt(
            self.spinConollyBaseKmh,
            "기본 속도(km/h)\n- 평지(경사 0) 기준 속도입니다.\n- 값↑ → 전체 이동 시간이 감소합니다.",
        )
        tt(
            self.lblConollyRefSlope,
            "기준 경사(°)\n- 비용 곡선의 기준점(민감도 기준)을 정합니다.\n- 값 변화에 따라 경사 페널티가 달라집니다.",
        )
        tt(
            self.spinConollyRefSlopeDeg,
            "기준 경사(°)\n- 비용 곡선의 기준점(민감도 기준)을 정합니다.\n- 값 변화에 따라 경사 페널티가 달라집니다.",
        )

        # Herzog wheeled (via Cuckovic)
        tt(
            self.lblWheeledBaseSpeed,
            "기본 속도(km/h)\n- 평지 기준 속도입니다.\n- 값↑ → 전체 이동 시간이 감소합니다.",
        )
        tt(
            self.spinWheeledBaseKmh,
            "기본 속도(km/h)\n- 평지 기준 속도입니다.\n- 값↑ → 전체 이동 시간이 감소합니다.",
        )
        tt(
            self.lblWheeledCriticalSlope,
            "기준 경사(°)\n- 이 값 이후로 비용이 급격히 증가하기 시작합니다.\n- 값↑ → 더 가파른 경사까지 '급증 전'으로 취급됩니다.",
        )
        tt(
            self.spinWheeledCriticalSlopeDeg,
            "기준 경사(°)\n- 이 값 이후로 비용이 급격히 증가하기 시작합니다.\n- 값↑ → 더 가파른 경사까지 '급증 전'으로 취급됩니다.",
        )
        tt(
            self.lblWheeledMaxSlope,
            "통행한계(°)\n- 이 경사를 초과하는 셀은 통과 불가(NoData)로 처리합니다.\n- 값↓ → 통과 불가 영역이 늘어납니다.",
        )
        tt(
            self.spinWheeledMaxSlopeDeg,
            "통행한계(°)\n- 이 경사를 초과하는 셀은 통과 불가(NoData)로 처리합니다.\n- 값↓ → 통과 불가 영역이 늘어납니다.",
        )

        # Pandolf
        tt(
            self.lblPandolfBody,
            "체중(kg)\n- 보행자 체중입니다.\n- 값↑ → 에너지 소모(kcal)가 증가합니다.",
        )
        tt(
            self.spinPandolfBodyKg,
            "체중(kg)\n- 보행자 체중입니다.\n- 값↑ → 에너지 소모(kcal)가 증가합니다.",
        )
        tt(
            self.lblPandolfLoad,
            "짐(kg)\n- 운반 짐 무게입니다.\n- 값↑ → 에너지 소모(kcal)가 증가합니다.",
        )
        tt(
            self.spinPandolfLoadKg,
            "짐(kg)\n- 운반 짐 무게입니다.\n- 값↑ → 에너지 소모(kcal)가 증가합니다.",
        )
        tt(
            self.lblPandolfSpeed,
            "속도(km/h)\n- 에너지 식 + 시간 환산(분/거리) 계산에 사용합니다.\n- 값↑ → 시간은 감소하지만 에너지는 항상 감소하지 않을 수 있습니다.",
        )
        tt(
            self.spinPandolfSpeedKmh,
            "속도(km/h)\n- 에너지 식 + 시간 환산(분/거리) 계산에 사용합니다.\n- 값↑ → 시간은 감소하지만 에너지는 항상 감소하지 않을 수 있습니다.",
        )
        tt(
            self.lblPandolfTerrain,
            "지면계수 η\n- 지면/마찰 계수(η). 1.0=단단한 지면, 값↑ → 같은 경사에서도 더 비싸짐.\n- 예: 1.0(도로/평탄) ~ 2.0+(거친 지면/진흙 등)",
        )
        tt(
            self.spinPandolfTerrainFactor,
            "지면계수 η\n- 지면/마찰 계수(η). 1.0=단단한 지면, 값↑ → 같은 경사에서도 더 비싸짐.\n- 예: 1.0(도로/평탄) ~ 2.0+(거친 지면/진흙 등)",
        )

    def _pick_hub_values(self):
        layer = self.cmbSiteLayer.currentLayer()
        hub_field = str(self.cmbHubField.currentData() or "")
        if layer is None or not hub_field:
            push_message(self.iface, "허브 값 선택", "유적 레이어와 허브 필드를 먼저 선택하세요.", level=1, duration=6)
            return

        try:
            idx = layer.fields().indexFromName(hub_field)
        except Exception:
            idx = -1
        if idx < 0:
            push_message(self.iface, "허브 값 선택", "허브 필드 인덱스를 찾을 수 없습니다.", level=2, duration=7)
            return

        vals = set()
        try:
            vals = set(layer.uniqueValues(idx))
        except Exception:
            pass
        if not vals:
            # Fallback: iterate a bit
            try:
                for i, ft in enumerate(layer.getFeatures()):
                    v = ft.attribute(idx)
                    if v is not None:
                        vals.add(v)
                    if i >= 50000:
                        break
            except Exception:
                vals = set()

        values = []
        for v in vals:
            s = str(v).strip()
            if not s or s.lower() in ("none", "null"):
                continue
            values.append(s)
        values = sorted(set(values), key=lambda x: x.lower())

        if not values:
            push_message(self.iface, "허브 값 선택", "선택 가능한 값이 없습니다(빈 값만 존재).", level=1, duration=7)
            return

        selected = set(_parse_csv_values(self.txtHubValues.text()))
        dlg = _ValuePickerDialog(
            parent=self,
            title="허브 값 선택 (Pick hub values)",
            hint="허브로 사용할 값을 체크하세요. (여러 개 선택 가능)",
            values=values,
            selected=selected,
        )
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return

        picked = dlg.selected_values()
        self.txtHubValues.setText(", ".join(picked))

    def _init_models(self):
        self.cmbModel.clear()

        items = [
            ("토블러 보행함수 (Tobler Hiking Function)", MODEL_TOBLER),
            ("나이스미스 규칙 (Naismith's Rule)", MODEL_NAISMITH),
            ("허조그 메타볼릭 (Herzog metabolic, via Čučković)", MODEL_HERZOG_METABOLIC),
            ("코놀리&레이크 경사비용 (Conolly & Lake, 2006)", MODEL_CONOLLY_LAKE),
            ("허조그 차량/수레 (Herzog wheeled, via Čučković)", MODEL_HERZOG_WHEELED),
            ("판돌프 운반 에너지 (Pandolf load carriage, 1977)", MODEL_PANDOLF),
        ]
        for label, key in items:
            self.cmbModel.addItem(label, key)
            idx = self.cmbModel.count() - 1
            self.cmbModel.setItemData(idx, self._model_help_text(key), Qt.ToolTipRole)

        # Item tooltips are stored in Qt.ToolTipRole; let Qt handle showing them.

    def _on_model_changed(self):
        try:
            model_key = self.cmbModel.currentData()
            self.groupToblerParams.setVisible(False)
            self.groupNaismithParams.setVisible(False)
            self.groupHerzogMetabolicParams.setVisible(False)
            self.groupConollyLakeParams.setVisible(False)
            self.groupHerzogWheeledParams.setVisible(False)
            self.groupPandolfParams.setVisible(False)

            if model_key == MODEL_TOBLER:
                self.groupToblerParams.setVisible(True)
            elif model_key == MODEL_NAISMITH:
                self.groupNaismithParams.setVisible(True)
            elif model_key == MODEL_HERZOG_METABOLIC:
                self.groupHerzogMetabolicParams.setVisible(True)
            elif model_key == MODEL_CONOLLY_LAKE:
                self.groupConollyLakeParams.setVisible(True)
            elif model_key == MODEL_HERZOG_WHEELED:
                self.groupHerzogWheeledParams.setVisible(True)
            elif model_key == MODEL_PANDOLF:
                self.groupPandolfParams.setVisible(True)

            # Energy output is meaningful only for Pandolf
            if model_key == MODEL_PANDOLF:
                self.cmbCostMode.setEnabled(True)
            else:
                self.cmbCostMode.setCurrentIndex(0)
                self.cmbCostMode.setEnabled(False)
        except Exception:
            pass

    def _model_help_text(self, model_key: str) -> str:
        if model_key == MODEL_TOBLER:
            return (
                "Tobler Hiking Function (1993)\n"
                "- 경사(tan θ)에 따라 보행 속도를 계산합니다.\n"
                "- 기본속도↑ → 전체 시간↓\n"
                "- 경사계수↑ → 경사에 더 민감(가파를수록 더 느림)"
            )
        if model_key == MODEL_NAISMITH:
            return (
                "Naismith’s Rule\n"
                "- 수평 이동 + 상승 고도 페널티로 시간을 근사합니다.\n"
                "- 수평속도↑ → 전체 시간↓\n"
                "- 상승 페널티↑ → 오르막 비용↑"
            )
        if model_key == MODEL_HERZOG_METABOLIC:
            return (
                "Herzog metabolic (via Čučković)\n"
                "- 경사에 따른 이동 비용을 경험적으로 모델링합니다.\n"
                "- 기본속도↑ → 전체 시간↓"
            )
        if model_key == MODEL_CONOLLY_LAKE:
            return (
                "Conolly & Lake (2006)\n"
                "- 경사에 따른 이동 비용을 보행 속도로 환산해 적용합니다.\n"
                "- 기본속도↑ → 전체 시간↓"
            )
        if model_key == MODEL_HERZOG_WHEELED:
            return (
                "Herzog wheeled (via Čučković)\n"
                "- 차량/수레 이동을 가정합니다.\n"
                "- 가파른 경사에서 비용이 급증하도록 설계됩니다.\n"
                "- 임계 경사↑ → 더 가파른 곳까지 통과 가능(급증 시작이 늦음)"
            )
        if model_key == MODEL_PANDOLF:
            return (
                "Pandolf et al. (1977) Load Carriage\n"
                "- 체중/짐무게/지형계수(마찰) + 경사로 에너지(J)를 추정합니다.\n"
                "- 체중·짐무게·지형계수↑ → 에너지 소모↑"
            )
        return "모델을 선택하면 경사(오르막/내리막)에 따라 이동 비용을 계산합니다."

    def _on_mode_changed(self):
        mode = self.cmbNetworkMode.currentData()
        is_all = mode == NETWORK_ALL
        is_knn = (mode == NETWORK_KNN) or is_all
        is_hub = (mode == NETWORK_HUB) or is_all
        is_mst = (mode == NETWORK_MST) or is_all

        for w in (self.lblKnnK, self.spinKnnK, self.lblKnnKHint):
            w.setVisible(bool(is_knn))

        hub_widgets = [self.lblHubField, self.cmbHubField, self.lblHubValues, self.txtHubValues]
        if getattr(self, "btnPickHubValues", None) is not None:
            hub_widgets.append(self.btnPickHubValues)
        hub_widgets.append(self.chkHubConnectMst)
        for w in hub_widgets:
            w.setVisible(bool(is_hub))
        self.cmbSymmetrize.setVisible(bool(is_mst))

        # Compact layout for "All" mode to avoid vertical overflow on small screens:
        # rely on tooltips (and per-mode hover tooltips) instead of extra hint labels.
        compact = bool(is_all)
        for w in (self.lblCandidateKHint, self.lblPairBufferHint, self.lblNetworkHelp):
            try:
                w.setVisible(not compact)
            except Exception:
                pass

    def _on_site_layer_changed(self):
        layer = self.cmbSiteLayer.currentLayer()

        def fill_combo(combo: QtWidgets.QComboBox, include_empty: bool, empty_label: str):
            combo.blockSignals(True)
            combo.clear()
            if include_empty:
                combo.addItem(empty_label, "")
            if layer is not None:
                try:
                    for f in layer.fields():
                        combo.addItem(f.name(), f.name())
                except Exception:
                    pass
            combo.blockSignals(False)

        fill_combo(self.cmbNameField, True, "(피처 ID 사용)")
        fill_combo(self.cmbHubField, True, "(허브 사용 안 함)")

    def _set_running_ui(self, running: bool):
        self.btnRun.setEnabled(not running)
        self.btnClose.setEnabled(not running)

    def run_analysis(self):
        if self._task_running:
            push_message(self.iface, "최소비용 네트워크", "이미 실행 중입니다.", level=1)
            return

        dem_layer = self.cmbDemLayer.currentLayer()
        if dem_layer is None:
            push_message(self.iface, "오류", "DEM 레이어를 선택하세요.", level=2)
            restore_ui_focus(self)
            return
        if not is_metric_crs(dem_layer.crs()):
            push_message(self.iface, "오류", "DEM CRS가 미터 단위가 아닙니다. (권장: 투영좌표계)", level=2)
            restore_ui_focus(self)
            return

        site_layer = self.cmbSiteLayer.currentLayer()
        if site_layer is None:
            push_message(self.iface, "오류", "유적 레이어를 선택하세요.", level=2)
            restore_ui_focus(self)
            return

        # Make sure users can see progress logs during long computations.
        ensure_log_panel_visible(self.iface, show_hint=True)

        mode = self.cmbNetworkMode.currentData()
        cost_mode = self.cmbCostMode.currentData()
        candidate_k = int(self.spinCandidateK.value())
        pair_buffer = float(self.spinPairBuffer.value())
        allow_diagonal = bool(self.chkDiagonal.isChecked())
        sym_method = self.cmbSymmetrize.currentData()
        knn_k = int(self.spinKnnK.value())
        hub_connect_mst = bool(self.chkHubConnectMst.isChecked())

        model_key = self.cmbModel.currentData()
        model_label = self.cmbModel.currentText()
        if model_key != MODEL_PANDOLF:
            cost_mode = COST_TIME

        name_field = str(self.cmbNameField.currentData() or "")
        poly_mode = str(self.cmbPolyPointMode.currentData() or "surface")
        use_selected = bool(self.chkSelectedOnly.isChecked())

        hub_field = str(self.cmbHubField.currentData() or "")
        hub_values = (
            set(_parse_csv_values(self.txtHubValues.text())) if mode in (NETWORK_HUB, NETWORK_ALL) else set()
        )

        try:
            feats = site_layer.selectedFeatures() if use_selected else list(site_layer.getFeatures())
        except Exception:
            feats = site_layer.selectedFeatures() if use_selected else []

        if len(feats) < 2:
            push_message(self.iface, "오류", "유적 피처가 2개 이상 필요합니다.", level=2)
            restore_ui_focus(self)
            return

        nodes: List[NetworkNode] = []
        skipped = 0
        for ft in feats:
            try:
                geom = ft.geometry()
                if geom is None or geom.isEmpty():
                    skipped += 1
                    continue

                pt = None
                if geom.type() == QgsWkbTypes.PointGeometry:
                    if geom.isMultipart():
                        mp = geom.asMultiPoint()
                        if mp:
                            pt = QgsPointXY(mp[0])
                    else:
                        pt = QgsPointXY(geom.asPoint())
                elif geom.type() == QgsWkbTypes.PolygonGeometry:
                    gpt = geom.pointOnSurface() if poly_mode == "surface" else geom.centroid()
                    if gpt is not None and (not gpt.isEmpty()):
                        pt = QgsPointXY(gpt.asPoint())
                else:
                    skipped += 1
                    continue

                if pt is None:
                    skipped += 1
                    continue

                pt_dem = transform_point(pt, site_layer.crs(), dem_layer.crs())

                fid = str(ft.id())
                name = fid
                if name_field:
                    try:
                        v = ft[name_field]
                        if v is not None and str(v).strip() != "":
                            name = str(v)
                    except Exception:
                        pass

                is_hub = False
                if mode in (NETWORK_HUB, NETWORK_ALL) and hub_field and hub_values:
                    try:
                        hv = ft[hub_field]
                        is_hub = str(hv).strip() in hub_values if hv is not None else False
                    except Exception:
                        is_hub = False

                nodes.append(
                    NetworkNode(
                        fid=fid,
                        name=name,
                        x=float(pt_dem.x()),
                        y=float(pt_dem.y()),
                        is_hub=is_hub,
                    )
                )
            except Exception:
                skipped += 1

        if len(nodes) < 2:
            push_message(self.iface, "오류", "유효한 노드가 2개 이상 필요합니다.", level=2)
            restore_ui_focus(self)
            return

        if mode == NETWORK_HUB and (not any(n.is_hub for n in nodes)):
            push_message(self.iface, "오류", "허브가 없습니다. 허브 필드/값을 확인하세요.", level=2)
            restore_ui_focus(self)
            return
        if mode == NETWORK_ALL and hub_field and hub_values and (not any(n.is_hub for n in nodes)):
            push_message(
                self.iface,
                "허브(Hub)",
                "선택한 허브 값과 일치하는 피처가 없습니다. Hub 네트워크는 생략됩니다.",
                level=1,
                duration=7,
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
            "wheeled_max_slope_deg": float(self.spinWheeledMaxSlopeDeg.value()),
            "pandolf_body_kg": float(self.spinPandolfBodyKg.value()),
            "pandolf_load_kg": float(self.spinPandolfLoadKg.value()),
            "pandolf_speed_mps": float(self.spinPandolfSpeedKmh.value()) * 1000.0 / 3600.0,
            "pandolf_terrain_factor": float(self.spinPandolfTerrainFactor.value()),
        }

        self._task_running = True
        self._set_running_ui(True)

        def on_done(res: NetworkTaskResult):
            self._task_running = False
            self._task = None
            self._set_running_ui(False)
            self._handle_task_result(res)

        task = CostNetworkWorker(
            dem_source=dem_layer.source(),
            dem_authid=dem_layer.crs().authid(),
            nodes=nodes,
            allow_diagonal=allow_diagonal,
            pair_buffer_m=pair_buffer,
            candidate_k=candidate_k,
            network_mode=mode,
            knn_k=knn_k,
            hub_connect_mst=hub_connect_mst,
            sym_method=sym_method,
            model_key=model_key,
            model_params=model_params,
            model_label=model_label,
            cost_mode=cost_mode,
            on_done=on_done,
        )
        self._task = task
        QgsApplication.taskManager().addTask(task)
        push_message(self.iface, "최소비용 네트워크", "분석을 시작했습니다. (QGIS 작업 관리자 확인)", level=0, duration=6)

    def _handle_task_result(self, res: NetworkTaskResult):
        if not isinstance(res, NetworkTaskResult) or not res.ok:
            msg = getattr(res, "message", "") or "분석 실패"
            push_message(self.iface, "오류", msg, level=2, duration=9)
            return

        try:
            self._add_result_layers(res)
        except Exception as e:
            log_message(f"Add network result layers error: {e}", level=Qgis.Critical)
            push_message(self.iface, "오류", f"결과 레이어 추가 실패: {e}", level=2, duration=9)
            return

        push_message(self.iface, "최소비용 네트워크", res.message or "완료", level=0, duration=7)

    def _add_result_layers(self, res: NetworkTaskResult):
        project = QgsProject.instance()
        root = project.layerTreeRoot()

        parent_name = "ArchToolkit - 최소비용 네트워크 (Least-cost Network)"
        parent_group = root.findGroup(parent_name)
        if parent_group is None:
            parent_group = root.insertGroup(0, parent_name)

        run_id = uuid.uuid4().hex[:6]
        model_tag = _safe_layer_name_fragment(res.model_label or "")
        mode_tag = str(res.network_mode or "")
        group_name = f"네트워크_{mode_tag}_{model_tag}_{run_id}" if model_tag else f"네트워크_{mode_tag}_{run_id}"
        run_group = parent_group.insertGroup(0, group_name)
        run_group.setExpanded(False)

        # Nodes
        pt_layer = QgsVectorLayer(f"Point?crs={res.dem_authid}", "유적 노드 (Sites)", "memory")
        pr = pt_layer.dataProvider()
        pr.addAttributes([QgsField("fid", QVariant.String), QgsField("name", QVariant.String), QgsField("is_hub", QVariant.Int)])
        pt_layer.updateFields()

        feats = []
        for n in (res.nodes or []):
            f = QgsFeature(pt_layer.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(n.x), float(n.y))))
            f.setAttributes([str(n.fid), str(n.name), 1 if n.is_hub else 0])
            feats.append(f)
        pr.addFeatures(feats)
        pt_layer.updateExtents()

        sym_site = QgsMarkerSymbol.createSimple(
            {"name": "circle", "color": "255,255,255,230", "outline_color": "0,0,0,180", "size": "2.5"}
        )
        sym_hub = QgsMarkerSymbol.createSimple(
            {"name": "star", "color": "255,200,0,230", "outline_color": "120,80,0,200", "size": "3.4"}
        )
        any_hub = any(bool(n.is_hub) for n in (res.nodes or []))
        pt_layer.setRenderer(
            QgsCategorizedSymbolRenderer(
                "is_hub",
                [QgsRendererCategory(0, sym_site, "Site")]
                + ([QgsRendererCategory(1, sym_hub, "Hub")] if any_hub else []),
            )
        )

        # Edges
        line_layer = QgsVectorLayer(f"LineString?crs={res.dem_authid}", "네트워크 (Network)", "memory")
        pr = line_layer.dataProvider()
        pr.addAttributes(
            [
                QgsField("kind", QVariant.String),
                QgsField("from_id", QVariant.String),
                QgsField("to_id", QVariant.String),
                QgsField("from_nm", QVariant.String),
                QgsField("to_nm", QVariant.String),
                QgsField("dist_m", QVariant.Double),
                QgsField("time_ab", QVariant.Double),
                QgsField("time_ba", QVariant.Double),
                QgsField("time_sym", QVariant.Double),
                QgsField("kcal_ab", QVariant.Double),
                QgsField("kcal_ba", QVariant.Double),
                QgsField("kcal_sym", QVariant.Double),
                QgsField("model", QVariant.String),
            ]
        )
        line_layer.updateFields()

        nodes = res.nodes or []
        feats = []
        for e in (res.edges or []):
            if not e.coords or len(e.coords) < 2:
                continue
            a = int(e.a)
            b = int(e.b)
            if not (0 <= a < len(nodes) and 0 <= b < len(nodes)):
                continue
            na = nodes[a]
            nb = nodes[b]
            pts = [QgsPointXY(float(x), float(y)) for x, y in e.coords]
            f = QgsFeature(line_layer.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY(pts))
            f.setAttributes(
                [
                    str(e.kind),
                    str(na.fid),
                    str(nb.fid),
                    str(na.name),
                    str(nb.name),
                    float(e.dist_m),
                    e.time_min_ab,
                    e.time_min_ba,
                    e.time_min_sym,
                    e.energy_kcal_ab,
                    e.energy_kcal_ba,
                    e.energy_kcal_sym,
                    str(res.model_label or ""),
                ]
            )
            feats.append(f)
        pr.addFeatures(feats)
        line_layer.updateExtents()

        def ls(color: str, width: str, style: str = "solid"):
            return QgsLineSymbol.createSimple({"color": color, "width": width, "line_style": style})

        present = {str(e.kind) for e in (res.edges or [])}
        cats = []
        if "mst" in present:
            cats.append(QgsRendererCategory("mst", ls("0,180,0,220", "1.6"), "MST"))
        if "knn" in present:
            cats.append(QgsRendererCategory("knn", ls("0,120,255,200", "1.2"), "k-NN"))
        if "hub_link" in present:
            cats.append(QgsRendererCategory("hub_link", ls("255,140,0,220", "1.4"), "Hub link"))
        if "hub_mst" in present:
            cats.append(QgsRendererCategory("hub_mst", ls("160,0,200,220", "1.2", "dash"), "Hub MST"))
        if not cats:
            cats.append(QgsRendererCategory("mst", ls("0,180,0,220", "1.6"), "Network"))
        line_layer.setRenderer(QgsCategorizedSymbolRenderer("kind", cats))

        pal = QgsPalLayerSettings()
        pal.isExpression = True
        if res.cost_mode == COST_ENERGY:
            pal.fieldName = "round(\"kcal_sym\", 0) || ' kcal'"
        else:
            pal.fieldName = (
                "case "
                "when \"time_sym\" >= 120 then round(\"time_sym\"/60.0, 1) || 'h' "
                "else round(\"time_sym\", 0) || '분' "
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

        line_layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        line_layer.setLabelsEnabled(True)

        project.addMapLayer(line_layer, False)
        project.addMapLayer(pt_layer, False)
        run_group.insertLayer(0, line_layer)
        run_group.insertLayer(0, pt_layer)

        try:
            if parent_group.parent() == root:
                idx = root.children().index(parent_group)
                if idx != 0:
                    root.removeChildNode(parent_group)
                    root.insertChildNode(0, parent_group)
        except Exception:
            pass


class _ValuePickerDialog(QtWidgets.QDialog):
    def __init__(self, *, parent, title: str, hint: str, values: List[str], selected: set):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(420, 520)

        layout = QtWidgets.QVBoxLayout(self)

        hint_lbl = QtWidgets.QLabel(hint)
        hint_lbl.setWordWrap(True)
        layout.addWidget(hint_lbl)

        self.txtFilter = QtWidgets.QLineEdit()
        self.txtFilter.setPlaceholderText("검색… (filter)")
        layout.addWidget(self.txtFilter)

        self.listWidget = QtWidgets.QListWidget()
        self.listWidget.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        layout.addWidget(self.listWidget, 1)

        for v in values:
            item = QtWidgets.QListWidgetItem(str(v))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if str(v) in selected else Qt.Unchecked)
            self.listWidget.addItem(item)

        btn_row = QtWidgets.QHBoxLayout()
        btn_all = QtWidgets.QPushButton("전체 선택")
        btn_none = QtWidgets.QPushButton("전체 해제")
        btn_row.addWidget(btn_all)
        btn_row.addWidget(btn_none)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        layout.addWidget(buttons)

        def apply_filter(text: str):
            t = (text or "").strip().lower()
            for i in range(self.listWidget.count()):
                it = self.listWidget.item(i)
                it.setHidden(bool(t) and t not in it.text().lower())

        self.txtFilter.textChanged.connect(apply_filter)
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

    def _set_all(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.listWidget.count()):
            it = self.listWidget.item(i)
            if not it.isHidden():
                it.setCheckState(state)

    def selected_values(self) -> List[str]:
        out: List[str] = []
        for i in range(self.listWidget.count()):
            it = self.listWidget.item(i)
            if it.checkState() == Qt.Checked:
                out.append(it.text())
        return out
