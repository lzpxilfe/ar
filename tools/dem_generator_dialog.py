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
import uuid
from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtWidgets import QTableWidgetItem, QCheckBox, QWidget, QHBoxLayout, QFileDialog, QListWidgetItem
from qgis.PyQt.QtCore import Qt, QSize
from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtGui import QIcon
import processing
import tempfile
from .utils import push_message, restore_ui_focus
from .live_log_dialog import ensure_live_log_dialog

# Load the UI file
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'dem_generator_dialog_base.ui'))

class DemGeneratorDialog(QtWidgets.QDialog, FORM_CLASS):
    # Map scale to recommended pixel size (meters)
    # Based on contour interval standards from National Geographic Information Institute
    SCALE_PIXEL_MAP = {
        '1:1,000 (등고선 1m)': 1.0,
        '1:2,500 (등고선 2m)': 2.0, 
        '1:5,000 (등고선 5m)': 5.0,
        '1:25,000 (등고선 10m)': 10.0,
        '1:50,000 (등고선 20m)': 20.0,
        'Custom (사용자 지정)': None
    }
    
    # Interpolation methods with academic citations
    INTERPOLATION_METHODS = {
        'TIN - Linear (선형)': {
            'algorithm': 'qgis:tininterpolation',
            'method': 0,
            'desc': '💡 삼각망 기반 선형 보간. 등고선 데이터에 적합 [Delaunay, 1934]'
        },
        'TIN - Clough-Tocher (곡면)': {
            'algorithm': 'qgis:tininterpolation',
            'method': 1,
            'desc': '💡 삼각망 기반 곡면 보간. 부드러운 지형 표현 [Clough & Tocher, 1965]'
        },
        'IDW (역거리 가중치)': {
            'algorithm': 'qgis:idwinterpolation',
            'method': None,
            'desc': '💡 포인트 데이터에 적합, 등고선에는 비추천 [Shepard, 1968]'
        }
    }
    
    # DXF Layer definitions for Korean digital topographic maps
    # NOTE: Only essential contour lines default=True to avoid bridges/structures
    DXF_LAYER_INFO = {
        # --- 현행 수치지형도(일반적으로 많이 쓰이는 F*** 코드) ---
        'F0017110': {'name': '주곡선', 'desc': '기본 등고선 (5m 간격)', 'category': '등고선', 'default': True},
        'F0017111': {'name': '계곡선', 'desc': '굵은 등고선 (25m 간격)', 'category': '등고선', 'default': True},
        'F0017112': {'name': '간곡선', 'desc': '완만 지형 파선 (선택적)', 'category': '등고선', 'default': False},
        'F0017113': {'name': '조곡선', 'desc': '아주 완만한 지형 점선', 'category': '등고선', 'default': False},
        'F0017114': {'name': '지성선', 'desc': '능선/계곡 변화점', 'category': '지형', 'default': False},
        'F0017115': {'name': '지성선(추가)', 'desc': '지형 굴곡 보조', 'category': '지형', 'default': False},
        'F0017120': {'name': '등고선 수치', 'desc': '등고선 숫자', 'category': '텍스트', 'default': False},
        'F0027111': {'name': '표고점(지형)', 'desc': '순수 지형 높이 (산정상 등)', 'category': '포인트', 'default': True},
        'F0027217': {'name': '표고점(구조물)', 'desc': '⚠️ 교량/구조물 높이 포함 주의!', 'category': '포인트', 'default': False},
        'E0011111': {'name': '하천중심선', 'desc': '하천 물길 (고도값 없을 수 있음)', 'category': '수계', 'default': False},
        'E0011112': {'name': '하천경계선', 'desc': '강물/지면 경계', 'category': '수계', 'default': False},
        'E0041311': {'name': '호수/저수지', 'desc': '수면 경계', 'category': '수계', 'default': False}
        ,
        # --- 구(2000년대 등) 수치지형도: 숫자 레이어 코드 ---
        # 주로 71XX(등고선), 7217(표고점), 73XX(기준점/수치) 형태로 등장합니다.
        # (예) "Layer" IN ('7111','7114','7217' ...)
        "7111": {
            "name": "주곡선(구)",
            "desc": "구 수치지도(숫자 코드) 등고선. (현행 예: CAA002)",
            "category": "구수치(등고선)",
            "default": False,
        },
        "7114": {
            "name": "계곡선(구)",
            "desc": "구 수치지도(숫자 코드) 등고선(계곡선). (현행 예: CAA001)",
            "category": "구수치(등고선)",
            "default": False,
        },
        "7217": {
            "name": "표고점(구)",
            "desc": "구 수치지도(숫자 코드) 표고점. (현행 예: CA002)",
            "category": "구수치(표고점)",
            "default": False,
        },
        "7132": {
            "name": "표고점수치(구)",
            "desc": "구 수치지도(숫자 코드) 표고점 수치(텍스트/표기). DEM 보간에는 보통 불필요",
            "category": "구수치(텍스트)",
            "default": False,
        },
        "2121": {
            "name": "해안선(육지)(구)",
            "desc": "구 수치지도(숫자 코드) 해안선(육지). 해안/수면을 0m 기준으로 쓰고 싶을 때만 선택(주의)",
            "category": "구수치(해안)",
            "default": False,
        },
        "2122": {
            "name": "해안선(섬)(구)",
            "desc": "구 수치지도(숫자 코드) 해안선(섬). 해안/수면 처리를 위해 선택할 수 있음(주의)",
            "category": "구수치(해안)",
            "default": False,
        },
        # --- 일부 데이터셋에서 쓰이는 영문 코드(참고용) ---
        "CAA001": {
            "name": "등고선(계곡선)",
            "desc": "영문 코드 등고선(계곡선). (구 코드 예: 7114)",
            "category": "현행(영문코드)",
            "default": False,
        },
        "CAA002": {
            "name": "등고선(주곡선)",
            "desc": "영문 코드 등고선(주곡선). (구 코드 예: 7111)",
            "category": "현행(영문코드)",
            "default": False,
        },
        "CA002": {
            "name": "표고점",
            "desc": "영문 코드 표고점. (구 코드 예: 7217)",
            "category": "현행(영문코드)",
            "default": False,
        },
        "CA0021": {
            "name": "표고점수치",
            "desc": "영문 코드 표고점수치(표기). DEM 보간에는 보통 불필요",
            "category": "현행(영문코드)",
            "default": False,
        },
    }

    DXF_LAYER_PRESETS = {
        "modern_f": {
            "label": "현행 수치지형도 (F*** 레이어)",
            "codes": ["F0017110", "F0017111", "F0027111"],
            "tooltip": (
                "현행 수치지형도(DXF)에서 많이 보이는 F*** 레이어 프리셋입니다.\n"
                "- 등고선(주곡선/계곡선) + 표고점(지형)만 기본 선택\n"
                "- 교량/구조물 표고점(F0027217)은 기본 제외"
            ),
        },
        "legacy_numeric": {
            "label": "구 수치지형도 (숫자 레이어: 71XX/72XX/73XX)",
            "codes": ["7111", "7114", "7217", "2121", "2122"],
            "tooltip": (
                "구(2000년대 등) 수치지형도에서 레이어 이름이 숫자로 들어오는 경우가 있습니다.\n"
                "예) \"Layer\" IN ('7111','7114','7217','2121','2122')\n"
                "- 71XX: 등고선, 7217: 표고점\n"
                "- 2121/2122(해안선)은 필요할 때만: 해안/수면을 0m 기준으로 강제할 수 있습니다(주의)"
            ),
        },
        "modern_ca": {
            "label": "현행 수치지형도 (영문 코드: CA*/CAA*)",
            "codes": ["CAA001", "CAA002", "CA002"],
            "tooltip": (
                "일부 데이터셋은 등고선/표고점 레이어가 CAA001/CAA002/CA002 같은 영문 코드로 들어옵니다.\n"
                "- 등고선 + 표고점만 기본 선택\n"
                "- (필요 시) CA0021(표고점수치)은 수치 표기라 DEM 보간에는 보통 불필요"
            ),
        },
    }

    
    def __init__(self, iface, parent=None):
        super(DemGeneratorDialog, self).__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self.loaded_dxf_layers = []
        
        # Initialize UI
        self.populate_layers()
        self.populate_scales()
        self.populate_interpolation_methods()
        self.setup_layer_table()
        self.setup_layer_presets()
        self.setup_layer_list()
        
        # Connect signals
        self.cmbScale.currentIndexChanged.connect(self.on_scale_changed)
        self.cmbInterpolation.currentIndexChanged.connect(self.on_interpolation_changed)
        self.btnLoadDxf.clicked.connect(self.load_dxf_file)
        self.btnSelectAll.clicked.connect(self.select_all_layers)
        self.btnDeselectAll.clicked.connect(self.deselect_all_layers)
        self.btnRefreshLayers.clicked.connect(self.populate_layers)
        self.btnRun.clicked.connect(self.run_process)
        self.btnClose.clicked.connect(self.reject)
        
        # Set button icon
        icon_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'dem_icon.png')
        if os.path.exists(icon_path):
            self.btnRun.setIcon(QIcon(icon_path))
            self.btnRun.setIconSize(QSize(32, 32))
    
    def setup_layer_list(self):
        """Setup multi-select layer list with checkboxes"""
        self.listLayers.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.listLayers.itemChanged.connect(self.on_layer_item_changed)
        self._updating_checkboxes = False
    
    def on_layer_item_changed(self, item):
        """When one checkbox is toggled, toggle all selected items too"""
        if self._updating_checkboxes:
            return
        
        self._updating_checkboxes = True
        new_state = item.checkState()
        
        # If this item is in selection, apply to all selected
        selected_items = self.listLayers.selectedItems()
        if item in selected_items:
            for sel_item in selected_items:
                sel_item.setCheckState(new_state)
        
        self._updating_checkboxes = False
    
    def populate_layers(self):
        """Populate layer list with vector layers (checkboxes)"""
        self.listLayers.clear()
        layers = QgsProject.instance().mapLayers().values()
        for layer in layers:
            if layer.type() == layer.VectorLayer:
                item = QListWidgetItem(layer.name())
                item.setData(Qt.UserRole, layer)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.listLayers.addItem(item)
        
        # Auto-check layers containing 'DEM용' in name
        for i in range(self.listLayers.count()):
            item = self.listLayers.item(i)
            if 'DEM용' in item.text() or '등고선' in item.text().lower():
                item.setCheckState(Qt.Checked)
    
    def setup_layer_table(self):
        """Setup the layer selection table with predefined DXF layers"""
        self.tblLayers.setColumnCount(4)
        self.tblLayers.setHorizontalHeaderLabels(['✓', '코드', '명칭', '설명'])
        self.tblLayers.horizontalHeader().setStretchLastSection(True)
        self.tblLayers.setColumnWidth(0, 30)
        self.tblLayers.setColumnWidth(1, 80)
        self.tblLayers.setColumnWidth(2, 100)
        
        self.layer_checkboxes = {}
        row = 0
        self.tblLayers.setRowCount(len(self.DXF_LAYER_INFO))
        
        for layer_code, info in self.DXF_LAYER_INFO.items():
            checkbox = QCheckBox()
            checkbox.setChecked(info['default'])
            checkbox.setToolTip(f"{info['category']}: {info['desc']}")
            self.layer_checkboxes[layer_code] = checkbox
            
            widget = QWidget()
            layout = QHBoxLayout(widget)
            layout.addWidget(checkbox)
            layout.setAlignment(Qt.AlignCenter)
            layout.setContentsMargins(0, 0, 0, 0)
            self.tblLayers.setCellWidget(row, 0, widget)
            
            code_item = QTableWidgetItem(layer_code)
            code_item.setFlags(code_item.flags() & ~Qt.ItemIsEditable)
            self.tblLayers.setItem(row, 1, code_item)
            
            name_item = QTableWidgetItem(info['name'])
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.tblLayers.setItem(row, 2, name_item)
            
            desc_item = QTableWidgetItem(info['desc'])
            desc_item.setFlags(desc_item.flags() & ~Qt.ItemIsEditable)
            self.tblLayers.setItem(row, 3, desc_item)
            
            row += 1

    def setup_layer_presets(self):
        """Add a compact 'layer preset' selector without changing the .ui file."""
        try:
            # horizontalLayout is defined in dem_generator_dialog_base.ui (row with SelectAll/Deselect/Load DXF).
            layout = getattr(self, "horizontalLayout", None)
            if layout is None:
                return

            self.lblLayerPreset = QtWidgets.QLabel("프리셋", self)
            self.cmbLayerPreset = QtWidgets.QComboBox(self)
            self.cmbLayerPreset.setMinimumWidth(220)

            # Add presets
            self.cmbLayerPreset.addItem("프리셋 선택…", "")
            for key, item in self.DXF_LAYER_PRESETS.items():
                self.cmbLayerPreset.addItem(item.get("label", key), key)
                idx = self.cmbLayerPreset.count() - 1
                tip = item.get("tooltip", "")
                if tip:
                    self.cmbLayerPreset.setItemData(idx, tip, Qt.ToolTipRole)

            def _sync_tip():
                try:
                    self.cmbLayerPreset.setToolTip(
                        str(self.cmbLayerPreset.itemData(self.cmbLayerPreset.currentIndex(), Qt.ToolTipRole) or "")
                    )
                except Exception:
                    pass

            self.cmbLayerPreset.currentIndexChanged.connect(self.on_layer_preset_changed)
            self.cmbLayerPreset.currentIndexChanged.connect(_sync_tip)
            _sync_tip()

            # Insert after "선택 해제" (keeps the same row height)
            try:
                idx = int(layout.indexOf(self.btnDeselectAll))
                if idx >= 0:
                    layout.insertWidget(idx + 1, self.lblLayerPreset)
                    layout.insertWidget(idx + 2, self.cmbLayerPreset)
                else:
                    layout.insertWidget(0, self.lblLayerPreset)
                    layout.insertWidget(1, self.cmbLayerPreset)
            except Exception:
                try:
                    layout.insertWidget(0, self.lblLayerPreset)
                    layout.insertWidget(1, self.cmbLayerPreset)
                except Exception:
                    pass
        except Exception:
            pass

    def on_layer_preset_changed(self):
        key = ""
        try:
            key = str(self.cmbLayerPreset.currentData() or "")
        except Exception:
            key = ""

        if not key:
            return

        preset = self.DXF_LAYER_PRESETS.get(key) or {}
        codes = set(preset.get("codes") or [])
        if not codes:
            return

        # Apply: uncheck everything then check preset codes.
        for code, checkbox in (self.layer_checkboxes or {}).items():
            try:
                checkbox.setChecked(str(code) in codes)
            except Exception:
                continue
    
    def select_all_layers(self):
        for checkbox in self.layer_checkboxes.values():
            checkbox.setChecked(True)
    
    def deselect_all_layers(self):
        for checkbox in self.layer_checkboxes.values():
            checkbox.setChecked(False)
    
    def get_selected_layer_codes(self):
        selected = []
        for code, checkbox in self.layer_checkboxes.items():
            if checkbox.isChecked():
                selected.append(code)
        return selected
    
    def load_dxf_file(self):
        """Load multiple DXF files"""
        dxf_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "DXF 파일 선택 (Ctrl+클릭으로 여러 개 선택)",
            "",
            "DXF Files (*.dxf);;All Files (*)"
        )
        
        if not dxf_paths:
            return
        
        selected_codes = self.get_selected_layer_codes()
        if not selected_codes:
            push_message(self.iface, "오류", "최소 하나의 레이어를 선택해주세요", level=2)
            restore_ui_focus(self)
            return
        
        query = '"Layer" IN (' + ','.join([f"'{code}'" for code in selected_codes]) + ')'
        
        total_features = 0
        loaded_count = 0
        
        for dxf_path in dxf_paths:
            try:
                layer_name = os.path.splitext(os.path.basename(dxf_path))[0] + "_DEM용"
                layer = QgsVectorLayer(dxf_path + "|layername=entities", layer_name, "ogr")
                
                if layer.isValid():
                    layer.setSubsetString(query)
                    QgsProject.instance().addMapLayer(layer)
                    self.loaded_dxf_layers.append(layer)
                    total_features += layer.featureCount()
                    loaded_count += 1
                    
            except Exception:
                push_message(self.iface, "경고", f"{os.path.basename(dxf_path)} 로드 실패", level=1)
        
        self.populate_layers()
        
        if loaded_count > 0:
            push_message(self.iface, "성공", f"{loaded_count}개 DXF 로드 완료: 총 {total_features}개 피처", level=0)
    
    def populate_scales(self):
        self.cmbScale.clear()
        for scale in self.SCALE_PIXEL_MAP.keys():
            self.cmbScale.addItem(scale)
        # Default to 1:5,000 (index 2)
        self.cmbScale.setCurrentIndex(2)
        self.on_scale_changed()
    
    def on_scale_changed(self):
        scale = self.cmbScale.currentText()
        recommended = self.SCALE_PIXEL_MAP.get(scale)
        
        if recommended is not None:
            self.spinPixelSize.setValue(recommended)
            self.lblRecommended.setText(f"(권장: {recommended}m)")
        else:
            self.lblRecommended.setText("(직접 입력)")
    
    def populate_interpolation_methods(self):
        self.cmbInterpolation.clear()
        for method_name in self.INTERPOLATION_METHODS.keys():
            self.cmbInterpolation.addItem(method_name)
        self.on_interpolation_changed()
    
    def on_interpolation_changed(self):
        method_name = self.cmbInterpolation.currentText()
        method_info = self.INTERPOLATION_METHODS.get(method_name, {})
        desc = method_info.get('desc', '')
        self.lblInterpDesc.setText(desc)

    def get_selected_layers(self):
        """Get list of checked layers from the list widget"""
        selected_layers = []
        for i in range(self.listLayers.count()):
            item = self.listLayers.item(i)
            if item.checkState() == Qt.Checked:
                layer = item.data(Qt.UserRole)
                if layer:
                    selected_layers.append(layer)
        return selected_layers

    def run_process(self):
        """Run the DEM generation process (v0.7.2: Merge → Filter → Interpolate)"""
        selected_layers = self.get_selected_layers()
        output_path = self.fileOutput.filePath()
        pixel_size = self.spinPixelSize.value()
        
        if not selected_layers:
            push_message(self.iface, "오류", "레이어를 체크해주세요", level=2)
            restore_ui_focus(self)
            return
        if not output_path:
            push_message(self.iface, "오류", "출력 파일 경로를 지정해주세요", level=2)
            restore_ui_focus(self)
            return

        # Live log window (non-modal) so users can see progress in real time.
        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)

        method_name = self.cmbInterpolation.currentText()
        method_info = self.INTERPOLATION_METHODS.get(method_name, {})
        algorithm = method_info.get('algorithm', 'qgis:tininterpolation')
        method_param = method_info.get('method')
        
        # Build query for DXF layer filtering
        selected_codes = self.get_selected_layer_codes()
        
        # Auto-exclude bridge/structure elevation points
        BRIDGE_CODES = ['F0027217']  # 교량/구조물 표고점
        excluded = len([c for c in selected_codes if c in BRIDGE_CODES])
        filtered_codes = [c for c in selected_codes if c not in BRIDGE_CODES]
        
        if filtered_codes:
            query = '"Layer" IN (' + ','.join([f"'{code}'" for code in filtered_codes]) + ')'
        else:
            query = None
        
        # Notify if bridge points were excluded
        if excluded > 0:
            push_message(self.iface, "알림", f"교량/구조물 표고점 {excluded}개 유형 자동 제외됨", level=0)
        
        push_message(self.iface, "처리 중", f"{len(selected_layers)}개 레이어 병합 중...", level=0)
        self.hide()
        QtWidgets.QApplication.processEvents()
        
        try:
            temp_merged = None
            
            # Step 1: Merge all selected layers into one temp file
            if len(selected_layers) > 1:
                temp_merged = os.path.join(tempfile.gettempdir(), f'archtoolkit_merged_{uuid.uuid4().hex[:8]}.gpkg')
                processing.run("native:mergevectorlayers", {
                    'LAYERS': selected_layers,
                    'CRS': selected_layers[0].crs(),
                    'OUTPUT': temp_merged
                })
                merged_layer = QgsVectorLayer(temp_merged, "merged", "ogr")
            else:
                merged_layer = selected_layers[0]
            
            if not merged_layer or not merged_layer.isValid():
                push_message(self.iface, "오류", "레이어 병합에 실패했습니다.", level=2)
                restore_ui_focus(self)
                return
            
            # Step 2: Apply query filter
            if query and merged_layer.fields().indexFromName('Layer') >= 0:
                merged_layer.setSubsetString(query)
            
            # Step 3: Find Z field
            z_field_idx = -1
            for fn in ['Z_COORD', 'z_coord', 'Elevation', 'ELEVATION', 'z_first']:
                idx = merged_layer.fields().indexFromName(fn)
                if idx >= 0:
                    z_field_idx = idx
                    break
            
            geom_type = merged_layer.geometryType()
            interp_type = 0 if geom_type == 0 else 1
            
            # Use source() for file-based layer
            source_path = merged_layer.source()
            
            if z_field_idx >= 0:
                interp_data = f'{source_path}::~::0::~::{z_field_idx}::~::{interp_type}'
            else:
                interp_data = f'{source_path}::~::1::~::0::~::{interp_type}'
            
            combined_extent = merged_layer.extent()


            
            params = {
                'INTERPOLATION_DATA': interp_data,
                'EXTENT': combined_extent,
                'PIXEL_SIZE': pixel_size,
                'OUTPUT': output_path
            }
            if method_param is not None:
                params['METHOD'] = method_param
            
            push_message(self.iface, "처리 중", "TIN 보간 실행 중...", level=0)
            QtWidgets.QApplication.processEvents()
            
            # Step 4: Run TIN interpolation
            result = processing.run(algorithm, params)
            
            # Add result to map
            if result and os.path.exists(output_path):
                self.iface.addRasterLayer(output_path, "생성된 DEM")
                push_message(self.iface, "완료", f"DEM 생성 완료! ({len(selected_layers)}개 레이어 병합)", level=0)
                self.accept()
            else:
                push_message(self.iface, "오류", "DEM이 생성되지 않았습니다.", level=2)
                restore_ui_focus(self)
            
        except Exception as e:
            push_message(self.iface, "오류", f"처리 중 오류: {str(e)}", level=2)
            restore_ui_focus(self)
        finally:
            if temp_merged and os.path.exists(temp_merged):
                from .utils import cleanup_files
                cleanup_files([temp_merged])






