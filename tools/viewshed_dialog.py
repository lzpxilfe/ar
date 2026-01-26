# -*- coding: utf-8 -*-

# ArchToolkit - Archaeology Toolkit for QGIS
# Copyright (C) 2026 balguljang2
# License: GPL v3
"""
Viewshed Analysis Dialog for ArchToolkit
Visibility analysis for archaeological applications: fortifications, temples, etc.

Reference:
- Wang, J., Robinson, G. J., & White, K. (1996). A Fast Solution to Local Viewshed 
  Computation Using Grid-Based Digital Elevation Models. PERS, 62(10), 1157-1164.
"""
import os
import tempfile
import uuid
import math
import shutil
import processing
import numpy as np
from osgeo import gdal, ogr
from qgis.PyQt import uic, QtWidgets, QtCore
from qgis.PyQt.QtCore import Qt, QVariant, QPointF
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QBrush, QFont, QImage, QPolygonF
from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QPushButton, QWidget, QFileDialog, QHBoxLayout, QLabel, QCheckBox
from qgis.core import (
    QgsProject, QgsRasterLayer, QgsVectorLayer, QgsMapLayerProxyModel, QgsRectangle,
    QgsCoordinateTransform, QgsFeatureRequest,
    QgsPointXY, QgsWkbTypes, QgsFeature, QgsGeometry, QgsField,
    QgsRasterShader, QgsColorRampShader, QgsSingleBandPseudoColorRenderer,
    QgsLineSymbol, QgsRendererCategory,
    QgsCategorizedSymbolRenderer, QgsSingleSymbolRenderer, QgsPointLocator,
    QgsMarkerSymbol, QgsPalLayerSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling,
    QgsTextAnnotation, Qgis, QgsUnitTypes
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand, QgsSnapIndicator, QgsMapCanvasAnnotationItem
from qgis.PyQt.QtGui import QTextDocument

from .utils import cleanup_files, is_metric_crs, log_message, restore_ui_focus, push_message, transform_point

# Load the UI file
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'viewshed_dialog_base.ui'))


