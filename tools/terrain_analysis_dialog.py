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
import tempfile
import uuid
from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsProject, QgsRasterLayer, QgsMapLayerProxyModel,
    QgsRasterShader, QgsColorRampShader, QgsSingleBandPseudoColorRenderer
)
from .utils import (
    ProcessingCancelled,
    ProcessingRunner,
    cleanup_files,
    get_archtoolkit_output_dir,
    push_message,
    restore_ui_focus,
    warn_if_temp_output,
)

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
        
        has_any = any([self.chkSlope.isChecked(), self.chkAspect.isChecked(),
                       self.chkTRI.isChecked(), self.chkTPI.isChecked(), 
                       self.chkRoughness.isChecked(), self.chkSlopePosition.isChecked()])
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
            output_dir = get_archtoolkit_output_dir("TerrainAnalysis")
            
            # Get user parameters
            tpi_radius = self.spinTPIRadius.value()
            tpi_threshold = self.spinTPIThreshold.value()
            slope_threshold = self.spinSlopeThreshold.value()
            tpi_low = self.spinTPILow.value()
            tpi_high = self.spinTPIHigh.value()
            tri_max = self.spinTRIMax.value()

            with ProcessingRunner(self.iface, "지형 분석", "처리 중...") as runner:
                # Slope
                if self.chkSlope.isChecked():
                    output = os.path.join(output_dir, f'archtoolkit_slope_{run_id}.tif')
                    runner.run("gdal:slope", {
                        'INPUT': dem_source, 'BAND': 1, 'SCALE': 1, 'AS_PERCENT': False, 'OUTPUT': output
                    }, text="경사도 계산 중...")
                    cls_key = self.get_selected_classification()
                    cls_info = self.SLOPE_CLASSIFICATIONS[cls_key]
                    layer = QgsRasterLayer(output, f"경사도_{cls_info['name']}")
                    if layer.isValid():
                        warn_if_temp_output(self.iface, output, what="지형 분석 결과")
                        QgsProject.instance().addMapLayer(layer)
                        self.apply_style(layer, cls_info['classes'], 90)
                        results.append("경사도")

                # Aspect
                if self.chkAspect.isChecked():
                    output = os.path.join(output_dir, f'archtoolkit_aspect_{run_id}.tif')
                    runner.run("gdal:aspect", {
                        'INPUT': dem_source, 'BAND': 1, 'TRIG_ANGLE': False, 'ZERO_FLAT': True, 'OUTPUT': output
                    }, text="사면방향 계산 중...")
                    layer = QgsRasterLayer(output, "사면방향_8방위")
                    if layer.isValid():
                        warn_if_temp_output(self.iface, output, what="지형 분석 결과")
                        QgsProject.instance().addMapLayer(layer)
                        self.apply_style(layer, self.ASPECT_CLASSES, 360)
                        results.append("사면방향")

                # TRI with user-defined classification threshold
                if self.chkTRI.isChecked():
                    output = os.path.join(output_dir, f'archtoolkit_tri_{run_id}.tif')
                    runner.run("gdal:triterrainruggednessindex", {
                        'INPUT': dem_source, 'BAND': 1, 'OUTPUT': output
                    }, text="TRI 계산 중...")
                    tri_classes = self.get_tri_classes(tri_max)
                    layer_name = f"TRI Riley 1999 (험준기준:{tri_max})"
                    layer = QgsRasterLayer(output, layer_name)
                    if layer.isValid():
                        warn_if_temp_output(self.iface, output, what="지형 분석 결과")
                        QgsProject.instance().addMapLayer(layer)
                        self.apply_style(layer, tri_classes, tri_max * 2.5)
                        results.append("TRI")

                # TPI with user parameters (radius and threshold)
                if self.chkTPI.isChecked():
                    self.run_tpi_analysis(
                        dem_layer,
                        dem_source,
                        tpi_radius,
                        tpi_threshold,
                        results,
                        run_id,
                        runner=runner,
                        output_dir=output_dir,
                    )

                # Roughness
                if self.chkRoughness.isChecked():
                    output = os.path.join(output_dir, f'archtoolkit_roughness_{run_id}.tif')
                    runner.run("gdal:roughness", {
                        'INPUT': dem_source, 'BAND': 1, 'OUTPUT': output
                    }, text="Roughness 계산 중...")
                    layer = QgsRasterLayer(output, "Roughness Wilson 2000")
                    if layer.isValid():
                        warn_if_temp_output(self.iface, output, what="지형 분석 결과")
                        QgsProject.instance().addMapLayer(layer)
                        self.apply_style(layer, self.ROUGHNESS_CLASSES, 20)
                        results.append("Roughness")

                # Slope Position - Weiss (2001) 6-class with user thresholds
                if self.chkSlopePosition.isChecked():
                    self.run_slope_position_analysis(
                        dem_source,
                        slope_threshold,
                        tpi_low,
                        tpi_high,
                        results,
                        run_id,
                        runner=runner,
                        output_dir=output_dir,
                    )
            
            if results:
                push_message(self.iface, "완료", f"분석 완료: {', '.join(results)}", level=0)
                success = True
                self.accept()
            else:
                push_message(self.iface, "오류", "분석 결과가 없습니다.", level=2)
                restore_ui_focus(self)
                
        except ProcessingCancelled:
            push_message(self.iface, "취소", "지형 분석이 취소되었습니다.", level=1)
            restore_ui_focus(self)

        except Exception as e:
            push_message(self.iface, "오류", f"처리 중 오류: {str(e)}", level=2)
            restore_ui_focus(self)
        finally:
            if not success:
                restore_ui_focus(self)
    
    def run_tpi_analysis(self, dem_layer, dem_source, radius, threshold, results, run_id, runner=None, output_dir=None):
        """Run TPI analysis with user-specified radius and classification threshold
        
        TPI = Elevation - Mean of Neighborhood
        
        Uses GDAL only - for radius > 1, uses resampling trick to approximate larger windows.
        
        Parameters:
        - radius: Number of cells for neighborhood window (larger = broader terrain features)
        - threshold: Classification boundary for valley/flat/ridge (smaller = more sensitive)
        """
        downsampled = None
        mean_approx = None
        owns_runner = False
        try:
            if output_dir is None:
                output_dir = get_archtoolkit_output_dir("TerrainAnalysis")
            output = os.path.join(output_dir, f'archtoolkit_tpi_{run_id}.tif')

            if runner is None:
                runner = ProcessingRunner(self.iface, "TPI 분석", "처리 중...")
                owns_runner = True
            
            # Calculate window size (must be odd number: 3, 5, 7, ...)
            window_size = radius * 2 + 1 if radius > 1 else 3
            
            if radius <= 1:
                # Use standard GDAL TPI for radius=1 (3x3 window)
                runner.run("gdal:tpitopographicpositionindex", {
                    'INPUT': dem_source, 'BAND': 1, 'OUTPUT': output
                }, text="TPI 계산 중...")
            else:
                # Pure GDAL approach for custom radius:
                
                # Get original resolution
                pixel_size_x = dem_layer.rasterUnitsPerPixelX()
                pixel_size_y = dem_layer.rasterUnitsPerPixelY()
                new_res = max(pixel_size_x, pixel_size_y) * radius
                
                # Step 1: Downsample (average resampling = approximate focal mean)
                downsampled = os.path.join(tempfile.gettempdir(), f'archtoolkit_tpi_down_{run_id}.tif')
                runner.run("gdal:warpreproject", {
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
                }, text="TPI (다운샘플) 처리 중...")
                
                # Step 2: Resample back to original resolution (neighborhood mean approximation)
                mean_approx = os.path.join(tempfile.gettempdir(), f'archtoolkit_tpi_mean_{run_id}.tif')
                extent = dem_layer.extent()
                extent_str = f"{extent.xMinimum()},{extent.xMaximum()},{extent.yMinimum()},{extent.yMaximum()}"
                
                runner.run("gdal:warpreproject", {
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
                }, text="TPI (재표본화) 처리 중...")
                
                # Step 3: Calculate TPI = DEM - Mean
                if os.path.exists(mean_approx):
                    runner.run("gdal:rastercalculator", {
                        'INPUT_A': dem_source, 'BAND_A': 1,
                        'INPUT_B': mean_approx, 'BAND_B': 1,
                        'FORMULA': 'A - B',
                        'OUTPUT': output,
                        'RTYPE': 5  # Float32
                    }, text="TPI 계산 중...")
                else:
                    # Fallback to standard GDAL TPI
                    runner.run("gdal:tpitopographicpositionindex", {
                        'INPUT': dem_source, 'BAND': 1, 'OUTPUT': output
                    }, text="TPI 계산 중...")
                    window_size = 3
            
            # Apply classification with user threshold
            tpi_classes = self.get_tpi_classes(threshold)
            layer_name = f"TPI (창:{window_size}x{window_size}, 임계값:±{threshold:.2f})"
            layer = QgsRasterLayer(output, layer_name)
            
            if layer.isValid():
                warn_if_temp_output(self.iface, output, what="지형 분석 결과")
                QgsProject.instance().addMapLayer(layer)
                self.apply_style(layer, tpi_classes, 10)
                results.append("TPI")
            
        except ProcessingCancelled:
            raise
        except Exception as e:
            self.iface.messageBar().pushMessage("경고", f"TPI 분석 오류: {str(e)}", level=1)
        finally:
            cleanup_files([downsampled, mean_approx])
            if owns_runner and runner:
                runner.close()
    
    def run_slope_position_analysis(self, dem_source, slope_thresh, tpi_low, tpi_high, results, run_id, runner=None, output_dir=None):
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
        tpi_path = None
        slope_path = None
        owns_runner = False
        try:
            if output_dir is None:
                output_dir = get_archtoolkit_output_dir("TerrainAnalysis")
            if runner is None:
                runner = ProcessingRunner(self.iface, "지형분류 분석", "처리 중...")
                owns_runner = True

            # 1. Generate TPI
            tpi_path = os.path.join(tempfile.gettempdir(), f'archtoolkit_tpi_temp_{run_id}.tif')
            runner.run("gdal:tpitopographicpositionindex", {
                'INPUT': dem_source, 'BAND': 1, 'OUTPUT': tpi_path
            }, text="TPI 생성 중...")
            
            # 2. Generate Slope
            slope_path = os.path.join(tempfile.gettempdir(), f'archtoolkit_slope_temp_{run_id}.tif')
            runner.run("gdal:slope", {
                'INPUT': dem_source, 'BAND': 1, 'SCALE': 1, 'AS_PERCENT': False, 'OUTPUT': slope_path
            }, text="경사도 생성 중...")
            
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
            output_path = os.path.join(output_dir, f'archtoolkit_landform_{run_id}.tif')
            
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
            
            result = runner.run("gdal:rastercalculator", {
                'INPUT_A': tpi_path, 'BAND_A': 1,
                'INPUT_B': slope_path, 'BAND_B': 1,
                'FORMULA': calc_expr,
                'OUTPUT': output_path,
                'RTYPE': 1  # Int16
            }, text="지형 분류 계산 중...")
            
            if result and os.path.exists(output_path):
                layer_name = f"지형분류 (경사:{slope_thresh}°, TPI:{tpi_low:.1f}~{tpi_high:.1f})"
                layer = QgsRasterLayer(output_path, layer_name)
                if layer.isValid():
                    warn_if_temp_output(self.iface, output_path, what="지형 분석 결과")
                    QgsProject.instance().addMapLayer(layer)
                    self.apply_style(layer, self.SLOPE_POSITION_CLASSES, 6)
                    results.append("지형분류")
                else:
                    self.iface.messageBar().pushMessage("경고", "지형분류 레이어 생성 실패", level=1)
            else:
                self.iface.messageBar().pushMessage("경고", "지형분류 래스터 생성 실패", level=1)
                
        except ProcessingCancelled:
            raise
        except Exception as e:
            self.iface.messageBar().pushMessage("경고", f"지형분류 분석 오류: {str(e)}", level=1)
        finally:
            cleanup_files([tpi_path, slope_path])
            if owns_runner and runner:
                runner.close()
