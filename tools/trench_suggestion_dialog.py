# -*- coding: utf-8 -*-
"""
Trench suggestion tool (MVP) for ArchToolkit.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsRaster,
    QgsRasterBandStats,
    QgsRasterLayer,
    QgsSingleSymbolRenderer,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox

import processing

from .help_dialog import show_help_dialog
from .live_log_dialog import ensure_live_log_dialog
from .utils import (
    cleanup_files,
    is_metric_crs,
    log_exception,
    log_message,
    new_run_id,
    push_message,
    restore_ui_focus,
    set_archtoolkit_layer_metadata,
)

_GRAVE_KEYWORDS = (
    "묘",
    "무덤",
    "분묘",
    "묘지",
    "묘역",
    "봉분",
    "고분",
    "고분군",
    "tomb",
    "grave",
    "burial",
    "cemetery",
)

_CODE_RE = re.compile(r"\b([A-Z][0-9]{7,8})\b")


def _safe_float(v, default=None):
    try:
        f = float(v)
        if math.isfinite(f):
            return f
    except Exception:
        pass
    return default


def _unary_union_geom(layer: QgsVectorLayer, *, selected_only: bool) -> Tuple[Optional[QgsGeometry], int]:
    if layer is None or not isinstance(layer, QgsVectorLayer):
        return None, 0
    geoms: List[QgsGeometry] = []
    n = 0
    try:
        feats = layer.selectedFeatures() if selected_only and layer.selectedFeatureCount() > 0 else layer.getFeatures()
    except Exception:
        feats = []
    for ft in feats:
        n += 1
        try:
            g = ft.geometry()
            if g is not None and (not g.isEmpty()):
                geoms.append(g)
        except Exception:
            continue
    if not geoms:
        return None, 0
    try:
        return (geoms[0], n) if len(geoms) == 1 else (QgsGeometry.unaryUnion(geoms), n)
    except Exception:
        try:
            out = geoms[0]
            for g in geoms[1:]:
                out = out.combine(g)
            return out, n
        except Exception:
            return None, 0


def _transform_geom(geom: QgsGeometry, src_crs, dst_crs) -> Optional[QgsGeometry]:
    if geom is None or geom.isEmpty():
        return None
    if src_crs == dst_crs:
        return QgsGeometry(geom)
    try:
        g = QgsGeometry(geom)
        g.transform(QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance()))
        return g
    except Exception:
        return None


def _transform_point(pt: QgsPointXY, src_crs, dst_crs) -> Optional[QgsPointXY]:
    if pt is None:
        return None
    if src_crs == dst_crs:
        return QgsPointXY(pt)
    try:
        return QgsPointXY(QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance()).transform(pt))
    except Exception:
        return None


def _bearing_to_unit_vec(bearing_deg: float) -> Tuple[float, float]:
    th = math.radians(90.0 - float(bearing_deg))
    return math.cos(th), math.sin(th)


def _rect_geom_from_center(center: QgsPointXY, *, length_m: float, width_m: float, bearing_deg: float) -> Optional[QgsGeometry]:
    hl = max(0.01, float(length_m)) * 0.5
    hw = max(0.01, float(width_m)) * 0.5
    ux, uy = _bearing_to_unit_vec(bearing_deg)
    vx, vy = -uy, ux
    cx = float(center.x())
    cy = float(center.y())
    p1 = QgsPointXY(cx + ux * hl + vx * hw, cy + uy * hl + vy * hw)
    p2 = QgsPointXY(cx + ux * hl - vx * hw, cy + uy * hl - vy * hw)
    p3 = QgsPointXY(cx - ux * hl - vx * hw, cy - uy * hl - vy * hw)
    p4 = QgsPointXY(cx - ux * hl + vx * hw, cy - uy * hl + vy * hw)
    try:
        return QgsGeometry.fromPolygonXY([[p1, p2, p3, p4, p1]])
    except Exception:
        return None


class TrenchSuggestionDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._grave_codes_cache: Optional[Set[str]] = None
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("트렌치 후보 제안 (Trench Suggestion) - ArchToolkit")
        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            icon_path = None
            for icon_name in ("trench.png", "terrain_icon.png", "icon.png"):
                p = os.path.join(plugin_dir, icon_name)
                if os.path.exists(p):
                    icon_path = p
                    break
            if icon_path:
                self.setWindowIcon(QIcon(icon_path))
        except Exception:
            pass

        self.setMinimumWidth(680)
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel(
            "<b>트렌치 후보 제안 (보조 도구)</b><br>"
            "AOI 내부에서 지형·AHP·주변 유적 맥락을 기준으로 트렌치 배치 후보를 자동 제안합니다.<br>"
            "<b>주의:</b> 본 결과는 조사 설계 보조용 가설이며, 매장문화재 존재를 보장하지 않습니다."
        )
        header.setWordWrap(True)
        header.setStyleSheet("background:#e8f5e9; padding:10px; border:1px solid #c8e6c9; border-radius:4px;")
        layout.addWidget(header)

        grp_in = QtWidgets.QGroupBox("1. 기본 입력")
        form_in = QtWidgets.QFormLayout(grp_in)
        self.cmbAoi = QgsMapLayerComboBox(grp_in)
        try:
            poly_filter = QgsMapLayerProxyModel.Filter.PolygonLayer
        except Exception:
            poly_filter = QgsMapLayerProxyModel.PolygonLayer
        self.cmbAoi.setFilters(poly_filter)
        form_in.addRow("AOI 폴리곤:", self.cmbAoi)

        self.chkAoiSelectedOnly = QtWidgets.QCheckBox("AOI 선택 피처만 사용")
        self.chkAoiSelectedOnly.setChecked(True)
        form_in.addRow("", self.chkAoiSelectedOnly)

        self.cmbDem = QgsMapLayerComboBox(grp_in)
        try:
            ras_filter = QgsMapLayerProxyModel.Filter.RasterLayer
        except Exception:
            ras_filter = QgsMapLayerProxyModel.RasterLayer
        self.cmbDem.setFilters(ras_filter)
        form_in.addRow("DEM:", self.cmbDem)

        self.cmbAhp = QgsMapLayerComboBox(grp_in)
        self.cmbAhp.setFilters(ras_filter)
        form_in.addRow("AHP 적합도 래스터(선택):", self.cmbAhp)
        layout.addWidget(grp_in)

        grp_tr = QtWidgets.QGroupBox("2. 트렌치 설정")
        grid_tr = QtWidgets.QGridLayout(grp_tr)

        self.spinWidth = QtWidgets.QDoubleSpinBox()
        self.spinWidth.setRange(0.5, 100.0)
        self.spinWidth.setDecimals(1)
        self.spinWidth.setValue(2.0)
        self.spinWidth.setSuffix(" m")

        self.spinLength = QtWidgets.QDoubleSpinBox()
        self.spinLength.setRange(2.0, 500.0)
        self.spinLength.setDecimals(1)
        self.spinLength.setValue(20.0)
        self.spinLength.setSuffix(" m")

        self.spinCount = QtWidgets.QSpinBox()
        self.spinCount.setRange(1, 300)
        self.spinCount.setValue(12)

        self.spinGrid = QtWidgets.QDoubleSpinBox()
        self.spinGrid.setRange(1.0, 200.0)
        self.spinGrid.setDecimals(1)
        self.spinGrid.setValue(10.0)
        self.spinGrid.setSuffix(" m")

        self.spinMinSpacing = QtWidgets.QDoubleSpinBox()
        self.spinMinSpacing.setRange(0.0, 500.0)
        self.spinMinSpacing.setDecimals(1)
        self.spinMinSpacing.setValue(6.0)
        self.spinMinSpacing.setSuffix(" m")

        self.spinInsidePct = QtWidgets.QDoubleSpinBox()
        self.spinInsidePct.setRange(10.0, 100.0)
        self.spinInsidePct.setDecimals(0)
        self.spinInsidePct.setValue(95.0)
        self.spinInsidePct.setSuffix(" %")

        self.cmbOrientation = QtWidgets.QComboBox()
        self.cmbOrientation.addItem("등고선 직교 (기본)", "orthogonal")
        self.cmbOrientation.addItem("등고선 평행", "parallel")

        grid_tr.addWidget(QtWidgets.QLabel("폭:"), 0, 0)
        grid_tr.addWidget(self.spinWidth, 0, 1)
        grid_tr.addWidget(QtWidgets.QLabel("길이:"), 0, 2)
        grid_tr.addWidget(self.spinLength, 0, 3)
        grid_tr.addWidget(QtWidgets.QLabel("제안 개수:"), 1, 0)
        grid_tr.addWidget(self.spinCount, 1, 1)
        grid_tr.addWidget(QtWidgets.QLabel("후보 격자 간격:"), 1, 2)
        grid_tr.addWidget(self.spinGrid, 1, 3)
        grid_tr.addWidget(QtWidgets.QLabel("최소 간격:"), 2, 0)
        grid_tr.addWidget(self.spinMinSpacing, 2, 1)
        grid_tr.addWidget(QtWidgets.QLabel("AOI 내부 포함비율 최소:"), 2, 2)
        grid_tr.addWidget(self.spinInsidePct, 2, 3)
        grid_tr.addWidget(QtWidgets.QLabel("방향 모드:"), 3, 0)
        grid_tr.addWidget(self.cmbOrientation, 3, 1, 1, 3)
        layout.addWidget(grp_tr)

        grp_ctx = QtWidgets.QGroupBox("3. 맥락/회피 설정")
        form_ctx = QtWidgets.QFormLayout(grp_ctx)
        self.cmbRefSites = QgsMapLayerComboBox(grp_ctx)
        try:
            vec_filter = QgsMapLayerProxyModel.Filter.VectorLayer
        except Exception:
            vec_filter = QgsMapLayerProxyModel.VectorLayer
        self.cmbRefSites.setFilters(vec_filter)
        form_ctx.addRow("주변 유적 레이어(선택):", self.cmbRefSites)

        self.spinRefRadius = QtWidgets.QDoubleSpinBox()
        self.spinRefRadius.setRange(50.0, 10_000.0)
        self.spinRefRadius.setDecimals(0)
        self.spinRefRadius.setValue(1000.0)
        self.spinRefRadius.setSuffix(" m")
        form_ctx.addRow("유적 근접 점수 반경:", self.spinRefRadius)

        self.chkAvoidGrave = QtWidgets.QCheckBox("무덤/분묘 회피 적용")
        self.chkAvoidGrave.setChecked(True)
        form_ctx.addRow("", self.chkAvoidGrave)

        self.cmbTopo = QgsMapLayerComboBox(grp_ctx)
        self.cmbTopo.setFilters(vec_filter)
        self.cmbTopo.layerChanged.connect(self._on_topo_layer_changed)
        form_ctx.addRow("수치지형도 벡터(선택):", self.cmbTopo)

        self.cmbTopoCodeField = QtWidgets.QComboBox()
        form_ctx.addRow("코드 필드:", self.cmbTopoCodeField)

        self.spinGraveBuffer = QtWidgets.QDoubleSpinBox()
        self.spinGraveBuffer.setRange(0.0, 100.0)
        self.spinGraveBuffer.setDecimals(1)
        self.spinGraveBuffer.setValue(3.0)
        self.spinGraveBuffer.setSuffix(" m")
        form_ctx.addRow("무덤 회피 버퍼:", self.spinGraveBuffer)

        self.lblLegendStatus = QtWidgets.QLabel("hidden XLS: 미확인")
        self.lblLegendStatus.setStyleSheet("color:#455a64;")
        form_ctx.addRow("범례 코드 상태:", self.lblLegendStatus)
        layout.addWidget(grp_ctx)

        grp_score = QtWidgets.QGroupBox("4. 점수 가중치")
        grid_sc = QtWidgets.QGridLayout(grp_score)
        self.spinWAhp = QtWidgets.QDoubleSpinBox()
        self.spinWAhp.setRange(0.0, 5.0)
        self.spinWAhp.setDecimals(2)
        self.spinWAhp.setValue(0.55)
        self.spinWRef = QtWidgets.QDoubleSpinBox()
        self.spinWRef.setRange(0.0, 5.0)
        self.spinWRef.setDecimals(2)
        self.spinWRef.setValue(0.25)
        self.spinWSlope = QtWidgets.QDoubleSpinBox()
        self.spinWSlope.setRange(0.0, 5.0)
        self.spinWSlope.setDecimals(2)
        self.spinWSlope.setValue(0.20)
        self.spinSlopeMax = QtWidgets.QDoubleSpinBox()
        self.spinSlopeMax.setRange(1.0, 90.0)
        self.spinSlopeMax.setDecimals(1)
        self.spinSlopeMax.setValue(30.0)
        self.spinSlopeMax.setSuffix(" deg")
        self.spinMaxEval = QtWidgets.QSpinBox()
        self.spinMaxEval.setRange(100, 50_000)
        self.spinMaxEval.setValue(8000)
        grid_sc.addWidget(QtWidgets.QLabel("AHP 가중치:"), 0, 0)
        grid_sc.addWidget(self.spinWAhp, 0, 1)
        grid_sc.addWidget(QtWidgets.QLabel("유적근접 가중치:"), 0, 2)
        grid_sc.addWidget(self.spinWRef, 0, 3)
        grid_sc.addWidget(QtWidgets.QLabel("경사 가중치:"), 1, 0)
        grid_sc.addWidget(self.spinWSlope, 1, 1)
        grid_sc.addWidget(QtWidgets.QLabel("최대 허용 경사:"), 1, 2)
        grid_sc.addWidget(self.spinSlopeMax, 1, 3)
        grid_sc.addWidget(QtWidgets.QLabel("최대 후보 평가 수:"), 2, 0)
        grid_sc.addWidget(self.spinMaxEval, 2, 1)
        layout.addWidget(grp_score)

        self.lblBusy = QtWidgets.QLabel("")
        self.lblBusy.setStyleSheet("color:#455a64;")
        layout.addWidget(self.lblBusy)

        row_btn = QtWidgets.QHBoxLayout()
        row_btn.addStretch(1)
        self.btnHelp = QtWidgets.QPushButton("도움말")
        self.btnRun = QtWidgets.QPushButton("후보 생성")
        self.btnClose = QtWidgets.QPushButton("닫기")
        row_btn.addWidget(self.btnHelp)
        row_btn.addWidget(self.btnRun)
        row_btn.addWidget(self.btnClose)
        layout.addLayout(row_btn)

        self.btnHelp.clicked.connect(self._on_help)
        self.btnRun.clicked.connect(self._run)
        self.btnClose.clicked.connect(self.reject)
        self._on_topo_layer_changed()
        self._update_legend_status()

    def _on_help(self):
        html = (
            "<h3>트렌치 후보 제안 도구</h3>"
            "<p>AOI 내부 후보점에 대해 지형 방향(등고선 직교/평행), AHP 적합도, 주변 유적 근접성을 종합해 트렌치 후보를 제안합니다.</p>"
            "<ul>"
            "<li><b>방향 기본값</b>: 등고선 직교</li>"
            "<li><b>평행 옵션</b>: 선형 유구/가마 등 사례 대응</li>"
            "<li><b>무덤 회피</b>: 수치지형도 속성과 hidden 범례(XLS) 기반 코드·키워드 회피</li>"
            "</ul>"
            "<p><b>주의:</b> 본 결과는 조사 보조용 가설이며, 유구 존재를 보장하지 않습니다. 최종 판단은 현장 조사자의 책임입니다.</p>"
        )
        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            show_help_dialog(parent=self, title="트렌치 후보 제안 도움말", html=html, plugin_dir=plugin_dir)
        except Exception:
            QtWidgets.QMessageBox.information(self, "도움말", "README를 참고하세요.")

    def _set_busy(self, message: str = ""):
        try:
            self.lblBusy.setText(str(message or ""))
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass

    def _on_topo_layer_changed(self):
        self.cmbTopoCodeField.clear()
        self.cmbTopoCodeField.addItem("(자동 탐지)", "")
        lyr = self.cmbTopo.currentLayer()
        if lyr is None or not isinstance(lyr, QgsVectorLayer):
            return
        try:
            for f in lyr.fields():
                n = str(f.name() or "")
                self.cmbTopoCodeField.addItem(n, n)
        except Exception:
            pass

    def _hidden_xls_path(self) -> str:
        plugin_dir = os.path.dirname(os.path.dirname(__file__))
        hidden_dir = os.path.join(plugin_dir, "hidden")
        if not os.path.isdir(hidden_dir):
            return ""
        try:
            names = [x for x in os.listdir(hidden_dir) if str(x).lower().endswith(".xls")]
        except Exception:
            names = []
        if not names:
            return ""
        names.sort()
        return os.path.join(hidden_dir, names[0])

    def _iter_rows_from_xls_pandas(self, xls_path: str) -> Iterable[List[str]]:
        try:
            import pandas as pd  # type: ignore
        except Exception:
            return []
        rows: List[List[str]] = []
        try:
            sheets = pd.read_excel(xls_path, sheet_name=None, dtype=str, engine=None)
        except Exception:
            return []
        for _sheet_name, df in (sheets or {}).items():
            try:
                for _idx, row in df.fillna("").iterrows():
                    vals = [str(v).strip() for v in row.tolist() if str(v).strip()]
                    if vals:
                        rows.append(vals)
            except Exception:
                continue
        return rows

    def _iter_rows_from_xls_qgis(self, xls_path: str) -> Iterable[List[str]]:
        out: List[List[str]] = []
        base = QgsVectorLayer(xls_path, "legend_xls", "ogr")
        if not base.isValid():
            return out

        layers: List[QgsVectorLayer] = []
        try:
            subs = base.dataProvider().subLayers() or []
        except Exception:
            subs = []
        if subs:
            for s in subs:
                name = str(s or "")
                if "!!::!!" in name:
                    try:
                        name = name.split("!!::!!", 1)[1]
                    except Exception:
                        pass
                uri = f"{xls_path}|layername={name}"
                lyr = QgsVectorLayer(uri, f"legend_{name}", "ogr")
                if lyr.isValid():
                    layers.append(lyr)
        else:
            layers.append(base)

        for lyr in layers:
            try:
                f_names = [str(f.name() or "") for f in lyr.fields()]
            except Exception:
                f_names = []
            for ft in lyr.getFeatures():
                vals: List[str] = []
                for fn in f_names:
                    try:
                        v = ft[fn]
                        s = str(v).strip() if v is not None else ""
                    except Exception:
                        s = ""
                    if s:
                        vals.append(s)
                if vals:
                    out.append(vals)
        return out

    def _extract_grave_codes(self, rows: Iterable[Sequence[str]]) -> Set[str]:
        codes: Set[str] = set()
        for row in rows:
            vals = [str(v).strip() for v in row if str(v).strip()]
            if not vals:
                continue
            row_txt = " ".join(vals)
            row_low = row_txt.lower()
            has_ko_kw = any((kw in row_txt) for kw in _GRAVE_KEYWORDS if re.search(r"[가-힣]", kw))
            has_en_kw = any((kw in row_low) for kw in _GRAVE_KEYWORDS if (not re.search(r"[가-힣]", kw)))
            if not (has_ko_kw or has_en_kw):
                continue
            for v in vals:
                for m in _CODE_RE.findall(str(v).upper()):
                    codes.add(str(m).upper())
        return codes

    def _load_grave_codes_from_hidden(self) -> Set[str]:
        if self._grave_codes_cache is not None:
            return set(self._grave_codes_cache)
        xls_path = self._hidden_xls_path()
        if not xls_path:
            self._grave_codes_cache = set()
            return set()

        codes: Set[str] = set()
        rows = self._iter_rows_from_xls_pandas(xls_path)
        if rows:
            codes = self._extract_grave_codes(rows)
        if not codes:
            rows2 = self._iter_rows_from_xls_qgis(xls_path)
            if rows2:
                codes = self._extract_grave_codes(rows2)

        self._grave_codes_cache = set(codes)
        return set(codes)

    def _update_legend_status(self):
        path = self._hidden_xls_path()
        if not path:
            self.lblLegendStatus.setText("hidden XLS 없음 (키워드 회피만 사용)")
            return
        codes = self._load_grave_codes_from_hidden()
        if codes:
            self.lblLegendStatus.setText(f"hidden XLS 로드됨 (무덤 코드 {len(codes)}개)")
        else:
            self.lblLegendStatus.setText("hidden XLS 인식 실패/코드 없음 (키워드 회피만 사용)")

    def _sample_raster_value(self, raster: QgsRasterLayer, pt: QgsPointXY, src_crs) -> Optional[float]:
        if raster is None or not isinstance(raster, QgsRasterLayer) or (not raster.isValid()):
            return None
        p = _transform_point(pt, src_crs, raster.crs())
        if p is None:
            return None
        try:
            res = raster.dataProvider().identify(p, QgsRaster.IdentifyFormatValue)
            if not res.isValid():
                return None
            vals = res.results() or {}
            if not vals:
                return None
            v = vals.get(1)
            if v is None:
                try:
                    v = list(vals.values())[0]
                except Exception:
                    v = None
            return _safe_float(v, default=None)
        except Exception:
            return None

    def _pick_code_field(self, lyr: QgsVectorLayer) -> str:
        sel = str(self.cmbTopoCodeField.currentData() or "").strip()
        if sel:
            try:
                if lyr.fields().indexFromName(sel) >= 0:
                    return sel
            except Exception:
                pass
        cands = (
            "code",
            "CODE",
            "dxf_code",
            "DXF_CODE",
            "표준코드",
            "지형지물코드",
            "class_id",
            "Class_id",
            "Layer",
            "layer",
            "element",
        )
        try:
            names = [str(f.name() or "") for f in lyr.fields()]
        except Exception:
            names = []
        lower_map = {n.lower(): n for n in names}
        for c in cands:
            if c.lower() in lower_map:
                return lower_map[c.lower()]
        return ""

    def _feature_has_grave_hint(self, ft: QgsFeature, *, code_field: str, grave_codes: Set[str]) -> bool:
        code = ""
        if code_field:
            try:
                v = ft[code_field]
                code = str(v).strip().upper() if v is not None else ""
            except Exception:
                code = ""
        if code and (code in grave_codes):
            return True
        txts: List[str] = []
        try:
            for v in ft.attributes():
                if v is None:
                    continue
                s = str(v).strip()
                if s:
                    txts.append(s)
        except Exception:
            pass
        if not txts:
            return False
        joined = " ".join(txts)
        joined_low = joined.lower()
        for kw in _GRAVE_KEYWORDS:
            if re.search(r"[가-힣]", kw):
                if kw in joined:
                    return True
            else:
                if kw in joined_low:
                    return True
        return False

    def _build_reference_index(self, layer: Optional[QgsVectorLayer], *, aoi_geom: QgsGeometry, aoi_crs):
        idx = QgsSpatialIndex()
        geom_by_id: Dict[int, QgsGeometry] = {}
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return idx, geom_by_id
        req = QgsFeatureRequest()
        try:
            g_on_layer = _transform_geom(aoi_geom, aoi_crs, layer.crs())
            if g_on_layer is not None and (not g_on_layer.isEmpty()):
                req.setFilterRect(g_on_layer.boundingBox())
        except Exception:
            pass
        for ft in layer.getFeatures(req):
            try:
                g0 = ft.geometry()
            except Exception:
                continue
            if g0 is None or g0.isEmpty():
                continue
            g = _transform_geom(g0, layer.crs(), aoi_crs)
            if g is None or g.isEmpty():
                continue
            f2 = QgsFeature()
            f2.setId(int(ft.id()))
            f2.setGeometry(g)
            try:
                idx.addFeature(f2)
                geom_by_id[int(ft.id())] = g
            except Exception:
                continue
        return idx, geom_by_id

    def _nearest_reference_distance(self, idx: QgsSpatialIndex, geom_by_id: Dict[int, QgsGeometry], pt: QgsPointXY) -> Optional[float]:
        if not geom_by_id:
            return None
        ptg = QgsGeometry.fromPointXY(pt)
        try:
            ids = idx.nearestNeighbor(pt, 8)
        except Exception:
            ids = list(geom_by_id.keys())[:8]
        dmin = None
        for fid in ids:
            g = geom_by_id.get(int(fid))
            if g is None:
                continue
            try:
                d = float(g.distance(ptg))
            except Exception:
                continue
            if (dmin is None) or (d < dmin):
                dmin = d
        return dmin

    def _build_grave_avoid_union(
        self,
        *,
        topo_layer: Optional[QgsVectorLayer],
        aoi_geom: QgsGeometry,
        aoi_crs,
        grave_buffer_m: float,
        use_avoid: bool,
    ) -> Optional[QgsGeometry]:
        if (not use_avoid) or topo_layer is None or (not isinstance(topo_layer, QgsVectorLayer)):
            return None
        grave_codes = self._load_grave_codes_from_hidden()
        code_field = self._pick_code_field(topo_layer)
        req = QgsFeatureRequest()
        try:
            g_on_topo = _transform_geom(aoi_geom, aoi_crs, topo_layer.crs())
            if g_on_topo is not None and (not g_on_topo.isEmpty()):
                bb = g_on_topo.boundingBox()
                bb.grow(max(5.0, float(grave_buffer_m) + 3.0))
                req.setFilterRect(bb)
        except Exception:
            pass

        geoms: List[QgsGeometry] = []
        for ft in topo_layer.getFeatures(req):
            if not self._feature_has_grave_hint(ft, code_field=code_field, grave_codes=grave_codes):
                continue
            try:
                g0 = ft.geometry()
            except Exception:
                continue
            if g0 is None or g0.isEmpty():
                continue
            g = _transform_geom(g0, topo_layer.crs(), aoi_crs)
            if g is None or g.isEmpty():
                continue
            try:
                if float(grave_buffer_m) > 0:
                    g = g.buffer(float(grave_buffer_m), 8)
            except Exception:
                pass
            geoms.append(g)

        if not geoms:
            return None
        try:
            return geoms[0] if len(geoms) == 1 else QgsGeometry.unaryUnion(geoms)
        except Exception:
            try:
                out = geoms[0]
                for g in geoms[1:]:
                    out = out.combine(g)
                return out
            except Exception:
                return None

    def _aoi_extent_in_raster_crs(self, aoi_geom: QgsGeometry, *, aoi_crs, raster: QgsRasterLayer):
        if raster is None or not isinstance(raster, QgsRasterLayer):
            return None
        try:
            g = _transform_geom(aoi_geom, aoi_crs, raster.crs())
            if g is None or g.isEmpty():
                return None
            return g.boundingBox()
        except Exception:
            return None

    def _run(self):
        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)
        self._set_busy("입력 검증 중…")

        aoi_layer = self.cmbAoi.currentLayer()
        if aoi_layer is None or not isinstance(aoi_layer, QgsVectorLayer):
            push_message(self.iface, "오류", "AOI 폴리곤 레이어를 선택하세요.", level=2, duration=7)
            restore_ui_focus(self)
            return
        if aoi_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            push_message(self.iface, "오류", "AOI는 폴리곤이어야 합니다.", level=2, duration=7)
            restore_ui_focus(self)
            return
        if not is_metric_crs(aoi_layer.crs()):
            push_message(self.iface, "오류", "AOI CRS는 미터 단위 투영 좌표계를 사용하세요.", level=2, duration=8)
            restore_ui_focus(self)
            return

        aoi_geom, aoi_n = _unary_union_geom(aoi_layer, selected_only=bool(self.chkAoiSelectedOnly.isChecked()))
        if aoi_geom is None or aoi_geom.isEmpty():
            push_message(self.iface, "오류", "AOI 지오메트리를 만들 수 없습니다.", level=2, duration=7)
            return

        dem_layer = self.cmbDem.currentLayer()
        if dem_layer is None or not isinstance(dem_layer, QgsRasterLayer):
            push_message(self.iface, "오류", "DEM 래스터를 선택하세요.", level=2, duration=7)
            return

        ahp_layer = self.cmbAhp.currentLayer()
        if ahp_layer is not None and (not isinstance(ahp_layer, QgsRasterLayer)):
            ahp_layer = None

        ref_layer = self.cmbRefSites.currentLayer()
        if ref_layer is not None and (not isinstance(ref_layer, QgsVectorLayer)):
            ref_layer = None
        topo_layer = self.cmbTopo.currentLayer()
        if topo_layer is not None and (not isinstance(topo_layer, QgsVectorLayer)):
            topo_layer = None

        trench_width = float(self.spinWidth.value())
        trench_length = float(self.spinLength.value())
        want_n = int(self.spinCount.value())
        grid_step = float(self.spinGrid.value())
        min_spacing = float(self.spinMinSpacing.value())
        inside_min_ratio = max(0.1, min(1.0, float(self.spinInsidePct.value()) / 100.0))
        orient_mode = str(self.cmbOrientation.currentData() or "orthogonal")
        use_avoid = bool(self.chkAvoidGrave.isChecked())
        grave_buffer_m = float(self.spinGraveBuffer.value())
        ref_radius_m = float(self.spinRefRadius.value())
        slope_max = float(self.spinSlopeMax.value())
        max_eval = int(self.spinMaxEval.value())

        w_ahp = float(self.spinWAhp.value())
        w_ref = float(self.spinWRef.value())
        w_slope = float(self.spinWSlope.value())
        w_sum = w_ahp + w_ref + w_slope
        if w_sum <= 0:
            push_message(self.iface, "오류", "가중치 합이 0보다 커야 합니다.", level=2, duration=7)
            return

        run_id = new_run_id("trench_suggestion")
        tmp_aspect = os.path.join(tempfile.gettempdir(), f"archtoolkit_trench_aspect_{run_id}.tif")
        tmp_slope = os.path.join(tempfile.gettempdir(), f"archtoolkit_trench_slope_{run_id}.tif")
        temp_files = [tmp_aspect, tmp_slope]

        try:
            self._set_busy("DEM 파생 레이어(경사/사면방향) 계산 중…")
            processing.run(
                "gdal:aspect",
                {
                    "INPUT": dem_layer.source(),
                    "BAND": 1,
                    "TRIG_ANGLE": False,
                    "ZERO_FLAT": True,
                    "COMPUTE_EDGES": True,
                    "ZEVENBERGEN": False,
                    "OUTPUT": tmp_aspect,
                },
            )
            processing.run(
                "gdal:slope",
                {
                    "INPUT": dem_layer.source(),
                    "BAND": 1,
                    "SCALE": 1,
                    "AS_PERCENT": False,
                    "COMPUTE_EDGES": True,
                    "ZEVENBERGEN": False,
                    "OUTPUT": tmp_slope,
                },
            )
            aspect_layer = QgsRasterLayer(tmp_aspect, f"tmp_aspect_{run_id}")
            slope_layer = QgsRasterLayer(tmp_slope, f"tmp_slope_{run_id}")
            if (not aspect_layer.isValid()) or (not slope_layer.isValid()):
                raise Exception("aspect/slope raster build failed")

            self._set_busy("주변 유적 인덱스/무덤 회피 마스크 준비 중…")
            ref_idx, ref_geoms = self._build_reference_index(ref_layer, aoi_geom=aoi_geom, aoi_crs=aoi_layer.crs())
            grave_union = self._build_grave_avoid_union(
                topo_layer=topo_layer,
                aoi_geom=aoi_geom,
                aoi_crs=aoi_layer.crs(),
                grave_buffer_m=grave_buffer_m,
                use_avoid=use_avoid,
            )

            ahp_min = None
            ahp_max = None
            if ahp_layer is not None and isinstance(ahp_layer, QgsRasterLayer) and ahp_layer.isValid():
                try:
                    ext = self._aoi_extent_in_raster_crs(aoi_geom, aoi_crs=aoi_layer.crs(), raster=ahp_layer)
                    stats = ahp_layer.dataProvider().bandStatistics(
                        1,
                        QgsRasterBandStats.Min | QgsRasterBandStats.Max,
                        ext if ext is not None else ahp_layer.extent(),
                        0,
                    )
                    ahp_min = _safe_float(getattr(stats, "minimumValue", None), default=None)
                    ahp_max = _safe_float(getattr(stats, "maximumValue", None), default=None)
                    if ahp_min is not None and ahp_max is not None and (ahp_max - ahp_min) <= 1e-12:
                        ahp_min = None
                        ahp_max = None
                except Exception:
                    ahp_min = None
                    ahp_max = None

            da = QgsDistanceArea()
            try:
                da.setSourceCrs(aoi_layer.crs(), QgsProject.instance().transformContext())
            except Exception:
                pass

            bbox = aoi_geom.boundingBox()
            x0 = float(bbox.xMinimum()) + grid_step * 0.5
            y0 = float(bbox.yMinimum()) + grid_step * 0.5
            xmax = float(bbox.xMaximum())
            ymax = float(bbox.yMaximum())

            candidates = []
            scanned = 0
            kept = 0

            x = x0
            while x <= xmax and scanned < max_eval:
                y = y0
                while y <= ymax and scanned < max_eval:
                    scanned += 1
                    pt = QgsPointXY(x, y)
                    pt_geom = QgsGeometry.fromPointXY(pt)
                    if not aoi_geom.contains(pt_geom):
                        y += grid_step
                        continue

                    aspect = self._sample_raster_value(aspect_layer, pt, aoi_layer.crs())
                    slope = self._sample_raster_value(slope_layer, pt, aoi_layer.crs())
                    if aspect is None or slope is None:
                        y += grid_step
                        continue
                    # Guard against nodata/invalid ranges from derived rasters.
                    if (aspect < 0.0) or (aspect > 360.0) or (slope < 0.0) or (slope > 90.0):
                        y += grid_step
                        continue
                    if slope > slope_max:
                        y += grid_step
                        continue

                    contour_bearing = (aspect + 90.0) % 180.0
                    bearing = contour_bearing if orient_mode == "parallel" else (aspect % 180.0)
                    trench_geom = _rect_geom_from_center(
                        pt, length_m=trench_length, width_m=trench_width, bearing_deg=bearing
                    )
                    if trench_geom is None or trench_geom.isEmpty():
                        y += grid_step
                        continue

                    try:
                        g_in = trench_geom.intersection(aoi_geom)
                        a_in = max(0.0, float(g_in.area())) if g_in is not None and (not g_in.isEmpty()) else 0.0
                        a_all = max(1e-9, float(trench_geom.area()))
                        inside_ratio = a_in / a_all
                    except Exception:
                        inside_ratio = 0.0
                    if inside_ratio < inside_min_ratio:
                        y += grid_step
                        continue

                    if grave_union is not None:
                        try:
                            if trench_geom.intersects(grave_union):
                                y += grid_step
                                continue
                        except Exception:
                            pass

                    ahp_val = self._sample_raster_value(ahp_layer, pt, aoi_layer.crs()) if ahp_layer else None
                    if ahp_val is not None and ahp_min is not None and ahp_max is not None and ahp_max > ahp_min:
                        ahp_score = max(0.0, min(1.0, (ahp_val - ahp_min) / (ahp_max - ahp_min)))
                    elif ahp_val is not None:
                        ahp_score = max(0.0, min(1.0, ahp_val))
                    else:
                        ahp_score = 0.5

                    ref_dist = self._nearest_reference_distance(ref_idx, ref_geoms, pt) if ref_geoms else None
                    ref_score = 0.5 if ref_dist is None else max(
                        0.0, min(1.0, 1.0 - (float(ref_dist) / max(1.0, ref_radius_m)))
                    )

                    slope_score = max(0.0, min(1.0, 1.0 - (float(slope) / max(1.0, slope_max))))
                    total = (w_ahp * ahp_score + w_ref * ref_score + w_slope * slope_score) / w_sum
                    candidates.append(
                        {
                            "point": pt,
                            "geom": trench_geom,
                            "bearing_deg": float(bearing),
                            "mode": orient_mode,
                            "inside_ratio": float(inside_ratio),
                            "slope_deg": float(slope),
                            "ahp_val": ahp_val,
                            "ahp_score": float(ahp_score),
                            "ref_dist_m": ref_dist,
                            "ref_score": float(ref_score),
                            "score": float(total),
                        }
                    )
                    kept += 1
                    y += grid_step
                x += grid_step

            if not candidates:
                push_message(
                    self.iface,
                    "정보",
                    "조건을 만족하는 트렌치 후보가 없습니다. 간격/경사/내부비율 조건을 완화해보세요.",
                    level=1,
                    duration=8,
                )
                return

            self._set_busy("상위 후보 선별 중…")
            candidates.sort(key=lambda d: float(d.get("score", 0.0)), reverse=True)
            selected = []
            selected_centers: List[QgsPointXY] = []
            selected_geoms: List[QgsGeometry] = []

            for cand in candidates:
                if len(selected) >= want_n:
                    break
                pt = cand.get("point")
                g = cand.get("geom")
                if pt is None or g is None:
                    continue

                conflict = False
                if min_spacing > 0:
                    for spt in selected_centers:
                        try:
                            d = float(da.measureLine(pt, spt))
                        except Exception:
                            d = float(QgsGeometry.fromPointXY(pt).distance(QgsGeometry.fromPointXY(spt)))
                        if d < min_spacing:
                            conflict = True
                            break
                if conflict:
                    continue
                for sg in selected_geoms:
                    try:
                        if g.intersects(sg):
                            conflict = True
                            break
                    except Exception:
                        continue
                if conflict:
                    continue

                selected.append(cand)
                selected_centers.append(pt)
                selected_geoms.append(g)

            if not selected:
                push_message(self.iface, "정보", "중복/간격 필터 후 남은 후보가 없습니다.", level=1, duration=6)
                return

            self._set_busy("결과 레이어 생성 중…")
            crs_authid = aoi_layer.crs().authid()
            trench_layer = QgsVectorLayer(f"Polygon?crs={crs_authid}", f"Trench_Suggestions_{run_id}", "memory")
            center_layer = QgsVectorLayer(f"Point?crs={crs_authid}", f"Trench_Centers_{run_id}", "memory")

            t_pr = trench_layer.dataProvider()
            c_pr = center_layer.dataProvider()
            fields = [
                QgsField("rank", QVariant.Int),
                QgsField("score", QVariant.Double),
                QgsField("mode", QVariant.String),
                QgsField("bearing_deg", QVariant.Double),
                QgsField("inside_pct", QVariant.Double),
                QgsField("slope_deg", QVariant.Double),
                QgsField("ahp_val", QVariant.Double),
                QgsField("ahp_score", QVariant.Double),
                QgsField("ref_dist_m", QVariant.Double),
                QgsField("ref_score", QVariant.Double),
            ]
            t_pr.addAttributes(fields)
            c_pr.addAttributes(fields)
            trench_layer.updateFields()
            center_layer.updateFields()

            trench_feats: List[QgsFeature] = []
            center_feats: List[QgsFeature] = []
            for i, d in enumerate(selected, start=1):
                vals = [
                    int(i),
                    float(d.get("score") or 0.0),
                    str(d.get("mode") or ""),
                    float(d.get("bearing_deg") or 0.0),
                    float(d.get("inside_ratio") or 0.0) * 100.0,
                    float(d.get("slope_deg") or 0.0),
                    _safe_float(d.get("ahp_val"), default=None),
                    float(d.get("ahp_score") or 0.0),
                    _safe_float(d.get("ref_dist_m"), default=None),
                    float(d.get("ref_score") or 0.0),
                ]

                f_poly = QgsFeature(trench_layer.fields())
                f_poly.setGeometry(d.get("geom"))
                for idx0, v in enumerate(vals):
                    f_poly[idx0] = v
                trench_feats.append(f_poly)

                f_pt = QgsFeature(center_layer.fields())
                f_pt.setGeometry(QgsGeometry.fromPointXY(d.get("point")))
                for idx0, v in enumerate(vals):
                    f_pt[idx0] = v
                center_feats.append(f_pt)

            t_pr.addFeatures(trench_feats)
            c_pr.addFeatures(center_feats)
            trench_layer.updateExtents()
            center_layer.updateExtents()
            try:
                t_sym = QgsFillSymbol.createSimple(
                    {"color": "255,99,71,40", "outline_color": "200,30,0,220", "outline_width": "0.6"}
                )
                trench_layer.setRenderer(QgsSingleSymbolRenderer(t_sym))
            except Exception:
                pass
            try:
                c_sym = QgsMarkerSymbol.createSimple({"name": "circle", "size": "2.0", "color": "40,120,220,220"})
                center_layer.setRenderer(QgsSingleSymbolRenderer(c_sym))
            except Exception:
                pass

            set_archtoolkit_layer_metadata(
                trench_layer,
                tool_id="trench_suggestion",
                run_id=run_id,
                kind="trench_polygon",
                units="m",
                params={
                    "width_m": trench_width,
                    "length_m": trench_length,
                    "count_requested": want_n,
                    "count_selected": len(selected),
                    "grid_step_m": grid_step,
                    "min_spacing_m": min_spacing,
                    "inside_min_ratio": inside_min_ratio,
                    "orientation_mode": orient_mode,
                    "grave_avoid": bool(use_avoid),
                    "grave_buffer_m": grave_buffer_m,
                    "ref_radius_m": ref_radius_m,
                    "max_slope_deg": slope_max,
                    "weights": {"ahp": w_ahp, "ref": w_ref, "slope": w_slope},
                    "aoi_features": int(aoi_n),
                    "candidates_scanned": int(scanned),
                    "candidates_kept": int(kept),
                    "hidden_xls_loaded": bool(len(self._load_grave_codes_from_hidden()) > 0),
                },
            )
            set_archtoolkit_layer_metadata(
                center_layer,
                tool_id="trench_suggestion",
                run_id=run_id,
                kind="trench_center",
                units="m",
                params={"paired_polygon_layer": trench_layer.name()},
            )

            root = QgsProject.instance().layerTreeRoot()
            parent_name = "ArchToolkit - Trench Suggestion"
            grp = root.findGroup(parent_name)
            if grp is None:
                grp = root.insertGroup(0, parent_name)
            run_grp = grp.insertGroup(0, f"TrenchSuggestion_{run_id}")
            QgsProject.instance().addMapLayer(trench_layer, False)
            QgsProject.instance().addMapLayer(center_layer, False)
            run_grp.insertLayer(0, trench_layer)
            run_grp.insertLayer(1, center_layer)

            push_message(
                self.iface,
                "완료",
                (
                    f"트렌치 후보 {len(selected)}개를 생성했습니다. "
                    "결과는 조사 보조용 제안이며, 최종 판단은 현장 조사자가 수행해야 합니다."
                ),
                level=0,
                duration=8,
            )
            log_message(
                f"TrenchSuggestion done: selected={len(selected)} scanned={scanned} kept={kept} run_id={run_id}",
                level=Qgis.Info,
            )
            self.accept()
        except Exception as e:
            log_exception("Trench suggestion error", e)
            push_message(self.iface, "오류", f"트렌치 후보 생성 실패: {e}", level=2, duration=10)
            restore_ui_focus(self)
        finally:
            cleanup_files(temp_files)
            self._set_busy("")
