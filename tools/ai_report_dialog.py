# -*- coding: utf-8 -*-
"""
AI AOI Report tool for ArchToolkit.

Summarizes the situation within a radius around an AOI polygon by scanning
project layers (preferably ArchToolkit outputs) and generates a Korean narrative
report. Supports a free/local mode and optional Gemini API mode.
"""

from __future__ import annotations

import json
import os

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QIcon

from qgis.core import QgsMapLayerProxyModel, QgsVectorLayer
from qgis.gui import QgsMapLayerComboBox  # noqa: F401 (needed for custom widget)

from . import ai_aoi_summary
from . import ai_gemini
from . import ai_local_summarizer
from .live_log_dialog import ensure_live_log_dialog
from .utils import log_message, push_message, restore_ui_focus


_SETTINGS_PREFIX = "ArchToolkit/ai/report"


class AiAoiReportDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._setup_ui()
        self._update_provider_ui()
        self._refresh_key_status()

    def _settings_get(self, key: str, default=None):
        try:
            return QSettings().value(f"{_SETTINGS_PREFIX}/{key}", default)
        except Exception:
            return default

    def _settings_set(self, key: str, value) -> None:
        try:
            QSettings().setValue(f"{_SETTINGS_PREFIX}/{key}", value)
        except Exception:
            pass

    def _get_provider(self) -> str:
        try:
            v = self.cmbProvider.currentData()
            return str(v or "").strip() or "local"
        except Exception:
            return "local"

    def _setup_ui(self):
        self.setWindowTitle("AI 조사요약 (AOI Report) - ArchToolkit")
        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            icon_path = os.path.join(plugin_dir, "icon.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except Exception:
            pass

        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel(
            "<b>AI 조사요약</b><br>"
            "조사지역(AOI) 반경 내의 프로젝트 레이어(특히 ArchToolkit 결과)를 요약하고,<br>"
            "보고서/업무 메모 형태의 문장으로 정리합니다.<br>"
            "<i>모드: 무료(로컬 요약) 또는 Gemini(API)</i><br>"
            "<i>주의: AI/요약 결과는 참고용이며, 반드시 사용자가 검토해야 합니다.</i>"
        )
        header.setWordWrap(True)
        header.setStyleSheet("background:#e3f2fd; padding:10px; border:1px solid #bbdefb; border-radius:4px;")
        layout.addWidget(header)

        grp_in = QtWidgets.QGroupBox("1. 입력")
        form = QtWidgets.QFormLayout(grp_in)

        self.cmbAoi = QgsMapLayerComboBox(grp_in)
        self.cmbAoi.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        form.addRow("조사지역 폴리곤(AOI):", self.cmbAoi)

        self.chkSelectedOnly = QtWidgets.QCheckBox("선택된 피처만 사용")
        form.addRow("", self.chkSelectedOnly)

        self.spinRadius = QtWidgets.QDoubleSpinBox(grp_in)
        self.spinRadius.setDecimals(0)
        self.spinRadius.setRange(1.0, 1_000_000.0)
        self.spinRadius.setValue(1000.0)
        self.spinRadius.setSingleStep(100.0)
        self.spinRadius.setSuffix(" m")
        form.addRow("반경:", self.spinRadius)

        self.chkOnlyArchToolkit = QtWidgets.QCheckBox("ArchToolkit 결과 레이어만 요약(권장)")
        self.chkOnlyArchToolkit.setChecked(True)
        form.addRow("", self.chkOnlyArchToolkit)

        layout.addWidget(grp_in)

        grp_ai = QtWidgets.QGroupBox("2. AI 설정")
        grid = QtWidgets.QGridLayout(grp_ai)

        self.cmbProvider = QtWidgets.QComboBox()
        self.cmbProvider.addItem("무료(로컬 요약)", "local")
        self.cmbProvider.addItem("Gemini(API)", "gemini")
        saved_provider = str(self._settings_get("provider", "local") or "").strip() or "local"
        try:
            idx = self.cmbProvider.findData(saved_provider)
            if idx >= 0:
                self.cmbProvider.setCurrentIndex(idx)
        except Exception:
            pass
        self.cmbProvider.currentIndexChanged.connect(self._on_provider_changed)

        self.lblKeyStatus = QtWidgets.QLabel("(키 상태: 확인 중)")
        self.lblKeyStatus.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)

        self.btnSetKey = QtWidgets.QPushButton("API 키 설정/변경…")
        self.btnSetKey.clicked.connect(self._on_set_key)

        self.txtModel = QtWidgets.QLineEdit()
        self.txtModel.setText(ai_gemini.get_configured_model())
        self.txtModel.setPlaceholderText("예: gemini-1.5-flash")

        self.btnSaveModel = QtWidgets.QPushButton("모델 저장")
        self.btnSaveModel.clicked.connect(self._on_save_model)

        grid.addWidget(QtWidgets.QLabel("모드:"), 0, 0)
        grid.addWidget(self.cmbProvider, 0, 1, 1, 2)
        grid.addWidget(QtWidgets.QLabel("키:"), 1, 0)
        grid.addWidget(self.lblKeyStatus, 1, 1)
        grid.addWidget(self.btnSetKey, 1, 2)
        grid.addWidget(QtWidgets.QLabel("모델:"), 2, 0)
        grid.addWidget(self.txtModel, 2, 1)
        grid.addWidget(self.btnSaveModel, 2, 2)

        self.lblLocalHint = QtWidgets.QLabel("무료(로컬 요약) 모드는 외부 API 호출/전송 없이, 프로젝트 통계를 문장으로 정리합니다.")
        self.lblLocalHint.setWordWrap(True)
        self.lblLocalHint.setStyleSheet("color:#455a64;")
        grid.addWidget(self.lblLocalHint, 3, 0, 1, 3)

        self.lblAuthHint = QtWidgets.QLabel(ai_gemini.explain_auth_manager_once())
        self.lblAuthHint.setWordWrap(True)
        self.lblAuthHint.setStyleSheet("color:#455a64;")
        grid.addWidget(self.lblAuthHint, 4, 0, 1, 3)

        layout.addWidget(grp_ai)

        grp_out = QtWidgets.QGroupBox("3. 결과")
        v = QtWidgets.QVBoxLayout(grp_out)
        self.txtOutput = QtWidgets.QTextEdit()
        self.txtOutput.setReadOnly(True)
        self.txtOutput.setPlaceholderText("여기에 AI 보고서가 생성됩니다.")
        v.addWidget(self.txtOutput)

        btn_row = QtWidgets.QHBoxLayout()
        self.btnGenerate = QtWidgets.QPushButton("AI 요약 생성")
        self.btnGenerate.clicked.connect(self._on_generate)
        self.btnExport = QtWidgets.QPushButton("저장…")
        self.btnExport.clicked.connect(self._on_export)
        self.btnClose = QtWidgets.QPushButton("닫기")
        self.btnClose.clicked.connect(self.reject)

        btn_row.addWidget(self.btnGenerate)
        btn_row.addWidget(self.btnExport)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btnClose)
        v.addLayout(btn_row)

        layout.addWidget(grp_out)

    def _on_provider_changed(self):
        provider = self._get_provider()
        self._settings_set("provider", provider)
        self._update_provider_ui()
        self._refresh_key_status()

    def _update_provider_ui(self):
        provider = self._get_provider()
        is_gemini = provider == "gemini"

        try:
            self.btnSetKey.setEnabled(is_gemini)
            self.txtModel.setEnabled(is_gemini)
            self.btnSaveModel.setEnabled(is_gemini)
        except Exception:
            pass

        try:
            self.lblAuthHint.setVisible(is_gemini)
            self.lblLocalHint.setVisible(not is_gemini)
        except Exception:
            pass

    def _refresh_key_status(self):
        if self._get_provider() != "gemini":
            self.lblKeyStatus.setText("불필요 (로컬 요약)")
            self.lblKeyStatus.setStyleSheet("color:#455a64; font-weight:bold;")
            return

        key = ai_gemini.get_api_key()
        if key:
            self.lblKeyStatus.setText("설정됨 (AuthManager)")
            self.lblKeyStatus.setStyleSheet("color:#2e7d32; font-weight:bold;")
        else:
            self.lblKeyStatus.setText("미설정")
            self.lblKeyStatus.setStyleSheet("color:#c62828; font-weight:bold;")

    def _on_set_key(self):
        if self._get_provider() != "gemini":
            push_message(self.iface, "정보", "현재 모드에서는 API 키가 필요하지 않습니다.", level=1, duration=4)
            return
        try:
            ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)
        except Exception:
            pass
        ai_gemini.configure_api_key(self, iface=self.iface)
        self._refresh_key_status()

    def _on_save_model(self):
        if self._get_provider() != "gemini":
            push_message(self.iface, "정보", "현재 모드에서는 모델 설정이 필요하지 않습니다.", level=1, duration=4)
            return
        model = str(self.txtModel.text() or "").strip()
        if not model:
            push_message(self.iface, "오류", "모델 이름을 입력하세요.", level=2, duration=5)
            return
        ai_gemini.set_configured_model(model)
        push_message(self.iface, "완료", f"모델을 저장했습니다: {model}", level=0, duration=4)

    def _build_prompt(self, ctx: dict) -> str:
        ctx_json = json.dumps(ctx, ensure_ascii=False, indent=2)
        radius_m = ctx.get("radius_m")
        aoi = ctx.get("aoi", {}) or {}
        aoi_name = aoi.get("layer_name", "")

        return (
            "당신은 한국의 고고학/문화유산 연구자를 돕는 GIS 분석 보조자입니다.\n"
            "아래 JSON은 QGIS 프로젝트에서 ‘조사지역(AOI) 반경’ 내의 레이어들을 요약한 것입니다.\n"
            "\n"
            "요청:\n"
            "1) 한국어로, 보고서/업무 메모 형태로 정리해 주세요.\n"
            "2) 과장/추측 금지: 수치가 없으면 단정하지 말고 '추정/참고'로 표시.\n"
            "3) 결과는 섹션으로 구분:\n"
            "   - 개요(조사지역/반경)\n"
            "   - 사용된 레이어/분석 요약(레이어별)\n"
            "   - 핵심 관찰(정량값이 있으면 포함)\n"
            "   - 한계/주의(좌표계/해상도/NoData/AI 한계)\n"
            "   - 다음 단계 제안\n"
            "4) 결과에 포함된 레이어 이름은 가능한 그대로 유지.\n"
            "\n"
            f"대상: AOI={aoi_name}, 반경={radius_m} m\n"
            "\n"
            "JSON:\n"
            f"{ctx_json}\n"
        )

    def _on_generate(self):
        aoi_layer = self.cmbAoi.currentLayer()
        if aoi_layer is None or not isinstance(aoi_layer, QgsVectorLayer):
            push_message(self.iface, "오류", "조사지역(AOI) 폴리곤 레이어를 선택하세요.", level=2, duration=6)
            restore_ui_focus(self)
            return

        provider = self._get_provider()
        is_gemini = provider == "gemini"

        api_key = None
        model = None
        if is_gemini:
            api_key = ai_gemini.get_api_key()
            if not api_key:
                push_message(self.iface, "정보", "Gemini API 키가 필요합니다. 먼저 설정하세요.", level=1, duration=6)
                self._on_set_key()
                api_key = ai_gemini.get_api_key()
                if not api_key:
                    return

            model = str(self.txtModel.text() or "").strip() or ai_gemini.get_configured_model()

        radius_m = float(self.spinRadius.value())
        selected_only = bool(self.chkSelectedOnly.isChecked())
        only_arch = bool(self.chkOnlyArchToolkit.isChecked())

        try:
            ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)
        except Exception:
            pass

        push_message(self.iface, "AI 요약", "AOI 주변 레이어 요약 생성 중…", level=0, duration=4)
        ctx, err = ai_aoi_summary.build_aoi_context(
            aoi_layer=aoi_layer,
            selected_only=selected_only,
            radius_m=radius_m,
            only_archtoolkit_layers=only_arch,
            max_layers=40,
        )
        if err:
            push_message(self.iface, "오류", err, level=2, duration=8)
            return
        if not ctx:
            push_message(self.iface, "오류", "AOI 요약 컨텍스트를 만들 수 없습니다.", level=2, duration=8)
            return

        if not is_gemini:
            try:
                text = ai_local_summarizer.generate_report(ctx)
            except Exception as e:
                push_message(self.iface, "오류", f"로컬 요약 생성 실패: {e}", level=2, duration=8)
                return

            self.txtOutput.setPlainText(text or "")
            push_message(self.iface, "AI 요약", "완료 (로컬)", level=0, duration=4)
            return

        prompt = self._build_prompt(ctx)

        push_message(self.iface, "AI 요약", "Gemini 호출 중…(데이터 요약/레이어명만 전송)", level=0, duration=5)
        self.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            text, api_err = ai_gemini.generate_text(
                api_key=str(api_key or ""),
                model=str(model or ""),
                prompt=prompt,
                temperature=0.2,
                max_output_tokens=1400,
                timeout_ms=45000,
            )
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.setEnabled(True)

        if api_err:
            log_message(f"Gemini error: {api_err}", level=2)
            push_message(self.iface, "오류", f"Gemini 호출 실패: {api_err}", level=2, duration=10)
            # Fallback to local report so user still gets something usable.
            try:
                fallback = ai_local_summarizer.generate_report(ctx)
                if fallback:
                    self.txtOutput.setPlainText(
                        "※ Gemini 호출 실패로 로컬 요약으로 대체했습니다.\n\n" + str(fallback)
                    )
                    push_message(self.iface, "AI 요약", "로컬 요약으로 대체 완료", level=1, duration=6)
            except Exception:
                pass
            return

        self.txtOutput.setPlainText(text or "")
        push_message(self.iface, "AI 요약", "완료", level=0, duration=4)

    def _on_export(self):
        txt = self.txtOutput.toPlainText()
        if not (txt or "").strip():
            push_message(self.iface, "정보", "저장할 내용이 없습니다.", level=1, duration=4)
            return
        path, _flt = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "보고서 저장",
            "aoi_report.md",
            "Markdown (*.md);;Text (*.txt);;All Files (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(txt)
            push_message(self.iface, "완료", f"저장했습니다: {path}", level=0, duration=5)
        except Exception as e:
            push_message(self.iface, "오류", f"저장 실패: {e}", level=2, duration=6)
