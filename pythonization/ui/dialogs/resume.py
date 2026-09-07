"""
ResumePickerDialog: 저장된 재개 지점(최근 10개) 중 하나를 선택하는 작은 창.

기본 선택은 가장 최근 지점. 선택된 ResumePoint를 selected_point로 노출한다.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QWidget,
)

from pythonization.measurement.resume_log import ResumePoint

_MONO = QFont("Consolas", 9)

#: 목록 앞에 붙이는 sweep 종류 표시. 모르는 값은 "더블"로 두어 기존 동작을 유지한다.
_SWEEP_TYPE_TAG = {
    "single":  "단일",
    "double":  "더블",
    "cycle":   "사이클",
    "cycle2d": "더블+",
}


class ResumePickerDialog(QDialog):
    """재개 지점 목록에서 하나를 선택."""

    def __init__(self, points: List[ResumePoint], parent: Optional[QWidget] = None,
                 sweep_type: Optional[str] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("측정 재개 지점 선택")
        self.setMinimumWidth(560)
        self.selected_point: Optional[ResumePoint] = None

        # sweep_type 필터 (지정 시 해당 타입만 표시)
        self._points = [
            p for p in points
            if sweep_type is None or p.sweep_type == sweep_type
        ]

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        title = QLabel("통신 오류로 중단된 측정을 재개할 지점을 선택하세요 (최신순):")
        root.addWidget(title)

        self._list = QListWidget()
        self._list.setFont(_MONO)
        for p in self._points:
            tag = _SWEEP_TYPE_TAG.get(p.sweep_type, "더블")
            item = QListWidgetItem(f"[{tag}] {p.timestamp}\n        {p.label}")
            item.setData(Qt.ItemDataRole.UserRole, p)
            self._list.addItem(item)
        if self._points:
            self._list.setCurrentRow(0)   # 가장 최근
        self._list.itemDoubleClicked.connect(lambda _i: self._accept())
        root.addWidget(self._list)

        if not self._points:
            empty = QLabel("저장된 재개 지점이 없습니다.")
            empty.setStyleSheet("color: #888888;")
            root.addWidget(empty)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_ok = QPushButton("이 지점부터 재개")
        self._btn_ok.setEnabled(bool(self._points))
        self._btn_ok.clicked.connect(self._accept)
        btn_cancel = QPushButton("취소")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_ok)
        btn_row.addWidget(btn_cancel)
        root.addLayout(btn_row)

    def _accept(self) -> None:
        item = self._list.currentItem()
        if item is not None:
            self.selected_point = item.data(Qt.ItemDataRole.UserRole)
            self.accept()
