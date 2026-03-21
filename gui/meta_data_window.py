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

from config.config_models import MetaDataConfig, MetaDataEntry, MeasType

if TYPE_CHECKING:
    from gui.main_window import MainWindow

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

        # Global enable toggle
        self._cb_enable = QCheckBox("Enable Meta Data")
        self._cb_enable.setStyleSheet("font-weight: bold;")
        self._cb_enable.stateChanged.connect(self._refresh_preview)
        lay.addWidget(self._cb_enable)

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
        else:
            super().keyPressEvent(event)
