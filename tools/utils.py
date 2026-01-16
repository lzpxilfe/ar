# -*- coding: utf-8 -*-
import os
import tempfile
from typing import Optional

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QWidget
from qgis.core import (
    QgsCoordinateTransform,
    QgsMessageLog,
    QgsProcessingFeedback,
    QgsProject,
    QgsUnitTypes,
    Qgis,
)

def transform_point(point, src_crs, dest_crs):
    """Transform point from source CRS to destination CRS"""
    if src_crs == dest_crs:
        return point
    transform = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
    return transform.transform(point)

def cleanup_files(file_paths):
    """Safely remove a list of file paths"""
    for path in file_paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

def log_message(message, level=Qgis.Info):
    """Log to QGIS Message Log (not the transient message bar)."""
    try:
        QgsMessageLog.logMessage(str(message), "ArchToolkit", level)
    except Exception:
        # Never crash due to logging
        pass

def is_metric_crs(crs):
    """Return True if CRS map units are meters (recommended for distance-based tools)."""
    try:
        return (not crs.isGeographic()) and crs.mapUnits() == QgsUnitTypes.DistanceMeters
    except Exception:
        return False

def restore_ui_focus(dialog):
    """Ensure the dialog is visible and has focus"""
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()

def push_message(iface, title, text, level=0, duration=3):
    """Helper to push message to QGIS message bar"""
    iface.messageBar().pushMessage(title, text, level=level, duration=duration)


def get_archtoolkit_output_dir(subdir: Optional[str] = None) -> str:
    """Return a stable output folder for generated files.

    Preference order:
    1) QGIS project homePath (if project is saved or has a home path)
    2) OS temp folder (fallback)

    The directory is created if needed.
    """
    try:
        base = (QgsProject.instance().homePath() or "").strip()
        if base and os.path.isdir(base):
            base = os.path.join(base, "ArchToolkit")
            if subdir:
                base = os.path.join(base, subdir)
            os.makedirs(base, exist_ok=True)
            return base
    except Exception:
        # Never fail due to output path selection.
        pass

    return tempfile.gettempdir()


def is_temp_path(path: str) -> bool:
    try:
        if not path:
            return False
        temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        abs_path = os.path.normcase(os.path.abspath(path))
        return abs_path.startswith(temp_root)
    except Exception:
        return False


def warn_if_temp_output(iface, path: str, what: str = "결과"):
    """Warn users when a final result is created under the OS temp folder."""
    try:
        if iface and is_temp_path(path):
            push_message(
                iface,
                "알림",
                f"{what}이(가) 임시 폴더에 생성되었습니다. 필요하면 '다른 이름으로 저장'으로 영구 저장하세요.",
                level=1,
                duration=8,
            )
    except Exception:
        pass


class ProcessingCancelled(RuntimeError):
    pass


class _MessageBarProgress:
    def __init__(self, iface, title: str, text: str = "", level=Qgis.Info):
        self._bar = getattr(iface, "messageBar", lambda: None)() if iface else None
        self._widget = None
        self._label = None
        self._progress = None
        self._btn_cancel = None

        if not self._bar:
            return

        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        label = QLabel()
        label.setText(f"{title} {text}".strip())

        progress = QProgressBar()
        progress.setMaximum(100)
        progress.setValue(0)
        progress.setTextVisible(False)

        btn_cancel = QPushButton("취소")

        layout.addWidget(label, 1)
        layout.addWidget(progress)
        layout.addWidget(btn_cancel)

        self._widget = widget
        self._label = label
        self._progress = progress
        self._btn_cancel = btn_cancel

        self._bar.pushWidget(widget, level)

    @property
    def cancel_button(self):
        return self._btn_cancel

    def set_text(self, text: str):
        try:
            if self._label:
                self._label.setText(str(text))
        except Exception:
            pass

    def set_progress(self, progress: float):
        try:
            if self._progress is not None:
                self._progress.setValue(int(max(0, min(100, progress))))
        except Exception:
            pass

        try:
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass

    def close(self):
        try:
            if self._bar and self._widget:
                self._bar.popWidget(self._widget)
        except Exception:
            pass
        self._widget = None
        self._label = None
        self._progress = None
        self._btn_cancel = None


class MessageBarProcessingFeedback(QgsProcessingFeedback):
    """Cancelable processing feedback with message-bar progress (no popup dialog)."""

    def __init__(self, iface, title: str, text: str = ""):
        super().__init__()
        self._ui = _MessageBarProgress(iface, title, text=text, level=Qgis.Info)

        try:
            if self._ui.cancel_button:
                self._ui.cancel_button.clicked.connect(self.cancel)
        except Exception:
            pass

    def setProgress(self, progress):  # type: ignore[override]
        try:
            self._ui.set_progress(progress)
        except Exception:
            pass
        super().setProgress(progress)

    def setProgressText(self, text):  # type: ignore[override]
        try:
            self._ui.set_text(text)
        except Exception:
            pass
        super().setProgressText(text)

    def close(self):
        self._ui.close()


class ProcessingRunner:
    """Convenience wrapper for processing.run with cancelable feedback + consistent UI."""

    def __init__(self, iface, title: str, text: str = ""):
        self._feedback = MessageBarProcessingFeedback(iface, title, text=text) if iface else None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def run(self, algorithm_id: str, params: dict, text: Optional[str] = None):
        if text and self._feedback:
            try:
                self._feedback.setProgressText(text)
            except Exception:
                pass

        import processing

        result = processing.run(
            algorithm_id,
            params,
            feedback=self._feedback if self._feedback else None,
        )

        if self._feedback and self._feedback.isCanceled():
            raise ProcessingCancelled()

        return result

    def close(self):
        if self._feedback:
            self._feedback.close()
