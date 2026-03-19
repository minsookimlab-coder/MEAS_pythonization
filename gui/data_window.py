"""
DataWindow: 가장 최근 측정 스텝의 instant 값을 표시합니다.
(누적 없음 — 매 스텝마다 Value 열만 갱신)
"""
from typing import List, Tuple

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor

_MONO = QFont("Consolas", 10)
_COL_AXIS  = QColor("#1c2d1c")
_COL_UNIT  = QColor("#1a1a2e")
_FG_AXIS   = QColor("#56d364")
_FG_UNIT   = QColor("#8b949e")
_FG_VALUE  = QColor("#c9d1d9")


class DataWindow(QDialog):
    """
    Figure Axis | Unit | Value 의 3열 테이블.
    행 수 = 데이터 채널 수 (next_target + active measurements).
    매 스텝마다 Value 열만 갱신.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Data")
        self.resize(400, 300)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self._row_count = 0
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Figure Axis", "Unit", "Value"])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setFont(_MONO)
        self._table.setStyleSheet(
            "QTableWidget { background:#0d1117; color:#c9d1d9; gridline-color:#21262d; }"
            "QHeaderView::section { background:#161b22; color:#8b949e; border:none; padding:4px; }"
        )
        layout.addWidget(self._table)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def configure_columns(self, columns: List[Tuple[str, str]]):
        """
        (figure_axis, unit) 목록으로 행 설정.
        _on_selection_applied / sweep channel 변경 시 호출.
        """
        self._row_count = len(columns)
        self._table.setRowCount(self._row_count)
        for row, (axis, unit) in enumerate(columns):
            axis_item = QTableWidgetItem(axis)
            axis_item.setBackground(_COL_AXIS)
            axis_item.setForeground(_FG_AXIS)
            self._table.setItem(row, 0, axis_item)

            unit_item = QTableWidgetItem(unit)
            unit_item.setBackground(_COL_UNIT)
            unit_item.setForeground(_FG_UNIT)
            self._table.setItem(row, 1, unit_item)

            val_item = QTableWidgetItem("—")
            val_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            val_item.setForeground(_FG_VALUE)
            self._table.setItem(row, 2, val_item)

    def update_values(self, values: List[str]):
        """Value 열만 갱신. values 길이 = configure_columns의 행 수."""
        for row, v in enumerate(values):
            item = self._table.item(row, 2)
            if item:
                item.setText(v)

    def clear_values(self):
        """sweep 시작 전 Value 열 초기화."""
        for row in range(self._row_count):
            item = self._table.item(row, 2)
            if item:
                item.setText("—")

    # ------------------------------------------------------------------

    def closeEvent(self, event):
        event.ignore()
        self.hide()
