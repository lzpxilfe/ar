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
            icon_path = None
            for icon_name in ("jijuk.png", "jijuk.jpg", "jijuk.jpeg", "icon.png"):
                p = os.path.join(plugin_dir, icon_name)
                if os.path.exists(p):
                    icon_path = p
                    break
            if icon_path and os.path.exists(icon_path):
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
        self.chkSplitBySurveyFeature = QtWidgets.QCheckBox("조사지역 피처별로 결과 레이어 분리")
        self.chkSplitBySurveyFeature.setChecked(False)

        form.addRow("지적도(필지) 레이어", self.cmbCadastral)
        form.addRow("", self.chkCadastralSelected)
        form.addRow("조사지역 레이어", self.cmbSurvey)
        form.addRow("", self.chkSurveySelected)
        form.addRow("", self.chkSplitBySurveyFeature)
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
        self.chkSplitBySurveyFeature.setToolTip(
            "조사지역 레이어에 폴리곤 피처가 여러 개라면, 피처(폴리곤)마다 결과 레이어를 따로 생성합니다.\n"
            "해당 폴리곤별로 속성테이블을 분리해서 보고 싶을 때 사용하세요."
        )

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
        try:
            split_by_feature = bool(self.chkSplitBySurveyFeature.isChecked())
        except Exception:
            split_by_feature = False

        cad_crs = cad.crs()
        da = self._distance_area(cad_crs)

        # Transform survey CRS -> cadastral CRS if needed
        ct = None
        if survey.crs() != cad_crs:
            try:
                ct = QgsCoordinateTransform(survey.crs(), cad_crs, QgsProject.instance())
            except Exception as e:
                ct = None
                log_message(f"CadastralOverlap: failed to build CRS transform (survey -> cad): {e}", level=Qgis.Warning)

        # Output fields: cadastral + computed
        base_fields = list(cad.fields())
        base_fields.append(QgsField("parcel_m2", QVariant.Double))
        base_fields.append(QgsField("in_aoi_m2", QVariant.Double))
        base_fields.append(QgsField("in_aoi_pct", QVariant.Double))

        def create_output_layer(name: str) -> QgsVectorLayer:
            out = QgsVectorLayer(f"Polygon?crs={cad_crs.authid()}", name, "memory")
            pr = out.dataProvider()
            pr.addAttributes(base_fields)
            out.updateFields()
            try:
                def _set_alias(field_name: str, alias: str):
                    idx = int(out.fields().indexFromName(field_name))
                    if idx >= 0:
                        out.setFieldAlias(idx, alias)

                _set_alias("parcel_m2", "필지면적(㎡)")
                _set_alias("in_aoi_m2", "조사지역 포함면적(㎡)")
                _set_alias("in_aoi_pct", "포함비율(%)")
            except Exception:
                pass
            return out

        # Prepare output group (lazy-create run group only when at least one layer is added)
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        parent_name = "ArchToolkit - Cadastral"
        parent_group = root.findGroup(parent_name)
        if parent_group is None:
            parent_group = root.insertGroup(0, parent_name)

        try:
            # Keep group near top
            if parent_group.parent() == root:
                idx = root.children().index(parent_group)
                if idx != 0:
                    root.removeChildNode(parent_group)
                    root.insertChildNode(0, parent_group)
        except Exception:
            pass

        run_id = uuid.uuid4().hex[:6]
        run_group_name = f"지적중첩_{run_id}"
        run_group = None

        def ensure_run_group():
            nonlocal run_group
            if run_group is None:
                run_group = parent_group.insertGroup(0, run_group_name)
                run_group.setExpanded(False)
            return run_group

        cad_selected_feats = None
        if cad_sel and cad.selectedFeatureCount() > 0:
            try:
                cad_selected_feats = list(cad.selectedFeatures())
            except Exception:
                cad_selected_feats = None

        def iter_survey_features():
            if survey_sel and survey.selectedFeatureCount() > 0:
                return survey.selectedFeatures()
            return survey.getFeatures()

        if split_by_feature:
            # Build AOI geometries per survey feature
            aoi_items: List[Tuple[int, QgsGeometry]] = []
            for sf in iter_survey_features():
                try:
                    g = sf.geometry()
                except Exception:
                    continue
                if not g or g.isEmpty():
                    continue
                g = _safe_make_valid(g)
                if ct is not None:
                    try:
                        gt = QgsGeometry(g)
                        gt.transform(ct)
                        g = _safe_make_valid(gt)
                    except Exception:
                        pass
                if g and (not g.isEmpty()):
                    try:
                        aoi_items.append((int(sf.id()), g))
                    except Exception:
                        aoi_items.append((0, g))

            if not aoi_items:
                push_message(self.iface, "오류", "조사지역(폴리곤)에서 유효한 지오메트리를 찾지 못했습니다.", level=2)
                restore_ui_focus(self)
                return

            log_message(
                f"CadastralOverlap: start split-by-feature (cad={cad.name()}, survey={survey.name()}, aoi_count={len(aoi_items)})",
                level=Qgis.Info,
            )

            progress = QtWidgets.QProgressDialog(
                "조사지역 피처별 지적도 중첩 면적 계산 중...", "취소", 0, len(aoi_items), self
            )
            progress.setWindowModality(Qt.WindowModal)
            progress.show()
            QtWidgets.QApplication.processEvents()

            created_layers = 0
            empty_aois = 0
            total_in_m2 = 0.0

            for idx, (aoi_fid, aoi_geom) in enumerate(aoi_items):
                if progress.wasCanceled():
                    push_message(self.iface, "취소", "중첩 계산이 취소되었습니다.", level=1, duration=4)
                    restore_ui_focus(self)
                    return

                progress.setValue(idx)
                try:
                    progress.setLabelText(f"조사지역 {idx+1}/{len(aoi_items)} 처리 중... (fid={aoi_fid})")
                except Exception:
                    pass
                QtWidgets.QApplication.processEvents()

                aoi_geom = _safe_make_valid(aoi_geom)
                if aoi_geom is None or aoi_geom.isEmpty():
                    empty_aois += 1
                    continue

                aoi_bbox = aoi_geom.boundingBox()

                # Collect candidate cadastral features (bbox filter)
                feats: List[QgsFeature] = []
                if cad_selected_feats is not None:
                    for cf in cad_selected_feats:
                        try:
                            cg = cf.geometry()
                            if cg and (not cg.isEmpty()) and cg.boundingBox().intersects(aoi_bbox):
                                feats.append(cf)
                        except Exception:
                            continue
                else:
                    req = QgsFeatureRequest()
                    try:
                        req.setFilterRect(aoi_bbox)
                    except Exception:
                        pass
                    try:
                        feats = list(cad.getFeatures(req))
                    except Exception:
                        feats = []

                out = create_output_layer(f"{run_group_name}_AOI{aoi_fid}")
                pr = out.dataProvider()

                out_feats: List[QgsFeature] = []
                sum_in = 0.0
                kept = 0

                for i, f in enumerate(feats):
                    if progress.wasCanceled():
                        push_message(self.iface, "취소", "중첩 계산이 취소되었습니다.", level=1, duration=4)
                        restore_ui_focus(self)
                        return
                    if i % 200 == 0:
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
                        inter = g.intersection(aoi_geom)
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

                if not out_feats:
                    empty_aois += 1
                    log_message(f"CadastralOverlap: AOI fid={aoi_fid} -> overlaps=0", level=Qgis.Info)
                else:
                    pr.addFeatures(out_feats)
                    out.updateExtents()
                    total_in_m2 += float(sum_in)
                    log_message(
                        f"CadastralOverlap: AOI fid={aoi_fid} done (parcels={kept}, in_m2={sum_in:.2f})",
                        level=Qgis.Info,
                    )

                # Add the output layer even when empty so users can open a per-AOI attribute table.
                project.addMapLayer(out, False)
                ensure_run_group().addLayer(out)
                created_layers += 1

            progress.setValue(len(aoi_items))
            QtWidgets.QApplication.processEvents()

            msg = f"완료: {created_layers}개 레이어 생성, 포함면적 합 {total_in_m2:,.2f} ㎡"
            if empty_aois > 0:
                msg += f"  (겹침 없음 {empty_aois}개)"
            push_message(self.iface, "지적도 중첩 면적표", msg, level=0, duration=7)
            log_message(f"CadastralOverlap: done split-by-feature ({msg})", level=Qgis.Info)
            self.accept()
            return

        # Default: unary union AOI (single output layer)
        survey_geoms = _iter_layer_geoms(survey, selected_only=survey_sel)
        aoi = _unary_union(survey_geoms)
        if aoi is None or aoi.isEmpty():
            push_message(self.iface, "오류", "조사지역(폴리곤)에서 유효한 지오메트리를 찾지 못했습니다.", level=2)
            restore_ui_focus(self)
            return
        aoi = _safe_make_valid(aoi)
        if ct is not None:
            try:
                aoi_t = QgsGeometry(aoi)
                aoi_t.transform(ct)
                aoi = _safe_make_valid(aoi_t)
            except Exception as e:
                log_message(f"CadastralOverlap: failed CRS transform AOI -> cad CRS: {e}", level=Qgis.Warning)

        aoi = _safe_make_valid(aoi)
        aoi_bbox = aoi.boundingBox()
        aoi_area_m2 = self._area_m2(da, aoi)

        # Collect candidate cadastral features (bbox filter)
        feats: List[QgsFeature] = []
        if cad_selected_feats is not None:
            for cf in cad_selected_feats:
                try:
                    cg = cf.geometry()
                    if cg and (not cg.isEmpty()) and cg.boundingBox().intersects(aoi_bbox):
                        feats.append(cf)
                except Exception:
                    continue
        else:
            req = QgsFeatureRequest()
            try:
                req.setFilterRect(aoi_bbox)
            except Exception:
                pass
            try:
                feats = list(cad.getFeatures(req))
            except Exception:
                feats = []

        total = len(feats)
        log_message(
            f"CadastralOverlap: start (cad={cad.name()}, survey={survey.name()}, total={total}, aoi_m2={aoi_area_m2:.2f})",
            level=Qgis.Info,
        )

        progress = QtWidgets.QProgressDialog("지적도 중첩 면적 계산 중...", "취소", 0, max(1, total), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents()

        out = create_output_layer(run_group_name)
        pr = out.dataProvider()

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

        project.addMapLayer(out, False)
        ensure_run_group().addLayer(out)

        msg = f"완료: {kept}개 필지, 포함면적 합 {sum_in:,.2f} ㎡"
        if aoi_area_m2 > 0.0:
            msg += f"  (AOI {aoi_area_m2:,.2f} ㎡ 대비 {sum_in / aoi_area_m2 * 100.0:.1f}%)"
        push_message(self.iface, "지적도 중첩 면적표", msg, level=0, duration=7)
        log_message(f"CadastralOverlap: done ({msg})", level=Qgis.Info)
        self.accept()
