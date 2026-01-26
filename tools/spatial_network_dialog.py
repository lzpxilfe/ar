# -*- coding: utf-8 -*-

# ArchToolkit - Archaeology Toolkit for QGIS
# Copyright (C) 2026 balguljang2
# License: GPL v3
"""
Spatial / Visibility Network Tool

- PPA (Proximal Point Analysis): Euclidean k-NN graph ("spatial proximity" only).
- Visibility Network: DEM-based Line of Sight graph (A <-> B if mutually visible).

This tool is intentionally separated from the Least-cost Network tool to keep
the UI simple and avoid mixing "cost" and "proximity/visibility" concepts.
"""

import heapq
import math
import os
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsLineSymbol,
    QgsMapLayerProxyModel,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsRendererCategory,
    QgsSingleSymbolRenderer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)

from .utils import (
    is_metric_crs,
    log_message,
    push_message,
    restore_ui_focus,
    transform_point,
)
from .live_log_dialog import ensure_live_log_dialog


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "spatial_network_dialog_base.ui")
)


NETWORK_PPA = "ppa"
NETWORK_VISIBILITY = "visibility"


@dataclass(frozen=True)
class _Node:
    fid: str
    name: str
    x: float
    y: float
    samples: Tuple[Tuple[float, float], ...]
    is_polygon: bool


class SpatialNetworkDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self.canvas = iface.mapCanvas()

        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            icon_candidates = [
                "spatial_network.png",
                "spatial_network.jpg",
                "spatial_network.jpeg",
                "network_icon.png",
                "network_icon.jpg",
                "network_icon.jpeg",
                "cost_icon.png",
            ]
            for icon_name in icon_candidates:
                icon_path = os.path.join(plugin_dir, icon_name)
                if os.path.exists(icon_path):
                    self.setWindowIcon(QIcon(icon_path))
                    break
        except Exception:
            pass

        # Layer filters
        self.cmbSiteLayer.setFilters(QgsMapLayerProxyModel.VectorLayer)
        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)

        # Polygon representative point mode
        self.cmbPolyPointMode.clear()
        self.cmbPolyPointMode.addItem("Point on surface (권장)", "surface")
        self.cmbPolyPointMode.addItem("Centroid", "centroid")

        # Name field
        self.cmbNameField.clear()
        self.cmbNameField.addItem("(FID 사용)", "")

        # Network type
        self.cmbNetworkType.clear()
        self.cmbNetworkType.addItem("근접성 네트워크 (PPA)", NETWORK_PPA)
        self.cmbNetworkType.addItem("가시성 네트워크 (Visibility / LOS)", NETWORK_VISIBILITY)

        self._setup_tooltips()

        # Signals
        self.cmbNetworkType.currentIndexChanged.connect(self._on_mode_changed)
        self.cmbSiteLayer.layerChanged.connect(self._on_site_layer_changed)
        try:
            self.chkVisAllPairs.toggled.connect(self._update_visibility_controls)
        except Exception:
            pass
        try:
            self.chkPolyBoundaryVis.toggled.connect(self._update_visibility_controls)
        except Exception:
            pass
        self.btnRun.clicked.connect(self.run_analysis)
        self.btnClose.clicked.connect(self.reject)

        self._on_site_layer_changed(self.cmbSiteLayer.currentLayer())
        self._on_mode_changed()

    def _setup_tooltips(self):
        # Keep the main UI compact; provide detailed explanations via tooltips.
        tooltip_ppa = (
            "PPA(Proximal Point Analysis)\n"
            "- 지형(DEM) 비용을 쓰지 않고, 유클리드 거리(직선거리)로 최근접 k개를 연결합니다.\n"
            "- k가 작을수록(예: 3~5) 현실적인 '이웃망' 형태가 되며, k가 크면 간선이 급격히 늘어납니다.\n"
            "- 본 도구는 SciPy(KDTree) 같은 외부 의존성 없이 동작합니다.\n\n"
            "Ref:\n"
            "- Terrell (1977) Human Biogeography in the Solomon Islands.\n"
            "- Brughmans & Peeples (2017) Trends in archaeological network research.\n"
            "- Amati, Shafie & Brandes (2018) Reconstructing Archaeological Networks with Structural Holes."
        )

        tooltip_vis = (
            "가시성 네트워크(Visibility / LOS)\n"
            "- DEM 기반 Line of Sight(가시선)으로 두 유적 사이에 지형이 시선을 가리는지 샘플링하여 판정합니다.\n"
            "- 결과 레이어는 '보임/안보임'을 색상으로 구분하고, 거리(km)는 속성(dist_km)으로 저장됩니다.\n"
            "- 계산량이 커질 수 있으므로 '후보 k'와 '최대거리'로 후보 쌍을 줄이는 것을 권장합니다.\n"
            "- 관측/대상 높이는 지표면(DEM) 위 추가 높이(m)입니다.\n\n"
            "Ref:\n"
            "- Van Dyke et al. (2016) Intervisibility in the Chacoan world (viewsheds + viewnets).\n"
            "- Gillings & Wheatley (2001) unresolved issues in archaeological visibility analysis.\n"
            "- Turner et al. (2001) From isovists to visibility graphs (VGA)."
        )

        # Per-item tooltips (combobox dropdown)
        try:
            self.cmbNetworkType.setItemData(0, tooltip_ppa, Qt.ToolTipRole)
            self.cmbNetworkType.setItemData(1, tooltip_vis, Qt.ToolTipRole)
        except Exception:
            pass

        # Show the currently selected item's tooltip even when the dropdown is closed.
        def _sync_network_type_tooltip():
            try:
                tip = self.cmbNetworkType.itemData(self.cmbNetworkType.currentIndex(), Qt.ToolTipRole) or ""
                self.cmbNetworkType.setToolTip(str(tip))
            except Exception:
                pass

        try:
            self.cmbNetworkType.currentIndexChanged.connect(_sync_network_type_tooltip)
        except Exception:
            pass
        _sync_network_type_tooltip()

        try:
            self.spinPpaK.setToolTip("각 유적(노드)에서 연결할 최근접 이웃 수 k입니다. (권장 3~5)")
            self.chkPpaMutualOnly.setToolTip(
                "상호 최근접(Mutual)일 때만 간선을 남깁니다.\n"
                "예) A의 최근접에 B가 포함되고, B의 최근접에도 A가 포함될 때만 연결."
            )
        except Exception:
            pass

        try:
            self.cmbPolyPointMode.setToolTip(
                "폴리곤을 노드(점)로 변환할 때 대표점을 선택합니다.\n"
                "- Point on surface: 폴리곤 내부 보장(권장)\n"
                "- Centroid: 중심점(폴리곤이 오목하면 밖으로 나갈 수 있음)"
            )
        except Exception:
            pass

        try:
            self.spinObsHeight.setToolTip("관측자 높이(m): DEM 지표면 위 추가 높이.")
            self.spinTgtHeight.setToolTip("대상 높이(m): DEM 지표면 위 추가 높이.")
            self.spinCandidateK.setToolTip(
                "각 노드에서 LOS 후보로 검사할 최근접 이웃 수입니다.\n"
                "값이 커질수록 정확도는 올라가지만 계산 시간이 증가합니다."
            )
            self.spinMaxDist.setToolTip(
                "최대 검사 거리(m). 0이면 제한 없음.\n"
                "거리 제한을 두면 계산량이 크게 줄어듭니다."
            )
            self.spinSampleStep.setToolTip(
                "LOS 샘플링 간격(m). 작을수록 정확하지만 느립니다.\n"
                "0 또는 너무 작으면 DEM 픽셀 크기를 기준으로 자동 보정됩니다."
            )
            self.chkVisAllPairs.setToolTip(
                "체크하면 후보 k 제한을 무시하고 (최대 거리 내) 모든 쌍을 LOS로 검사합니다.\n"
                "노드가 많으면 시간이 오래 걸릴 수 있습니다."
            )
            self.chkPolyBoundaryVis.setToolTip(
                "입력 레이어가 폴리곤일 때, 대표점 1개가 아니라 폴리곤 경계를 샘플링해\n"
                "가시성 비율(vis_ratio, 0~1)을 계산합니다. (느릴 수 있음)"
            )
            self.spinPolyBoundaryStep.setToolTip("폴리곤 경계에서 샘플 점을 뽑는 간격(m)입니다.")
            self.spinPolyMaxBoundaryPts.setToolTip("폴리곤 1개당 경계 샘플 점의 최대 개수(속도 제한)입니다.")
        except Exception:
            pass

    def _on_mode_changed(self):
        mode = self.cmbNetworkType.currentData()
        is_ppa = mode == NETWORK_PPA
        is_vis = mode == NETWORK_VISIBILITY

        try:
            self.groupPpa.setVisible(is_ppa)
        except Exception:
            pass
        try:
            self.groupVisibility.setVisible(is_vis)
        except Exception:
            pass

        self._update_visibility_controls()

    def _update_visibility_controls(self):
        """Show/hide/enable advanced visibility options based on mode + input geometry."""
        mode = None
        try:
            mode = self.cmbNetworkType.currentData()
        except Exception:
            mode = None

        is_vis = mode == NETWORK_VISIBILITY
        site_layer = None
        try:
            site_layer = self.cmbSiteLayer.currentLayer()
        except Exception:
            site_layer = None

        is_polygon_layer = False
        try:
            if site_layer and site_layer.isValid():
                is_polygon_layer = site_layer.geometryType() == QgsWkbTypes.PolygonGeometry
        except Exception:
            is_polygon_layer = False

        # Candidate-k is irrelevant when all-pairs is enabled.
        all_pairs = False
        try:
            all_pairs = bool(self.chkVisAllPairs.isChecked())
        except Exception:
            all_pairs = False

        try:
            self.spinCandidateK.setEnabled(is_vis and (not all_pairs))
            self.lblCandidateK.setEnabled(is_vis and (not all_pairs))
        except Exception:
            pass

        show_poly = bool(is_vis and is_polygon_layer)
        poly_enabled = False
        try:
            poly_enabled = bool(self.chkPolyBoundaryVis.isChecked())
        except Exception:
            poly_enabled = False

        for w in ("chkPolyBoundaryVis",):
            try:
                getattr(self, w).setVisible(show_poly)
            except Exception:
                pass

        for w in ("lblPolyBoundaryStep", "spinPolyBoundaryStep", "lblPolyMaxPts", "spinPolyMaxBoundaryPts"):
            try:
                getattr(self, w).setVisible(show_poly and poly_enabled)
            except Exception:
                pass

    def _on_site_layer_changed(self, layer):
        # Populate name fields (string-ish fields only)
        try:
            self.cmbNameField.blockSignals(True)
            self.cmbNameField.clear()
            self.cmbNameField.addItem("(FID 사용)", "")

            if layer and layer.isValid():
                for f in layer.fields():
                    try:
                        if f.type() in (QVariant.String, QVariant.Int, QVariant.LongLong):
                            self.cmbNameField.addItem(f.name(), f.name())
                    except Exception:
                        continue
        finally:
            try:
                self.cmbNameField.blockSignals(False)
            except Exception:
                pass

        self._update_visibility_controls()

    def _collect_nodes(
        self,
        *,
        layer,
        name_field: str,
        poly_mode: str,
        use_selected_only: bool,
        target_crs,
        collect_polygon_boundary: bool = False,
        boundary_step_m: float = 50.0,
        boundary_max_points: int = 30,
    ) -> List[_Node]:
        feats = []
        try:
            if use_selected_only:
                feats = layer.selectedFeatures()
            else:
                feats = list(layer.getFeatures())
        except Exception:
            feats = layer.selectedFeatures() if use_selected_only else []

        nodes: List[_Node] = []
        skipped = 0

        ct = None
        try:
            if layer.crs() != target_crs:
                ct = QgsCoordinateTransform(layer.crs(), target_crs, QgsProject.instance())
        except Exception:
            ct = None

        for ft in feats:
            try:
                geom = ft.geometry()
                if geom is None or geom.isEmpty():
                    skipped += 1
                    continue

                is_polygon = geom.type() == QgsWkbTypes.PolygonGeometry

                # Work in target CRS (meters expected for distance-based tools)
                geom_t = geom
                if ct is not None:
                    try:
                        geom_t = QgsGeometry(geom)
                        geom_t.transform(ct)
                    except Exception:
                        geom_t = geom

                pt_t = None
                if geom_t.type() == QgsWkbTypes.PointGeometry:
                    if geom_t.isMultipart():
                        mp = geom_t.asMultiPoint()
                        if mp:
                            pt_t = QgsPointXY(mp[0])
                    else:
                        pt_t = QgsPointXY(geom_t.asPoint())
                elif geom_t.type() == QgsWkbTypes.PolygonGeometry:
                    gpt = geom_t.pointOnSurface() if poly_mode == "surface" else geom_t.centroid()
                    if gpt is not None and (not gpt.isEmpty()):
                        pt_t = QgsPointXY(gpt.asPoint())
                else:
                    skipped += 1
                    continue

                if pt_t is None:
                    skipped += 1
                    continue

                fid = str(ft.id())

                name = fid
                if name_field:
                    try:
                        v = ft[name_field]
                        if v is not None and str(v).strip() != "":
                            name = str(v)
                    except Exception:
                        pass

                samples: Tuple[Tuple[float, float], ...] = ((float(pt_t.x()), float(pt_t.y())),)
                if is_polygon and collect_polygon_boundary:
                    try:
                        step = float(boundary_step_m or 0.0)
                    except Exception:
                        step = 50.0
                    try:
                        mx = int(boundary_max_points or 0)
                    except Exception:
                        mx = 30
                    pts = self._sample_polygon_boundary_points(geom_t, step_m=step, max_points=mx)
                    if pts:
                        samples = pts

                nodes.append(
                    _Node(
                        fid=fid,
                        name=name,
                        x=float(pt_t.x()),
                        y=float(pt_t.y()),
                        samples=samples,
                        is_polygon=bool(is_polygon),
                    )
                )
            except Exception:
                skipped += 1

        if skipped:
            log_message(f"SpatialNetwork: skipped {skipped} feature(s) (empty/unsupported geometry)", level=Qgis.Warning)
        return nodes

    def _sample_polygon_boundary_points(
        self,
        geom_t: QgsGeometry,
        *,
        step_m: float,
        max_points: int,
    ) -> Tuple[Tuple[float, float], ...]:
        """Sample points along polygon boundary in *target CRS units* (meters expected)."""
        try:
            boundary = geom_t.boundary()
        except Exception:
            boundary = None

        if boundary is None or boundary.isEmpty():
            return ()

        try:
            length = float(boundary.length() or 0.0)
        except Exception:
            length = 0.0

        if length <= 0:
            return ()

        try:
            step = float(step_m or 0.0)
        except Exception:
            step = 0.0
        if step <= 0:
            step = 50.0

        try:
            mx = int(max_points or 0)
        except Exception:
            mx = 0
        if mx > 0:
            # Enforce a cap by increasing the step when needed.
            step = max(step, length / float(mx))

        try:
            num = int(length / step) + 1
        except Exception:
            num = 1
        num = max(1, num)

        pts: List[Tuple[float, float]] = []
        for i in range(num + 1):
            d = min(length, float(i) * step)
            try:
                p = boundary.interpolate(d)
            except Exception:
                p = None
            if p is None or p.isEmpty():
                continue
            try:
                pt = p.asPoint()
                pts.append((float(pt.x()), float(pt.y())))
            except Exception:
                continue

        # Deduplicate (rounded to reduce near-duplicates from interpolation).
        uniq: List[Tuple[float, float]] = []
        seen: Set[Tuple[int, int]] = set()
        for x, y in pts:
            k = (int(round(x * 1000.0)), int(round(y * 1000.0)))
            if k in seen:
                continue
            seen.add(k)
            uniq.append((x, y))

        return tuple(uniq)

    def _ensure_metric(self, crs, title: str) -> bool:
        if is_metric_crs(crs):
            return True
        push_message(
            self.iface,
            title,
            "CRS 단위가 미터가 아닙니다. (권장: 투영좌표계/미터) 레이어를 재투영 후 다시 시도해주세요.",
            level=2,
            duration=8,
        )
        return False

    def run_analysis(self):
        mode = self.cmbNetworkType.currentData()

        site_layer = self.cmbSiteLayer.currentLayer()
        if site_layer is None or (not site_layer.isValid()):
            push_message(self.iface, "네트워크", "입력 유적(벡터) 레이어를 선택해주세요.", level=2)
            restore_ui_focus(self)
            return

        use_selected = bool(self.chkSelectedOnly.isChecked())
        if use_selected and site_layer.selectedFeatureCount() < 2:
            push_message(self.iface, "네트워크", "선택 피처가 2개 이상 필요합니다.", level=2)
            restore_ui_focus(self)
            return

        # Live log window (non-modal) so users can see progress in real time.
        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)

        name_field = str(self.cmbNameField.currentData() or "")
        poly_mode = str(self.cmbPolyPointMode.currentData() or "surface")

        if mode == NETWORK_PPA:
            if not self._ensure_metric(site_layer.crs(), "PPA"):
                restore_ui_focus(self)
                return

            nodes = self._collect_nodes(
                layer=site_layer,
                name_field=name_field,
                poly_mode=poly_mode,
                use_selected_only=use_selected,
                target_crs=site_layer.crs(),
            )
            if len(nodes) < 2:
                push_message(self.iface, "PPA", "유효한 노드가 2개 이상 필요합니다.", level=2)
                restore_ui_focus(self)
                return

            k = int(self.spinPpaK.value())
            mutual = bool(self.chkPpaMutualOnly.isChecked())
            self._run_ppa(nodes, k=k, mutual_only=mutual)
            return

        # Visibility network
        dem_layer = self.cmbDemLayer.currentLayer()
        if dem_layer is None or (not dem_layer.isValid()):
            push_message(self.iface, "가시성 네트워크", "DEM(래스터) 레이어를 선택해주세요.", level=2)
            restore_ui_focus(self)
            return
        if not self._ensure_metric(dem_layer.crs(), "가시성 네트워크"):
            restore_ui_focus(self)
            return

        poly_boundary = False
        boundary_step = 50.0
        boundary_max_pts = 30
        try:
            poly_boundary = bool(self.chkPolyBoundaryVis.isChecked())
        except Exception:
            poly_boundary = False
        try:
            boundary_step = float(self.spinPolyBoundaryStep.value())
        except Exception:
            boundary_step = 50.0
        try:
            boundary_max_pts = int(self.spinPolyMaxBoundaryPts.value())
        except Exception:
            boundary_max_pts = 30

        nodes = self._collect_nodes(
            layer=site_layer,
            name_field=name_field,
            poly_mode=poly_mode,
            use_selected_only=use_selected,
            target_crs=dem_layer.crs(),
            collect_polygon_boundary=poly_boundary,
            boundary_step_m=boundary_step,
            boundary_max_points=boundary_max_pts,
        )
        if len(nodes) < 2:
            push_message(self.iface, "가시성 네트워크", "유효한 노드가 2개 이상 필요합니다.", level=2)
            restore_ui_focus(self)
            return

        obs_h = float(self.spinObsHeight.value())
        tgt_h = float(self.spinTgtHeight.value())
        cand_k = int(self.spinCandidateK.value())
        max_dist = float(self.spinMaxDist.value())
        step_m = float(self.spinSampleStep.value())

        self._run_visibility_network(
            dem_layer=dem_layer,
            nodes=nodes,
            obs_height=obs_h,
            tgt_height=tgt_h,
            candidate_k=cand_k,
            max_dist=max_dist,
            sample_step_m=step_m,
            use_poly_boundary_ratio=poly_boundary,
        )

    def _run_ppa(self, nodes: List[_Node], k: int, mutual_only: bool):
        n = len(nodes)
        k = max(1, min(int(k), max(1, n - 1)))

        push_message(self.iface, "PPA", f"근접성 네트워크 생성 중... (노드 {n}, k={k})", level=0, duration=4)
        QtWidgets.QApplication.processEvents()

        # Build k-NN neighbor sets (directed)
        neigh: List[Set[int]] = [set() for _ in range(n)]
        for i in range(n):
            dists = []
            xi, yi = nodes[i].x, nodes[i].y
            for j in range(n):
                if i == j:
                    continue
                xj, yj = nodes[j].x, nodes[j].y
                dsq = (xi - xj) ** 2 + (yi - yj) ** 2
                dists.append((dsq, j))
            for _dsq, j in heapq.nsmallest(k, dists, key=lambda t: t[0]):
                neigh[i].add(j)

        # Select undirected edges
        edges: Set[Tuple[int, int]] = set()
        for i in range(n):
            for j in neigh[i]:
                a, b = (i, j) if i < j else (j, i)
                if mutual_only:
                    if i in neigh[j]:
                        edges.add((a, b))
                else:
                    edges.add((a, b))

        self._add_edge_layer(
            nodes=nodes,
            edges=sorted(edges),
            layer_name=f"PPA_kNN_{k}",
            color=QColor(80, 80, 80, 220),
            add_dist=True,
            crs_authid=self.cmbSiteLayer.currentLayer().crs().authid()
            if self.cmbSiteLayer.currentLayer()
            else QgsProject.instance().crs().authid(),
        )
        push_message(self.iface, "PPA", f"완료: 간선 {len(edges)}개", level=0, duration=5)
        self.accept()

    def _los_visible(
        self,
        *,
        dem_layer,
        provider=None,
        ax: float,
        ay: float,
        bx: float,
        by: float,
        obs_height: float,
        tgt_height: float,
        sample_step_m: float,
    ) -> Optional[bool]:
        dx = bx - ax
        dy = by - ay
        total_dist = math.hypot(dx, dy)
        if total_dist <= 0:
            return True

        px = abs(float(dem_layer.rasterUnitsPerPixelX() or 0.0))
        py = abs(float(dem_layer.rasterUnitsPerPixelY() or 0.0))
        pix = min([v for v in (px, py) if v > 0] or [5.0])
        step = float(sample_step_m or 0.0)
        if step <= 0:
            step = max(pix, 5.0)
        else:
            step = max(pix, step)

        # Network use-case: keep sampling reasonable.
        num_samples = int(total_dist / step) if step > 0 else 200
        num_samples = max(80, min(num_samples, 2000))

        if provider is None:
            provider = dem_layer.dataProvider()

        # Endpoints
        obs_elev0, ok0 = provider.sample(QgsPointXY(ax, ay), 1)
        tgt_elev0, ok1 = provider.sample(QgsPointXY(bx, by), 1)
        if not ok0 or not ok1:
            return None
        try:
            obs_elev = float(obs_elev0) + float(obs_height)
            tgt_elev = float(tgt_elev0) + float(tgt_height)
        except Exception:
            return None

        for i in range(1, num_samples):
            frac = i / num_samples
            x = ax + frac * dx
            y = ay + frac * dy
            elev, ok = provider.sample(QgsPointXY(x, y), 1)
            if not ok:
                return None
            try:
                z = float(elev)
            except Exception:
                return None

            sight = obs_elev + frac * (tgt_elev - obs_elev)
            if z > sight:
                return False
        return True

    def _run_visibility_network(
        self,
        *,
        dem_layer,
        nodes: List[_Node],
        obs_height: float,
        tgt_height: float,
        candidate_k: int,
        max_dist: float,
        sample_step_m: float,
        use_poly_boundary_ratio: bool = False,
    ):
        n = len(nodes)
        if n < 2:
            return

        all_pairs = False
        try:
            all_pairs = bool(self.chkVisAllPairs.isChecked())
        except Exception:
            all_pairs = False

        if candidate_k <= 0:
            candidate_k = 1
        candidate_k = min(int(candidate_k), max(1, n - 1))

        max_dist = float(max_dist or 0.0)
        if max_dist < 0:
            max_dist = 0.0
        max_dist_sq = (max_dist ** 2) if max_dist > 0 else 0.0

        if all_pairs:
            # Count pairs for progress.
            total_pairs = 0
            for i in range(n):
                xi, yi = nodes[i].x, nodes[i].y
                for j in range(i + 1, n):
                    if max_dist_sq > 0:
                        xj, yj = nodes[j].x, nodes[j].y
                        dsq = (xi - xj) ** 2 + (yi - yj) ** 2
                        if dsq > max_dist_sq:
                            continue
                    total_pairs += 1

            # Large all-pairs runs can be slow; ask for confirmation.
            try:
                est_los = None
                if use_poly_boundary_ratio:
                    avg_samples = sum(len(nd.samples) for nd in nodes) / float(max(1, n))
                    est_los = int(total_pairs * avg_samples * 2)  # A->B + B->A

                if total_pairs >= 5000 or (est_los is not None and est_los >= 200000):
                    extra = ""
                    if est_los is not None:
                        extra = f"\n(추정 LOS 호출: 약 {est_los:,}회)"
                    res = QtWidgets.QMessageBox.warning(
                        self,
                        "경고",
                        f"반경 내 검사 쌍이 많습니다: {total_pairs:,}쌍{extra}\n계속 진행할까요?",
                        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                        QtWidgets.QMessageBox.No,
                    )
                    if res != QtWidgets.QMessageBox.Yes:
                        restore_ui_focus(self)
                        return
            except Exception:
                pass

            progress = QtWidgets.QProgressDialog(
                f"가시성 네트워크(LOS) 계산 중... (쌍 {total_pairs}개 검사)",
                "취소",
                0,
                max(1, total_pairs),
                self,
            )
        else:
            progress = QtWidgets.QProgressDialog(
                "가시성 네트워크(LOS) 계산 중...", "취소", 0, n, self
            )
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents()

        edges: Set[Tuple[int, int]] = set()
        status_by_edge: Dict[Tuple[int, int], str] = {}
        ratio_by_edge: Dict[Tuple[int, int], float] = {}
        tested_pairs = 0
        failed_pairs = 0
        provider = dem_layer.dataProvider()

        def _ratio_for_samples(
            obs_samples: Tuple[Tuple[float, float], ...],
            tx: float,
            ty: float,
            *,
            obs_h: float,
            tgt_h: float,
        ) -> Optional[float]:
            visible = 0
            valid = 0
            for ox, oy in obs_samples:
                vis = self._los_visible(
                    dem_layer=dem_layer,
                    provider=provider,
                    ax=float(ox),
                    ay=float(oy),
                    bx=float(tx),
                    by=float(ty),
                    obs_height=obs_h,
                    tgt_height=tgt_h,
                    sample_step_m=sample_step_m,
                )
                if vis is None:
                    continue
                valid += 1
                if vis:
                    visible += 1
            if valid <= 0:
                return None
            return float(visible) / float(valid)

        if all_pairs:
            for i in range(n):
                if progress.wasCanceled():
                    push_message(self.iface, "가시성 네트워크", "취소되었습니다.", level=1, duration=4)
                    restore_ui_focus(self)
                    return

                xi, yi = nodes[i].x, nodes[i].y
                for j in range(i + 1, n):
                    if max_dist_sq > 0:
                        xj, yj = nodes[j].x, nodes[j].y
                        dsq = (xi - xj) ** 2 + (yi - yj) ** 2
                        if dsq > max_dist_sq:
                            continue

                    tested_pairs += 1

                    status = ""
                    ratio_val: float = 0.0

                    if use_poly_boundary_ratio:
                        r_ab = _ratio_for_samples(
                            nodes[i].samples,
                            nodes[j].x,
                            nodes[j].y,
                            obs_h=obs_height,
                            tgt_h=tgt_height,
                        )
                        r_ba = _ratio_for_samples(
                            nodes[j].samples,
                            nodes[i].x,
                            nodes[i].y,
                            obs_h=obs_height,
                            tgt_h=tgt_height,
                        )
                        vals = [v for v in (r_ab, r_ba) if v is not None]
                        if not vals:
                            failed_pairs += 1
                            status = "샘플 실패"
                            ratio_val = 0.0
                        else:
                            ratio_val = float(sum(vals) / float(len(vals)))
                            status = "보임" if ratio_val > 0.0 else "안보임"
                    else:
                        vis = self._los_visible(
                            dem_layer=dem_layer,
                            provider=provider,
                            ax=nodes[i].x,
                            ay=nodes[i].y,
                            bx=nodes[j].x,
                            by=nodes[j].y,
                            obs_height=obs_height,
                            tgt_height=tgt_height,
                            sample_step_m=sample_step_m,
                        )
                        if vis is None:
                            failed_pairs += 1
                            status = "샘플 실패"
                            ratio_val = 0.0
                        else:
                            status = "보임" if vis else "안보임"
                            ratio_val = 1.0 if vis else 0.0

                    edges.add((i, j))
                    status_by_edge[(i, j)] = status
                    ratio_by_edge[(i, j)] = ratio_val

                    if tested_pairs % 20 == 0:
                        progress.setValue(min(progress.maximum(), tested_pairs))
                        QtWidgets.QApplication.processEvents()

            progress.setValue(progress.maximum())
        else:
            tested: Set[Tuple[int, int]] = set()
            for i in range(n):
                if progress.wasCanceled():
                    push_message(self.iface, "가시성 네트워크", "취소되었습니다.", level=1, duration=4)
                    restore_ui_focus(self)
                    return

                # Candidate neighbors by Euclidean distance (in DEM CRS units, meters expected)
                xi, yi = nodes[i].x, nodes[i].y
                dists = []
                for j in range(n):
                    if i == j:
                        continue
                    xj, yj = nodes[j].x, nodes[j].y
                    dsq = (xi - xj) ** 2 + (yi - yj) ** 2
                    if max_dist_sq > 0 and dsq > max_dist_sq:
                        continue
                    dists.append((dsq, j))

                for _dsq, j in heapq.nsmallest(candidate_k, dists, key=lambda t: t[0]):
                    a, b = (i, j) if i < j else (j, i)
                    if a == b:
                        continue
                    if (a, b) in tested:
                        continue
                    tested.add((a, b))
                    tested_pairs += 1

                    status = ""
                    ratio_val: float = 0.0

                    if use_poly_boundary_ratio:
                        r_ab = _ratio_for_samples(
                            nodes[a].samples,
                            nodes[b].x,
                            nodes[b].y,
                            obs_h=obs_height,
                            tgt_h=tgt_height,
                        )
                        r_ba = _ratio_for_samples(
                            nodes[b].samples,
                            nodes[a].x,
                            nodes[a].y,
                            obs_h=obs_height,
                            tgt_h=tgt_height,
                        )
                        vals = [v for v in (r_ab, r_ba) if v is not None]
                        if not vals:
                            failed_pairs += 1
                            status = "샘플 실패"
                            ratio_val = 0.0
                        else:
                            ratio_val = float(sum(vals) / float(len(vals)))
                            status = "보임" if ratio_val > 0.0 else "안보임"
                    else:
                        vis = self._los_visible(
                            dem_layer=dem_layer,
                            provider=provider,
                            ax=nodes[a].x,
                            ay=nodes[a].y,
                            bx=nodes[b].x,
                            by=nodes[b].y,
                            obs_height=obs_height,
                            tgt_height=tgt_height,
                            sample_step_m=sample_step_m,
                        )
                        if vis is None:
                            failed_pairs += 1
                            status = "샘플 실패"
                            ratio_val = 0.0
                        else:
                            status = "보임" if vis else "안보임"
                            ratio_val = 1.0 if vis else 0.0

                    edges.add((a, b))
                    status_by_edge[(a, b)] = status
                    ratio_by_edge[(a, b)] = ratio_val

                progress.setValue(i + 1)
                QtWidgets.QApplication.processEvents()

        self._add_edge_layer(
            nodes=nodes,
            edges=sorted(edges),
            layer_name="Visibility_LOS",
            color=QColor(0, 160, 80, 220),
            add_dist=True,
            crs_authid=dem_layer.crs().authid(),
            status_by_edge=status_by_edge,
            ratio_by_edge=ratio_by_edge,
            label_distance=bool(tested_pairs <= 300),
        )
        visible_edges = sum(1 for v in status_by_edge.values() if v == "보임")
        hidden_edges = sum(1 for v in status_by_edge.values() if v == "안보임")
        fail_edges = sum(1 for v in status_by_edge.values() if v == "샘플 실패")

        msg = (
            f"완료: 검사쌍 {tested_pairs}개 (보임 {visible_edges}, 안보임 {hidden_edges}, 실패 {fail_edges})"
        )
        if use_poly_boundary_ratio:
            msg += "  [vis_ratio]"
        log_message(
            f"VisibilityNetwork: {msg} (all_pairs={all_pairs}, max_dist={max_dist}, poly_ratio={use_poly_boundary_ratio})",
            level=Qgis.Info,
        )
        push_message(self.iface, "가시성 네트워크", msg, level=0, duration=8)
        self.accept()

    def _add_edge_layer(
        self,
        *,
        nodes: List[_Node],
        edges: List[Tuple[int, int]],
        layer_name: str,
        color: QColor,
        add_dist: bool,
        crs_authid: str,
        status_by_edge: Optional[Dict[Tuple[int, int], str]] = None,
        ratio_by_edge: Optional[Dict[Tuple[int, int], float]] = None,
        label_distance: bool = False,
    ):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        parent_name = "ArchToolkit - Networks (PPA/Visibility)"
        parent_group = root.findGroup(parent_name)
        if parent_group is None:
            parent_group = root.insertGroup(0, parent_name)

        run_id = uuid.uuid4().hex[:6]
        run_group = parent_group.insertGroup(0, f"{layer_name}_{run_id}")
        run_group.setExpanded(False)

        layer = QgsVectorLayer(
            f"LineString?crs={crs_authid}",
            layer_name,
            "memory",
        )
        pr = layer.dataProvider()
        fields = [
            QgsField("from_id", QVariant.String),
            QgsField("to_id", QVariant.String),
            QgsField("from_nm", QVariant.String),
            QgsField("to_nm", QVariant.String),
        ]
        if status_by_edge is not None:
            fields.append(QgsField("status", QVariant.String))
        if ratio_by_edge is not None:
            fields.append(QgsField("vis_ratio", QVariant.Double))
        if add_dist:
            fields.append(QgsField("dist_m", QVariant.Double))
            fields.append(QgsField("dist_km", QVariant.Double))
        pr.addAttributes(fields)
        layer.updateFields()

        feats = []
        for a, b in edges:
            na = nodes[a]
            nb = nodes[b]
            geom = QgsGeometry.fromPolylineXY([QgsPointXY(na.x, na.y), QgsPointXY(nb.x, nb.y)])
            f = QgsFeature(layer.fields())
            f.setGeometry(geom)
            f["from_id"] = na.fid
            f["to_id"] = nb.fid
            f["from_nm"] = na.name
            f["to_nm"] = nb.name
            if add_dist:
                dist_m = float(math.hypot(nb.x - na.x, nb.y - na.y))
                f["dist_m"] = dist_m
                f["dist_km"] = dist_m / 1000.0
            if status_by_edge is not None:
                f["status"] = str(status_by_edge.get((a, b), ""))
            if ratio_by_edge is not None:
                try:
                    f["vis_ratio"] = float(ratio_by_edge.get((a, b), 0.0))
                except Exception:
                    f["vis_ratio"] = 0.0
            feats.append(f)
        pr.addFeatures(feats)
        layer.updateExtents()

        # Styling
        if status_by_edge is not None:
            categories: List[QgsRendererCategory] = []

            def _mk_sym(col: QColor, *, dashed: bool = False, dotted: bool = False) -> QgsLineSymbol:
                sym = QgsLineSymbol.createSimple(
                    {
                        "color": f"{col.red()},{col.green()},{col.blue()},{col.alpha()}",
                        "width": "0.7",
                    }
                )
                if dashed or dotted:
                    try:
                        ls = "dash" if dashed else "dot"
                        sym.symbolLayer(0).setPenStyle(Qt.DashLine if ls == "dash" else Qt.DotLine)
                    except Exception:
                        pass
                return sym

            categories.append(QgsRendererCategory("보임", _mk_sym(QColor(0, 180, 0, 220)), "보임"))
            categories.append(QgsRendererCategory("안보임", _mk_sym(QColor(220, 0, 0, 180), dashed=True), "안보임"))
            categories.append(QgsRendererCategory("샘플 실패", _mk_sym(QColor(120, 120, 120, 180), dotted=True), "샘플 실패"))

            renderer = QgsCategorizedSymbolRenderer("status", categories)
            layer.setRenderer(renderer)
        else:
            sym = QgsLineSymbol.createSimple(
                {"color": f"{color.red()},{color.green()},{color.blue()},{color.alpha()}", "width": "0.7"}
            )
            layer.setRenderer(QgsSingleSymbolRenderer(sym))

        # Labels: enable only when requested (or small graphs)
        try:
            pal = QgsPalLayerSettings()
            if label_distance and add_dist:
                pal.enabled = True
                pal.isExpression = True
                pal.fieldName = 'round("dist_km", 2) || \' km\''

                fmt = QgsTextFormat()
                fmt.setSize(8)
                fmt.setColor(QColor(40, 40, 40))
                buf = QgsTextBufferSettings()
                buf.setEnabled(True)
                buf.setSize(1.0)
                buf.setColor(QColor(255, 255, 255))
                fmt.setBuffer(buf)
                pal.setFormat(fmt)

                layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
                layer.setLabelsEnabled(True)
            else:
                pal.enabled = False
                layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        except Exception:
            pass

        project.addMapLayer(layer, False)
        run_group.addLayer(layer)

        try:
            # Keep group near top
            if parent_group.parent() == root:
                idx = root.children().index(parent_group)
                if idx != 0:
                    root.removeChildNode(parent_group)
                    root.insertChildNode(0, parent_group)
        except Exception:
            pass