class ViewshedDialog(QtWidgets.QDialog, FORM_CLASS):
    
    def __init__(self, iface, parent=None):
        super(ViewshedDialog, self).__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        
        # Selected observer point(s)
        self.observer_point = None
        self.target_point = None  # For Line of Sight
        self.observer_points = []  # For multi-point viewshed
        self.point_labels = []  # Text annotations for point numbers
        self.multi_point_mode = False
        self.los_mode = False
        self.los_click_count = 0

        # Reverse viewshed target (polygon) selected via map click
        self._reverse_target_geom = None  # QgsGeometry in source CRS
        self._reverse_target_crs = None
        self._reverse_target_layer_name = None
        self._reverse_target_fid = None
        self.last_result_layer_id = None
        self.result_marker_map = {} # layer_id -> [markers]
        self.result_annotation_map = {} # layer_id -> [annotations] [v1.6.02]
        self.result_observer_layer_map = {} # [v1.6.18] viewshed_layer_id -> observer_layer_id
        self.result_aux_layer_map = {}  # [v1.6.49] raster_layer_id -> [aux_layer_ids]
        self.label_layer = None # Core reference to prevent GC issues
        self._los_profile_data = {}  # viscode_layer_id -> profile payload
        self._los_profile_dialogs = {}  # viscode_layer_id -> dialog instance
        self._los_selection_handlers = {}  # viscode_layer_id -> selectionChanged handler (for disconnect)

        
        
        # Setup layer combos
        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
        self.cmbObserverLayer.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
        
        # Connect signals
        self.btnRun.clicked.connect(self.run_analysis)
        self.btnClose.clicked.connect(self.close)
        self.btnSelectPoint.clicked.connect(self.start_point_selection)
        
        # Auto-sync source radio when layer is selected
        self.cmbObserverLayer.layerChanged.connect(self.on_layer_selection_changed)
        
        # Listen for layer removal for marker cleanup
        QgsProject.instance().layersWillBeRemoved.connect(self.on_layers_removed)

        # LOS profile reopen: selecting the Viscode layer can reopen its profile
        try:
            self.iface.currentLayerChanged.connect(self._on_current_layer_changed)
        except Exception:
            pass
        try:
            self.iface.layerTreeView().clicked.connect(self._on_layer_tree_clicked)
        except Exception:
            pass
        
        # Mode radio buttons
        self.radioSinglePoint.toggled.connect(self.on_mode_changed)
        self.radioLineViewshed.toggled.connect(self.on_mode_changed)
        self.radioReverseViewshed.toggled.connect(self.on_mode_changed)
        self.radioMultiPoint.toggled.connect(self.on_mode_changed)
        self.radioLineOfSight.toggled.connect(self.on_mode_changed)
        
        # Layer source radio buttons
        self.radioClickMap.toggled.connect(self.on_source_changed)
        self.radioFromLayer.toggled.connect(self.on_source_changed)
        
        # Default to Map Click as requested
        self.radioClickMap.setChecked(True)
        
        # Map tool for point selection
        self.map_tool = None
        self.original_tool = None
        
        # Rubber band for showing selected point
        self.point_marker = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        self.point_marker.setColor(QColor(255, 0, 0))
        self.point_marker.setWidth(3)
        self.point_marker.setIconSize(8)
        self.point_marker.setIcon(QgsRubberBand.ICON_CIRCLE)
        
        # Set default colors for visibility styling
        if hasattr(self, 'btnNotVisibleColor'):
            self.btnNotVisibleColor.setColor(QColor(255, 223, 223))  # #ffdfdf
        if hasattr(self, 'btnVisibleColor'):
            self.btnVisibleColor.setColor(QColor(0, 200, 0, 180))  # Semi-transparent green
        
        # [v1.5.95] Initialize scientific context and Higuchi signals
        if hasattr(self, 'chkHiguchi'):
            self.chkHiguchi.toggled.connect(self.on_higuchi_toggled)
        
        # Programmatically update tooltips for scientific basis
        if hasattr(self, 'chkCurvature'):
            self.chkCurvature.setToolTip(
                "지구 곡률 보정(평면 가정 해제)\n"
                "- 곡률 하강량(근사): Δh ≈ d²/(2R)\n"
                "- R: 지구 반경(약 6,371km)\n"
                "- 효과는 거리(d)의 제곱에 비례하므로, 반경이 짧으면 결과가 거의 안 바뀔 수 있습니다."
            )
        if hasattr(self, 'chkRefraction'):
            self.chkRefraction.setToolTip(
                "대기 굴절 보정(표준대기 근사)\n"
                "- 굴절계수 k(기본 0.13): 빛이 아래로 휘는 정도(곡률 효과를 일부 상쇄)\n"
                "- k↑ → 곡률 보정량↓ → 원거리에서 '더 보임' 쪽으로 결과가 바뀔 수 있음\n"
                "- k↓ → 곡률 보정량↑ → 원거리에서 '덜 보임' 쪽으로 결과가 바뀔 수 있음\n"
                "※ 굴절은 곡률과 함께 의미가 있어, 일반적으로 곡률 보정과 같이 사용합니다."
            )
        
        # [v1.6.0] Add Refraction UI programmatically since we can't edit .ui easily
        # Insert a spinbox next to the refraction checkbox if possible, or in a new layout
        self.spinRefraction = QtWidgets.QDoubleSpinBox(self)
        self.spinRefraction.setRange(0.0, 1.0)
        self.spinRefraction.setSingleStep(0.01)
        self.spinRefraction.setDecimals(2)
        self.spinRefraction.setValue(0.13) # Default refraction coefficient
        self.spinRefraction.setToolTip(
            "대기 굴절 계수 k (Refraction Coefficient)\n"
            "- 범위(권장): 대략 0.00~0.20 (대기 상태에 따라 변동)\n"
            "- 해석: k가 커질수록 지구 곡률로 인한 시야 제한이 완화됩니다.\n"
            "- 본 도구는 GDAL gdal_viewshed의 -cc(곡률/굴절 계수)에 cc=1-k로 전달합니다."
        )
        self.spinRefraction.setEnabled(self.chkRefraction.isChecked())
        
        # [v1.5.96] Correctly inject Refraction UI into QGridLayout
        if hasattr(self, 'gridLayout_2'):
            layout = self.gridLayout_2
            # Move chkRefraction to col 0 (original was colspan 2)
            layout.removeWidget(self.chkRefraction)
            layout.addWidget(self.chkRefraction, 5, 0)
            # Add spinbox to col 1
            layout.addWidget(self.spinRefraction, 5, 1)
            
            # Keep the main UI clean: show only a short summary + a "details" dialog.
            self.lblScienceSummary = QtWidgets.QLabel(self)
            self.lblScienceSummary.setWordWrap(True)
            self.lblScienceSummary.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(self.lblScienceSummary, 6, 0)

            self.btnScienceHelp = QtWidgets.QToolButton(self)
            self.btnScienceHelp.setText("설명")
            self.btnScienceHelp.setToolTip("곡률/굴절(대기굴절) 보정 설명 보기")
            layout.addWidget(self.btnScienceHelp, 6, 1)
            
        # [v1.6.19] Connect signal for automatic cleanup (Line 88 already uses layersWillBeRemoved)
        # Consolidating to line 88 for redundancy reduction.
            
        self.chkRefraction.toggled.connect(self.spinRefraction.setEnabled)
        if hasattr(self, 'chkRefraction'):
            self.chkRefraction.toggled.connect(self._on_refraction_toggled)
        if hasattr(self, 'chkCurvature'):
            self.chkCurvature.toggled.connect(self._on_curvature_toggled)
        if hasattr(self, "btnScienceHelp"):
            self.btnScienceHelp.clicked.connect(self._show_curvature_refraction_help_dialog)
        if hasattr(self, 'spinRefraction'):
            self.spinRefraction.valueChanged.connect(self._update_curvature_refraction_help)
        if hasattr(self, "spinMaxDistance"):
            self.spinMaxDistance.valueChanged.connect(self._update_curvature_refraction_help)
        if hasattr(self, "spinObserverHeight"):
            self.spinObserverHeight.valueChanged.connect(self._update_curvature_refraction_help)
        if hasattr(self, "spinTargetHeight"):
            self.spinTargetHeight.valueChanged.connect(self._update_curvature_refraction_help)
        
        # [v1.5.90] Code-level UI overrides for terminology and defaults
        self.radioLineViewshed.setText("선형 및 둘레 가시권 (Line/Perimeter)")
        self.radioLineViewshed.setToolTip("선형 경로(도로, 해안선)나 성곽 둘레(Perimeter)를 따라 이동하며 보이는 영역을 분석합니다.")

        self.radioLineOfSight.setToolTip(
            "두 지점 사이의 시야가 확보되는지를 단면(프로파일)로 확인합니다.\n"
            "- 지도/프로파일 색상: 초록=보임, 빨강=안보임\n"
            "- 결과 Viscode 선을 선택하면 프로파일을 다시 열 수 있습니다."
        )
        
        if hasattr(self, "spinLineMaxPoints"):
            self.spinLineMaxPoints.setValue(50)
        if hasattr(self, "spinMaxPoints"):
            self.spinMaxPoints.setValue(50)

        # [v1.6.1] Fix Maximum Distance limit to allow > 2500m
        if hasattr(self, "spinMaxDistance"):
            self.spinMaxDistance.setMaximum(999999) # Allow large analysis radius
            # Set default if needed, but respect UI default usually
        
        # [v1.6.1] Safer Refraction Widget Insertion
        # If previous insertion failed (no parent layout found), try finding thegroupBox
        if self.spinRefraction.parent() == self:
             # It means it's just floating on the dialog, which might be invisible or wrongly placed
             # Let's try to add it to 'groupParameters' layout if exists
             if hasattr(self, 'groupParameters') and self.groupParameters.layout():
                 row = self.groupParameters.layout().rowCount()
                 self.groupParameters.layout().addWidget(QLabel("대기 굴절 계수 (Refraction):"), row, 0)
                 self.groupParameters.layout().addWidget(self.spinRefraction, row, 1)
             
             # Or if chkRefraction is in a specific layout
             elif self.chkRefraction.parentWidget():
                  layout = self.chkRefraction.parentWidget().layout()
                  if layout:
                      # Attempt to add to the layout
                      if isinstance(layout, QtWidgets.QGridLayout):
                          # Logic to find position? Too complex, just add to end
                          layout.addWidget(self.spinRefraction)
                      elif isinstance(layout, (QtWidgets.QVBoxLayout, QtWidgets.QHBoxLayout)):
                          layout.addWidget(self.spinRefraction)

        self._update_curvature_refraction_help()
    
    def transform_point(self, point, source_crs, dest_crs):
        """Wrapper method to call the utility transform_point function"""
        return transform_point(point, source_crs, dest_crs)

    def _identify_polygon_feature_at_canvas_point(self, canvas_point):
        """Identify a polygon feature under a canvas click.

        Returns:
            (QgsGeometry, QgsCoordinateReferenceSystem, layer_name, fid) or None
        """
        try:
            canvas_crs = self.canvas.mapSettings().destinationCrs()
            layers = list(self.canvas.mapSettings().layers() or [])
            if not layers:
                return None

            try:
                tol = float(self.canvas.mapUnitsPerPixel()) * 5.0
            except Exception:
                tol = 0.0
            if tol <= 0.0:
                tol = 1.0

            for layer in reversed(layers):  # top-most first
                if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                    continue
                if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                    continue

                try:
                    pt_layer = self.transform_point(canvas_point, canvas_crs, layer.crs())
                except Exception:
                    continue

                rect = QgsRectangle(
                    pt_layer.x() - tol,
                    pt_layer.y() - tol,
                    pt_layer.x() + tol,
                    pt_layer.y() + tol,
                )
                request = QgsFeatureRequest().setFilterRect(rect).setLimit(10)
                click_geom = QgsGeometry.fromPointXY(pt_layer)
                for feat in layer.getFeatures(request):
                    geom = feat.geometry()
                    if not geom or geom.isEmpty():
                        continue
                    try:
                        if geom.contains(click_geom) or geom.intersects(click_geom):
                            return geom, layer.crs(), layer.name(), feat.id()
                    except Exception:
                        # If geometry predicates fail due to invalid geometry, still accept bbox match.
                        return geom, layer.crs(), layer.name(), feat.id()
        except Exception as e:
            log_message(f"Polygon identify error: {e}", level=Qgis.Warning)
        return None

    def _build_gdal_viewshed_extra(self, curvature, refraction, refraction_coeff):
        """
        Build GDAL viewshed command-line args for QGIS Processing's `gdal:viewshed`.

        QGIS 3.40's `gdal:viewshed` wrapper does not expose curvature/refraction
        parameters directly; instead we pass them through the `EXTRA` string.

        Note: GDAL's `-cc` expects a combined curvature/refraction coefficient.
        This plugin's UI uses a refraction coefficient `k` (default ~0.13), so:
        - curvature off  -> -cc 0
        - curvature on, refraction off -> -cc 1
        - curvature on, refraction on  -> -cc (1 - k)
        """
        cc = self._calculate_gdal_viewshed_cc(curvature, refraction, refraction_coeff)
        return f"-cc {cc}"

    def _calculate_gdal_viewshed_cc(self, curvature, refraction, refraction_coeff):
        # Refraction is a correction applied together with curvature.
        if refraction and not curvature:
            curvature = True

        if not curvature:
            return 0.0

        if refraction:
            cc = 1.0 - float(refraction_coeff)
            cc = max(0.0, min(1.0, cc))
        else:
            cc = 1.0

        return cc

    def _on_refraction_toggled(self, checked):
        if checked and hasattr(self, 'chkCurvature') and not self.chkCurvature.isChecked():
            # Refraction without curvature isn't meaningful; keep UI consistent with execution.
            self.chkCurvature.setChecked(True)
        self._update_curvature_refraction_help()

    def _on_curvature_toggled(self, checked):
        if not checked and hasattr(self, 'chkRefraction') and self.chkRefraction.isChecked():
            self.chkRefraction.setChecked(False)
        self._update_curvature_refraction_help()

    def _update_curvature_refraction_help(self):
        if not hasattr(self, 'lblScienceSummary'):
            return

        try:
            r_earth = 6371000.0  # meters
            max_dist = self.spinMaxDistance.value() if hasattr(self, "spinMaxDistance") else 0.0

            curvature = self.chkCurvature.isChecked() if hasattr(self, "chkCurvature") else False
            refraction = self.chkRefraction.isChecked() if hasattr(self, "chkRefraction") else False
            k = self.spinRefraction.value() if hasattr(self, "spinRefraction") else 0.13

            cc = self._calculate_gdal_viewshed_cc(curvature, refraction, k)

            # Curvature drop over distance d (flat-earth vs sphere) approximation.
            drop_curv = (max_dist ** 2) / (2.0 * r_earth) if max_dist else 0.0
            drop_apparent = drop_curv * cc

            def curvature_drop(distance_m):
                return (distance_m ** 2) / (2.0 * r_earth)

            # Rule-of-thumb examples (flat terrain): how big curvature/refraction is at km scales.
            d5 = 5000.0
            d10 = 10000.0
            d20 = 20000.0
            drop5 = curvature_drop(d5)
            drop10 = curvature_drop(d10)
            drop20 = curvature_drop(d20)
            refr_relief_5 = drop5 * k
            refr_relief_10 = drop10 * k
            refr_relief_20 = drop20 * k

            # Distance where refraction (0 ~ k) changes curvature drop by 1m / 5m.
            if k > 0:
                d_for_1m = math.sqrt((2.0 * r_earth * 1.0) / k)
                d_for_5m = math.sqrt((2.0 * r_earth * 5.0) / k)
                ref_meaning_text = f"굴절 보정(=곡률 낙하 차이) 1m~{d_for_1m/1000:.1f}km, 5m~{d_for_5m/1000:.1f}km"
            else:
                ref_meaning_text = "k=0이면 굴절 효과 없음"

            status_label = "OFF"
            if curvature and refraction:
                status_label = "곡률+굴절"
            elif curvature:
                status_label = "곡률"

            self.lblScienceSummary.setText(
                f"{status_label}: k={k:.2f}, cc={cc:.3f} · 반경 {max_dist:,.0f}m: "
                f"곡률 하강 {drop_curv:.2f}m → 적용 {drop_apparent:.2f}m"
            )

            self._science_help_html = (
                "<div style='font-size:11pt; line-height:1.45; color:#222;'>"
                "<h3 style='margin:0 0 6px 0;'>곡률/굴절(대기굴절) 보정</h3>"
                f"<b>현재 설정</b><br>"
                f"- 곡률: {'ON' if curvature else 'OFF'} / 굴절: {'ON' if refraction else 'OFF'}<br>"
                f"- k={k:.2f} → cc={cc:.3f} (GDAL gdal_viewshed -cc로 전달)<br><br>"
                "<b>근거(근사)</b><br>"
                "- 곡률 하강량: Δh ~ d²/(2R), R=6,371km<br>"
                "- 굴절 포함: Δh ~ d²/(2R) · cc, (곡률 ON일 때) cc=1-k<br>"
                "- GDAL 기본값: cc=0.85714(~6/7 → k~0.14286)<br><br>"
                "<b>현재 반경에서 규모</b><br>"
                f"- 반경 {max_dist:,.0f}m: 곡률 하강(굴절없음) ~ {drop_curv:.2f}m, 적용 ~ {drop_apparent:.2f}m<br>"
                "- d² 비례라 반경이 짧으면(예: 1km) 체크해도 결과가 거의 안 바뀔 수 있음<br><br>"
                "<b>언제 의미 있나(대략)</b><br>"
                f"- {ref_meaning_text}<br><br>"
                "<b>예시(평탄 지형 기준)</b><br>"
                f"- 5km: 곡률 ~ {drop5:.1f}m, 굴절 완화 ~ {refr_relief_5:.2f}m<br>"
                f"- 10km: 곡률 ~ {drop10:.1f}m, 굴절 완화 ~ {refr_relief_10:.2f}m<br>"
                f"- 20km: 곡률 ~ {drop20:.1f}m, 굴절 완화 ~ {refr_relief_20:.2f}m<br>"
                "</div>"
            )
        except Exception:
            # Never fail the tool due to UI help text
            pass

    def _show_curvature_refraction_help_dialog(self):
        try:
            self._update_curvature_refraction_help()
            html = getattr(self, "_science_help_html", None) or ""

            dlg = QDialog(self)
            dlg.setWindowTitle("곡률/굴절(대기굴절) 보정 설명")
            layout = QVBoxLayout(dlg)

            text = QtWidgets.QTextBrowser(dlg)
            text.setOpenExternalLinks(True)
            text.setHtml(html)
            layout.addWidget(text)

            btn_close = QPushButton("닫기", dlg)
            btn_close.clicked.connect(dlg.accept)
            layout.addWidget(btn_close)

            dlg.resize(640, 480)
            dlg.exec_()
        except Exception:
            pass
    
    def reset_selection(self):
        """Reset all manual point selections and markers"""
        self.observer_point = None
        self.target_point = None
        self.observer_points = []
        self._reverse_target_geom = None
        self._reverse_target_crs = None
        self._reverse_target_layer_name = None
        self._reverse_target_fid = None
        if hasattr(self, 'drawn_line_points'):
            self.drawn_line_points = []
        self.los_click_count = 0
        if hasattr(self, 'point_marker'):
            self.point_marker.reset(QgsWkbTypes.PointGeometry)
        
        # Clear point number labels (Canvas items)
        if hasattr(self, 'point_labels'):
            for item in self.point_labels:
                try:
                    if self.canvas and self.canvas.scene():
                        self.canvas.scene().removeItem(item)
                except Exception:
                    pass
            self.point_labels = []
        
        # Move label layer to top logic - we use canvas items now, but results are in layers
        # Standardize result layer addition
        self._remove_label_layer()
        self.lblSelectedPoint.setText("선택된 관측점 없음")

        self.lblSelectedPoint.setStyleSheet("")
        self.canvas.refresh()
    
    def _add_point_to_label_canvas(self, point, number):
        """Add a numbered label directly to map canvas using Annotations (High Stability)"""
        try:
            # 1. Create a Text Annotation
            annotation = QgsTextAnnotation()
            
            # 2. Configure Text Document
            doc = QTextDocument()
            html = f'<div style="color: red; font-weight: bold; background-color: rgba(255,255,255,180); border: 1px solid red; padding: 1px 3px; border-radius: 3px;">{number}</div>'
            doc.setHtml(html)
            annotation.setDocument(doc)
            
            # 3. Position and Settings
            annotation.setMapPosition(point)
            annotation.setHasFixedMapPosition(True)
            annotation.setFrameSizeQt(QtCore.QSizeF(30, 20)) # Width, Height
            
            # Simple offset to top-right
            annotation.setRelativePosition(QtCore.QPointF(0.5, 0.5))
            
            # 4. Create Canvas Item (This actually shows it on map without project layer)
            item = QgsMapCanvasAnnotationItem(annotation, self.canvas)
            
            # Store for cleanup
            self.point_labels.append(item)
            return item
            
        except Exception as e:
            log_message(f"Canvas labeling error: {e}", level=Qgis.Warning)
            return None
    
    # [v1.6.17] _get_or_create_label_layer REMOVED - deprecated, was returning None


    
    def _remove_label_layer(self):
        """Remove the temporary label layer"""
        layer_name = "관측점_번호_라벨"
        layers = QgsProject.instance().mapLayersByName(layer_name)
        for layer in layers:
            try:
                QgsProject.instance().removeMapLayer(layer.id())
            except Exception:
                pass
        self.label_layer = None

                
    def update_layer_order(self):
        """Move the label layer to the top of the layer list to prevent it from being covered"""
        layer_name = "관측점_번호_라벨"
        layers = QgsProject.instance().mapLayersByName(layer_name)
        if layers:
            layer = layers[0]
            root = QgsProject.instance().layerTreeRoot()
            layer_node = root.findLayer(layer.id())
            if layer_node:
                # Store visibility state
                is_visible = layer_node.isVisible()
                # Clone and move to top (index 0)
                parent = layer_node.parent()
                clone = layer_node.clone()
                clone.setItemVisibilityChecked(is_visible)
                root.insertChildNode(0, clone)
                parent.removeChildNode(layer_node)

    def on_mode_changed(self):
        """Enable/disable options based on analysis mode"""
        # Clear previous selections when mode changes
        self.reset_selection()
        
        is_line_mode = self.radioLineViewshed.isChecked()
        is_multi_mode = self.radioMultiPoint.isChecked()
        is_los_mode = self.radioLineOfSight.isChecked()
        is_reverse_mode = self.radioReverseViewshed.isChecked()
        
        # Enable line options for appropriate modes
        self.groupLineOptions.setEnabled(is_line_mode or is_multi_mode or is_reverse_mode)
        
        # Show/Hide Count Only checkbox - relevant for Line and Multi-point
        if hasattr(self, 'chkCountOnly'):
            self.chkCountOnly.setVisible(is_line_mode or is_multi_mode)
            if not (is_line_mode or is_multi_mode):
                self.chkCountOnly.setChecked(False)

        # Show/Hide Visual Imbalance checkbox - reverse viewshed only
        if hasattr(self, "chkVisualImbalance"):
            self.chkVisualImbalance.setVisible(is_reverse_mode)
            if not is_reverse_mode:
                self.chkVisualImbalance.setChecked(False)
        
        # Update internal mode flags
        self.multi_point_mode = is_multi_mode
        self.los_mode = is_los_mode
        
        # === Mode-specific UI adjustments ===
        
        # 1. Line Mode: Enable Drawing OR Layer selection
        if is_line_mode:
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 분석 대상(선형/둘레) 설정")
            
            # Filter layer for Line/Polygon only
            self.cmbObserverLayer.setFilters(QgsMapLayerProxyModel.Filter.LineLayer | QgsMapLayerProxyModel.Filter.PolygonLayer)
            
            if self.radioFromLayer.isChecked():
                self.btnSelectPoint.setText("🖱️ 추가 관측점 클릭 (선택사항)")
                if hasattr(self, 'lblLayerHint'):
                    self.lblLayerHint.setText("💡 성곽(Polygon)이나 도로(Line) 레이어를 선택하세요.")
            else:
                self.btnSelectPoint.setText("🖱️ 지도에서 경로(둘레) 그리기")
                if hasattr(self, 'lblLayerHint'):
                    self.lblLayerHint.setText("💡 시작점 클릭 후 경로를 그리세요 (시작점 재클릭 시 자동 닫힘).")
            
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setVisible(True)
        
        # 2. Point-based modes: Enable both options
        elif self.radioSinglePoint.isChecked():
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 관측점 설정")
            self.btnSelectPoint.setText("🖱️ 지도에서 관측점 선택")
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setText("💡 레이어 선택 시: 피처의 중심점(Centroid)에서 가시권을 계산합니다.")
                self.lblLayerHint.setVisible(self.radioFromLayer.isChecked())
        
        elif is_multi_mode:
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 관측점 설정 (다중 선택)")
            self.btnSelectPoint.setText("🖱️ 추가 관측점 클릭")
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setText("💡 레이어의 포인트 + 지도 클릭을 함께 사용할 수 있습니다.")
                self.lblLayerHint.setVisible(self.radioFromLayer.isChecked())
        
        elif is_los_mode:
            self.radioClickMap.setEnabled(True)
            self.radioClickMap.setChecked(True)
            self.radioFromLayer.setEnabled(False)  # LOS는 지도 클릭만
            self.groupObserver.setTitle("3. 가시선 설정")
            self.btnSelectPoint.setText("🖱️ 관측점 → 대상점 순서로 클릭")
            self.observer_point = None
            self.target_point = None
            self.los_click_count = 0
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setText(
                    "팁: 정확한 클릭을 원하면 포인트(점) 벡터 레이어를 만든 뒤 스냅(자석 아이콘)을 켜고 찍으세요.\n"
                    "레이어에서 직접 선택(관측점/대상점 지정) 기능은 단순화를 위해 현재 비활성화되어 있습니다."
                )
                self.lblLayerHint.setVisible(True)
        
        elif is_reverse_mode:
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 대상물 위치 설정")
            self.btnSelectPoint.setText("🖱️ 지도에서 대상물/영역 지정")
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setText(
                    "팁: 점=1회 클릭 후 우클릭/Enter로 완료, 폴리곤=여러 점(3점 이상) 찍고 우클릭/Enter로 완료.\n"
                    "기존 폴리곤 위를 클릭하면 해당 폴리곤이 자동 선택됩니다.\n"
                    "직접 그리려면 Shift를 누른 채 첫 점을 찍으세요."
                )
                self.lblLayerHint.setVisible(True)
        
        else:
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 관측점 설정")
            self.btnSelectPoint.setText("🖱️ 지도에서 위치 선택")
        
        # Reset layer selection radio for non-LOS modes
        if not is_los_mode:
            self.radioFromLayer.setEnabled(True)
            
        # 3. Target Height: Enable only for Reverse Viewshed or Line of Sight
        # (Target Height is the object height we are looking for in standard viewshed, 
        # but the user requested to limit it to avoid confusion)
        self.spinTargetHeight.setEnabled(is_reverse_mode or is_los_mode)
        if hasattr(self, 'lblTargetHeight'):
            self.lblTargetHeight.setEnabled(is_reverse_mode or is_los_mode)

        # 4. Layer filters: Filter by geometry type based on mode
        if is_line_mode:
            # Only show Line or Polygon layers for Line Viewshed (those with length/perimeter)
            self.cmbObserverLayer.setFilters(QgsMapLayerProxyModel.Filter.LineLayer | QgsMapLayerProxyModel.Filter.PolygonLayer)
        else:
            # Show Point and Polygon layers (to support centroid-based analysis)
            self.cmbObserverLayer.setFilters(QgsMapLayerProxyModel.Filter.PointLayer | QgsMapLayerProxyModel.Filter.PolygonLayer)
        
        # Trigger source change handler to update dependent UI
        self.on_source_changed()
    
    def on_source_changed(self):
        """Toggle between map click and layer selection
        
        Handles mode-specific text and behavior:
        - Line mode: Show layer selection hints
        - Point modes: Show point layer hints
        - Multi-point: Enable hybrid mode (layer + clicks)
        """
        from_layer = self.radioFromLayer.isChecked()
        is_multi = self.radioMultiPoint.isChecked()
        is_line_mode = self.radioLineViewshed.isChecked()
        
        # Update radio button text based on mode
        if is_line_mode:
            self.radioFromLayer.setText("레이어에서 선택")
        elif from_layer:
            self.radioFromLayer.setText("레이어에서 선택")
        else:
            self.radioFromLayer.setText("레이어에서 선택")
        
        # If switching to layer, clear manual selection
        if from_layer and not is_line_mode:
            self.reset_selection()
        
        self.cmbObserverLayer.setEnabled(from_layer)
        
        # Button enable logic
        if is_line_mode:
            # Line mode: allow additional point clicks
            self.btnSelectPoint.setEnabled(True)
        elif is_multi:
            # Multi-point: always allow manual clicks (hybrid mode)
            self.btnSelectPoint.setEnabled(True)
        else:
            # Other modes: disable button when using layer
            self.btnSelectPoint.setEnabled(not from_layer)
        
        # UI Feedback based on mode
        if from_layer:
            if is_line_mode:
                self.lblSelectedPoint.setText("소스: 선택된 선형/둘레 레이어")
            else:
                self.lblSelectedPoint.setText("소스: 선택된 레이어")
            
            if not is_multi and not is_line_mode:
                self.point_marker.reset(QgsWkbTypes.PointGeometry)
        else:
            if is_line_mode:
                if hasattr(self, 'drawn_line_points') and self.drawn_line_points:
                    self.lblSelectedPoint.setText(f"그려진 경로: {len(self.drawn_line_points)}개 정점 {'(폐곡선)' if self.is_line_closed else '(개곡선)'}")
                else:
                    self.lblSelectedPoint.setText("그려진 경로: 없음 (지도를 클릭하세요)")
            elif self.observer_point:
                self.lblSelectedPoint.setText(f"선택된 위치: {self.observer_point.x():.1f}, {self.observer_point.y():.1f}")
            else:
                self.lblSelectedPoint.setText("선택된 위치: 없음")

        # Update optional UI that depends on source + geometry type.
        self._update_cutout_input_polygon_ui()

    def on_layer_selection_changed(self, layer):
        """Auto-check 'From Layer' when a layer is selected in the combo box"""
        if layer:
            self.radioFromLayer.setChecked(True)
        self._update_cutout_input_polygon_ui()

    def _update_cutout_input_polygon_ui(self):
        """Show/enable cut-out option only when it applies (Multi + From Layer + Polygon)."""
        if not hasattr(self, "chkCutoutInputPolygon"):
            return
        try:
            is_multi = self.radioMultiPoint.isChecked()
            from_layer = self.radioFromLayer.isChecked()
            obs_layer = self.cmbObserverLayer.currentLayer() if from_layer else None
            is_poly = bool(
                obs_layer
                and hasattr(obs_layer, "geometryType")
                and obs_layer.geometryType() == QgsWkbTypes.PolygonGeometry
            )
            show = is_multi and from_layer and is_poly
            self.chkCutoutInputPolygon.setVisible(show)
            self.chkCutoutInputPolygon.setEnabled(show)
            if not show:
                self.chkCutoutInputPolygon.setChecked(False)
        except Exception:
            try:
                self.chkCutoutInputPolygon.setVisible(False)
            except Exception:
                pass

    def on_layers_removed(self, layer_ids):
        """Clean up markers and annotations if the corresponding analysis layer is removed"""
        for lid in layer_ids:
            # 1. Clean up RubberBands (Red Dots)
            if lid in self.result_marker_map:
                markers = self.result_marker_map[lid]
                for m in markers:
                    try:
                        if m:
                            m.hide() # Force hide first
                            m.reset(QgsWkbTypes.PointGeometry) # Clear geometry
                            if self.canvas and self.canvas.scene():
                                self.canvas.scene().removeItem(m) # Remove from scene
                    except Exception as e:
                        log_message(f"Marker cleanup error: {e}", level=Qgis.Warning)
                del self.result_marker_map[lid]
                
            # 2. Clean up Text Annotations (Labels) [v1.6.02]
            if lid in self.result_annotation_map:
                annotations = self.result_annotation_map[lid]
                for item in annotations:
                    try:
                        if item and self.canvas.scene():
                            self.canvas.scene().removeItem(item)
                    except Exception as e:
                        log_message(f"Annotation cleanup error: {e}", level=Qgis.Warning)
                del self.result_annotation_map[lid]
            
            # 3. [v1.6.18] Clean up linked Observer Layer (red points layer)
            if lid in self.result_observer_layer_map:
                obs_layer_id = self.result_observer_layer_map[lid]
                try:
                    QgsProject.instance().removeMapLayer(obs_layer_id)
                except Exception:
                    pass
                del self.result_observer_layer_map[lid]

            # 3-1. Clean up linked auxiliary layers (e.g., analysis radius rings)
            if lid in self.result_aux_layer_map:
                aux_ids = self.result_aux_layer_map.get(lid, [])
                for aux_id in aux_ids:
                    try:
                        QgsProject.instance().removeMapLayer(aux_id)
                    except Exception:
                        pass
                del self.result_aux_layer_map[lid]

            # 4. Clean up LOS profile payload/dialogs
            if lid in getattr(self, "_los_profile_data", {}):
                try:
                    del self._los_profile_data[lid]
                except Exception:
                    pass

            # 4-1. Disconnect LOS selection handlers (to avoid keeping dialog alive)
            if lid in getattr(self, "_los_selection_handlers", {}):
                try:
                    handler = self._los_selection_handlers.pop(lid, None)
                    layer = QgsProject.instance().mapLayer(lid)
                    if layer and handler:
                        layer.selectionChanged.disconnect(handler)
                except Exception:
                    pass

            if lid in getattr(self, "_los_profile_dialogs", {}):
                try:
                    dlg = self._los_profile_dialogs.pop(lid, None)
                    if dlg:
                        dlg.close()
                except Exception:
                    pass
        
        if self.last_result_layer_id in layer_ids:
            self.reset_selection()
            self.last_result_layer_id = None

    def _on_current_layer_changed(self, layer):
        try:
            if not layer:
                return
            layer_id = layer.id()
            if layer_id in getattr(self, "_los_profile_data", {}):
                self.open_los_profile(layer_id)
        except Exception as e:
            log_message(f"Current layer handler error: {e}", level=Qgis.Warning)

    def _on_layer_tree_clicked(self, _index):
        try:
            layer = self.iface.activeLayer()
            if not layer:
                return
            layer_id = layer.id()
            if layer_id in getattr(self, "_los_profile_data", {}):
                self.open_los_profile(layer_id)
        except Exception as e:
            log_message(f"Layer tree handler error: {e}", level=Qgis.Warning)

    def get_context_point_and_crs(self):
        """Helper to get observer point(s) and their source CRS
        Returns a list of (point, crs) tuples.
        """
        points_with_crs = []
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        
        # 1. Check for manual override (If user clicked on map, use it regardless of mode)
        if self.observer_point:
            points_with_crs.append((self.observer_point, canvas_crs))
            
        # 2. If no manual override, or in multi-point/layer mode, add layer features
        if self.radioFromLayer.isChecked():
            obs_layer = self.cmbObserverLayer.currentLayer()
            if obs_layer:
                # Prioritize selected features
                selected_features = obs_layer.selectedFeatures()
                features = selected_features if selected_features else []
                
                # If nothing selected and no manual point, fallback to first feature
                if not features and not points_with_crs:
                    first_feat = next(obs_layer.getFeatures(), None)
                    if first_feat:
                        features = [first_feat]
                
                for feat in features:
                    if not feat: continue
                    geom = feat.geometry()
                    if geom and not geom.isEmpty():
                        # Use centroid
                        pt = geom.centroid().asPoint()
                        # Only add if it's not already the manual point (edge case)
                        points_with_crs.append((pt, obs_layer.crs()))
        
        # 3. Handle multi-point clicks
        if self.multi_point_mode:
            for p in self.observer_points:
                points_with_crs.append((p, canvas_crs))
        
        return points_with_crs

    def start_point_selection(self):
        """Start point or line selection on map depending on mode"""
        # NO project modification here!
        self.original_tool = self.canvas.mapTool()

        
        # Use line drawing tool for Line Viewshed and Reverse Viewshed (polygon drawing)
        if self.radioLineViewshed.isChecked() or self.radioReverseViewshed.isChecked():
            self.map_tool = ViewshedLineTool(self.canvas, self)
            self.canvas.setMapTool(self.map_tool)

            if self.radioReverseViewshed.isChecked():
                self.iface.messageBar().pushMessage(
                    "역방향 가시권",
                    "점=1회 클릭 후 우클릭/Enter로 완료, 폴리곤=여러 점(3점 이상) 찍고 우클릭/Enter로 완료. 기존 폴리곤 위 클릭=자동 선택, Shift+클릭=직접 그리기.",
                    level=0,
                )
            else:
                self.iface.messageBar().pushMessage(
                    "선형 및 둘레 가시권",
                    "지도에서 라인을 그리세요. 클릭으로 점 추가, 시작점 클릭 시 자동 닫힘(Snap), 우클릭으로 완료",
                    level=0,
                )
        else:
            self.map_tool = ViewshedPointTool(self.canvas, self)
            self.canvas.setMapTool(self.map_tool)

            title = "가시권 분석"
            text = "지도에서 관측점을 클릭하세요"
            if self.los_mode:
                title = "가시선 분석"
                text = "지도에서 관측점 → 대상점 순서로 클릭하세요 (2번)"
            elif self.radioReverseViewshed.isChecked():
                title = "역방향 가시권"
                text = "지도에서 대상물(점/폴리곤)을 클릭하세요. 폴리곤은 영역을 클릭하면 선택됩니다."
            elif self.multi_point_mode:
                title = "다중점 가시권"
                text = "지도에서 관측점을 여러 번 클릭하세요 (ESC로 완료)"

            self.iface.messageBar().pushMessage(
                title, text, level=0
            )
        self.hide()
    
    def set_observer_point(self, point):
        """Called when user clicks on map"""
        if self.multi_point_mode:
            # Multi-point mode: add to list
            self.observer_points.append(point)
            self.point_marker.addPoint(point)
            
            count = len(self.observer_points)
            
            # Add point number on canvas
            self._add_point_to_label_canvas(point, count)

            
            self.lblSelectedPoint.setText(f"선택된 관측점: {count}개")
            self.lblSelectedPoint.setStyleSheet("color: #2196F3; font-weight: bold;")
            
            # Show message and continue adding
            self.iface.messageBar().pushMessage(
                "다중점 가시권", 
                f"▶ 점 {count} 추가됨. 계속 클릭하거나 ESC로 완료", 
                level=0
            )
            # Don't return to dialog yet - let user add more points
        
        elif self.los_mode:
            # Line of Sight mode: first click = observer, second click = target
            self.los_click_count += 1
            self.point_marker.addPoint(point)
            
            if self.los_click_count == 1:
                self.observer_point = point
                self.iface.messageBar().pushMessage(
                    "가시선 분석", 
                    "관측점 설정 완료. 이제 대상점을 클릭하세요", 
                    level=0
                )
            else:
                self.target_point = point
                self.lblSelectedPoint.setText(
                    f"관측점→대상점: ({self.observer_point.x():.0f},{self.observer_point.y():.0f}) → ({point.x():.0f},{point.y():.0f})"
                )
                self.lblSelectedPoint.setStyleSheet("color: #2196F3; font-weight: bold;")
                
                # Both points selected, return to dialog
                if self.original_tool:
                    self.canvas.setMapTool(self.original_tool)
                self.show()
        
        else:
            # Single point mode
            # Reset reverse-viewshed polygon selection (if any)
            self._reverse_target_geom = None
            self._reverse_target_crs = None
            self._reverse_target_layer_name = None
            self._reverse_target_fid = None

            # Reverse viewshed: allow polygon selection by clicking on a polygon feature.
            if self.radioReverseViewshed.isChecked() and not self.radioFromLayer.isChecked():
                hit = self._identify_polygon_feature_at_canvas_point(point)
                if hit:
                    geom, src_crs, layer_name, fid = hit
                    self._reverse_target_geom = geom
                    self._reverse_target_crs = src_crs
                    self._reverse_target_layer_name = layer_name
                    self._reverse_target_fid = fid

                    # Show marker at polygon centroid (more intuitive than the clicked interior point)
                    marker_pt = point
                    try:
                        centroid_src = geom.centroid().asPoint()
                        marker_pt = self.transform_point(
                            centroid_src,
                            src_crs,
                            self.canvas.mapSettings().destinationCrs(),
                        )
                    except Exception:
                        pass

                    self.observer_point = marker_pt
                    self.point_marker.reset(QgsWkbTypes.PointGeometry)
                    self.point_marker.addPoint(marker_pt)

                    self.lblSelectedPoint.setText(f"선택된 폴리곤: {layer_name} (FID: {fid})")
                    self.lblSelectedPoint.setStyleSheet("color: #2196F3; font-weight: bold;")

                    # Restore original tool and show dialog
                    if self.original_tool:
                        self.canvas.setMapTool(self.original_tool)
                    self.show()
                    return

            self.observer_point = point
            self.point_marker.reset(QgsWkbTypes.PointGeometry)
            self.point_marker.addPoint(point)
            
            self.lblSelectedPoint.setText(f"선택된 위치: {point.x():.1f}, {point.y():.1f}")
            self.lblSelectedPoint.setStyleSheet("color: #2196F3; font-weight: bold;")
            
            # Restore original tool and show dialog
            if self.original_tool:
                self.canvas.setMapTool(self.original_tool)
            self.show()
    
    # [v1.6.21] transform_to_dem_crs REMOVED - deprecated
    
    def set_line_from_tool(self, points, is_closed=False):
        """Handle a user-drawn line/polygon from the map tool."""
        if not points:
            return

        # Reverse viewshed: treat drawn vertices as a closed polygon target.
        if self.radioReverseViewshed.isChecked():
            if len(points) < 3:
                push_message(self.iface, "오류", "역방향 폴리곤은 최소 3개 점이 필요합니다.", level=2)
                return

            canvas_crs = self.canvas.mapSettings().destinationCrs()
            ring = list(points)
            if ring[0] != ring[-1]:
                ring.append(ring[0])

            geom = QgsGeometry.fromPolygonXY([ring])
            if not geom or geom.isEmpty():
                push_message(self.iface, "오류", "폴리곤 생성에 실패했습니다. 점을 다시 선택해주세요.", level=2)
                return

            self._reverse_target_geom = geom
            self._reverse_target_crs = canvas_crs
            self._reverse_target_layer_name = "사용자 정의 영역"
            self._reverse_target_fid = None

            # Show the polygon outline on map (selection marker)
            self.point_marker.reset(QgsWkbTypes.LineGeometry)
            for pt in ring:
                self.point_marker.addPoint(pt)

            # Store centroid as observer_point for downstream single-point fallback / UI state
            try:
                self.observer_point = geom.centroid().asPoint()
            except Exception:
                self.observer_point = points[0]

            self.lblSelectedPoint.setText("선택된 폴리곤: 사용자 정의 영역")
            self.lblSelectedPoint.setStyleSheet("color: #2196F3; font-weight: bold;")
            return

        # Default: line viewshed path storage
        self.drawn_line_points = points
        self.is_line_closed = is_closed
        self.observer_point = points[0]

        # [v1.5.85] Maintain vertex visibility on the map
        self.point_marker.reset(QgsWkbTypes.LineGeometry)
        for pt in points:
            self.point_marker.addPoint(pt)
        if is_closed:
            self.point_marker.addPoint(points[0])

        self.lblSelectedPoint.setText(f"선택된 경로: {len(points)}개 정점 {'(폐곡선)' if is_closed else '(개곡선)'}")
        self.lblSelectedPoint.setStyleSheet("color: #2196F3; font-weight: bold;")
    
    def run_analysis(self):
        """Run the selected viewshed analysis"""
        dem_layer = self.cmbDemLayer.currentLayer()
        if not dem_layer:
            self.iface.messageBar().pushMessage("오류", "DEM 래스터를 선택해주세요", level=2)
            return

        # Distance-based viewshed tools assume metric DEM CRS (meters).
        dem_crs = dem_layer.crs()
        if not is_metric_crs(dem_crs):
            unit_name = QgsUnitTypes.toString(dem_crs.mapUnits())
            push_message(
                self.iface,
                "오류",
                f"DEM CRS 단위가 미터가 아닙니다 (현재: {unit_name}). 가시권/히구치 분석은 미터 단위 투영 CRS가 필요합니다.",
                level=2,
                duration=8,
            )
            return
         
        # Check observer point
        # Check observer point (Supports single selection and multi-clicked list)
        has_manual = self.observer_point is not None or len(self.observer_points) > 0
        has_layer = self.radioFromLayer.isChecked() and self.cmbObserverLayer.currentLayer() is not None

        if not has_manual and not has_layer:
            self.iface.messageBar().pushMessage("오류", "관측점을 선택하거나 레이어를 지정해주세요", level=2)
            return
        
        # Get parameters
        observer_height = self.spinObserverHeight.value()
        target_height = self.spinTargetHeight.value()
        max_distance = self.spinMaxDistance.value()
        curvature = self.chkCurvature.isChecked()
        refraction = self.chkRefraction.isChecked()
        refraction_coeff = 0.13
        if hasattr(self, 'spinRefraction'):
            refraction_coeff = self.spinRefraction.value()
        
        self.iface.messageBar().pushMessage("처리 중", "가시권 분석 실행 중...", level=0)
        
        # [v1.5.97] REMOVED global self.hide() from here. 
        # It is now moved into each specialized run_* method to avoid freezes during warnings.
        
        try:
            if self.radioSinglePoint.isChecked():
                self.run_single_viewshed(
                    dem_layer, observer_height, target_height, 
                    max_distance, curvature, refraction, refraction_coeff
                )
            elif self.radioLineViewshed.isChecked():
                # [v1.6.13] Line Viewshed now uses run_multi_viewshed for proper union logic
                self.run_multi_viewshed(
                    dem_layer, observer_height, target_height,
                    max_distance, curvature, refraction, refraction_coeff
                )
            elif self.radioMultiPoint.isChecked():
                self.run_multi_viewshed(
                    dem_layer, observer_height, target_height,
                    max_distance, curvature, refraction, refraction_coeff
                )
            elif self.radioLineOfSight.isChecked():
                if not self.observer_point or not self.target_point:
                    self.iface.messageBar().pushMessage("오류", "관측점과 대상점을 모두 선택해주세요", level=2)
                    self.show()
                    return
                self.run_line_of_sight(
                    dem_layer, observer_height, target_height
                )
            else:  # Reverse viewshed
                self.run_reverse_viewshed(
                    dem_layer, observer_height, target_height,
                    max_distance, curvature, refraction, refraction_coeff
                )
        except Exception as e:
            self.iface.messageBar().pushMessage("오류", f"분석 중 오류: {str(e)}", level=2)
            self.show()
    
    def run_buffer_visibility(self, dem_layer, obs_height, tgt_height):
        """Analyze visibility from buffer perimeter to center point
        
        Creates points around buffer, checks LOS to center, and creates
        color-coded result showing visible (green) vs obstructed (red) directions.
        """
        center = self.observer_point
        center_crs = self.canvas.mapSettings().destinationCrs()
        # If observer_point is None, but we are in fromLayer mode, we need to pick the centroid
        if not center:
            pts = self.get_context_point_and_crs()
            if pts:
                center, center_crs = pts[0]

        if not center:
            push_message(self.iface, "오류", "중심점을 선택해주세요", level=2)
            restore_ui_focus(self)
            return

        # Transform to DEM CRS for accurate distance calculations
        center_dem = self.transform_point(center, center_crs, dem_layer.crs())
        
        buffer_radius = self.spinMaxDistance.value()  # Use max distance as buffer radius
        interval = self.spinLineInterval.value()
        
        # Calculate number of points based on circumference and interval
        circumference = 2 * math.pi * buffer_radius
        num_points = max(8, int(circumference / interval))
        
        # Generate points around buffer perimeter
        perimeter_points = []
        for i in range(num_points):
            angle = (2 * math.pi * i) / num_points
            x = center_dem.x() + buffer_radius * math.cos(angle)
            y = center_dem.y() + buffer_radius * math.sin(angle)
            perimeter_points.append(QgsPointXY(x, y))
        
        # Run LOS from each perimeter point to center
        provider = dem_layer.dataProvider()
        
        # Consolidate perimeter points into a single ring styling
        # Instead of rays, we draw the perimeter itself, colored by visibility from center.
        
        layer = QgsVectorLayer("LineString?crs=" + dem_layer.crs().authid(),
                              f"가시권_링분석_{int(buffer_radius)}m", "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("status", QVariant.String),
            QgsField("score", QVariant.Double)
        ])
        layer.updateFields()
        
        # To get status for segments between points, we can use the status of the starting point
        # OR we can supersample. For now, point status -> segment status.
        
        # Let's perform the check for all points first
        point_status = []
        visible_count = 0
        
        for pt in perimeter_points:
            # Check LOS Center <-> Point
            # Simple check at 3 points along ray to be sure? No, just end-to-end for speed
            # Use original sampling logic for accuracy
             # Sample along line to center
            dx = center_dem.x() - pt.x()
            dy = center_dem.y() - pt.y()
            
            elev_p, ok_p = provider.sample(pt, 1)
            elev_c, ok_c = provider.sample(center_dem, 1)
            if not (ok_p and ok_c):
                point_status.append(False)
                continue

            try:
                elev_p = float(elev_p)
                elev_c = float(elev_c)
            except (TypeError, ValueError):
                point_status.append(False)
                continue

            if math.isnan(elev_p) or math.isnan(elev_c):
                point_status.append(False)
                continue

            p_h = elev_p + obs_height
            c_h = elev_c + tgt_height

            # Quick Check: 10 samples
            is_visible = True
            for k in range(1, 11):
                f = k / 10.0
                sx = pt.x() + f * dx
                sy = pt.y() + f * dy
                
                elev_s, ok_s = provider.sample(QgsPointXY(sx, sy), 1)
                if not ok_s:
                    continue
                try:
                    elev_s = float(elev_s)
                except (TypeError, ValueError):
                    continue
                if math.isnan(elev_s):
                    continue
                
                sight = p_h + f * (c_h - p_h)
                if elev_s > sight:
                    is_visible = False
                    break
            
            point_status.append(is_visible)
            if is_visible: visible_count += 1
            
        # Creates segments
        for i in range(len(perimeter_points)):
            p1 = perimeter_points[i]
            p2 = perimeter_points[(i+1) % len(perimeter_points)]
            
            status = point_status[i]

            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPolylineXY([p1, p2]))
            feat.setAttributes(["감시 가능" if status else "사각지대", 1 if status else 0])
            pr.addFeature(feat)
        
        layer.updateExtents()
        
        # Style: Cleaner lines for perimeter ring
        categories = [
            QgsRendererCategory("감시 가능", QgsLineSymbol.createSimple({
                'color': '0,200,0', 'width': '1.0', 'line_style': 'solid'
            }), "감시 가능 (Visible)"),
            QgsRendererCategory("사각지대", QgsLineSymbol.createSimple({
                'color': '255,0,0', 'width': '1.0', 'line_style': 'solid'
            }), "사각지대 (Hidden)")
        ]
        layer.setRenderer(QgsCategorizedSymbolRenderer("status", categories))
        QgsProject.instance().addMapLayers([layer])
        self.last_result_layer_id = layer.id()
        
        # Ensure label layer is on top
        self.update_layer_order()
        
        # Link center marker
        self.link_current_marker_to_layer(layer.id(), [(center, center_crs)])
        
        # Summary message
        visibility_pct = (visible_count / len(perimeter_points) * 100) if perimeter_points else 0
        self.iface.messageBar().pushMessage(
            "가시권 링 분석 (Visibility Ring Analysis)",
            f"중심점 감시율: {visibility_pct:.1f}% ({visible_count}/{len(perimeter_points)}개 지점에서 보임)",
            level=0
        )
        
        self.accept()
    
    def create_observer_layer(self, name, points_info):
        """Create a persistent memory layer for manual observer points"""
        crs = self.canvas.mapSettings().destinationCrs().authid()
        
        # Check if we have points or lines
        is_line = False
        if not self.radioFromLayer.isChecked() and hasattr(self, 'drawn_line_points') and self.radioLineViewshed.isChecked():
            is_line = True
            
        if is_line:
            layer = QgsVectorLayer(f"LineString?crs={crs}", name, "memory")
        else:
            layer = QgsVectorLayer(f"Point?crs={crs}", name, "memory")
            
        pr = layer.dataProvider()
        
        # Add fields
        pr.addAttributes([QgsField("no", QVariant.Int)])
        layer.updateFields()
        
        # Add features
        features = []
        if is_line:
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPolylineXY(self.drawn_line_points))
            feat.setAttributes([1])
            features.append(feat)
        else:
            for i, (pt, _) in enumerate(points_info):
                feat = QgsFeature(layer.fields())
                feat.setGeometry(QgsGeometry.fromPointXY(pt))
                feat.setAttributes([i + 1])
                features.append(feat)
        
        pr.addFeatures(features)
        
        # Style the layer
        if is_line:
            symbol = QgsLineSymbol.createSimple({'color': 'blue', 'width': '0.6'})
        else:
            # Create a red point marker
            symbol = QgsMarkerSymbol.createSimple({
                'name': 'circle',
                'color': 'red',
                'outline_color': 'white',
                'size': '3.0'
            })
            
            # Add labeling
            text_format = QgsTextFormat()
            text_format.setSize(10)
            text_format.setColor(QColor(255, 0, 0)) # Red text
            
            # Buffer around text for readability (Essential for topological maps)
            from qgis.core import QgsTextBufferSettings
            buffer_settings = QgsTextBufferSettings()
            buffer_settings.setEnabled(True)
            buffer_settings.setSize(1.2) # Slightly larger buffer
            buffer_settings.setColor(QColor(255, 255, 255, 230)) # Dense white buffer
            text_format.setBuffer(buffer_settings)
            
            label_settings = QgsPalLayerSettings()
            label_settings.setFormat(text_format)
            label_settings.fieldName = "no"
            label_settings.enabled = True
            
            # Placement: Around the point (more stable than OverPoint in some Python bindings)
            label_settings.placement = QgsPalLayerSettings.AroundPoint
            label_settings.dist = 1
            
            layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
            layer.setLabelsEnabled(True)
            
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        
        QgsProject.instance().addMapLayers([layer])
        return layer

    def run_single_viewshed(self, dem_layer, obs_height, tgt_height, max_dist, curvature, refraction, refraction_coeff=0.13):
        """Run single point viewshed analysis with circular masking"""
        points_info = self.get_context_point_and_crs()
        if not points_info:
            push_message(self.iface, "오류", "관측점을 선택해주세요", level=2)
            restore_ui_focus(self)
            return
            
        # [v1.5.97] Hide dialog only when processing starts
        self.hide()
        QtWidgets.QApplication.processEvents()

        point, src_crs = points_info[0] # Take first one for single viewshed
        
        # If manual selection, create persistent point layer
        if not self.radioFromLayer.isChecked():
            observer_layer_name = "가시권_관측점"
            if self.radioReverseViewshed.isChecked():
                observer_layer_name = "역방향_대상물"
            self.create_observer_layer(observer_layer_name, points_info)
        
        run_id = str(uuid.uuid4())[:12]
        raw_output = os.path.join(tempfile.gettempdir(), f'archt_vs_raw_{run_id}.tif')
        final_output = os.path.join(tempfile.gettempdir(), f'archt_vs_final_{run_id}.tif')
        
        # Transform point to DEM CRS
        point_dem = self.transform_point(point, src_crs, dem_layer.crs())

        extra = self._build_gdal_viewshed_extra(curvature, refraction, refraction_coeff)
        
        # Build params
        params = {
            'INPUT': dem_layer.source(),
            'BAND': 1,
            'OBSERVER': f"{point_dem.x()},{point_dem.y()}",
            'OBSERVER_HEIGHT': obs_height,
            'TARGET_HEIGHT': tgt_height,
            'MAX_DISTANCE': max_dist,
            'EXTRA': extra,
            'OUTPUT': raw_output
        }
        
        try:
            processing.run("gdal:viewshed", params)
            
            # Circular Masking: Clip raw output by a circular buffer
            if os.path.exists(raw_output):
                # Create a temporary memory layer for the circular mask
                mask_layer = QgsVectorLayer("Polygon?crs=" + dem_layer.crs().authid(), "temp_mask", "memory")
                pr = mask_layer.dataProvider()
                circle_feat = QgsFeature()
                # Create extremely detailed circle buffer for smooth edges
                circle_feat.setGeometry(QgsGeometry.fromPointXY(point_dem).buffer(max_dist, 128))
                pr.addFeatures([circle_feat])
                
                # Clip using universal algorithm
                # Force Float32 (6) and set NoData to -9999 to ensure absolute transparency
                processing.run("gdal:cliprasterbymasklayer", {
                    'INPUT': raw_output,
                    'MASK': mask_layer,
                    'NODATA': -9999,
                    'DATA_TYPE': 6, # Float32
                    'ALPHA_BAND': False,
                    'CROP_TO_CUTLINE': True,
                    'KEEP_RESOLUTION': True,
                    'OUTPUT': final_output
                })
                
                if not os.path.exists(final_output):
                    shutil.copy(raw_output, final_output)
            
            if os.path.exists(final_output):
                use_higuchi = self.chkHiguchi.isChecked()
                is_reverse = self.radioReverseViewshed.isChecked()

                raster_path = final_output
                if use_higuchi:
                    layer_name = f"가시권_히구치_{int(max_dist)}m"
                    higuchi_output = os.path.join(tempfile.gettempdir(), f'archt_vs_higuchi_{run_id}.tif')
                    self._create_higuchi_viewshed_raster(
                        final_output, higuchi_output, point, src_crs, dem_layer
                    )
                    raster_path = higuchi_output
                elif is_reverse:
                    layer_name = f"역방향_가시권_{int(max_dist)}m"
                else:
                    layer_name = f"가시권_단일점_{int(max_dist)}m"
                viewshed_layer = QgsRasterLayer(raster_path, layer_name)
                
                if viewshed_layer.isValid():
                    if use_higuchi:
                        self.apply_higuchi_style(viewshed_layer)
                    else:
                        self.apply_viewshed_style(viewshed_layer)
                    
                    QgsProject.instance().addMapLayers([viewshed_layer])
                    if use_higuchi:
                        # Add rings after raster so they draw on top.
                        self.create_higuchi_rings(point, src_crs, max_dist, dem_layer)
                    self.link_current_marker_to_layer(viewshed_layer.id(), [(point, src_crs)])
                    
                    # Ensure label layer is on top
                    self.update_layer_order()
                    cleanup_files([raw_output])
                    self.accept()
                else:
                    raise Exception("결과 레이어 로드 실패")
        except Exception as e:
            push_message(self.iface, "오류", f"분석 중 오류: {str(e)}", level=2)
            restore_ui_focus(self)
        finally:
            cleanup_files([raw_output])

    def _is_visual_imbalance_enabled(self):
        if hasattr(self, "chkHiguchi") and self.chkHiguchi.isChecked():
            return False
        return (
            hasattr(self, "chkVisualImbalance")
            and self.chkVisualImbalance.isVisible()
            and self.chkVisualImbalance.isEnabled()
            and self.chkVisualImbalance.isChecked()
            and self.radioReverseViewshed.isChecked()
        )

    def _compute_viewshed_raster_file(
        self,
        dem_layer,
        point,
        src_crs,
        obs_height,
        tgt_height,
        max_dist,
        curvature,
        refraction,
        refraction_coeff=0.13,
        prefix="vs",
    ):
        """Compute a binary viewshed raster (0/255) clipped to a circular radius.

        Returns:
            str: output GeoTIFF path
        """
        run_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        raw_output = os.path.join(tempfile.gettempdir(), f"archt_vs_raw_{run_id}.tif")
        final_output = os.path.join(tempfile.gettempdir(), f"archt_vs_final_{run_id}.tif")

        point_dem = self.transform_point(point, src_crs, dem_layer.crs())
        extra = self._build_gdal_viewshed_extra(curvature, refraction, refraction_coeff)

        params = {
            "INPUT": dem_layer.source(),
            "BAND": 1,
            "OBSERVER": f"{point_dem.x()},{point_dem.y()}",
            "OBSERVER_HEIGHT": float(obs_height),
            "TARGET_HEIGHT": float(tgt_height),
            "MAX_DISTANCE": float(max_dist),
            "EXTRA": extra,
            "OUTPUT": raw_output,
        }

        try:
            processing.run("gdal:viewshed", params)

            if os.path.exists(raw_output):
                mask_layer = QgsVectorLayer(
                    "Polygon?crs=" + dem_layer.crs().authid(),
                    "temp_mask",
                    "memory",
                )
                pr = mask_layer.dataProvider()
                circle_feat = QgsFeature()
                circle_feat.setGeometry(QgsGeometry.fromPointXY(point_dem).buffer(max_dist, 128))
                pr.addFeatures([circle_feat])

                processing.run(
                    "gdal:cliprasterbymasklayer",
                    {
                        "INPUT": raw_output,
                        "MASK": mask_layer,
                        "NODATA": -9999,
                        "DATA_TYPE": 6,  # Float32
                        "ALPHA_BAND": False,
                        "CROP_TO_CUTLINE": True,
                        "KEEP_RESOLUTION": True,
                        "OUTPUT": final_output,
                    },
                )

                if not os.path.exists(final_output):
                    shutil.copy(raw_output, final_output)

            if not os.path.exists(final_output):
                raise Exception("viewshed 결과 래스터 생성 실패")

            return final_output
        finally:
            cleanup_files([raw_output])

    def _create_visual_imbalance_raster(
        self,
        forward_raster_path,
        reverse_raster_path,
        output_raster_path,
        nodata_value=-9999,
    ):
        """Create a raster highlighting where forward/reverse visibility differs.

        Output values (Int16):
        - -9999: NoData
        - 0: same (both visible or both invisible) -> transparent in style
        - 1: forward-only (center can see, but cannot be seen)
        - 2: reverse-only (center is seen, but cannot see)
        """
        ds_f = None
        ds_r = None
        out_ds = None
        try:
            ds_f = gdal.Open(forward_raster_path, gdal.GA_ReadOnly)
            ds_r = gdal.Open(reverse_raster_path, gdal.GA_ReadOnly)
            if ds_f is None or ds_r is None:
                raise Exception("불균등 분석: 입력 래스터를 열 수 없습니다.")

            xsize = ds_r.RasterXSize
            ysize = ds_r.RasterYSize
            if ds_f.RasterXSize != xsize or ds_f.RasterYSize != ysize:
                raise Exception("불균등 분석: 두 래스터의 해상도/범위가 일치하지 않습니다.")

            gt = ds_r.GetGeoTransform()
            proj = ds_r.GetProjection()

            f_band = ds_f.GetRasterBand(1)
            r_band = ds_r.GetRasterBand(1)
            f_nodata = f_band.GetNoDataValue()
            r_nodata = r_band.GetNoDataValue()

            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(
                output_raster_path,
                xsize,
                ysize,
                1,
                gdal.GDT_Int16,
                options=["TILED=YES", "COMPRESS=LZW"],
            )
            if out_ds is None:
                raise Exception("불균등 분석: 출력 래스터 생성 실패")

            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj)
            out_band = out_ds.GetRasterBand(1)
            out_band.SetNoDataValue(int(nodata_value))

            block_x, block_y = f_band.GetBlockSize()
            if not block_x or not block_y:
                block_x, block_y = 512, 512

            for yoff in range(0, ysize, block_y):
                yblock = min(block_y, ysize - yoff)
                for xoff in range(0, xsize, block_x):
                    xblock = min(block_x, xsize - xoff)
                    f_arr = f_band.ReadAsArray(xoff, yoff, xblock, yblock)
                    r_arr = r_band.ReadAsArray(xoff, yoff, xblock, yblock)
                    if f_arr is None or r_arr is None:
                        continue

                    f_arr = f_arr.astype(np.float32, copy=False)
                    r_arr = r_arr.astype(np.float32, copy=False)

                    nodata_mask = np.zeros(f_arr.shape, dtype=bool)
                    if f_nodata is not None:
                        nodata_mask |= f_arr == f_nodata
                    if r_nodata is not None:
                        nodata_mask |= r_arr == r_nodata
                    nodata_mask |= f_arr == -9999
                    nodata_mask |= r_arr == -9999

                    f_vis = (~nodata_mask) & (f_arr > 0.5)
                    r_vis = (~nodata_mask) & (r_arr > 0.5)

                    out = np.zeros(f_arr.shape, dtype=np.int16)
                    out[f_vis & (~r_vis)] = 1
                    out[r_vis & (~f_vis)] = 2
                    out[nodata_mask] = int(nodata_value)

                    out_band.WriteArray(out, xoff, yoff)

            out_band.FlushCache()
            out_ds.FlushCache()
        except Exception:
            cleanup_files([output_raster_path])
            raise
        finally:
            out_ds = None
            ds_f = None
            ds_r = None
    
    # [v1.6.17] run_line_viewshed REMOVED - Line Viewshed now uses run_multi_viewshed

    def _ask_reverse_polygon_target_mode(self, allow_boundary=True):
        """Ask how to interpret polygon targets for reverse viewshed.

        Returns: "centroid", "boundary", or None (cancel)
        """
        from qgis.PyQt.QtWidgets import QMessageBox

        interval = 50
        if hasattr(self, "spinLineInterval"):
            try:
                interval = int(self.spinLineInterval.value())
            except Exception:
                interval = 50

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Question)
        msg.setWindowTitle("역방향 가시권: 폴리곤 처리")
        msg.setText("폴리곤(면) 대상물을 선택했습니다.\n어떤 기준으로 역방향 가시권을 계산할까요?")
        if allow_boundary:
            msg.setInformativeText(
                f"테두리 모드는 경계선을 약 {interval}m 간격으로 샘플링해 합집합(Union)으로 계산합니다."
            )
        else:
            msg.setInformativeText("히구치 거리대는 폴리곤 테두리(다중점) 모드에서 지원되지 않습니다.")

        btn_centroid = msg.addButton("중심점(빠름)", QMessageBox.AcceptRole)
        btn_boundary = None
        if allow_boundary:
            btn_boundary = msg.addButton("테두리(합집합)", QMessageBox.AcceptRole)
        btn_cancel = msg.addButton("취소", QMessageBox.RejectRole)
        msg.setDefaultButton(btn_centroid)

        msg.exec_()
        clicked = msg.clickedButton()
        if clicked == btn_centroid:
            return "centroid"
        if btn_boundary is not None and clicked == btn_boundary:
            return "boundary"
        if clicked == btn_cancel:
            return None
        return None

    def _get_sampling_max_points(self):
        max_points = 50
        if hasattr(self, "spinLineMaxPoints"):
            try:
                max_points = int(self.spinLineMaxPoints.value())
            except Exception:
                pass
        elif hasattr(self, "spinMaxPoints"):
            try:
                max_points = int(self.spinMaxPoints.value())
            except Exception:
                pass
        return max(1, max_points)

    def _sample_polygon_boundary_points(self, polygon_geom, interval):
        """Sample points along polygon exterior ring.

        Args:
            polygon_geom: QgsGeometry (Polygon/MultiPolygon), assumed to be in a metric CRS.
            interval: sampling distance in map units (meters).

        Returns:
            List[QgsPointXY]
        """
        points = []
        try:
            interval = float(interval)
        except Exception:
            interval = 50.0
        if interval <= 0:
            interval = 50.0

        if not polygon_geom or polygon_geom.isEmpty():
            return points

        if polygon_geom.isMultipart():
            polygons = polygon_geom.asMultiPolygon()
        else:
            polygons = [polygon_geom.asPolygon()]

        for poly in polygons:
            if not poly or len(poly) < 1 or not poly[0]:
                continue
            exterior_ring = poly[0]
            ring_geom = QgsGeometry.fromPolylineXY(exterior_ring)
            length = ring_geom.length()
            if length <= 0:
                continue

            num_pts = max(1, int(length / interval))
            for i in range(num_pts + 1):
                frac = i / num_pts if num_pts > 0 else 0
                pt_geom = ring_geom.interpolate(frac * length)
                if pt_geom and not pt_geom.isEmpty():
                    points.append(QgsPointXY(pt_geom.asPoint()))

        return points

    def _burn_nodata_for_geometries_in_raster(self, raster_path, geometries, nodata_value=-9999):
        """Burn NoData value into a raster where geometries cover (to 'cut out' areas)."""
        if not raster_path or not geometries:
            return

        ds = None
        mem_ds = None
        try:
            ds = gdal.Open(raster_path, gdal.GA_Update)
            if ds is None:
                raise Exception("출력 래스터를 열 수 없습니다.")

            band = ds.GetRasterBand(1)
            try:
                band.SetNoDataValue(float(nodata_value))
            except Exception:
                pass

            ogr_driver = ogr.GetDriverByName("Memory")
            if ogr_driver is None:
                raise Exception("OGR Memory 드라이버를 찾을 수 없습니다.")

            mem_ds = ogr_driver.CreateDataSource("mask")
            if mem_ds is None:
                raise Exception("메모리 벡터 데이터소스를 만들 수 없습니다.")

            mem_lyr = mem_ds.CreateLayer("mask", None, ogr.wkbUnknown)
            if mem_lyr is None:
                raise Exception("메모리 레이어를 만들 수 없습니다.")

            added = 0
            for geom in geometries:
                if not geom or geom.isEmpty():
                    continue
                try:
                    ogr_geom = ogr.CreateGeometryFromWkb(bytes(geom.asWkb()))
                except Exception:
                    ogr_geom = None
                if ogr_geom is None:
                    try:
                        ogr_geom = ogr.CreateGeometryFromWkt(geom.asWkt())
                    except Exception:
                        ogr_geom = None
                if ogr_geom is None:
                    continue

                feat = ogr.Feature(mem_lyr.GetLayerDefn())
                feat.SetGeometry(ogr_geom)
                mem_lyr.CreateFeature(feat)
                feat = None
                added += 1

            if added <= 0:
                return

            # Rasterize into the existing raster (burn NoData)
            err = gdal.RasterizeLayer(
                ds,
                [1],
                mem_lyr,
                burn_values=[float(nodata_value)],
                options=["ALL_TOUCHED=TRUE"],
            )
            if err != 0:
                raise Exception(f"RasterizeLayer failed (err={err})")

            try:
                band.FlushCache()
            except Exception:
                pass
            try:
                ds.FlushCache()
            except Exception:
                pass
        except Exception as e:
            log_message(f"Raster mask error: {e}", level=Qgis.Warning)
        finally:
            mem_ds = None
            ds = None

    def _run_union_viewshed_for_points(
        self,
        dem_layer,
        points,
        obs_height,
        tgt_height,
        max_dist,
        curvature,
        refraction,
        refraction_coeff,
        layer_name,
        marker_points_with_crs=None,
        mask_geometries_dem=None,
    ):
        """Run a union (binary) viewshed for multiple observer points.

        This is a simplified variant of multi-viewshed intended for reverse-viewshed polygon targets.
        """
        if not points:
            push_message(self.iface, "오류", "대상점이 최소 1개 이상 필요합니다", level=2)
            restore_ui_focus(self)
            return

        # Performance guard
        max_points = self._get_sampling_max_points()
        if len(points) > max_points:
            from qgis.PyQt.QtWidgets import QMessageBox

            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("대상점 개수 경고")
            msg.setText(
                f"전체 분석에 {len(points)}개의 대상점이 포함되어 있습니다.\n"
                f"성능을 위해 기본적으로 {max_points}개로 제한됩니다."
            )
            msg.setInformativeText(
                "고해상도 DEM과 많은 대상점은 수 분 이상 소요될 수 있습니다.\n\n"
                f"예(Yes): {max_points}개로 축소하여 안전하게 진행\n"
                f"아니오(No): 전체 {len(points)}개 분석 (매우 느림)\n"
                "취소(Cancel): 취소 및 설정으로 복귀"
            )
            msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            msg.setDefaultButton(QMessageBox.Yes)

            res_msg = msg.exec_()
            if res_msg == QMessageBox.Cancel:
                restore_ui_focus(self)
                return
            if res_msg == QMessageBox.Yes:
                step = max(1, len(points) // max_points)
                points = points[::step][:max_points]
                self.iface.messageBar().pushMessage("알림", f"대상점이 {len(points)}개로 샘플링되었습니다.", level=1)

        # Hide dialog only when processing starts
        self.hide()
        QtWidgets.QApplication.processEvents()

        extra = self._build_gdal_viewshed_extra(curvature, refraction, refraction_coeff)

        progress = QtWidgets.QProgressDialog("역방향 가시권 분석 실행 중...", "취소", 0, len(points), self)
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents()

        temp_outputs = []
        viewshed_results = []
        final_output = None
        try:
            # Smart analysis extent
            total_obs_ext = QgsRectangle()
            total_obs_ext.setMinimal()
            for pt, p_crs in points:
                pt_dem = self.transform_point(pt, p_crs, dem_layer.crs())
                total_obs_ext.combineExtentWith(pt_dem.x(), pt_dem.y())

            smart_ext = QgsRectangle(
                total_obs_ext.xMinimum() - max_dist * 1.2,
                total_obs_ext.yMinimum() - max_dist * 1.2,
                total_obs_ext.xMaximum() + max_dist * 1.2,
                total_obs_ext.yMaximum() + max_dist * 1.2,
            )
            final_ext = smart_ext.intersect(dem_layer.extent())
            if final_ext.isEmpty():
                final_ext = dem_layer.extent()

            # Unified grid snapping
            res = dem_layer.rasterUnitsPerPixelX()
            dem_ext = dem_layer.extent()

            snap_xmin = dem_ext.xMinimum() + math.floor((final_ext.xMinimum() - dem_ext.xMinimum()) / res) * res
            snap_ymax = dem_ext.yMaximum() - math.floor((dem_ext.yMaximum() - final_ext.yMaximum()) / res) * res
            snap_xmax = dem_ext.xMinimum() + math.ceil((final_ext.xMaximum() - dem_ext.xMinimum()) / res) * res
            snap_ymin = dem_ext.yMaximum() - math.ceil((dem_ext.yMaximum() - final_ext.yMinimum()) / res) * res

            target_rect = QgsRectangle(snap_xmin, snap_ymin, snap_xmax, snap_ymax)
            grid_info = {
                "xmin": snap_xmin,
                "ymax": snap_ymax,
                "xmax": snap_xmax,
                "ymin": snap_ymin,
                "res": res,
                "width": int(round((snap_xmax - snap_xmin) / res)),
                "height": int(round((snap_ymax - snap_ymin) / res)),
            }

            for i, (point, p_crs) in enumerate(points):
                if progress.wasCanceled():
                    break
                progress.setValue(i)
                QtWidgets.QApplication.processEvents()

                output_raw = os.path.join(tempfile.gettempdir(), f"archt_rvs_raw_{i}_{uuid.uuid4().hex[:8]}.tif")
                pt_dem = self.transform_point(point, p_crs, dem_layer.crs())
                try:
                    processing.run(
                        "gdal:viewshed",
                        {
                            "INPUT": dem_layer.source(),
                            "BAND": 1,
                            "OBSERVER": f"{pt_dem.x()},{pt_dem.y()}",
                            "OBSERVER_HEIGHT": obs_height,
                            "TARGET_HEIGHT": tgt_height,
                            "MAX_DISTANCE": max_dist,
                            "EXTRA": extra,
                            "OUTPUT": output_raw,
                        },
                    )
                except Exception as e:
                    log_message(f"reverse viewshed failed for point #{i}: {e}", level=Qgis.Warning)
                    continue

                if not os.path.exists(output_raw):
                    continue

                temp_outputs.append(output_raw)
                full_vs = os.path.join(tempfile.gettempdir(), f"archt_rvs_full_{i}_{uuid.uuid4().hex[:8]}.tif")
                try:
                    processing.run(
                        "gdal:warpreproject",
                        {
                            "INPUT": output_raw,
                            "TARGET_EXTENT": target_rect,
                            "TARGET_EXTENT_CRS": dem_layer.crs().authid(),
                            "NODATA": -9999,
                            "TARGET_RESOLUTION": res,
                            "RESAMPLING": 0,
                            "DATA_TYPE": 5,
                            "OUTPUT": full_vs,
                        },
                    )
                    if os.path.exists(full_vs):
                        temp_outputs.append(full_vs)
                        viewshed_results.append((i, full_vs))
                        try:
                            os.remove(output_raw)
                        except Exception:
                            pass
                except Exception as e:
                    log_message(f"warpreproject failed for reverse viewshed #{i}: {e}", level=Qgis.Warning)

            progress.setValue(len(points))

            if progress.wasCanceled():
                push_message(self.iface, "취소", "역방향 가시권 분석이 취소되었습니다.", level=1)
                restore_ui_focus(self)
                return

            if not viewshed_results:
                raise Exception("유효한 역방향 가시권 결과를 생성하지 못했습니다.")

            progress.setLabelText("결과 통합 중 (Union)...")
            QtWidgets.QApplication.processEvents()

            final_output = os.path.join(
                tempfile.gettempdir(),
                f"archtoolkit_reverse_viewshed_union_{uuid.uuid4().hex[:8]}.tif",
            )

            success = self.combine_viewsheds_numpy(
                dem_layer=dem_layer,
                viewshed_files=viewshed_results,
                output_path=final_output,
                observer_points=points,
                max_dist=max_dist,
                is_count_mode=False,
                grid_info=grid_info,
                union_mode=True,
            )
            if not success or not os.path.exists(final_output):
                raise Exception("역방향 가시권 결과 생성 실패 (Union)")

            # Optional: Cut out polygon interior (NoData) so "outside visibility" is emphasized.
            if mask_geometries_dem:
                self._burn_nodata_for_geometries_in_raster(final_output, mask_geometries_dem, nodata_value=-9999)

            viewshed_layer = QgsRasterLayer(final_output, layer_name)
            if not viewshed_layer.isValid():
                raise Exception("결과 레이어 로드 실패")

            self.apply_viewshed_style(viewshed_layer)
            QgsProject.instance().addMapLayer(viewshed_layer)
            self.last_result_layer_id = viewshed_layer.id()

            # Link marker(s) for cleanup when the raster is removed
            if marker_points_with_crs:
                self.link_current_marker_to_layer(viewshed_layer.id(), marker_points_with_crs)
            else:
                self.link_current_marker_to_layer(viewshed_layer.id(), points[:1])

            self.update_layer_order()
            self.iface.messageBar().pushMessage(
                "완료",
                f"역방향 가시권 분석 완료 ({len(points)}개 대상점, Union)",
                level=0,
            )
            self.accept()
        except Exception as e:
            push_message(self.iface, "오류", f"역방향 가시권 처리 중 오류: {str(e)}", level=2)
            restore_ui_focus(self)
        finally:
            try:
                progress.close()
            except Exception:
                pass
            cleanup_files(temp_outputs)

    def run_reverse_viewshed_with_visual_imbalance(
        self,
        dem_layer,
        obs_height,
        tgt_height,
        max_dist,
        curvature,
        refraction,
        refraction_coeff=0.13,
    ):
        """Reverse viewshed + visual imbalance (forward vs reverse mismatch) result."""
        points_info = self.get_context_point_and_crs()
        if not points_info:
            push_message(self.iface, "오류", "대상물 위치를 선택해주세요.", level=2)
            restore_ui_focus(self)
            return

        if hasattr(self, "chkHiguchi") and self.chkHiguchi.isChecked():
            push_message(
                self.iface,
                "안내",
                "시각적 불균등 분석은 히구치 거리대 모드에서 지원되지 않습니다.",
                level=1,
            )
            self.run_single_viewshed(
                dem_layer,
                tgt_height,  # target becomes observer
                obs_height,  # observer height becomes target
                max_dist,
                curvature,
                refraction,
                refraction_coeff,
            )
            return

        # [v1.5.97] Hide dialog only when processing starts
        self.hide()
        QtWidgets.QApplication.processEvents()

        point, src_crs = points_info[0]

        # If manual selection, create persistent point layer
        if not self.radioFromLayer.isChecked():
            self.create_observer_layer("역방향_대상물", points_info)

        forward_raster = None
        try:
            self.iface.messageBar().pushMessage("처리 중", "시각적 불균등: 1/3 (정방향 가시권)", level=0)
            forward_raster = self._compute_viewshed_raster_file(
                dem_layer=dem_layer,
                point=point,
                src_crs=src_crs,
                obs_height=obs_height,
                tgt_height=0.0,
                max_dist=max_dist,
                curvature=curvature,
                refraction=refraction,
                refraction_coeff=refraction_coeff,
                prefix="fwd",
            )

            self.iface.messageBar().pushMessage("처리 중", "시각적 불균등: 2/3 (역방향 가시권)", level=0)
            reverse_raster = self._compute_viewshed_raster_file(
                dem_layer=dem_layer,
                point=point,
                src_crs=src_crs,
                obs_height=tgt_height,  # target becomes observer
                tgt_height=obs_height,  # observer height becomes target
                max_dist=max_dist,
                curvature=curvature,
                refraction=refraction,
                refraction_coeff=refraction_coeff,
                prefix="rev",
            )

            imbalance_raster = os.path.join(
                tempfile.gettempdir(),
                f"archt_rvs_imbalance_{uuid.uuid4().hex[:8]}.tif",
            )
            self.iface.messageBar().pushMessage("처리 중", "시각적 불균등: 3/3 (불균등 분류)", level=0)
            self._create_visual_imbalance_raster(forward_raster, reverse_raster, imbalance_raster)

            reverse_layer_name = f"역방향_가시권_{int(max_dist)}m"
            imbalance_layer_name = f"역방향_불균등_{int(max_dist)}m"

            reverse_layer = QgsRasterLayer(reverse_raster, reverse_layer_name)
            if not reverse_layer.isValid():
                raise Exception("역방향 결과 레이어 로드 실패")
            self.apply_viewshed_style(reverse_layer)
            QgsProject.instance().addMapLayer(reverse_layer)
            self.last_result_layer_id = reverse_layer.id()

            imbalance_layer = QgsRasterLayer(imbalance_raster, imbalance_layer_name)
            if not imbalance_layer.isValid():
                raise Exception("불균등 결과 레이어 로드 실패")
            self.apply_visual_imbalance_style(imbalance_layer)
            QgsProject.instance().addMapLayer(imbalance_layer)

            # Draw a radius ring so the analysis boundary is visible even when "동일" areas are transparent.
            try:
                ring_layer = self.create_analysis_radius_ring(
                    point,
                    src_crs,
                    max_dist,
                    dem_layer,
                    layer_name=f"역방향_반경_{int(max_dist)}m",
                )
                if ring_layer is not None:
                    self.result_aux_layer_map.setdefault(imbalance_layer.id(), []).append(ring_layer.id())
            except Exception:
                pass

            self.link_current_marker_to_layer(reverse_layer.id(), [(point, src_crs)])
            self.update_layer_order()

            self.iface.messageBar().pushMessage("완료", "시각적 불균등 분석 완료", level=0)
            self.accept()
        except Exception as e:
            push_message(self.iface, "오류", f"시각적 불균등 처리 중 오류: {str(e)}", level=2)
            restore_ui_focus(self)
        finally:
            cleanup_files([forward_raster] if forward_raster else [])

    def run_reverse_viewshed(self, dem_layer, obs_height, tgt_height, max_dist, curvature, refraction, refraction_coeff=0.13):
        """Run reverse viewshed - from where can the target be seen?
        
        This swaps observer and target heights to answer:
        "From where can a structure of height X be seen?"
        """
        # Polygon target (map click)
        if not self.radioFromLayer.isChecked() and self._reverse_target_geom is not None:
            src_crs = self._reverse_target_crs or self.canvas.mapSettings().destinationCrs()
            use_higuchi = hasattr(self, "chkHiguchi") and self.chkHiguchi.isChecked()
            mode = self._ask_reverse_polygon_target_mode(allow_boundary=not use_higuchi)
            if mode is None:
                restore_ui_focus(self)
                return

            if mode == "centroid":
                if self._is_visual_imbalance_enabled():
                    self.run_reverse_viewshed_with_visual_imbalance(
                        dem_layer,
                        obs_height,
                        tgt_height,
                        max_dist,
                        curvature,
                        refraction,
                        refraction_coeff,
                    )
                else:
                    # Existing single-point pipeline (supports Higuchi).
                    self.run_single_viewshed(
                        dem_layer,
                        tgt_height,  # Target becomes observer
                        obs_height,  # Observer height becomes target
                        max_dist,
                        curvature,
                        refraction,
                        refraction_coeff,
                    )
                return

            # Boundary mode (Union)
            interval = self.spinLineInterval.value() if hasattr(self, "spinLineInterval") else 50
            try:
                transform = QgsCoordinateTransform(src_crs, dem_layer.crs(), QgsProject.instance())
                geom_dem = QgsGeometry(self._reverse_target_geom)
                geom_dem.transform(transform)
            except Exception:
                geom_dem = QgsGeometry(self._reverse_target_geom)

            sampled = self._sample_polygon_boundary_points(geom_dem, interval)
            pts = [(pt, dem_layer.crs()) for pt in sampled]
            if not pts:
                push_message(self.iface, "오류", "폴리곤 테두리에서 샘플링할 점을 생성할 수 없습니다.", level=2)
                restore_ui_focus(self)
                return

            marker = []
            try:
                centroid_src = self._reverse_target_geom.centroid().asPoint()
                marker = [(centroid_src, src_crs)]
            except Exception:
                pass

            self._run_union_viewshed_for_points(
                dem_layer=dem_layer,
                points=pts,
                obs_height=tgt_height,
                tgt_height=obs_height,
                max_dist=max_dist,
                curvature=curvature,
                refraction=refraction,
                refraction_coeff=refraction_coeff,
                layer_name=f"역방향_가시권_테두리_{int(max_dist)}m",
                marker_points_with_crs=marker,
                mask_geometries_dem=[geom_dem],
            )
            return

        # Polygon target (layer selection)
        if self.radioFromLayer.isChecked():
            obs_layer = self.cmbObserverLayer.currentLayer()
            if obs_layer and obs_layer.isValid() and obs_layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                selected = obs_layer.selectedFeatures()
                features = selected if selected else []
                if not features:
                    first_feat = next(obs_layer.getFeatures(), None)
                    if first_feat:
                        features = [first_feat]

                geoms = []
                for feat in features:
                    geom = feat.geometry() if feat else None
                    if geom and not geom.isEmpty():
                        geoms.append(geom)

                if geoms:
                    use_higuchi = hasattr(self, "chkHiguchi") and self.chkHiguchi.isChecked()
                    mode = self._ask_reverse_polygon_target_mode(allow_boundary=not use_higuchi)
                    if mode is None:
                        restore_ui_focus(self)
                        return

                    if mode == "centroid":
                        if len(geoms) == 1:
                            if self._is_visual_imbalance_enabled():
                                self.run_reverse_viewshed_with_visual_imbalance(
                                    dem_layer,
                                    obs_height,
                                    tgt_height,
                                    max_dist,
                                    curvature,
                                    refraction,
                                    refraction_coeff,
                                )
                            else:
                                self.run_single_viewshed(
                                    dem_layer,
                                    tgt_height,
                                    obs_height,
                                    max_dist,
                                    curvature,
                                    refraction,
                                    refraction_coeff,
                                )
                            return

                        pts = []
                        for g in geoms:
                            try:
                                pts.append((g.centroid().asPoint(), obs_layer.crs()))
                            except Exception:
                                continue
                        if not pts:
                            push_message(self.iface, "오류", "폴리곤 중심점을 계산할 수 없습니다.", level=2)
                            restore_ui_focus(self)
                            return

                        self._run_union_viewshed_for_points(
                            dem_layer=dem_layer,
                            points=pts,
                            obs_height=tgt_height,
                            tgt_height=obs_height,
                            max_dist=max_dist,
                            curvature=curvature,
                            refraction=refraction,
                            refraction_coeff=refraction_coeff,
                            layer_name=f"역방향_가시권_{int(max_dist)}m",
                            marker_points_with_crs=pts[:10],
                        )
                        return

                    # Boundary mode (Union)
                    interval = self.spinLineInterval.value() if hasattr(self, "spinLineInterval") else 50
                    pts = []
                    mask_geoms_dem = []
                    try:
                        transform = QgsCoordinateTransform(obs_layer.crs(), dem_layer.crs(), QgsProject.instance())
                    except Exception:
                        transform = None

                    for g in geoms:
                        g_dem = QgsGeometry(g)
                        if transform is not None:
                            try:
                                g_dem.transform(transform)
                            except Exception:
                                pass
                        mask_geoms_dem.append(g_dem)
                        for pt in self._sample_polygon_boundary_points(g_dem, interval):
                            pts.append((pt, dem_layer.crs()))

                    if not pts:
                        push_message(self.iface, "오류", "폴리곤 테두리에서 샘플링할 점을 생성할 수 없습니다.", level=2)
                        restore_ui_focus(self)
                        return

                    marker = []
                    try:
                        marker = [(geoms[0].centroid().asPoint(), obs_layer.crs())]
                    except Exception:
                        pass

                    self._run_union_viewshed_for_points(
                        dem_layer=dem_layer,
                        points=pts,
                        obs_height=tgt_height,
                        tgt_height=obs_height,
                        max_dist=max_dist,
                        curvature=curvature,
                        refraction=refraction,
                        refraction_coeff=refraction_coeff,
                        layer_name=f"역방향_가시권_테두리_{int(max_dist)}m",
                        marker_points_with_crs=marker,
                        mask_geometries_dem=mask_geoms_dem,
                    )
                    return

        # Fallback: regular reverse viewshed (single point)
        if self._is_visual_imbalance_enabled():
            self.run_reverse_viewshed_with_visual_imbalance(
                dem_layer,
                obs_height,
                tgt_height,
                max_dist,
                curvature,
                refraction,
                refraction_coeff,
            )
        else:
            self.run_single_viewshed(
                dem_layer,
                tgt_height,  # Target becomes observer
                obs_height,  # Observer height becomes target
                max_dist,
                curvature,
                refraction,
                refraction_coeff,
            )
    
    def run_line_of_sight(self, dem_layer, obs_height, tgt_height):
        """Run Line of Sight analysis between observer and target points
        
        Samples terrain along line, computes sight line, and detects obstructions.
        Creates visual output showing visible vs obstructed segments.
        """
        observer = self.observer_point
        target = self.target_point
        
        if not observer or not target:
            self.iface.messageBar().pushMessage("오류", "관측점과 대상점을 클릭하여 선택해주세요", level=2)
            self.show()
            return

        # Transform points to DEM CRS for sampling and output layers
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        observer_dem = self.transform_point(observer, canvas_crs, dem_layer.crs())
        target_dem = self.transform_point(target, canvas_crs, dem_layer.crs())

        # Calculate distance in DEM units (meters expected)
        dx = target_dem.x() - observer_dem.x()
        dy = target_dem.y() - observer_dem.y()
        total_dist = math.hypot(dx, dy)

        if total_dist <= 0:
            push_message(self.iface, "오류", "관측점과 대상점이 동일합니다.", level=2)
            restore_ui_focus(self)
            return

        if not self.radioFromLayer.isChecked() and total_dist > 1000:
            from qgis.PyQt.QtWidgets import QMessageBox

            res = QMessageBox.warning(
                self,
                "경고",
                f"가시선 길이({total_dist:.0f}m)가 기본 최대 분석 반경(1000m)을 초과합니다.\n계속 진행할까요?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if res == QMessageBox.No:
                restore_ui_focus(self)
                return
        
        # Sample terrain along line
        pixel_x = abs(dem_layer.rasterUnitsPerPixelX())
        pixel_y = abs(dem_layer.rasterUnitsPerPixelY())
        pixel_sizes = [v for v in (pixel_x, pixel_y) if v and v > 0]
        min_pixel = min(pixel_sizes) if pixel_sizes else 5.0
        desired_step = max(min_pixel, 5.0)

        num_samples = int(total_dist / desired_step) if desired_step > 0 else 200
        num_samples = max(200, min(num_samples, 5000))

        profile_data = []
        
        provider = dem_layer.dataProvider()
        
        for i in range(num_samples + 1):
            frac = i / num_samples
            x = observer_dem.x() + frac * dx
            y = observer_dem.y() + frac * dy
            dist = frac * total_dist
            
            # Sample elevation from DEM
            elev, ok = provider.sample(QgsPointXY(x, y), 1)
            if not ok:
                continue
            try:
                elev_value = float(elev)
            except (TypeError, ValueError):
                continue
            if math.isnan(elev_value):
                continue
            profile_data.append({
                'distance': dist,
                'elevation': elev_value,
                'x': x,
                'y': y
            })
        
        if len(profile_data) < 2:
            push_message(self.iface, "오류", "지형 데이터를 샘플링할 수 없습니다", level=2)
            restore_ui_focus(self)
            return
        
        # Observer and target elevations (with height added)
        obs_elev = profile_data[0]['elevation'] + obs_height
        tgt_elev = profile_data[-1]['elevation'] + tgt_height

        # Determine obstruction against the LOS line to the TARGET height (target visibility)
        first_obstruction = None
        is_visible_overall = True
        prev_pt = profile_data[0]
        prev_delta = prev_pt['elevation'] - obs_elev

        for pt in profile_data[1:-1]:
            frac = pt['distance'] / total_dist
            sight = obs_elev + frac * (tgt_elev - obs_elev)
            delta = pt['elevation'] - sight

            if delta > 0:
                is_visible_overall = False
                if prev_delta <= 0:
                    denom = (prev_delta - delta)
                    t = (prev_delta / denom) if denom != 0 else 0.0
                    t = max(0.0, min(1.0, t))
                    first_obstruction = {
                        'distance': prev_pt['distance'] + t * (pt['distance'] - prev_pt['distance']),
                        'elevation': prev_pt['elevation'] + t * (pt['elevation'] - prev_pt['elevation']),
                        'x': prev_pt['x'] + t * (pt['x'] - prev_pt['x']),
                        'y': prev_pt['y'] + t * (pt['y'] - prev_pt['y']),
                    }
                else:
                    first_obstruction = pt
                break

            prev_pt = pt
            prev_delta = delta

        # Create result layer (Viscode-style segmented line)
        layer = QgsVectorLayer(
            "LineString?crs=" + dem_layer.crs().authid(),
            f"가시선_Viscode_{int(total_dist)}m",
            "memory",
        )
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("status", QVariant.String),  # "보임" / "안보임"
            QgsField("from_m", QVariant.Double),
            QgsField("to_m", QVariant.Double),
            QgsField("length_m", QVariant.Double),
        ])
        layer.updateFields()

        # Build merged segments matching the profile visibility coloring (max-angle algorithm)
        terrain_visibility = [True]  # Observer point is always "visible"
        max_angle = -float("inf")
        start_elev = obs_elev

        for pt in profile_data[1:]:
            d = float(pt["distance"])
            if d <= 0:
                terrain_visibility.append(True)
                continue

            angle = (float(pt["elevation"]) - start_elev) / d
            if angle >= max_angle:
                max_angle = angle
                terrain_visibility.append(True)
            else:
                terrain_visibility.append(False)

        segments = []
        if len(profile_data) >= 2:
            current_status = "보임" if terrain_visibility[1] else "안보임"
            seg_from = 0.0
            current_pts = [QgsPointXY(profile_data[0]["x"], profile_data[0]["y"])]

            for idx in range(1, len(profile_data)):
                status = "보임" if terrain_visibility[idx] else "안보임"
                if status != current_status:
                    seg_to = float(profile_data[idx - 1]["distance"])
                    segments.append((current_status, seg_from, seg_to, current_pts))
                    current_pts = [current_pts[-1]]
                    seg_from = seg_to
                    current_status = status

                current_pts.append(QgsPointXY(profile_data[idx]["x"], profile_data[idx]["y"]))

            seg_to = float(profile_data[-1]["distance"])
            segments.append((current_status, seg_from, seg_to, current_pts))

        # Add features for each segment
        for status, from_m, to_m, pts in segments:
            if len(pts) < 2:
                continue
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPolylineXY(pts))
            length_m = max(0.0, float(to_m) - float(from_m))
            feat.setAttributes([status, float(from_m), float(to_m), length_m])
            pr.addFeature(feat)
            
        layer.updateExtents()
        
        # Style: Thin lines for visibility (Green/Red)
        categories = [
            QgsRendererCategory("보임", QgsLineSymbol.createSimple({
                'color': '0,200,0', 'width': '0.8'
            }), "보임"),
            QgsRendererCategory("안보임", QgsLineSymbol.createSimple({
                'color': '255,0,0', 'width': '0.8'
            }), "안보임")
        ]
        layer.setRenderer(QgsCategorizedSymbolRenderer("status", categories))
        
        # Create observer/target point layers (reference-style legend)
        observer_layer = QgsVectorLayer(
            "Point?crs=" + dem_layer.crs().authid(),
            f"가시선_Observers_{int(total_dist)}m",
            "memory",
        )
        observer_pr = observer_layer.dataProvider()
        observer_pr.addAttributes([QgsField("status", QVariant.String)])
        observer_layer.updateFields()

        observer_status = "보이는 대상 있음" if is_visible_overall else "보이는 대상 없음"
        observer_feat = QgsFeature(observer_layer.fields())
        observer_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(observer_dem.x(), observer_dem.y())))
        observer_feat.setAttributes([observer_status])
        observer_pr.addFeature(observer_feat)
        observer_layer.updateExtents()

        observer_categories = [
            QgsRendererCategory("보이는 대상 있음", QgsMarkerSymbol.createSimple({
                'name': 'triangle',
                'color': '0,200,0',
                'outline_color': '255,255,255',
                'size': '3.2',
            }), "보이는 대상 있음"),
            QgsRendererCategory("보이는 대상 없음", QgsMarkerSymbol.createSimple({
                'name': 'triangle',
                'color': '255,0,0',
                'outline_color': '255,255,255',
                'size': '3.2',
            }), "보이는 대상 없음"),
        ]
        observer_layer.setRenderer(QgsCategorizedSymbolRenderer("status", observer_categories))

        target_layer = QgsVectorLayer(
            "Point?crs=" + dem_layer.crs().authid(),
            f"가시선_Targets_{int(total_dist)}m",
            "memory",
        )
        target_pr = target_layer.dataProvider()
        target_pr.addAttributes([QgsField("status", QVariant.String)])
        target_layer.updateFields()

        target_status = "보임" if is_visible_overall else "안보임"
        target_feat = QgsFeature(target_layer.fields())
        target_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(target_dem.x(), target_dem.y())))
        target_feat.setAttributes([target_status])
        target_pr.addFeature(target_feat)
        target_layer.updateExtents()

        target_categories = [
            QgsRendererCategory("보임", QgsMarkerSymbol.createSimple({
                'name': 'circle',
                'color': '0,200,0',
                'outline_color': '255,255,255',
                'size': '3.2',
            }), "보임"),
            QgsRendererCategory("안보임", QgsMarkerSymbol.createSimple({
                'name': 'circle',
                'color': '255,0,0',
                'outline_color': '255,255,255',
                'size': '3.2',
            }), "안보임"),
        ]
        target_layer.setRenderer(QgsCategorizedSymbolRenderer("status", target_categories))
        
        obs_layer = None

        # If obstructed, mark the first obstacle
        if first_obstruction:
            obs_layer = QgsVectorLayer("Point?crs=" + dem_layer.crs().authid(),
                                       "첫번째_장애물", "memory")
            obs_pr = obs_layer.dataProvider()
            obs_pr.addAttributes([
                QgsField("distance", QVariant.Double),
                QgsField("elevation", QVariant.Double)
            ])
            obs_layer.updateFields()
            
            obs_feat = QgsFeature(obs_layer.fields())
            obs_feat.setGeometry(QgsGeometry.fromPointXY(
                QgsPointXY(first_obstruction['x'], first_obstruction['y'])
            ))
            obs_feat.setAttributes([
                first_obstruction['distance'],
                first_obstruction['elevation']
            ])
            obs_pr.addFeature(obs_feat)
            obs_layer.updateExtents()
            
            marker_symbol = QgsMarkerSymbol.createSimple({
                'name': 'circle',
                'color': '255,0,0',
                'size': '4'
            })
            obs_layer.setRenderer(QgsSingleSymbolRenderer(marker_symbol))
        
        # Add result layers under a group (to reduce clutter)
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        los_root_group_name = "ArchToolkit - 가시선"

        insert_index = 0
        try:
            label_layers = project.mapLayersByName("관측점_번호_라벨")
            if label_layers:
                label_node = root.findLayer(label_layers[0].id())
                if label_node and label_node.parent() == root:
                    insert_index = 1  # keep labels on top
        except Exception:
            pass

        parent_group = root.findGroup(los_root_group_name)
        if parent_group is None:
            parent_group = root.insertGroup(insert_index, los_root_group_name)
        else:
            try:
                current_index = root.children().index(parent_group)
                if current_index != insert_index:
                    is_visible = parent_group.isVisible()
                    is_expanded = parent_group.isExpanded()
                    clone = parent_group.clone()
                    clone.setItemVisibilityChecked(is_visible)
                    clone.setExpanded(is_expanded)
                    root.insertChildNode(insert_index, clone)
                    root.removeChildNode(parent_group)
                    parent_group = clone
            except Exception:
                pass

        run_id = str(uuid.uuid4())[:8]
        group_name = f"가시선_{int(total_dist)}m_{run_id}"
        run_group = parent_group.insertGroup(0, group_name)
        run_group.setExpanded(False)

        layers_to_add = [observer_layer, target_layer, layer]
        if obs_layer:
            layers_to_add.append(obs_layer)

        for lyr in layers_to_add:
            project.addMapLayer(lyr, False)
            run_group.addLayer(lyr)

        self.last_result_layer_id = layer.id()

        # Store profile payload for later reopening (selecting the line can reopen the profile)
        self._los_profile_data[layer.id()] = {
            "profile_data": profile_data,
            "obs_height": obs_height,
            "tgt_height": tgt_height,
            "total_dist": total_dist,
            "is_visible_overall": is_visible_overall,
            "first_obstruction": first_obstruction,
            "line_start_canvas": observer,
            "line_end_canvas": target,
        }

        try:
            handler = lambda *_args, lid=layer.id(): self._on_los_layer_selection_changed(lid)
            self._los_selection_handlers[layer.id()] = handler
            layer.selectionChanged.connect(handler)
        except Exception:
            pass

        # Ensure label layer is on top (if present from other analyses)
        self.update_layer_order()
        
        # Show result message
        if is_visible_overall:
            self.iface.messageBar().pushMessage(
                "가시선 분석", 
                f"직시 가능 (보임) | 거리: {total_dist:.0f}m",
                level=0
            )
        else:
            if first_obstruction:
                self.iface.messageBar().pushMessage(
                    "가시선 분석", 
                    f"직시 불가 (안보임) | 장애물: {first_obstruction['distance']:.0f}m (고도 {first_obstruction['elevation']:.1f}m)",
                    level=1
                )
            else:
                self.iface.messageBar().pushMessage(
                    "가시선 분석", 
                    "직시 불가 (안보임)",
                    level=1
                )
        
        # Open Profiler for visualization
        self.show_profiler(
            profile_data,
            obs_height,
            tgt_height,
            total_dist,
            is_visible_overall,
            first_obstruction,
            line_start_canvas=observer,
            line_end_canvas=target,
            result_layer_id=layer.id(),
        )
        
        self.accept()
        
    def _on_los_layer_selection_changed(self, layer_id):
        try:
            layer = QgsProject.instance().mapLayer(layer_id)
            if not layer or layer.selectedFeatureCount() <= 0:
                return
            self.open_los_profile(layer_id)
        except Exception as e:
            log_message(f"LOS selection handler error: {e}", level=Qgis.Warning)

    def open_los_profile(self, layer_id):
        payload = self._los_profile_data.get(layer_id)
        if not payload:
            return
        self.show_profiler(
            payload.get("profile_data") or [],
            payload.get("obs_height", 0.0),
            payload.get("tgt_height", 0.0),
            payload.get("total_dist", 0.0),
            payload.get("is_visible_overall", True),
            payload.get("first_obstruction"),
            line_start_canvas=payload.get("line_start_canvas"),
            line_end_canvas=payload.get("line_end_canvas"),
            result_layer_id=layer_id,
        )

    def show_profiler(
        self,
        profile_data,
        obs_height,
        tgt_height,
        total_dist,
        is_visible_overall=True,
        first_obstruction=None,
        line_start_canvas=None,
        line_end_canvas=None,
        result_layer_id=None,
    ):
        """Open the 2D Profiler dialog (modeless)"""
        try:
            from qgis.PyQt.QtCore import Qt

            if result_layer_id and result_layer_id in self._los_profile_dialogs:
                dlg = self._los_profile_dialogs.get(result_layer_id)
                if dlg:
                    try:
                        dlg.show()
                        dlg.raise_()
                        dlg.activateWindow()
                        return
                    except Exception:
                        self._los_profile_dialogs.pop(result_layer_id, None)

            profiler = ViewshedProfilerDialog(
                self.iface,
                profile_data,
                obs_height,
                tgt_height,
                total_dist,
                is_visible_overall=is_visible_overall,
                first_obstruction=first_obstruction,
                line_start_canvas=line_start_canvas,
                line_end_canvas=line_end_canvas,
                parent=self.iface.mainWindow(),
            )
            profiler.setWindowModality(Qt.NonModal)
            profiler.setAttribute(Qt.WA_DeleteOnClose, True)
            if result_layer_id:
                self._los_profile_dialogs[result_layer_id] = profiler
                profiler.destroyed.connect(
                    lambda *_args, lid=result_layer_id: self._los_profile_dialogs.pop(lid, None)
                )
            profiler.show()
            profiler.raise_()
            profiler.activateWindow()
        except Exception as e:
            log_message(f"Profiler error: {e}", level=Qgis.Warning)
    
    def combine_viewsheds_numpy(self, dem_layer, viewshed_files, output_path, observer_points, max_dist, is_count_mode, grid_info, union_mode=False):
        """Highly optimized cumulative viewshed merging with unified grid alignment.
        """
        try:
            # 1. Get base parameters from grid_info
            target_xmin = grid_info['xmin']
            target_ymax = grid_info['ymax']
            target_width = grid_info['width']
            target_height = grid_info['height']
            dem_xres = grid_info['res']
            dem_yres = grid_info['res']
            
            dem_ds = gdal.Open(dem_layer.source(), gdal.GA_ReadOnly)
            dem_proj = dem_ds.GetProjection()
            dem_ds = None
            
            # 2. Initialize Arrays
            cumulative = np.zeros((target_height, target_width), dtype=np.float32)
            circular_mask = np.zeros((target_height, target_width), dtype=np.bool_)
            
            # Universal meshgrid for clipping
            r_full, c_full = np.ogrid[:target_height, :target_width]
            
            # 3. Process each viewshed
            for pt_idx, vs_file in viewshed_files:
                if not os.path.exists(vs_file): continue
                vs_ds = gdal.Open(vs_file, gdal.GA_ReadOnly)
                if not vs_ds: continue
                
                vs_band = vs_ds.GetRasterBand(1)
                vs_nodata = vs_band.GetNoDataValue()
                vs_data = vs_band.ReadAsArray().astype(np.float32)
                
                # [v1.6.12] Simplified Merging (Aligning is already handled by gdal:warpreproject)
                v_h, v_w = vs_data.shape
                h_overlap = min(target_height, v_h)
                w_overlap = min(target_width, v_w)
                
                # Define val_to_add for cumulative mode
                if not union_mode:
                    val_to_add = 1 if is_count_mode else (2 ** min(pt_idx, 30))
                
                # [v1.6.14] Always calculate circular_mask for buffer-shape boundary
                pt, pt_crs = observer_points[pt_idx]
                pt_dem = self.transform_point(pt, pt_crs, dem_layer.crs())
                c_col = (pt_dem.x() - target_xmin) / dem_xres
                c_row = (target_ymax - pt_dem.y()) / dem_yres
                rad_pix = max_dist / dem_xres
                point_mask = ((c_full - c_col)**2 + (r_full - c_row)**2 <= rad_pix**2)
                circular_mask |= point_mask
                
                # Robust Visibility Detection
                if union_mode:
                    vis_mask = (vs_data[:h_overlap, :w_overlap] > 0.5)
                else:
                    vis_mask = (vs_data[:h_overlap, :w_overlap] > 0.5) & point_mask[:h_overlap, :w_overlap]
                
                if vs_nodata is not None:
                    vis_mask &= (vs_data[:h_overlap, :w_overlap] != vs_nodata)
                
                if union_mode:
                    cumulative[:h_overlap, :w_overlap][vis_mask] = 255
                else:
                    cumulative[:h_overlap, :w_overlap][vis_mask] += val_to_add
                    
                vs_ds = None
            
            # 4. Final NoData masking
            # [v1.6.14] Apply circular buffer masking for ALL modes
            nodata_value = -9999
            cumulative[~circular_mask] = nodata_value
            
            # Save Result
            driver = gdal.GetDriverByName('GTiff')
            out_ds = driver.Create(output_path, target_width, target_height, 1, gdal.GDT_Float32)
            out_ds.SetGeoTransform((target_xmin, dem_xres, 0, target_ymax, 0, -dem_yres))
            out_ds.SetProjection(dem_proj)
            band = out_ds.GetRasterBand(1)
            band.SetNoDataValue(nodata_value)
            band.WriteArray(cumulative)
            out_ds = None
            return True
        except Exception as e:
            import traceback

            log_message(f"Viewshed merge error: {e}", level=Qgis.Critical)
            log_message(traceback.format_exc(), level=Qgis.Critical)
            return False
    
    def run_multi_viewshed(self, dem_layer, obs_height, tgt_height, max_dist, curvature, refraction, refraction_coeff=0.13):
        """Run cumulative viewshed from multiple observer points
        
        Combines points from multiple sources:
        1. Point layer: all points from selected layer
        2. Line/Polygon layer: points generated along boundary at interval
        3. Manual clicks: additional points added by user
        
        Creates a raster where cell values indicate how many observer points
        can see that location. Color-coded from red (1 point) to green (all points).
        """
        points = [] # Start empty, we'll collect from all sources as (pt, crs)
        mask_geometries_dem = []
        want_cutout_input_polygon = bool(
            hasattr(self, "chkCutoutInputPolygon") and self.chkCutoutInputPolygon.isChecked()
        )
        interval = self.spinLineInterval.value()
        canvas_crs = self.canvas.mapSettings().destinationCrs()

        # 1. Add manual clicks
        for p in self.observer_points:
            points.append((p, canvas_crs))
        if self.observer_point: # Also check the single selection if any
            points.append((self.observer_point, canvas_crs))
        
        # [v1.6.16] Handle manually drawn lines (from Line Viewshed tool)
        if hasattr(self, 'drawn_line_points') and self.drawn_line_points and len(self.drawn_line_points) >= 2:
            pts_for_geom = list(self.drawn_line_points)
            if getattr(self, 'is_line_closed', False):
                pts_for_geom.append(self.drawn_line_points[0])
            
            line_geom = QgsGeometry.fromPolylineXY(pts_for_geom)
            length = line_geom.length()
            
            if length > 0:
                num_pts = max(1, int(length / interval))
                for i in range(num_pts + 1):
                    frac = i / num_pts if num_pts > 0 else 0
                    pt = line_geom.interpolate(frac * length)
                    if pt and not pt.isEmpty():
                        points.append((pt.asPoint(), canvas_crs))
        
        # 2. Add points from layer if selected
        if self.radioFromLayer.isChecked():
            obs_layer = self.cmbObserverLayer.currentLayer()
            if obs_layer:
                transform_to_dem = None
                if want_cutout_input_polygon and obs_layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                    try:
                        transform_to_dem = QgsCoordinateTransform(
                            obs_layer.crs(), dem_layer.crs(), QgsProject.instance()
                        )
                    except Exception:
                        transform_to_dem = None

                # [v1.6.02] Auto-hide labels to reduce clutter
                try:
                    obs_layer.setLabelsEnabled(False)
                except Exception:
                    pass
                
                # Use selection if exists
                selected_features = obs_layer.selectedFeatures()
                target_features = selected_features if selected_features else obs_layer.getFeatures()
                
                for feat in target_features:
                    geom = feat.geometry()
                    if not geom or geom.isEmpty(): continue
                    
                    if geom.type() == QgsWkbTypes.PointGeometry:
                        if geom.isMultipart():
                            for pt in geom.asMultiPoint():
                                points.append((pt, obs_layer.crs()))
                        else:
                            points.append((geom.asPoint(), obs_layer.crs()))
                    
                    elif geom.type() == QgsWkbTypes.LineGeometry:
                        length = geom.length()
                        num_pts = max(1, int(length / interval))
                        for i in range(num_pts + 1):
                            frac = i / num_pts if num_pts > 0 else 0
                            pt = geom.interpolate(frac * length).asPoint()
                            points.append((pt, obs_layer.crs()))
                    
                    elif geom.type() == QgsWkbTypes.PolygonGeometry:
                        if want_cutout_input_polygon:
                            try:
                                geom_dem = QgsGeometry(geom)
                                if transform_to_dem is not None:
                                    try:
                                        geom_dem.transform(transform_to_dem)
                                    except Exception:
                                        pass
                                mask_geometries_dem.append(geom_dem)
                            except Exception:
                                pass
                        if geom.isMultipart():
                            polygons = geom.asMultiPolygon()
                        else:
                            polygons = [geom.asPolygon()]
                        
                        for polygon in polygons:
                            if polygon and len(polygon) > 0:
                                exterior_ring = polygon[0]
                                ring_geom = QgsGeometry.fromPolylineXY(exterior_ring)
                                length = ring_geom.length()
                                num_pts = max(1, int(length / interval))
                                for i in range(num_pts + 1):
                                    frac = i / num_pts if num_pts > 0 else 0
                                    pt = ring_geom.interpolate(frac * length).asPoint()
                                    points.append((pt, obs_layer.crs()))
        
        if not points or len(points) < 1:
            push_message(self.iface, "오류", "관측점이 최소 1개 이상 필요합니다", level=2)
            restore_ui_focus(self)
            return

        # [v1.5.85] Robust point management for cumulative analysis
        total_needed = len(points)
        # [v1.6.15] Use UI spinMaxPoints value, default 50
        MAX_POINTS = 50
        if hasattr(self, 'spinMaxPoints'):
            MAX_POINTS = self.spinMaxPoints.value() 
        
        if total_needed > MAX_POINTS:
            from qgis.PyQt.QtWidgets import QMessageBox
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("관측점 개수 경고")
            msg.setText(f"⚠️ 전체 분석에 {total_needed}개의 관측점이 포함되어 있습니다.\n"
                       f"성능을 위해 기본적으로 {MAX_POINTS}개로 제한됩니다.")
            msg.setInformativeText(f"고해상도 DEM과 많은 관측점은 수 분 이상 소요될 수 있습니다.\n\n"
                                  f"• 예(Yes): {MAX_POINTS}개로 축소하여 안전하게 진행\n"
                                  f"• 아니오(No): 전체 {total_needed}개 분석 (매우 느림)\n"
                                  f"• 취소(Cancel): 취소 및 설정으로 복귀")
            msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            msg.setDefaultButton(QMessageBox.Yes)
            
            res_msg = msg.exec_()
            if res_msg == QMessageBox.Cancel:
                self.show()
                self.raise_()
                self.activateWindow()
                return
            elif res_msg == QMessageBox.Yes:
                step = len(points) // MAX_POINTS
                points = points[::step][:MAX_POINTS]
                self.iface.messageBar().pushMessage("알림", f"관측점이 {len(points)}개로 샘플링되었습니다.", level=1)
            else:
                self.iface.messageBar().pushMessage("경고", f"{total_needed}개 전체 점에 대해 분석을 시작합니다. 처리 중 QGIS가 응답하지 않을 수 있습니다.", level=1)

        # [v1.5.97] Hide dialog ONLY after all warnings and user decisions
        self.hide()
        QtWidgets.QApplication.processEvents()

        extra = self._build_gdal_viewshed_extra(curvature, refraction, refraction_coeff)

        # Setup progress dialog
        progress = QtWidgets.QProgressDialog("다중점 가시권 분석 초기화 중...", "취소", 0, len(points), self)
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents() # Ensure visibility
        # [v1.5.65] Smart Analysis Extent Optimization
        total_obs_ext = QgsRectangle()
        total_obs_ext.setMinimal()
        for pt, p_crs in points:
            pt_dem = self.transform_point(pt, p_crs, dem_layer.crs())
            total_obs_ext.combineExtentWith(pt_dem.x(), pt_dem.y())
        
        smart_ext = QgsRectangle(
            total_obs_ext.xMinimum() - max_dist * 1.2, total_obs_ext.yMinimum() - max_dist * 1.2,
            total_obs_ext.xMaximum() + max_dist * 1.2, total_obs_ext.yMaximum() + max_dist * 1.2
        )
        final_ext = smart_ext.intersect(dem_layer.extent())
        if final_ext.isEmpty(): final_ext = dem_layer.extent()

        # [v1.5.75] Unified Grid Snapping - Calculate ONCE for both Warp and NumPy
        res = dem_layer.rasterUnitsPerPixelX()
        dem_ext = dem_layer.extent()
        
        # Snap the combined analysis extent to the DEM's pixel grid
        # snap_xmin = dem_origin_x + N * res
        snap_xmin = dem_ext.xMinimum() + math.floor((final_ext.xMinimum() - dem_ext.xMinimum()) / res) * res
        snap_ymax = dem_ext.yMaximum() - math.floor((dem_ext.yMaximum() - final_ext.yMaximum()) / res) * res
        snap_xmax = dem_ext.xMinimum() + math.ceil((final_ext.xMaximum() - dem_ext.xMinimum()) / res) * res
        snap_ymin = dem_ext.yMaximum() - math.ceil((dem_ext.yMaximum() - final_ext.yMinimum()) / res) * res
        
        target_rect = QgsRectangle(snap_xmin, snap_ymin, snap_xmax, snap_ymax)
        target_extent_str = f"{snap_xmin},{snap_ymin},{snap_xmax},{snap_ymax}"
        t_width = int(round((snap_xmax - snap_xmin) / res))
        t_height = int(round((snap_ymax - snap_ymin) / res))
        
        grid_info = {
            'xmin': snap_xmin, 'ymax': snap_ymax, 'xmax': snap_xmax, 'ymin': snap_ymin,
            'res': res, 'width': t_width, 'height': t_height
        }
        
        # Diagnostic Log
        self.iface.messageBar().pushMessage(
            "분석 정보", 
            f"스마트 범위 적용: {final_ext.width():.1f}x{final_ext.height():.1f}m (전체 대비 { (final_ext.area()/dem_ext.area())*100:.1f}%)", 
            level=0
        )

        temp_outputs = []
        viewshed_results = []
        for i, (point, p_crs) in enumerate(points):
            if progress.wasCanceled():
                break
            progress.setValue(i)
            QtWidgets.QApplication.processEvents()
            
            output_raw = os.path.join(tempfile.gettempdir(), f'archt_vs_raw_{i}_{uuid.uuid4().hex[:8]}.tif')
            pt_dem = self.transform_point(point, p_crs, dem_layer.crs())
             
            try:
                processing.run("gdal:viewshed", {
                    'INPUT': dem_layer.source(), 'BAND': 1, 'OBSERVER': f"{pt_dem.x()},{pt_dem.y()}",
                    'OBSERVER_HEIGHT': obs_height, 'TARGET_HEIGHT': tgt_height, 'MAX_DISTANCE': max_dist,
                    'EXTRA': extra, 'OUTPUT': output_raw
                })
                
                if os.path.exists(output_raw):
                    temp_outputs.append(output_raw)
                    full_vs = os.path.join(tempfile.gettempdir(), f'archt_fullvs_{i}_{uuid.uuid4().hex[:8]}.tif')
                    try:
                        # [v1.6.11] ENSURE PERFECT ALIGNMENT: Warp each result to the combined target extent.
                        processing.run("gdal:warpreproject", {
                            'INPUT': output_raw, 
                            'TARGET_EXTENT': target_rect, 
                            'TARGET_EXTENT_CRS': dem_layer.crs().authid(),
                            'NODATA': -9999, 'TARGET_RESOLUTION': res, 'RESAMPLING': 0, 'DATA_TYPE': 5, 'OUTPUT': full_vs
                        })
                        if os.path.exists(full_vs):
                            temp_outputs.append(full_vs)
                            viewshed_results.append((i, full_vs))
                            try:
                                os.remove(output_raw)
                            except Exception:
                                pass
                    except Exception as e:
                        log_message(f"warpreproject failed for viewshed #{i}: {e}", level=Qgis.Warning)
            except Exception as e:
                log_message(f"viewshed failed for point #{i}: {e}", level=Qgis.Warning)
                continue
        
        progress.setValue(len(points))
        
        if not viewshed_results:
            self.iface.messageBar().pushMessage("오류", "유효한 가시권 분석 결과를 생성하지 못했습니다. 보간 또는 범위 설정을 확인하세요.", level=2)
            self.show()
            return
        
        # Combine all viewsheds by summing (cumulative viewshed)
        # Using a safer approach with processing.run("gdal:merge")
        final_output = os.path.join(tempfile.gettempdir(), f'archtoolkit_viewshed_cumulative_{uuid.uuid4().hex[:8]}.tif')
        
        try:
            # [v1.5.68] Optimized Cumulative Viewshed Merge using NumPy
            progress.setLabelText("결과 통합 중 (NumPy)...")
            QtWidgets.QApplication.processEvents()
            
            # Determine merge/style mode
            # - Default: bit-flag combinations (V(1,2,3...))
            # - Optional: count-only (0~N) via chkCountOnly
            # - Safety: for Line mode or too many points, fall back to Union unless count-only is requested
            is_line_mode = self.radioLineViewshed.isChecked()
            is_count_mode = hasattr(self, "chkCountOnly") and self.chkCountOnly.isChecked()
            is_union_mode = (not is_count_mode) and (is_line_mode or len(points) > 20)
            
            if is_union_mode:
                mode_str = "합집합(Union)"
            elif is_count_mode:
                mode_str = "누적 개수(Count)"
            else:
                mode_str = "누적 조합(Bit-flag)"
            self.iface.messageBar().pushMessage("분석 시작", f"모드: {mode_str}, 점 개수: {len(points)}", level=0)
            
            # viewshed_results is already [(idx, filepath), ...] as needed by combine_viewsheds_numpy
            success = self.combine_viewsheds_numpy(
                dem_layer=dem_layer,
                viewshed_files=viewshed_results,
                output_path=final_output,
                observer_points=points,
                max_dist=max_dist,
                is_count_mode=is_count_mode,
                grid_info=grid_info,
                union_mode=is_union_mode
            )
            
            if not success or not os.path.exists(final_output):
                raise Exception("누적 가시권 결과 생성 실패 (NumPy)")
            
            # Clean up intermediate vs files
            cleanup_files(temp_outputs)

            # Optional: cut out input polygon interior (NoData) so the outside pattern is clearer.
            if want_cutout_input_polygon and mask_geometries_dem:
                try:
                    progress.setLabelText("입력 폴리곤 내부 비우는 중...")
                    QtWidgets.QApplication.processEvents()
                except Exception:
                    pass
                self._burn_nodata_for_geometries_in_raster(
                    final_output, mask_geometries_dem, nodata_value=-9999
                )
    
            
            
            # Add result to map
            layer_name = f"가시권_누적_{len(points)}개점"
            viewshed_layer = QgsRasterLayer(final_output, layer_name)
            
            if viewshed_layer.isValid():
                # Apply result style
                if is_union_mode:
                    self.apply_viewshed_style(viewshed_layer)
                elif is_count_mode:
                    self.apply_count_style(viewshed_layer, len(points))
                else:
                    self.apply_cumulative_style(viewshed_layer, len(points))
                
                # [v1.5.80] Always create a numbered observer layer for cumulative analysis.
                # This ensures Point 1, 2, 3... are clearly visible and match the legend V(1,2).
                observer_layer = self.create_observer_layer("누적가시권_관측점", points)
                
                QgsProject.instance().addMapLayer(viewshed_layer)
                self.last_result_layer_id = viewshed_layer.id()
                
                # [v1.6.18] Link observer layer for cleanup when viewshed layer is deleted
                if observer_layer:
                    self.result_observer_layer_map[viewshed_layer.id()] = observer_layer.id()
                
                # Ensure label layer is on top
                self.update_layer_order()
                
                # Link markers and annotations [v1.6.02]
                current_annotations = list(self.point_labels)
                self.link_current_marker_to_layer(viewshed_layer.id(), points, annotations=current_annotations)
                self.point_labels = [] # Ownership transferred
                
                self.iface.messageBar().pushMessage(
                    "완료", 
                    f"누적 가시권 분석 완료 ({len(points)}개 관측점)", 
                    level=0
                )

                self.accept()
            else:
                push_message(self.iface, "오류", "결과 레이어 로드 실패", level=2)
                restore_ui_focus(self)
        except Exception as e:
            push_message(self.iface, "오류", f"병합 중 오류: {str(e)}", level=2)
            restore_ui_focus(self)
        finally:
            if 'progress' in locals():
                progress.close()
            cleanup_files(temp_outputs)
    
    def apply_frequency_style(self, layer, max_count):
        """Apply a standard color ramp (Viridis-like) for frequency count analysis"""
        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Interpolated)
        
        layer.dataProvider().setNoDataValue(1, -9999)
        
        # Get user-defined "Not Visible" color
        not_visible_color = self.btnNotVisibleColor.color()
        if not_visible_color.alpha() == 255:
            not_visible_color.setAlpha(0) # Transparent background for frequency
            
        # Standard Red-Yellow-Cyan ramp
        # 0: Transparent
        # 1: Red (Rarely seen)
        # Max/2: Yellow
        # Max: Cyan/Green (Frequently seen)
        
        colors = [
            QgsColorRampShader.ColorRampItem(0, not_visible_color, "보이지 않음 (0)"),
            QgsColorRampShader.ColorRampItem(1, QColor(255, 0, 0, 180), "1개소 관측 (최소)"),
            QgsColorRampShader.ColorRampItem(max_count / 2, QColor(255, 255, 0, 180), f"{max_count/2:.1f}개소 중첩"),
            QgsColorRampShader.ColorRampItem(max_count, QColor(0, 255, 255, 180), f"{max_count}개소 관측 (최대)")
        ]
        
        color_ramp.setColorRampItemList(colors)
        shader.setRasterShaderFunction(color_ramp)
        
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setClassificationMax(max_count)
        renderer.setClassificationMin(0)
        layer.setRenderer(renderer)
        layer.setOpacity(0.8)
        layer.triggerRepaint()

    def apply_count_style(self, layer, num_points):
        """Apply count-based styling for cumulative viewshed.

        Values:
        - -9999: NoData (outside radius, cut-outs) -> transparent
        - 0: not visible
        - 1..N: number of observer points that can see the cell
        """
        nodata_value = -9999
        layer.dataProvider().setNoDataValue(1, nodata_value)

        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)

        not_visible_color = self.btnNotVisibleColor.color()
        if not_visible_color.alpha() == 255:
            not_visible_color.setAlpha(180)

        max_count = max(1, int(num_points or 1))
        colors = [
            QgsColorRampShader.ColorRampItem(nodata_value, QColor(0, 0, 0, 0), "NoData"),
            QgsColorRampShader.ColorRampItem(0, not_visible_color, "보이지 않음"),
        ]

        # Red -> Yellow -> Green gradient by count (HSV hue 0..120)
        for k in range(1, max_count + 1):
            if max_count == 1:
                hue = 120
            else:
                t = (k - 1) / (max_count - 1)
                hue = int(round(t * 120))
            colors.append(
                QgsColorRampShader.ColorRampItem(
                    k,
                    QColor.fromHsv(hue, 200, 255, 200),
                    f"{k}개 누적",
                )
            )

        color_ramp.setColorRampItemList(colors)
        shader.setRasterShaderFunction(color_ramp)

        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setClassificationMax(max_count)
        renderer.setClassificationMin(0)
        layer.setRenderer(renderer)
        layer.setOpacity(0.7)
        layer.triggerRepaint()

    def apply_visual_imbalance_style(self, layer):
        """Apply styling for visual imbalance raster (forward vs reverse mismatch)."""
        nodata_value = -9999
        layer.dataProvider().setNoDataValue(1, nodata_value)

        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)

        colors = [
            QgsColorRampShader.ColorRampItem(nodata_value, QColor(0, 0, 0, 0), "NoData"),
            QgsColorRampShader.ColorRampItem(0, QColor(0, 0, 0, 0), "표시 안함(둘 다 보임/둘 다 안보임)"),
            QgsColorRampShader.ColorRampItem(1, QColor(0, 150, 255, 200), "관측점만 보임 (내가 보고, 상대는 못봄)"),
            QgsColorRampShader.ColorRampItem(2, QColor(255, 140, 0, 200), "역방향만 보임 (상대는 보고, 나는 못봄)"),
        ]

        color_ramp.setColorRampItemList(colors)
        shader.setRasterShaderFunction(color_ramp)

        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setClassificationMax(2)
        renderer.setClassificationMin(0)
        layer.setRenderer(renderer)
        layer.setOpacity(0.8)
        layer.triggerRepaint()

    def apply_cumulative_style(self, layer, num_points):
        """Apply bit-flag based styling for cumulative viewshed
        
        Values: binary combination of observers (1, 2, 4, 8...)
        """
        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)
        
        layer.dataProvider().setNoDataValue(1, -9999)
        
        # Get user-defined "Not Visible" color
        not_visible_color = self.btnNotVisibleColor.color()
        if not_visible_color.alpha() == 255:
            not_visible_color.setAlpha(180)
            
        colors = [
            QgsColorRampShader.ColorRampItem(0, not_visible_color, "보이지 않음"),
        ]
        
        # Custom discrete color mixing for v1.5.5
        # Primary base colors for up to 8 observers
        base_colors = [
            QColor(255, 0, 0, 200),   # 1: Red
            QColor(0, 255, 0, 200),   # 2: Green
            QColor(0, 0, 255, 200),   # 3: Blue
            QColor(255, 255, 0, 200), # 4: Yellow
            QColor(255, 0, 255, 200), # 5: Magenta
            QColor(0, 255, 255, 200), # 6: Cyan
            QColor(255, 128, 0, 200), # 7: Orange
            QColor(128, 0, 255, 200)  # 8: Purple
        ]
        
        # Limit discrete entries to avoid lag (up to 128 combinations)
        max_combinations = min(2**num_points, 128)
        
        for v in range(1, max_combinations):
            # Find which points see this pixel
            component_colors = []
            seen_pts = []
            for i in range(num_points):
                if v & (1 << i):
                    seen_pts.append(str(i + 1))
                    if i < len(base_colors):
                        component_colors.append(base_colors[i])
            
            count = len(seen_pts)
            label = f"V({','.join(seen_pts)})"
            if count > 1:
                label += f" - {count}개소 중첩"
            else:
                label += " - 가시"
                
            # Mixed color logic
            if not component_colors:
                # Fallback for many points
                r, g, b = (v * 43) % 256, (v * 87) % 256, (v * 123) % 256
                mixed_color = QColor(r, g, b, 200)
            elif len(component_colors) == 1:
                mixed_color = component_colors[0]
            else:
                # Average components for intuitive mixing (Red + Green = Yellow-ish)
                avg_r = sum(c.red() for c in component_colors) // len(component_colors)
                avg_g = sum(c.green() for c in component_colors) // len(component_colors)
                avg_b = sum(c.blue() for c in component_colors) // len(component_colors)
                mixed_color = QColor(avg_r, avg_g, avg_b, 200)
            
            colors.append(QgsColorRampShader.ColorRampItem(v, mixed_color, label))
            
        color_ramp.setColorRampItemList(colors)
        shader.setRasterShaderFunction(color_ramp)
        
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setClassificationMax(2**num_points - 1)
        renderer.setClassificationMin(0)
        layer.setRenderer(renderer)
        layer.setOpacity(0.7)
        layer.triggerRepaint()
    
    def _create_higuchi_viewshed_raster(self, input_raster_path, output_raster_path, observer_point, observer_crs, dem_layer):
        """Reclassify a binary viewshed raster into Higuchi distance zones.

        Output classes (Byte-like, stored as Int16 to keep NoData=-9999):
        - 0: not visible (transparent in Higuchi style)
        - 85: near view (0~500m)
        - 170: mid view (500m~2.5km)
        - 255: far view (2.5km~)
        """
        # Observer point must be in DEM CRS to compute metric distance per pixel.
        observer_dem = self.transform_point(observer_point, observer_crs, dem_layer.crs())
        ox = float(observer_dem.x())
        oy = float(observer_dem.y())

        nodata_out = -9999

        ds = None
        out_ds = None
        try:
            ds = gdal.Open(input_raster_path, gdal.GA_ReadOnly)
            if ds is None:
                raise Exception("히구치 재분류: 입력 래스터를 열 수 없습니다.")

            band = ds.GetRasterBand(1)
            in_nodata = band.GetNoDataValue()
            gt = ds.GetGeoTransform()
            proj = ds.GetProjection()
            xsize = ds.RasterXSize
            ysize = ds.RasterYSize

            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(
                output_raster_path,
                xsize,
                ysize,
                1,
                gdal.GDT_Int16,
                options=["TILED=YES", "COMPRESS=LZW"],
            )
            if out_ds is None:
                raise Exception("히구치 재분류: 출력 래스터를 만들 수 없습니다.")

            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj)
            out_band = out_ds.GetRasterBand(1)
            out_band.SetNoDataValue(nodata_out)

            block_x, block_y = band.GetBlockSize()
            if not block_x or not block_y:
                block_x, block_y = 512, 512

            for yoff in range(0, ysize, block_y):
                yblock = min(block_y, ysize - yoff)
                for xoff in range(0, xsize, block_x):
                    xblock = min(block_x, xsize - xoff)

                    arr = band.ReadAsArray(xoff, yoff, xblock, yblock)
                    if arr is None:
                        continue

                    # Pixel center coordinates via affine transform.
                    cols = (xoff + np.arange(xblock, dtype=np.float64)) + 0.5
                    rows = (yoff + np.arange(yblock, dtype=np.float64)) + 0.5
                    col_grid, row_grid = np.meshgrid(cols, rows)
                    x = gt[0] + col_grid * gt[1] + row_grid * gt[2]
                    y = gt[3] + col_grid * gt[4] + row_grid * gt[5]
                    dist = np.sqrt((x - ox) ** 2 + (y - oy) ** 2)

                    nodata_mask = np.zeros(arr.shape, dtype=bool)
                    if in_nodata is not None:
                        nodata_mask |= arr == in_nodata
                    # Our pipeline commonly uses -9999 for masked-out pixels.
                    nodata_mask |= arr == -9999

                    valid = ~nodata_mask
                    visible = valid & (arr > 0)

                    out = np.full(arr.shape, nodata_out, dtype=np.int16)
                    out[valid] = 0
                    out[visible & (dist <= 500.0)] = 85
                    out[visible & (dist > 500.0) & (dist <= 2500.0)] = 170
                    out[visible & (dist > 2500.0)] = 255

                    out_band.WriteArray(out, xoff, yoff)

            out_band.FlushCache()
            out_ds.FlushCache()
        except Exception:
            # Avoid leaving a partially-written raster behind.
            cleanup_files([output_raster_path])
            raise
        finally:
            out_ds = None
            ds = None

    def apply_higuchi_style(self, layer):
        """Apply Higuchi (1975) distance-based landscape zone styling"""
        # Set NoData value to ensure corners are transparent
        layer.dataProvider().setNoDataValue(1, -9999)

        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)

        # Use the user's "Not visible" color (default: pink) for non-visible cells (value 0).
        not_visible_color = self.btnNotVisibleColor.color() if hasattr(self, "btnNotVisibleColor") else QColor(255, 105, 180, 180)
        if not_visible_color.alpha() == 255:
            not_visible_color.setAlpha(180)

        colors = [
            QgsColorRampShader.ColorRampItem(0, not_visible_color, "보이지 않음"),
            QgsColorRampShader.ColorRampItem(85, QColor(255, 50, 50, 200), "근경 (0~500m: 질감/세부 인지)"),     # Sharp Red
            QgsColorRampShader.ColorRampItem(170, QColor(255, 165, 0, 200), "중경 (500m~2.5km: 형태/부피 파악)"), # Orange
            QgsColorRampShader.ColorRampItem(255, QColor(138, 43, 226, 200), "원경 (2.5km~: 실루엣/스카이라인)"), # Purple/Blue
        ]
        
        color_ramp.setColorRampItemList(colors)
        shader.setRasterShaderFunction(color_ramp)
        
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        layer.setRenderer(renderer)
        layer.setOpacity(0.7)
        layer.triggerRepaint()
    
    def on_higuchi_toggled(self, checked):
        """Suggest parameters suited for Higuchi analysis"""
        # Visual imbalance overlay is not compatible with Higuchi reclassification.
        if hasattr(self, "chkVisualImbalance"):
            try:
                self.chkVisualImbalance.setEnabled(not checked)
                if checked:
                    self.chkVisualImbalance.setChecked(False)
            except Exception:
                pass

        if not checked or not hasattr(self, "spinMaxDistance"):
            return

        # Higuchi zones: Near(0~500m) / Mid(500m~2.5km) / Far(2.5km~)
        current_dist = float(self.spinMaxDistance.value())
        if current_dist >= 2500:
            return

        from qgis.PyQt.QtWidgets import QMessageBox

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("히구치 거리대 안내")
        msg.setText(
            "히구치 거리대는 '보이는 영역'을 거리별로 근경/중경/원경으로 나눠 색으로 표시합니다.\n"
            f"현재 최대거리: {current_dist:,.0f} m\n\n"
            "원경(2.5km~)을 보려면 최소 2,500m가 필요합니다. (권장: 5,000m)"
        )

        btn_2500 = msg.addButton("2,500m로 설정", QMessageBox.AcceptRole)
        btn_5000 = msg.addButton("5,000m로 설정(권장)", QMessageBox.AcceptRole)
        btn_keep = msg.addButton("유지", QMessageBox.RejectRole)
        msg.setDefaultButton(btn_5000)

        msg.exec_()
        clicked = msg.clickedButton()
        if clicked == btn_2500:
            self.spinMaxDistance.setValue(2500)
        elif clicked == btn_5000:
            self.spinMaxDistance.setValue(5000)
        elif clicked == btn_keep:
            return
    
    def create_higuchi_rings(self, center_point, center_crs, max_dist, dem_layer):
        """Create buffer rings showing Higuchi distance zones"""
        
        # Use DEM CRS instead of hardcoded EPSG:5186
        layer = QgsVectorLayer("LineString?crs=" + dem_layer.crs().authid(), "히구치_거리대", "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("zone", QVariant.String),
            QgsField("distance_m", QVariant.Int),
        ])
        layer.updateFields()
        
        # We need point in DEM CRS for buffer
        center_dem = self.transform_point(center_point, center_crs, dem_layer.crs())
        zones = [
            (500, "근경 (500m)", QColor(255, 80, 80)),      # Red
            (2500, "중경 (2.5km)", QColor(255, 200, 0)),    # Yellow
        ]
        
        # Add far zone only if max_dist is larger
        if max_dist > 2500:
            zones.append((max_dist, f"원경 ({max_dist/1000:.1f}km)", QColor(50, 200, 50))) # Green
        
        # Create ring features
        for distance, zone_name, color in zones:
            if distance <= max_dist:
                # Create circular buffer
                center_geom = QgsGeometry.fromPointXY(center_dem)
                buffer_geom = center_geom.buffer(distance, 64)
                
                # Robustly get the exterior ring (handling potential MultiPolygon from buffer)
                if buffer_geom.isEmpty():
                    continue
                    
                # buffer_geom of a point should be a Polygon, but let's be safe
                if buffer_geom.isMultipart():
                    parts = buffer_geom.asMultiPolygon()
                    if not parts:
                        continue
                    # Take the exterior ring of the first part
                    rings = parts[0]
                else:
                    rings = buffer_geom.asPolygon()
                
                if rings and len(rings) > 0:
                    exterior_ring = rings[0]
                    ring_geom = QgsGeometry.fromPolylineXY(exterior_ring)
                    feat = QgsFeature(layer.fields())
                    feat.setGeometry(ring_geom)
                    feat.setAttributes([zone_name, int(distance)])
                    pr.addFeature(feat)
        
        layer.updateExtents()
        
        # Apply categorized styling
        categories = []
        for distance, zone_name, color in zones:
            if distance <= max_dist:
                symbol = QgsLineSymbol.createSimple({
                    'color': color.name(),
                    'width': '1.5',
                    'line_style': 'dash'
                })
                category = QgsRendererCategory(zone_name, symbol, zone_name)
                categories.append(category)
        
        if categories:
            renderer = QgsCategorizedSymbolRenderer("zone", categories)
            layer.setRenderer(renderer)
        
        QgsProject.instance().addMapLayers([layer])

    def create_analysis_radius_ring(self, center_point, center_crs, max_dist, dem_layer, layer_name=None):
        """Create a single dashed ring showing the analysis radius."""
        if not layer_name:
            layer_name = f"관측반경_{int(max_dist)}m"

        layer = QgsVectorLayer("LineString?crs=" + dem_layer.crs().authid(), layer_name, "memory")
        pr = layer.dataProvider()
        pr.addAttributes(
            [
                QgsField("distance_m", QVariant.Int),
            ]
        )
        layer.updateFields()

        center_dem = self.transform_point(center_point, center_crs, dem_layer.crs())
        buffer_geom = QgsGeometry.fromPointXY(center_dem).buffer(float(max_dist), 128)
        if not buffer_geom or buffer_geom.isEmpty():
            return None

        ring_geom = buffer_geom.boundary()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(ring_geom)
        feat.setAttributes([int(max_dist)])
        pr.addFeature(feat)
        layer.updateExtents()

        symbol = QgsLineSymbol.createSimple(
            {
                "color": "120,120,120,220",
                "width": "1.0",
                "line_style": "dash",
            }
        )
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        # Ensure the ring is visible even when new layers are added under rasters.
        QgsProject.instance().addMapLayer(layer, False)
        try:
            QgsProject.instance().layerTreeRoot().insertLayer(0, layer)
        except Exception:
            QgsProject.instance().addMapLayer(layer)
        return layer
    
    def apply_viewshed_style(self, layer):
        """Apply a binary visibility style to viewshed raster
        
        gdal:viewshed output:
        - 0 = Not visible
        - 255 = Visible
        """
        # Set NoData to -9999 so 0 is treated as valid data (Not Visible = Pink),
        # and masked areas (outside radius / cut-outs) become transparent.
        nodata_value = -9999
        layer.dataProvider().setNoDataValue(1, nodata_value)
        
        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)
        
        # Get user-defined colors from UI
        visible_color = self.btnVisibleColor.color()
        if visible_color.alpha() == 255:
            visible_color.setAlpha(180)
            
        not_visible_color = self.btnNotVisibleColor.color()
        if not_visible_color.alpha() == 255:
            not_visible_color.setAlpha(180)
            
        # gdal:viewshed outputs 0=not visible, 255=visible
        colors = [
            QgsColorRampShader.ColorRampItem(nodata_value, QColor(0, 0, 0, 0), "NoData"),
            QgsColorRampShader.ColorRampItem(0, not_visible_color, "보이지 않음"),
            QgsColorRampShader.ColorRampItem(255, visible_color, "보임")
        ]
        color_ramp.setColorRampItemList(colors)
        shader.setRasterShaderFunction(color_ramp)
        
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        layer.setRenderer(renderer)
        layer.setOpacity(0.7)
        layer.triggerRepaint()

    def link_current_marker_to_layer(self, layer_id, active_points_with_crs=None, annotations=None):
        """Register point markers and annotations to be cleaned up when layer_id is removed.
        Ensures points are transformed to Canvas CRS for visibility.
        """
        result_marker = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        result_marker.setColor(QColor(255, 0, 0, 200)) # Semi-transparent red
        # ... (rest of rubberband setup is same, skipping lines for brevity if possible, but replace needs context)
        result_marker.setWidth(2)
        result_marker.setIconSize(4) # Small dots
        result_marker.setIcon(QgsRubberBand.ICON_CIRCLE)
        
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        
        # If specific points with CRS are passed, transform them to canvas
        if active_points_with_crs:
            for pt, p_crs in active_points_with_crs:
                # Transform to canvas CRS for correct display
                pt_canvas = self.transform_point(pt, p_crs, canvas_crs)
                result_marker.addPoint(pt_canvas)
        else:
            # Fallback for manual map clicks (already in Canvas CRS)
            if self.observer_point:
                result_marker.addPoint(self.observer_point)
            for p in self.observer_points:
                result_marker.addPoint(p)
            
        if layer_id not in self.result_marker_map:
            self.result_marker_map[layer_id] = []
        self.result_marker_map[layer_id].append(result_marker)
        result_marker.show()
        
        # [v1.6.02] Store text annotations
        if annotations:
            if layer_id not in self.result_annotation_map:
                self.result_annotation_map[layer_id] = []
            self.result_annotation_map[layer_id].extend(annotations)
    
    def accept(self):
        """Close dialog after successful analysis - keep only result markers visible"""
        # Clear the transient selection markers immediately
        self.point_marker.reset(QgsWkbTypes.PointGeometry)
        
        # Reset state for next use
        self.observer_points = []
        self.observer_point = None
        self.target_point = None
        self.los_click_count = 0
        self._reverse_target_geom = None
        self._reverse_target_crs = None
        self._reverse_target_layer_name = None
        self._reverse_target_fid = None
        super().accept()
    
    def reject(self):
        """Clear markers on cancel (no analysis run)"""
        self.point_marker.reset(QgsWkbTypes.PointGeometry)
        self.observer_points = []
        self.observer_point = None
        self.target_point = None
        self.los_click_count = 0
        self._reverse_target_geom = None
        self._reverse_target_crs = None
        self._reverse_target_layer_name = None
        self._reverse_target_fid = None
        # Ensure indicator is hidden if tool was active
        if self.map_tool:
            try:
                self.map_tool.snap_indicator.setMatch(QgsPointLocator.Match())
            except Exception:
                pass
        super().reject()
    
    def closeEvent(self, event):
        """Clean up when dialog closes via X button"""
        self.point_marker.reset(QgsWkbTypes.PointGeometry)
        if self.original_tool:
            self.canvas.setMapTool(self.original_tool)
        event.accept()

    def cleanup_for_unload(self):
        """Disconnect long-lived signals and close child dialogs (for plugin unload/reload)."""
        # Disconnect global/project signals that keep this dialog alive across plugin reloads.
        try:
            QgsProject.instance().layersWillBeRemoved.disconnect(self.on_layers_removed)
        except Exception:
            pass
        try:
            self.iface.currentLayerChanged.disconnect(self._on_current_layer_changed)
        except Exception:
            pass
        try:
            self.iface.layerTreeView().clicked.disconnect(self._on_layer_tree_clicked)
        except Exception:
            pass

        # Disconnect per-layer selection handlers for LOS profile reopen.
        try:
            for lid, handler in list(getattr(self, "_los_selection_handlers", {}).items()):
                try:
                    layer = QgsProject.instance().mapLayer(lid)
                    if layer and handler:
                        layer.selectionChanged.disconnect(handler)
                except Exception:
                    pass
            self._los_selection_handlers = {}
        except Exception:
            pass

        # Close any open profile dialogs to ensure their canvas signals are released.
        try:
            for _lid, dlg in list(getattr(self, "_los_profile_dialogs", {}).items()):
                try:
                    if dlg:
                        dlg.close()
                except Exception:
                    pass
            self._los_profile_dialogs = {}
        except Exception:
            pass


