# -*- coding: utf-8 -*-

import math

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QDateTime, QPoint, Qt
from qgis.PyQt.QtGui import QFontDatabase
from qgis.core import Qgis

from .utils import add_ui_log_listener, remove_ui_log_listener, start_ui_log_pump

_live_log_dialog = None


def _level_name(level) -> str:
    try:
        if level == Qgis.Warning:
            return "WARN"
        if level == Qgis.Critical:
            return "ERROR"
        if level == Qgis.Success:
            return "OK"
    except Exception:
        pass
    return "INFO"


class ArchToolkitLiveLogDialog(QtWidgets.QDialog):
    """Lightweight, non-modal live log window for long-running tools."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ArchToolkit 작업 로그")
        try:
            self.setWindowFlag(Qt.Tool, True)
        except Exception:
            pass
        try:
            self.setModal(False)
        except Exception:
            pass

        self._txt = QtWidgets.QPlainTextEdit(self)
        self._txt.setReadOnly(True)
        self._txt.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        try:
            self._txt.document().setMaximumBlockCount(5000)
        except Exception:
            pass
        try:
            fixed_font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
            if fixed_font is not None:
                self._txt.setFont(fixed_font)
        except Exception:
            pass

        btn_clear = QtWidgets.QPushButton("비우기", self)
        btn_close = QtWidgets.QPushButton("닫기", self)
        btn_clear.clicked.connect(self._txt.clear)
        btn_close.clicked.connect(self.close)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(btn_clear)
        btn_row.addWidget(btn_close)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self._txt, 1)
        layout.addLayout(btn_row)
        self.setLayout(layout)

        self._listener = self._on_log
        add_ui_log_listener(self._listener)
        try:
            self.destroyed.connect(lambda *_: remove_ui_log_listener(self._listener))
        except Exception:
            pass

        try:
            self.resize(520, 360)
        except Exception:
            pass

    def clear(self):
        try:
            self._txt.clear()
        except Exception:
            pass

    def _on_log(self, message: str, level):
        try:
            ts = QDateTime.currentDateTime().toString("HH:mm:ss")
            lvl = _level_name(level)
            self._txt.appendPlainText(f"[{ts}] [{lvl}] {message}")
        except Exception:
            pass

    def show_near(self, owner=None):
        """Show window near the owner dialog (to the right), best-effort."""
        try:
            if owner is not None:
                g = owner.frameGeometry()
                x = int(g.right() + 12)
                y = int(g.top())

                # Avoid positioning far off-screen (best-effort).
                screen = QtWidgets.QApplication.primaryScreen()
                if screen is not None:
                    avail = screen.availableGeometry()
                    w = int(self.width() or 520)
                    h = int(self.height() or 360)
                    x = max(avail.left(), min(avail.right() - w, x))
                    y = max(avail.top(), min(avail.bottom() - h, y))

                self.move(QPoint(x, y))
        except Exception:
            pass

        try:
            self.show()
        except Exception:
            pass
        try:
            self.raise_()
        except Exception:
            pass


def ensure_live_log_dialog(iface=None, *, owner=None, show: bool = True, clear: bool = False):
    """Return a singleton live log dialog; optionally show it next to `owner`."""
    global _live_log_dialog

    try:
        start_ui_log_pump()
    except Exception:
        pass

    parent = None
    try:
        if iface is not None and hasattr(iface, "mainWindow"):
            parent = iface.mainWindow()
    except Exception:
        parent = None

    if _live_log_dialog is None:
        _live_log_dialog = ArchToolkitLiveLogDialog(parent=parent)
    else:
        # Re-parent if needed (best-effort).
        try:
            if parent is not None and _live_log_dialog.parent() is None:
                _live_log_dialog.setParent(parent)
        except Exception:
            pass

    if clear:
        try:
            _live_log_dialog.clear()
        except Exception:
            pass

    if show:
        try:
            _live_log_dialog.show_near(owner)
        except Exception:
            pass

    return _live_log_dialog

