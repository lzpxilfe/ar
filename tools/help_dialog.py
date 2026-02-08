# -*- coding: utf-8 -*-
import os
from typing import Optional

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices


def _open_local_file(path: str) -> bool:
    try:
        if not path:
            return False
        p = os.path.abspath(path)
        if not os.path.exists(p):
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(p)))
    except Exception:
        return False


class ArchToolkitHelpDialog(QtWidgets.QDialog):
    def __init__(
        self,
        *,
        title: str,
        html: str,
        plugin_dir: Optional[str] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(str(title or "도움말"))
        self.setMinimumSize(700, 520)

        self._plugin_dir = os.path.abspath(plugin_dir) if plugin_dir else None

        layout = QtWidgets.QVBoxLayout(self)

        self.browser = QtWidgets.QTextBrowser(self)
        self.browser.setOpenExternalLinks(True)
        try:
            self.browser.setHtml(str(html or ""))
        except Exception:
            self.browser.setPlainText(str(html or ""))
        layout.addWidget(self.browser, 1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)

        self.btnReadme = QtWidgets.QPushButton("README 열기", self)
        self.btnRefs = QtWidgets.QPushButton("REFERENCES 열기", self)
        self.btnClose = QtWidgets.QPushButton("닫기", self)

        self.btnReadme.clicked.connect(self._open_readme)
        self.btnRefs.clicked.connect(self._open_references)
        self.btnClose.clicked.connect(self.accept)

        btn_row.addWidget(self.btnReadme)
        btn_row.addWidget(self.btnRefs)
        btn_row.addWidget(self.btnClose)
        layout.addLayout(btn_row)

    def _open_readme(self):
        if not self._plugin_dir:
            return
        _open_local_file(os.path.join(self._plugin_dir, "README.md"))

    def _open_references(self):
        if not self._plugin_dir:
            return
        _open_local_file(os.path.join(self._plugin_dir, "REFERENCES.md"))


def show_help_dialog(*, parent, title: str, html: str, plugin_dir: Optional[str] = None) -> None:
    dlg = ArchToolkitHelpDialog(title=title, html=html, plugin_dir=plugin_dir, parent=parent)
    try:
        dlg.exec_()
    except Exception:
        try:
            dlg.exec()
        except Exception:
            dlg.show()

