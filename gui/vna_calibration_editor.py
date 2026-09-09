"""
VnaCalibrationEditor: Calibration 창의 버튼 목록과 전역 OPC 를 편집하는 다이얼로그.

명령 편집 UI 는 VNA Config 의 것을 그대로 재사용한다 (_CmdListWidget). 같은 일을
하는 목록 위젯을 두 벌 만들면 한쪽만 고쳐지는 일이 생긴다.
"""
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
)

from gui.vna_config_window import _CmdListWidget
from gui.vna_models import VnaCalibrationConfig

if TYPE_CHECKING:
    from core.visa_library_registry import VisaLibraryRegistry


class VnaCalibrationEditor(QDialog):

    def __init__(self, lib_reg: "VisaLibraryRegistry",
                 cfg: VnaCalibrationConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Calibration 버튼 구성")
        self.setMinimumWidth(640)
        self.resize(760, 560)
        self._lib_reg = lib_reg
        self._result = cfg.model_copy(deep=True)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        note = QLabel(
            "VISA Library 에 등록된 명령을 고르면 그 항목 하나가 Calibration 창의 "
            "버튼 하나가 됩니다. 순서(↑ ↓)가 버튼 배치 순서입니다."
        )
        note.setStyleSheet("color: #888888; font-size: 11px;")
        note.setWordWrap(True)
        root.addWidget(note)

        grp_btn = QGroupBox("교정 명령 (버튼이 될 항목)")
        lay_btn = QVBoxLayout(grp_btn)
        lay_btn.setContentsMargins(6, 8, 6, 6)
        self._list_buttons = _CmdListWidget(lib_reg, mode="write")
        self._list_buttons.set_commands(self._result.buttons)
        lay_btn.addWidget(self._list_buttons)
        root.addWidget(grp_btn, stretch=3)

        grp_opc = QGroupBox("완료 대기 OPC (전역 — 모든 버튼이 공유)")
        lay_opc = QVBoxLayout(grp_opc)
        lay_opc.setContentsMargins(6, 8, 6, 6)
        hint = QLabel("버튼을 누른 뒤 이 쿼리의 응답이 1 이 될 때까지 모든 버튼이 "
                      "잠깁니다. 비워 두면 대기 없이 바로 끝납니다.")
        hint.setStyleSheet("color: #888888; font-size: 11px;")
        hint.setWordWrap(True)
        lay_opc.addWidget(hint)
        self._list_opc = _CmdListWidget(lib_reg, mode="read")
        self._list_opc.set_commands(self._result.opc)
        lay_opc.addWidget(self._list_opc)
        root.addWidget(grp_opc, stretch=2)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("취소")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_ok = QPushButton("저장")
        btn_ok.setMinimumHeight(28)
        btn_ok.setStyleSheet(
            "QPushButton{background:#1f4e8c;color:white;"
            "border-radius:4px;font-weight:bold;}"
        )
        btn_ok.clicked.connect(self._on_ok)
        btn_row.addWidget(btn_ok)
        root.addLayout(btn_row)

    def _on_ok(self):
        self._result.buttons = self._list_buttons.get_commands()
        self._result.opc = self._list_opc.get_commands()
        self.accept()

    def result_config(self) -> VnaCalibrationConfig:
        return self._result

    def keyPressEvent(self, event):
        # Esc 로 실수로 닫혀 편집분이 날아가지 않게 — 취소 버튼으로만 닫는다
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            return
        super().keyPressEvent(event)
