import math
import time

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton,
    QDoubleSpinBox, QFormLayout, QFrame, QMessageBox,
    QButtonGroup, QRadioButton, QCheckBox, QFileDialog,
    QComboBox, QInputDialog, QScrollArea,
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QAction, QFont

from core.data_saver import DataSaver
from core.derivative_channel import DerivativeChannel, DerivativeConfig, OUTPUT_KEY as _DERIV_KEY
from core.instrument_registry import InstrumentRegistry
from core.instrument_session import InstrumentSession
from core.sweep import SweepConfig, calculate_next_step
from core.sweep_channel import sweep_channel_from_instantiated, TimeChannel, TIME_CHANNEL
from core.sweep_worker import SweepWorker, StepRequest, StepResult
from core.visa_library_registry import VisaLibraryRegistry
from core.profile_registry import ProfileRegistry
from config.config_models import MainUIProfile
from gui.console_handler import ConsoleCommand, ConsoleCommandHandler
from gui.debug_window import DebugWindow
from gui.sweep_array_window import SweepArrayWindow

_MONO = QFont("Consolas", 10)

_ALIAS_PALETTE = [
    "#79c0ff",  # blue
    "#f78166",  # salmon
    "#ffa657",  # orange
    "#d2a8ff",  # lavender
    "#7ee787",  # green
    "#ff7b72",  # red
    "#56d364",  # bright green
    "#a5f3fc",  # cyan
    "#fde68a",  # yellow
]


