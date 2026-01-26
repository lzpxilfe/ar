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
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsLineSymbol,
    QgsMapLayerProxyModel,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)

from .utils import is_metric_crs, log_message, push_message, restore_ui_focus, transform_point


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


class SpatialNetworkDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self.canvas = iface.mapCanvas()

        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            icon_path = os.path.join(plugin_dir, "network_icon.jpg")
            if not os.path.exists(icon_path):
                icon_path = os.path.join(plugin_dir, "cost_icon.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
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

        # General hint
        try:
            self.cmbNetworkType.setToolTip("방식에 마우스를 올리면 설명/참고문헌이 표시됩니다.")
        except Exception:
            pass

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

    def _collect_nodes(
        self,
        *,
        layer,
        name_field: str,
        poly_mode: str,
        use_selected_only: bool,
        target_crs,
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

                pt_t = transform_point(pt, layer.crs(), target_crs)
                fid = str(ft.id())

                name = fid
                if name_field:
                    try:
                        v = ft[name_field]
                        if v is not None and str(v).strip() != "":
                            name = str(v)
                    except Exception:
                        pass

                nodes.append(_Node(fid=fid, name=name, x=float(pt_t.x()), y=float(pt_t.y())))
            except Exception:
                skipped += 1

        if skipped:
            log_message(f"SpatialNetwork: skipped {skipped} feature(s) (empty/unsupported geometry)", level=Qgis.Warning)
        return nodes

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

        nodes = self._collect_nodes(
            layer=site_layer,
            name_field=name_field,
            poly_mode=poly_mode,
            use_selected_only=use_selected,
            target_crs=dem_layer.crs(),
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
    ):
        n = len(nodes)
        if n < 2:
            return

        if candidate_k <= 0:
            candidate_k = 1
        candidate_k = min(int(candidate_k), max(1, n - 1))

        max_dist = float(max_dist or 0.0)
        if max_dist < 0:
            max_dist = 0.0

        progress = QtWidgets.QProgressDialog(
            "가시성 네트워크(LOS) 계산 중...", "취소", 0, n, self
        )
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents()

        tested: Set[Tuple[int, int]] = set()
        edges: Set[Tuple[int, int]] = set()

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
                if max_dist > 0 and dsq > (max_dist ** 2):
                    continue
                dists.append((dsq, j))

            for _dsq, j in heapq.nsmallest(candidate_k, dists, key=lambda t: t[0]):
                a, b = (i, j) if i < j else (j, i)
                if a == b:
                    continue
                if (a, b) in tested:
                    continue
                tested.add((a, b))

                vis = self._los_visible(
                    dem_layer=dem_layer,
                    ax=nodes[a].x,
                    ay=nodes[a].y,
                    bx=nodes[b].x,
                    by=nodes[b].y,
                    obs_height=obs_height,
                    tgt_height=tgt_height,
                    sample_step_m=sample_step_m,
                )
                if vis is None:
                    # Treat failed sampling as non-edge (outside raster or NoData).
                    continue
                if vis:
                    edges.add((a, b))

            progress.setValue(i + 1)
            QtWidgets.QApplication.processEvents()

        self._add_edge_layer(
            nodes=nodes,
            edges=sorted(edges),
            layer_name="Visibility_LOS",
            color=QColor(0, 160, 80, 220),
            add_dist=True,
            crs_authid=dem_layer.crs().authid(),
        )
        push_message(self.iface, "가시성 네트워크", f"완료: 간선 {len(edges)}개", level=0, duration=6)
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
        if add_dist:
            fields.append(QgsField("dist_m", QVariant.Double))
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
                f["dist_m"] = float(math.hypot(nb.x - na.x, nb.y - na.y))
            feats.append(f)
        pr.addFeatures(feats)
        layer.updateExtents()

        # Simple styling
        sym = QgsLineSymbol.createSimple(
            {"color": f"{color.red()},{color.green()},{color.blue()},{color.alpha()}", "width": "0.7"}
        )
        layer.setRenderer(QgsSingleSymbolRenderer(sym))

        # Optional: label distances lightly (disabled by default)
        try:
            pal = QgsPalLayerSettings()
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
