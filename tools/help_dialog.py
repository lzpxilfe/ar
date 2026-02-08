# -*- coding: utf-8 -*-
from typing import Optional

from qgis.PyQt import QtWidgets


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

        layout = QtWidgets.QVBoxLayout(self)

        self.browser = QtWidgets.QTextBrowser(self)
        # Keep help self-contained: don't launch the user's browser from inside QGIS.
        self.browser.setOpenExternalLinks(False)
        try:
            self.browser.setHtml(str(html or ""))
        except Exception:
            self.browser.setPlainText(str(html or ""))
        layout.addWidget(self.browser, 1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)

        self.btnCopy = QtWidgets.QPushButton("복사", self)
        self.btnClose = QtWidgets.QPushButton("닫기", self)

        self.btnCopy.clicked.connect(self._copy_text)
        self.btnClose.clicked.connect(self.accept)

        btn_row.addWidget(self.btnCopy)
        btn_row.addWidget(self.btnClose)
        layout.addLayout(btn_row)

    def _copy_text(self):
        try:
            QtWidgets.QApplication.clipboard().setText(self.browser.toPlainText())
        except Exception:
            pass


def show_help_dialog(*, parent, title: str, html: str, plugin_dir: Optional[str] = None) -> None:
    # plugin_dir is currently unused (kept for compatibility with callers).
    dlg = ArchToolkitHelpDialog(title=title, html=html, plugin_dir=plugin_dir, parent=parent)
    try:
        dlg.exec_()
    except Exception:
        try:
            dlg.exec()
        except Exception:
            dlg.show()
