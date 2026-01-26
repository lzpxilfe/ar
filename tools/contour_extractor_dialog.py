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
import os
import tempfile
import uuid
from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject, QgsVectorLayer, QgsMapLayerProxyModel
import processing
from .utils import push_message
from .live_log_dialog import ensure_live_log_dialog

# Load the UI file
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'contour_extractor_dialog_base.ui'))


class ContourExtractorDialog(QtWidgets.QDialog, FORM_CLASS):
    # Contour layer codes
    CONTOUR_CODES = {
        'F0017110': '주곡선',
        'F0017111': '계곡선',
        'F0017112': '간곡선',
        'F0017113': '조곡선'
    }
    
    def __init__(self, iface, parent=None):
        super(ContourExtractorDialog, self).__init__(parent)
        self.setupUi(self)
        self.iface = iface
        
        # Store original filters for undo
        self.original_filters = {}
        
        # Setup layer filters for DEM mode
        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)
        
        # Connect signals
        self.radioDxf.toggled.connect(self.on_mode_changed)
        self.btnRun.clicked.connect(self.run_process)
        self.btnClose.clicked.connect(self.reject)
        self.btnRefreshLayers.clicked.connect(self.refresh_layer_list)
        self.btnResetFilter.clicked.connect(self.reset_filters)
        
        # Initial state
        self.on_mode_changed()
        self.refresh_layer_list()
    
    def refresh_layer_list(self):
        """Populate the vector layer list"""
        self.listDxfLayers.clear()
        
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                item = QtWidgets.QListWidgetItem(layer.name())
                item.setData(Qt.UserRole, layer.id())
                self.listDxfLayers.addItem(item)
    
    def on_mode_changed(self):
        """Toggle visibility of mode panels"""
        is_dxf = self.radioDxf.isChecked()
        self.groupDxf.setEnabled(is_dxf)
        self.groupDem.setEnabled(not is_dxf)
    
    def get_selected_contour_codes(self):
        """Get list of selected contour codes"""
        codes = []
        if self.chkContour1.isChecked():
            codes.append('F0017110')
        if self.chkContour2.isChecked():
            codes.append('F0017111')
        if self.chkContour3.isChecked():
            codes.append('F0017112')
        if self.chkContour4.isChecked():
            codes.append('F0017113')
        return codes
    
    def get_selected_layers(self):
        """Get list of selected vector layers"""
        layers = []
        for item in self.listDxfLayers.selectedItems():
            layer_id = item.data(Qt.UserRole)
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer:
                layers.append(layer)
        return layers
    
    def run_process(self):
        """Run the contour extraction process"""
        # Live log window (non-modal) so users can see progress in real time.
        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)
        if self.radioDxf.isChecked():
            self.extract_from_dxf()
        else:
            self.extract_from_dem()
    
    def extract_from_dxf(self):
        """Extract contours by filtering DXF layers (supports multiple layers)"""
        layers = self.get_selected_layers()
        if not layers:
            push_message(self.iface, "오류", "벡터 레이어를 선택해주세요", level=2)
            return
        
        codes = self.get_selected_contour_codes()
        if not codes:
            push_message(self.iface, "오류", "추출할 등고선 유형을 선택해주세요", level=2)
            return
        
        try:
            # Build query
            query = '"Layer" IN (' + ','.join([f"'{c}'" for c in codes]) + ')'
            
            filtered_count = 0
            for layer in layers:
                # Store original filter for undo
                if layer.id() not in self.original_filters:
                    self.original_filters[layer.id()] = layer.subsetString()
                
                # Apply filter
                layer.setSubsetString(query)
                filtered_count += 1
            
            push_message(
                self.iface, "완료", 
                f"{filtered_count}개 레이어에 등고선 필터 적용 완료 ({len(codes)}개 유형)", 
                level=0
            )
            self.accept()
            
        except Exception as e:
            self.iface.messageBar().pushMessage("오류", f"처리 중 오류: {str(e)}", level=2)
    
    def reset_filters(self):
        """Reset filters on selected layers (undo)"""
        layers = self.get_selected_layers()
        if not layers:
            # If no selection, reset all stored filters
            reset_count = 0
            for layer_id, original_filter in self.original_filters.items():
                layer = QgsProject.instance().mapLayer(layer_id)
                if layer:
                    layer.setSubsetString(original_filter)
                    reset_count += 1
            self.original_filters.clear()
            self.iface.messageBar().pushMessage("완료", f"{reset_count}개 레이어 필터 초기화 완료", level=0)
        else:
            # Reset only selected layers
            reset_count = 0
            for layer in layers:
                if layer.id() in self.original_filters:
                    layer.setSubsetString(self.original_filters[layer.id()])
                    del self.original_filters[layer.id()]
                else:
                    layer.setSubsetString("")
                reset_count += 1
            self.iface.messageBar().pushMessage("완료", f"{reset_count}개 레이어 필터 초기화 완료", level=0)
    
    def extract_from_dem(self):
        """Generate contours from DEM raster"""
        
        layer = self.cmbDemLayer.currentLayer()
        if not layer:
            self.iface.messageBar().pushMessage("오류", "DEM 래스터를 선택해주세요", level=2)
            return
        
        interval = self.spinInterval.value()
        
        try:
            self.iface.messageBar().pushMessage("처리 중", "등고선 생성 중...", level=0)
            self.hide()
            QtWidgets.QApplication.processEvents()
            
            # Use temp file instead of memory layer
            run_id = uuid.uuid4().hex[:8]
            temp_output = os.path.join(tempfile.gettempdir(), f'archtoolkit_contour_{interval}m_{run_id}.gpkg')
            
            # Run GDAL contour
            result = processing.run("gdal:contour", {
                'INPUT': layer.source(),
                'BAND': 1,
                'INTERVAL': interval,
                'FIELD_NAME': 'ELEV',
                'CREATE_3D': False,
                'IGNORE_NODATA': True,
                'NODATA': None,
                'OUTPUT': temp_output
            })
            
            # Add result to map
            if result and os.path.exists(temp_output):
                output_layer = QgsVectorLayer(temp_output, f"등고선_{interval}m", "ogr")
                if output_layer.isValid():
                    QgsProject.instance().addMapLayer(output_layer)
                    self.iface.messageBar().pushMessage("완료", f"등고선 생성 완료 (간격: {interval}m)", level=0)
                    self.accept()
                else:
                    self.iface.messageBar().pushMessage("오류", "등고선 레이어를 로드할 수 없습니다.", level=2)
                    self.show()
            else:
                self.iface.messageBar().pushMessage("오류", "등고선 생성에 실패했습니다.", level=2)
                self.show()
            
        except Exception as e:
            self.iface.messageBar().pushMessage("오류", f"처리 중 오류: {str(e)}", level=2)
            self.show()
