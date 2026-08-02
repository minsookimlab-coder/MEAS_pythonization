"""
SecondChannelTableWindow — double sweep의 second-channel 값 테이블 편집 창.

SecondChannelModel(스레드 안전)을 감싸 표시/편집한다. 완료 행은 비활성, 현재 측정 행은
색상, 미래(PENDING) 행은 측정 중에도 값 수정·행 추가·삭제 가능.

워커(다른 스레드)는 절대 이 창/QTableWidget을 직접 만지지 않는다 — VnaWindow가 bound
@Slot(on_current/on_done)으로 이 창의 refresh만 호출한다.
"""
from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QDialog, QTableWidget, QTableWidgetItem, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QAbstractItemView,
)

from gui.second_channel_model import SecondChannelModel, PENDING, CURRENT, DONE

_MONO = QFont("Consolas", 10)

_BG_DONE    = QColor("#161b22")
_FG_DONE    = QColor("#6e7681")
_BG_CURRENT = QColor("#1f6feb")
_FG_CURRENT = QColor("#ffffff")
_STATUS = {PENDING: "대기", CURRENT: "측정중", DONE: "완료"}


class SecondChannelTableWindow(QDialog):
    def __init__(self, model: SecondChannelModel, vna_window, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Second Channel 값 테이블")
        self.resize(360, 480)
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint)
        self._model = model
        self._vna = vna_window
        self._refreshing = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        hint = QLabel("완료 행은 잠기고, 현재 측정 행은 색으로 표시됩니다.\n"
                      "아직 측정 안 한(대기) 행은 측정 중에도 값 수정·추가·삭제할 수 있습니다.")
        hint.setStyleSheet("color:#888; font-size:10px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["#", "Second 값", "상태"])
        self._table.setFont(_MONO)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setColumnWidth(0, 44)
        self._table.setColumnWidth(1, 160)
        self._table.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self._table, stretch=1)

        self._dup_lbl = QLabel("")
        self._dup_lbl.setStyleSheet("color:#d7ba7d; font-size:10px;")
        self._dup_lbl.setWordWrap(True)
        lay.addWidget(self._dup_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(6)
        self._btn_add = QPushButton("+ 행")
        self._btn_add.clicked.connect(self._on_add)
        self._btn_del = QPushButton("✕ 행")
        self._btn_del.clicked.connect(self._on_del)
        self._btn_reset = QPushButton("Start/Stop/N에서 재생성")
        self._btn_reset.clicked.connect(self._on_reset)
        btn_row.addWidget(self._btn_add)
        btn_row.addWidget(self._btn_del)
        btn_row.addWidget(self._btn_reset)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self.refresh()

    # ------------------------------------------------------------------
    # 렌더링
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        vals, states, _cur = self._model.snapshot()
        self._refreshing = True
        try:
            self._table.setRowCount(len(vals))
            for i, (v, stt) in enumerate(zip(vals, states)):
                # # 열
                it0 = QTableWidgetItem(str(i))
                it0.setFlags(Qt.ItemFlag.ItemIsEnabled)
                it0.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(i, 0, it0)
                # 값 열
                it1 = QTableWidgetItem(f"{v:.6g}")
                if stt == PENDING:
                    it1.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable
                                 | Qt.ItemFlag.ItemIsSelectable)
                else:
                    it1.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self._table.setItem(i, 1, it1)
                # 상태 열
                it2 = QTableWidgetItem(_STATUS.get(stt, "?"))
                it2.setFlags(Qt.ItemFlag.ItemIsEnabled)
                it2.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(i, 2, it2)
                # 색상
                for col in range(3):
                    cell = self._table.item(i, col)
                    if stt == DONE:
                        cell.setBackground(_BG_DONE); cell.setForeground(_FG_DONE)
                    elif stt == CURRENT:
                        cell.setBackground(_BG_CURRENT); cell.setForeground(_FG_CURRENT)
            # 중복 값 경고
            if len(set(f"{v:.6g}" for v in vals)) < len(vals):
                self._dup_lbl.setText("⚠ 중복된 값이 있습니다 — 저장 폴더는 인덱스로 구분됩니다.")
            else:
                self._dup_lbl.setText("")
        finally:
            self._refreshing = False

    def set_running(self, running: bool) -> None:
        """측정 중이면 '재생성'만 잠근다(전체 초기화 방지). 값 수정·행 추가/삭제는 유지."""
        self._btn_reset.setEnabled(not running)

    @Slot(int)
    def on_current(self, idx: int) -> None:
        self.refresh()

    @Slot(int)
    def on_done(self, idx: int) -> None:
        self.refresh()

    # ------------------------------------------------------------------
    # 편집
    # ------------------------------------------------------------------
    def _on_item_changed(self, item) -> None:
        if self._refreshing or item.column() != 1:
            return
        idx = item.row()
        try:
            v = float(item.text())
        except ValueError:
            self.refresh()   # 잘못된 입력 → 되돌림
            return
        if not self._model.set_value(idx, v):
            self.refresh()   # 완료/현재 행은 편집 거부 → 되돌림
        else:
            self.refresh()   # 표시 정규화(중복 경고 등)

    def _on_add(self) -> None:
        vals = self._model.values()
        default = vals[-1] if vals else 0.0
        self._model.append(default)
        self.refresh()

    def _on_del(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        if not self._model.remove(row):
            # 완료/현재 행은 삭제 불가
            self._dup_lbl.setText("완료·측정중 행은 삭제할 수 없습니다.")
        self.refresh()

    def _on_reset(self) -> None:
        # 측정 중엔 버튼이 비활성이라 여기 안 옴. VnaWindow가 Start/Stop/N로 재생성.
        try:
            self._vna._reset_second_table_from_controls()
        except Exception:
            pass
        self.refresh()