class ViewshedPointTool(QgsMapToolEmitPoint):
    """Map tool for selecting viewshed observer point with snapping support"""
    
    def __init__(self, canvas, dialog):
        super().__init__(canvas)
        self.dialog = dialog
        self.snap_indicator = QgsSnapIndicator(canvas)
    
    def canvasMoveEvent(self, event):
        """Show snapping indicator"""
        res = self.canvas().snappingUtils().snapToMap(event.pos())
        if res.isValid():
            self.snap_indicator.setMatch(res)
        else:
            self.snap_indicator.setMatch(QgsPointLocator.Match())
    
    def canvasReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self.finish_selection()
            return
        
        res = self.canvas().snappingUtils().snapToMap(event.pos())
        if res.isValid():
            point = res.point()
        else:
            point = self.toMapCoordinates(event.pos())
        
        self.dialog.set_observer_point(point)
    
    def keyPressEvent(self, event):
        from qgis.PyQt.QtCore import Qt
        if event.key() == Qt.Key_Escape:
            self.finish_selection()
    
    def finish_selection(self):
        """Finish point selection and return to dialog"""
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        if self.dialog.original_tool:
            self.dialog.canvas.setMapTool(self.dialog.original_tool)
        self.dialog.show()
    
    def deactivate(self):
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()


