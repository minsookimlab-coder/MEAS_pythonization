"""
VnaCalibrationWindow: VNA 교정(calibration) 전용 창.

VISA Library 에 등록된 명령을 골라 두면 그것들이 그대로 **버튼**이 된다.
버튼을 누르면 그 명령 하나를 바로 보내고, 전역 OPC 쿼리의 응답이 1 이 될 때까지
**모든 버튼을 잠근다**. 교정은 한 단계가 끝나기 전에 다음 단계를 누르면 안 되기
때문이다.

예) SOLT 교정
    [1port short] 클릭 -> short 교정 명령 전송 -> OPC 대기 -> 잠금 해제
    [1port open]  클릭 -> ...

설정은 프로파일이 아니라 **전역**으로 저장한다 — 교정 절차는 장비의 성질이지
측정 프로파일마다 달라지는 값이 아니다. (SETTINGS_DIR/vna_calibration.yaml)
"""
from typing import List, Optional, Tuple, TYPE_CHECKING

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QGroupBox, QFrame, QMessageBox, QWidget, QLineEdit, QSizePolicy,
)

from gui.vna_models import (
    VnaCalibrationConfig, VnaCommandEntry, build_cmd, get_figure_axis,
    get_template, load_vna_calibration, save_vna_calibration,
)

if TYPE_CHECKING:
    from core.visa_library_registry import VisaLibraryRegistry

#: 버튼을 한 줄에 몇 개까지 놓을지
_COLS = 3


class _CalButton(QPushButton):
    """교정 명령 하나에 대응하는 버튼."""

    def __init__(self, entry: VnaCommandEntry, lib, parent=None):
        label = get_figure_axis(lib, entry) or entry.description or entry.alias
        super().__init__(label, parent)
        self.entry = entry
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(f"[{entry.alias}] {entry.description}")


