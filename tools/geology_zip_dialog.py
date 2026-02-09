# -*- coding: utf-8 -*-
"""
KIGAM 1:50,000 geology map ZIP loader + vector->raster conversion (MaxEnt-ready).
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
import csv
from typing import Dict, List, Optional, Tuple

import processing
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor, QFont, QIcon
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFillSymbol,
    QgsLayerTreeGroup,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsProject,
    QgsRasterFillSymbolLayer,
    QgsRasterMarkerSymbolLayer,
    QgsRendererCategory,
    QgsTextFormat,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)

from .help_dialog import show_help_dialog
from .live_log_dialog import ensure_live_log_dialog
from .utils import (
    log_message,
    push_message,
    restore_ui_focus,
    set_archtoolkit_layer_metadata,
    new_run_id,
)


PARENT_GROUP_NAME = "ArchToolkit - Geology"


def _safe_name(name: str) -> str:
    base = str(name or "").strip()
    if not base:
        return "layer"
    base = re.sub(r"[\\/:*?\"<>|]+", "_", base)
    base = re.sub(r"\s+", "_", base).strip("_")
    return base or "layer"


def _ensure_output_extension(path: str, fmt: str) -> str:
    p = str(path or "").strip()
    if not p:
        return p
    fmt0 = str(fmt or "").strip().lower()
    desired_ext = ".tif" if fmt0 == "tif" else ".asc" if fmt0 == "asc" else ""
    if not desired_ext:
        return p

    # Strip known raster extensions repeatedly (handles accidental double extensions like ".tif.asc").
    root = p
    while True:
        root2, ext = os.path.splitext(root)
        if ext.lower() in (".tif", ".tiff", ".asc"):
            root = root2
            continue
        break

    return root + desired_ext

class KigamZipProcessor:
    def __init__(self):
        self.extract_root = os.path.join(tempfile.gettempdir(), "ArchToolkit_KIGAM_Extract")
        try:
            os.makedirs(self.extract_root, exist_ok=True)
        except Exception:
            pass

    def process_zip(
        self,
        zip_path: str,
        *,
        font_family: str,
        font_size: int,
        apply_style: bool = True,
        apply_labels: bool = True,
        run_id: str,
    ) -> List[QgsVectorLayer]:
        zip_basename = os.path.splitext(os.path.basename(zip_path))[0]
        extract_dir = os.path.join(self.extract_root, zip_basename)

        try:
            if os.path.exists(extract_dir):
                shutil.rmtree(extract_dir)
            os.makedirs(extract_dir, exist_ok=True)
        except Exception:
            pass

        # Extract ZIP
        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(extract_dir)
        except Exception as e:
            log_message(f"KIGAM ZIP 추출 실패: {e}", level=Qgis.Warning)
            return []

        # Locate 'sym' folder (optional)
        sym_path = None
        for root, dirs, _ in os.walk(extract_dir):
            if "sym" in dirs:
                sym_path = os.path.join(root, "sym")
                break

        if apply_style and not sym_path:
            log_message("KIGAM ZIP에 'sym' 폴더가 없습니다. 심볼 적용은 건너뜁니다.", level=Qgis.Warning)

        loaded_layers: List[QgsVectorLayer] = []
        for root, _, files in os.walk(extract_dir):
            for fname in files:
                if not fname.lower().endswith(".shp"):
                    continue
                shp_path = os.path.join(root, fname)
                layer_name = os.path.splitext(fname)[0]

                layer = QgsVectorLayer(shp_path, layer_name, "ogr")
                try:
                    layer.setProviderEncoding("cp949")
                except Exception:
                    pass
                if not layer.isValid():
                    log_message(f"KIGAM 레이어 로드 실패: {shp_path}", level=Qgis.Warning)
                    continue

                QgsProject.instance().addMapLayer(layer, False)
                loaded_layers.append(layer)

                if apply_style and sym_path:
                    try:
                        self.apply_sym_styling(layer, sym_path)
                    except Exception as e:
                        log_message(f"KIGAM 스타일 적용 실패: {layer.name()} ({e})", level=Qgis.Warning)

                if apply_labels and ("Litho" in layer_name or "LITHO" in layer_name):
                    try:
                        self.apply_labeling(layer, font_family, font_size)
                    except Exception:
                        pass

                try:
                    set_archtoolkit_layer_metadata(
                        layer,
                        tool_id="kigam_zip",
                        run_id=run_id,
                        kind="vector",
                        params={"zip": os.path.basename(zip_path)},
                    )
                except Exception:
                    pass

        self.organize_layers(loaded_layers, zip_basename)
        return loaded_layers

    def apply_sym_styling(self, layer: QgsVectorLayer, sym_path: str) -> None:
        sym_files = {
            os.path.splitext(f)[0]: os.path.join(sym_path, f)
            for f in os.listdir(sym_path)
            if f.lower().endswith(".png")
        }
        if not sym_files:
            return

        # Find best matching field
        best_field = None
        max_matches = 0
        priority_fields = ["LITHOIDX", "TYPE", "ASGN_CODE", "SIGN", "CODE", "AGEIDX"]
        all_fields = [f.name() for f in layer.fields()]
        sorted_fields = [f for f in priority_fields if f in all_fields] + [f for f in all_fields if f not in priority_fields]

        for field_name in sorted_fields:
            idx = layer.fields().indexOf(field_name)
            try:
                unique_values = layer.uniqueValues(idx)
            except Exception:
                unique_values = set()
            matches = 0
            for val in unique_values:
                if str(val) in sym_files:
                    matches += 1
            if matches > max_matches:
                max_matches = matches
                best_field = field_name

        if not best_field:
            return

        categories = []
        unique_values = layer.uniqueValues(layer.fields().indexOf(best_field))
        for val in unique_values:
            val_str = str(val)
            symbol = None

            if val_str in sym_files:
                png_path = sym_files[val_str]
                if layer.geometryType() == QgsWkbTypes.PointGeometry:
                    symbol_layer = QgsRasterMarkerSymbolLayer(png_path)
                    symbol_layer.setSize(6)
                    symbol = QgsMarkerSymbol()
                    symbol.changeSymbolLayer(0, symbol_layer)
                elif layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                    symbol_layer = QgsRasterFillSymbolLayer()
                    symbol_layer.setImageFilePath(png_path)
                    symbol_layer.setWidth(10.0)
                    symbol = QgsFillSymbol()
                    symbol.changeSymbolLayer(0, symbol_layer)

            if symbol:
                categories.append(QgsRendererCategory(val, symbol, val_str))
            else:
                if layer.geometryType() == QgsWkbTypes.PointGeometry:
                    symbol = QgsMarkerSymbol.createSimple({"color": "#ff0000"})
                elif layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                    symbol = QgsFillSymbol.createSimple({"color": "#cccccc", "outline_color": "black"})
                else:
                    continue
                categories.append(QgsRendererCategory(val, symbol, val_str))

        if categories:
            renderer = QgsCategorizedSymbolRenderer(best_field, categories)
            layer.setRenderer(renderer)
            layer.triggerRepaint()

    def apply_labeling(self, layer: QgsVectorLayer, font_family: str, font_size: int) -> None:
        settings = QgsPalLayerSettings()
        fields = [f.name() for f in layer.fields()]
        label_field = "LITHOIDX" if "LITHOIDX" in fields else "LITHONAME" if "LITHONAME" in fields else fields[0]
        settings.fieldName = label_field
        text_format = QgsTextFormat()
        text_format.setFont(QFont(font_family))
        text_format.setSize(int(font_size))
        text_format.setColor(QColor("black"))
        settings.setFormat(text_format)
        settings.placement = QgsPalLayerSettings.Horizontal
        settings.centroidInside = True
        settings.fitInPolygonOnly = True
        settings.priority = 5
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)

    def organize_layers(self, layers: List[QgsVectorLayer], group_name: str) -> None:
        if not layers:
            return
        root = QgsProject.instance().layerTreeRoot()
        parent = root.findGroup(PARENT_GROUP_NAME)
        if parent is None:
            parent = root.insertGroup(0, PARENT_GROUP_NAME)
        run_group = parent.insertGroup(0, f"KIGAM_{group_name}")

        def _priority(layer: QgsVectorLayer) -> int:
            name = (layer.name() or "").lower()
            geom = layer.geometryType()

            # Reference / sheet helpers
            if "frame" in name:
                return 0

            # Polygons should sit below linework so labels/lines aren't hidden by fills.
            if "litho" in name:
                return 30
            if geom == QgsWkbTypes.PolygonGeometry:
                return 25

            # Linework (top)
            if geom == QgsWkbTypes.LineGeometry:
                if "crosssection" in name:
                    return 55
                if "boundary" in name:
                    return 50
                if "foliation" in name:
                    return 45
                if "schistosity" in name:
                    return 44
                return 40

            # Points (very top)
            if geom == QgsWkbTypes.PointGeometry:
                return 60

            return 10

        def _hide_by_default(layer: QgsVectorLayer) -> bool:
            name = (layer.name() or "").lower()
            return ("frame" in name) or ("crosssection" in name)

        scored: List[Tuple[int, int, QgsVectorLayer]] = []
        for i, layer in enumerate(layers):
            scored.append((_priority(layer), i, layer))
        # Display order (top->bottom): higher priority first, stable by original order.
        scored.sort(key=lambda x: (-x[0], x[1]))

        # insertLayer(0, ...) builds the list from bottom->top, so iterate reversed.
        for _, __, layer in reversed(scored):
            node = run_group.insertLayer(0, layer)
            if _hide_by_default(layer):
                try:
                    node.setItemVisibilityChecked(False)
                except Exception:
                    pass
        run_group.setExpanded(True)
        parent.setExpanded(True)


class GeologyZipDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("지질도 도엽 ZIP 불러오기 / MaxEnt 래스터 변환 - ArchToolkit")
        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            icon_path = os.path.join(plugin_dir, "geochem.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except Exception:
            pass

        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel(
            "<b>KIGAM 1:50,000 지질도 ZIP 불러오기</b> + <b>벡터→래스터 변환</b><br>"
            "지질도 도엽 ZIP을 바로 로드하고, MaxEnt 같은 예측 모델링용 래스터로 변환합니다."
        )
        header.setWordWrap(True)
        header.setStyleSheet("background:#e3f2fd; padding:10px; border:1px solid #bbdefb; border-radius:4px;")
        layout.addWidget(header)

        # 1) ZIP loader
        grp_zip = QtWidgets.QGroupBox("1. 지질도 ZIP 불러오기 (KIGAM 1:50,000)")
        form_zip = QtWidgets.QFormLayout(grp_zip)

        self.txtZip = QtWidgets.QLineEdit()
        self.txtZip.setPlaceholderText("ZIP 파일을 선택하거나 경로를 입력하세요…")
        btn_browse = QtWidgets.QPushButton("찾기…")
        btn_browse.clicked.connect(self._browse_zip)
        row_zip = QtWidgets.QHBoxLayout()
        row_zip.addWidget(self.txtZip, 1)
        row_zip.addWidget(btn_browse)
        form_zip.addRow("ZIP 파일:", row_zip)

        self.cmbFont = QtWidgets.QFontComboBox()
        form_zip.addRow("라벨 글꼴:", self.cmbFont)

        self.spinFontSize = QtWidgets.QSpinBox()
        self.spinFontSize.setRange(5, 50)
        self.spinFontSize.setValue(10)
        form_zip.addRow("라벨 크기:", self.spinFontSize)

        self.chkApplyStyle = QtWidgets.QCheckBox("표준 심볼(sym 폴더) 적용")
        self.chkApplyStyle.setChecked(True)
        self.chkApplyLabels = QtWidgets.QCheckBox("지층 코드 라벨 적용")
        self.chkApplyLabels.setChecked(True)
        form_zip.addRow("", self.chkApplyStyle)
        form_zip.addRow("", self.chkApplyLabels)

        self.btnLoadZip = QtWidgets.QPushButton("ZIP 불러오기")
        self.btnLoadZip.clicked.connect(self._load_zip)
        form_zip.addRow("", self.btnLoadZip)

        layout.addWidget(grp_zip)

        # 2) Rasterize for MaxEnt
        grp_rst = QtWidgets.QGroupBox("2. 벡터 → 래스터 (MaxEnt/예측모델)")
        vbox = QtWidgets.QVBoxLayout(grp_rst)
        vbox.addWidget(QtWidgets.QLabel("변환할 벡터 레이어를 선택하세요:"))

        row_filter = QtWidgets.QHBoxLayout()
        row_filter.addWidget(QtWidgets.QLabel("필터:"))
        self.chkKigamOnly = QtWidgets.QCheckBox("KIGAM ZIP 레이어만")
        self.chkKigamOnly.setChecked(True)
        self.chkKigamOnly.setToolTip("ArchToolkit의 KIGAM ZIP 로더로 불러온 레이어만 목록에 표시합니다.")
        self.chkLithoOnly = QtWidgets.QCheckBox("Litho(폴리곤)만")
        self.chkLithoOnly.setChecked(True)
        self.chkLithoOnly.setToolTip("보통 예측모델링에는 Litho(암상/지층) 폴리곤만 있으면 충분합니다.")
        self.chkKigamOnly.stateChanged.connect(self.refresh_layer_list)
        self.chkLithoOnly.stateChanged.connect(self.refresh_layer_list)
        row_filter.addWidget(self.chkKigamOnly)
        row_filter.addWidget(self.chkLithoOnly)
        row_filter.addStretch(1)
        vbox.addLayout(row_filter)

        self.lstLayers = QtWidgets.QListWidget()
        self.lstLayers.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.lstLayers.itemChanged.connect(self._refresh_field_choices)
        vbox.addWidget(self.lstLayers)

        row_refresh = QtWidgets.QHBoxLayout()
        btn_refresh = QtWidgets.QPushButton("레이어 목록 새로고침")
        btn_refresh.clicked.connect(self.refresh_layer_list)
        row_refresh.addWidget(btn_refresh)
        row_refresh.addStretch(1)
        vbox.addLayout(row_refresh)

        form_rst = QtWidgets.QFormLayout()
        self.cmbField = QtWidgets.QComboBox()
        self.cmbField.addItem("(자동 선택)")
        form_rst.addRow("값 필드:", self.cmbField)

        self.spinPixel = QtWidgets.QDoubleSpinBox()
        self.spinPixel.setRange(0.1, 10000.0)
        self.spinPixel.setSingleStep(1.0)
        self.spinPixel.setValue(10.0)
        self.spinPixel.setSuffix(" m")
        form_rst.addRow("해상도(픽셀 크기):", self.spinPixel)

        self.spinNoData = QtWidgets.QDoubleSpinBox()
        self.spinNoData.setRange(-9999999.0, 9999999.0)
        self.spinNoData.setDecimals(2)
        self.spinNoData.setValue(-9999.0)
        form_rst.addRow("NoData 값:", self.spinNoData)

        vbox.addLayout(form_rst)

        self.radMerge = QtWidgets.QRadioButton("선택 레이어 병합 후 단일 래스터")
        self.radPerLayer = QtWidgets.QRadioButton("레이어별 래스터 출력")
        self.radMerge.setChecked(True)
        self.radMerge.toggled.connect(self._toggle_output_mode)
        vbox.addWidget(self.radMerge)
        vbox.addWidget(self.radPerLayer)

        # Output path (single)
        self.txtOutFile = QtWidgets.QLineEdit()
        btn_out_file = QtWidgets.QPushButton("저장 위치…")
        btn_out_file.clicked.connect(self._browse_out_file)
        row_out = QtWidgets.QHBoxLayout()
        row_out.addWidget(self.txtOutFile, 1)
        row_out.addWidget(btn_out_file)
        vbox.addWidget(QtWidgets.QLabel("출력 파일(단일 모드):"))
        vbox.addLayout(row_out)

        # Output dir (per-layer)
        self.txtOutDir = QtWidgets.QLineEdit()
        btn_out_dir = QtWidgets.QPushButton("폴더 선택…")
        btn_out_dir.clicked.connect(self._browse_out_dir)
        row_dir = QtWidgets.QHBoxLayout()
        row_dir.addWidget(self.txtOutDir, 1)
        row_dir.addWidget(btn_out_dir)
        vbox.addWidget(QtWidgets.QLabel("출력 폴더(레이어별 모드):"))
        vbox.addLayout(row_dir)

        self.cmbFormat = QtWidgets.QComboBox()
        self.cmbFormat.addItem("GeoTIFF (*.tif)", "tif")
        self.cmbFormat.addItem("ASCII Grid (*.asc)", "asc")
        vbox.addWidget(QtWidgets.QLabel("출력 형식:"))
        vbox.addWidget(self.cmbFormat)

        self.btnRasterize = QtWidgets.QPushButton("래스터 변환 실행")
        self.btnRasterize.clicked.connect(self._run_rasterize)
        vbox.addWidget(self.btnRasterize)

        layout.addWidget(grp_rst)

        # Bottom buttons
        row_bottom = QtWidgets.QHBoxLayout()
        self.btnHelp = QtWidgets.QPushButton("도움말")
        self.btnHelp.clicked.connect(self._on_help)
        self.btnClose = QtWidgets.QPushButton("닫기")
        self.btnClose.clicked.connect(self.reject)
        row_bottom.addWidget(self.btnHelp)
        row_bottom.addStretch(1)
        row_bottom.addWidget(self.btnClose)
        layout.addLayout(row_bottom)

        self.resize(720, 760)
        self.refresh_layer_list()
        self._toggle_output_mode()

    def _browse_zip(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "KIGAM ZIP 파일 선택", "", "ZIP Files (*.zip *.ZIP)"
        )
        if path:
            self.txtZip.setText(path)

    def _browse_out_file(self):
        ext = self.cmbFormat.currentData() or "tif"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "래스터 저장", "", f"GeoTIFF (*.tif);;ASCII Grid (*.asc)"
        )
        if path:
            self.txtOutFile.setText(path)

    def _browse_out_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "출력 폴더 선택", "")
        if path:
            self.txtOutDir.setText(path)

    def _toggle_output_mode(self):
        is_merge = self.radMerge.isChecked()
        self.txtOutFile.setEnabled(is_merge)
        # find browse button by sibling layout
        for w in self.findChildren(QtWidgets.QPushButton):
            if w.text() == "저장 위치…":
                w.setEnabled(is_merge)
            if w.text() == "폴더 선택…":
                w.setEnabled(not is_merge)
        self.txtOutDir.setEnabled(not is_merge)

    def _kigam_region_for_layer(self, layer: QgsVectorLayer) -> str:
        try:
            root = QgsProject.instance().layerTreeRoot()
            node = root.findLayer(layer.id())
            while node is not None:
                parent = node.parent()
                if parent is None:
                    break
                if isinstance(parent, QgsLayerTreeGroup):
                    name = str(parent.name() or "").strip()
                    if name.startswith("KIGAM_"):
                        return name[len("KIGAM_") :].strip()
                node = parent
        except Exception:
            pass
        return ""

    def refresh_layer_list(self):
        checked_ids = set()
        try:
            for i in range(self.lstLayers.count()):
                it = self.lstLayers.item(i)
                if it is not None and it.checkState() == Qt.Checked:
                    checked_ids.add(it.data(Qt.UserRole))
        except Exception:
            checked_ids = set()

        self.lstLayers.blockSignals(True)
        self.lstLayers.clear()
        layers = list(QgsProject.instance().mapLayers().values())

        kigam_only = True
        litho_only = True
        try:
            kigam_only = bool(self.chkKigamOnly.isChecked())
            litho_only = bool(self.chkLithoOnly.isChecked())
        except Exception:
            pass

        scored = []
        for layer in layers:
            if not isinstance(layer, QgsVectorLayer):
                continue
            if kigam_only:
                try:
                    tool_id = str(layer.customProperty("archtoolkit/tool_id", "") or "").strip()
                    if tool_id != "kigam_zip":
                        continue
                except Exception:
                    continue

            geom = layer.geometryType()
            if litho_only:
                if geom != QgsWkbTypes.PolygonGeometry:
                    continue
                try:
                    lname = str(layer.name() or "").lower()
                    fields_up = {str(f.name() or "").upper() for f in layer.fields()}
                    if ("litho" not in lname) and ("LITHOIDX" not in fields_up) and ("LITHONAME" not in fields_up):
                        continue
                except Exception:
                    continue

            region = self._kigam_region_for_layer(layer)
            scored.append((region, str(layer.name() or ""), layer, geom))

        scored.sort(key=lambda x: (x[0], x[1]))
        for region, layer_name, layer, geom in scored:
            shown_name = layer_name
            if region:
                shown_name = f"[{region}] {layer_name}"
            item = QtWidgets.QListWidgetItem(layer.name())
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if layer.id() in checked_ids else Qt.Unchecked)
            item.setData(Qt.UserRole, layer.id())
            tip = []
            if geom == QgsWkbTypes.PointGeometry:
                tip.append("포인트 레이어")
            elif geom == QgsWkbTypes.LineGeometry:
                tip.append("라인 레이어")
            elif geom == QgsWkbTypes.PolygonGeometry:
                tip.append("폴리곤 레이어")
            if region:
                tip.append(f"지역/도엽: {region}")
            if tip:
                item.setToolTip(" / ".join(tip))
            item.setText(shown_name)
            self.lstLayers.addItem(item)
        self.lstLayers.blockSignals(False)
        self._refresh_field_choices()

    def _selected_vector_layers(self) -> List[QgsVectorLayer]:
        out: List[QgsVectorLayer] = []
        layer_map = QgsProject.instance().mapLayers()
        for i in range(self.lstLayers.count()):
            item = self.lstLayers.item(i)
            if item.checkState() != Qt.Checked:
                continue
            lid = item.data(Qt.UserRole)
            layer = layer_map.get(lid)
            if isinstance(layer, QgsVectorLayer) and layer.isValid():
                out.append(layer)
        return out

    def _refresh_field_choices(self, *_):
        layers = self._selected_vector_layers()
        fields = set()
        if layers:
            for lyr in layers:
                for f in lyr.fields():
                    fields.add(f.name())
        else:
            layer_map = QgsProject.instance().mapLayers()
            for i in range(self.lstLayers.count()):
                item = self.lstLayers.item(i)
                if item is None:
                    continue
                lid = item.data(Qt.UserRole)
                lyr = layer_map.get(lid)
                if isinstance(lyr, QgsVectorLayer):
                    for f in lyr.fields():
                        fields.add(f.name())

        current = self.cmbField.currentText()
        self.cmbField.blockSignals(True)
        self.cmbField.clear()
        self.cmbField.addItem("(자동 선택)")
        for name in sorted(fields):
            self.cmbField.addItem(name)
        if current:
            idx = self.cmbField.findText(current)
            if idx >= 0:
                self.cmbField.setCurrentIndex(idx)
        self.cmbField.blockSignals(False)

    def _choose_common_field(self, layers: List[QgsVectorLayer]) -> Optional[str]:
        if not layers:
            return None
        chosen = self.cmbField.currentText().strip()
        if chosen and chosen != "(자동 선택)":
            if all(lyr.fields().indexOf(chosen) >= 0 for lyr in layers):
                return chosen
        # Auto: prefer common fields
        common = set(f.name() for f in layers[0].fields())
        for lyr in layers[1:]:
            common &= set(f.name() for f in lyr.fields())
        if not common:
            return None
        priority = ["LITHOIDX", "AGEIDX", "LITHONAME", "TYPE", "ASGN_CODE", "SIGN", "CODE"]
        for p in priority:
            if p in common:
                return p
        return sorted(common)[0]

    def _suggest_label_field(self, layer: QgsVectorLayer, field_name: str) -> Optional[str]:
        try:
            fields = [f.name() for f in (layer.fields() or [])]
        except Exception:
            fields = []
        if not fields:
            return None

        up = {str(f).upper(): str(f) for f in fields if f}
        base = str(field_name or "").strip()
        base_up = base.upper()
        if not base_up:
            return None

        if base_up.endswith("IDX"):
            cand = base_up[:-3] + "NAME"
            if cand in up:
                return up[cand]

        if base_up.endswith("ID"):
            cand = base_up[:-2] + "NAME"
            if cand in up:
                return up[cand]

        # Common KIGAM pairs / fallbacks
        for cand in ("LITHONAME", "AGENAME", "NAME", "KOR_NAME", "ENG_NAME"):
            if cand in up and up[cand] != base:
                return up[cand]

        return None

    def _is_numeric_field(self, layer: QgsVectorLayer, field_name: str) -> bool:
        try:
            f = layer.fields().field(field_name)
            if f is None:
                return False
            return f.type() in (
                QVariant.Int,
                QVariant.UInt,
                QVariant.LongLong,
                QVariant.ULongLong,
                QVariant.Double,
            )
        except Exception:
            return False

    def _build_numeric_merge_layer(
        self,
        layers: List[QgsVectorLayer],
        field_name: str,
    ) -> Tuple[Optional[QgsVectorLayer], Dict[str, int], Dict[str, str], Dict[int, int]]:
        if not layers:
            return None, {}, {}, {}

        target_crs = layers[0].crs()
        try:
            wkb = QgsWkbTypes.flatType(layers[0].wkbType())
        except Exception:
            wkb = layers[0].wkbType()
        geom_str = QgsWkbTypes.displayString(wkb) or "Polygon"

        authid = ""
        try:
            if target_crs is not None and target_crs.isValid():
                authid = str(target_crs.authid() or "").strip()
        except Exception:
            authid = ""

        uri = geom_str
        if authid and authid.upper() != "EPSG:0":
            uri = f"{geom_str}?crs={authid}"

        out_layer = QgsVectorLayer(uri, "merged_tmp", "memory")
        if out_layer is None or not out_layer.isValid():
            # Best-effort fallback: omit CRS from URI (some layers may have unknown CRS/authid).
            out_layer = QgsVectorLayer(geom_str, "merged_tmp", "memory")

        if out_layer is None or not out_layer.isValid():
            log_message(f"병합 레이어 생성 실패(메모리 레이어 초기화 실패): geom={geom_str}, crs={authid}", level=Qgis.Warning)
            return None, {}, {}, {}

        try:
            if target_crs is not None and target_crs.isValid():
                out_layer.setCrs(target_crs)
        except Exception:
            pass

        pr = out_layer.dataProvider()
        pr.addAttributes([QgsField("ATK_VAL", QVariant.Int)])
        out_layer.updateFields()

        mapping: Dict[str, int] = {}
        labels: Dict[str, str] = {}
        counts: Dict[int, int] = {}
        next_id = 1
        numeric = self._is_numeric_field(layers[0], field_name)
        label_field = self._suggest_label_field(layers[0], field_name) if numeric else None

        for lyr in layers:
            if lyr.geometryType() != layers[0].geometryType():
                log_message(f"지오메트리 타입 불일치: {lyr.name()} (skip)", level=Qgis.Warning)
                continue
            transform = None
            if lyr.crs() != target_crs:
                try:
                    transform = QgsCoordinateTransform(lyr.crs(), target_crs, QgsProject.instance())
                except Exception:
                    transform = None

            for f in lyr.getFeatures():
                try:
                    geom = f.geometry()
                    if geom is None or geom.isEmpty():
                        continue
                    if transform is not None:
                        geom.transform(transform)
                    val = f[field_name]
                    if val is None or str(val).strip() == "":
                        continue
                    if numeric:
                        try:
                            out_int = int(float(val))
                        except Exception:
                            continue
                        code = str(out_int)
                        mapping[code] = out_int
                        if label_field and code not in labels:
                            try:
                                lbl = f[label_field]
                                if lbl is not None and str(lbl).strip():
                                    labels[code] = str(lbl).strip()
                            except Exception:
                                pass
                        out_val = float(out_int)
                    else:
                        key = str(val)
                        if key not in mapping:
                            mapping[key] = next_id
                            next_id += 1
                        if key not in labels:
                            labels[key] = key
                        out_val = float(mapping[key])

                    try:
                        out_i = int(out_val)
                        counts[out_i] = counts.get(out_i, 0) + 1
                    except Exception:
                        pass

                    nf = QgsFeature(out_layer.fields())
                    nf.setGeometry(geom)
                    nf.setAttributes([out_val])
                    pr.addFeatures([nf])
                except Exception:
                    continue

        out_layer.updateExtents()
        return out_layer, mapping, labels, counts

    def _write_mapping_csv(
        self,
        mapping: Dict[str, int],
        out_path: str,
        *,
        labels: Optional[Dict[str, str]] = None,
        counts: Optional[Dict[int, int]] = None,
    ) -> Optional[str]:
        if not mapping and not labels and not counts:
            return None
        try:
            base = os.path.splitext(out_path)[0]
            csv_path = base + "_mapping.csv"
            labels = labels or {}
            counts = counts or {}
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["code", "int_value", "label", "feature_count"])
                rows = []
                for code, v in (mapping or {}).items():
                    vv = int(v)
                    rows.append((vv, str(code), str(labels.get(str(code), "") or ""), int(counts.get(vv, 0))))
                rows.sort(key=lambda x: (x[0], x[1]))
                for vv, code, label, cnt in rows:
                    w.writerow([code, vv, label, cnt])
            if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
                return csv_path
            return None
        except Exception:
            return None

    def _rasterize_layer(
        self,
        layer: QgsVectorLayer,
        field_name: str,
        out_path: str,
        pixel_size: float,
        nodata: float,
    ) -> str:
        rect = None
        try:
            rect = layer.extent()
        except Exception:
            rect = None
        try:
            authid = str(layer.crs().authid() or "").strip() if layer.crs().isValid() else ""
        except Exception:
            authid = ""

        log_message(
            "KIGAM rasterize: "
            f"layer={layer.name()} field={field_name} out={out_path} "
            f"px={pixel_size} nodata={nodata} crs={authid} extent={rect}",
            level=Qgis.Info,
        )

        params = {
            "INPUT": layer,
            "FIELD": field_name,
            "UNITS": 1,
            "WIDTH": float(pixel_size),
            "HEIGHT": float(pixel_size),
            "EXTENT": rect if rect is not None else layer.extent(),
            "NODATA": float(nodata),
            # Categorical rasters should stay integer-coded for MaxEnt/ML workflows.
            "DATA_TYPE": 4,  # Int32
            "OUTPUT": out_path,
        }
        try:
            result = processing.run("gdal:rasterize", params)
        except Exception as e:
            log_message(f"gdal:rasterize 실패: {e}", level=Qgis.Warning)
            raise

        raster_path = out_path
        try:
            if isinstance(result, dict) and result.get("OUTPUT"):
                raster_path = str(result.get("OUTPUT"))
        except Exception:
            raster_path = out_path

        # Verify output actually exists (some Processing failures don't raise).
        exists = False
        size = 0
        try:
            exists = os.path.exists(raster_path)
            size = os.path.getsize(raster_path) if exists else 0
        except Exception:
            exists = False
            size = 0

        log_message(
            f"KIGAM rasterize result: OUTPUT={raster_path} exists={exists} size={size}",
            level=Qgis.Info if exists and size > 0 else Qgis.Warning,
        )
        if exists and size > 0:
            return raster_path

        # Fallback: export memory layer to disk and retry (helps some GDAL/Processing edge cases).
        try:
            tmp_root = os.path.join(tempfile.gettempdir(), "ArchToolkit_KIGAM_Rasterize")
            os.makedirs(tmp_root, exist_ok=True)
            tmp_vec = os.path.join(tmp_root, f"atk_vec_{new_run_id('kigam')}.gpkg")
            save_res = processing.run("native:savefeatures", {"INPUT": layer, "OUTPUT": tmp_vec})
            vec_path = tmp_vec
            if isinstance(save_res, dict) and save_res.get("OUTPUT"):
                vec_path = str(save_res.get("OUTPUT"))

            params2 = dict(params)
            params2["INPUT"] = vec_path
            result2 = processing.run("gdal:rasterize", params2)
            raster_path2 = out_path
            if isinstance(result2, dict) and result2.get("OUTPUT"):
                raster_path2 = str(result2.get("OUTPUT"))

            exists2 = os.path.exists(raster_path2)
            size2 = os.path.getsize(raster_path2) if exists2 else 0
            log_message(
                f"KIGAM rasterize retry: INPUT={vec_path} OUTPUT={raster_path2} exists={exists2} size={size2}",
                level=Qgis.Info if exists2 and size2 > 0 else Qgis.Warning,
            )
            if exists2 and size2 > 0:
                try:
                    if os.path.exists(vec_path):
                        os.remove(vec_path)
                except Exception:
                    pass
                return raster_path2
        except Exception as e:
            log_message(f"KIGAM rasterize 재시도 실패: {e}", level=Qgis.Warning)

        # If we get here, we couldn't verify a raster file on disk.
        try:
            log_message(f"KIGAM rasterize raw result={result}", level=Qgis.Warning)
        except Exception:
            pass
        raise RuntimeError("래스터 파일이 생성되지 않았습니다. 출력 경로/권한/로그를 확인하세요.")

    def _run_rasterize(self):
        layers = self._selected_vector_layers()
        if not layers:
            push_message(self.iface, "오류", "선택된 벡터 레이어가 없습니다.", level=2)
            restore_ui_focus(self)
            return

        field = self._choose_common_field(layers) if self.radMerge.isChecked() else None
        if self.radMerge.isChecked() and not field:
            push_message(self.iface, "오류", "공통 필드를 찾을 수 없습니다. 필드를 직접 선택하세요.", level=2)
            restore_ui_focus(self)
            return

        fmt = self.cmbFormat.currentData() or "tif"
        pixel = float(self.spinPixel.value())
        nodata = float(self.spinNoData.value())
        run_id = new_run_id("kigam_raster")
        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)

        try:
            if self.radMerge.isChecked():
                out_path = (self.txtOutFile.text() or "").strip()
                if not out_path:
                    push_message(self.iface, "오류", "출력 파일을 지정하세요.", level=2)
                    restore_ui_focus(self)
                    return
                out_path = _ensure_output_extension(out_path, fmt)

                merged_layer, mapping, labels, counts = self._build_numeric_merge_layer(layers, field)
                if merged_layer is None or not merged_layer.isValid():
                    raise RuntimeError("병합 레이어 생성에 실패했습니다.")

                raster_path = self._rasterize_layer(merged_layer, "ATK_VAL", out_path, pixel, nodata)
                csv_path = self._write_mapping_csv(mapping, raster_path, labels=labels, counts=counts)

                try:
                    r_name = os.path.splitext(os.path.basename(raster_path))[0].strip() or f"Geology_{run_id}"
                    if field:
                        r_name = f"{r_name} ({field})"
                except Exception:
                    r_name = f"Geology_{run_id}"

                rlayer = QgsRasterLayer(raster_path, r_name)
                if rlayer and rlayer.isValid():
                    QgsProject.instance().addMapLayer(rlayer, False)
                    try:
                        root = QgsProject.instance().layerTreeRoot()
                        parents = []
                        for lyr in layers:
                            node = root.findLayer(lyr.id())
                            if node is not None and node.parent() is not None:
                                parents.append(node.parent())
                        target = parents[0] if parents and all(p is parents[0] for p in parents) else root.findGroup(PARENT_GROUP_NAME) or root
                        target.insertLayer(0, rlayer)
                    except Exception:
                        QgsProject.instance().layerTreeRoot().insertLayer(0, rlayer)
                    set_archtoolkit_layer_metadata(
                        rlayer,
                        tool_id="kigam_raster",
                        run_id=run_id,
                        kind="raster",
                        params={"field": field, "pixel": pixel},
                    )
                if csv_path:
                    log_message(f"코드 매핑 저장: {csv_path}", level=Qgis.Info)
                push_message(self.iface, "완료", f"래스터 생성: {raster_path}", level=0, duration=7)
                return

            # Per-layer mode
            out_dir = (self.txtOutDir.text() or "").strip()
            if not out_dir or not os.path.isdir(out_dir):
                push_message(self.iface, "오류", "출력 폴더를 지정하세요.", level=2)
                restore_ui_focus(self)
                return

            for lyr in layers:
                field = self.cmbField.currentText().strip()
                if field == "(자동 선택)" or lyr.fields().indexOf(field) < 0:
                    # Choose best field for this layer
                    field = None
                    for p in ["LITHOIDX", "AGEIDX", "LITHONAME", "TYPE", "ASGN_CODE", "SIGN", "CODE"]:
                        if lyr.fields().indexOf(p) >= 0:
                            field = p
                            break
                    if field is None:
                        field = lyr.fields()[0].name() if lyr.fields() else None
                if not field:
                    log_message(f"{lyr.name()}: 필드 없음, 건너뜀", level=Qgis.Warning)
                    continue

                merged_layer, mapping, labels, counts = self._build_numeric_merge_layer([lyr], field)
                if merged_layer is None or not merged_layer.isValid():
                    continue

                out_path = os.path.join(out_dir, f"{_safe_name(lyr.name())}.{fmt}")
                raster_path = self._rasterize_layer(merged_layer, "ATK_VAL", out_path, pixel, nodata)
                csv_path = self._write_mapping_csv(mapping, raster_path, labels=labels, counts=counts)

                rlayer = QgsRasterLayer(raster_path, f"{lyr.name()}_raster")
                if rlayer and rlayer.isValid():
                    QgsProject.instance().addMapLayer(rlayer, False)
                    try:
                        root = QgsProject.instance().layerTreeRoot()
                        node = root.findLayer(lyr.id())
                        target = node.parent() if node is not None and node.parent() is not None else root.findGroup(PARENT_GROUP_NAME) or root
                        target.insertLayer(0, rlayer)
                    except Exception:
                        QgsProject.instance().layerTreeRoot().insertLayer(0, rlayer)
                    set_archtoolkit_layer_metadata(
                        rlayer,
                        tool_id="kigam_raster",
                        run_id=run_id,
                        kind="raster",
                        params={"field": field, "pixel": pixel},
                    )
                if csv_path:
                    log_message(f"코드 매핑 저장: {csv_path}", level=Qgis.Info)

            push_message(self.iface, "완료", "레이어별 래스터 변환이 완료되었습니다.", level=0, duration=7)
        except Exception as e:
            log_message(f"래스터 변환 실패: {e}", level=Qgis.Warning)
            push_message(self.iface, "오류", f"래스터 변환 실패: {e}", level=2)

    def _load_zip(self):
        zip_path = (self.txtZip.text() or "").strip()
        if not zip_path:
            push_message(self.iface, "오류", "ZIP 파일을 선택해주세요.", level=2)
            restore_ui_focus(self)
            return
        if not os.path.exists(zip_path):
            push_message(self.iface, "오류", "선택한 ZIP 파일이 존재하지 않습니다.", level=2)
            restore_ui_focus(self)
            return

        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)
        run_id = new_run_id("kigam_zip")
        processor = KigamZipProcessor()
        layers = processor.process_zip(
            zip_path,
            font_family=self.cmbFont.currentFont().family(),
            font_size=int(self.spinFontSize.value()),
            apply_style=bool(self.chkApplyStyle.isChecked()),
            apply_labels=bool(self.chkApplyLabels.isChecked()),
            run_id=run_id,
        )
        if layers:
            try:
                frame_layer = next((l for l in layers if "frame" in l.name().lower()), None)
                target = frame_layer or layers[0]
                if target and target.isValid():
                    canvas = self.iface.mapCanvas()
                    canvas.setExtent(target.extent())
                    canvas.refresh()
            except Exception:
                pass
            push_message(self.iface, "완료", f"ZIP에서 {len(layers)}개 레이어를 로드했습니다.", level=0, duration=7)
        else:
            push_message(self.iface, "경고", "로드된 레이어가 없습니다. 로그를 확인하세요.", level=1)

        self.refresh_layer_list()

    def _on_help(self):
        html = """