class ViewshedLineTool(QgsMapToolEmitPoint):
    """Map tool for drawing a polyline on the map. Click to add vertices, right-click to finish."""
    
    def __init__(self, canvas, dialog):
        super().__init__(canvas)
        self.dialog = dialog
        self.snap_indicator = QgsSnapIndicator(canvas)
        self.points = []
        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.rubber_band.setColor(QColor(0, 100, 255, 180))
        self.rubber_band.setWidth(2)
    
    def canvasMoveEvent(self, event):
        res = self.canvas().snappingUtils().snapToMap(event.pos())
        if res.isValid():
            self.snap_indicator.setMatch(res)
            mouse_pt = res.point()
        else:
            self.snap_indicator.setMatch(QgsPointLocator.Match())
            mouse_pt = self.toMapCoordinates(event.pos())
        
        # UX Enhancement: Visual feedback for line closure
        is_near_start = False
        if len(self.points) >= 2:
            start_px = self.toCanvasCoordinates(self.points[0])
            curr_px = event.pos()
            dist = math.sqrt((start_px.x() - curr_px.x())**2 + (start_px.y() - curr_px.y())**2)
            if dist < 30: # 30px threshold
                mouse_pt = self.points[0] # Snap exactly to start
                is_near_start = True
        
        if self.points:
            self.rubber_band.reset(QgsWkbTypes.LineGeometry)
            if is_near_start:
                self.rubber_band.setColor(QColor(0, 200, 0, 180)) # Green when snapping for closure
                self.rubber_band.setWidth(3)
            else:
                self.rubber_band.setColor(QColor(0, 100, 255, 180)) # Normal blue
                self.rubber_band.setWidth(2)
                
            for pt in self.points:
                self.rubber_band.addPoint(pt)
            self.rubber_band.addPoint(mouse_pt)
    
    def canvasReleaseEvent(self, event):
        from qgis.PyQt.QtCore import Qt
        if event.button() == Qt.RightButton:
            self.finish_line()
            return
        
        res = self.canvas().snappingUtils().snapToMap(event.pos())
        point = res.point() if res.isValid() else self.toMapCoordinates(event.pos())

        modifiers = event.modifiers() if hasattr(event, "modifiers") else Qt.NoModifier
        shift_pressed = bool(modifiers & Qt.ShiftModifier)

        # Reverse viewshed: first click on an existing polygon selects it directly
        # (unless Shift is held to force drawing a custom polygon).
        is_reverse_mode = (
            self.dialog is not None
            and getattr(self.dialog, "radioReverseViewshed", None) is not None
            and self.dialog.radioReverseViewshed.isChecked()
            and (getattr(self.dialog, "radioFromLayer", None) is None or not self.dialog.radioFromLayer.isChecked())
        )
        if is_reverse_mode and not self.points and not shift_pressed:
            # 1) If clicking on an existing polygon, select it immediately.
            try:
                hit = self.dialog._identify_polygon_feature_at_canvas_point(point)
            except Exception:
                hit = None
            if hit:
                self.dialog.set_observer_point(point)
                self.cleanup()
                return
        
        # Check for snapping to start point (Close Loop)
        if len(self.points) >= 2:
            start_px = self.toCanvasCoordinates(self.points[0])
            curr_px = event.pos()
            # If distance < 30 pixels, close the line
            dist = math.sqrt((start_px.x() - curr_px.x())**2 + (start_px.y() - curr_px.y())**2)
            if dist < 30:
                self.finish_line(close_line=True)
                return
        
        self.points.append(point)
        self.rubber_band.addPoint(point)
    
    def keyPressEvent(self, event):
        from qgis.PyQt.QtCore import Qt
        if event.key() == Qt.Key_Escape:
            self.cleanup()
            self.dialog.show()
        elif event.key() == Qt.Key_C:
            self.finish_line(close_line=True)
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish_line(close_line=False)
    
    def finish_line(self, close_line=False):
        # Reverse viewshed: allow 1-point target or polygon (3+ points).
        if (
            self.dialog is not None
            and getattr(self.dialog, "radioReverseViewshed", None) is not None
            and self.dialog.radioReverseViewshed.isChecked()
        ):
            if len(self.points) == 1:
                # Treat as a single target point (reverse viewshed point)
                self.dialog.set_observer_point(self.points[0])
                self.cleanup()
                return
            if len(self.points) >= 3:
                self.dialog.set_line_from_tool(self.points, is_closed=True)
                self.cleanup()
                self.dialog.show()
                return
            self.dialog.iface.messageBar().pushMessage(
                "알림",
                "역방향 폴리곤은 최소 3개 점이 필요합니다 (또는 1개 점으로 대상점 선택).",
                level=1,
            )
            return

        if len(self.points) >= 2:
            self.dialog.set_line_from_tool(self.points, is_closed=close_line)
            self.cleanup()
            self.dialog.show()
            return

        self.dialog.iface.messageBar().pushMessage("알림", "최소 2개 점이 필요합니다", level=1)
    
    def cleanup(self):
        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        self.points = []
        if self.dialog.original_tool:
            self.dialog.canvas.setMapTool(self.dialog.original_tool)
    
    def deactivate(self):
        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()


