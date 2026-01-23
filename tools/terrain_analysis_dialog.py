# -*- coding: utf-8 -*-

# ArchToolkit - Archaeology Toolkit for QGIS
# Copyright (C) 2026 balguljang2
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
Terrain Analysis Dialog for ArchToolkit
Slope, Aspect, TRI, TPI, Roughness, Slope Position with archaeological classifications
User-configurable parameters for TPI radius, TPI thresholds, and Slope Position
"""
import os
import math
import tempfile
import uuid
from osgeo import gdal
from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsSingleSymbolRenderer,
    QgsColorRampShader,
    QgsSymbolLayer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsProperty,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
import processing
from .utils import restore_ui_focus, push_message, cleanup_files

# This tool uses only QGIS built-in libraries and GDAL processing algorithms.
# No external plugins or libraries (like numpy, matplotlib) are required.

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'terrain_analysis_dialog_base.ui'))


class TerrainAnalysisDialog(QtWidgets.QDialog, FORM_CLASS):
    
    # Slope classifications
    SLOPE_CLASSIFICATIONS = {
        'korean': {
            'name': '한국표준',
            'classes': [
                {'max': 15, 'label': '완경사지 | 0~15° | 주거지 최적', 'color': '#1a5f1a'},
                {'max': 20, 'label': '경사지 | 15~20° | 계단식 경작', 'color': '#7ec87e'},
                {'max': 25, 'label': '급경사지 | 20~25° | 산지 산림', 'color': '#ffff00'},
                {'max': 30, 'label': '험준지 | 25~30° | 접근 곤란', 'color': '#ffa500'},
                {'max': 90, 'label': '절험지 | 30°+ | 절벽/암벽', 'color': '#ff0000'},
            ]
        },
        'tobler': {
            'name': 'Tobler 1993',
            'classes': [
                {'max': 6, 'label': '1등급 | 0~6° | 일반 보행', 'color': '#1a5f1a'},
                {'max': 12, 'label': '2등급 | 6~12° | 속도 감소', 'color': '#7ec87e'},
                {'max': 18, 'label': '3등급 | 12~18° | 이동 지체', 'color': '#ffff00'},
                {'max': 25, 'label': '4등급 | 18~25° | 한계', 'color': '#ffa500'},
                {'max': 90, 'label': '5등급 | 25°+ | 불가', 'color': '#ff0000'},
            ]
        },
        'minetti': {
            'name': 'Minetti 2002',
            'classes': [
                {'max': 3, 'label': '1등급 | 0~3° | 일상', 'color': '#20b2aa'},
                {'max': 9, 'label': '2등급 | 3~9° | 노동', 'color': '#ffff00'},
                {'max': 15, 'label': '3등급 | 9~15° | 고강도', 'color': '#ffa500'},
                {'max': 25, 'label': '4등급 | 15~25° | 임계', 'color': '#ff0000'},
                {'max': 90, 'label': '5등급 | 25°+ | 금지', 'color': '#800080'},
            ]
        },
        'llobera': {
            'name': 'Llobera 2007',
            'classes': [
                {'max': 2, 'label': '1등급 | 0~2° | 평탄', 'color': '#d3d3d3'},
                {'max': 6, 'label': '2등급 | 2~6° | 인지', 'color': '#add8e6'},
                {'max': 12, 'label': '3등급 | 6~12° | 언덕', 'color': '#00ffff'},
                {'max': 20, 'label': '4등급 | 12~20° | 장벽', 'color': '#800080'},
                {'max': 90, 'label': '5등급 | 20°+ | 수직', 'color': '#000000'},
            ]
        }
    }
    
    # Aspect 8-direction with flat area
    ASPECT_CLASSES = [
        {'max': 0, 'label': '평탄 | 0° | 평지/수면', 'color': '#808080'},
        {'max': 45, 'label': 'N-NE | 0~45° | 북~북동', 'color': '#ff0000'},
        {'max': 90, 'label': 'NE-E | 45~90° | 북동~동', 'color': '#ff7f00'},
        {'max': 135, 'label': 'E-SE | 90~135° | 동~남동', 'color': '#ffff00'},
        {'max': 180, 'label': 'SE-S | 135~180° | 남동~남', 'color': '#7fff00'},
        {'max': 225, 'label': 'S-SW | 180~225° | 남~남서', 'color': '#00ffff'},
        {'max': 270, 'label': 'SW-W | 225~270° | 남서~서', 'color': '#007fff'},
        {'max': 315, 'label': 'W-NW | 270~315° | 서~북서', 'color': '#0000ff'},
        {'max': 360, 'label': 'NW-N | 315~360° | 북서~북', 'color': '#7f00ff'},
    ]
    
    # TRI - Riley et al. (1999) - Blue to Red (default values)
    TRI_CLASSES = [
        {'max': 2, 'label': 'I | 0~2 | 평탄', 'color': '#2166ac'},
        {'max': 5, 'label': 'II | 2~5 | 거의평탄', 'color': '#67a9cf'},
        {'max': 10, 'label': 'III | 5~10 | 약간거침', 'color': '#f7f7f7'},
        {'max': 20, 'label': 'IV | 10~20 | 중간', 'color': '#ef8a62'},
        {'max': 500, 'label': 'V | 20+ | 험준', 'color': '#b2182b'},
    ]
    
    # Weiss (2001) 6-class Slope Position Classification
    SLOPE_POSITION_CLASSES = [
        {'max': 1, 'label': '1 | 깊은 곡저 (Incised Valley)', 'color': '#08306b'},
        {'max': 2, 'label': '2 | 곡저/하상 (Valley Floor)', 'color': '#2171b5'},
        {'max': 3, 'label': '3 | 평지/단구 (Flat or Terrace)', 'color': '#f7f7f7'},
        {'max': 4, 'label': '4 | 중간 사면 (Mid Slope)', 'color': '#fee391'},
        {'max': 5, 'label': '5 | 능선 평탄부 (Upland Flat)', 'color': '#ec7014'},
        {'max': 6, 'label': '6 | 급경사 능선 (Steep Ridge)', 'color': '#8c2d04'},
    ]
    
    # Roughness - Wilson (2000) - Greens to Purple
    ROUGHNESS_CLASSES = [
        {'max': 1, 'label': '평탄 | 0~1m', 'color': '#d9f0d3'},
        {'max': 3, 'label': '미세거침 | 1~3m', 'color': '#a6dba0'},
        {'max': 6, 'label': '중간거침 | 3~6m', 'color': '#5aae61'},
        {'max': 15, 'label': '험준 | 6~15m', 'color': '#c2a5cf'},
        {'max': 500, 'label': '극도험준 | 15m+', 'color': '#762a83'},
    ]
    
    def __init__(self, iface, parent=None):
        super(TerrainAnalysisDialog, self).__init__(parent)
        self.setupUi(self)
        self.iface = iface
        
        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.btnRun.clicked.connect(self.run_analysis)
        self.btnClose.clicked.connect(self.reject)
        
        # Advanced settings toggle - EXPANDED by default (user request)
        self.widgetAdvanced.setVisible(True)
        self.btnAdvanced.setText("⚙ 고급 설정 ▲")
        self.btnAdvanced.clicked.connect(self.toggle_advanced)
        
        # Auto-SD checkbox connection and initial state
        if hasattr(self, 'chkAutoSD'):
            self.chkAutoSD.stateChanged.connect(self.on_auto_sd_changed)
            # Apply initial state - disable inputs if auto-SD is checked
            self._apply_auto_sd_state(self.chkAutoSD.isChecked())
    
    def on_auto_sd_changed(self, state):
        """Enable/disable manual TPI threshold inputs based on auto-SD checkbox"""
        # Use isChecked() for reliable check - stateChanged sends int (0, 1, or 2)
        auto_mode = self.chkAutoSD.isChecked()
        self._apply_auto_sd_state(auto_mode)
    
    def _apply_auto_sd_state(self, auto_mode):
        """Apply the auto-SD state to disable/enable relevant spinboxes"""
        self.spinTPILow.setEnabled(not auto_mode)
        self.spinTPIHigh.setEnabled(not auto_mode)
        self.spinTPIThreshold.setEnabled(not auto_mode)
    
    def toggle_advanced(self):
        """Toggle visibility of advanced settings"""
        is_visible = self.widgetAdvanced.isVisible()
        self.widgetAdvanced.setVisible(not is_visible)
        if is_visible:
            self.btnAdvanced.setText("⚙ 고급 설정 ▼")
        else:
            self.btnAdvanced.setText("⚙ 고급 설정 ▲")
    
    def get_selected_classification(self):
        if self.radioKorean.isChecked():
            return 'korean'
        elif self.radioTobler.isChecked():
            return 'tobler'
        elif self.radioMinetti.isChecked():
            return 'minetti'
        else:
            return 'llobera'
    
    def get_tpi_classes(self, threshold):
        """Generate TPI classification classes based on user threshold"""
        return [
            {'max': -threshold, 'label': f'골짜기 | <-{threshold:.2f}', 'color': '#2166ac'},
            {'max': threshold, 'label': f'평지 | -{threshold:.2f}~+{threshold:.2f}', 'color': '#f7f7f7'},
            {'max': 500, 'label': f'능선 | >+{threshold:.2f}', 'color': '#8b4513'},
        ]
    
    def get_tri_classes(self, max_rugged):
        """Generate TRI classification classes based on user-defined max ruggedness threshold
        
        Parameters:
        - max_rugged: The threshold above which terrain is classified as 'rugged' (V)
          Lower values = more sensitive to subtle terrain variations
          Higher values = only extreme ruggedness is classified as 'rugged'
        """
        # Proportionally distribute the 5 classes based on max_rugged
        t1 = max_rugged * 0.1   # ~10% = flat
        t2 = max_rugged * 0.25  # ~25% = nearly flat  
        t3 = max_rugged * 0.5   # ~50% = slightly rugged
        t4 = max_rugged         # 100% = moderately rugged
        return [
            {'max': t1, 'label': f'I | 0~{t1:.0f} | 평탄', 'color': '#2166ac'},
            {'max': t2, 'label': f'II | {t1:.0f}~{t2:.0f} | 거의평탄', 'color': '#67a9cf'},
            {'max': t3, 'label': f'III | {t2:.0f}~{t3:.0f} | 약간거침', 'color': '#f7f7f7'},
            {'max': t4, 'label': f'IV | {t3:.0f}~{t4:.0f} | 중간', 'color': '#ef8a62'},
            {'max': 500, 'label': f'V | {t4:.0f}+ | 험준', 'color': '#b2182b'},
        ]
    
    def apply_style(self, layer, classes, max_val):
        """Apply discrete color classification"""
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)
        
        items = []
        for cls in classes:
            item = QgsColorRampShader.ColorRampItem(
                cls['max'], QColor(cls['color']), cls['label']
            )
            items.append(item)
        
        color_ramp.setColorRampItemList(items)
        
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(color_ramp)
        
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setClassificationMin(0)
        renderer.setClassificationMax(max_val)
        
        layer.setRenderer(renderer)
        layer.triggerRepaint()
    
    def run_analysis(self):
        dem_layer = self.cmbDemLayer.currentLayer()
        if not dem_layer:
            push_message(self.iface, "오류", "DEM 래스터를 선택해주세요", level=2)
            restore_ui_focus(self)
            return
        
        # Drafting outputs (vectorized slope labels / aspect arrows)
        draft_slope = bool(getattr(self, "chkDraftSlopeLabel", None) and self.chkDraftSlopeLabel.isChecked())
        draft_aspect = bool(getattr(self, "chkDraftAspectArrow", None) and self.chkDraftAspectArrow.isChecked())

        has_any = any([self.chkSlope.isChecked(), self.chkAspect.isChecked(),
                       self.chkTRI.isChecked(), self.chkTPI.isChecked(), 
                       self.chkRoughness.isChecked(), self.chkSlopePosition.isChecked(),
                       draft_slope, draft_aspect])
        if not has_any:
            push_message(self.iface, "오류", "분석 유형을 선택해주세요", level=2)
            restore_ui_focus(self)
            return
        
        push_message(self.iface, "처리 중", "지형 분석 실행 중...", level=0)
        self.hide()
        QtWidgets.QApplication.processEvents()
        
        success = False
        try:
            dem_source = dem_layer.source()
            results = []
            run_id = uuid.uuid4().hex[:8]
            
            # Get user parameters
            tpi_radius = self.spinTPIRadius.value()
            tpi_threshold = self.spinTPIThreshold.value()
            slope_threshold = self.spinSlopeThreshold.value()
            tpi_low = self.spinTPILow.value()
            tpi_high = self.spinTPIHigh.value()
            tri_max = self.spinTRIMax.value()

            # Drafting params (safe defaults if UI isn't present)
            draft_step = int(getattr(self, "spinDraftStep", None).value()) if hasattr(self, "spinDraftStep") else 5
            arrow_size_mm = float(getattr(self, "spinDraftArrowSize", None).value()) if hasattr(self, "spinDraftArrowSize") else 1.2
            label_size_pt = float(getattr(self, "spinDraftLabelSize", None).value()) if hasattr(self, "spinDraftLabelSize") else 7.0
            draft_step = max(1, int(draft_step))
            arrow_size_mm = max(0.1, float(arrow_size_mm))
            label_size_pt = max(4.0, float(label_size_pt))
            
            slope_path = None
            aspect_path = None

            # Slope (also used by drafting)
            if self.chkSlope.isChecked() or draft_slope or draft_aspect:
                slope_path = os.path.join(tempfile.gettempdir(), f'archtoolkit_slope_{run_id}.tif')
                processing.run("gdal:slope", {
                    'INPUT': dem_source, 'BAND': 1, 'SCALE': 1, 'AS_PERCENT': False, 'OUTPUT': slope_path
                })
                if self.chkSlope.isChecked():
                    cls_key = self.get_selected_classification()
                    cls_info = self.SLOPE_CLASSIFICATIONS[cls_key]
                    layer = QgsRasterLayer(slope_path, f"경사도_{cls_info['name']}")
                    if layer.isValid():
                        QgsProject.instance().addMapLayer(layer)
                        self.apply_style(layer, cls_info['classes'], 90)
                        results.append("경사도")
            
            # Aspect (also used by drafting)
            if self.chkAspect.isChecked() or draft_aspect:
                aspect_path = os.path.join(tempfile.gettempdir(), f'archtoolkit_aspect_{run_id}.tif')
                processing.run("gdal:aspect", {
                    'INPUT': dem_source, 'BAND': 1, 'TRIG_ANGLE': False, 'ZERO_FLAT': True, 'OUTPUT': aspect_path
                })
                if self.chkAspect.isChecked():
                    layer = QgsRasterLayer(aspect_path, "사면방향_8방위")
                    if layer.isValid():
                        QgsProject.instance().addMapLayer(layer)
                        self.apply_style(layer, self.ASPECT_CLASSES, 360)
                        results.append("사면방향")
            
            # TRI with user-defined classification threshold
            if self.chkTRI.isChecked():
                output = os.path.join(tempfile.gettempdir(), f'archtoolkit_tri_{run_id}.tif')
                processing.run("gdal:triterrainruggednessindex", {
                    'INPUT': dem_source, 'BAND': 1, 'OUTPUT': output
                })
                tri_classes = self.get_tri_classes(tri_max)
                layer_name = f"TRI Riley 1999 (험준기준:{tri_max})"
                layer = QgsRasterLayer(output, layer_name)
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    self.apply_style(layer, tri_classes, tri_max * 2.5)
                    results.append("TRI")
            
            # TPI with user parameters (radius and threshold)
            if self.chkTPI.isChecked():
                self.run_tpi_analysis(dem_layer, dem_source, tpi_radius, tpi_threshold, results, run_id)
            
            # Roughness
            if self.chkRoughness.isChecked():
                output = os.path.join(tempfile.gettempdir(), f'archtoolkit_roughness_{run_id}.tif')
                processing.run("gdal:roughness", {
                    'INPUT': dem_source, 'BAND': 1, 'OUTPUT': output
                })
                layer = QgsRasterLayer(output, "Roughness Wilson 2000")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    self.apply_style(layer, self.ROUGHNESS_CLASSES, 20)
                    results.append("Roughness")
            
            # Slope Position - Weiss (2001) 6-class with user thresholds
            if self.chkSlopePosition.isChecked():
                self.run_slope_position_analysis(dem_source, slope_threshold, tpi_low, tpi_high, results, run_id)

            # Drafting vector layer (slope labels + aspect arrows)
            if draft_slope or draft_aspect:
                created = self.create_slope_aspect_drafting_layer(
                    slope_path=slope_path,
                    aspect_path=aspect_path,
                    dem_layer=dem_layer,
                    step_cells=draft_step,
                    arrow_size_mm=arrow_size_mm,
                    label_size_pt=label_size_pt,
                    draw_slope_labels=draft_slope,
                    draw_aspect_arrows=draft_aspect,
                )
                if created:
                    results.append("도면화(경사/경사향)")
            
            if results:
                push_message(self.iface, "완료", f"분석 완료: {', '.join(results)}", level=0)
                success = True
                self.accept()
            else:
                push_message(self.iface, "오류", "분석 결과가 없습니다.", level=2)
                restore_ui_focus(self)
                
        except Exception as e:
            push_message(self.iface, "오류", f"처리 중 오류: {str(e)}", level=2)
            restore_ui_focus(self)
        finally:
            if not success:
                restore_ui_focus(self)

    def create_slope_aspect_drafting_layer(
        self,
        slope_path: str,
        aspect_path: str,
        dem_layer: QgsRasterLayer,
        step_cells: int,
        arrow_size_mm: float,
        label_size_pt: float,
        draw_slope_labels: bool,
        draw_aspect_arrows: bool,
    ) -> bool:
        """Create a point vector layer for drafting: slope labels (1°) and/or aspect arrows.

        - Each sampled raster cell becomes a point at the cell center.
        - slope is rounded to integer degrees for label readability.
        - aspect arrows are hidden on (near-)flat slope cells.
        """
        if not slope_path or not os.path.exists(slope_path):
            push_message(self.iface, "오류", "도면화용 경사도 래스터를 찾을 수 없습니다.", level=2)
            return False

        if draw_aspect_arrows and (not aspect_path or not os.path.exists(aspect_path)):
            push_message(self.iface, "오류", "도면화용 사면방향 래스터를 찾을 수 없습니다.", level=2)
            return False

        step_cells = max(1, int(step_cells))

        try:
            ds_slope = gdal.Open(slope_path, gdal.GA_ReadOnly)
            if ds_slope is None:
                push_message(self.iface, "오류", "경사도 래스터를 열 수 없습니다.", level=2)
                return False

            band_slope = ds_slope.GetRasterBand(1)
            xsize = int(ds_slope.RasterXSize)
            ysize = int(ds_slope.RasterYSize)

            ds_aspect = None
            band_aspect = None
            if draw_aspect_arrows:
                ds_aspect = gdal.Open(aspect_path, gdal.GA_ReadOnly)
                if ds_aspect is None:
                    push_message(self.iface, "오류", "사면방향 래스터를 열 수 없습니다.", level=2)
                    return False
                band_aspect = ds_aspect.GetRasterBand(1)

            # Safety limit: avoid generating millions of points accidentally.
            nx = (xsize + step_cells - 1) // step_cells
            ny = (ysize + step_cells - 1) // step_cells
            point_count = int(nx * ny)
            max_points = 300_000
            if point_count > max_points:
                push_message(
                    self.iface,
                    "도면화(경사/경사향)",
                    f"생성될 점이 너무 많습니다: 약 {point_count:,}개 (최대 {max_points:,}개). "
                    "표시 간격(셀)을 늘려주세요.",
                    level=1,
                    duration=8,
                )
                return False

            gt = ds_slope.GetGeoTransform()
            if gt is None:
                push_message(self.iface, "오류", "래스터 지오트랜스폼 정보를 읽을 수 없습니다.", level=2)
                return False

            name = "경사도/경사향 도면화 (Slope & Aspect Drafting)"
            layer = QgsVectorLayer("Point", name, "memory")
            try:
                layer.setCrs(dem_layer.crs())
            except Exception:
                pass

            pr = layer.dataProvider()
            pr.addAttributes(
                [
                    QgsField("slope_deg", QVariant.Int),
                    QgsField("slope", QVariant.Double),
                    QgsField("aspect_deg", QVariant.Int),
                    QgsField("aspect", QVariant.Double),
                    QgsField("draw_arrow", QVariant.Int),
                ]
            )
            layer.updateFields()

            flat_thresh_deg = 0.5
            feats = []

            # Read in row blocks for reasonable performance without huge memory spikes.
            chunk_rows = 256
            for row0 in range(0, ysize, chunk_rows):
                rows_to_read = min(chunk_rows, ysize - row0)
                slope_arr = band_slope.ReadAsArray(0, row0, xsize, rows_to_read)
                aspect_arr = None
                if band_aspect is not None:
                    aspect_arr = band_aspect.ReadAsArray(0, row0, xsize, rows_to_read)

                if slope_arr is None:
                    continue

                for dr in range(0, rows_to_read, step_cells):
                    row = row0 + dr
                    for col in range(0, xsize, step_cells):
                        sval = slope_arr[dr, col]
                        try:
                            slope = float(sval)
                        except Exception:
                            continue
                        if not math.isfinite(slope):
                            continue

                        slope_deg = int(round(slope))

                        aspect = None
                        aspect_deg = None
                        draw_arrow = 0
                        if aspect_arr is not None:
                            aval = aspect_arr[dr, col]
                            try:
                                aspect = float(aval)
                            except Exception:
                                aspect = None
                            if aspect is not None and math.isfinite(aspect):
                                # Hide arrows on (near-)flat areas; keep true North slopes (aspect≈0) visible.
                                if slope > flat_thresh_deg:
                                    draw_arrow = 1
                                    aspect_deg = int(round(aspect)) % 360
                                else:
                                    draw_arrow = 0
                                    aspect_deg = int(round(aspect)) % 360

                        # Cell center coordinates from geotransform
                        x = gt[0] + (col + 0.5) * gt[1] + (row + 0.5) * gt[2]
                        y = gt[3] + (col + 0.5) * gt[4] + (row + 0.5) * gt[5]

                        f = QgsFeature(layer.fields())
                        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
                        f["slope_deg"] = slope_deg
                        f["slope"] = slope
                        if aspect is not None:
                            f["aspect"] = aspect
                        if aspect_deg is not None:
                            f["aspect_deg"] = aspect_deg
                        f["draw_arrow"] = int(draw_arrow)
                        feats.append(f)

                    # batch flush
                    if len(feats) >= 5000:
                        pr.addFeatures(feats)
                        feats = []

            if feats:
                pr.addFeatures(feats)

            layer.updateExtents()

            # Style: arrow symbol rotated by aspect + slope label with buffer.
            symbol = QgsMarkerSymbol.createSimple(
                {
                    "name": "triangle",
                    "color": "120,0,180,200",
                    "outline_color": "0,0,0,220",
                    "outline_width": "0.1",
                    "size": str(float(arrow_size_mm)),
                }
            )
            try:
                sl = symbol.symbolLayer(0)
                if sl is not None and draw_aspect_arrows:
                    sl.setDataDefinedProperty(QgsSymbolLayer.PropertyAngle, QgsProperty.fromField("aspect_deg"))
                    sl.setDataDefinedProperty(
                        QgsSymbolLayer.PropertySize,
                        QgsProperty.fromExpression(
                            f"CASE WHEN \"draw_arrow\"=1 THEN {float(arrow_size_mm)} ELSE 0 END"
                        ),
                    )
                elif sl is not None:
                    sl.setDataDefinedProperty(QgsSymbolLayer.PropertySize, QgsProperty.fromExpression("0"))
            except Exception:
                pass

            layer.setRenderer(QgsSingleSymbolRenderer(symbol))

            if draw_slope_labels:
                settings = QgsPalLayerSettings()
                settings.fieldName = "slope_deg"
                settings.placement = QgsPalLayerSettings.OverPoint

                fmt = QgsTextFormat()
                fmt.setSize(float(label_size_pt))
                fmt.setColor(QColor(0, 0, 0))

                buf = QgsTextBufferSettings()
                buf.setEnabled(True)
                buf.setColor(QColor(255, 255, 255))
                buf.setSize(0.8)
                fmt.setBuffer(buf)

                settings.setFormat(fmt)

                layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
                layer.setLabelsEnabled(True)
            else:
                layer.setLabelsEnabled(False)

            layer.triggerRepaint()

            # Add on top for visibility
            QgsProject.instance().addMapLayer(layer, False)
            try:
                QgsProject.instance().layerTreeRoot().insertLayer(0, layer)
            except Exception:
                QgsProject.instance().addMapLayer(layer)

            return True

        except Exception as e:
            push_message(self.iface, "오류", f"도면화 생성 실패: {str(e)}", level=2, duration=8)
            return False
    
    def run_tpi_analysis(self, dem_layer, dem_source, radius, threshold, results, run_id):
        """Run TPI analysis with user-specified radius and classification threshold
        
        TPI = Elevation - Mean of Neighborhood
        
        Uses GDAL only - for radius > 1, uses resampling trick to approximate larger windows.
        
        Parameters:
        - radius: Number of cells for neighborhood window (larger = broader terrain features)
        - threshold: Classification boundary for valley/flat/ridge (smaller = more sensitive)
        """
        downsampled = None
        mean_approx = None
        try:
            output = os.path.join(tempfile.gettempdir(), f'archtoolkit_tpi_{run_id}.tif')
            
            # Calculate window size (must be odd number: 3, 5, 7, ...)
            window_size = radius * 2 + 1 if radius > 1 else 3
            
            if radius <= 1:
                # Use standard GDAL TPI for radius=1 (3x3 window)
                processing.run("gdal:tpitopographicpositionindex", {
                    'INPUT': dem_source, 'BAND': 1, 'OUTPUT': output
                })
            else:
                # Pure GDAL approach for custom radius:
                
                # Get original resolution
                pixel_size_x = dem_layer.rasterUnitsPerPixelX()
                pixel_size_y = dem_layer.rasterUnitsPerPixelY()
                new_res = max(pixel_size_x, pixel_size_y) * radius
                
                # Step 1: Downsample (average resampling = approximate focal mean)
                downsampled = os.path.join(tempfile.gettempdir(), f'archtoolkit_tpi_down_{run_id}.tif')
                processing.run("gdal:warpreproject", {
                    'INPUT': dem_source,
                    'SOURCE_CRS': None,
                    'TARGET_CRS': None,
                    'RESAMPLING': 5,  # Average
                    'NODATA': None,
                    'TARGET_RESOLUTION': new_res,
                    'OPTIONS': '',
                    'DATA_TYPE': 0,
                    'TARGET_EXTENT': None,
                    'TARGET_EXTENT_CRS': None,
                    'MULTITHREADING': False,
                    'EXTRA': '',
                    'OUTPUT': downsampled
                })
                
                # Step 2: Resample back to original resolution (neighborhood mean approximation)
                mean_approx = os.path.join(tempfile.gettempdir(), f'archtoolkit_tpi_mean_{run_id}.tif')
                extent = dem_layer.extent()
                extent_str = f"{extent.xMinimum()},{extent.xMaximum()},{extent.yMinimum()},{extent.yMaximum()}"
                
                processing.run("gdal:warpreproject", {
                    'INPUT': downsampled,
                    'SOURCE_CRS': None,
                    'TARGET_CRS': None,
                    'RESAMPLING': 1,  # Bilinear
                    'NODATA': None,
                    'TARGET_RESOLUTION': pixel_size_x,
                    'OPTIONS': '',
                    'DATA_TYPE': 0,
                    'TARGET_EXTENT': extent_str,
                    'TARGET_EXTENT_CRS': dem_layer.crs().authid(),
                    'MULTITHREADING': False,
                    'EXTRA': '',
                    'OUTPUT': mean_approx
                })
                
                # Step 3: Calculate TPI = DEM - Mean
                if os.path.exists(mean_approx):
                    processing.run("gdal:rastercalculator", {
                        'INPUT_A': dem_source, 'BAND_A': 1,
                        'INPUT_B': mean_approx, 'BAND_B': 1,
                        'FORMULA': 'A - B',
                        'OUTPUT': output,
                        'RTYPE': 5  # Float32
                    })
                else:
                    # Fallback to standard GDAL TPI
                    processing.run("gdal:tpitopographicpositionindex", {
                        'INPUT': dem_source, 'BAND': 1, 'OUTPUT': output
                    })
                    window_size = 3
            
            # Apply classification with user threshold
            tpi_classes = self.get_tpi_classes(threshold)
            layer_name = f"TPI (창:{window_size}x{window_size}, 임계값:±{threshold:.2f})"
            layer = QgsRasterLayer(output, layer_name)
            
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self.apply_style(layer, tpi_classes, 10)
                results.append("TPI")
            
        except Exception as e:
            self.iface.messageBar().pushMessage("경고", f"TPI 분석 오류: {str(e)}", level=1)
        finally:
            cleanup_files([downsampled, mean_approx])
    
    def run_slope_position_analysis(self, dem_source, slope_thresh, tpi_low, tpi_high, results, run_id):
        """Run Weiss (2001) 6-class Landform Classification using GDAL with user thresholds
        
        Parameters:
        - slope_thresh: Degree threshold for flat vs sloped areas (e.g., 5°)
        - tpi_low: TPI threshold for valley classification (e.g., -1.0)
        - tpi_high: TPI threshold for ridge classification (e.g., 1.0)
        
        Classification Logic:
        1. 깊은 곡저 (Incised Valley): TPI < tpi_low
        2. 곡저/하상 (Valley Floor): tpi_low <= TPI < tpi_low/2
        3. 평지/단구 (Flat or Terrace): |TPI| <= |tpi_low/2| and Slope <= slope_thresh
        4. 중간 사면 (Mid Slope): |TPI| <= |tpi_high/2| and Slope > slope_thresh
        5. 능선 평탄부 (Upland Flat): tpi_high/2 < TPI <= tpi_high
        6. 급경사 능선 (Steep Ridge): TPI > tpi_high
        """
        try:
            # 1. Generate TPI
            tpi_path = os.path.join(tempfile.gettempdir(), f'archtoolkit_tpi_temp_{run_id}.tif')
            processing.run("gdal:tpitopographicpositionindex", {
                'INPUT': dem_source, 'BAND': 1, 'OUTPUT': tpi_path
            })
            
            # 2. Generate Slope
            slope_path = os.path.join(tempfile.gettempdir(), f'archtoolkit_slope_temp_{run_id}.tif')
            processing.run("gdal:slope", {
                'INPUT': dem_source, 'BAND': 1, 'SCALE': 1, 'AS_PERCENT': False, 'OUTPUT': slope_path
            })
            
            # 3. Check if files exist
            if not os.path.exists(tpi_path) or not os.path.exists(slope_path):
                self.iface.messageBar().pushMessage("경고", "TPI/Slope 생성 실패", level=1)
                return
            
            # 3.5 AUTO-SD CALCULATION (Weiss 2001 standard approach)
            # Calculate TPI statistics to use 1 SD as threshold
            use_auto_sd = hasattr(self, 'chkAutoSD') and self.chkAutoSD.isChecked()
            if use_auto_sd:
                tpi_layer = QgsRasterLayer(tpi_path, "TPI_temp")
                if tpi_layer.isValid():
                    provider = tpi_layer.dataProvider()
                    stats = provider.bandStatistics(1)
                    tpi_sd = stats.stdDev
                    tpi_mean = stats.mean
                    # Weiss (2001): use 1 SD as threshold
                    tpi_low = -tpi_sd
                    tpi_high = tpi_sd
                    self.iface.messageBar().pushMessage(
                        "자동 SD", 
                        f"TPI 통계: 평균={tpi_mean:.2f}, 표준편차={tpi_sd:.2f} → 임계값 ±{tpi_sd:.2f} 적용",
                        level=0
                    )
            
            # 4. Use gdal_calc.py for classification with thresholds
            output_path = os.path.join(tempfile.gettempdir(), f'archtoolkit_landform_{run_id}.tif')
            
            # Calculate intermediate thresholds (Weiss 2001: 0.5 SD boundaries)
            tpi_mid_low = tpi_low / 2   # -0.5 SD
            tpi_mid_high = tpi_high / 2  # +0.5 SD
            
            # Classification: 1=Valley, 2=Lower, 3=Flat, 4=Mid, 5=Upper, 6=Ridge
            # Using user-defined thresholds
            calc_expr = (
                f"(A<{tpi_low})*1 + "
                f"((A>={tpi_low})*(A<{tpi_mid_low}))*2 + "
                f"((A>={tpi_mid_low})*(A<={tpi_mid_high})*(B<={slope_thresh}))*3 + "
                f"((A>={tpi_mid_low})*(A<={tpi_mid_high})*(B>{slope_thresh}))*4 + "
                f"((A>{tpi_mid_high})*(A<={tpi_high}))*5 + "
                f"(A>{tpi_high})*6"
            )
            
            result = processing.run("gdal:rastercalculator", {
                'INPUT_A': tpi_path, 'BAND_A': 1,
                'INPUT_B': slope_path, 'BAND_B': 1,
                'FORMULA': calc_expr,
                'OUTPUT': output_path,
                'RTYPE': 1  # Int16
            })
            
            if result and os.path.exists(output_path):
                layer_name = f"지형분류 (경사:{slope_thresh}°, TPI:{tpi_low:.1f}~{tpi_high:.1f})"
                layer = QgsRasterLayer(output_path, layer_name)
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    self.apply_style(layer, self.SLOPE_POSITION_CLASSES, 6)
                    results.append("지형분류")
                else:
                    self.iface.messageBar().pushMessage("경고", "지형분류 레이어 생성 실패", level=1)
            else:
                self.iface.messageBar().pushMessage("경고", "지형분류 래스터 생성 실패", level=1)
                
        except Exception as e:
            self.iface.messageBar().pushMessage("경고", f"지형분류 분석 오류: {str(e)}", level=1)
        finally:
            cleanup_files([tpi_path, slope_path])