<h3>지질도 ZIP 불러오기 / MaxEnt 래스터 변환</h3>
<p>
KIGAM 1:50,000 지질도 ZIP(도엽)을 바로 로드하고, 지질 코드 기반으로 래스터를 생성합니다.
지구화학도 수치 래스터와 함께 MaxEnt 같은 예측 모델링 입력으로 사용할 수 있습니다.
</p>

<h4>ZIP 불러오기</h4>
<ul>
  <li>KIGAM에서 받은 ZIP을 선택하면 SHP를 자동 로드하고, sym 폴더가 있으면 심볼을 적용합니다.</li>
  <li>LITHOIDX/LITHONAME 레이어는 라벨을 자동 적용할 수 있습니다.</li>
  <li>레이어는 <code>ArchToolkit - Geology</code> 그룹 아래 <code>KIGAM_도엽명</code>으로 정리되고, 라인/포인트가 폴리곤(Litho) 위로 올라오도록 순서를 맞춥니다.</li>
</ul>

<h4>벡터 → 래스터</h4>
<ul>
  <li><b>기본 목록</b>: 보통 Litho(암상/지층) 폴리곤만 있으면 충분하므로, 기본은 <b>KIGAM ZIP 레이어 + Litho(폴리곤)</b>만 표시합니다.</li>
  <li>레이어 이름 앞에 <code>[GF13_청주]</code>처럼 <b>도엽/지역</b> 정보가 함께 표시됩니다(여러 도엽을 불러온 경우 구분용).</li>
  <li>값 필드는 보통 <code>LITHOIDX</code>/<code>AGEIDX</code>를 사용합니다.</li>
  <li>문자 코드(예: Qa, Jbgr)일 경우 자동으로 정수 코드로 매핑하며, <code>*_mapping.csv</code>를 함께 저장합니다.</li>
  <li>숫자 코드(예: <code>LITHOIDX</code>)를 선택해도 가능한 경우 <code>LITHONAME</code>/<code>AGENAME</code>을 함께 매핑 CSV에 기록합니다.</li>
  <li>단일 래스터(병합) 또는 레이어별 출력 중 선택할 수 있습니다.</li>
  <li>실행 후에는 <b>출력 파일이 실제로 생성되었는지</b> 확인하고, 문제가 있으면 로그에 원인을 남깁니다.</li>