class ProfilePlotWidget(QWidget):
    """Custom widget to draw 2D terrain profile for Viewshed Profiler"""
    def __init__(self, profile_data, obs_height, tgt_height, is_visible_overall=True, first_obstruction=None, parent=None):
        super().__init__(parent)
        self.profile_data = profile_data
        self.obs_height = obs_height
        self.tgt_height = tgt_height
        self.is_visible_overall = is_visible_overall
        self.first_obstruction = first_obstruction
        self.hover_distance = None
        self.hover_elevation = None
        self.on_hover_callback = None  # Function(distance_m|None) for map synchronization
        self.zoom_level = 1.0
        self.pan_offset = 0.0  # Horizontal offset in meters
        self.is_dragging = False
        self.drag_start_x = 0
        self.drag_start_offset = 0.0

        # Margins
        self.margin_left = 60
        self.margin_right = 30
        self.margin_top = 30
        self.margin_bottom = 40
        self.setMinimumSize(700, 350)
        self.setMouseTracking(True)

    def reset_view(self):
        self.zoom_level = 1.0
        self.pan_offset = 0.0
        self.set_hover_distance(None)
        self.update()

    def set_hover_distance(self, distance_m):
        if distance_m is None or not self.profile_data:
            self.hover_distance = None
            self.hover_elevation = None
            self.update()
            return

        try:
            distance_m = float(distance_m)
        except (TypeError, ValueError):
            return

        if distance_m < 0:
            distance_m = 0.0

        closest = min(self.profile_data, key=lambda p: abs(float(p["distance"]) - distance_m))
        self.hover_distance = float(closest["distance"])
        self.hover_elevation = float(closest["elevation"])
        self.update()

    def _get_view_params(self):
        if not self.profile_data:
            return None

        max_dist = float(self.profile_data[-1]["distance"]) if float(self.profile_data[-1]["distance"]) > 0 else 0.0
        if max_dist <= 0:
            return None

        plot_w = self.width() - self.margin_left - self.margin_right
        plot_h = self.height() - self.margin_top - self.margin_bottom
        if plot_w <= 0 or plot_h <= 0:
            return None

        zoom = max(1.0, float(self.zoom_level))
        visible_range = max_dist / zoom
        visible_range = max(1e-6, visible_range)

        max_offset = max(0.0, max_dist - visible_range)
        self.pan_offset = max(0.0, min(max_offset, float(self.pan_offset)))
        view_start = float(self.pan_offset)
        view_end = view_start + visible_range

        return {
            "max_dist": max_dist,
            "plot_w": plot_w,
            "plot_h": plot_h,
            "visible_range": visible_range,
            "view_start": view_start,
            "view_end": view_end,
        }

    def _distance_from_mouse(self, x, y):
        if not self.profile_data:
            return None

        view = self._get_view_params()
        if not view:
            return None

        if not (
            self.margin_left <= x <= self.margin_left + view["plot_w"]
            and self.margin_top <= y <= self.margin_top + view["plot_h"]
        ):
            return None

        rel_x = (x - self.margin_left) / view["plot_w"]
        return view["view_start"] + rel_x * view["visible_range"]

    def mouseMoveEvent(self, event):
        if self.is_dragging and self.zoom_level > 1.0:
            view = self._get_view_params()
            if view:
                delta_x = event.x() - self.drag_start_x
                delta_distance = (delta_x / view["plot_w"]) * view["visible_range"]
                self.pan_offset = self.drag_start_offset - delta_distance
                self.set_hover_distance(None)
                if self.on_hover_callback:
                    self.on_hover_callback(None)
                self.update()
            return

        distance = self._distance_from_mouse(event.x(), event.y())
        if distance is None:
            self.setToolTip("")
            self.set_hover_distance(None)
            if self.on_hover_callback:
                self.on_hover_callback(None)
            return

        closest = min(self.profile_data, key=lambda p: abs(float(p["distance"]) - distance))
        self.hover_distance = float(closest["distance"])
        self.hover_elevation = float(closest["elevation"])

        self.setToolTip(f"거리: {self.hover_distance:.1f}m\n고도: {self.hover_elevation:.1f}m")
        if self.on_hover_callback:
            self.on_hover_callback(self.hover_distance)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            view = self._get_view_params()
            if view and self.zoom_level > 1.0:
                self.is_dragging = True
                self.drag_start_x = event.x()
                self.drag_start_offset = float(self.pan_offset)
                self.setCursor(Qt.ClosedHandCursor)
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.is_dragging:
            self.is_dragging = False
            self.setCursor(Qt.ArrowCursor)
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.reset_view()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        if not self.profile_data:
            return

        view = self._get_view_params()
        if not view:
            return

        delta = event.angleDelta().y()
        if delta == 0:
            return

        factor = 1.2 if delta > 0 else (1.0 / 1.2)
        new_zoom = max(1.0, min(25.0, float(self.zoom_level) * factor))
        if abs(new_zoom - float(self.zoom_level)) < 1e-6:
            return

        try:
            pos = event.position()
            mx, my = pos.x(), pos.y()
        except AttributeError:
            pos = event.pos()
            mx, my = pos.x(), pos.y()

        anchor = self._distance_from_mouse(mx, my)
        if anchor is None:
            anchor = view["view_start"] + (view["visible_range"] / 2.0)

        rel = (anchor - view["view_start"]) / view["visible_range"] if view["visible_range"] > 0 else 0.5
        rel = max(0.0, min(1.0, rel))

        max_dist = view["max_dist"]
        new_visible_range = max_dist / new_zoom
        new_visible_range = max(1e-6, new_visible_range)
        new_view_start = anchor - rel * new_visible_range
        new_view_start = max(0.0, min(max_dist - new_visible_range, new_view_start))

        self.zoom_level = new_zoom
        self.pan_offset = new_view_start
        self.set_hover_distance(None)
        if self.on_hover_callback:
            self.on_hover_callback(None)
        self.update()
        event.accept()

    def leaveEvent(self, event):
        self.setToolTip("")
        self.set_hover_distance(None)
        if self.on_hover_callback:
            self.on_hover_callback(None)
        super().leaveEvent(event)
        
    def paintEvent(self, event):
        if not self.profile_data: return
        
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        width = self.width()
        height = self.height()
        
        view = self._get_view_params()
        if not view:
            return

        plot_w = view["plot_w"]
        plot_h = view["plot_h"]
        view_start = view["view_start"]
        view_end = view["view_end"]
        visible_range = view["visible_range"]
        
        # Data extraction
        distances = [p['distance'] for p in self.profile_data]
        elevations = [p['elevation'] for p in self.profile_data]
        
        max_dist = distances[-1] if distances[-1] > 0 else 1
        obs_elev = elevations[0] + self.obs_height
        tgt_elev = elevations[-1] + self.tgt_height
        
        min_elev = min(elevations) - 5
        max_elev = max(max(elevations), obs_elev, tgt_elev) + 5
        elev_range = max_elev - min_elev if max_elev > min_elev else 10
        
        def to_screen(d, e):
            sx = self.margin_left + ((d - view_start) / visible_range) * plot_w
            sy = self.margin_top + plot_h - ((e - min_elev) / elev_range) * plot_h
            return sx, sy

        # --- 1. Draw Axes ---
        painter.setPen(QPen(Qt.black, 1))
        painter.drawLine(self.margin_left, self.margin_top + plot_h, self.margin_left + plot_w, self.margin_top + plot_h)  # X
        painter.drawLine(self.margin_left, self.margin_top, self.margin_left, self.margin_top + plot_h)  # Y
        
        # Axis Labels
        painter.setFont(QFont("Arial", 8))
        painter.drawText(self.margin_left - 5, height - 10, f"{int(view_start)}")
        painter.drawText(width - self.margin_right - 60, height - 10, f"{int(view_end)}m")
        painter.drawText(5, self.margin_top + plot_h, f"{int(min_elev)}m")
        painter.drawText(5, self.margin_top + 10, f"{int(max_elev)}m")
        
        # Title
        painter.setFont(QFont("Arial", 10, QFont.Bold))
        painter.drawText(self.margin_left, 18, "지형 단면 및 가시선 (Terrain Profile & Line of Sight)")
        
        # --- 2. Calculate Visibility using Max-Angle Algorithm ---
        # Compute visibility status for each profile point
        visibility = []  # True = Visible, False = Hidden
        max_angle = -float('inf')
        start_elev = elevations[0] + self.obs_height
        
        for i, (d, e) in enumerate(zip(distances, elevations)):
            if d == 0:
                visibility.append(True)  # Observer point is always "visible"
                continue
            
            # Angle from observer to this point's terrain surface
            angle = (e - start_elev) / d
            
            if angle >= max_angle:
                max_angle = angle
                visibility.append(True)
            else:
                visibility.append(False)

        # --- 3. Fill Terrain by Visibility (Green/Red) ---
        fill_visible = QColor(0, 200, 0, 70)
        fill_hidden = QColor(255, 0, 0, 70)
        painter.setPen(Qt.NoPen)

        for i in range(len(distances) - 1):
            d1, e1 = distances[i], elevations[i]
            d2, e2 = distances[i + 1], elevations[i + 1]

            if d2 < view_start or d1 > view_end:
                continue

            d1c, e1c = d1, e1
            d2c, e2c = d2, e2

            if d1c < view_start and d2c > d1c:
                t = (view_start - d1c) / (d2c - d1c)
                d1c = view_start
                e1c = e1c + t * (e2c - e1c)

            if d2c > view_end and d2c > d1c:
                t = (view_end - d1c) / (d2c - d1c)
                d2c = view_end
                e2c = e1c + t * (e2c - e1c)

            if d2c <= d1c:
                continue

            x1, y1 = to_screen(d1c, e1c)
            x2, y2 = to_screen(d2c, e2c)
            xb1, yb1 = to_screen(d1c, min_elev)
            xb2, yb2 = to_screen(d2c, min_elev)

            poly = QPolygonF([
                QPointF(xb1, yb1),
                QPointF(x1, y1),
                QPointF(x2, y2),
                QPointF(xb2, yb2),
            ])

            painter.setBrush(QBrush(fill_visible if visibility[i + 1] else fill_hidden))
            painter.drawPolygon(poly)
        
        # --- 4. Draw Visibility Segments on Terrain Surface ---
        pen_visible = QPen(QColor(0, 200, 0), 2.0)  # Green
        pen_hidden = QPen(QColor(255, 0, 0), 2.0)   # Red
        
        for i in range(len(distances) - 1):
            d1, e1 = distances[i], elevations[i]
            d2, e2 = distances[i + 1], elevations[i + 1]

            if d2 < view_start or d1 > view_end:
                continue

            d1c, e1c = d1, e1
            d2c, e2c = d2, e2

            if d1c < view_start and d2c > d1c:
                t = (view_start - d1c) / (d2c - d1c)
                d1c = view_start
                e1c = e1c + t * (e2c - e1c)

            if d2c > view_end and d2c > d1c:
                t = (view_end - d1c) / (d2c - d1c)
                d2c = view_end
                e2c = e1c + t * (e2c - e1c)

            if d2c <= d1c:
                continue

            x1, y1 = to_screen(d1c, e1c)
            x2, y2 = to_screen(d2c, e2c)
            
            # Use status of the endpoint to determine color
            if visibility[i+1]:
                painter.setPen(pen_visible)
            else:
                painter.setPen(pen_hidden)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # Redraw axes on top of fills for readability
        painter.setPen(QPen(Qt.black, 1))
        painter.drawLine(self.margin_left, self.margin_top + plot_h, self.margin_left + plot_w, self.margin_top + plot_h)  # X
        painter.drawLine(self.margin_left, self.margin_top, self.margin_left, self.margin_top + plot_h)  # Y

        # Hover indicator (distance cursor)
        if self.hover_distance is not None and self.hover_elevation is not None:
            hover_x, _ = to_screen(self.hover_distance, min_elev)
            hover_x = max(self.margin_left, min(self.margin_left + plot_w, hover_x))
            hover_y = to_screen(self.hover_distance, self.hover_elevation)[1]

            painter.setPen(QPen(QColor(255, 0, 0, 160), 1, Qt.DashLine))
            painter.drawLine(int(hover_x), self.margin_top, int(hover_x), self.margin_top + plot_h)

            painter.setPen(QPen(QColor(255, 0, 0), 2))
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            painter.drawEllipse(QPointF(hover_x, hover_y), 5, 5)

            painter.setPen(QPen(Qt.black))
            painter.setFont(QFont("Arial", 8))
            painter.drawText(int(hover_x) + 8, int(hover_y) - 6, f"{self.hover_distance:.0f}m")
        
        # --- 6. Draw Sight Line (Dashed Blue) ---
        def sight_elev_at(d):
            frac = (d / max_dist) if max_dist > 0 else 0.0
            return obs_elev + frac * (tgt_elev - obs_elev)

        def draw_sight_segment(d1, d2, color):
            if d2 < view_start or d1 > view_end or d2 <= d1:
                return
            sd1 = max(view_start, d1)
            sd2 = min(view_end, d2)
            if sd2 <= sd1:
                return
            p1 = QPointF(*to_screen(sd1, sight_elev_at(sd1)))
            p2 = QPointF(*to_screen(sd2, sight_elev_at(sd2)))
            painter.setPen(QPen(color, 1, Qt.DashLine))
            painter.drawLine(p1, p2)

        if self.first_obstruction and not self.is_visible_overall:
            obstruction_dist = float(self.first_obstruction.get("distance", 0.0))
            obstruction_dist = max(0.0, min(max_dist, obstruction_dist))

            draw_sight_segment(0.0, obstruction_dist, QColor(0, 100, 255, 150))
            draw_sight_segment(obstruction_dist, max_dist, QColor(255, 0, 0, 150))

            if view_start <= obstruction_dist <= view_end:
                obstruct_screen = to_screen(obstruction_dist, sight_elev_at(obstruction_dist))
                painter.setBrush(QBrush(QColor(255, 0, 0)))
                painter.setPen(QPen(Qt.white, 1))
                painter.drawEllipse(QPointF(*obstruct_screen), 4, 4)
        else:
            draw_sight_segment(0.0, max_dist, QColor(0, 100, 255, 150))
        
        # --- 7. Draw Start (S) and End (E) Markers ---
        painter.setFont(QFont("Arial", 9, QFont.Bold))

        # Observer (Blue Circle with S)
        if view_start <= 0.0 <= view_end:
            obs_screen = to_screen(0.0, obs_elev)
            painter.setBrush(QBrush(QColor(0, 100, 255)))
            painter.setPen(QPen(Qt.white, 1))
            painter.drawEllipse(QPointF(*obs_screen), 8, 8)
            painter.setPen(Qt.white)
            painter.drawText(int(obs_screen[0]) - 4, int(obs_screen[1]) + 4, "S")

        # Target (Orange Circle with E)
        if view_start <= max_dist <= view_end:
            tgt_screen = to_screen(max_dist, tgt_elev)
            painter.setBrush(QBrush(QColor(255, 140, 0)))
            painter.setPen(QPen(Qt.white, 1))
            painter.drawEllipse(QPointF(*tgt_screen), 8, 8)
            painter.setPen(Qt.white)
            painter.drawText(int(tgt_screen[0]) - 4, int(tgt_screen[1]) + 4, "E")
        
        # --- 8. Draw Legend ---
        legend_x = self.margin_left + 10
        legend_y = self.margin_top + 10
        painter.setFont(QFont("Arial", 8))
        
        painter.setPen(pen_visible)
        painter.drawLine(legend_x, legend_y, legend_x + 20, legend_y)
        painter.setPen(Qt.black)
        painter.drawText(legend_x + 25, legend_y + 4, "보임 (Visible)")
        
        painter.setPen(pen_hidden)
        painter.drawLine(legend_x, legend_y + 15, legend_x + 20, legend_y + 15)
        painter.setPen(Qt.black)
        painter.drawText(legend_x + 25, legend_y + 19, "안보임 (Hidden)")