class VnaCalibrationWindow(QDialog):
    """교정 버튼 창. 실제 VISA 실행은 VnaWindow 가 맡고, 이 창은 요청만 올려 보낸다."""

    def __init__(self, lib_reg: "VisaLibraryRegistry", runner, parent=None):
        """runner — 실행을 담당하는 객체(VnaWindow).
        run_calibration(cmds, opc_cmds, on_finished) 를 제공해야 한다."""
        super().__init__(parent)
        self.setWindowTitle("VNA Calibration")
        self.setMinimumWidth(560)
        self.resize(720, 520)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self._lib_reg = lib_reg
        self._runner = runner
        self._cfg: VnaCalibrationConfig = load_vna_calibration()
        self._buttons: List[_CalButton] = []
        self._inputs: dict = {}          # _CalButton -> QLineEdit (값이 필요한 명령만)
        self._busy = False

        self._build_ui()
        self._rebuild_buttons()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        hdr = QHBoxLayout()
        title = QLabel("VNA Calibration")
        title.setStyleSheet("font-weight: bold; font-size: 13px; color: #79c0ff;")
        hdr.addWidget(title)
        hdr.addStretch()
        btn_cfg = QPushButton("버튼 구성…")
        btn_cfg.setFixedHeight(24)
        btn_cfg.setToolTip("VISA Library 에서 교정 명령을 골라 버튼으로 만든다.")
        btn_cfg.clicked.connect(self._open_editor)
        hdr.addWidget(btn_cfg)
        root.addLayout(hdr)

        note = QLabel(
            "버튼을 누르면 그 명령을 바로 보내고, OPC 응답이 올 때까지 모든 버튼이 "
            "잠깁니다. 설정은 프로파일과 무관하게 전역으로 저장됩니다."
        )
        note.setStyleSheet("color: #888888; font-size: 11px;")
        note.setWordWrap(True)
        root.addWidget(note)

        self._grp = QGroupBox("교정 명령")
        self._grid = QGridLayout(self._grp)
        self._grid.setSpacing(6)
        root.addWidget(self._grp, stretch=1)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(line)

        self._lbl_opc = QLabel()
        self._lbl_opc.setStyleSheet("color: #888888; font-size: 11px;")
        self._lbl_opc.setWordWrap(True)
        root.addWidget(self._lbl_opc)

        self._lbl_status = QLabel("준비")
        self._lbl_status.setStyleSheet("color: #888888;")
        self._lbl_status.setWordWrap(True)
        root.addWidget(self._lbl_status)

    def _rebuild_buttons(self):
        """설정의 buttons 목록으로 버튼 격자를 다시 만든다."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons = []
        self._inputs = {}

        entries = [e for e in self._cfg.buttons if e.enabled]
        if not entries:
            lbl = QLabel("등록된 교정 명령이 없습니다 — [버튼 구성…] 에서 "
                         "VISA Library 의 명령을 고르세요.")
            lbl.setStyleSheet("color: #555555;")
            lbl.setWordWrap(True)
            self._grid.addWidget(lbl, 0, 0, 1, _COLS)
        else:
            for i, entry in enumerate(entries):
                try:
                    lib = self._lib_reg.get_library(entry.alias)
                except Exception:
                    continue
                btn = _CalButton(entry, lib, self)
                btn.clicked.connect(lambda _c=False, b=btn: self._on_button(b))
                row, col = divmod(i, _COLS)
                if any(p.is_user_input for p in entry.params):
                    # 값이 필요한 명령 — 버튼 옆에 입력칸을 붙인다
                    cell = QWidget()
                    h = QHBoxLayout(cell)
                    h.setContentsMargins(0, 0, 0, 0)
                    h.setSpacing(4)
                    h.addWidget(btn, stretch=1)
                    le = QLineEdit()
                    le.setFixedWidth(70)
                    up = next(p for p in entry.params if p.is_user_input)
                    le.setPlaceholderText(f"[{up.name}]")
                    if up.value:
                        le.setText(up.value)
                    h.addWidget(le)
                    self._inputs[btn] = le
                    self._grid.addWidget(cell, row, col)
                else:
                    self._grid.addWidget(btn, row, col)
                self._buttons.append(btn)

        opc_names = [e.description for e in self._cfg.opc if e.enabled]
        self._lbl_opc.setText(
            "완료 대기(OPC): " + (", ".join(opc_names) if opc_names else
                                  "없음 — 명령을 보내고 대기 없이 바로 끝납니다.")
        )

    # ------------------------------------------------------------------
    # 설정 편집
    # ------------------------------------------------------------------

    def _open_editor(self):
        from gui.vna_calibration_editor import VnaCalibrationEditor
        dlg = VnaCalibrationEditor(self._lib_reg, self._cfg, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._cfg = dlg.result_config()
            save_vna_calibration(self._cfg)
            self._rebuild_buttons()
            self._set_status("설정을 저장했습니다.", "#7ee787")

    # ------------------------------------------------------------------
    # 실행
    # ------------------------------------------------------------------

    def _resolve(self, entry: VnaCommandEntry,
                 user_value: str = "") -> Optional[Tuple[str, str, bool]]:
        """라이브러리 템플릿을 채워 (alias, cmd, is_query) 로 만든다."""
        try:
            lib = self._lib_reg.get_library(entry.alias)
            template = get_template(lib, entry)
            if template is None:
                return None
            return (entry.alias, build_cmd(template, entry.params, user_value),
                    entry.cmd_type == "query")
        except Exception:
            return None

    def _opc_pairs(self) -> list:
        out = []
        for entry in self._cfg.opc:
            if not entry.enabled:
                continue
            resolved = self._resolve(entry)
            if resolved:
                out.append((resolved[0], resolved[1]))
        return out

    def _on_button(self, btn: _CalButton):
        if self._busy:
            return
        le = self._inputs.get(btn)
        resolved = self._resolve(btn.entry, le.text().strip() if le else "")
        if resolved is None:
            QMessageBox.warning(
                self, "명령을 만들 수 없습니다",
                f"'{btn.entry.description}' 의 VISA 명령을 찾지 못했습니다.\n"
                "VISA Library 에서 이 명령이 지워졌거나 이름이 바뀌었는지 확인하세요.")
            return
        ok, why = self._runner.run_calibration(
            [resolved], self._opc_pairs(), self._on_finished)
        if not ok:
            self._set_status(why or "실행할 수 없습니다.", "#f78166")
            return
        self._set_busy(True)
        self._set_status(f"{btn.text()} 실행 중…", "#d7ba7d")

    def _on_finished(self, error: Optional[str]):
        self._set_busy(False)
        if error:
            self._set_status(f"실패: {error}", "#f78166")
        else:
            self._set_status("완료.", "#7ee787")

    def _set_busy(self, busy: bool):
        self._busy = busy
        for b in self._buttons:
            b.setEnabled(not busy)
        for le in self._inputs.values():
            le.setEnabled(not busy)

    def _set_status(self, text: str, color: str = "#888888"):
        self._lbl_status.setText(text)
        self._lbl_status.setStyleSheet(f"color: {color};")

    # ------------------------------------------------------------------

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._busy:
            # 실행 중 창을 닫아도 워커는 VnaWindow 가 관리한다. 숨기기만 한다.
            self._set_status("실행 중 — 창을 닫아도 명령은 계속됩니다.", "#d7ba7d")
        event.ignore()
        self.hide()