class MainWindow(QMainWindow):
    """
    메인 애플리케이션 윈도우.
    - Sweep 파라미터 입력 및 Start/Stop 제어
    - 루프 동작 중 테두리 글로우 애니메이션
    - Debug/SweepArray 는 별도 창으로 분리
    """

    # Worker 스레드로 step 요청 전달
    request_step = Signal(object)   # StepRequest
    # VISA 로그를 메인 스레드로 릴레이 (worker 스레드에서 호출되므로 Signal 경유)
    _visa_log_relay = Signal(str, str, str, str, object)  # alias, addr, cmd_type, cmd, result

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Measurement System")
        self.resize(860, 560)

        self._settings_window = None
        self._visa_lib_window = None
        self._double_sweep_window = None
        self._graph_window = None
        self._registry = InstrumentRegistry()
        self._visa_lib_registry = VisaLibraryRegistry()
        self._session = InstrumentSession(self._registry)
        self._cmd_handler = ConsoleCommandHandler(self._session)
        self._sweep_config = SweepConfig()
        self._sweep_channel = None
        self._meas_checkboxes: list[QCheckBox] = []
        self._active_meas_indices: list[int] = []
        self._param_manager_reg = ProfileRegistry()
        self._param_manager_window = None
        self._active_profile: MainUIProfile = MainUIProfile()
        self._running = False
        self._loading_profile = False
        self._alias_color_map: dict = {}
        self._meas_suffix_edits: list = []
        self._deriv_channel: DerivativeChannel = DerivativeChannel(DerivativeConfig())
        self._sweep_step_count = 0
        self._tick_start: float = 0.0
        self._last_write_value: "float | None" = None
        self._data_saver = DataSaver()
        self._data_saver.set_error_callback(
            lambda msg: self._log(f"  [DataSaver] {msg}", color="#f44747")
        )

        # Worker 스레드 셋업
        self._worker = SweepWorker()
        self._worker.set_session(self._session)
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self.request_step.connect(self._worker.run_step)
        self._worker.step_done.connect(self._on_step_done)
        self._worker.step_error.connect(self._on_step_error)
        self._worker_thread.start()

        # 글로우 애니메이션
        self._glow_phase = 0.0
        self._glow_timer = QTimer(self)
        self._glow_timer.setInterval(30)
        self._glow_timer.timeout.connect(self._update_glow)

        # 스텝 타이머 (single-shot, time_per_point마다 한 스텝)
        self._sweep_step_timer = QTimer(self)
        self._sweep_step_timer.setSingleShot(True)
        self._sweep_step_timer.timeout.connect(self._sweep_tick)

        # 자식 창
        self._debug_window = DebugWindow(self)
        self._sweep_status_window = SweepArrayWindow(self)
        from gui.timing_window import TimingWindow
        from gui.data_window import DataWindow
        self._timing_window = TimingWindow(self)
        self._data_window = DataWindow(self)

        # 콘솔 입력 → 메인 핸들러 연결
        self._debug_window.set_submit_callback(self._handle_command)

        # VISA 로그 → 릴레이 Signal 경유로 메인 스레드에서 디버그 창 갱신
        self._visa_log_relay.connect(self._apply_visa_log)
        self._session.add_log_callback(self._on_visa_log)

        self._setup_menu()
        self._setup_ui()

        self._log("System ready.")
        self._log("등록된 장비: " + ", ".join(self._registry.list_aliases() or ["(없음)"]))

        # 마지막으로 사용한 프로파일 전체 복원 (sweep params, 폴더, sweep channel 포함)
        self._apply_active_profile()

    # ------------------------------------------------------------------
    # UI Setup
    # ------------------------------------------------------------------

    def _setup_menu(self):
        menubar = self.menuBar()

        settings_menu = menubar.addMenu("Settings")
        act_instruments = QAction("Instrument Settings...", self)
        act_instruments.triggered.connect(self._open_instrument_settings)
        settings_menu.addAction(act_instruments)
        act_visa_lib = QAction("VISA Library...", self)
        act_visa_lib.triggered.connect(self._open_visa_library)
        settings_menu.addAction(act_visa_lib)
        act_pm = QAction("Parameter Manager...", self)
        act_pm.triggered.connect(self._open_parameter_manager)
        settings_menu.addAction(act_pm)

        view_menu = menubar.addMenu("View")
        act_debug = QAction("Debug Window", self)
        act_debug.triggered.connect(lambda: self._debug_window.show())
        view_menu.addAction(act_debug)
        act_status = QAction("Sweep Status", self)
        act_status.triggered.connect(lambda: self._sweep_status_window.show())
        view_menu.addAction(act_status)
        act_timing = QAction("Timing", self)
        act_timing.triggered.connect(lambda: self._timing_window.show())
        view_menu.addAction(act_timing)
        act_data = QAction("Data", self)
        act_data.triggered.connect(lambda: self._data_window.show())
        view_menu.addAction(act_data)
        act_graph = QAction("Graph...", self)
        act_graph.triggered.connect(self._open_graph_window)
        view_menu.addAction(act_graph)
        view_menu.addSeparator()
        act_double = QAction("Double Sweep...", self)
        act_double.triggered.connect(self._open_double_sweep)
        view_menu.addAction(act_double)

    def _setup_ui(self):
        self._glow_frame = QFrame()
        self._glow_frame.setObjectName("glowFrame")
        self._glow_frame.setStyleSheet(
            "QFrame#glowFrame { border: 3px solid transparent; border-radius: 6px; }"
        )
        self.setCentralWidget(self._glow_frame)

        outer = QVBoxLayout(self._glow_frame)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addWidget(self._build_profile_panel())
        outer.addWidget(self._build_sequence_panel())

    def _build_profile_panel(self) -> QWidget:
        panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(4)

        row.addWidget(QLabel("Profile:"))
        self._combo_profile = QComboBox()
        self._combo_profile.setMinimumWidth(160)
        self._combo_profile.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        row.addWidget(self._combo_profile, stretch=1)

        btn_add = QPushButton("Add")
        btn_add.setFixedWidth(46)
        btn_add.clicked.connect(self._profile_add)
        btn_dup = QPushButton("Copy")
        btn_dup.setFixedWidth(46)
        btn_dup.clicked.connect(self._profile_duplicate)
        btn_del = QPushButton("Delete")
        btn_del.setFixedWidth(52)
        btn_del.clicked.connect(self._profile_delete)
        row.addWidget(btn_add)
        row.addWidget(btn_dup)
        row.addWidget(btn_del)

        self._refresh_profile_combo()
        self._combo_profile.currentTextChanged.connect(self._on_profile_combo_changed)
        return panel

    def _refresh_profile_combo(self):
        self._combo_profile.blockSignals(True)
        self._combo_profile.clear()
        for name in self._param_manager_reg.list_profiles():
            self._combo_profile.addItem(name)
        idx = self._combo_profile.findText(self._param_manager_reg.active_name)
        self._combo_profile.setCurrentIndex(max(idx, 0))
        self._combo_profile.blockSignals(False)

    def _on_profile_combo_changed(self, name: str):
        if not name or name == self._param_manager_reg.active_name:
            return
        self._save_current_to_active_profile()
        self._param_manager_reg.set_active(name)
        self._apply_active_profile()

    def _profile_add(self):
        name, ok = QInputDialog.getText(self, "Add Profile", "프로파일 이름:")
        if not ok or not name.strip():
            return
        actual = self._param_manager_reg.add_profile(name.strip())
        self._save_current_to_active_profile()
        self._param_manager_reg.set_active(actual)
        self._refresh_profile_combo()
        self._combo_profile.blockSignals(True)
        self._combo_profile.setCurrentText(actual)
        self._combo_profile.blockSignals(False)
        self._apply_active_profile()

    def _profile_duplicate(self):
        new_name = self._param_manager_reg.duplicate_profile(
            self._param_manager_reg.active_name
        )
        self._save_current_to_active_profile()
        self._param_manager_reg.set_active(new_name)
        self._refresh_profile_combo()
        self._combo_profile.blockSignals(True)
        self._combo_profile.setCurrentText(new_name)
        self._combo_profile.blockSignals(False)

    def _profile_delete(self):
        name = self._param_manager_reg.active_name
        reply = QMessageBox.question(
            self, "프로파일 삭제",
            f"'{name}' 프로파일을 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._param_manager_reg.delete_profile(name)
        except ValueError as e:
            QMessageBox.warning(self, "삭제 불가", str(e))
            return
        self._refresh_profile_combo()
        self._apply_active_profile()

    def _save_current_to_active_profile(self):
        """현재 UI 상태를 활성 프로파일에 저장."""
        if self._loading_profile:
            return
        fp = self._param_manager_reg.get_active_profile()
        fp.main_ui = self._active_profile
        fp.sweep_to = self._sweep_config.sweep_to
        fp.sweep_rate = self._sweep_config.sweep_rate
        fp.time_per_point = self._sweep_config.time_per_point
        fp.main_folder = self._le_main_folder.text()
        fp.custom_folder = self._le_custom_folder.text()
        fp.custom_word = self._le_custom_word.text()
        fp.include_date = self._cb_save_date.isChecked()
        fp.save_enabled = self._cb_save_enable.isChecked()
        fp.active_sweep_channel_idx = self._sweep_radio_group.checkedId()
        self._param_manager_reg.save_active_profile(fp)

    def _apply_active_profile(self):
        """활성 프로파일 설정을 UI에 적용."""
        self._loading_profile = True
        try:
            fp = self._param_manager_reg.get_active_profile()
            rebuilt = self._param_manager_reg.rebuild_main_ui_from_library(self._visa_lib_registry)
            if rebuilt.sweep_values or rebuilt.measurements or rebuilt.write_cmds:
                self._param_manager_reg.save_main_ui(rebuilt)
                self._on_selection_applied(rebuilt)
            else:
                self._rebuild_sweep_channel_panel([])
            # Apply sweep params (block signals to avoid recursive saves)
            for sb, val in [
                (self._sb_sweep_to,       fp.sweep_to),
                (self._sb_sweep_rate,     fp.sweep_rate),
                (self._sb_time_per_point, fp.time_per_point),
            ]:
                sb.blockSignals(True)
                sb.setValue(val)
                sb.blockSignals(False)
            self._sweep_config.sweep_to       = fp.sweep_to
            self._sweep_config.sweep_rate     = fp.sweep_rate
            self._sweep_config.time_per_point = fp.time_per_point
            self._timing_window.set_time_per_point(fp.time_per_point)
            self._update_step_size_label()
            # Apply save settings
            self._le_main_folder.blockSignals(True)
            self._le_custom_folder.blockSignals(True)
            self._le_custom_word.blockSignals(True)
            self._le_main_folder.setText(fp.main_folder)
            self._le_custom_folder.setText(fp.custom_folder)
            self._le_custom_word.setText(fp.custom_word)
            self._le_main_folder.blockSignals(False)
            self._le_custom_folder.blockSignals(False)
            self._le_custom_word.blockSignals(False)
            self._cb_save_date.setChecked(fp.include_date)
            self._cb_save_enable.setChecked(fp.save_enabled)
            self._update_save_preview()
            # Restore sweep channel selection
            btn = self._sweep_radio_group.button(fp.active_sweep_channel_idx)
            if btn:
                btn.setChecked(True)
        finally:
            self._loading_profile = False

    def _build_sequence_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        # --- 제목 + 창 열기 버튼 ---
        title_row = QHBoxLayout()
        title = QLabel("Measurement Sequence")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        title_row.addWidget(title)
        title_row.addStretch()
        btn_debug = QPushButton("Debug")
        btn_debug.setFixedWidth(70)
        btn_debug.clicked.connect(lambda: self._debug_window.show())
        btn_array = QPushButton("Status")
        btn_array.setFixedWidth(70)
        btn_array.clicked.connect(lambda: self._sweep_status_window.show())
        title_row.addWidget(btn_debug)
        title_row.addWidget(btn_array)
        layout.addLayout(title_row)

        # --- Sweep Parameters ---
        sweep_box = QFrame()
        sweep_box.setFrameShape(QFrame.Shape.StyledPanel)
        sweep_layout = QVBoxLayout(sweep_box)

        sweep_title = QLabel("Sweep Parameters")
        sweep_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        sweep_layout.addWidget(sweep_title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        sweep_layout.addLayout(form)

        self._le_source_value = QLineEdit("—")
        self._le_source_value.setReadOnly(True)
        self._le_source_value.setStyleSheet("color: #888888;")
        self._le_source_value.setFixedWidth(120)
        form.addRow("Source Value:", self._le_source_value)

        self._sb_sweep_to = QDoubleSpinBox()
        self._sb_sweep_to.setRange(-1e9, 1e9)
        self._sb_sweep_to.setDecimals(4)
        self._sb_sweep_to.setValue(0.0)
        self._sb_sweep_to.setFixedWidth(140)
        self._sb_sweep_to.valueChanged.connect(self._update_step_size_label)
        self._sb_sweep_to.editingFinished.connect(self._on_sweep_params_confirmed)
        form.addRow("Sweep To:", self._sb_sweep_to)

        self._sb_sweep_rate = QDoubleSpinBox()
        self._sb_sweep_rate.setRange(1e-6, 1e9)
        self._sb_sweep_rate.setDecimals(4)
        self._sb_sweep_rate.setValue(1.0)
        self._sb_sweep_rate.setSuffix("  units/min")
        self._sb_sweep_rate.setFixedWidth(180)
        self._sb_sweep_rate.valueChanged.connect(self._update_step_size_label)
        self._sb_sweep_rate.editingFinished.connect(self._on_sweep_params_confirmed)
        form.addRow("Sweep Rate:", self._sb_sweep_rate)

        self._sb_time_per_point = QDoubleSpinBox()
        self._sb_time_per_point.setRange(0.001, 3600)
        self._sb_time_per_point.setDecimals(3)
        self._sb_time_per_point.setValue(1.0)
        self._sb_time_per_point.setSuffix("  sec")
        self._sb_time_per_point.setFixedWidth(180)
        self._sb_time_per_point.valueChanged.connect(self._update_step_size_label)
        self._sb_time_per_point.editingFinished.connect(self._on_sweep_params_confirmed)
        form.addRow("Time / Point:", self._sb_time_per_point)

        self._lbl_step_size = QLabel("—")
        self._lbl_step_size.setStyleSheet("color: #555555;")
        form.addRow("Step Size:", self._lbl_step_size)

        self._lbl_idle = QLabel("—")
        self._lbl_idle.setStyleSheet("color: #555555;")
        form.addRow("Idle:", self._lbl_idle)

        self._lbl_remaining = QLabel("—")
        self._lbl_remaining.setStyleSheet("color: #555555;")
        form.addRow("Remaining:", self._lbl_remaining)

        layout.addWidget(sweep_box)

        # --- Sweep Channel panel (radio buttons, populated by profile) ---
        self._sweep_channel_panel = QFrame()
        self._sweep_channel_panel.setFrameShape(QFrame.Shape.StyledPanel)
        sc_outer = QVBoxLayout(self._sweep_channel_panel)
        sc_outer.setContentsMargins(8, 6, 8, 6)
        sc_outer.setSpacing(4)
        sc_title = QLabel("Sweep Channel")
        sc_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #79c0ff;")
        sc_outer.addWidget(sc_title)
        self._sweep_ch_scroll = QScrollArea()
        self._sweep_ch_scroll.setWidgetResizable(True)
        self._sweep_ch_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._sweep_ch_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._sweep_ch_scroll.setMaximumHeight(150)
        self._sweep_ch_scroll.setStyleSheet("background: transparent;")
        sc_outer.addWidget(self._sweep_ch_scroll)
        self._sweep_radio_group = QButtonGroup(self)
        self._sweep_radio_group.setExclusive(True)
        self._sweep_radio_group.idToggled.connect(self._on_sweep_radio_toggled)
        self._sweep_channel_panel.setVisible(False)
        layout.addWidget(self._sweep_channel_panel)

        # --- Active Measurements panel (checkboxes + suffix) ---
        self._meas_panel = QFrame()
        self._meas_panel.setFrameShape(QFrame.Shape.StyledPanel)
        meas_outer = QVBoxLayout(self._meas_panel)
        meas_outer.setContentsMargins(8, 6, 8, 6)
        meas_outer.setSpacing(4)
        m_title = QLabel("Active Measurements")
        m_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #56d364;")
        meas_outer.addWidget(m_title)
        self._meas_scroll = QScrollArea()
        self._meas_scroll.setWidgetResizable(True)
        self._meas_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._meas_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._meas_scroll.setMaximumHeight(180)
        self._meas_scroll.setStyleSheet("background: transparent;")
        meas_outer.addWidget(self._meas_scroll)
        self._meas_panel.setVisible(False)
        layout.addWidget(self._meas_panel)

        # --- Write Commands panel ---
        self._write_panel = QFrame()
        self._write_panel.setFrameShape(QFrame.Shape.StyledPanel)
        self._write_layout = QVBoxLayout(self._write_panel)
        self._write_layout.setContentsMargins(8, 6, 8, 6)
        w_title = QLabel("Write Commands")
        w_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #e3b341;")
        self._write_layout.addWidget(w_title)
        self._write_panel.setVisible(False)
        layout.addWidget(self._write_panel)

        # --- Save Settings panel ---
        save_box = QFrame()
        save_box.setFrameShape(QFrame.Shape.StyledPanel)
        save_layout = QVBoxLayout(save_box)
        save_layout.setContentsMargins(8, 6, 8, 6)
        save_layout.setSpacing(4)

        save_title = QLabel("Save Settings")
        save_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #e3b341;")
        save_layout.addWidget(save_title)

        save_form = QFormLayout()
        save_form.setHorizontalSpacing(8)
        save_form.setVerticalSpacing(3)
        save_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        mf_row = QHBoxLayout()
        self._le_main_folder = QLineEdit()
        self._le_main_folder.setFont(_MONO)
        self._le_main_folder.setPlaceholderText("Main 폴더")
        self._le_main_folder.textChanged.connect(self._update_save_preview)
        btn_browse = QPushButton("Browse")
        btn_browse.setFixedWidth(60)
        btn_browse.clicked.connect(self._browse_save_folder)
        mf_row.addWidget(self._le_main_folder)
        mf_row.addWidget(btn_browse)
        save_form.addRow("Main Folder:", mf_row)

        self._le_custom_folder = QLineEdit()
        self._le_custom_folder.setFont(_MONO)
        self._le_custom_folder.setPlaceholderText("하위 폴더 (선택)")
        self._le_custom_folder.textChanged.connect(self._update_save_preview)
        save_form.addRow("Sub Folder:", self._le_custom_folder)

        fn_row = QHBoxLayout()
        self._le_custom_word = QLineEdit()
        self._le_custom_word.setFont(_MONO)
        self._le_custom_word.setPlaceholderText("접두어 (선택)")
        self._le_custom_word.textChanged.connect(self._update_save_preview)
        self._cb_save_date = QCheckBox("날짜")
        self._cb_save_date.setChecked(True)
        self._cb_save_date.stateChanged.connect(self._update_save_preview)
        fn_row.addWidget(self._le_custom_word)
        fn_row.addWidget(self._cb_save_date)
        save_form.addRow("Filename:", fn_row)

        save_layout.addLayout(save_form)

        preview_row = QHBoxLayout()
        self._lbl_save_preview = QLabel("—")
        self._lbl_save_preview.setFont(_MONO)
        self._lbl_save_preview.setStyleSheet("color: #555555; font-size: 9px;")
        self._lbl_save_preview.setWordWrap(True)
        btn_copy_path = QPushButton("Copy")
        btn_copy_path.setFixedWidth(46)
        btn_copy_path.setFixedHeight(20)
        btn_copy_path.setFont(QFont("Consolas", 8))
        btn_copy_path.clicked.connect(self._copy_save_path)
        preview_row.addWidget(QLabel("→"))
        preview_row.addWidget(self._lbl_save_preview, stretch=1)
        preview_row.addWidget(btn_copy_path)
        save_layout.addLayout(preview_row)

        self._cb_save_enable = QCheckBox("Auto-save 활성화")
        self._cb_save_enable.setChecked(False)
        self._cb_save_enable.stateChanged.connect(self._update_save_preview)
        save_layout.addWidget(self._cb_save_enable)

        layout.addWidget(save_box)
        layout.addWidget(self._build_deriv_panel())

        # --- Start / Stop ---
        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("Start")
        self._btn_start.setMinimumHeight(44)
        self._btn_start.setStyleSheet(
            "QPushButton { font-weight: bold; font-size: 14px;"
            "background-color: #2e7d32; color: white; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #1a3a1a; color: #3d553d; border-radius: 4px; }"
        )
        self._btn_start.clicked.connect(self._on_start)

        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setMinimumHeight(44)
        self._btn_stop.setStyleSheet(
            "QPushButton { font-weight: bold; font-size: 14px;"
            "background-color: #c62828; color: white; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #3a1a1a; color: #553d3d; border-radius: 4px; }"
        )
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)

        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_stop)
        layout.addLayout(btn_row)

        layout.addStretch()
        return panel

    # ------------------------------------------------------------------
    # Glow animation
    # ------------------------------------------------------------------

    def _update_glow(self):
        self._glow_phase += 0.07
        intensity = (math.sin(self._glow_phase) + 1) / 2
        alpha = int(80 + intensity * 140)
        green = int(140 + intensity * 80)
        self._glow_frame.setStyleSheet(
            f"QFrame#glowFrame {{"
            f"border: 3px solid rgba(40, {green}, 70, {alpha});"
            f"border-radius: 6px; }}"
        )

    def _stop_glow(self):
        self._glow_timer.stop()
        self._glow_frame.setStyleSheet(
            "QFrame#glowFrame { border: 3px solid transparent; border-radius: 6px; }"
        )

    # ------------------------------------------------------------------
    # Parameter Manager
    # ------------------------------------------------------------------

    def _open_parameter_manager(self):
        from gui.parameter_manager_window import ParameterManagerWindow
        if self._param_manager_window is None or not self._param_manager_window.isVisible():
            self._param_manager_window = ParameterManagerWindow(
                self._visa_lib_registry, self._param_manager_reg, self
            )
            self._param_manager_window.selection_applied.connect(self._on_selection_applied)
        self._param_manager_window.show()
        self._param_manager_window.raise_()

    def _open_graph_window(self):
        from gui.graph_window import GraphWindow
        if self._graph_window is None:
            self._graph_window = GraphWindow()
            # If a sweep is already running, initialise with current columns
            if self._running:
                self._graph_window.begin_session(self._build_graph_columns())
        self._graph_window.show()
        self._graph_window.raise_()

    def _build_graph_columns(self) -> list:
        """Build [(key, label, unit)] for the current sweep + active measurements + derivative."""
        profile = self._active_profile
        cols = []
        sv_id = self._sweep_radio_group.checkedId()
        if sv_id == self._TIME_ID:
            cols.append(("__sweep__", "time", ""))
        elif 0 <= sv_id < len(profile.sweep_values):
            sv = profile.sweep_values[sv_id]
            cols.append(("__sweep__", sv.figure_axis or sv.description, sv.unit))
        else:
            cols.append(("__sweep__", "target", ""))
        for idx in self._active_meas_indices:
            m = profile.measurements[idx]
            cols.append((m.description, m.figure_axis or m.description, m.unit))
        if self._deriv_channel._cfg.enabled:
            cols.append(self._deriv_channel.col_info())
        return cols

    def _open_double_sweep(self):
        from gui.double_sweep_window import DoubleSweepWindow
        if self._double_sweep_window is None:
            self._double_sweep_window = DoubleSweepWindow(self, self._param_manager_reg, self)
            self._double_sweep_window.sweep_started.connect(self._on_double_sweep_started)
            self._double_sweep_window.sweep_finished.connect(self._on_double_sweep_finished)
        self._double_sweep_window.show()
        self._double_sweep_window.raise_()

    def _on_double_sweep_started(self):
        """Double sweep 시작 → main UI Start 비활성화."""
        self._btn_start.setEnabled(False)

    def _on_double_sweep_finished(self):
        """Double sweep 종료 → main UI Start 복원 (single sweep 실행 중이 아닐 때만)."""
        if not self._running:
            self._btn_start.setEnabled(True)

    # ------------------------------------------------------------------
    # Derivative Channel UI
    # ------------------------------------------------------------------

    def _build_deriv_panel(self) -> QWidget:
        """dA1/dA2 실시간 파생 채널 설정 패널 (측정 중 비활성화)."""
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        vbox = QVBoxLayout(box)
        vbox.setContentsMargins(8, 6, 8, 6)
        vbox.setSpacing(4)

        # Title + enable toggle
        title_row = QHBoxLayout()
        lbl = QLabel("Derivative  dA\u2081/dA\u2082")
        lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
        title_row.addWidget(lbl)
        title_row.addStretch()
        self._cb_deriv_enable = QCheckBox("Enable")
        self._cb_deriv_enable.toggled.connect(self._on_deriv_enable_toggled)
        title_row.addWidget(self._cb_deriv_enable)
        vbox.addLayout(title_row)

        # A1 / A2 selectors
        self._deriv_controls = QWidget()
        form = QFormLayout(self._deriv_controls)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._cmb_deriv_a1 = QComboBox()
        self._cmb_deriv_a1.setFont(_MONO)
        self._cmb_deriv_a1.setMinimumWidth(160)
        form.addRow("A\u2081 (numerator):", self._cmb_deriv_a1)

        self._cmb_deriv_a2 = QComboBox()
        self._cmb_deriv_a2.setFont(_MONO)
        self._cmb_deriv_a2.setMinimumWidth(160)
        form.addRow("A\u2082 (denominator):", self._cmb_deriv_a2)

        # Label / Unit
        label_row = QHBoxLayout()
        self._le_deriv_label = QLineEdit()
        self._le_deriv_label.setPlaceholderText("e.g. dV/dI")
        self._le_deriv_label.setFixedWidth(100)
        self._le_deriv_unit = QLineEdit()
        self._le_deriv_unit.setPlaceholderText("unit e.g. \u03a9")
        self._le_deriv_unit.setFixedWidth(70)
        label_row.addWidget(QLabel("Label:"))
        label_row.addWidget(self._le_deriv_label)
        label_row.addSpacing(8)
        label_row.addWidget(QLabel("Unit:"))
        label_row.addWidget(self._le_deriv_unit)
        label_row.addStretch()

        # Window / Method
        wm_row = QHBoxLayout()
        from PySide6.QtWidgets import QSpinBox
        self._sb_deriv_window = QSpinBox()
        self._sb_deriv_window.setRange(3, 50)
        self._sb_deriv_window.setValue(10)
        self._sb_deriv_window.setFixedWidth(60)
        self._cmb_deriv_method = QComboBox()
        self._cmb_deriv_method.addItems(["Linear Regression", "Savitzky-Golay"])
        self._le_deriv_min_delta = QLineEdit("1e-10")
        self._le_deriv_min_delta.setFixedWidth(80)
        self._le_deriv_min_delta.setFont(_MONO)
        wm_row.addWidget(QLabel("Window:"))
        wm_row.addWidget(self._sb_deriv_window)
        wm_row.addSpacing(8)
        wm_row.addWidget(QLabel("Method:"))
        wm_row.addWidget(self._cmb_deriv_method)
        wm_row.addSpacing(8)
        wm_row.addWidget(QLabel("Min |ΔA\u2082|:"))
        wm_row.addWidget(self._le_deriv_min_delta)
        wm_row.addStretch()

        ctrl_vbox = QVBoxLayout()
        ctrl_vbox.setSpacing(3)
        ctrl_vbox.addWidget(self._deriv_controls)
        ctrl_vbox.addLayout(label_row)
        ctrl_vbox.addLayout(wm_row)
        vbox.addLayout(ctrl_vbox)

        self._deriv_setting_widgets = [
            self._cmb_deriv_a1, self._cmb_deriv_a2,
            self._le_deriv_label, self._le_deriv_unit,
            self._sb_deriv_window, self._cmb_deriv_method,
            self._le_deriv_min_delta,
        ]
        return box

    def _rebuild_deriv_combos(self):
        """파라미터가 변경될 때 A1/A2 콤보박스 재구성."""
        if not hasattr(self, "_cmb_deriv_a1"):
            return
        profile = self._active_profile
        items = [("__sweep__", "— sweep channel —")]
        for m in profile.measurements:
            items.append((m.description, f"{m.figure_axis or m.description} [{m.unit}]"))

        prev_a1 = self._cmb_deriv_a1.currentData()
        prev_a2 = self._cmb_deriv_a2.currentData()

        for cmb, prev in [(self._cmb_deriv_a1, prev_a1), (self._cmb_deriv_a2, prev_a2)]:
            cmb.blockSignals(True)
            cmb.clear()
            for key, display in items:
                cmb.addItem(display, userData=key)
            idx = cmb.findData(prev)
            cmb.setCurrentIndex(idx if idx >= 0 else 0)
            cmb.blockSignals(False)

    def _on_deriv_enable_toggled(self, checked: bool):
        for w in self._deriv_setting_widgets:
            w.setEnabled(checked and not self._running)

    def _build_deriv_config(self) -> DerivativeConfig:
        """현재 UI 상태에서 DerivativeConfig 생성."""
        method_map = {0: "linear", 1: "savgol"}
        try:
            min_delta = float(self._le_deriv_min_delta.text())
        except ValueError:
            min_delta = 1e-10
        return DerivativeConfig(
            enabled=self._cb_deriv_enable.isChecked(),
            numerator_key=self._cmb_deriv_a1.currentData() or "",
            denominator_key=self._cmb_deriv_a2.currentData() or "",
            output_label=self._le_deriv_label.text().strip(),
            output_unit=self._le_deriv_unit.text().strip(),
            window_size=self._sb_deriv_window.value(),
            method=method_map.get(self._cmb_deriv_method.currentIndex(), "linear"),
            min_delta=min_delta,
        )

    def _deriv_val_for_step(self, result, meas_map: dict) -> "float | None":
        """현재 스텝에서 A1/A2 값을 추출하고 파생값 계산. None이면 비활성."""
        cfg = self._deriv_channel._cfg
        if not cfg.enabled:
            return None

        def _get(key):
            if key == "__sweep__":
                return result.next_v
            for idx in self._active_meas_indices:
                if self._active_profile.measurements[idx].description == key:
                    v = meas_map.get(idx)
                    return v
            return None

        a1 = _get(cfg.numerator_key)
        a2 = _get(cfg.denominator_key)
        if a1 is None or a2 is None:
            return None
        return self._deriv_channel.push(a1, a2)

    def _on_selection_applied(self, profile: MainUIProfile):
        self._active_profile = profile
        self._rebuild_sweep_channel_panel(profile.sweep_values)
        self._rebuild_meas_panel(profile.measurements)
        self._rebuild_write_panel(profile.write_cmds)
        self._rebuild_deriv_combos()

    _TIME_ID = -2   # QButtonGroup ID for the fixed Time channel

    def _get_alias_color(self, alias: str) -> str:
        """Return a consistent color for the given alias (instrument)."""
        if alias not in self._alias_color_map:
            n = len(self._alias_color_map)
            self._alias_color_map[alias] = _ALIAS_PALETTE[n % len(_ALIAS_PALETTE)]
        return self._alias_color_map[alias]

    def _meas_label_for(self, idx: int, m) -> str:
        """Return figure_axis + user suffix for data-file column header."""
        base = m.figure_axis or m.description
        if idx < len(self._meas_suffix_edits):
            suffix = self._meas_suffix_edits[idx].text().strip()
            if suffix:
                return f"{base}_{suffix}"
        return base

    def _rebuild_sweep_channel_panel(self, sweep_values: list):
        for btn in self._sweep_radio_group.buttons():
            self._sweep_radio_group.removeButton(btn)

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(2)

        # Time channel: always first
        rb_time = QRadioButton("Time")
        rb_time.setFont(_MONO)
        rb_time.setStyleSheet("QRadioButton { color: #888888; }")
        self._sweep_radio_group.addButton(rb_time, self._TIME_ID)
        cl.addWidget(rb_time)

        for idx, sv in enumerate(sweep_values):
            color = self._get_alias_color(sv.alias)
            rb = QRadioButton(f"[{sv.alias}]  {sv.description}  ({sv.unit})")
            rb.setFont(_MONO)
            rb.setStyleSheet(f"QRadioButton {{ color: {color}; }}")
            self._sweep_radio_group.addButton(rb, idx)
            cl.addWidget(rb)

        cl.addStretch()
        self._sweep_ch_scroll.setWidget(content)

        if sweep_values:
            self._sweep_radio_group.button(0).setChecked(True)
        else:
            rb_time.setChecked(True)

        self._sweep_channel_panel.setVisible(True)

    def _on_sweep_radio_toggled(self, btn_id: int, checked: bool):
        if not checked:
            return
        if btn_id == self._TIME_ID:
            self._sweep_channel = TIME_CHANNEL
            self._sync_data_window_columns()
            return
        svs = self._active_profile.sweep_values
        if 0 <= btn_id < len(svs):
            self._sweep_channel = sweep_channel_from_instantiated(svs[btn_id])
            self._sync_data_window_columns()

    def _rebuild_meas_panel(self, measurements: list):
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(2)

        self._meas_checkboxes = []
        self._meas_suffix_edits = []

        for m in measurements:
            color = self._get_alias_color(m.alias)
            row_w = QWidget()
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.setSpacing(6)

            cb = QCheckBox(f"[{m.alias}]  {m.description}  ({m.unit})")
            cb.setFont(_MONO)
            cb.setStyleSheet(f"QCheckBox {{ color: {color}; }}")
            cb.setChecked(True)
            self._meas_checkboxes.append(cb)
            row_h.addWidget(cb)

            le_suffix = QLineEdit()
            le_suffix.setFont(_MONO)
            le_suffix.setFixedWidth(110)
            le_suffix.setPlaceholderText("suffix")
            le_suffix.setText(m.axis_suffix)
            le_suffix.setToolTip("Data column suffix: e.g. 'port10' → smua_current_port10")
            le_suffix.textChanged.connect(self._sync_data_window_columns)
            self._meas_suffix_edits.append(le_suffix)
            row_h.addWidget(le_suffix)
            row_h.addStretch()

            cl.addWidget(row_w)

        cl.addStretch()
        self._meas_scroll.setWidget(content)
        self._meas_panel.setVisible(bool(measurements))
        self._sync_data_window_columns()

    def _rebuild_write_panel(self, write_cmds: list):
        while self._write_layout.count() > 1:
            item = self._write_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()

        for wc in write_cmds:
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)

            lbl = QLabel(f"[{wc.alias}]  {wc.description}  ({wc.unit})")
            lbl.setFont(_MONO)
            lbl.setStyleSheet("color: #e3b341;")
            row.addWidget(lbl)
            row.addStretch()

            has_v = "{v}" in wc.cmd_set
            if has_v:
                le_val = QLineEdit()
                le_val.setFont(_MONO)
                le_val.setFixedWidth(90)
                le_val.setPlaceholderText("값")
                row.addWidget(le_val)
                btn = QPushButton("Send")
                btn.setFixedWidth(60)
                btn.clicked.connect(
                    lambda *_, a=wc.alias, cmd=wc.cmd_set, le=le_val: self._send_write_cmd(a, cmd, le)
                )
            else:
                btn = QPushButton("Send")
                btn.setFixedWidth(60)
                btn.clicked.connect(
                    lambda *_, a=wc.alias, cmd=wc.cmd_set: self._send_write_cmd(a, cmd, None)
                )
            row.addWidget(btn)
            self._write_layout.addWidget(row_widget)

        self._write_panel.setVisible(bool(write_cmds))

    def _send_write_cmd(self, alias: str, cmd: str, value_edit):
        if value_edit is not None:
            val_text = value_edit.text().strip()
            if not val_text:
                self._log(f"  [{alias}] Send 실패: 값을 입력하세요.", color="#f44747")
                return
            try:
                cmd = cmd.format(v=float(val_text))
            except ValueError:
                cmd = cmd.format(v=val_text)
        try:
            self._session.write(alias, cmd)
            self._log(f"  [{alias}] write: {cmd}", color="#e3b341")
        except Exception as e:
            self._log(f"  ERROR: {e}", color="#f44747")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _open_instrument_settings(self):
        from gui.instrument_settings_ui import InstrumentSettingsUI
        if self._settings_window is None or not self._settings_window.isVisible():
            self._settings_window = InstrumentSettingsUI()
        self._settings_window.show()
        self._settings_window.raise_()

    def _open_visa_library(self):
        from gui.visa_library_window import VisaLibraryWindow
        if self._visa_lib_window is None or not self._visa_lib_window.isVisible():
            self._visa_lib_window = VisaLibraryWindow(
                self._visa_lib_registry, self._registry, self
            )
        self._visa_lib_window.show()
        self._visa_lib_window.raise_()

    def _on_start(self):
        if self._running:
            return
        if self._sweep_channel is None:
            QMessageBox.warning(self, "No Sweep Channel",
                "Parameter Manager에서 Sweep Value를 선택하세요.")
            return
        self._running = True
        self._sweep_step_count = 0
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._glow_phase = 0.0
        self._glow_timer.start()
        self._sweep_status_window.reset()
        self._last_write_value = None
        # 체크된 measurement 인덱스 스냅샷 (sweep 중 고정)
        self._active_meas_indices = [
            i for i, (_, cb) in enumerate(
                zip(self._active_profile.measurements, self._meas_checkboxes)
            )
            if cb.isChecked()
        ]
        self._sync_data_window_columns()
        # sweep channel / measurement 컨트롤 비활성화
        self._sweep_channel_panel.setEnabled(False)
        self._meas_panel.setEnabled(False)
        self._update_save_preview()
        filepath = self._data_saver.start_session()
        self._data_window.clear_values()
        self._lbl_idle.setText("—")
        self._lbl_idle.setStyleSheet("color: #555555;")
        self._lbl_remaining.setText("—")
        if filepath:
            self._log(f"  Data → {filepath}", color="#888888")
        # disable Double Sweep while single sweep is running
        if self._double_sweep_window is not None:
            self._double_sweep_window._btn_start.setEnabled(False)
        # Derivative channel — reconfigure and reset buffer
        deriv_cfg = self._build_deriv_config()
        self._deriv_channel.reconfigure(deriv_cfg)
        self._deriv_channel.reset()
        # Disable derivative settings while running
        for w in self._deriv_setting_widgets:
            w.setEnabled(False)
        self._cb_deriv_enable.setEnabled(False)

        if self._graph_window is not None:
            self._graph_window.begin_session(self._build_graph_columns())
        self._log("Sweep started.", color="#4ec9b0")
        self._sweep_step_timer.start(0)

    def _on_stop(self):
        if not self._running:
            return
        self._running = False
        self._last_write_value = None
        self._worker.request_stop()
        self._sweep_step_timer.stop()
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._sweep_channel_panel.setEnabled(True)
        self._meas_panel.setEnabled(True)
        # re-enable Double Sweep if it's not actively running
        if self._double_sweep_window is not None:
            from gui.double_sweep_window import DoubleSweepPhase
            if self._double_sweep_window._phase == DoubleSweepPhase.IDLE:
                self._double_sweep_window._btn_start.setEnabled(True)
        # Re-enable derivative settings
        self._cb_deriv_enable.setEnabled(True)
        enabled = self._cb_deriv_enable.isChecked()
        for w in self._deriv_setting_widgets:
            w.setEnabled(enabled)
        self._stop_glow()
        self._log("Sweep stopped.", color="#ce9178")

    def _sweep_tick(self):
        """메인 스레드: VISA 작업을 worker에 위임하고 즉시 리턴합니다."""
        if not self._running:
            return

        self._tick_start = time.perf_counter()

        # 체크박스 상태는 메인 스레드에서만 읽어야 하므로 여기서 스냅샷
        active = [
            (row, m.alias, m.description, m.resolved_cmd)
            for row, (m, cb) in enumerate(
                zip(self._active_profile.measurements, self._meas_checkboxes)
            )
            if cb.isChecked()
        ]


        sv = self._active_profile.sweep_values[
            self._sweep_radio_group.checkedId()
        ] if self._sweep_radio_group.checkedId() >= 0 else None

        t_emit = time.perf_counter()
        self.request_step.emit(StepRequest(
            sweep_channel=self._sweep_channel,
            sweep_to=self._sweep_config.sweep_to,
            sweep_rate=self._sweep_config.sweep_rate,
            time_per_point=self._sweep_config.time_per_point,
            t_emit=t_emit,
            last_write_value=self._last_write_value,
            safety_steps=sv.safety_steps if sv else 0,
            safety_interval_ms=sv.safety_interval_ms if sv else 0.0,
            active_measurements=active,
        ))
        # 여기서 즉시 리턴 → Qt 이벤트 루프 반환 → UI 반응 가능

    def _on_step_done(self, result: StepResult):
        """Worker 스레드 완료 후 메인 스레드에서 UI 갱신."""
        t_recv = time.perf_counter()

        if not self._running:
            return

        self.set_source_value(result.current)
        self._sweep_step_count += 1

        # DataWindow 갱신: next_v + 체크된 measurement 값만
        meas_map = {row: val for row, val in result.meas_results}
        row_vals = [f"{result.next_v:.6g}"]
        for idx in self._active_meas_indices:
            val = meas_map.get(idx)
            row_vals.append(f"{val:.6g}" if val is not None else "ERR")

        # Derivative channel computation (numpy on ≤50 points — GUI thread safe)
        deriv_val = self._deriv_val_for_step(result, meas_map)
        if self._deriv_channel._cfg.enabled:
            row_vals.append(f"{deriv_val:.6g}" if deriv_val is not None else "—")

        self._data_window.update_values(row_vals)
        self._data_saver.append_row(row_vals)

        # Graph update
        if self._graph_window is not None:
            from gui.graph_window import GraphDataPoint
            gvals = {"__sweep__": result.next_v}
            for idx in self._active_meas_indices:
                val = meas_map.get(idx)
                if val is not None:
                    gvals[self._active_profile.measurements[idx].description] = val
            if deriv_val is not None:
                gvals[_DERIV_KEY] = deriv_val
            self._graph_window.append_point(GraphDataPoint(values=gvals, phase=""))

        next_display = None if result.is_done else calculate_next_step(
            result.next_v,
            self._sweep_config.sweep_to,
            self._sweep_config.sweep_rate,
            self._sweep_config.time_per_point,
        )[0]
        self._sweep_status_window.update_step(
            self._sweep_step_count, result.current, next_display
        )

        self._last_write_value = result.next_v

        # Remaining time 예측
        tpp = self._sweep_config.time_per_point
        step_size = self._sweep_config.sweep_rate * tpp / 60.0
        if step_size > 0 and not result.is_done:
            distance = abs(self._sweep_config.sweep_to - result.next_v)
            remaining_steps = math.ceil(distance / step_size) if distance > 1e-12 else 0
            remaining_s = remaining_steps * tpp
            if remaining_s < 60:
                self._lbl_remaining.setText(f"{remaining_s:.1f} s")
            else:
                m, s = divmod(int(remaining_s), 60)
                self._lbl_remaining.setText(f"{m} min {s} s")
        else:
            self._lbl_remaining.setText("—")

        t_ui_done = time.perf_counter()
        self._timing_window.update_timing(result.timing, t_recv, t_ui_done)

        if result.is_done:
            self._lbl_idle.setText("—")
            self._lbl_remaining.setText("—")
            self._log(f"Sweep complete. ({self._sweep_step_count} steps)", color="#4ec9b0")
            self._on_stop()
        else:
            # t_ui_done을 타이머 직전에 다시 찍어 모든 처리 시간 반영
            t_before_timer = time.perf_counter()
            elapsed_ms = int((t_before_timer - result.timing.t_emit) * 1000)
            interval_ms = max(0, int(tpp * 1000) - elapsed_ms)
            self._lbl_idle.setText(
                f"{interval_ms} ms" if interval_ms >= 0
                else f"overrun {-interval_ms} ms"
            )
            self._lbl_idle.setStyleSheet(
                "color: #f44747;" if interval_ms <= 0 else "color: #555555;"
            )
            self._sweep_step_timer.start(interval_ms)

    def _on_step_error(self, msg: str):
        """Worker에서 예외 발생 시 메인 스레드에서 처리."""
        self._log(f"  ERROR (sweep tick): {msg}", color="#f44747")
        self._on_stop()

    def _browse_save_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Main Folder 선택", self._le_main_folder.text() or ""
        )
        if folder:
            self._le_main_folder.setText(folder)

    def _copy_save_path(self):
        path = self._lbl_save_preview.text()
        if path and path != "—" and not path.startswith("("):
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(path)

    def _update_save_preview(self):
        self._data_saver.set_main_folder(self._le_main_folder.text())
        self._data_saver.set_custom_folder(self._le_custom_folder.text())
        self._data_saver.set_custom_word(self._le_custom_word.text())
        self._data_saver.set_include_date(self._cb_save_date.isChecked())
        self._data_saver.set_enabled(self._cb_save_enable.isChecked())
        self._lbl_save_preview.setText(self._data_saver.preview_path())

    def _sync_data_window_columns(self):
        """선택된 sweep channel + 체크된 measurements로 DataWindow 컬럼 설정."""
        profile = self._active_profile
        columns = []
        # 첫 번째 컬럼: 현재 선택된 sweep channel의 next target value
        sv_id = self._sweep_radio_group.checkedId()
        if sv_id == self._TIME_ID:
            columns.append(("time", ""))
        elif 0 <= sv_id < len(profile.sweep_values):
            sv = profile.sweep_values[sv_id]
            columns.append((sv.figure_axis or "target", sv.unit))
        else:
            columns.append(("target", ""))
        # 이후 컬럼: sweep 중이면 _active_meas_indices 기준, 아니면 현재 체크 상태
        if self._running:
            for idx in self._active_meas_indices:
                m = profile.measurements[idx]
                columns.append((m.figure_axis or m.description, m.unit))
        else:
            for i, m in enumerate(profile.measurements):
                cb = self._meas_checkboxes[i] if i < len(self._meas_checkboxes) else None
                if cb is None or cb.isChecked():
                    columns.append((m.figure_axis or m.description, m.unit))
        if self._deriv_channel._cfg.enabled:
            _, lbl, unit = self._deriv_channel.col_info()
            columns.append((lbl, unit))
        self._data_window.configure_columns(columns)
        self._data_saver.set_columns(columns)

    def _update_step_size_label(self):
        tmp = SweepConfig(
            sweep_rate=self._sb_sweep_rate.value(),
            time_per_point=self._sb_time_per_point.value(),
        )
        self._lbl_step_size.setText(f"{tmp.step_size():.6g}  units/step")

    def _on_sweep_params_confirmed(self):
        self._sweep_config.sweep_to = self._sb_sweep_to.value()
        self._sweep_config.sweep_rate = self._sb_sweep_rate.value()
        self._sweep_config.time_per_point = self._sb_time_per_point.value()
        self._timing_window.set_time_per_point(self._sweep_config.time_per_point)
        self._last_write_value = None  # 다음 스텝에서 VISA readback으로 재초기화
        self._log("  Sweep params updated.", color="#888888")

    def set_source_value(self, value: float):
        self._sweep_config.source_value = value
        self._le_source_value.setText(f"{value:.6g}")
        self._le_source_value.setStyleSheet("color: #222222;")

    # ------------------------------------------------------------------
    # VISA log callback
    # ------------------------------------------------------------------

    def _on_visa_log(self, alias: str, cmd_type: str, cmd: str, result=None):
        """VISA 콜백 — worker 스레드에서도 호출될 수 있으므로 Signal로 릴레이."""
        try:
            addr = self._registry.get_visa_address(alias) or "?"
        except Exception:
            addr = "?"
        self._visa_log_relay.emit(alias, addr, cmd_type, cmd, result)

    def _apply_visa_log(self, alias: str, addr: str, cmd_type: str, cmd: str, result):
        """메인 스레드에서만 실행: 디버그 창에 VISA 로그 출력."""
        self._debug_window.log_visa(alias, addr, cmd_type, cmd, result)

    # ------------------------------------------------------------------
    # Console command handling
    # ------------------------------------------------------------------

    def _handle_command(self, text: str):
        self._registry.reload()
        aliases = self._registry.list_aliases()

        cmd = ConsoleCommand.parse(text)
        if cmd:
            try:
                result = self._cmd_handler.execute(cmd)
                if cmd.cmd_type == "write":
                    self._log(f"  [{cmd.alias}] write OK: {cmd.visa_cmd}", color="#4ec9b0")
                elif cmd.cmd_type == "read":
                    self._log(f"  [{cmd.alias}] read  → {result}", color="#4ec9b0")
                elif cmd.cmd_type == "query":
                    self._log(f"  [{cmd.alias}] query → {result}", color="#4ec9b0")
            except Exception as e:
                self._log(f"  ERROR: {e}", color="#f44747")
            return

        if text.lower() == "help":
            self._log("명령어 목록:")
            self._log("  alias:<alias> VISA:<cmd> type:write|query|read")
            self._log("  close:<alias>  /  list  /  clear")
            return

        if text.lower() == "list":
            self._log("등록된 장비: " + (", ".join(aliases) if aliases else "(없음)"))
            return

        if text.lower() == "clear":
            self._debug_window._console_output.clear()
            return

        if text.lower().startswith("close:"):
            alias = text[6:].strip()
            if self._session.is_open(alias):
                self._session.close(alias)
                self._log(f"  [{alias}] 연결 해제됨.")
            else:
                self._log(f"  [{alias}] 연결된 세션이 없습니다.", color="#f44747")
            return

        config = self._registry.get_config(text)
        if config:
            visa = self._registry.get_visa_address(text)
            connected = "연결됨" if self._session.is_open(text) else "미연결"
            self._log(f"[{text}]  ({connected})")
            self._log(f"  VISA: {visa}", color="#4ec9b0")
            self._log(f"  Driver: {config.class_name}")
        else:
            self._log(f"'{text}' 에 해당하는 장비가 없습니다.", color="#f44747")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(self, text: str, color: str = "#d4d4d4"):
        self._debug_window.log_console(text, color)

    def closeEvent(self, event):
        if self._running:
            reply = QMessageBox.question(
                self, "종료 확인",
                "Sweep이 실행 중입니다. 종료하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            self._on_stop()

        self._save_current_to_active_profile()
        self._glow_timer.stop()
        self._worker_thread.quit()
        self._worker_thread.wait()
        if self._double_sweep_window is not None:
            self._double_sweep_window._worker_thread.quit()
            self._double_sweep_window._worker_thread.wait()
            self._double_sweep_window._second_thread.quit()
            self._double_sweep_window._second_thread.wait()
        self._session.shutdown()
        self._debug_window.deleteLater()
        self._sweep_status_window.deleteLater()
        self._timing_window.deleteLater()
        self._data_window.deleteLater()
        super().closeEvent(event)
