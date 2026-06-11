"""
공통 도움말 버튼 — 작은 "?" 버튼. 마우스를 올리면 툴팁, 클릭하면 상세 안내 창.
여러 UI에서 일관된 도움말 UX를 제공한다.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton, QMessageBox, QWidget

_HELP_STYLE = (
    "QPushButton { background-color: #4ec9b0; color: #1e1e1e; border-radius: 9px;"
    "font-weight: bold; font-size: 11px; }"
    "QPushButton:hover { background-color: #6fe9d0; }"
)


def make_help_button(html: str, title: str = "도움말",
                     parent: Optional[QWidget] = None) -> QPushButton:
    """HTML 도움말을 담은 작은 '?' 버튼을 반환한다.

    - 마우스 호버: 툴팁으로 동일 내용 표시
    - 클릭: 스크롤 가능한 QMessageBox로 상세 표시
    """
    btn = QPushButton("?", parent)
    btn.setFixedSize(18, 18)
    btn.setCursor(Qt.CursorShape.WhatsThisCursor)
    btn.setStyleSheet(_HELP_STYLE)
    btn.setToolTip(html)

    def _show() -> None:
        box = QMessageBox(btn)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(html)
        box.setIcon(QMessageBox.Icon.Information)
        box.exec()

    btn.clicked.connect(_show)
    return btn
