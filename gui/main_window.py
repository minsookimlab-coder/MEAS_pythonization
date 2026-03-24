import math
import time

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton,
    QFormLayout, QFrame, QMessageBox,
    QButtonGroup, QRadioButton, QCheckBox, QFileDialog,
    QComboBox, QInputDialog, QScrollArea,
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QAction, QFont

from core.data_saver import DataSaver
from core.derivative_channel import (
    DerivativeChannel, DerivativeConfig,
    OUTPUT_KEY as _DERIV_KEY, OUTPUT_KEY_2 as _DERIV2_KEY, OUTPUT_KEY_3 as _DERIV3_KEY,
)
from core.meta_data_manager import MetaDataManager
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


class _WhaleBgFrame(QFrame):
    """배경에 이미지를 반투명하게 채워 그리는 QFrame."""
    def __init__(self, image_path: str, opacity: float = 0.3, parent=None):
        super().__init__(parent)
        from PySide6.QtGui import QPixmap
        self._pixmap = QPixmap(image_path)
        self._opacity = opacity  # 0.0 ~ 1.0

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._pixmap.isNull():
            return
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        scaled = self._pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (self.width()  - scaled.width())  // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
        painter.end()


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
        self.resize(1120, 660)

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
        self._meas_type_combos: list = []
        self._deriv_channel:  DerivativeChannel = DerivativeChannel(DerivativeConfig(order=1))
        self._deriv_channel2: DerivativeChannel = DerivativeChannel(DerivativeConfig(order=2))
        self._deriv_channel3: DerivativeChannel = DerivativeChannel(DerivativeConfig(order=3))
        self._sweep_step_count = 0
        self._tick_start: float = 0.0
        self._last_write_value: "float | None" = None
        self._step_context: str = ""   # 마지막 sweep tick 컨텍스트 (오류 시 참조)
        self._data_saver = DataSaver()
        self._data_saver.set_error_callback(
            lambda msg: self._log(f"  [DataSaver] {msg}", color="#f44747")
        )
        self._meta_manager = MetaDataManager(self._session)
        self._meta_data_window = None
        self._command_window = None

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
        self._debug_window.set_visa_log_callback(self._session.set_log_enabled) # Debug 창의 토글과 세션의 로그 활성화 상태 연결

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
        act_meta = QAction("Meta Data Config...", self)
        act_meta.triggered.connect(self._open_meta_data_config)
        view_menu.addAction(act_meta)
        act_cmd = QAction("Command Window...", self)
        act_cmd.triggered.connect(self._open_command_window)
        view_menu.addAction(act_cmd)

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
        btn_ren = QPushButton("Rename")
        btn_ren.setFixedWidth(60)
        btn_ren.clicked.connect(self._profile_rename)
        btn_del = QPushButton("Delete")
        btn_del.setFixedWidth(52)
        btn_del.clicked.connect(self._profile_delete)
        row.addWidget(btn_add)
        row.addWidget(btn_dup)
        row.addWidget(btn_ren)
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

    def _profile_rename(self):
        old = self._param_manager_reg.active_name
        new_name, ok = QInputDialog.getText(
            self, "Rename Profile", "새 이름:", text=old
        )
        if not ok or not new_name.strip() or new_name.strip() == old:
            return
        actual = self._param_manager_reg.rename_profile(old, new_name.strip())
        self._refresh_profile_combo()
        self._combo_profile.blockSignals(True)
        self._combo_profile.setCurrentText(actual)
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
            mui = fp.main_ui
            if mui.sweep_values or mui.measurements or mui.write_cmds:
                self._on_selection_applied(mui)
            else:
                self._rebuild_sweep_channel_panel([])
            # Apply sweep params (block signals to avoid recursive saves)
            for le, val in [
                (self._le_sweep_to,       fp.sweep_to),
                (self._le_sweep_rate,     fp.sweep_rate),
                (self._le_time_per_point, fp.time_per_point),
            ]:
                le.blockSignals(True)
                le.setText(f"{val:g}")
                le.blockSignals(False)
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
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

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
        outer.addLayout(title_row)

        # ── 2-column content layout ────────────────────────────────────────
        content_row = QHBoxLayout()
        content_row.setSpacing(10)
        outer.addLayout(content_row, stretch=1)

        # ── LEFT COLUMN ──────────────────────────────────────────────────
        left_widget = QWidget()
        left_widget.setMinimumWidth(320)
        left_widget.setMaximumWidth(420)
        layout = QVBoxLayout(left_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        content_row.addWidget(left_widget)

        # --- Sweep Parameters ---
        import os as _os
        sweep_box = _WhaleBgFrame(
            _os.path.join(_os.path.dirname(__file__), "whale.png"),
            opacity=0.3,
        )
        sweep_box.setFrameShape(QFrame.Shape.StyledPanel)
        sweep_layout = QVBoxLayout(sweep_box)

        sweep_title = QLabel("Sweep Parameters")
        sweep_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        sweep_layout.addWidget(sweep_title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        sweep_layout.addLayout(form)

        _SWEEP_LE_STYLE = (
            "QLineEdit { background-color: rgba(255, 255, 255, 179);"
            " color: #000000; border: 1px solid #aaa; border-radius: 3px; }"
            "QLineEdit:focus { border: 1px solid #1a73e8; }"
        )

        self._le_source_value = QLineEdit("—")
        self._le_source_value.setReadOnly(True)
        self._le_source_value.setStyleSheet(
            "QLineEdit { background-color: transparent;"
            " color: #444444; border: none; }"
        )
        self._le_source_value.setFixedWidth(120)
        form.addRow("Source Value:", self._le_source_value)

        def _sweep_le(placeholder: str) -> QLineEdit:
            le = QLineEdit(placeholder)
            le.setFont(_MONO)
            le.setMinimumWidth(280)
            le.setStyleSheet(_SWEEP_LE_STYLE)
            from PySide6.QtGui import QDoubleValidator
            le.setValidator(QDoubleValidator(-1e18, 1e18, 10, le))
            return le

        self._le_sweep_to = _sweep_le("0")
        self._le_sweep_to.textChanged.connect(self._update_step_size_label)
        self._le_sweep_to.editingFinished.connect(self._on_sweep_params_confirmed)
        self._lbl_sweep_to_unit = QLabel("")
        _st_row = QWidget(); _st_h = QHBoxLayout(_st_row)
        _st_h.setContentsMargins(0, 0, 0, 0); _st_h.setSpacing(4)
        _st_h.addWidget(self._le_sweep_to); _st_h.addWidget(self._lbl_sweep_to_unit)
        _st_h.addStretch()
        form.addRow("Sweep To:", _st_row)

        self._le_sweep_rate = _sweep_le("1")
        self._le_sweep_rate.textChanged.connect(self._update_step_size_label)
        self._le_sweep_rate.editingFinished.connect(self._on_sweep_params_confirmed)
        self._lbl_sweep_rate_unit = QLabel("units/min")
        _sr_row = QWidget(); _sr_h = QHBoxLayout(_sr_row)
        _sr_h.setContentsMargins(0, 0, 0, 0); _sr_h.setSpacing(4)
        _sr_h.addWidget(self._le_sweep_rate); _sr_h.addWidget(self._lbl_sweep_rate_unit)
        _sr_h.addStretch()
        form.addRow("Sweep Rate:", _sr_row)

        self._le_time_per_point = _sweep_le("1")
        self._le_time_per_point.textChanged.connect(self._update_step_size_label)
        self._le_time_per_point.editingFinished.connect(self._on_sweep_params_confirmed)
        _tp_row = QWidget(); _tp_h = QHBoxLayout(_tp_row)
        _tp_h.setContentsMargins(0, 0, 0, 0); _tp_h.setSpacing(4)
        _tp_h.addWidget(self._le_time_per_point); _tp_h.addWidget(QLabel("sec"))
        _tp_h.addStretch()
        form.addRow("Time / Point:", _tp_row)

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

        # --- Quick-access buttons: Graph / Double Sweep / Connection Test ---
        quick_row = QHBoxLayout()
        quick_row.setSpacing(4)
        btn_quick_graph = QPushButton("Graph")
        btn_quick_graph.setToolTip("Open Graph window")
        btn_quick_graph.clicked.connect(self._open_graph_window)
        btn_quick_ds = QPushButton("Double Sweep")
        btn_quick_ds.setToolTip("Open Double Sweep window")
        btn_quick_ds.clicked.connect(self._open_double_sweep)
        btn_quick_conn = QPushButton("Connection Test")
        btn_quick_conn.setToolTip(
            "Test *IDN? on all active instruments\n"
            "(sweep channel + active measurements + second channel if DS open)"
        )
        btn_quick_conn.clicked.connect(self._on_connection_test)
        quick_row.addWidget(btn_quick_graph)
        quick_row.addWidget(btn_quick_ds)
        quick_row.addWidget(btn_quick_conn)
        layout.addLayout(quick_row)

        # --- Save Settings panel ---
        save_box = QFrame()
        self._save_settings_frame = save_box
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
        self._btn_copy_path = QPushButton("Copy")
        self._btn_copy_path.setFixedWidth(46)
        self._btn_copy_path.setFixedHeight(20)
        self._btn_copy_path.setFont(QFont("Consolas", 8))
        self._btn_copy_path.clicked.connect(self._copy_save_path)
        btn_copy_path = self._btn_copy_path
        self._btn_open_folder = QPushButton("Open Folder")
        self._btn_open_folder.setFixedHeight(20)
        self._btn_open_folder.setFont(QFont("Consolas", 8))
        self._btn_open_folder.clicked.connect(self._open_save_folder)
        btn_open_folder = self._btn_open_folder
        preview_row.addWidget(QLabel("→"))
        preview_row.addWidget(self._lbl_save_preview, stretch=1)
        preview_row.addWidget(btn_copy_path)
        preview_row.addWidget(btn_open_folder)
        save_layout.addLayout(preview_row)

        self._cb_save_enable = QCheckBox("Auto-save 활성화")
        self._cb_save_enable.setChecked(False)
        self._cb_save_enable.stateChanged.connect(self._update_save_preview)
        save_layout.addWidget(self._cb_save_enable)

        layout.addWidget(save_box)
        layout.addWidget(self._build_deriv_panel(1))
        layout.addWidget(self._build_deriv_panel(2))
        layout.addWidget(self._build_deriv_panel(3))

        layout.addStretch()

        # ── RIGHT COLUMN ─────────────────────────────────────────────────
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        content_row.addWidget(right_widget, stretch=1)

        # --- Sweep Channel panel ---
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
        self._sweep_ch_scroll.setStyleSheet("background: transparent;")
        sc_outer.addWidget(self._sweep_ch_scroll, stretch=1)
        self._sweep_radio_group = QButtonGroup(self)
        self._sweep_radio_group.setExclusive(True)
        self._sweep_radio_group.idToggled.connect(self._on_sweep_radio_toggled)
        self._sweep_channel_panel.setVisible(False)
        right_layout.addWidget(self._sweep_channel_panel, stretch=1)

        # --- Active Measurements panel ---
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
        self._meas_scroll.setStyleSheet("background: transparent;")
        meas_outer.addWidget(self._meas_scroll, stretch=1)
        self._meas_panel.setVisible(False)
        right_layout.addWidget(self._meas_panel, stretch=2)

        # --- Write Commands panel (hidden, kept for compatibility) ---
        self._write_panel = QFrame()
        self._write_panel.setFrameShape(QFrame.Shape.StyledPanel)
        self._write_layout = QVBoxLayout(self._write_panel)
        self._write_layout.setContentsMargins(8, 6, 8, 6)
        w_title = QLabel("Write Commands")
        w_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #e3b341;")
        self._write_layout.addWidget(w_title)
        self._write_panel.setVisible(False)
        right_layout.addWidget(self._write_panel)

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
            cols.append((m.description, self._meas_label_for(idx, m), m.unit))
        for ch in (self._deriv_channel, self._deriv_channel2, self._deriv_channel3):
            if ch._cfg.enabled:
                cols.append(ch.col_info())
        return cols

    def _open_meta_data_config(self):
        from gui.meta_data_window import MetaDataConfigWindow
        if self._meta_data_window is None:
            self._meta_data_window = MetaDataConfigWindow(self)
        self._meta_data_window.show()
        self._meta_data_window.raise_()

    def _open_command_window(self):
        from gui.command_window import CommandWindow
        if self._command_window is None:
            self._command_window = CommandWindow(self._session, self._visa_lib_registry, self)
        self._command_window.show()
        self._command_window.raise_()

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
    # Connection Test
    # ------------------------------------------------------------------

    def collect_active_aliases(self, include_second: bool = False) -> list:
        """활성 기기(alias)를 중복 없이 순서대로 반환.

        include_second=True이면 Double Sweep 창이 열려 있을 때
        second_sweep_channels의 alias도 포함합니다.
        """
        seen: set = set()
        aliases: list = []

        def _add(alias: str):
            if alias and alias not in seen:
                seen.add(alias)
                aliases.append(alias)

        # Sweep channel
        sv_id = self._sweep_radio_group.checkedId()
        if sv_id != self._TIME_ID and 0 <= sv_id < len(self._active_profile.sweep_values):
            _add(self._active_profile.sweep_values[sv_id].alias)

        # Active measurement checkboxes
        for m, cb in zip(self._active_profile.measurements, self._meas_checkboxes):
            if cb.isChecked():
                _add(m.alias)

        # Second sweep channels (only if double sweep window is open)
        if include_second and self._double_sweep_window is not None and \
                self._double_sweep_window.isVisible():
            for ch in self._active_profile.second_sweep_channels:
                _add(ch.alias)

        return aliases

    def _run_connection_test(self, include_second: bool = False,
                             show_success: bool = True) -> bool:
        """각 기기에 *IDN? 쿼리를 보내 연결 상태를 확인합니다.

        show_success=False이면 오류가 있을 때만 다이얼로그를 표시합니다.
        반환값: 모두 성공이면 True, 하나라도 실패하면 False.
        """
        aliases = self.collect_active_aliases(include_second=include_second)
        if not aliases:
            if show_success:
                QMessageBox.information(self, "Connection Test", "활성화된 기기가 없습니다.")
            return True

        results: dict = {}  # alias → (ok: bool, message: str)
        for alias in aliases:
            try:
                idn = self._session.query_once(alias, "*IDN?")
                results[alias] = (True, idn.strip())
            except Exception as exc:
                results[alias] = (False, str(exc))

        all_ok = all(ok for ok, _ in results.values())

        if not all_ok or show_success:
            lines = []
            for alias, (ok, msg) in results.items():
                icon = "✓" if ok else "✗"
                short = msg[:100] + ("…" if len(msg) > 100 else "")
                lines.append(f"{icon}  {alias}\n    {short}")
            body = "\n\n".join(lines)
            if all_ok:
                QMessageBox.information(self, "Connection Test — OK", body)
            else:
                QMessageBox.critical(self, "Connection Test — 실패", body)

        return all_ok

    def _on_connection_test(self):
        """Connection Test 버튼 핸들러 — 항상 요약 창 표시."""
        self._run_connection_test(include_second=True, show_success=True)

    # ------------------------------------------------------------------
    # Derivative Channel UI
    # ------------------------------------------------------------------

    def _build_deriv_panel(self, order: int) -> QWidget:
        """d^n A1/dA2^n 실시간 파생 채널 설정 패널 (order = 1/2/3)."""
        from PySide6.QtWidgets import QSpinBox

        sup = {1: "", 2: "²", 3: "³"}
        pre = {1: "d", 2: "d²", 3: "d³"}
        title_text = f"{pre[order]}A\u2081/{pre[order]}A\u2082{sup[order]}"

        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        vbox = QVBoxLayout(box)
        vbox.setContentsMargins(8, 6, 8, 6)
        vbox.setSpacing(4)

        # Title + enable toggle
        title_row = QHBoxLayout()
        lbl = QLabel(f"Derivative  {title_text}")
        lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
        title_row.addWidget(lbl)
        title_row.addStretch()
        cb_enable = QCheckBox("Enable")
        title_row.addWidget(cb_enable)
        vbox.addLayout(title_row)

        # A1 / A2 selectors
        controls_widget = QWidget()
        form = QFormLayout(controls_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        cmb_a1 = QComboBox()
        cmb_a1.setFont(_MONO)
        cmb_a1.setMinimumWidth(160)
        form.addRow("A\u2081 (numerator):", cmb_a1)

        cmb_a2 = QComboBox()
        cmb_a2.setFont(_MONO)
        cmb_a2.setMinimumWidth(160)
        form.addRow("A\u2082 (denominator):", cmb_a2)

        # Label / Unit
        label_row = QHBoxLayout()
        le_label = QLineEdit()
        le_label.setPlaceholderText(f"e.g. {title_text}")
        le_label.setFixedWidth(100)
        le_unit = QLineEdit()
        le_unit.setPlaceholderText("unit")
        le_unit.setFixedWidth(70)
        label_row.addWidget(QLabel("Label:"))
        label_row.addWidget(le_label)
        label_row.addSpacing(8)
        label_row.addWidget(QLabel("Unit:"))
        label_row.addWidget(le_unit)
        label_row.addStretch()

        # Window / Method / Min delta
        wm_row = QHBoxLayout()
        sb_window = QSpinBox()
        sb_window.setRange(3, 50)
        sb_window.setValue(10)
        sb_window.setFixedWidth(60)
        cmb_method = QComboBox()
        cmb_method.addItems(["Linear Regression", "Savitzky-Golay"])
        if order > 1:
            cmb_method.setEnabled(False)  # savgol only for order==1
            cmb_method.setToolTip("Savitzky-Golay는 1차 미분 전용; 고차 미분은 Polynomial Fit 사용")
        le_min_delta = QLineEdit("1e-10")
        le_min_delta.setFixedWidth(80)
        le_min_delta.setFont(_MONO)
        wm_row.addWidget(QLabel("Window:"))
        wm_row.addWidget(sb_window)
        wm_row.addSpacing(8)
        wm_row.addWidget(QLabel("Method:"))
        wm_row.addWidget(cmb_method)
        wm_row.addSpacing(8)
        wm_row.addWidget(QLabel("Min |ΔA\u2082|:"))
        wm_row.addWidget(le_min_delta)
        wm_row.addStretch()

        ctrl_vbox = QVBoxLayout()
        ctrl_vbox.setSpacing(3)
        ctrl_vbox.addWidget(controls_widget)
        ctrl_vbox.addLayout(label_row)
        ctrl_vbox.addLayout(wm_row)
        vbox.addLayout(ctrl_vbox)

        setting_widgets = [cmb_a1, cmb_a2, le_label, le_unit, sb_window, le_min_delta]
        if order == 1:
            setting_widgets.append(cmb_method)

        # Store widget refs on self using order-suffixed names
        suffix = "" if order == 1 else str(order)
        setattr(self, f"_cb_deriv{suffix}_enable",       cb_enable)
        setattr(self, f"_cmb_deriv{suffix}_a1",          cmb_a1)
        setattr(self, f"_cmb_deriv{suffix}_a2",          cmb_a2)
        setattr(self, f"_le_deriv{suffix}_label",         le_label)
        setattr(self, f"_le_deriv{suffix}_unit",          le_unit)
        setattr(self, f"_sb_deriv{suffix}_window",        sb_window)
        setattr(self, f"_cmb_deriv{suffix}_method",       cmb_method)
        setattr(self, f"_le_deriv{suffix}_min_delta",     le_min_delta)
        setattr(self, f"_deriv{suffix}_setting_widgets",  setting_widgets)

        cb_enable.toggled.connect(
            lambda checked, s=setting_widgets: self._on_deriv_enable_toggled_widgets(checked, s)
        )
        return box

    def _rebuild_deriv_combos(self):
        """파라미터가 변경될 때 모든 파생 채널 A1/A2 콤보박스 재구성."""
        if not hasattr(self, "_cmb_deriv_a1"):
            return
        profile = self._active_profile
        items = [("__sweep__", "— sweep channel —")]
        for m in profile.measurements:
            items.append((m.description, f"{m.figure_axis or m.description} [{m.unit}]"))

        for suffix in ("", "2", "3"):
            cmb_a1 = getattr(self, f"_cmb_deriv{suffix}_a1")
            cmb_a2 = getattr(self, f"_cmb_deriv{suffix}_a2")
            for cmb in (cmb_a1, cmb_a2):
                prev = cmb.currentData()
                cmb.blockSignals(True)
                cmb.clear()
                for key, display in items:
                    cmb.addItem(display, userData=key)
                idx = cmb.findData(prev)
                cmb.setCurrentIndex(idx if idx >= 0 else 0)
                cmb.blockSignals(False)

    def _on_deriv_enable_toggled_widgets(self, checked: bool, widgets: list):
        for w in widgets:
            w.setEnabled(checked and not self._running)

    def _build_deriv_config(self, order: int = 1) -> DerivativeConfig:
        """현재 UI 상태에서 DerivativeConfig 생성 (order = 1/2/3)."""
        suffix = "" if order == 1 else str(order)
        method_map = {0: "linear", 1: "savgol"}
        try:
            min_delta = float(getattr(self, f"_le_deriv{suffix}_min_delta").text())
        except ValueError:
            min_delta = 1e-10
        cmb_method = getattr(self, f"_cmb_deriv{suffix}_method")
        method = method_map.get(cmb_method.currentIndex(), "linear") if order == 1 else "linear"
        return DerivativeConfig(
            enabled=getattr(self, f"_cb_deriv{suffix}_enable").isChecked(),
            order=order,
            numerator_key=getattr(self, f"_cmb_deriv{suffix}_a1").currentData() or "",
            denominator_key=getattr(self, f"_cmb_deriv{suffix}_a2").currentData() or "",
            output_label=getattr(self, f"_le_deriv{suffix}_label").text().strip(),
            output_unit=getattr(self, f"_le_deriv{suffix}_unit").text().strip(),
            window_size=getattr(self, f"_sb_deriv{suffix}_window").value(),
            method=method,
            min_delta=min_delta,
        )

    def _deriv_val_for_step(self, result, meas_map: dict) -> "float | None":
        """현재 스텝에서 1차 미분값 계산."""
        return self._deriv_val_for_order(result, meas_map, self._deriv_channel)

    def _deriv_val_for_step2(self, result, meas_map: dict) -> "float | None":
        """현재 스텝에서 2차 미분값 계산."""
        return self._deriv_val_for_order(result, meas_map, self._deriv_channel2)

    def _deriv_val_for_step3(self, result, meas_map: dict) -> "float | None":
        """현재 스텝에서 3차 미분값 계산."""
        return self._deriv_val_for_order(result, meas_map, self._deriv_channel3)

    def _deriv_val_for_order(self, result, meas_map: dict, channel: DerivativeChannel) -> "float | None":
        """공통: A1/A2 값 추출 후 채널에 push."""
        cfg = channel._cfg
        if not cfg.enabled:
            return None

        def _get(key):
            if key == "__sweep__":
                return result.next_v
            for idx in self._active_meas_indices:
                if self._active_profile.measurements[idx].description == key:
                    return meas_map.get(idx)
            return None

        a1 = _get(cfg.numerator_key)
        a2 = _get(cfg.denominator_key)
        if a1 is None or a2 is None:
            return None
        return channel.push(a1, a2)

    def _collect_meas_ui_state(self) -> tuple:
        """현재 UI 위젯에서 (prev_checked, prev_suffix, prev_type) 딕셔너리를 수집."""
        prev_checked: dict = {}
        prev_suffix: dict = {}
        prev_type: dict = {}
        if self._active_profile is None:
            return prev_checked, prev_suffix, prev_type
        for m, cb in zip(self._active_profile.measurements, self._meas_checkboxes):
            key = (m.alias, m.description)
            prev_checked[key] = cb.isChecked()
        for m, le in zip(self._active_profile.measurements, self._meas_suffix_edits):
            key = (m.alias, m.description)
            prev_suffix[key] = le.text()
        for m, ct in zip(self._active_profile.measurements, self._meas_type_combos):
            key = (m.alias, m.description)
            prev_type[key] = ct.currentData()
        return prev_checked, prev_suffix, prev_type

    def _on_selection_applied(self, profile: MainUIProfile):
        prev_checked, prev_suffix, prev_type = self._collect_meas_ui_state()
        self._active_profile = profile
        self._rebuild_sweep_channel_panel(profile.sweep_values)
        self._rebuild_meas_panel(profile.measurements, prev_checked, prev_suffix, prev_type)
        self._rebuild_write_panel(profile.write_cmds)
        self._rebuild_deriv_combos()
        if self._double_sweep_window is not None:
            self._double_sweep_window._rebuild_second_channel_radios()

    _TIME_ID = -2   # QButtonGroup ID for the fixed Time channel

    def _get_alias_color(self, alias: str) -> str:
        """Return a consistent color for the given alias (instrument)."""
        if alias not in self._alias_color_map:
            n = len(self._alias_color_map)
            self._alias_color_map[alias] = _ALIAS_PALETTE[n % len(_ALIAS_PALETTE)]
        return self._alias_color_map[alias]

    def _meas_label_for(self, idx: int, m) -> str:
        """Return column header for data-file.

        contact type → base = "contact" (suffix completely replaces figure_axis).
        other types  → base = figure_axis (suffix appended as before).
        """
        from config.config_models import MeasType
        suffix = ""
        if idx < len(self._meas_suffix_edits):
            suffix = self._meas_suffix_edits[idx].text().strip()
        meas_type_val = MeasType.NONE.value
        if idx < len(self._meas_type_combos):
            meas_type_val = self._meas_type_combos[idx].currentData() or MeasType.NONE.value
        if meas_type_val == MeasType.CONTACT.value:
            base = "contact"
        else:
            base = m.figure_axis or m.description
        return f"{base}_{suffix}" if suffix else base

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
            self._update_sweep_unit_labels("sec")
            self._sync_data_window_columns()
            return
        svs = self._active_profile.sweep_values
        if 0 <= btn_id < len(svs):
            self._sweep_channel = sweep_channel_from_instantiated(svs[btn_id])
            self._update_sweep_unit_labels(svs[btn_id].unit)
            self._sync_data_window_columns()

    def _update_sweep_unit_labels(self, unit: str):
        self._lbl_sweep_to_unit.setText(unit)
        self._lbl_sweep_rate_unit.setText(f"{unit}/min" if unit else "units/min")

    def _rebuild_meas_panel(self, measurements: list,
                             prev_checked: dict = None,
                             prev_suffix: dict = None,
                             prev_type: dict = None):
        from config.config_models import MeasType
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(2)

        self._meas_checkboxes = []
        self._meas_suffix_edits = []
        self._meas_type_combos = []

        for m in measurements:
            color = self._get_alias_color(m.alias)
            row_w = QWidget()
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.setSpacing(6)

            key = (m.alias, m.description)

            # ── Checkbox ──────────────────────────────────────────────
            cb = QCheckBox(f"[{m.alias}]  {m.description}  ({m.unit})")
            cb.setFont(_MONO)
            cb.setStyleSheet(f"QCheckBox {{ color: {color}; }}")
            init_checked = prev_checked.get(key, m.checked) if prev_checked else m.checked
            cb.setChecked(init_checked)
            m.checked = init_checked  # write-back: 프로파일과 동기화

            def _make_cb_wb(meas, _cb):
                def _wb():
                    meas.checked = _cb.isChecked()
                    self._refresh_meta_data_preview()
                return _wb
            cb.stateChanged.connect(_make_cb_wb(m, cb))
            self._meas_checkboxes.append(cb)
            row_h.addWidget(cb)

            # ── Type selector ─────────────────────────────────────────
            cb_type = QComboBox()
            cb_type.setFont(_MONO)
            cb_type.setFixedWidth(88)
            for t in MeasType:
                cb_type.addItem(t.value, t.value)
            init_type = prev_type.get(key, m.meas_type.value) if prev_type else m.meas_type.value
            idx_t = cb_type.findData(init_type)
            if idx_t >= 0:
                cb_type.setCurrentIndex(idx_t)
            try:
                m.meas_type = MeasType(init_type)  # write-back
            except Exception:
                pass

            def _make_type_wb(meas, widget):
                def _wb():
                    try:
                        meas.meas_type = MeasType(widget.currentData())
                    except Exception:
                        pass
                    self._on_meas_type_changed()
                return _wb
            cb_type.currentIndexChanged.connect(_make_type_wb(m, cb_type))
            self._meas_type_combos.append(cb_type)
            row_h.addWidget(cb_type)

            # ── Suffix edit ───────────────────────────────────────────
            le_suffix = QLineEdit()
            le_suffix.setFont(_MONO)
            le_suffix.setFixedWidth(110)
            le_suffix.setPlaceholderText("suffix")
            init_suffix = prev_suffix.get(key, m.axis_suffix) if prev_suffix else m.axis_suffix
            le_suffix.setText(init_suffix)
            m.axis_suffix = init_suffix  # write-back

            def _make_suffix_wb(meas, widget):
                def _wb(text):
                    meas.axis_suffix = text
                    self._sync_data_window_columns()
                return _wb
            le_suffix.textChanged.connect(_make_suffix_wb(m, le_suffix))
            le_suffix.setToolTip(
                "contact: 컬럼명 = contact_{suffix}\n"
                "기타 type: 컬럼명 = {figure_axis}_{suffix}"
            )
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
        # 연결 상태 확인 — 실패 시 측정 중단
        if not self._run_connection_test(include_second=False, show_success=False):
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
        # sweep channel / measurement / save 컨트롤 비활성화
        self._sweep_channel_panel.setEnabled(False)
        self._meas_panel.setEnabled(False)
        self._save_settings_frame.setEnabled(False)
        self._btn_copy_path.setEnabled(True)
        self._btn_open_folder.setEnabled(True)
        # Double Sweep window UI 잠금
        if self._double_sweep_window is not None:
            self._double_sweep_window.lock_ui(True)
        # Meta Data Config window UI 잠금
        if self._meta_data_window is not None:
            self._meta_data_window.lock_ui(True)
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
        # Derivative channels — reconfigure and reset buffers
        for order, ch in [(1, self._deriv_channel), (2, self._deriv_channel2), (3, self._deriv_channel3)]:
            ch.reconfigure(self._build_deriv_config(order))
            ch.reset()
        # Disable derivative settings while running
        for suffix in ("", "2", "3"):
            getattr(self, f"_cb_deriv{suffix}_enable").setEnabled(False)
            for w in getattr(self, f"_deriv{suffix}_setting_widgets"):
                w.setEnabled(False)

        if self._graph_window is not None:
            self._graph_window.begin_session(self._build_graph_columns())
        # MetaDataManager: configure T/B buffer for this sweep
        _meas_labels = [
            self._meas_label_for(idx, self._active_profile.measurements[idx])
            for idx in self._active_meas_indices
        ]
        from config.config_models import MeasType
        _meas_type_overrides = {}
        for idx in self._active_meas_indices:
            if idx < len(self._meas_type_combos):
                val = self._meas_type_combos[idx].currentData()
                try:
                    _meas_type_overrides[idx] = MeasType(val)
                except Exception:
                    pass
        self._meta_manager.configure(
            self._active_meas_indices,
            self._active_profile.measurements,
            _meas_labels,
            meas_type_overrides=_meas_type_overrides,
        )
        self._log("Sweep started.", color="#4ec9b0")
        self._log_sweep(
            f"━━ Sweep started  target={self._sweep_config.sweep_to:.4g}  "
            f"rate={self._sweep_config.sweep_rate:.4g}  "
            f"tpp={self._sweep_config.time_per_point:.3g}s",
            color="#4ec9b0",
        )
        # 초기 상태 측정 (이동 없이 현재 위치에서 measurement만)
        import time as _t
        active_init = [
            (row, self._active_profile.measurements[row].alias,
             self._active_profile.measurements[row].description,
             self._active_profile.measurements[row].resolved_cmd)
            for row in self._active_meas_indices
        ]
        self.request_step.emit(StepRequest(
            sweep_channel=self._sweep_channel,
            sweep_to=self._sweep_config.sweep_to,
            sweep_rate=self._sweep_config.sweep_rate,
            time_per_point=self._sweep_config.time_per_point,
            t_emit=_t.perf_counter(),
            last_write_value=None,
            active_measurements=active_init,
            measure_only=True,
        ))

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
        self._save_settings_frame.setEnabled(True)
        # re-enable Double Sweep if it's not actively running
        if self._double_sweep_window is not None:
            from gui.double_sweep_window import DoubleSweepPhase
            if self._double_sweep_window._phase == DoubleSweepPhase.IDLE:
                self._double_sweep_window._btn_start.setEnabled(True)
                self._double_sweep_window.lock_ui(False)
        if self._meta_data_window is not None:
            self._meta_data_window.lock_ui(False)
        # Re-enable derivative settings
        for suffix in ("", "2", "3"):
            cb = getattr(self, f"_cb_deriv{suffix}_enable")
            cb.setEnabled(True)
            enabled = cb.isChecked()
            for w in getattr(self, f"_deriv{suffix}_setting_widgets"):
                w.setEnabled(enabled)
        self._stop_glow()
        self._log("Sweep stopped.", color="#ce9178")

    def _log_sweep(self, text: str, color: str = "#c9d1d9", verbose: bool = False):
        """Sweep Log 패널에 기록. verbose=True 항목은 Verbose 체크 시에만 표시."""
        self._debug_window.log_sweep(text, color=color, verbose=verbose)

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

        # Sweep Log: 단계 컨텍스트 저장 (오류 발생 시 참조용) + verbose 로그
        meas_names = ", ".join(desc for _, _, desc, _ in active) or "—"
        self._step_context = (
            f"step#{self._sweep_step_count + 1}  "
            f"target={self._sweep_config.sweep_to:.4g}  "
            f"meas=[{meas_names}]"
        )
        self._log_sweep(
            f"→ {self._step_context}",
            color="#555555", verbose=True,
        )


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

        # is_done=True without measurements → already at target, no data to record
        if result.is_done and not result.meas_results:
            self._lbl_idle.setText("—")
            self._lbl_remaining.setText("—")
            self._log(f"Sweep complete. ({self._sweep_step_count} steps)", color="#4ec9b0")
            self._log_sweep(
                f"★ Sweep complete — {self._sweep_step_count} steps",
                color="#4ec9b0",
            )
            self._meta_manager.save(
                self._param_manager_reg.meta_data_config,
                self._data_saver.get_filepath(),
            )
            self._on_stop()
            return

        self.set_source_value(result.current)
        self._sweep_step_count += 1

        # DataWindow 갱신: next_v + 체크된 measurement 값만
        meas_map = {row: val for row, val in result.meas_results}
        row_vals = [f"{result.next_v:.6g}"]
        has_err = False
        for idx in self._active_meas_indices:
            val = meas_map.get(idx)
            if val is None:
                has_err = True
            row_vals.append(f"{val:.6g}" if val is not None else "ERR")

        if has_err:
            err_descs = [
                self._active_profile.measurements[idx].description
                + (f": {result.meas_errors[idx]}" if idx in result.meas_errors else "")
                for idx in self._active_meas_indices
                if meas_map.get(idx) is None
            ]
            err_msg = (
                f"✗ ERR @ {self._step_context}\n"
                f"실패 채널: {', '.join(err_descs)}"
            )
            self._log_sweep(f"  {err_msg}", color="#f44747")
            self._on_stop()
            QMessageBox.critical(self, "Measurement Error", err_msg)
            return
        else:
            # verbose: 측정값 요약
            val_summary = "  ".join(
                f"{self._active_profile.measurements[idx].description}="
                f"{meas_map[idx]:.4g}"
                for idx in self._active_meas_indices
                if meas_map.get(idx) is not None
            )
            if val_summary:
                self._log_sweep(
                    f"  ✓ v={result.next_v:.4g}  {val_summary}",
                    color="#888888", verbose=True,
                )

        # Derivative channel computation (numpy on ≤50 points — GUI thread safe)
        deriv_val  = self._deriv_val_for_step(result, meas_map)
        deriv_val2 = self._deriv_val_for_step2(result, meas_map)
        deriv_val3 = self._deriv_val_for_step3(result, meas_map)
        for ch, val in [
            (self._deriv_channel,  deriv_val),
            (self._deriv_channel2, deriv_val2),
            (self._deriv_channel3, deriv_val3),
        ]:
            if ch._cfg.enabled:
                row_vals.append(f"{val:.6g}" if val is not None else "—")

        self._data_window.update_values(row_vals)
        self._data_saver.append_row(row_vals)

        # MetaDataManager: T/B 버퍼에 이번 스텝 값 누적
        self._meta_manager.record_step(result.meas_results)

        # Graph update
        if self._graph_window is not None:
            from gui.graph_window import GraphDataPoint
            gvals = {"__sweep__": result.next_v}
            for idx in self._active_meas_indices:
                val = meas_map.get(idx)
                if val is not None:
                    gvals[self._active_profile.measurements[idx].description] = val
            for ch, key, val in [
                (self._deriv_channel,  _DERIV_KEY,  deriv_val),
                (self._deriv_channel2, _DERIV2_KEY, deriv_val2),
                (self._deriv_channel3, _DERIV3_KEY, deriv_val3),
            ]:
                if ch._cfg.enabled:
                    gvals[key] = val if val is not None else float("nan")
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
            self._log_sweep(
                f"★ Sweep complete — {self._sweep_step_count} steps",
                color="#4ec9b0",
            )
            # MetaData 저장 (sweep worker 완료 후이므로 VISA 안전)
            self._meta_manager.save(
                self._param_manager_reg.meta_data_config,
                self._data_saver.get_filepath(),
            )
            self._on_stop()
        elif result.measure_only:
            # 초기 상태 측정 완료 → 즉시 sweep 타이머 시작
            self._sweep_step_timer.start(0)
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
        ctx = getattr(self, "_step_context", "unknown step")
        self._log(f"  ERROR (sweep tick): {msg}", color="#f44747")
        self._log_sweep(
            f"  ✗ EXCEPTION @ {ctx}\n    {msg}",
            color="#f44747",
        )
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

    def _open_save_folder(self):
        import subprocess, os
        path = self._lbl_save_preview.text()
        if not path or path == "—" or path.startswith("("):
            return
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            # 아직 생성 안 됐으면 상위 폴더로 올라가기
            from pathlib import Path
            p = Path(folder)
            while p and not p.is_dir():
                p = p.parent
            folder = str(p) if p and p.is_dir() else ""
        if folder:
            subprocess.Popen(f'explorer "{folder}"')

    def _update_save_preview(self):
        self._data_saver.set_main_folder(self._le_main_folder.text())
        self._data_saver.set_custom_folder(self._le_custom_folder.text())
        self._data_saver.set_custom_word(self._le_custom_word.text())
        self._data_saver.set_include_date(self._cb_save_date.isChecked())
        self._data_saver.set_enabled(self._cb_save_enable.isChecked())
        self._lbl_save_preview.setText(self._data_saver.preview_path())

    def _on_meas_type_changed(self):
        self._sync_data_window_columns()
        self._refresh_meta_data_preview()

    def _refresh_meta_data_preview(self):
        if self._meta_data_window and self._meta_data_window.isVisible():
            self._meta_data_window._refresh_preview()

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
                columns.append((self._meas_label_for(idx, m), m.unit))
        else:
            for i, m in enumerate(profile.measurements):
                cb = self._meas_checkboxes[i] if i < len(self._meas_checkboxes) else None
                if cb is None or cb.isChecked():
                    columns.append((self._meas_label_for(i, m), m.unit))
        for ch in (self._deriv_channel, self._deriv_channel2, self._deriv_channel3):
            if ch._cfg.enabled:
                _, lbl, unit = ch.col_info()
                columns.append((lbl, unit))
        self._data_window.configure_columns(columns)
        self._data_saver.set_columns(columns)

    @staticmethod
    def _parse_sweep_float(text: str, default: float) -> float:
        try:
            return float(text)
        except (ValueError, TypeError):
            return default

    def _update_step_size_label(self):
        tmp = SweepConfig(
            sweep_rate=self._parse_sweep_float(self._le_sweep_rate.text(), 1.0),
            time_per_point=self._parse_sweep_float(self._le_time_per_point.text(), 1.0),
        )
        self._lbl_step_size.setText(f"{tmp.step_size():.6g}  units/step")

    def _on_sweep_params_confirmed(self):
        self._sweep_config.sweep_to       = self._parse_sweep_float(self._le_sweep_to.text(), 0.0)
        self._sweep_config.sweep_rate     = self._parse_sweep_float(self._le_sweep_rate.text(), 1.0)
        self._sweep_config.time_per_point = self._parse_sweep_float(self._le_time_per_point.text(), 1.0)
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

    def keyPressEvent(self, event):
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_S):
            self._save_current_to_active_profile()
            event.accept()
            return
        super().keyPressEvent(event)

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

        # 모든 하위 창 닫기
        for win in (
            self._graph_window,
            self._double_sweep_window,
            self._param_manager_window,
            self._visa_lib_window,
            self._settings_window,
        ):
            if win is not None:
                win.hide()

        self._debug_window.deleteLater()
        self._sweep_status_window.deleteLater()
        self._timing_window.deleteLater()
        self._data_window.deleteLater()

        from PySide6.QtWidgets import QApplication
        QApplication.quit()
        super().closeEvent(event)
