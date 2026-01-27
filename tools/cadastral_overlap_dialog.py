# -*- coding: utf-8 -*-

"""
지적도 중첩 면적표 (Cadastral Overlap Table)

입력:
- 지적도(필지) 폴리곤 레이어
- 조사지역(면/경계) 폴리곤 레이어

출력:
- 조사지역과 겹치는 지적도 폴리곤(클립) 레이어(메모리)
  - 원본 지적 속성 + 면적 필드 추가
    - parcel_m2: 필지 전체면적(㎡)
    - in_aoi_m2: 조사지역 내 포함면적(㎡)
    - in_aoi_pct: 포함비율(%)

의도:
한국 고고학 조사 실무에서 “조사지역 내 어떤 필지가 얼마나 포함되는지”를 빠르게 표로 만들기 위한 도구.
"""

import math
import os
import uuid
from typing import Iterable, List, Optional, Tuple

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsProject,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox

from .live_log_dialog import ensure_live_log_dialog
from .utils import log_message, push_message, restore_ui_focus


def _safe_make_valid(geom: QgsGeometry) -> QgsGeometry:
    try:
        if geom is None or geom.isEmpty():
            return geom
        if geom.isGeosValid():
            return geom
    except Exception:
        pass
    try:
        mv = geom.makeValid()
        if mv and (not mv.isEmpty()):
            return mv
    except Exception:
        pass
    return geom


def _iter_layer_geoms(layer: QgsVectorLayer, *, selected_only: bool) -> List[QgsGeometry]:
    geoms: List[QgsGeometry] = []
    feats: Iterable[QgsFeature]
    if selected_only and layer.selectedFeatureCount() > 0:
        feats = layer.selectedFeatures()
    else:
        feats = layer.getFeatures()
    for f in feats:
        try:
            g = f.geometry()
            if g and (not g.isEmpty()):
                geoms.append(_safe_make_valid(g))
        except Exception:
            continue
    return geoms


def _unary_union(geoms: List[QgsGeometry]) -> Optional[QgsGeometry]:
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    try:
        return QgsGeometry.unaryUnion(geoms)
    except Exception:
        try:
            # Fallback: iterative combine
            out = geoms[0]
            for g in geoms[1:]:
                out = out.combine(g)
            return out
        except Exception:
            return None


class CadastralOverlapDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("지적도 중첩 면적표 (Cadastral Overlap) - ArchToolkit")
        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            icon_path = os.path.join(plugin_dir, "icon.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except Exception:
            pass

        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel(
            "<b>지적도 중첩 면적표</b><br>"
            "조사지역 폴리곤이 지적도(필지) 폴리곤을 어느 면적만큼 포함하는지 계산하여<br>"
            "클립(모자이크) 레이어 + 속성테이블(면적/비율)을 생성합니다."
        )
        header.setWordWrap(True)
        header.setStyleSheet("background:#e8f5e9; padding:10px; border:1px solid #c8e6c9; border-radius:4px;")
        layout.addWidget(header)

        grp = QtWidgets.QGroupBox("1. 입력 레이어")
        form = QtWidgets.QFormLayout(grp)

        self.cmbCadastral = QgsMapLayerComboBox(grp)
        self.cmbCadastral.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        self.cmbSurvey = QgsMapLayerComboBox(grp)
        self.cmbSurvey.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)

        self.chkCadastralSelected = QtWidgets.QCheckBox("지적도: 선택 피처만 사용")
        self.chkSurveySelected = QtWidgets.QCheckBox("조사지역: 선택 피처만 사용")
        self.chkSurveySelected.setChecked(True)

        form.addRow("지적도(필지) 레이어", self.cmbCadastral)
        form.addRow("", self.chkCadastralSelected)
        form.addRow("조사지역 레이어", self.cmbSurvey)
        form.addRow("", self.chkSurveySelected)
        layout.addWidget(grp)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        self.btnRun = QtWidgets.QPushButton("실행")
        self.btnClose = QtWidgets.QPushButton("닫기")
        btn_row.addWidget(self.btnRun)
        btn_row.addWidget(self.btnClose)
        layout.addLayout(btn_row)

        self.btnRun.clicked.connect(self.run)
        self.btnClose.clicked.connect(self.reject)

        # Tooltips (compact UI, detailed info on hover)
        self.cmbCadastral.setToolTip("조사 범위와 겹치는 지적도(필지) 폴리곤 레이어를 선택하세요.")
        self.cmbSurvey.setToolTip("조사 범위를 나타내는 폴리곤 레이어를 선택하세요. 여러 피처면 합집합으로 처리합니다.")
        self.chkCadastralSelected.setToolTip("체크하면 지적도 레이어에서 선택한 피처만 대상으로 계산합니다.")
        self.chkSurveySelected.setToolTip("체크하면 조사지역 레이어에서 선택한 피처만 합집합(AOI)으로 사용합니다.")

    def _validate_layer(self, layer, *, name: str) -> Optional[QgsVectorLayer]:
        if layer is None or (not layer.isValid()):
            push_message(self.iface, "오류", f"{name} 레이어를 선택해주세요.", level=2)
            restore_ui_focus(self)
            return None
        if layer.type() != layer.VectorLayer:
            push_message(self.iface, "오류", f"{name}는 벡터 레이어여야 합니다.", level=2)
            restore_ui_focus(self)
            return None
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            push_message(self.iface, "오류", f"{name}는 폴리곤 레이어여야 합니다.", level=2)
            restore_ui_focus(self)
            return None
        return layer

    def _distance_area(self, crs) -> QgsDistanceArea:
        da = QgsDistanceArea()
        try:
            da.setSourceCrs(crs, QgsProject.instance().transformContext())
        except Exception:
            pass
        try:
            ell = QgsProject.instance().ellipsoid() or "WGS84"
            if str(ell).strip():
                da.setEllipsoid(str(ell))
        except Exception:
            pass
        return da

    def _area_m2(self, da: QgsDistanceArea, geom: QgsGeometry) -> float:
        if geom is None or geom.isEmpty():
            return 0.0
        try:
            a = float(da.measureArea(geom))
            return float(da.convertAreaMeasurement(a, QgsUnitTypes.AreaSquareMeters))
        except Exception:
            try:
                return float(geom.area())
            except Exception:
                return 0.0

    def run(self):
        cad = self._validate_layer(self.cmbCadastral.currentLayer(), name="지적도")
        if cad is None:
            return
        survey = self._validate_layer(self.cmbSurvey.currentLayer(), name="조사지역")
        if survey is None:
            return

        # Live log window
        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)

        cad_sel = bool(self.chkCadastralSelected.isChecked())
        survey_sel = bool(self.chkSurveySelected.isChecked())

        # Build AOI geometry (unary union) in survey CRS
        survey_geoms = _iter_layer_geoms(survey, selected_only=survey_sel)
        aoi = _unary_union(survey_geoms)
        if aoi is None or aoi.isEmpty():
            push_message(self.iface, "오류", "조사지역(폴리곤)에서 유효한 지오메트리를 찾지 못했습니다.", level=2)
            restore_ui_focus(self)
            return
        aoi = _safe_make_valid(aoi)

        # Transform AOI into cadastral CRS if needed
        cad_crs = cad.crs()
        if survey.crs() != cad_crs:
            try:
                ct = QgsCoordinateTransform(survey.crs(), cad_crs, QgsProject.instance())
                aoi_t = QgsGeometry(aoi)
                aoi_t.transform(ct)
                aoi = aoi_t
            except Exception as e:
                log_message(f"CadastralOverlap: failed CRS transform AOI -> cad CRS: {e}", level=Qgis.Warning)

        aoi = _safe_make_valid(aoi)
        aoi_bbox = aoi.boundingBox()

        da = self._distance_area(cad_crs)
        aoi_area_m2 = self._area_m2(da, aoi)

        run_id = uuid.uuid4().hex[:6]
        layer_name = f"지적중첩_{run_id}"

        # Output fields: cadastral + computed
        fields = list(cad.fields())
        fields.append(QgsField("parcel_m2", QVariant.Double))
        fields.append(QgsField("in_aoi_m2", QVariant.Double))
        fields.append(QgsField("in_aoi_pct", QVariant.Double))

        out = QgsVectorLayer(f"Polygon?crs={cad_crs.authid()}", layer_name, "memory")
        pr = out.dataProvider()
        pr.addAttributes(fields)
        out.updateFields()

        # Collect candidate cadastral features (bbox filter)
        feats: Iterable[QgsFeature]
        if cad_sel and cad.selectedFeatureCount() > 0:
            feats = cad.selectedFeatures()
        else:
            req = QgsFeatureRequest()
            try:
                req.setFilterRect(aoi_bbox)
            except Exception:
                pass
            feats = cad.getFeatures(req)

        feats = list(feats)
        total = len(feats)

        log_message(
            f"CadastralOverlap: start (cad={cad.name()}, survey={survey.name()}, total={total}, aoi_m2={aoi_area_m2:.2f})",
            level=Qgis.Info,
        )

        progress = QtWidgets.QProgressDialog("지적도 중첩 면적 계산 중...", "취소", 0, max(1, total), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents()

        out_feats: List[QgsFeature] = []
        sum_in = 0.0
        kept = 0

        for i, f in enumerate(feats):
            if progress.wasCanceled():
                push_message(self.iface, "취소", "중첩 계산이 취소되었습니다.", level=1, duration=4)
                restore_ui_focus(self)
                return
            if i % 50 == 0:
                progress.setValue(min(total, i))
                QtWidgets.QApplication.processEvents()

            try:
                g = f.geometry()
            except Exception:
                continue
            if not g or g.isEmpty():
                continue
            g = _safe_make_valid(g)
            try:
                if not g.boundingBox().intersects(aoi_bbox):
                    continue
            except Exception:
                pass

            try:
                inter = g.intersection(aoi)
            except Exception:
                inter = None
            if inter is None or inter.isEmpty():
                continue
            inter = _safe_make_valid(inter)

            in_m2 = self._area_m2(da, inter)
            if not math.isfinite(float(in_m2)) or float(in_m2) <= 0.0:
                continue

            parcel_m2 = self._area_m2(da, g)
            if parcel_m2 > 0.0 and math.isfinite(float(parcel_m2)):
                pct = float(in_m2) / float(parcel_m2) * 100.0
            else:
                pct = 0.0

            feat_out = QgsFeature(out.fields())
            try:
                # Copy original attributes
                attrs = list(f.attributes())
                attrs.append(float(parcel_m2))
                attrs.append(float(in_m2))
                attrs.append(float(pct))
                feat_out.setAttributes(attrs)
            except Exception:
                pass

            feat_out.setGeometry(inter)
            out_feats.append(feat_out)
            sum_in += float(in_m2)
            kept += 1

        progress.setValue(total)
        QtWidgets.QApplication.processEvents()

        if not out_feats:
            push_message(self.iface, "결과 없음", "조사지역과 겹치는 지적도 피처를 찾지 못했습니다.", level=1, duration=5)
            restore_ui_focus(self)
            return

        pr.addFeatures(out_feats)
        out.updateExtents()

        # Add to project under a dedicated group
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        parent_name = "ArchToolkit - Cadastral"
        parent_group = root.findGroup(parent_name)
        if parent_group is None:
            parent_group = root.insertGroup(0, parent_name)
        run_group = parent_group.insertGroup(0, f"{layer_name}")
        run_group.setExpanded(False)

        project.addMapLayer(out, False)
        run_group.addLayer(out)

        try:
            # Keep group near top
            if parent_group.parent() == root:
                idx = root.children().index(parent_group)
                if idx != 0:
                    root.removeChildNode(parent_group)
                    root.insertChildNode(0, parent_group)
        except Exception:
            pass

        msg = f"완료: {kept}개 필지, 포함면적 합 {sum_in:,.2f} ㎡"
        if aoi_area_m2 > 0.0:
            msg += f"  (AOI {aoi_area_m2:,.2f} ㎡ 대비 {sum_in / aoi_area_m2 * 100.0:.1f}%)"
        push_message(self.iface, "지적도 중첩 면적표", msg, level=0, duration=7)
        log_message(f"CadastralOverlap: done ({msg})", level=Qgis.Info)
        self.accept()
