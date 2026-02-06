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
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import processing

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
    QgsMarkerSymbol,
    QgsMapLayerProxyModel,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSingleSymbolRenderer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsGraduatedSymbolRenderer,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox  # noqa: F401 (needed for .ui custom widget loading)

from .utils import (
    is_metric_crs,
    log_message,
    push_message,
    restore_ui_focus,
    set_archtoolkit_layer_metadata,
    transform_point,
)
from .live_log_dialog import ensure_live_log_dialog


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "spatial_network_dialog_base.ui")
)


NETWORK_PPA = "ppa"
NETWORK_VISIBILITY = "visibility"

PPA_KNN = "knn"
PPA_THRESHOLD = "threshold"
PPA_DELAUNAY = "delaunay"
PPA_GABRIEL = "gabriel"
PPA_RNG = "rng"

VIS_RULE_MUTUAL = "mutual"
VIS_RULE_EITHER = "either"


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

        # Extra widgets (created dynamically to avoid .ui editing regressions)
        self._ensure_extra_widgets()

        # PPA graph selector
        try:
            self.cmbPpaGraph.clear()
            self.cmbPpaGraph.addItem("k-NN (직선거리)", PPA_KNN)
            self.cmbPpaGraph.addItem("Distance threshold (반경)", PPA_THRESHOLD)
            self.cmbPpaGraph.addItem("Delaunay (삼각망)", PPA_DELAUNAY)
            self.cmbPpaGraph.addItem("Gabriel graph", PPA_GABRIEL)
            self.cmbPpaGraph.addItem("RNG (Relative neighbor graph)", PPA_RNG)
        except Exception:
            pass

        # Visibility edge rule (for node metrics/components)
        try:
            self.cmbVisEdgeRule.clear()
            self.cmbVisEdgeRule.addItem("상호 보임만 (Mutual)", VIS_RULE_MUTUAL)
            self.cmbVisEdgeRule.addItem("단방향 포함 (Either direction)", VIS_RULE_EITHER)
        except Exception:
            pass

        self._setup_tooltips()

        # Signals
        self.cmbNetworkType.currentIndexChanged.connect(self._on_mode_changed)
        self.cmbSiteLayer.layerChanged.connect(self._on_site_layer_changed)
        try:
            self.cmbPpaGraph.currentIndexChanged.connect(self._update_ppa_controls)
        except Exception:
            pass
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
            self.cmbPpaGraph.setToolTip(
                "PPA 간선(그래프) 생성 규칙입니다.\n"
                "- k-NN: 각 노드에서 가까운 k개 연결\n"
                "- Threshold: 반경 내 모든 쌍 연결\n"
                "- Delaunay/Gabriel/RNG: 스파게티(과도한 간선)를 줄이는 대표적인 근접 그래프"
            )
            self.spinPpaMaxDist.setToolTip(
                "PPA 최대 거리(m) 필터입니다. 0이면 제한 없음.\n"
                "Threshold 그래프에서는 필수 파라미터(0이면 오류)입니다."
            )
        except Exception:
            pass

        try:
            # Per-item tooltip (shown on hover in the dropdown)
            ppa_tips = {
                PPA_KNN: (
                    "k-NN (유클리드 거리)\n"
                    "- 각 노드에서 직선거리로 가까운 k개를 연결합니다.\n"
                    "- k가 커지면 간선이 급증하므로(스파게티) 보통 3~5 권장.\n\n"
                    "Ref:\n"
                    "- Terrell (1977) Human Biogeography in the Solomon Islands.\n"
                    "- Brughmans & Peeples (2017) Trends in archaeological network research."
                ),
                PPA_THRESHOLD: (
                    "Distance threshold (반경)\n"
                    "- 지정 반경 안의 모든 쌍을 연결합니다.\n"
                    "- Max dist(m)가 필수입니다(0이면 의미 없음).\n\n"
                    "Tip:\n"
                    "- 반경이 커지면 간선이 매우 많아질 수 있습니다."
                ),
                PPA_DELAUNAY: (
                    "Delaunay (삼각망)\n"
                    "- 점 집합의 Delaunay 삼각분할 간선만 남깁니다.\n"
                    "- '공간적 이웃'을 과도하게 연결하지 않으면서 전역 구조를 보기에 좋습니다.\n\n"
                    "Ref:\n"
                    "- Delaunay (1934) Sur la sphère vide.\n"
                    "- Okabe, Boots & Sugihara (1992) Spatial Tessellations."
                ),
                PPA_GABRIEL: (
                    "Gabriel graph\n"
                    "- Delaunay 간선 중 '원(지름 AB) 내부에 다른 점이 없을 때'만 남깁니다.\n"
                    "- Delaunay보다 더 희소(sparser)한 근접 그래프입니다.\n\n"
                    "Ref:\n"
                    "- Gabriel & Sokal (1969) A new statistical approach to geographic variation analysis."
                ),
                PPA_RNG: (
                    "RNG (Relative Neighborhood Graph)\n"
                    "- 간선 AB에 대해, A와 B에 동시에 더 가까운 점이 있으면 AB를 제거합니다.\n"
                    "- 매우 희소한 근접 그래프(스파게티 감소)에 유리합니다.\n\n"
                    "Ref:\n"
                    "- Toussaint (1980) The relative neighborhood graph of a finite planar set."
                ),
            }
            for idx in range(int(self.cmbPpaGraph.count())):
                key = str(self.cmbPpaGraph.itemData(idx) or "")
                tip = ppa_tips.get(key, "")
                if tip:
                    self.cmbPpaGraph.setItemData(idx, tip, Qt.ToolTipRole)

            def _sync_ppa_graph_tooltip():
                try:
                    tip = self.cmbPpaGraph.itemData(self.cmbPpaGraph.currentIndex(), Qt.ToolTipRole) or ""
                    self.cmbPpaGraph.setToolTip(str(tip))
                except Exception:
                    pass

            try:
                self.cmbPpaGraph.currentIndexChanged.connect(_sync_ppa_graph_tooltip)
            except Exception:
                pass
            _sync_ppa_graph_tooltip()
        except Exception:
            pass

        try:
            vis_rule_tips = {
                VIS_RULE_MUTUAL: (
                    "Mutual(상호 보임)만 연결\n"
                    "- A↔B 양방향 모두 보일 때만 간선으로 간주합니다.\n"
                    "- '확실한 통신/감시' 관계만 남기고 싶을 때 권장."
                ),
                VIS_RULE_EITHER: (
                    "Either(단방향 포함)\n"
                    "- A→B 또는 B→A 중 하나라도 보이면 간선으로 간주합니다.\n"
                    "- 지형/높이 차로 단방향이 생길 수 있는 상황에서 탐색적으로 유용."
                ),
            }
            for idx in range(int(self.cmbVisEdgeRule.count())):
                key = str(self.cmbVisEdgeRule.itemData(idx) or "")
                tip = vis_rule_tips.get(key, "")
                if tip:
                    self.cmbVisEdgeRule.setItemData(idx, tip, Qt.ToolTipRole)

            def _sync_vis_rule_tooltip():
                try:
                    tip = self.cmbVisEdgeRule.itemData(self.cmbVisEdgeRule.currentIndex(), Qt.ToolTipRole) or ""
                    self.cmbVisEdgeRule.setToolTip(str(tip))
                except Exception:
                    pass

            try:
                self.cmbVisEdgeRule.currentIndexChanged.connect(_sync_vis_rule_tooltip)
            except Exception:
                pass
            _sync_vis_rule_tooltip()
        except Exception:
            pass

        try:
            self.chkCreateNodeMetrics.setToolTip(
                "노드(유적)별 네트워크 지표를 계산한 점 레이어를 추가합니다.\n"
                "기본: degree(연결 수), component(연결된 덩어리)."
            )
            self.chkCloseness.setToolTip("Closeness centrality(근접 중심성)를 계산합니다. 노드가 많으면 느릴 수 있습니다.")
            self.chkBetweenness.setToolTip("Betweenness centrality(매개 중심성)를 계산합니다. 노드가 많으면 매우 느릴 수 있습니다.")
            self.cmbVisEdgeRule.setToolTip(
                "가시성 네트워크에서 '연결'로 간주할 규칙입니다.\n"
                "- Mutual: A↔B 모두 보일 때만 연결\n"
                "- Either: A→B 또는 B→A 중 하나라도 보이면 연결"
            )
        except Exception:
            pass

        try:
            self.btnInterpretGuide.setToolTip(
                "현재 선택한 네트워크(PPA/가시성) 결과를 어떻게 읽어야 하는지\n"
                "해석 가이드를 작은 창으로 표시합니다."
            )
        except Exception:
            pass

    def _ensure_extra_widgets(self):
        """Create optional widgets at runtime (keeps .ui stable and avoids regressions)."""
        # --- PPA graph controls ---
        try:
            if not hasattr(self, "cmbPpaGraph"):
                self.lblPpaGraph = QtWidgets.QLabel("Graph", self.groupPpa)
                self.lblPpaGraph.setObjectName("lblPpaGraph")
                self.cmbPpaGraph = QtWidgets.QComboBox(self.groupPpa)
                self.cmbPpaGraph.setObjectName("cmbPpaGraph")

                self.lblPpaMaxDist = QtWidgets.QLabel("Max dist (m)", self.groupPpa)
                self.lblPpaMaxDist.setObjectName("lblPpaMaxDist")
                self.spinPpaMaxDist = QtWidgets.QDoubleSpinBox(self.groupPpa)
                self.spinPpaMaxDist.setObjectName("spinPpaMaxDist")
                self.spinPpaMaxDist.setDecimals(0)
                self.spinPpaMaxDist.setMinimum(0.0)
                self.spinPpaMaxDist.setMaximum(100000000.0)
                self.spinPpaMaxDist.setValue(0.0)
                self.spinPpaMaxDist.setSuffix(" m")

                try:
                    row = int(self.gridLayout_Ppa.rowCount())
                except Exception:
                    row = 2
                self.gridLayout_Ppa.addWidget(self.lblPpaGraph, row, 0)
                self.gridLayout_Ppa.addWidget(self.cmbPpaGraph, row, 1)
                self.gridLayout_Ppa.addWidget(self.lblPpaMaxDist, row + 1, 0)
                self.gridLayout_Ppa.addWidget(self.spinPpaMaxDist, row + 1, 1)
        except Exception:
            pass

        # --- SNA metrics group ---
        try:
            if not hasattr(self, "groupSna"):
                self.groupSna = QtWidgets.QGroupBox("4. SNA 지표", self)
                self.groupSna.setObjectName("groupSna")
                grid = QtWidgets.QGridLayout(self.groupSna)
                grid.setObjectName("gridLayout_Sna")

                self.chkCreateNodeMetrics = QtWidgets.QCheckBox("노드 지표 레이어(점) 생성", self.groupSna)
                self.chkCreateNodeMetrics.setObjectName("chkCreateNodeMetrics")
                self.chkCreateNodeMetrics.setChecked(True)

                self.chkCloseness = QtWidgets.QCheckBox("Closeness 계산", self.groupSna)
                self.chkCloseness.setObjectName("chkCloseness")
                self.chkCloseness.setChecked(False)

                self.chkBetweenness = QtWidgets.QCheckBox("Betweenness 계산", self.groupSna)
                self.chkBetweenness.setObjectName("chkBetweenness")
                self.chkBetweenness.setChecked(False)

                self.lblVisEdgeRule = QtWidgets.QLabel("LOS 연결 규칙", self.groupSna)
                self.lblVisEdgeRule.setObjectName("lblVisEdgeRule")
                self.cmbVisEdgeRule = QtWidgets.QComboBox(self.groupSna)
                self.cmbVisEdgeRule.setObjectName("cmbVisEdgeRule")

                grid.addWidget(self.chkCreateNodeMetrics, 0, 0, 1, 4)
                grid.addWidget(self.chkCloseness, 1, 0, 1, 2)
                grid.addWidget(self.chkBetweenness, 1, 2, 1, 2)
                grid.addWidget(self.lblVisEdgeRule, 2, 0, 1, 1)
                grid.addWidget(self.cmbVisEdgeRule, 2, 1, 1, 3)

                # Insert above the button row.
                try:
                    idx = max(0, int(self.verticalLayout.count()) - 1)
                    self.verticalLayout.insertWidget(idx, self.groupSna)
                except Exception:
                    try:
                        self.verticalLayout.addWidget(self.groupSna)
                    except Exception:
                        pass
        except Exception:
            pass

        # --- Interpretation guide button (kept in the button row to avoid increasing dialog height) ---
        try:
            if not hasattr(self, "btnInterpretGuide"):
                self.btnInterpretGuide = QtWidgets.QPushButton("해석 가이드", self)
                self.btnInterpretGuide.setObjectName("btnInterpretGuide")
                try:
                    from qgis.core import QgsApplication

                    self.btnInterpretGuide.setIcon(QgsApplication.getThemeIcon("/mActionHelpContents.svg"))
                except Exception:
                    pass

                # Insert just before "실행" so the main buttons stay at the right.
                try:
                    idx = int(self.horizontalLayout_Buttons.indexOf(self.btnRun))
                    if idx >= 0:
                        self.horizontalLayout_Buttons.insertWidget(idx, self.btnInterpretGuide)
                    else:
                        self.horizontalLayout_Buttons.addWidget(self.btnInterpretGuide)
                except Exception:
                    try:
                        self.horizontalLayout_Buttons.addWidget(self.btnInterpretGuide)
                    except Exception:
                        pass

                try:
                    self.btnInterpretGuide.clicked.connect(self._show_interpretation_guide)
                except Exception:
                    pass
        except Exception:
            pass

    def _update_ppa_controls(self):
        """Enable/disable PPA controls depending on the selected graph rule."""
        method = PPA_KNN
        try:
            method = str(self.cmbPpaGraph.currentData() or PPA_KNN)
        except Exception:
            method = PPA_KNN

        use_knn = method == PPA_KNN
        use_thresh = method == PPA_THRESHOLD

        try:
            self.spinPpaK.setEnabled(use_knn)
            self.lblPpaK.setEnabled(use_knn)
        except Exception:
            pass
        try:
            self.chkPpaMutualOnly.setEnabled(use_knn)
        except Exception:
            pass

        try:
            self.spinPpaMaxDist.setEnabled((not use_knn))
            self.lblPpaMaxDist.setEnabled((not use_knn))
        except Exception:
            pass

        # If threshold mode is selected, make it visually clear that max distance is required.
        try:
            if use_thresh:
                self.spinPpaMaxDist.setStyleSheet("font-weight: bold;")
            else:
                self.spinPpaMaxDist.setStyleSheet("")
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

    def _interpretation_guide_html(self) -> str:
        mode = None
        try:
            mode = str(self.cmbNetworkType.currentData() or "")
        except Exception:
            mode = ""

        # Keep it practical: how to read the output layers/fields and when to use each option.
        ppa = """
        <h3>근접성 네트워크 (PPA)</h3>
        <p><b>무엇을 보는가?</b><br>
        지형(DEM)을 무시하고 <b>직선거리(유클리드)</b>로 “이웃” 관계를 연결합니다.
        평원/도서 지역처럼 지형 영향이 약한 환경이나, <b>‘이웃 공동체’</b> 가설을 빠르게 점검할 때 유용합니다.</p>

        <p><b>결과를 어떻게 읽나?</b><br>
        <ul>
          <li><b>Edge 레이어</b>: 유적 간 “이웃” 연결. <code>dist_km</code>는 직선거리(km).</li>
          <li><b>Nodes 레이어(SNA)</b>: <code>degree</code>(연결 수), <code>component</code>(연결 덩어리), <code>comp_size</code>(덩어리 크기).</li>
        </ul></p>

        <p><b>스파게티(선 과다) 줄이는 팁</b><br>
        <ul>
          <li><b>Mutual k‑NN</b>: 서로의 k 안에 들어갈 때만 연결(더 보수적).</li>
          <li><b>Gabriel/RNG</b>: Delaunay에서 더 “필요한” 간선만 남겨 희소화.</li>
          <li><b>Max dist(m)</b>: 너무 먼 간선은 제거(현실적 상호작용 범위 반영).</li>
        </ul></p>
        """

        vis = """
        <h3>가시성 네트워크 (Visibility / LOS)</h3>
        <p><b>무엇을 보는가?</b><br>
        DEM 기반 Line of Sight(가시선)으로 유적 A↔B 사이에 지형이 시선을 가리는지 샘플링해 연결합니다.
        방어·봉수·감시/통신 체계처럼 <b>‘보이는가’</b>가 핵심인 질문에 적합합니다.</p>

        <p><b>결과를 어떻게 읽나?</b><br>
        <ul>
          <li><b>Edge 레이어(LOS)</b>: <code>status</code>로 “상호 보임/단방향 보임/상호 안보임/샘플 실패”를 구분합니다.</li>
          <li><b>방향성</b>: <code>vis_ab</code>, <code>vis_ba</code> (0/1)로 A→B, B→A를 따로 기록합니다.</li>
          <li><b>폴리곤 입력</b>: 경계 샘플링을 켜면 <code>vis_ratio_ab</code>(0~1)처럼 “얼마나 보이는가”를 비율로 확인할 수 있습니다.</li>
        </ul></p>

        <p><b>연산량 줄이는 팁</b><br>
        <ul>
          <li><b>후보 k</b>: 각 노드에서 가까운 후보만 검사(빠름).</li>
          <li><b>반경 내 모든 쌍</b>: 작은 데이터에만 권장(정확하지만 느림).</li>
          <li><b>Max dist(m)</b>: 먼 쌍은 애초에 검사하지 않음.</li>
        </ul></p>
        """

        sna = """
        <h3>SNA 지표(점 레이어)</h3>
        <p><b>왜 필요한가?</b><br>
        “선 몇 개”로 끝나지 않고, <b>중심지/요충지/고립</b>이 어디인지 수치로 드러내기 위함입니다.</p>
        <ul>
          <li><code>degree</code>: 연결 수(많을수록 ‘허브’ 후보).</li>
          <li><code>component</code>/<code>comp_size</code>: 네트워크가 몇 덩어리로 끊기는지, 각 덩어리의 크기.</li>
          <li><code>closeness</code>: 전체에 ‘가까운’ 정도(노드가 많으면 느릴 수 있음).</li>
          <li><code>betweenness</code>: 다른 노드 사이를 ‘중개’하는 정도(매우 느릴 수 있어 큰 데이터는 자동 스킵될 수 있음).</li>
        </ul>
        """

        refs = """
        <h3>참고(요약)</h3>
        <ul>
          <li>근접 그래프: Delaunay(1934), Gabriel &amp; Sokal(1969), Toussaint(1980)</li>
          <li>고고학 네트워크 리뷰: Brughmans &amp; Peeples(2017)</li>
          <li>가시성/가시성 그래프: Gillings &amp; Wheatley(2001), Turner et al.(2001), Van Dyke et al.(2016)</li>
        </ul>
        """

        body = ""
        if mode == NETWORK_VISIBILITY:
            body = vis + sna + ppa
        else:
            body = ppa + sna + vis

        return (
            "<html><head><meta charset='utf-8'></head><body style='font-family:Sans-Serif;'>"
            "<h2>네트워크 해석 가이드</h2>"
            "<p style='color:#444'>Tip: 각 옵션 위에 마우스를 올리면 짧은 설명/참고문헌을 바로 볼 수 있어요.</p>"
            + body
            + refs
            + "</body></html>"
        )

    def _show_interpretation_guide(self):
        try:
            # Reuse if already open (prevents multiple floating dialogs).
            if getattr(self, "_interpretGuideDialog", None) is not None:
                try:
                    if self._interpretGuideDialog.isVisible():
                        self._interpretGuideBrowser.setHtml(self._interpretation_guide_html())
                        self._interpretGuideDialog.raise_()
                        self._interpretGuideDialog.activateWindow()
                        return
                except Exception:
                    pass

            dlg = QtWidgets.QDialog(self)
            dlg.setAttribute(Qt.WA_DeleteOnClose, True)
            dlg.setWindowTitle("해석 가이드 (Network Interpretation)")
            dlg.resize(560, 520)

            layout = QtWidgets.QVBoxLayout(dlg)
            browser = QtWidgets.QTextBrowser(dlg)
            browser.setOpenExternalLinks(True)
            browser.setHtml(self._interpretation_guide_html())
            layout.addWidget(browser)

            row = QtWidgets.QHBoxLayout()
            btn_copy = QtWidgets.QPushButton("복사", dlg)
            btn_close = QtWidgets.QPushButton("닫기", dlg)
            row.addStretch(1)
            row.addWidget(btn_copy)
            row.addWidget(btn_close)
            layout.addLayout(row)

            def _copy():
                try:
                    QtWidgets.QApplication.clipboard().setText(browser.toPlainText())
                except Exception:
                    pass

            btn_copy.clicked.connect(_copy)
            btn_close.clicked.connect(dlg.close)

            # Keep python refs for stability (avoid GC on modeless dialogs).
            self._interpretGuideDialog = dlg
            self._interpretGuideBrowser = browser

            def _clear_refs():
                try:
                    self._interpretGuideDialog = None
                    self._interpretGuideBrowser = None
                except Exception:
                    pass

            try:
                dlg.destroyed.connect(lambda _=None: _clear_refs())
            except Exception:
                pass

            dlg.show()
        except Exception as e:
            log_message(f"InterpretGuide: failed to open guide dialog: {e}", level=Qgis.Warning)

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
        try:
            # Visibility edge rule is only meaningful for LOS.
            self.lblVisEdgeRule.setEnabled(is_vis)
            self.cmbVisEdgeRule.setEnabled(is_vis)
        except Exception:
            pass

        try:
            self._update_ppa_controls()
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
            method = str(getattr(self, "cmbPpaGraph", None).currentData() if hasattr(self, "cmbPpaGraph") else PPA_KNN)
            try:
                max_dist_m = float(self.spinPpaMaxDist.value()) if hasattr(self, "spinPpaMaxDist") else 0.0
            except Exception:
                max_dist_m = 0.0

            make_nodes = bool(getattr(self, "chkCreateNodeMetrics", None) and self.chkCreateNodeMetrics.isChecked())
            do_close = bool(getattr(self, "chkCloseness", None) and self.chkCloseness.isChecked())
            do_betw = bool(getattr(self, "chkBetweenness", None) and self.chkBetweenness.isChecked())

            self._run_ppa(
                nodes,
                method=method,
                k=k,
                mutual_only=mutual,
                max_dist_m=max_dist_m,
                create_node_metrics=make_nodes,
                compute_closeness=do_close,
                compute_betweenness=do_betw,
            )
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

        make_nodes = bool(getattr(self, "chkCreateNodeMetrics", None) and self.chkCreateNodeMetrics.isChecked())
        do_close = bool(getattr(self, "chkCloseness", None) and self.chkCloseness.isChecked())
        do_betw = bool(getattr(self, "chkBetweenness", None) and self.chkBetweenness.isChecked())
        vis_rule = str(
            getattr(self, "cmbVisEdgeRule", None).currentData()
            if hasattr(self, "cmbVisEdgeRule")
            else VIS_RULE_MUTUAL
        )

        self._run_visibility_network(
            dem_layer=dem_layer,
            nodes=nodes,
            obs_height=obs_h,
            tgt_height=tgt_h,
            candidate_k=cand_k,
            max_dist=max_dist,
            sample_step_m=step_m,
            use_poly_boundary_ratio=poly_boundary,
            create_node_metrics=make_nodes,
            compute_closeness=do_close,
            compute_betweenness=do_betw,
            vis_edge_rule=vis_rule,
        )

    def _run_ppa(
        self,
        nodes: List[_Node],
        *,
        method: str,
        k: int,
        mutual_only: bool,
        max_dist_m: float,
        create_node_metrics: bool,
        compute_closeness: bool,
        compute_betweenness: bool,
    ):
        n = int(len(nodes))
        if n < 2:
            return

        method = str(method or PPA_KNN)
        max_dist_m = float(max_dist_m or 0.0)
        if max_dist_m < 0:
            max_dist_m = 0.0

        coords = np.array([(float(nd.x), float(nd.y)) for nd in nodes], dtype=np.float64)

        if method == PPA_THRESHOLD and max_dist_m <= 0.0:
            push_message(self.iface, "PPA", "Threshold 그래프는 '최대 거리(m)'가 필요합니다. (0보다 크게)", level=2)
            restore_ui_focus(self)
            return

        # --- Build edges ---
        edges: Set[Tuple[int, int]] = set()

        if method == PPA_KNN:
            k_eff = max(1, min(int(k), max(1, n - 1)))
            log_message(f"PPA: k-NN building (n={n}, k={k_eff}, mutual={bool(mutual_only)})", level=Qgis.Info)

            neigh: List[Set[int]] = [set() for _ in range(n)]
            for i in range(n):
                d2 = (coords[:, 0] - coords[i, 0]) ** 2 + (coords[:, 1] - coords[i, 1]) ** 2
                d2[i] = np.inf
                nn = np.argsort(d2)[:k_eff]
                for j in nn:
                    neigh[i].add(int(j))

            for i in range(n):
                for j in neigh[i]:
                    a, b = (i, j) if i < j else (j, i)
                    if mutual_only:
                        if i in neigh[j]:
                            edges.add((a, b))
                    else:
                        edges.add((a, b))

            layer_name = f"PPA_kNN_{k_eff}" + ("_mutual" if mutual_only else "")

        elif method == PPA_THRESHOLD:
            r2 = float(max_dist_m) ** 2
            log_message(f"PPA: threshold building (n={n}, max_dist_m={max_dist_m})", level=Qgis.Info)
            for i in range(n - 1):
                dx = coords[i + 1 :, 0] - coords[i, 0]
                dy = coords[i + 1 :, 1] - coords[i, 1]
                d2 = dx * dx + dy * dy
                js = np.where(d2 <= r2)[0]
                for j_off in js:
                    j = int(i + 1 + int(j_off))
                    edges.add((i, j))
            layer_name = f"PPA_threshold_{int(round(max_dist_m))}m"

        else:
            crs_authid = (
                self.cmbSiteLayer.currentLayer().crs().authid()
                if self.cmbSiteLayer.currentLayer()
                else QgsProject.instance().crs().authid()
            )
            cand = self._ppa_delaunay_edges(nodes=nodes, crs_authid=crs_authid)
            if not cand:
                push_message(self.iface, "PPA", "Delaunay 기반 간선을 만들 수 없습니다. (점이 너무 적거나 중복일 수 있음)", level=2)
                restore_ui_focus(self)
                return

            if method == PPA_GABRIEL:
                edges = self._ppa_filter_gabriel(cand_edges=cand, coords=coords)
                layer_name = "PPA_gabriel"
            elif method == PPA_RNG:
                edges = self._ppa_filter_rng(cand_edges=cand, coords=coords)
                layer_name = "PPA_rng"
            else:
                edges = set(cand)
                layer_name = "PPA_delaunay"

            if max_dist_m > 0.0:
                edges = self._filter_edges_max_dist(edges=edges, coords=coords, max_dist_m=max_dist_m)
                layer_name = f"{layer_name}_max{int(round(max_dist_m))}m"

        # --- Output layers ---
        crs_authid = (
            self.cmbSiteLayer.currentLayer().crs().authid()
            if self.cmbSiteLayer.currentLayer()
            else QgsProject.instance().crs().authid()
        )

        push_message(self.iface, "PPA", f"근접성 네트워크 생성 중... (노드 {n}, 간선 {len(edges)})", level=0, duration=4)
        QtWidgets.QApplication.processEvents()

        edge_layer, run_group, run_id = self._add_edge_layer(
            nodes=nodes,
            edges=sorted(edges),
            layer_name=layer_name,
            color=QColor(80, 80, 80, 220),
            add_dist=True,
            crs_authid=crs_authid,
        )

        # Node metrics (SNA) layer
        if create_node_metrics:
            self._add_node_metrics_layer(
                nodes=nodes,
                edges=set(edges),
                crs_authid=crs_authid,
                run_group=run_group,
                run_id=run_id,
                title="PPA_Nodes",
                compute_closeness=compute_closeness,
                compute_betweenness=compute_betweenness,
            )

        # Summary
        deg = self._degrees(n, edges)
        comps, comp_sizes = self._components(n, edges)
        msg = (
            f"완료: 노드 {n} / 간선 {len(edges)}  "
            f"(평균 degree {float(sum(deg))/max(1,n):.2f}, components {len(comp_sizes)})"
        )
        log_message(f"PPA: {msg}  [method={method}]", level=Qgis.Info)
        push_message(self.iface, "PPA", msg, level=0, duration=7)
        self.accept()

    def _degrees(self, n: int, edges: Set[Tuple[int, int]]) -> List[int]:
        deg = [0] * int(n)
        for a, b in edges:
            try:
                deg[int(a)] += 1
                deg[int(b)] += 1
            except Exception:
                continue
        return deg

    def _components(self, n: int, edges: Set[Tuple[int, int]]) -> Tuple[List[int], Dict[int, int]]:
        parent = list(range(int(n)))
        rank = [0] * int(n)

        def find(x: int) -> int:
            x = int(x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int):
            ra = find(a)
            rb = find(b)
            if ra == rb:
                return
            if rank[ra] < rank[rb]:
                parent[ra] = rb
            elif rank[ra] > rank[rb]:
                parent[rb] = ra
            else:
                parent[rb] = ra
                rank[ra] += 1

        for a, b in edges:
            try:
                union(int(a), int(b))
            except Exception:
                continue

        roots = [find(i) for i in range(int(n))]
        # Compact component IDs (0..k-1)
        remap: Dict[int, int] = {}
        comp_id: List[int] = [0] * int(n)
        for i, r in enumerate(roots):
            if r not in remap:
                remap[r] = len(remap)
            comp_id[i] = remap[r]

        comp_sizes: Dict[int, int] = {}
        for cid in comp_id:
            comp_sizes[int(cid)] = comp_sizes.get(int(cid), 0) + 1

        return comp_id, comp_sizes

    def _filter_edges_max_dist(
        self, *, edges: Set[Tuple[int, int]], coords: np.ndarray, max_dist_m: float
    ) -> Set[Tuple[int, int]]:
        if not edges:
            return set()
        r2 = float(max_dist_m) ** 2
        out: Set[Tuple[int, int]] = set()
        for a, b in edges:
            try:
                dx = float(coords[a, 0] - coords[b, 0])
                dy = float(coords[a, 1] - coords[b, 1])
                if (dx * dx + dy * dy) <= r2:
                    out.add((int(a), int(b)))
            except Exception:
                continue
        return out

    def _ppa_delaunay_edges(self, *, nodes: List[_Node], crs_authid: str) -> Set[Tuple[int, int]]:
        """Return candidate edges from a Delaunay triangulation (best-effort, uses QGIS Processing)."""
        n = int(len(nodes))
        if n < 3:
            return set()

        try:
            pt_layer = QgsVectorLayer(f"Point?crs={crs_authid}", "PPA_points_tmp", "memory")
            pr = pt_layer.dataProvider()
            pr.addAttributes([QgsField("idx", QVariant.Int)])
            pt_layer.updateFields()

            feats = []
            for i, nd in enumerate(nodes):
                f = QgsFeature(pt_layer.fields())
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(nd.x), float(nd.y))))
                f["idx"] = int(i)
                feats.append(f)
            pr.addFeatures(feats)
            pt_layer.updateExtents()
        except Exception as e:
            log_message(f"PPA: failed to build temp point layer for Delaunay: {e}", level=Qgis.Warning)
            return set()

        tri_layer = None
        last_err = None
        for alg_id in ("qgis:delaunaytriangulation", "native:delaunaytriangulation"):
            try:
                res = processing.run(alg_id, {"INPUT": pt_layer, "OUTPUT": "memory:"})
                out = res.get("OUTPUT")
                if isinstance(out, QgsVectorLayer):
                    tri_layer = out
                elif isinstance(out, str) and out:
                    tri_layer = QgsVectorLayer(out, "Delaunay", "ogr")
                if tri_layer is not None and tri_layer.isValid():
                    break
            except Exception as e:
                last_err = e
                tri_layer = None

        if tri_layer is None or (not tri_layer.isValid()):
            log_message(f"PPA: Delaunay algorithm not available/failed: {last_err}", level=Qgis.Warning)
            return set()

        # Map vertex coordinates back to node indices (rounded)
        lookup: Dict[Tuple[int, int], int] = {}
        for i, nd in enumerate(nodes):
            key = (int(round(float(nd.x) * 1000.0)), int(round(float(nd.y) * 1000.0)))
            lookup[key] = int(i)

        coords = np.array([(float(nd.x), float(nd.y)) for nd in nodes], dtype=np.float64)

        def _idx_for_xy(x: float, y: float) -> Optional[int]:
            key = (int(round(float(x) * 1000.0)), int(round(float(y) * 1000.0)))
            if key in lookup:
                return int(lookup[key])
            # Fallback: nearest
            try:
                d2 = (coords[:, 0] - float(x)) ** 2 + (coords[:, 1] - float(y)) ** 2
                j = int(np.argmin(d2))
                if float(d2[j]) <= 1e-6:
                    return j
            except Exception:
                return None
            return None

        edges: Set[Tuple[int, int]] = set()
        for ft in tri_layer.getFeatures():
            try:
                geom = ft.geometry()
                if geom is None or geom.isEmpty():
                    continue
                polys = geom.asPolygon()
                if not polys:
                    mp = geom.asMultiPolygon()
                    if mp and mp[0]:
                        polys = mp[0]
                if not polys or not polys[0]:
                    continue
                ring = polys[0]
                if len(ring) >= 2 and ring[0] == ring[-1]:
                    ring = ring[:-1]

                idxs: List[int] = []
                for p in ring:
                    j = _idx_for_xy(p.x(), p.y())
                    if j is not None:
                        idxs.append(int(j))
                idxs = list(dict.fromkeys(idxs))  # stable unique
                if len(idxs) < 3:
                    continue
                a, b, c = idxs[0], idxs[1], idxs[2]
                for u, v in ((a, b), (b, c), (c, a)):
                    uu, vv = (u, v) if u < v else (v, u)
                    if uu != vv:
                        edges.add((uu, vv))
            except Exception:
                continue

        return edges

    def _ppa_filter_gabriel(self, *, cand_edges: Set[Tuple[int, int]], coords: np.ndarray) -> Set[Tuple[int, int]]:
        """Gabriel graph filter (usually applied on Delaunay candidate edges)."""
        out: Set[Tuple[int, int]] = set()
        n = int(coords.shape[0])
        eps = 1e-9
        for a, b in cand_edges:
            a = int(a)
            b = int(b)
            if a == b:
                continue
            midx = 0.5 * (coords[a, 0] + coords[b, 0])
            midy = 0.5 * (coords[a, 1] + coords[b, 1])
            r2 = ((coords[a, 0] - midx) ** 2) + ((coords[a, 1] - midy) ** 2)  # (d/2)^2

            d2 = (coords[:, 0] - midx) ** 2 + (coords[:, 1] - midy) ** 2
            d2[a] = np.inf
            d2[b] = np.inf
            if float(np.min(d2)) >= float(r2) - eps:
                out.add((a, b) if a < b else (b, a))
        return out

    def _ppa_filter_rng(self, *, cand_edges: Set[Tuple[int, int]], coords: np.ndarray) -> Set[Tuple[int, int]]:
        """Relative Neighborhood Graph (RNG) filter (usually applied on Delaunay candidate edges)."""
        out: Set[Tuple[int, int]] = set()
        eps = 1e-9
        for a, b in cand_edges:
            a = int(a)
            b = int(b)
            if a == b:
                continue
            dx = float(coords[a, 0] - coords[b, 0])
            dy = float(coords[a, 1] - coords[b, 1])
            dij2 = dx * dx + dy * dy

            d2a = (coords[:, 0] - coords[a, 0]) ** 2 + (coords[:, 1] - coords[a, 1]) ** 2
            d2b = (coords[:, 0] - coords[b, 0]) ** 2 + (coords[:, 1] - coords[b, 1]) ** 2
            d2a[a] = np.inf
            d2a[b] = np.inf
            d2b[a] = np.inf
            d2b[b] = np.inf

            if not bool(np.any(np.maximum(d2a, d2b) < (float(dij2) - eps))):
                out.add((a, b) if a < b else (b, a))
        return out

    def _add_node_metrics_layer(
        self,
        *,
        nodes: List[_Node],
        edges: Set[Tuple[int, int]],
        crs_authid: str,
        run_group,
        run_id: str,
        title: str,
        compute_closeness: bool,
        compute_betweenness: bool,
        extra_node_fields: Optional[List[QgsField]] = None,
        extra_values_by_node: Optional[Dict[int, Dict[str, Any]]] = None,
    ):
        n = int(len(nodes))
        if n <= 0:
            return

        deg = self._degrees(n, edges)
        comp_id, comp_sizes = self._components(n, edges)

        # Build adjacency
        adj: List[List[int]] = [[] for _ in range(n)]
        for a, b in edges:
            a = int(a)
            b = int(b)
            if a == b:
                continue
            adj[a].append(b)
            adj[b].append(a)

        # Advanced SNA metrics can be expensive; guard for large graphs.
        if n > 500 and (compute_closeness or compute_betweenness):
            log_message(
                f"SNA: advanced metrics skipped (n={n} too large). Use smaller selection or disable advanced metrics.",
                level=Qgis.Warning,
            )
            compute_closeness = False
            compute_betweenness = False

        closeness = None
        if compute_closeness:
            closeness = self._closeness_centrality(n=n, adj=adj)

        betweenness = None
        if compute_betweenness:
            betweenness = self._betweenness_centrality(n=n, adj=adj)

        layer = QgsVectorLayer(f"Point?crs={crs_authid}", title, "memory")
        pr = layer.dataProvider()
        fields = [
            QgsField("fid", QVariant.String),
            QgsField("name", QVariant.String),
            QgsField("degree", QVariant.Int),
            QgsField("component", QVariant.Int),
            QgsField("comp_size", QVariant.Int),
        ]
        if compute_closeness:
            fields.append(QgsField("closeness", QVariant.Double))
        if compute_betweenness:
            fields.append(QgsField("betweenness", QVariant.Double))
        if extra_node_fields:
            fields.extend(extra_node_fields)
        pr.addAttributes(fields)
        layer.updateFields()

        feats = []
        for i, nd in enumerate(nodes):
            f = QgsFeature(layer.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(nd.x), float(nd.y))))
            f["fid"] = str(nd.fid)
            f["name"] = str(nd.name)
            f["degree"] = int(deg[i])
            f["component"] = int(comp_id[i])
            f["comp_size"] = int(comp_sizes.get(int(comp_id[i]), 1))
            if compute_closeness and closeness is not None:
                try:
                    f["closeness"] = float(closeness[i])
                except Exception:
                    f["closeness"] = 0.0
            if compute_betweenness and betweenness is not None:
                try:
                    f["betweenness"] = float(betweenness[i])
                except Exception:
                    f["betweenness"] = 0.0
            if extra_values_by_node and i in extra_values_by_node:
                for k, v in (extra_values_by_node.get(i) or {}).items():
                    try:
                        f[str(k)] = v
                    except Exception:
                        pass
            feats.append(f)

        pr.addFeatures(feats)
        layer.updateExtents()

        # Styling: degree-based graduated colors (simple, readable)
        try:
            vmax = int(max(deg) if deg else 0)
            if vmax <= 0:
                sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "255,0,0,200", "size": "3"})
                layer.setRenderer(QgsSingleSymbolRenderer(sym))
            else:
                classes = 5
                vmin = int(min(deg) if deg else 0)
                step = max(1.0, (float(vmax) - float(vmin)) / float(classes))
                ranges: List[QgsRendererRange] = []
                for i in range(classes):
                    lo = float(vmin) + float(i) * step
                    hi = float(vmax) if i == classes - 1 else (float(vmin) + float(i + 1) * step)
                    t = 0.0 if classes <= 1 else float(i) / float(classes - 1)
                    r = int(255)
                    g = int(round(240.0 * (1.0 - t)))
                    b = int(round(120.0 * (1.0 - t)))
                    col = QColor(r, g, b, 220)
                    sym = QgsMarkerSymbol.createSimple(
                        {"name": "circle", "color": f"{col.red()},{col.green()},{col.blue()},{col.alpha()}", "size": f"{3.0 + 2.0 * t:.1f}"}
                    )
                    label = f"{int(round(lo))}–{int(round(hi))}"
                    ranges.append(QgsRendererRange(lo, hi, sym, label))
                renderer = QgsGraduatedSymbolRenderer("degree", ranges)
                layer.setRenderer(renderer)
        except Exception:
            pass

        # Labels (name)
        try:
            pal = QgsPalLayerSettings()
            pal.enabled = True
            pal.fieldName = "name"
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
        except Exception:
            pass

        project = QgsProject.instance()
        try:
            set_archtoolkit_layer_metadata(
                layer,
                tool_id="spatial_network",
                run_id=str(run_id),
                kind="nodes_metrics",
                units="",
                params={"title": str(title or "")},
            )
        except Exception:
            pass
        project.addMapLayer(layer, False)
        try:
            run_group.addLayer(layer)
        except Exception:
            project.addMapLayer(layer)

    def _closeness_centrality(self, *, n: int, adj: List[List[int]]) -> List[float]:
        out = [0.0] * int(n)
        for s in range(int(n)):
            dist = [-1] * int(n)
            dist[s] = 0
            q = deque([s])
            while q:
                v = q.popleft()
                for w in adj[v]:
                    if dist[w] < 0:
                        dist[w] = dist[v] + 1
                        q.append(w)
            reachable = [d for d in dist if d > 0]
            if not reachable:
                out[s] = 0.0
            else:
                out[s] = float(len(reachable)) / float(sum(reachable))
        return out

    def _betweenness_centrality(self, *, n: int, adj: List[List[int]]) -> List[float]:
        """Brandes betweenness for unweighted undirected graphs (no external deps)."""
        bc = [0.0] * int(n)
        for s in range(int(n)):
            stack: List[int] = []
            pred: List[List[int]] = [[] for _ in range(int(n))]
            sigma = [0.0] * int(n)
            sigma[s] = 1.0
            dist = [-1] * int(n)
            dist[s] = 0
            q = deque([s])

            while q:
                v = q.popleft()
                stack.append(v)
                for w in adj[v]:
                    if dist[w] < 0:
                        q.append(w)
                        dist[w] = dist[v] + 1
                    if dist[w] == dist[v] + 1:
                        sigma[w] += sigma[v]
                        pred[w].append(v)

            delta = [0.0] * int(n)
            while stack:
                w = stack.pop()
                for v in pred[w]:
                    if sigma[w] > 0:
                        delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
                if w != s:
                    bc[w] += delta[w]

        # Undirected normalization: each shortest path counted twice.
        for i in range(int(n)):
            bc[i] = bc[i] * 0.5
        return bc

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
        create_node_metrics: bool = True,
        compute_closeness: bool = False,
        compute_betweenness: bool = False,
        vis_edge_rule: str = VIS_RULE_MUTUAL,
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
        extra_by_edge: Dict[Tuple[int, int], Dict[str, Any]] = {}
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

        def _status_from_vis(vis: Optional[bool]) -> str:
            if vis is None:
                return "샘플 실패"
            return "보임" if bool(vis) else "안보임"

        def _eval_pair(a: int, b: int) -> Tuple[str, float, Dict[str, Any]]:
            """Evaluate visibility for pair (a<b), returning (edge_status, ratio_mean, extras)."""
            a = int(a)
            b = int(b)
            vis_ab = None
            vis_ba = None
            r_ab = None
            r_ba = None

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
                # Convert ratio -> visible bool (any visible sample)
                vis_ab = None if r_ab is None else bool(float(r_ab) > 0.0)
                vis_ba = None if r_ba is None else bool(float(r_ba) > 0.0)
            else:
                vis_ab = self._los_visible(
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
                if abs(float(obs_height) - float(tgt_height)) <= 1e-9:
                    vis_ba = vis_ab
                else:
                    vis_ba = self._los_visible(
                        dem_layer=dem_layer,
                        provider=provider,
                        ax=nodes[b].x,
                        ay=nodes[b].y,
                        bx=nodes[a].x,
                        by=nodes[a].y,
                        obs_height=obs_height,
                        tgt_height=tgt_height,
                        sample_step_m=sample_step_m,
                    )
                # Point nodes: ratio is 0/1 when valid
                r_ab = None if vis_ab is None else (1.0 if bool(vis_ab) else 0.0)
                r_ba = None if vis_ba is None else (1.0 if bool(vis_ba) else 0.0)

            status_ab = _status_from_vis(vis_ab)
            status_ba = _status_from_vis(vis_ba)

            # Aggregate ratio
            vals = [v for v in (r_ab, r_ba) if v is not None]
            ratio_mean = float(sum(vals) / float(len(vals))) if vals else 0.0

            # Aggregate status for styling
            if status_ab == "샘플 실패" or status_ba == "샘플 실패":
                edge_status = "샘플 실패"
            else:
                vab = bool(vis_ab)
                vba = bool(vis_ba)
                if vab and vba:
                    edge_status = "상호 보임"
                elif vab or vba:
                    edge_status = "단방향 보임"
                else:
                    edge_status = "상호 안보임"

            extras = {
                "status_ab": status_ab,
                "status_ba": status_ba,
                "vis_ab": int(bool(vis_ab)) if vis_ab is not None else 0,
                "vis_ba": int(bool(vis_ba)) if vis_ba is not None else 0,
                "vis_ratio_ab": float(r_ab) if r_ab is not None else 0.0,
                "vis_ratio_ba": float(r_ba) if r_ba is not None else 0.0,
                "vis_ratio": float(ratio_mean),
                "mutual": int(1 if (edge_status == "상호 보임") else 0),
            }
            return edge_status, float(ratio_mean), extras

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
                    edge_status, ratio_mean, extras = _eval_pair(i, j)
                    if edge_status == "샘플 실패":
                        failed_pairs += 1

                    edges.add((i, j))
                    status_by_edge[(i, j)] = edge_status
                    ratio_by_edge[(i, j)] = ratio_mean
                    extra_by_edge[(i, j)] = extras

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
                    edge_status, ratio_mean, extras = _eval_pair(a, b)
                    if edge_status == "샘플 실패":
                        failed_pairs += 1

                    edges.add((a, b))
                    status_by_edge[(a, b)] = edge_status
                    ratio_by_edge[(a, b)] = ratio_mean
                    extra_by_edge[(a, b)] = extras

                progress.setValue(i + 1)
                QtWidgets.QApplication.processEvents()

        extra_fields = [
            QgsField("status_ab", QVariant.String),
            QgsField("status_ba", QVariant.String),
            QgsField("vis_ab", QVariant.Int),
            QgsField("vis_ba", QVariant.Int),
            QgsField("vis_ratio_ab", QVariant.Double),
            QgsField("vis_ratio_ba", QVariant.Double),
            QgsField("mutual", QVariant.Int),
        ]

        edge_layer, run_group, run_id = self._add_edge_layer(
            nodes=nodes,
            edges=sorted(edges),
            layer_name="Visibility_LOS",
            color=QColor(0, 160, 80, 220),
            add_dist=True,
            crs_authid=dem_layer.crs().authid(),
            status_by_edge=status_by_edge,
            ratio_by_edge=ratio_by_edge,
            extra_fields=extra_fields,
            extra_values_by_edge=extra_by_edge,
            label_distance=bool(tested_pairs <= 300),
        )

        # Node metrics layer (SNA)
        if create_node_metrics:
            edges_for_metrics: Set[Tuple[int, int]] = set()
            out_deg = [0] * int(n)
            in_deg = [0] * int(n)
            for (a, b), ex in extra_by_edge.items():
                try:
                    va = int(ex.get("vis_ab", 0))
                    vb = int(ex.get("vis_ba", 0))
                    out_deg[int(a)] += va
                    in_deg[int(b)] += va
                    out_deg[int(b)] += vb
                    in_deg[int(a)] += vb

                    if str(vis_edge_rule or VIS_RULE_MUTUAL) == VIS_RULE_EITHER:
                        if va or vb:
                            edges_for_metrics.add((int(a), int(b)))
                    else:
                        if va and vb:
                            edges_for_metrics.add((int(a), int(b)))
                except Exception:
                    continue

            extra_node_fields = [
                QgsField("out_deg", QVariant.Int),
                QgsField("in_deg", QVariant.Int),
                QgsField("vis_total", QVariant.Int),
            ]
            extra_values_by_node: Dict[int, Dict[str, Any]] = {}
            for i0 in range(int(n)):
                extra_values_by_node[int(i0)] = {
                    "out_deg": int(out_deg[i0]),
                    "in_deg": int(in_deg[i0]),
                    "vis_total": int(out_deg[i0] + in_deg[i0]),
                }

            self._add_node_metrics_layer(
                nodes=nodes,
                edges=edges_for_metrics,
                crs_authid=dem_layer.crs().authid(),
                run_group=run_group,
                run_id=run_id,
                title="LOS_Nodes",
                compute_closeness=compute_closeness,
                compute_betweenness=compute_betweenness,
                extra_node_fields=extra_node_fields,
                extra_values_by_node=extra_values_by_node,
            )

        mutual_edges = sum(1 for v in status_by_edge.values() if v == "상호 보임")
        oneway_edges = sum(1 for v in status_by_edge.values() if v == "단방향 보임")
        hidden_edges = sum(1 for v in status_by_edge.values() if v == "상호 안보임")
        fail_edges = sum(1 for v in status_by_edge.values() if v == "샘플 실패")

        msg = (
            f"완료: 검사쌍 {tested_pairs}개 (상호보임 {mutual_edges}, 단방향 {oneway_edges}, "
            f"상호안보임 {hidden_edges}, 실패 {fail_edges})"
        )
        if use_poly_boundary_ratio:
            msg += "  [vis_ratio]"
        log_message(
            f"VisibilityNetwork: {msg} (all_pairs={all_pairs}, max_dist={max_dist}, poly_ratio={use_poly_boundary_ratio}, rule={vis_edge_rule})",
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
        extra_fields: Optional[List[QgsField]] = None,
        extra_values_by_edge: Optional[Dict[Tuple[int, int], Dict[str, Any]]] = None,
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
        if extra_fields:
            fields.extend(extra_fields)
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
            if extra_values_by_edge is not None and (a, b) in extra_values_by_edge:
                for k, v in (extra_values_by_edge.get((a, b)) or {}).items():
                    try:
                        f[str(k)] = v
                    except Exception:
                        pass
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

            # Backward compatible labels (older builds used "보임/안보임").
            categories.append(QgsRendererCategory("상호 보임", _mk_sym(QColor(0, 180, 0, 230)), "상호 보임"))
            categories.append(QgsRendererCategory("보임", _mk_sym(QColor(0, 180, 0, 230)), "보임"))
            categories.append(QgsRendererCategory("단방향 보임", _mk_sym(QColor(240, 140, 0, 220), dashed=True), "단방향 보임"))
            categories.append(QgsRendererCategory("상호 안보임", _mk_sym(QColor(220, 0, 0, 190), dashed=True), "상호 안보임"))
            categories.append(QgsRendererCategory("안보임", _mk_sym(QColor(220, 0, 0, 190), dashed=True), "안보임"))
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

        try:
            set_archtoolkit_layer_metadata(
                layer,
                tool_id="spatial_network",
                run_id=str(run_id),
                kind="edges",
                units="m",
                params={
                    "layer_name": str(layer_name or ""),
                    "add_dist": bool(add_dist),
                    "has_status": bool(status_by_edge is not None),
                    "has_ratio": bool(ratio_by_edge is not None),
                },
            )
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

        return layer, run_group, run_id