</ul>

<h4>예측모델링 팁</h4>
<ul>
  <li>지질 단위(지층/암상)는 보통 <b>범주형(categorical)</b> 변수입니다. 래스터는 숫자로 저장되며, 매핑 CSV로 해석합니다.</li>
  <li>MaxEnt를 쓴다면 해당 변수를 범주형으로 지정하는 방식을 권장합니다(연속형 숫자로 해석되면 왜곡될 수 있음).</li>
  <li>다중 변수(예: 지질+지구화학+지형)로 모델을 만들 때는 모든 래스터의 <b>좌표계/해상도/Extent</b>를 맞추는 것이 중요합니다.</li>
</ul>

<h4>문제 해결</h4>
<ul>
  <li>CSV는 나오는데 래스터가 없으면: 출력 폴더 권한/경로(특수문자/보안 설정)를 확인하고, 다른 폴더(예: Downloads)로 다시 저장해보세요.</li>
  <li>원인 파악: ArchToolkit 실시간 로그/로그 파일에서 <code>KIGAM rasterize result</code> 줄을 확인하세요.</li>
</ul>
"""
        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            show_help_dialog(parent=self, title="지질도 ZIP/MaxEnt 도움말", html=html, plugin_dir=plugin_dir)
        except Exception:
            pass