class ViewshedProfilerDialog(QDialog):
    """Dialog to show 2D Viewshed Profile chart"""
    def __init__(
        self,
        iface,
        profile_data,
        obs_height,
        tgt_height,
        total_dist,
        is_visible_overall=True,
        first_obstruction=None,
        line_start_canvas=None,
        line_end_canvas=None,
        parent=None,
    ):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.line_start_canvas = line_start_canvas
        self.line_end_canvas = line_end_canvas
        self.total_dist = float(total_dist) if total_dist is not None else 0.0

        self.setWindowTitle("가시선 프로파일 (Line of Sight Profile)")
        self.setMinimumSize(800, 500)
        
        layout = QVBoxLayout()
        
        # Info Header
        target_visibility = "보임" if is_visible_overall else "안보임"
        obstruction_txt = ""
        if (not is_visible_overall) and first_obstruction and first_obstruction.get('distance') is not None:
            obstruction_txt = f" | <b>장애물:</b> {float(first_obstruction['distance']):.0f}m"

        header = QLabel(
            f"<b>거리:</b> {total_dist:.1f}m"
            f" | <b>관측고:</b> {obs_height}m"
            f" | <b>대상고:</b> {tgt_height}m"
            f" | <b>대상점:</b> {target_visibility}"
            f"{obstruction_txt}"
        )
        header.setStyleSheet("font-size: 14px; padding: 10px; background: #f0f0f0; border-radius: 5px;")
        layout.addWidget(header)

        # Map/Profile synchronization
        sync_layout = QHBoxLayout()
        self.chkSync = QCheckBox("지도-프로파일 연동")
        self.chkSync.setChecked(True)
        self.chkSync.toggled.connect(self._on_sync_toggled)
        sync_layout.addWidget(self.chkSync)
        sync_layout.addStretch()
        layout.addLayout(sync_layout)

        ref = QLabel(
            '참고: <a href="https://github.com/zoran-cuckovic/QGIS-visibility-analysis">Visibility Analysis</a> '
            '(Zoran Čučković) 플러그인의 출력 레이어 구성(Observers/Targets/Viscode)에서 아이디어를 얻었습니다.'
        )
        ref.setOpenExternalLinks(True)
        ref.setStyleSheet("font-size: 11px; color: #555; padding: 0 10px 6px 10px;")
        layout.addWidget(ref)
        
        # Plot area
        self.plot = ProfilePlotWidget(
            profile_data,
            obs_height,
            tgt_height,
            is_visible_overall=is_visible_overall,
            first_obstruction=first_obstruction,
        )
        self.plot.on_hover_callback = self._on_profile_hover
        layout.addWidget(self.plot)

        # Hover marker on map
        self.hover_marker = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        self.hover_marker.setColor(QColor(255, 0, 0))
        self.hover_marker.setWidth(10)
        self.hover_marker.setIcon(QgsRubberBand.ICON_CIRCLE)
        self.hover_marker.hide()

        # Sync map cursor -> profile cursor
        try:
            self.canvas.xyCoordinates.connect(self._on_canvas_xy)
        except Exception:
            pass
        
        # Footer buttons
        btn_layout = QHBoxLayout()
        btn_save = QPushButton("이미지로 저장 (.png)")
        btn_save.clicked.connect(self.save_image)
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.close)
        
        btn_save.setStyleSheet("padding: 8px 15px; font-weight: bold; background: #4CAF50; color: white; border: none;")
        btn_close.setStyleSheet("padding: 8px 15px;")
        
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)
        
        self.setLayout(layout)
        
    def _set_map_marker(self, point):
        if point is None:
            self.hover_marker.reset(QgsWkbTypes.PointGeometry)
            self.hover_marker.hide()
            return

        self.hover_marker.reset(QgsWkbTypes.PointGeometry)
        self.hover_marker.addPoint(point)
        self.hover_marker.show()

    def _on_sync_toggled(self, checked):
        if not checked:
            self.plot.set_hover_distance(None)
            self._set_map_marker(None)

    def _on_profile_hover(self, distance_m):
        if not self.chkSync.isChecked():
            self._set_map_marker(None)
            return

        if distance_m is None or not self.line_start_canvas or not self.line_end_canvas or self.total_dist <= 0:
            self._set_map_marker(None)
            return

        frac = max(0.0, min(1.0, float(distance_m) / self.total_dist))
        sx, sy = self.line_start_canvas.x(), self.line_start_canvas.y()
        ex, ey = self.line_end_canvas.x(), self.line_end_canvas.y()
        point = QgsPointXY(sx + frac * (ex - sx), sy + frac * (ey - sy))
        self._set_map_marker(point)

    def _on_canvas_xy(self, point):
        if not self.chkSync.isChecked():
            return
        if not self.line_start_canvas or not self.line_end_canvas or self.total_dist <= 0:
            return

        sx, sy = self.line_start_canvas.x(), self.line_start_canvas.y()
        ex, ey = self.line_end_canvas.x(), self.line_end_canvas.y()
        vx, vy = (ex - sx), (ey - sy)
        vv = vx * vx + vy * vy
        if vv <= 0:
            return

        px, py = (point.x() - sx), (point.y() - sy)
        t = (px * vx + py * vy) / vv
        t = max(0.0, min(1.0, t))

        proj_x = sx + t * vx
        proj_y = sy + t * vy
        dx = point.x() - proj_x
        dy = point.y() - proj_y

        try:
            units_per_px = float(self.canvas.mapUnitsPerPixel())
        except Exception:
            units_per_px = float(self.canvas.mapSettings().mapUnitsPerPixel())

        tolerance = units_per_px * 8.0
        if (dx * dx + dy * dy) > (tolerance * tolerance):
            self.plot.set_hover_distance(None)
            self._set_map_marker(None)
            return

        self.plot.set_hover_distance(t * self.total_dist)
        self._set_map_marker(QgsPointXY(proj_x, proj_y))

    def closeEvent(self, event):
        try:
            self.canvas.xyCoordinates.disconnect(self._on_canvas_xy)
        except Exception:
            pass
        self._set_map_marker(None)
        super().closeEvent(event)

    def save_image(self):
        filename, _ = QFileDialog.getSaveFileName(self, "이미지 저장", "viewshed_profile.png", "PNG (*.png)")
        if filename:
            # Render the widget to image
            image = QImage(self.plot.size(), QImage.Format_ARGB32)
            # Fill with white background
            image.fill(Qt.white)
            painter = QPainter(image)
            self.plot.render(painter)
            painter.end()
            image.save(filename)
            from qgis.PyQt.QtWidgets import QMessageBox

            QMessageBox.information(self, "저장 완료", f"프로파일 이미지 저장: {filename}")

