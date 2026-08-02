"""
MetaDataConfigWindow: 메타 데이터 기록 설정 창.

- Parameter Manager에 등록된 measurement 항목에서 기록할 것을 체크박스로 선택
- 우측 미리보기: 저장될 JSON 구조를 실시간으로 표시 (값은 --- 로 표기)
- Temperature / Bfield class가 부여된 active measurement의 평균/표준편차는
  자동으로 포함됨 (선택 불필요, 미리보기에만 표시)
"""
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QPushButton, QScrollArea, QFrame, QWidget, QSizePolicy,
    QSplitter, QTextEdit,
)
from PySide6.QtGui import QFont

from pythonization.config.models import MetaDataConfig, MetaDataEntry, MeasType

if TYPE_CHECKING:
    from pythonization.ui.main_window import MainWindow

_MONO = QFont("Consolas", 9)


class MetaDataConfigWindow(QDialog):
    """메타 데이터 기록 설정 창."""

    def __init__(self, main_win: "MainWindow"):
        super().__init__(main_win)
        self.setWindowTitle("Meta Data Config")
        self.resize(640, 460)
        self._main_win = main_win
        self._row_checkboxes: list[QCheckBox] = []
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # Global enable toggle + 도움말 (?)
        top_row = QHBoxLayout()
        self._cb_enable = QCheckBox("Enable Meta Data")
        self._cb_enable.setStyleSheet("font-weight: bold;")
        self._cb_enable.stateChanged.connect(self._refresh_preview)
        top_row.addWidget(self._cb_enable)
        top_row.addStretch()
        self._btn_help = QPushButton("?")
        self._btn_help.setFixedSize(20, 20)
        self._btn_help.setStyleSheet(
            "QPushButton { background-color: #4ec9b0; color: #1e1e1e; border-radius: 10px;"
            "font-weight: bold; font-size: 11px; }"
            "QPushButton:hover { background-color: #6fe9d0; }"
        )
        self._btn_help.setToolTip(self._help_html())
        self._btn_help.clicked.connect(self._show_help)
        top_row.addWidget(self._btn_help)
        lay.addLayout(top_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #555;")
        lay.addWidget(sep)

        # Splitter: left = entry list, right = JSON preview
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: entry list ──────────────────────────────────────────
        left_w = QWidget()
        left_lay = QVBoxLayout(left_w)
        left_lay.setContentsMargins(0, 0, 4, 0)
        left_lay.setSpacing(4)

        # Header
        hdr = QWidget()
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(4, 0, 4, 0)
        hdr_lay.addWidget(QLabel("On"), 0)
        lbl_h_ax = QLabel("Figure Axis")
        lbl_h_ax.setFixedWidth(80)
        hdr_lay.addWidget(lbl_h_ax, 0)
        hdr_lay.addWidget(QLabel("Description / Alias"), 1)
        left_lay.addWidget(hdr)

        # Scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._inner = QWidget()
        self._inner_lay = QVBoxLayout(self._inner)
        self._inner_lay.setContentsMargins(0, 0, 0, 0)
        self._inner_lay.setSpacing(2)
        self._scroll.setWidget(self._inner)
        left_lay.addWidget(self._scroll, 1)

        # Auto T/B info
        self._lbl_auto = QLabel()
        self._lbl_auto.setStyleSheet("color: #888; font-size: 10px;")
        self._lbl_auto.setWordWrap(True)
        left_lay.addWidget(self._lbl_auto)

        splitter.addWidget(left_w)

        # ── Right: JSON preview ───────────────────────────────────────
        right_w = QWidget()
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(4, 0, 0, 0)
        right_lay.setSpacing(4)
        right_lay.addWidget(QLabel("저장 형태 미리보기"))
        self._preview = QTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setFont(_MONO)
        self._preview.setStyleSheet("background: #1e1e1e; color: #d4d4d4; border: 1px solid #444;")
        right_lay.addWidget(self._preview, 1)
        splitter.addWidget(right_w)

        splitter.setSizes([300, 320])
        lay.addWidget(splitter, 1)

        # Save button
        btn_row = QHBoxLayout()
        btn_save = QPushButton("Save && Close")
        btn_save.clicked.connect(self._on_save)
        self._btn_save = btn_save
        btn_row.addStretch()
        btn_row.addWidget(btn_save)
        lay.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def _help_html(self) -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>Meta Data 기능 안내</b><hr>"
            "측정 1회(sweep)가 끝날 때, 그 측정에 대한 <b>요약 메타 정보</b>를 "
            "데이터 파일(.dat)과 같은 이름의 <b>.json</b> 파일로 저장합니다.<br>"
            "(Double Sweep은 각 array step의 TRACE 파일 옆에 저장)<hr>"
            "<b>저장되는 값 — 2가지 방식</b><br>"
            "<table cellspacing='3' cellpadding='2'>"
            "<tr valign='top'><td><b>① 단일 쿼리</b></td>"
            "<td>일반 항목: <b>sweep 종료 시점에 1회 쿼리</b>한 값을 저장.<br>"
            "예: 종료 시 자기장·온도 한 번 읽기.</td></tr>"
            "<tr valign='top'><td><b>② 평균/표준편차</b></td>"
            "<td>아래 3조건을 <b>모두</b> 만족하면 sweep 전체 측정값의 "
            "<b>평균(mean)·표준편차(std)</b>를 자동 계산해 저장:<br>"
            "&nbsp;&nbsp;1) 측정의 class가 <b>Temperature</b> 또는 <b>Bfield</b><br>"
            "&nbsp;&nbsp;2) 해당 측정이 <b>active</b>(측정 패널에서 체크됨)<br>"
            "&nbsp;&nbsp;3) 이 창에서 <b>체크</b>됨</td></tr>"
            "</table><hr>"
            "<b>사용법</b><br>"
            "1. <b>Enable Meta Data</b>를 켭니다.<br>"
            "2. 목록(Parameter Manager에 등록된 항목)에서 저장할 것을 <b>체크</b>합니다.<br>"
            "3. 우측 <b>미리보기</b>에서 저장될 JSON 구조를 확인합니다 (값은 --- 표기).<br>"
            "4. <b>Save &amp; Close</b>로 저장. 이후 측정마다 자동으로 .json이 생성됩니다.<hr>"
            "<b>참고</b><br>"
            "• 목록 항목은 <b>Parameter Manager</b>에서 등록/관리합니다.<br>"
            "• class(Temperature/Bfield) 지정은 메인 창의 측정 행 콤보에서 합니다.<br>"
            "• 응답에 단위 접미사(T, A…)가 붙어도 숫자로 자동 파싱됩니다."
            "</body></html>"
        )

    def _show_help(self):
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle("Meta Data — 도움말")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(self._help_html())
        box.setIcon(QMessageBox.Icon.Information)
        box.exec()

    # ------------------------------------------------------------------
    # Populate / refresh
    # ------------------------------------------------------------------

    def _populate(self):
        """
        MetaDataConfig.entries (Parameter Manager에서 등록된 항목)만 표시합니다.
        체크박스 = 이번 sweep에서 활성화 여부 (per-sweep).
        """
        while self._inner_lay.count():
            item = self._inner_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._row_checkboxes.clear()

        cfg = self._main_win._param_manager_reg.meta_data_config

        self._cb_enable.blockSignals(True)
        self._cb_enable.setChecked(cfg.enabled)
        self._cb_enable.blockSignals(False)

        if not cfg.entries:
            empty_lbl = QLabel("(등록된 항목 없음 — Parameter Manager에서 추가하세요)")
            empty_lbl.setStyleSheet("color: #666; font-size: 10px;")
            empty_lbl.setWordWrap(True)
            self._inner_lay.addWidget(empty_lbl)
        else:
            for entry in cfg.entries:
                row_w = QWidget()
                row_lay = QHBoxLayout(row_w)
                row_lay.setContentsMargins(4, 1, 4, 1)

                cb = QCheckBox()
                cb.setChecked(entry.enabled)
                cb.stateChanged.connect(self._refresh_preview)
                row_lay.addWidget(cb, 0)

                lbl_ax = QLabel(entry.figure_axis or "—")
                lbl_ax.setFixedWidth(80)
                row_lay.addWidget(lbl_ax, 0)

                lbl_desc = QLabel(f"{entry.description}  [{entry.alias}]")
                lbl_desc.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                row_lay.addWidget(lbl_desc, 1)

                self._inner_lay.addWidget(row_w)
                self._row_checkboxes.append(cb)

        self._inner_lay.addStretch()

        # T/B 조건 3개 모두 만족하는 항목 안내
        tb_entries = [
            e for i, e in enumerate(cfg.entries)
            if (i < len(self._row_checkboxes) and self._row_checkboxes[i].isChecked()
                and self._get_runtime_meas_type(e) in (MeasType.TEMPERATURE, MeasType.BFIELD)
                and self._is_meas_active(e))
        ]
        if tb_entries:
            names = ", ".join(e.description for e in tb_entries)
            self._lbl_auto.setText(f"T/B class — sweep 중 평균·표준편차로 저장됨:\n{names}")
        else:
            self._lbl_auto.setText("")

        self._refresh_preview()

    def _find_meas_index(self, entry) -> int:
        """active_profile.measurements에서 alias+description 일치 인덱스 반환, 없으면 -1."""
        for i, m in enumerate(self._main_win._active_profile.measurements):
            if m.alias == entry.alias and m.description == entry.description:
                return i
        return -1

    def _get_runtime_meas_type(self, entry) -> MeasType:
        """entry에 대응하는 측정의 런타임 meas_type을 메인창 콤보박스에서 읽어옴."""
        idx = self._find_meas_index(entry)
        if idx >= 0:
            if idx < len(self._main_win._meas_type_combos):
                val = self._main_win._meas_type_combos[idx].currentData()
                try:
                    return MeasType(val)
                except Exception:
                    pass
            return self._main_win._active_profile.measurements[idx].meas_type
        return entry.meas_type

    def _is_meas_active(self, entry) -> bool:
        """active measurement 패널에서 해당 항목의 체크박스가 켜져 있는지 확인."""
        idx = self._find_meas_index(entry)
        if idx >= 0 and idx < len(self._main_win._meas_checkboxes):
            return self._main_win._meas_checkboxes[idx].isChecked()
        return False

    def _refresh_preview(self):
        """체크 상태에 따라 우측 JSON 미리보기를 갱신합니다."""
        if not self._cb_enable.isChecked():
            self._preview.setPlainText("(Meta Data 비활성화)")
            return

        cfg = self._main_win._param_manager_reg.meta_data_config
        lines = ["{", '  "timestamp": "---",']

        # Configured entries (checkbox = per-sweep activation)
        # 세 조건 모두 만족 시 mean/std, 아니면 일반 value 1회 쿼리
        for i, entry in enumerate(cfg.entries):
            if i >= len(self._row_checkboxes):
                break
            if not self._row_checkboxes[i].isChecked():          # 조건 3
                continue
            key = entry.figure_axis or entry.description
            unit_str = f'"{entry.unit}"' if entry.unit else '""'
            runtime_type = self._get_runtime_meas_type(entry)
            is_tb = (runtime_type in (MeasType.TEMPERATURE, MeasType.BFIELD)
                     and self._is_meas_active(entry))             # 조건 1+2
            if is_tb:
                lines.append(f'  "{key}_mean": {{ "value": ---, "unit": {unit_str} }},')
                lines.append(f'  "{key}_std":  {{ "value": ---, "unit": {unit_str} }},')
            else:
                lines.append(f'  "{key}": {{ "value": ---, "unit": {unit_str} }},')

        for i in range(len(lines) - 1, 0, -1):
            if lines[i].endswith(","):
                lines[i] = lines[i][:-1]
                break

        lines.append("}")
        self._preview.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------
    # Save / close
    # ------------------------------------------------------------------

    def _on_save(self):
        """체크박스 상태(per-sweep enabled)를 entries에 반영해 저장합니다."""
        existing_cfg = self._main_win._param_manager_reg.meta_data_config
        new_entries: list[MetaDataEntry] = []
        for i, entry in enumerate(existing_cfg.entries):
            enabled = self._row_checkboxes[i].isChecked() if i < len(self._row_checkboxes) else entry.enabled
            new_entries.append(entry.model_copy(update={"enabled": enabled}))
        cfg = MetaDataConfig(
            enabled=self._cb_enable.isChecked(),
            entries=new_entries,
        )
        self._main_win._param_manager_reg.save_meta_data_config(cfg)
        self.hide()

    def showEvent(self, event):
        self._populate()
        super().showEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def lock_ui(self, locked: bool) -> None:
        """측정 중 UI 잠금/해제."""
        self._cb_enable.setEnabled(not locked)
        self._scroll.setEnabled(not locked)
        self._btn_save.setEnabled(not locked)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._on_save()
            self.hide()          # 다른 창들과 동일하게 Esc = 저장 후 닫기
            event.accept()
            return
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_S):
            self._on_save()      # Ctrl+S = 저장(닫지 않음)
            event.accept()
            return
        super().keyPressEvent(event)
