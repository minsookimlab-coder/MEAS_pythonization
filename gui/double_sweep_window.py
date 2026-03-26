"""
DoubleSweepWindow: Double Sweep 전용 창.
Second sweep channel 값을 배열로 순회하며 각 값마다 DUMMY→TRACE→RETRACE를 실행합니다.

실행 흐름:
  PRE_INIT (first channel → start_point, 데이터 없음)
  → ADVANCING_SECOND (second channel 설정)
  → DUMMY  (first channel: start_point→start_point, dummy/ 저장)
  → TRACE  (first channel: start_point→stop_point,  trace/ 저장)
  → RETRACE(first channel: stop_point→start_point,  retrace/ 저장)
  → ADVANCING_SECOND (다음 array 값) → ... → IDLE
"""
import math
import time
from datetime import datetime as _datetime, timedelta as _timedelta
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Tuple, TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QDoubleValidator, QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QFrame,
    QLabel, QLineEdit, QPushButton, QSpinBox, QCheckBox,
    QButtonGroup, QRadioButton, QMessageBox, QWidget, QComboBox,
    QScrollArea, QSizePolicy,
)

from config.config_models import (
    DoubleSweepConfig, InstantiatedSecondSweepChannel, SecondSweepAdvanceType,
    AlarmConfig, AlarmTrigger, AlarmOperator, InstantiatedMeasurement,
)
from core.alarm_manager import AlarmManager
from core.data_saver import DataSaver
from core.second_channel_worker import SecondChannelWorker, SecondChannelRequest
from core.sweep_worker import SweepWorker, StepRequest, StepResult

if TYPE_CHECKING:
    from gui.main_window import MainWindow

_MONO = QFont("Consolas", 10)


@dataclass
class DoubleSweepContext:
    """Double Sweep 시작 시 MainWindow에서 스냅샷한 sweep 실행 컨텍스트.

    MainWindow private 속성에 sweep 도중 반복 접근하는 대신
    시작 시점에 필요한 정보를 캡처해 사용합니다.
    """
    # First channel (SweepChannel 또는 TimeChannel)
    sweep_channel: object
    # First channel sweep value의 safety 파라미터 (없으면 0)
    sv_safety_steps: int
    sv_safety_interval_ms: float
    # 체크된 measurement 인덱스 + 실행에 필요한 resolved 정보
    active_meas_indices: List[int]
    active_measurements: List[Tuple[int, str, str, str]]  # (row, alias, desc, cmd)
    # DataSaver 열 헤더: (name, unit)
    sweep_col: Tuple[str, str]
    meas_cols: List[Tuple[str, str]]
    # DataSaver 저장 경로 설정
    main_folder: str
    custom_folder: str
    custom_word: str
    include_date: bool
    save_enabled: bool


class DoubleSweepPhase(Enum):
    IDLE             = auto()
    PRE_INIT         = auto()   # first channel → start_point (no data)
    RETURNING_ZERO   = auto()   # second channel → 0 after all array steps (no measurement)
    ADVANCING_SECOND = auto()
    DUMMY            = auto()
    TRACE            = auto()
    RETRACE          = auto()


def _generate_array(cfg: DoubleSweepConfig) -> List[float]:
    """numpy 없이 arange 구현 (step 부호 자동 처리)."""
    if abs(cfg.array_step) < 1e-12:
        return [cfg.array_from]
    step = cfg.array_step
    eps = abs(step) * 1e-9
    result = []
    v = cfg.array_from
    while (step > 0 and v <= cfg.array_to + eps) or (step < 0 and v >= cfg.array_to - eps):
        result.append(round(v, 12))
        v += step
    return result if result else [cfg.array_from]


def _estimate_total_seconds(
    cfg: "DoubleSweepConfig",
    n_array: int,
    second_ch: "Optional[InstantiatedSecondSweepChannel]" = None,
) -> float:
    """Estimate total sweep time in seconds.

    Rules:
    - time = distance * 60 / rate  (tpp is measurement interval only, does not affect travel time)
    - retrace_to_zero: retrace goes to 0; dummy goes from 0 to start_point
    - second channel advance time: SWEEP → step * 60 / sweep_rate; WAIT_FOR_TIME → wait_time; others → 0
    """
    if n_array == 0:
        return 0.0

    def _sweep_time(dist: float, rate: float) -> float:
        if rate <= 0 or dist <= 0:
            return 0.0
        return dist * 60.0 / rate

    trace_dist   = abs(cfg.stop_point - cfg.start_point)
    if cfg.retrace_to_zero:
        retrace_dist = abs(cfg.stop_point)
        dummy_dist   = abs(cfg.start_point)
    else:
        retrace_dist = trace_dist
        dummy_dist   = 0.0

    trace_time   = _sweep_time(trace_dist,   cfg.rate_trace)
    retrace_time = _sweep_time(retrace_dist, cfg.rate_retrace)
    dummy_time   = _sweep_time(dummy_dist,   cfg.rate_dummy)

    advance_time = 0.0
    if second_ch is not None:
        if second_ch.advance_type == SecondSweepAdvanceType.SWEEP and second_ch.sweep_rate > 0:
            step = abs(cfg.array_step) if abs(cfg.array_step) > 1e-12 else 0.0
            advance_time = _sweep_time(step, second_ch.sweep_rate)
        elif second_ch.advance_type == SecondSweepAdvanceType.WAIT_FOR_TIME:
            advance_time = getattr(second_ch, "wait_time", 0.0)

    time_per_cycle = advance_time + dummy_time + trace_time + retrace_time
    total = n_array * time_per_cycle

    # to_zero_at_last: add time for final second channel → 0 move
    if cfg.to_zero_at_last and second_ch is not None:
        last_val = cfg.array_from + (n_array - 1) * cfg.array_step
        if second_ch.advance_type == SecondSweepAdvanceType.SWEEP and second_ch.sweep_rate > 0:
            total += _sweep_time(abs(last_val), second_ch.sweep_rate)
        elif second_ch.advance_type == SecondSweepAdvanceType.WAIT_FOR_TIME:
            total += getattr(second_ch, "wait_time", 0.0)

    return total


def _fmt_hms(total_sec: float) -> str:
    total_sec = max(0.0, total_sec)
    h = int(total_sec) // 3600
    m = (int(total_sec) % 3600) // 60
    s = int(total_sec) % 60
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ---------------------------------------------------------------------------


_OP_LABELS = [">", "<", ">=", "<=", "==", "!="]


def _truncate(s: str, n: int = 40) -> str:
    return s[:n] + ("…" if len(s) > n else "")


class TelegramPanel(QFrame):
    """
    Double Sweep 창 우측 패널 — Telegram Bot 알람 설정.

    수신자 목록(이름 → Chat ID)을 저장·관리하고,
    콤보박스 선택 = 알람 전송 대상.
    """

    def __init__(self, alarm_manager, parent=None):
        super().__init__(parent)
        self._alarm_manager = alarm_manager
        self._contacts: list = []   # [{"name": str, "chat_id": str}]
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        title = QLabel("Telegram")
        title.setStyleSheet("font-weight: bold; color: #58a6ff; font-size: 13px;")
        root.addWidget(title)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #30363d;"); root.addWidget(sep)

        self._cb_enable = QCheckBox("활성화")
        self._cb_enable.setStyleSheet("font-weight: bold;")
        root.addWidget(self._cb_enable)

        # Bot Token
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(6); form.setVerticalSpacing(4)
        token_row = QHBoxLayout(); token_row.setSpacing(4)
        self._le_token = QLineEdit()
        self._le_token.setFont(_MONO)
        self._le_token.setPlaceholderText("123456789:AAB...")
        self._le_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._btn_show = QPushButton("Show")
        self._btn_show.setFixedHeight(22); self._btn_show.setFixedWidth(44)
        self._btn_show.setCheckable(True)
        self._btn_show.toggled.connect(self._on_show_toggled)
        token_row.addWidget(self._le_token, stretch=1); token_row.addWidget(self._btn_show)
        form.addRow("Bot Token:", token_row)
        root.addLayout(form)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color: #30363d;"); root.addWidget(sep2)

        # 수신자 선택 (알람 대상)
        recv_lbl = QLabel("수신자 (알람 대상)")
        recv_lbl.setStyleSheet("color: #79c0ff; font-weight: bold;")
        root.addWidget(recv_lbl)

        self._combo_contacts = QComboBox()
        self._combo_contacts.setFont(_MONO)
        self._combo_contacts.setPlaceholderText("(저장된 수신자 없음)")
        self._combo_contacts.currentIndexChanged.connect(self._on_contact_selected)
        root.addWidget(self._combo_contacts)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setStyleSheet("color: #30363d;"); root.addWidget(sep3)

        # 수신자 추가/편집 영역
        add_lbl = QLabel("수신자 추가 / 편집")
        add_lbl.setStyleSheet("color: #888; font-size: 10px;")
        root.addWidget(add_lbl)

        edit_form = QFormLayout()
        edit_form.setContentsMargins(0, 0, 0, 0)
        edit_form.setHorizontalSpacing(6); edit_form.setVerticalSpacing(3)
        self._le_contact_name = QLineEdit()
        self._le_contact_name.setFont(_MONO)
        self._le_contact_name.setPlaceholderText("표시 이름 (예: 실험실)")
        edit_form.addRow("이름:", self._le_contact_name)
        self._le_new_chat_id = QLineEdit()
        self._le_new_chat_id.setFont(_MONO)
        self._le_new_chat_id.setPlaceholderText("-100123456789")
        edit_form.addRow("Chat ID:", self._le_new_chat_id)
        root.addLayout(edit_form)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        btn_save = QPushButton("저장"); btn_save.setFixedHeight(24)
        btn_del  = QPushButton("삭제"); btn_del.setFixedHeight(24)
        btn_save.clicked.connect(self._on_save_contact)
        btn_del.clicked.connect(self._on_del_contact)
        btn_row.addWidget(btn_save); btn_row.addWidget(btn_del); btn_row.addStretch()
        root.addLayout(btn_row)

        sep4 = QFrame(); sep4.setFrameShape(QFrame.Shape.HLine)
        sep4.setStyleSheet("color: #30363d;"); root.addWidget(sep4)

        test_row = QHBoxLayout()
        self._btn_test = QPushButton("Test"); self._btn_test.setFixedHeight(24)
        self._btn_test.clicked.connect(self._on_test)
        test_row.addWidget(self._btn_test); test_row.addStretch()
        root.addLayout(test_row)

        self._lbl_status = QLabel("")
        self._lbl_status.setFont(_MONO)
        self._lbl_status.setStyleSheet("font-size: 10px;")
        self._lbl_status.setWordWrap(True)
        root.addWidget(self._lbl_status)

        root.addStretch()

    # ------------------------------------------------------------------

    def _on_show_toggled(self, checked: bool):
        self._le_token.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self._btn_show.setText("Hide" if checked else "Show")

    def _active_chat_id(self) -> str:
        """콤보박스에서 선택된 수신자의 Chat ID 반환."""
        idx = self._combo_contacts.currentIndex()
        if 0 <= idx < len(self._contacts):
            return self._contacts[idx]["chat_id"]
        return ""

    def _on_contact_selected(self, idx: int):
        """콤보 선택 시 편집 필드에 해당 수신자 정보 채움."""
        if 0 <= idx < len(self._contacts):
            c = self._contacts[idx]
            self._le_contact_name.setText(c["name"])
            self._le_new_chat_id.setText(c["chat_id"])

    def _on_save_contact(self):
        name    = self._le_contact_name.text().strip()
        chat_id = self._le_new_chat_id.text().strip()
        if not name or not chat_id:
            self._set_status("이름과 Chat ID를 입력하세요.", "#f44747"); return
        for c in self._contacts:
            if c["name"] == name:
                c["chat_id"] = chat_id
                self._refresh_combo(name)
                self._set_status(f"'{name}' 업데이트됨.", "#56d364"); return
        self._contacts.append({"name": name, "chat_id": chat_id})
        self._refresh_combo(name)
        self._set_status(f"'{name}' 저장됨.", "#56d364")

    def _on_del_contact(self):
        idx = self._combo_contacts.currentIndex()
        if 0 <= idx < len(self._contacts):
            name = self._contacts.pop(idx)["name"]
            self._refresh_combo()
            self._le_contact_name.clear(); self._le_new_chat_id.clear()
            self._set_status(f"'{name}' 삭제됨.", "#e3b341")

    def _refresh_combo(self, select_name: str = ""):
        self._combo_contacts.blockSignals(True)
        self._combo_contacts.clear()
        for c in self._contacts:
            self._combo_contacts.addItem(c["name"])
        if select_name:
            idx = self._combo_contacts.findText(select_name)
            if idx >= 0:
                self._combo_contacts.setCurrentIndex(idx)
        self._combo_contacts.blockSignals(False)

    def _set_status(self, msg: str, color: str):
        self._lbl_status.setStyleSheet(f"color: {color}; font-size: 10px;")
        self._lbl_status.setText(msg)

    def _on_test(self):
        token   = self._le_token.text().strip()
        chat_id = self._active_chat_id()
        if not token or not chat_id:
            self._set_status("Token과 수신자를 설정하세요.", "#f44747"); return
        self._btn_test.setEnabled(False)
        self._set_status("전송 중...", "#888")
        import threading
        def _do():
            err = self._alarm_manager.send_telegram_test(token, chat_id)
            from PySide6.QtCore import QMetaObject, Qt as _Qt, Q_ARG
            QMetaObject.invokeMethod(
                self, "_on_test_result", _Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, err or ""), Q_ARG(bool, err is None),
            )
        threading.Thread(target=_do, daemon=True).start()

    @Slot(str, bool)
    def _on_test_result(self, err: str, ok: bool):
        self._btn_test.setEnabled(True)
        if ok:
            self._set_status("✓ 메시지 전송 성공", "#56d364")
        else:
            self._set_status(f"✗ {err}", "#f44747")

    # ------------------------------------------------------------------
    # Config serialization
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        return {
            "use_telegram": self._cb_enable.isChecked(),
            "telegram_bot_token": self._le_token.text().strip(),
            "telegram_chat_id": self._active_chat_id(),   # 선택된 수신자
            "telegram_contacts": list(self._contacts),
        }

    def load_config(self, cfg: "AlarmConfig"):
        self._cb_enable.setChecked(cfg.use_telegram)
        self._le_token.setText(cfg.telegram_bot_token)
        self._contacts = [{"name": c.name, "chat_id": c.chat_id}
                          for c in cfg.telegram_contacts]
        # 저장된 chat_id와 일치하는 수신자를 선택 상태로 복원
        self._refresh_combo()
        if cfg.telegram_chat_id:
            for i, c in enumerate(self._contacts):
                if c["chat_id"] == cfg.telegram_chat_id:
                    self._combo_contacts.setCurrentIndex(i)
                    break


class AlarmPanel(QFrame):
    """
    Double Sweep 창 우측 패널 — Alarm 세부 설정.

    - Enable / Sound / Email 토글
    - Fixed triggers: 통신 오류, 모든 측정 완료
    - Measurement triggers: alarm_measurements 목록에서 조건 추가/삭제
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._trigger_rows: list = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        title = QLabel("Alarm")
        title.setStyleSheet("font-weight: bold; color: #ffa657; font-size: 13px;")
        root.addWidget(title)

        sep0 = QFrame(); sep0.setFrameShape(QFrame.Shape.HLine)
        sep0.setStyleSheet("color: #30363d;"); root.addWidget(sep0)

        self._cb_enable = QCheckBox("활성화")
        self._cb_enable.setStyleSheet("font-weight: bold;")
        self._cb_enable.toggled.connect(self._update_enabled_state)
        root.addWidget(self._cb_enable)

        notif = QHBoxLayout()
        self._cb_sound = QCheckBox("사운드"); self._cb_sound.setChecked(True)
        self._cb_email = QCheckBox("이메일")
        self._cb_email.toggled.connect(self._on_email_toggled)
        notif.addWidget(self._cb_sound); notif.addWidget(self._cb_email); notif.addStretch()
        root.addLayout(notif)

        self._email_widget = QWidget()
        ef = QFormLayout(self._email_widget)
        ef.setContentsMargins(12, 0, 0, 0); ef.setHorizontalSpacing(6); ef.setVerticalSpacing(3)
        self._le_email_to  = QLineEdit(); self._le_email_to.setPlaceholderText("recipient@example.com")
        self._le_smtp_host = QLineEdit("smtp.gmail.com")
        self._sb_smtp_port = QSpinBox(); self._sb_smtp_port.setRange(1, 65535); self._sb_smtp_port.setValue(587)
        self._le_smtp_user = QLineEdit(); self._le_smtp_user.setPlaceholderText("sender@gmail.com")
        self._le_smtp_pass = QLineEdit(); self._le_smtp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._le_smtp_pass.setPlaceholderText("앱 비밀번호")
        for le in (self._le_email_to, self._le_smtp_host, self._le_smtp_user, self._le_smtp_pass):
            le.setFont(_MONO)
        ef.addRow("수신:", self._le_email_to); ef.addRow("SMTP:", self._le_smtp_host)
        ef.addRow("Port:", self._sb_smtp_port); ef.addRow("User:", self._le_smtp_user)
        ef.addRow("Pass:", self._le_smtp_pass)
        self._email_widget.setVisible(False)
        root.addWidget(self._email_widget)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet("color: #30363d;"); root.addWidget(sep1)

        fixed_lbl = QLabel("고정 트리거")
        fixed_lbl.setStyleSheet("color: #79c0ff; font-weight: bold;")
        root.addWidget(fixed_lbl)
        self._cb_comm_error  = QCheckBox("통신 오류 / Timeout")
        self._cb_meas_error  = QCheckBox("측정값 ERR (채널 실패)")
        self._cb_on_complete = QCheckBox("모든 측정 완료 시")
        root.addWidget(self._cb_comm_error)
        root.addWidget(self._cb_meas_error)
        root.addWidget(self._cb_on_complete)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color: #30363d;"); root.addWidget(sep2)

        meas_lbl = QLabel("측정값 조건")
        meas_lbl.setStyleSheet("color: #79c0ff; font-weight: bold;")
        root.addWidget(meas_lbl)

        self._trig_scroll = QScrollArea()
        self._trig_scroll.setWidgetResizable(True)
        self._trig_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._trig_scroll.setMinimumHeight(60)
        self._trig_scroll.setMaximumHeight(200)
        self._trig_content = QWidget()
        self._trig_layout = QVBoxLayout(self._trig_content)
        self._trig_layout.setContentsMargins(0, 0, 0, 0); self._trig_layout.setSpacing(2)
        self._trig_layout.addStretch()
        self._trig_scroll.setWidget(self._trig_content)
        root.addWidget(self._trig_scroll)

        self._combo_meas = QComboBox()
        self._combo_meas.setFont(_MONO)
        self._combo_meas.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        root.addWidget(self._combo_meas)

        add_row = QHBoxLayout(); add_row.setSpacing(4)
        self._combo_op = QComboBox(); self._combo_op.addItems(_OP_LABELS); self._combo_op.setFixedWidth(52)
        self._le_thresh = QLineEdit("0"); self._le_thresh.setFont(_MONO)
        btn_add = QPushButton("+ 추가"); btn_add.setFixedHeight(24)
        btn_add.clicked.connect(self._add_trigger_row)
        add_row.addWidget(self._combo_op); add_row.addWidget(self._le_thresh, stretch=1); add_row.addWidget(btn_add)
        root.addLayout(add_row)

        root.addStretch()
        self._update_enabled_state(False)

    def _on_email_toggled(self, checked: bool):
        self._email_widget.setVisible(checked)

    def _update_enabled_state(self, enabled: bool):
        for w in (self._cb_sound, self._cb_email, self._cb_comm_error, self._cb_meas_error,
                  self._cb_on_complete, self._trig_scroll, self._combo_meas,
                  self._combo_op, self._le_thresh):
            w.setEnabled(enabled)

    def _add_trigger_row(self, meas: str = "", op_str: str = "", thresh: float = 0.0):
        if not meas:
            meas = self._combo_meas.currentText().strip()
        if not meas:
            return
        if not op_str:
            op_str = self._combo_op.currentText()
        if not thresh:
            try:
                thresh = float(self._le_thresh.text())
            except ValueError:
                thresh = 0.0

        row_w = QWidget()
        row_h = QHBoxLayout(row_w)
        row_h.setContentsMargins(0, 0, 0, 0); row_h.setSpacing(3)

        cb = QCheckBox(); cb.setChecked(True); cb.setFixedWidth(18)
        lbl = QLabel(f"{_truncate(meas, 22)}  {op_str}  {thresh:.4g}")
        lbl.setFont(_MONO); lbl.setStyleSheet("color: #c9d1d9; font-size: 10px;")
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        btn_del = QPushButton("×"); btn_del.setFixedSize(18, 18)
        btn_del.setStyleSheet("color: #f44747; font-weight: bold; border: none;")

        row_h.addWidget(cb); row_h.addWidget(lbl, stretch=1); row_h.addWidget(btn_del)

        entry = {"widget": row_w, "cb": cb, "meas": meas, "op": op_str, "thresh": thresh}
        self._trigger_rows.append(entry)
        self._trig_layout.insertWidget(self._trig_layout.count() - 1, row_w)
        btn_del.clicked.connect(lambda: self._remove_trigger_row(entry))

    def _remove_trigger_row(self, entry: dict):
        if entry in self._trigger_rows:
            self._trigger_rows.remove(entry)
        w = entry["widget"]
        self._trig_layout.removeWidget(w)
        w.deleteLater()

    def refresh_measurements(self, measurements: List[InstantiatedMeasurement]):
        """alarm_measurements 목록으로 combo 갱신."""
        cur = self._combo_meas.currentText()
        self._combo_meas.blockSignals(True)
        self._combo_meas.clear()
        self._combo_meas.addItems([m.description for m in measurements])
        idx = self._combo_meas.findText(cur)
        if idx >= 0:
            self._combo_meas.setCurrentIndex(idx)
        self._combo_meas.blockSignals(False)

    def get_config(self) -> AlarmConfig:
        triggers = []
        if self._cb_comm_error.isChecked():
            triggers.append(AlarmTrigger(kind="comm_error", enabled=True))
        if self._cb_meas_error.isChecked():
            triggers.append(AlarmTrigger(kind="meas_error", enabled=True))
        for entry in self._trigger_rows:
            try:
                op = AlarmOperator(entry["op"])
            except ValueError:
                op = AlarmOperator.GT
            triggers.append(AlarmTrigger(
                kind="measurement",
                enabled=entry["cb"].isChecked(),
                meas_description=entry["meas"],
                operator=op,
                threshold=entry["thresh"],
            ))
        return AlarmConfig(
            enabled=self._cb_enable.isChecked(),
            use_sound=self._cb_sound.isChecked(),
            use_email=self._cb_email.isChecked(),
            email_to=self._le_email_to.text().strip(),
            smtp_host=self._le_smtp_host.text().strip(),
            smtp_port=self._sb_smtp_port.value(),
            smtp_user=self._le_smtp_user.text().strip(),
            smtp_password=self._le_smtp_pass.text(),
            fire_on_complete=self._cb_on_complete.isChecked(),
            triggers=triggers,
        )

    def load_config(self, cfg: AlarmConfig):
        self._cb_enable.setChecked(cfg.enabled)
        self._cb_sound.setChecked(cfg.use_sound)
        self._cb_email.setChecked(cfg.use_email)
        self._le_email_to.setText(cfg.email_to)
        self._le_smtp_host.setText(cfg.smtp_host)
        self._sb_smtp_port.setValue(cfg.smtp_port)
        self._le_smtp_user.setText(cfg.smtp_user)
        self._le_smtp_pass.setText(cfg.smtp_password)
        self._email_widget.setVisible(cfg.use_email)
        self._cb_on_complete.setChecked(cfg.fire_on_complete)

        has_comm = any(t.kind == "comm_error" and t.enabled for t in cfg.triggers)
        self._cb_comm_error.setChecked(has_comm)
        has_meas_err = any(t.kind == "meas_error" and t.enabled for t in cfg.triggers)
        self._cb_meas_error.setChecked(has_meas_err)

        for entry in list(self._trigger_rows):
            self._remove_trigger_row(entry)

        for t in cfg.triggers:
            if t.kind != "measurement":
                continue
            if self._combo_meas.findText(t.meas_description) < 0 and t.meas_description:
                self._combo_meas.addItem(t.meas_description)
            self._add_trigger_row(meas=t.meas_description, op_str=t.operator.value, thresh=t.threshold)
            if self._trigger_rows:
                self._trigger_rows[-1]["cb"].setChecked(t.enabled)

        self._update_enabled_state(cfg.enabled)


# ---------------------------------------------------------------------------
class DoubleSweepWindow(QDialog):
    """
    Double Sweep 창.

    - main_win._sweep_channel / _active_profile / _meas_checkboxes 사용 (읽기 전용)
    - 자체 SweepWorker + QThread + QTimer
    - 자체 SecondChannelWorker + QThread
    - 자체 DataSaver (phase별 subfolder)
    """

    # main_window에 sweep lock 신호
    sweep_started  = Signal()
    sweep_finished = Signal()

    # 워커에 요청
    request_step    = Signal(object)   # StepRequest → _sweep_worker
    request_advance = Signal(object)   # SecondChannelRequest → _second_worker

    def __init__(self, main_win: "MainWindow", param_reg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Double Sweep")
        self.setMinimumWidth(900)
        self.resize(1200, 680)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._main_win  = main_win
        self._param_reg = param_reg

        self._phase: DoubleSweepPhase = DoubleSweepPhase.IDLE
        self._array: List[float] = []
        self._array_idx: int = 0
        self._last_write_value: Optional[float] = None
        self._second_channel: Optional[InstantiatedSecondSweepChannel] = None
        self._ctx: Optional[DoubleSweepContext] = None   # set at sweep start
        self._cfg: Optional[DoubleSweepConfig] = None   # set at sweep start
        self._last_meas_values: dict = {}  # {description: float} 최신 측정값 캐시
        self._trace_filepath = None        # TRACE 파일 경로 (meta data JSON 저장용)
        self._alarm_manager = AlarmManager()

        # DataSaver
        self._data_saver = DataSaver()
        self._data_saver.set_error_callback(
            lambda msg: main_win._log(f"  [DoubleSweep DataSaver] {msg}", color="#f44747")
        )

        # Sweep worker
        self._sweep_worker = SweepWorker()
        self._sweep_worker.set_session(main_win._session)
        self._worker_thread = QThread(self)
        self._sweep_worker.moveToThread(self._worker_thread)
        self.request_step.connect(self._sweep_worker.run_step)
        self._sweep_worker.step_done.connect(self._on_step_done)
        self._sweep_worker.step_error.connect(self._on_step_error)
        self._worker_thread.start()

        # Second channel worker
        self._second_worker = SecondChannelWorker()
        self._second_worker.set_session(main_win._session)
        self._second_thread = QThread(self)
        self._second_worker.moveToThread(self._second_thread)
        self.request_advance.connect(self._second_worker.advance)
        self._second_worker.done.connect(self._on_advance_done)
        self._second_worker.error.connect(self._on_advance_error)
        self._second_thread.start()

        # Step timer (single-shot)
        self._sweep_timer = QTimer(self)
        self._sweep_timer.setSingleShot(True)
        self._sweep_timer.timeout.connect(self._sweep_tick)

        # Glow animation
        self._glow_phase = 0.0
        self._glow_timer = QTimer(self)
        self._glow_timer.setInterval(30)
        self._glow_timer.timeout.connect(self._update_glow)

        self._build_ui()
        self._load_config()
        self._rebuild_second_channel_radios()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        dialog_layout = QVBoxLayout(self)
        dialog_layout.setContentsMargins(0, 0, 0, 0)
        self._glow_frame = QFrame()
        self._glow_frame.setObjectName("dsGlowFrame")
        self._glow_frame.setStyleSheet(
            "QFrame#dsGlowFrame { border: 3px solid transparent; border-radius: 6px; }"
        )
        dialog_layout.addWidget(self._glow_frame)

        # 좌/우 2-패널 레이아웃
        glow_h = QHBoxLayout(self._glow_frame)
        glow_h.setContentsMargins(0, 0, 0, 0)
        glow_h.setSpacing(0)

        # 좌측 패널 (기존 컨트롤)
        left_widget = QWidget()
        outer = QVBoxLayout(left_widget)
        outer.setContentsMargins(10, 10, 6, 10)
        outer.setSpacing(8)
        glow_h.addWidget(left_widget, stretch=1)

        # 우측 패널 (AlarmPanel)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setMinimumWidth(260)
        right_scroll.setMaximumWidth(320)
        self._alarm_panel = AlarmPanel()
        right_scroll.setWidget(self._alarm_panel)
        self._right_alarm_scroll = right_scroll
        glow_h.addWidget(right_scroll)

        # 우측 패널 (TelegramPanel)
        tg_scroll = QScrollArea()
        tg_scroll.setWidgetResizable(True)
        tg_scroll.setFrameShape(QFrame.Shape.NoFrame)
        tg_scroll.setMinimumWidth(200)
        tg_scroll.setMaximumWidth(260)
        self._telegram_panel = TelegramPanel(self._alarm_manager)
        tg_scroll.setWidget(self._telegram_panel)
        self._right_tg_scroll = tg_scroll
        glow_h.addWidget(tg_scroll)

        # Second Channel selector
        ch_frame = QFrame()
        ch_frame.setFrameShape(QFrame.Shape.StyledPanel)
        ch_layout = QVBoxLayout(ch_frame)
        ch_layout.setContentsMargins(8, 6, 8, 6)
        ch_title = QLabel("Second Channel")
        ch_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #f78166;")
        ch_layout.addWidget(ch_title)
        self._second_ch_layout = QVBoxLayout()
        ch_layout.addLayout(self._second_ch_layout)
        self._second_radio_group = QButtonGroup(self)
        self._second_radio_group.setExclusive(True)
        self._second_radio_group.idToggled.connect(self._on_second_radio_toggled)
        self._ch_frame = ch_frame
        outer.addWidget(ch_frame)

        # Sweep Parameters
        sp_frame = QFrame()
        sp_frame.setFrameShape(QFrame.Shape.StyledPanel)
        sp_layout = QVBoxLayout(sp_frame)
        sp_layout.setContentsMargins(8, 6, 8, 6)
        sp_title = QLabel("Sweep Parameters")
        sp_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        sp_layout.addWidget(sp_title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        sp_layout.addLayout(form)

        def _lefield(w=160):
            """QLineEdit + unit QLabel + container widget."""
            le = QLineEdit()
            le.setFont(_MONO)
            le.setFixedWidth(w)
            vd = QDoubleValidator()
            vd.setNotation(QDoubleValidator.Notation.StandardNotation)
            le.setValidator(vd)
            lbl_u = QLabel("")
            lbl_u.setFont(_MONO)
            lbl_u.setStyleSheet("color: #888888;")
            lbl_u.setMinimumWidth(60)
            cnt = QWidget()
            h = QHBoxLayout(cnt)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            h.addWidget(le)
            h.addWidget(lbl_u)
            h.addStretch()
            return le, lbl_u, cnt

        self._le_start,  self._lbl_start_unit,  cnt_start  = _lefield()
        self._le_stop,   self._lbl_stop_unit,   cnt_stop   = _lefield()
        self._le_rate_t, self._lbl_rate_t_unit, cnt_rate_t = _lefield()
        self._le_rate_r, self._lbl_rate_r_unit, cnt_rate_r = _lefield()
        self._le_rate_d, self._lbl_rate_d_unit, cnt_rate_d = _lefield()
        self._le_tpp,    _lbl_tpp_unit,         cnt_tpp    = _lefield()
        _lbl_tpp_unit.setText("sec")

        self._cb_retrace_to_zero = QCheckBox("Retrace to 0")
        self._cb_retrace_to_zero.setFont(_MONO)
        self._cb_retrace_to_zero.setToolTip("When checked, RETRACE sweeps to 0 instead of Start Point")

        form.addRow("Start Point:", cnt_start)
        form.addRow("Stop Point:",  cnt_stop)
        form.addRow("Rate (trace):",    cnt_rate_t)
        form.addRow("Rate (retrace):",  cnt_rate_r)
        form.addRow("",                 self._cb_retrace_to_zero)
        form.addRow("Rate (dummy):",    cnt_rate_d)
        form.addRow("Time / Point:",    cnt_tpp)
        self._sp_frame = sp_frame
        outer.addWidget(sp_frame)

        # SWEEP Channel Settings (only visible when second channel advance_type == SWEEP)
        self._sweep_ch_frame = QFrame()
        self._sweep_ch_frame.setFrameShape(QFrame.Shape.StyledPanel)
        sc_layout = QVBoxLayout(self._sweep_ch_frame)
        sc_layout.setContentsMargins(8, 6, 8, 6)
        sc_title = QLabel("SWEEP Channel Settings")
        sc_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #c586c0;")
        sc_layout.addWidget(sc_title)

        sc_form = QFormLayout()
        sc_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        sc_form.setHorizontalSpacing(12)
        sc_layout.addLayout(sc_form)

        self._le_second_rate, self._lbl_second_rate_unit, cnt_sr = _lefield()
        sc_form.addRow("Sweep Rate:", cnt_sr)

        self._cb_second_safety = QCheckBox("Use Safety Ramp")
        self._cb_second_safety.setFont(_MONO)
        sc_form.addRow("", self._cb_second_safety)

        self._sb_second_steps = QSpinBox()
        self._sb_second_steps.setRange(0, 100000)
        self._sb_second_steps.setValue(0)
        self._sb_second_steps.setFont(_MONO)
        self._sb_second_steps.setFixedWidth(100)
        self._sb_second_steps.setEnabled(False)
        sc_form.addRow("Safety Steps:", self._sb_second_steps)

        self._le_second_interval, _lbl_si_unit, cnt_si = _lefield()
        _lbl_si_unit.setText("ms")
        self._le_second_interval.setEnabled(False)
        sc_form.addRow("Safety Interval:", cnt_si)

        self._cb_second_safety.toggled.connect(self._sb_second_steps.setEnabled)
        self._cb_second_safety.toggled.connect(self._le_second_interval.setEnabled)

        self._sweep_ch_frame.setVisible(False)
        outer.addWidget(self._sweep_ch_frame)

        # Second Channel Array
        arr_frame = QFrame()
        arr_frame.setFrameShape(QFrame.Shape.StyledPanel)
        arr_layout = QVBoxLayout(arr_frame)
        arr_layout.setContentsMargins(8, 6, 8, 6)
        arr_title = QLabel("Second Channel Array")
        arr_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        arr_layout.addWidget(arr_title)

        def _le_inline(w=90):
            """QLineEdit + unit QLabel without a container (avoids parent-child GC issue)."""
            le = QLineEdit()
            le.setFont(_MONO)
            le.setFixedWidth(w)
            vd = QDoubleValidator()
            vd.setNotation(QDoubleValidator.Notation.StandardNotation)
            le.setValidator(vd)
            lbl = QLabel("")
            lbl.setFont(_MONO)
            lbl.setStyleSheet("color: #888888;")
            lbl.setMinimumWidth(50)
            return le, lbl

        arr_row = QHBoxLayout()
        arr_row.addWidget(QLabel("From:"))
        self._le_arr_from,  self._lbl_arr_from_unit  = _le_inline()
        self._le_arr_to,    self._lbl_arr_to_unit    = _le_inline()
        self._le_arr_step,  self._lbl_arr_step_unit  = _le_inline()
        arr_row.addWidget(self._le_arr_from)
        arr_row.addWidget(self._lbl_arr_from_unit)
        arr_row.addWidget(QLabel("To:"))
        arr_row.addWidget(self._le_arr_to)
        arr_row.addWidget(self._lbl_arr_to_unit)
        arr_row.addWidget(QLabel("Step:"))
        arr_row.addWidget(self._le_arr_step)
        arr_row.addWidget(self._lbl_arr_step_unit)
        self._lbl_n_points = QLabel("→ — pts")
        self._lbl_n_points.setStyleSheet("color: #888888;")
        arr_row.addWidget(self._lbl_n_points)
        arr_layout.addLayout(arr_row)

        self._cb_to_zero_at_last = QCheckBox("to 0 at last step")
        self._cb_to_zero_at_last.setFont(_MONO)
        self._cb_to_zero_at_last.setToolTip(
            "마지막 array step 완료 후 second channel을 0으로 전송합니다.\n"
            "SWEEP type: advance type 그대로 0까지 sweep\n"
            "기타 type: VISA write 명령어로 즉시 0 전송"
        )
        arr_layout.addWidget(self._cb_to_zero_at_last)
        self._arr_frame = arr_frame
        outer.addWidget(arr_frame)

        # Connect array inputs to point count update
        for le in (self._le_arr_from, self._le_arr_to, self._le_arr_step):
            le.textChanged.connect(self._update_n_points)

        # Estimated time + ETA row
        est_row = QHBoxLayout()
        est_row.addWidget(QLabel("Estimated:"))
        self._lbl_est_time = QLabel("—")
        self._lbl_est_time.setFont(_MONO)
        self._lbl_est_time.setStyleSheet("color: #79c0ff; font-weight: bold;")
        est_row.addWidget(self._lbl_est_time)
        est_row.addSpacing(10)
        eta_lbl = QLabel("종료 예상:")
        eta_lbl.setStyleSheet("color: #888888;")
        est_row.addWidget(eta_lbl)
        self._lbl_eta = QLabel("—")
        self._lbl_eta.setFont(_MONO)
        self._lbl_eta.setStyleSheet("color: #56d364; font-weight: bold;")
        est_row.addWidget(self._lbl_eta)
        est_row.addStretch()
        outer.addLayout(est_row)

        # Connect all params that affect the estimate
        for le in (self._le_start, self._le_stop,
                   self._le_rate_t, self._le_rate_r, self._le_rate_d,
                   self._le_tpp,
                   self._le_arr_from, self._le_arr_to, self._le_arr_step,
                   self._le_second_rate):
            le.textChanged.connect(self._update_est_time)
        self._cb_retrace_to_zero.stateChanged.connect(self._update_est_time)
        self._cb_to_zero_at_last.stateChanged.connect(self._update_est_time)

        # Status row
        status_frame = QFrame()
        status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(8, 4, 8, 4)

        self._lbl_phase = QLabel("IDLE")
        self._lbl_phase.setFont(_MONO)
        self._lbl_phase.setStyleSheet("color: #888888;")
        status_layout.addWidget(QLabel("Phase:"))
        status_layout.addWidget(self._lbl_phase)

        status_layout.addSpacing(12)
        status_layout.addWidget(QLabel("Second:"))
        self._lbl_second_val = QLabel("—")
        self._lbl_second_val.setFont(_MONO)
        self._lbl_second_val.setStyleSheet("color: #f78166;")
        status_layout.addWidget(self._lbl_second_val)

        status_layout.addSpacing(12)
        status_layout.addWidget(QLabel("Array:"))
        self._lbl_array_progress = QLabel("—/—")
        self._lbl_array_progress.setFont(_MONO)
        self._lbl_array_progress.setStyleSheet("color: #79c0ff;")
        status_layout.addWidget(self._lbl_array_progress)
        status_layout.addStretch()

        status_layout.addSpacing(12)
        status_layout.addWidget(QLabel("Alarm:"))
        self._lbl_last_alarm = QLabel("(없음)")
        self._lbl_last_alarm.setFont(_MONO)
        self._lbl_last_alarm.setStyleSheet("color: #888888; font-size: 10px;")
        self._lbl_last_alarm.setMaximumWidth(200)
        status_layout.addWidget(self._lbl_last_alarm)

        outer.addWidget(status_frame)

        # Start / Stop
        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("Start Double Sweep")
        self._btn_start.setMinimumHeight(40)
        self._btn_start.setStyleSheet(
            "QPushButton { font-weight: bold; font-size: 13px;"
            "background-color: #0d6830; color: white; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #0a2b1a; color: #2d4d35; border-radius: 4px; }"
        )
        self._btn_start.clicked.connect(self._on_start)

        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setMinimumHeight(40)
        self._btn_stop.setStyleSheet(
            "QPushButton { font-weight: bold; font-size: 13px;"
            "background-color: #c62828; color: white; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #3a1a1a; color: #553d3d; border-radius: 4px; }"
        )
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)

        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_stop)
        outer.addLayout(btn_row)

        # Save path preview (trace 폴더 기준)
        save_path_row = QHBoxLayout()
        save_path_lbl = QLabel("Trace →")
        save_path_lbl.setFont(_MONO)
        save_path_lbl.setStyleSheet("color: #888888;")
        save_path_row.addWidget(save_path_lbl)
        self._lbl_ds_save_path = QLabel("—")
        self._lbl_ds_save_path.setFont(QFont("Consolas", 8))
        self._lbl_ds_save_path.setStyleSheet("color: #555555;")
        self._lbl_ds_save_path.setWordWrap(True)
        save_path_row.addWidget(self._lbl_ds_save_path, stretch=1)
        btn_open_ds = QPushButton("📂")
        btn_open_ds.setFixedWidth(28)
        btn_open_ds.setFixedHeight(20)
        btn_open_ds.setFont(_MONO)
        btn_open_ds.setToolTip("저장 폴더 열기")
        btn_open_ds.clicked.connect(self._open_ds_folder)
        save_path_row.addWidget(btn_open_ds)
        outer.addLayout(save_path_row)
        outer.addStretch()

    # ------------------------------------------------------------------
    # Second channel radio buttons
    # ------------------------------------------------------------------

    def _rebuild_second_channel_radios(self):
        for btn in self._second_radio_group.buttons():
            self._second_radio_group.removeButton(btn)
        while self._second_ch_layout.count():
            item = self._second_ch_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        channels = self._param_reg.main_ui_profile.second_sweep_channels
        if not channels:
            lbl = QLabel("(Parameter Manager에서 Second Sweep Channel을 등록하세요)")
            lbl.setStyleSheet("color: #555555; font-size: 11px;")
            self._second_ch_layout.addWidget(lbl)
            self._second_channel = None
            self._sweep_ch_frame.setVisible(False)
            return

        for idx, ch in enumerate(channels):
            rb = QRadioButton(f"[{ch.alias}]  {ch.description}  ({ch.unit})  — {ch.advance_type.value}")
            rb.setFont(_MONO)
            rb.setStyleSheet("color: #f78166;")
            self._second_radio_group.addButton(rb, idx)
            self._second_ch_layout.addWidget(rb)

        cfg_idx = min(self._param_reg.double_sweep_config.selected_channel_idx, len(channels) - 1)
        btn = self._second_radio_group.button(cfg_idx)
        if btn:
            btn.setChecked(True)
        else:
            self._second_radio_group.buttons()[0].setChecked(True)

    def _on_second_radio_toggled(self, btn_id: int, checked: bool):
        if not checked:
            return
        channels = self._param_reg.main_ui_profile.second_sweep_channels
        if 0 <= btn_id < len(channels):
            self._second_channel = channels[btn_id]
            self._sweep_ch_frame.setVisible(
                self._second_channel.advance_type == SecondSweepAdvanceType.SWEEP
            )
            self._update_unit_labels()

    # ------------------------------------------------------------------
    # Config load / save
    # ------------------------------------------------------------------

    def _load_config(self):
        cfg = self._param_reg.double_sweep_config
        self._le_start.setText(f"{cfg.start_point:g}")
        self._le_stop.setText(f"{cfg.stop_point:g}")
        self._le_rate_t.setText(f"{cfg.rate_trace:g}")
        self._le_rate_r.setText(f"{cfg.rate_retrace:g}")
        self._le_rate_d.setText(f"{cfg.rate_dummy:g}")
        self._le_tpp.setText(f"{cfg.time_per_point:g}")
        self._le_arr_from.setText(f"{cfg.array_from:g}")
        self._le_arr_to.setText(f"{cfg.array_to:g}")
        self._le_arr_step.setText(f"{cfg.array_step:g}")
        self._cb_retrace_to_zero.setChecked(cfg.retrace_to_zero)
        self._cb_to_zero_at_last.setChecked(cfg.to_zero_at_last)
        self._le_second_rate.setText(f"{cfg.second_sweep_rate:g}")
        self._cb_second_safety.setChecked(cfg.second_use_safety)
        self._sb_second_steps.setValue(cfg.second_safety_steps)
        self._le_second_interval.setText(f"{cfg.second_safety_interval_ms:g}")
        self._update_n_points()
        self._update_est_time()
        # AlarmPanel: refresh combo from alarm_measurements, then load saved config
        alarm_meas = self._param_reg.main_ui_profile.alarm_measurements
        self._alarm_panel.refresh_measurements(alarm_meas)
        self._alarm_panel.load_config(cfg.alarm)
        self._telegram_panel.load_config(cfg.alarm)

    def _build_alarm_config(self) -> AlarmConfig:
        from config.config_models import TelegramContact
        _TG_KEYS = {"use_telegram", "telegram_bot_token", "telegram_chat_id", "telegram_contacts"}
        base = {k: v for k, v in self._alarm_panel.get_config().model_dump().items()
                if k not in _TG_KEYS}
        tg = self._telegram_panel.get_config()
        contacts = [TelegramContact(**c) for c in tg.pop("telegram_contacts", [])]
        return AlarmConfig(**base, **tg, telegram_contacts=contacts)

    def _save_config(self):
        cfg = DoubleSweepConfig(
            start_point=self._parse_ds_float(self._le_start.text(), 0.0),
            stop_point=self._parse_ds_float(self._le_stop.text(), 1.0),
            rate_trace=self._parse_ds_float(self._le_rate_t.text(), 1.0),
            rate_retrace=self._parse_ds_float(self._le_rate_r.text(), 1.0),
            rate_dummy=self._parse_ds_float(self._le_rate_d.text(), 1.0),
            time_per_point=self._parse_ds_float(self._le_tpp.text(), 1.0),
            array_from=self._parse_ds_float(self._le_arr_from.text(), 0.0),
            array_to=self._parse_ds_float(self._le_arr_to.text(), 1.0),
            array_step=self._parse_ds_float(self._le_arr_step.text(), 0.1),
            selected_channel_idx=max(0, self._second_radio_group.checkedId()),
            retrace_to_zero=self._cb_retrace_to_zero.isChecked(),
            to_zero_at_last=self._cb_to_zero_at_last.isChecked(),
            second_sweep_rate=self._parse_ds_float(self._le_second_rate.text(), 1.0),
            second_use_safety=self._cb_second_safety.isChecked(),
            second_safety_steps=self._sb_second_steps.value(),
            second_safety_interval_ms=self._parse_ds_float(self._le_second_interval.text(), 0.0),
            alarm=self._build_alarm_config(),
        )
        self._param_reg.save_double_sweep_config(cfg)

    def _current_cfg(self) -> DoubleSweepConfig:
        return DoubleSweepConfig(
            start_point=self._parse_ds_float(self._le_start.text(), 0.0),
            stop_point=self._parse_ds_float(self._le_stop.text(), 1.0),
            rate_trace=self._parse_ds_float(self._le_rate_t.text(), 1.0),
            rate_retrace=self._parse_ds_float(self._le_rate_r.text(), 1.0),
            rate_dummy=self._parse_ds_float(self._le_rate_d.text(), 1.0),
            time_per_point=self._parse_ds_float(self._le_tpp.text(), 1.0),
            array_from=self._parse_ds_float(self._le_arr_from.text(), 0.0),
            array_to=self._parse_ds_float(self._le_arr_to.text(), 1.0),
            array_step=self._parse_ds_float(self._le_arr_step.text(), 0.1),
            retrace_to_zero=self._cb_retrace_to_zero.isChecked(),
            to_zero_at_last=self._cb_to_zero_at_last.isChecked(),
        )

    @staticmethod
    def _parse_ds_float(text: str, default: float = 0.0) -> float:
        try:
            return float(text.strip())
        except (ValueError, AttributeError):
            return default

    def _get_first_channel_unit(self) -> str:
        try:
            profile = self._main_win._active_profile
            idx = profile.active_sweep_channel_idx
            if 0 <= idx < len(profile.main_ui.sweep_values):
                return profile.main_ui.sweep_values[idx].unit
        except Exception:
            pass
        return ""

    def _update_unit_labels(self):
        """Refresh unit labels based on current first/second channel selections."""
        u1 = self._get_first_channel_unit()
        self._lbl_start_unit.setText(u1)
        self._lbl_stop_unit.setText(u1)
        rate_u1 = f"{u1}/min" if u1 else "units/min"
        self._lbl_rate_t_unit.setText(rate_u1)
        self._lbl_rate_r_unit.setText(rate_u1)
        self._lbl_rate_d_unit.setText(rate_u1)

        u2 = self._second_channel.unit if self._second_channel else ""
        self._lbl_arr_from_unit.setText(u2)
        self._lbl_arr_to_unit.setText(u2)
        self._lbl_arr_step_unit.setText(u2)
        rate_u2 = f"{u2}/min" if u2 else "units/min"
        self._lbl_second_rate_unit.setText(rate_u2)

    def _update_n_points(self):
        cfg = self._current_cfg()
        arr = _generate_array(cfg)
        self._lbl_n_points.setText(f"→ {len(arr)} pts")

    def _update_est_time(self):
        cfg = self._current_cfg()
        arr = _generate_array(cfg)
        second_ch = None
        if self._second_channel is not None:
            second_ch = self._make_effective_second_channel()
        sec = _estimate_total_seconds(cfg, len(arr), second_ch)
        self._lbl_est_time.setText(_fmt_hms(sec))
        eta = _datetime.now() + _timedelta(seconds=sec)
        # 24h 이내면 HH:MM, 그 이상이면 날짜 포함
        if sec < 86400:
            self._lbl_eta.setText(eta.strftime("%H:%M"))
        else:
            self._lbl_eta.setText(eta.strftime("%m/%d %H:%M"))

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def lock_ui(self, locked: bool) -> None:
        """측정 중 설정 UI 잠금/해제 (Start/Stop 버튼 제외)."""
        self._ch_frame.setEnabled(not locked)
        self._sp_frame.setEnabled(not locked)
        self._arr_frame.setEnabled(not locked)
        self._sweep_ch_frame.setEnabled(not locked)
        self._right_alarm_scroll.setEnabled(not locked)
        self._right_tg_scroll.setEnabled(not locked)

    def _unlock_main_ui(self) -> None:
        """Double Sweep 종료 시 Main Window UI 복원."""
        self.lock_ui(False)
        mw = self._main_win
        mw._btn_start.setEnabled(True)
        mw._sweep_channel_panel.setEnabled(True)
        mw._meas_panel.setEnabled(True)
        mw._save_settings_frame.setEnabled(True)
        for _sfx in ("", "2", "3"):
            cb = getattr(mw, f"_cb_deriv{_sfx}_enable")
            cb.setEnabled(True)
            for _w in getattr(mw, f"_deriv{_sfx}_setting_widgets"):
                _w.setEnabled(cb.isChecked())
        if mw._meta_data_window is not None:
            mw._meta_data_window.lock_ui(False)

    def _on_start(self):
        if self._phase != DoubleSweepPhase.IDLE:
            return
        if self._main_win._running:
            QMessageBox.warning(self, "Sweep 실행 중",
                "Main sweep이 실행 중입니다. 먼저 중단하세요.")
            return
        if self._main_win._sweep_channel is None:
            QMessageBox.warning(self, "No Sweep Channel",
                "Main UI에서 Sweep Channel을 선택하세요.")
            return
        if self._second_channel is None:
            QMessageBox.warning(self, "No Second Channel",
                "Second Sweep Channel을 선택하세요.\n"
                "Parameter Manager에서 Second Sweep Channel을 등록하세요.")
            return

        # 연결 상태 확인 (second channel 포함) — 실패 시 측정 중단
        if not self._main_win._run_connection_test(include_second=True, show_success=False):
            return

        self._save_config()
        self._cfg = self._current_cfg()
        self._array = _generate_array(self._cfg)
        if not self._array:
            QMessageBox.warning(self, "Array 오류", "Array 생성 실패: 포인트 수가 0입니다.")
            return

        self._array_idx = 0
        self._last_write_value = None
        self._last_meas_values = {}
        self._trace_filepath = None
        # Reconfigure derivative channels from current UI settings (single sweep과 동일하게)
        mw = self._main_win
        for order, ch in [(1, mw._deriv_channel), (2, mw._deriv_channel2), (3, mw._deriv_channel3)]:
            ch.reconfigure(mw._build_deriv_config(order))
            ch.reset()
        self._ctx = self._build_context()
        self._lbl_last_alarm.setText("(없음)")
        self._lbl_last_alarm.setStyleSheet("color: #888888; font-size: 10px;")

        # MetaDataManager: T/B 버퍼 설정 (sweep 전체에서 공유)
        _meas_labels = [lbl for lbl, _ in self._ctx.meas_cols]
        self._main_win._meta_manager.configure(
            self._ctx.active_meas_indices,
            self._main_win._active_profile.measurements,
            _meas_labels,
        )

        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._glow_phase = 0.0
        self._glow_timer.start()
        self.sweep_started.emit()
        # Lock own settings UI and main window panels
        self.lock_ui(True)
        mw = self._main_win
        mw._btn_start.setEnabled(False)
        mw._sweep_channel_panel.setEnabled(False)
        mw._meas_panel.setEnabled(False)
        mw._save_settings_frame.setEnabled(False)
        for _sfx in ("", "2", "3"):
            getattr(mw, f"_cb_deriv{_sfx}_enable").setEnabled(False)
            for _w in getattr(mw, f"_deriv{_sfx}_setting_widgets"):
                _w.setEnabled(False)
        if mw._meta_data_window is not None:
            mw._meta_data_window.lock_ui(True)

        # DataWindow 컬럼 동기화 (double sweep 측정값을 main DataWindow에 표시)
        ctx = self._ctx
        dw_cols = [(ctx.sweep_col[0], ctx.sweep_col[1])]
        for (fig_ax, unit) in ctx.meas_cols:
            dw_cols.append((fig_ax, unit))
        self._main_win._data_window.configure_columns(dw_cols)
        self._main_win._data_window.clear_values()

        # Graph window에 2D map base path 전달
        if self._main_win._graph_window is not None:
            mw = self._main_win
            base = mw._le_main_folder.text().strip()
            sub = mw._le_custom_folder.text().strip()
            if base:
                map_base = f"{base}/{sub}" if sub else base
                self._main_win._graph_window.update_map_base(map_base)

        # Graph: begin fresh session with the same columns as the sweep context
        if self._main_win._graph_window is not None:
            try:
                cols = [("__sweep__", self._ctx.sweep_col[0], self._ctx.sweep_col[1])]
                for (row, alias, desc, cmd), (fig_ax, unit) in zip(
                    self._ctx.active_measurements, self._ctx.meas_cols
                ):
                    cols.append((desc, fig_ax, unit))
                for ch in (
                    self._main_win._deriv_channel,
                    self._main_win._deriv_channel2,
                    self._main_win._deriv_channel3,
                ):
                    if ch._cfg.enabled:
                        cols.append(ch.col_info())
                self._main_win._graph_window.begin_session(cols)
            except Exception as _e:
                self._main_win._log(f"  [Graph] begin_session failed: {_e}", color="#f44747")

        # Reset derivative sliding windows for a clean new sweep
        for ch in (self._main_win._deriv_channel, self._main_win._deriv_channel2, self._main_win._deriv_channel3):
            ch.reset()

        self._main_win._log("Double Sweep started. Pre-positioning first channel → start_point.", color="#4ec9b0")
        self._start_pre_init()

    def _on_stop(self):
        if self._phase == DoubleSweepPhase.IDLE:
            return
        self._sweep_timer.stop()
        self._sweep_worker.request_stop()
        self._second_worker.request_stop()
        self._set_phase(DoubleSweepPhase.IDLE)
        self._lbl_second_val.setText("—")
        self._lbl_array_progress.setText("—/—")
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._stop_glow()
        self.sweep_finished.emit()
        self._main_win._log("Double Sweep stopped.", color="#ce9178")
        self._unlock_main_ui()

    def _finish(self):
        # 모든 측정 완료 알람
        self._check_alarm(is_sweep_complete=True)
        self._set_phase(DoubleSweepPhase.IDLE)
        self._lbl_second_val.setText("—")
        self._lbl_array_progress.setText(f"{len(self._array)}/{len(self._array)}")
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._stop_glow()
        self.sweep_finished.emit()
        self._main_win._log(
            f"Double Sweep complete. ({len(self._array)} array points)", color="#4ec9b0"
        )
        self._unlock_main_ui()

    # ------------------------------------------------------------------
    # Context snapshot
    # ------------------------------------------------------------------

    def _build_context(self) -> DoubleSweepContext:
        """Double Sweep 시작 시 MainWindow 상태를 스냅샷합니다."""
        mw = self._main_win
        profile = mw._active_profile

        # Active measurements
        active_meas_indices = [
            i for i, (_, cb) in enumerate(zip(profile.measurements, mw._meas_checkboxes))
            if cb.isChecked()
        ]
        active_measurements = [
            (row, m.alias, m.description, m.resolved_cmd)
            for row, m in enumerate(profile.measurements)
            if row in active_meas_indices
        ]
        meas_cols = [
            (mw._meas_label_for(row, m), m.unit)
            for row, m in enumerate(profile.measurements)
            if row in active_meas_indices
        ]

        # First channel sweep column header + safety params
        sv_id = mw._sweep_radio_group.checkedId()
        sv_list = profile.sweep_values
        sv = sv_list[sv_id] if 0 <= sv_id < len(sv_list) else None
        if sv_id == mw._TIME_ID:
            sweep_col: Tuple[str, str] = ("time", "")
        elif sv:
            sweep_col = (sv.figure_axis or "target", sv.unit)
        else:
            sweep_col = ("target", "")

        return DoubleSweepContext(
            sweep_channel=mw._sweep_channel,
            sv_safety_steps=sv.safety_steps if sv else 0,
            sv_safety_interval_ms=sv.safety_interval_ms if sv else 0.0,
            active_meas_indices=active_meas_indices,
            active_measurements=active_measurements,
            sweep_col=sweep_col,
            meas_cols=meas_cols,
            main_folder=mw._le_main_folder.text(),
            custom_folder=mw._le_custom_folder.text(),
            custom_word=mw._le_custom_word.text(),
            include_date=mw._cb_save_date.isChecked(),
            save_enabled=mw._cb_save_enable.isChecked(),
        )

    # ------------------------------------------------------------------
    # Phase management
    # ------------------------------------------------------------------

    def _set_phase(self, phase: DoubleSweepPhase):
        self._phase = phase
        self._lbl_phase.setText(phase.name)

    def _start_pre_init(self):
        """First channel을 start_point로 이동 (데이터 저장 없음)."""
        self._set_phase(DoubleSweepPhase.PRE_INIT)
        self._sweep_timer.start(0)

    def _make_effective_second_channel(self) -> InstantiatedSecondSweepChannel:
        """Return second channel with UI-overridden SWEEP params when advance_type is SWEEP."""
        ch = self._second_channel
        if ch.advance_type != SecondSweepAdvanceType.SWEEP:
            return ch
        use_safety = self._cb_second_safety.isChecked()
        return ch.model_copy(update={
            "sweep_rate": self._parse_ds_float(self._le_second_rate.text(), 1.0),
            "safety_steps": self._sb_second_steps.value() if use_safety else 0,
            "safety_interval_ms": self._parse_ds_float(self._le_second_interval.text(), 0.0) if use_safety else 0.0,
        })

    def _advance_second(self, next_val: float, prev: Optional[float]):
        ch = self._make_effective_second_channel()
        self._lbl_second_val.setText(f"{next_val:.4g} {ch.unit}")
        self._lbl_array_progress.setText(f"{self._array_idx + 1}/{len(self._array)}")
        self._set_phase(DoubleSweepPhase.ADVANCING_SECOND)

        # 새 step 시작 — T/B 버퍼 초기화
        self._trace_filepath = None
        self._main_win._meta_manager.clear()

        # Clear graph and reset derivative buffers when advancing to next array step
        if prev is not None:
            for _dc in (self._main_win._deriv_channel, self._main_win._deriv_channel2, self._main_win._deriv_channel3):
                _dc.reset()
            if self._main_win._graph_window is not None:
                try:
                    cols = [("__sweep__", self._ctx.sweep_col[0], self._ctx.sweep_col[1])]
                    for (row, alias, desc, cmd), (fig_ax, unit) in zip(
                        self._ctx.active_measurements, self._ctx.meas_cols
                    ):
                        cols.append((desc, fig_ax, unit))
                    for deriv_ch in (self._main_win._deriv_channel, self._main_win._deriv_channel2, self._main_win._deriv_channel3):
                        if deriv_ch._cfg.enabled:
                            cols.append(deriv_ch.col_info())
                    self._main_win._graph_window.begin_session(cols)
                except Exception as _e:
                    self._main_win._log(f"  [Graph] clear failed: {_e}", color="#f44747")

        self.request_advance.emit(SecondChannelRequest(
            channel=ch,
            next_value=next_val,
            prev_value=prev,
            time_per_point=self._cfg.time_per_point,
        ))

    def _return_second_to_zero(self):
        """모든 array step 완료 후 second channel을 0으로 전송."""
        ch = self._make_effective_second_channel()
        last_val = self._array[-1] if self._array else None
        self._set_phase(DoubleSweepPhase.RETURNING_ZERO)
        self._lbl_phase.setText("RETURNING 0")

        if ch.advance_type == SecondSweepAdvanceType.SWEEP:
            # SWEEP type: use full advance worker so safety ramp / rate are respected
            self.request_advance.emit(SecondChannelRequest(
                channel=ch,
                next_value=0.0,
                prev_value=last_val,
                time_per_point=self._cfg.time_per_point,
            ))
        else:
            # Other types: single VISA write to 0, then finish immediately
            ch_simple = ch.model_copy(update={"advance_type": SecondSweepAdvanceType.SIMPLE_HOP})
            self.request_advance.emit(SecondChannelRequest(
                channel=ch_simple,
                next_value=0.0,
                prev_value=last_val,
                time_per_point=self._cfg.time_per_point,
            ))

    @Slot()
    def _on_advance_done(self):
        if self._phase == DoubleSweepPhase.RETURNING_ZERO:
            self._finish()
            return
        if self._phase != DoubleSweepPhase.ADVANCING_SECOND:
            return
        self._start_sweep_phase(DoubleSweepPhase.DUMMY)

    @Slot(str)
    def _on_advance_error(self, msg: str):
        self._main_win._log(f"  [DoubleSweep] Second channel error: {msg}", color="#f44747")
        self._check_alarm(is_comm_error=True, extra_reason=f"Second channel error: {msg}")
        self._on_stop()

    def _start_sweep_phase(self, phase: DoubleSweepPhase):
        self._set_phase(phase)
        self._setup_datasaver_for_phase(phase.name.lower())
        # 각 페이즈 시작 시 초기 상태 측정 (이동 없이 현재 위치에서 measurement만)
        ctx = self._ctx
        cfg = self._cfg
        self.request_step.emit(StepRequest(
            sweep_channel=ctx.sweep_channel,
            sweep_to=0.0,
            sweep_rate=1.0,
            time_per_point=cfg.time_per_point,
            t_emit=time.perf_counter(),
            last_write_value=self._last_write_value,
            active_measurements=ctx.active_measurements,
            measure_only=True,
        ))

    def _advance_phase(self):
        if self._phase == DoubleSweepPhase.DUMMY:
            self._start_sweep_phase(DoubleSweepPhase.TRACE)
        elif self._phase == DoubleSweepPhase.TRACE:
            self._start_sweep_phase(DoubleSweepPhase.RETRACE)
        elif self._phase == DoubleSweepPhase.RETRACE:
            # ── RETRACE 완료: 알람 체크 → 메타 데이터 저장 → 다음 step ──
            self._check_alarm(is_comm_error=False)
            # 메타 데이터 JSON 저장 (TRACE 파일과 같은 이름, 확장자 .json)
            self._main_win._meta_manager.save(
                self._main_win._param_manager_reg.meta_data_config,
                self._trace_filepath,
            )
            self._array_idx += 1
            if self._array_idx < len(self._array):
                self._advance_second(
                    self._array[self._array_idx],
                    prev=self._array[self._array_idx - 1],
                )
            else:
                if self._cfg.to_zero_at_last and self._second_channel is not None:
                    self._return_second_to_zero()
                else:
                    self._finish()

    # ------------------------------------------------------------------
    # DataSaver
    # ------------------------------------------------------------------

    def _setup_datasaver_for_phase(self, phase_name: str):
        from datetime import date as _date
        ctx = self._ctx
        base = ctx.custom_folder
        phase_subfolder = f"{base}/{phase_name}" if base else phase_name

        # Double sweep 파일명: X{N:03d} 대신 _{fig_axis}_{step_value} 사용
        ch = self._second_channel
        step_val = self._array[self._array_idx] if self._array_idx < len(self._array) else 0.0
        fig_ax = (ch.figure_axis or "").strip()
        val_str = f"{step_val:.6g}"
        id_str = f"_{fig_ax}_{val_str}" if fig_ax else f"_{val_str}"
        # stem = custom_word + date (동일한 DataSaver 규칙 유지)
        stem_parts = []
        if ctx.custom_word.strip():
            stem_parts.append(ctx.custom_word.strip())
        if ctx.include_date:
            stem_parts.append(_date.today().strftime("%Y%m%d"))
        fixed_name = "".join(stem_parts) + id_str + ".dat"

        self._data_saver.set_main_folder(ctx.main_folder)
        self._data_saver.set_custom_folder(phase_subfolder)
        self._data_saver.set_custom_word(ctx.custom_word)
        self._data_saver.set_include_date(ctx.include_date)
        self._data_saver.set_enabled(ctx.save_enabled)
        self._data_saver.set_fixed_name(fixed_name)
        deriv_cols = [
            (ch.col_info()[1], ch.col_info()[2])
            for ch in (self._main_win._deriv_channel, self._main_win._deriv_channel2, self._main_win._deriv_channel3)
            if ch._cfg.enabled
        ]
        self._data_saver.set_columns([ctx.sweep_col] + ctx.meas_cols + deriv_cols)
        filepath = self._data_saver.start_session()
        if filepath:
            self._main_win._log(f"  [{phase_name}] Data → {filepath}", color="#888888")
        # TRACE 파일 경로를 메타 데이터 JSON 저장에 사용
        if phase_name == "trace":
            self._trace_filepath = self._data_saver.get_filepath()
        self._update_ds_save_path()

    # ------------------------------------------------------------------
    # Sweep tick (same pattern as main_window)
    # ------------------------------------------------------------------

    def _sweep_tick(self):
        if self._phase not in (DoubleSweepPhase.PRE_INIT,
                               DoubleSweepPhase.DUMMY,
                               DoubleSweepPhase.TRACE,
                               DoubleSweepPhase.RETRACE):
            return

        cfg = self._cfg
        ctx = self._ctx

        if self._phase == DoubleSweepPhase.PRE_INIT:
            sweep_to, sweep_rate = cfg.start_point, cfg.rate_dummy
            active_meas = []                        # 데이터 수집 없음
        elif self._phase == DoubleSweepPhase.DUMMY:
            sweep_to, sweep_rate = cfg.start_point, cfg.rate_dummy
            active_meas = ctx.active_measurements
        elif self._phase == DoubleSweepPhase.TRACE:
            sweep_to, sweep_rate = cfg.stop_point, cfg.rate_trace
            active_meas = ctx.active_measurements
        else:  # RETRACE
            sweep_to = 0.0 if cfg.retrace_to_zero else cfg.start_point
            sweep_rate = cfg.rate_retrace
            active_meas = ctx.active_measurements

        t_emit = time.perf_counter()
        self.request_step.emit(StepRequest(
            sweep_channel=ctx.sweep_channel,
            sweep_to=sweep_to,
            sweep_rate=sweep_rate,
            time_per_point=cfg.time_per_point,
            t_emit=t_emit,
            last_write_value=self._last_write_value,
            safety_steps=ctx.sv_safety_steps,
            safety_interval_ms=ctx.sv_safety_interval_ms,
            active_measurements=active_meas,
        ))

    @Slot(object)
    def _on_step_done(self, result: StepResult):
        if self._phase == DoubleSweepPhase.PRE_INIT:
            self._last_write_value = result.next_v
            if result.is_done:
                self._main_win._log("  First channel at start_point. Advancing second channel.", color="#4ec9b0")
                self._advance_second(self._array[0], prev=None)
            else:
                # dummy phase와 동일한 time_per_point 간격으로 진행
                t_before_timer = time.perf_counter()
                elapsed_ms = int((t_before_timer - result.timing.t_emit) * 1000)
                interval_ms = max(0, int(self._cfg.time_per_point * 1000) - elapsed_ms)
                self._sweep_timer.start(interval_ms)
            return

        if self._phase not in (DoubleSweepPhase.DUMMY,
                               DoubleSweepPhase.TRACE,
                               DoubleSweepPhase.RETRACE):
            return

        # is_done=True without measurements → already at target, advance phase
        if result.is_done and not result.meas_results:
            self._advance_phase()
            return

        t_recv = time.perf_counter()

        # Append to data saver
        meas_map = {row: val for row, val in result.meas_results}
        row_vals = [f"{result.next_v:.6g}"]
        has_err = False
        err_descs = []
        for idx in self._ctx.active_meas_indices:
            val = meas_map.get(idx)
            if val is None:
                has_err = True
                desc = ""
                for _row, _alias, _desc, _cmd in self._ctx.active_measurements:
                    if _row == idx:
                        desc = _desc
                        break
                detail = result.meas_errors.get(idx, "")
                err_descs.append(f"{desc}: {detail}" if detail else desc)
            row_vals.append(f"{val:.6g}" if val is not None else "ERR")

        if has_err:
            err_msg = (
                f"✗ ERR @ phase={self._phase.name}\n"
                f"실패 채널: {', '.join(err_descs)}"
            )
            self._main_win._log(f"  [DoubleSweep] {err_msg}", color="#f44747")
            self._check_alarm(
                is_meas_error=True,
                extra_reason=f"측정값 ERR — {', '.join(err_descs)}",
            )
            self._on_stop()
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Measurement Error", err_msg)
            return
        mw = self._main_win
        for ch, fn in [
            (mw._deriv_channel,  mw._deriv_val_for_step),
            (mw._deriv_channel2, mw._deriv_val_for_step2),
            (mw._deriv_channel3, mw._deriv_val_for_step3),
        ]:
            if ch._cfg.enabled:
                dv = fn(result, meas_map)
                row_vals.append(f"{dv:.6g}" if dv is not None else "—")
        self._data_saver.append_row(row_vals)

        # MetaDataManager: T/B 버퍼 누적
        self._main_win._meta_manager.record_step(result.meas_results)

        # DataWindow 갱신 (main_window의 데이터창에 현재 측정값 표시)
        self._main_win._data_window.update_values(row_vals)

        # Graph update (includes derivatives if enabled in main window)
        if self._main_win._graph_window is not None:
            try:
                from gui.graph_window import GraphDataPoint
                from core.derivative_channel import OUTPUT_KEY as _DERIV_KEY, OUTPUT_KEY_2 as _DERIV2_KEY, OUTPUT_KEY_3 as _DERIV3_KEY
                _phase_str = {
                    DoubleSweepPhase.DUMMY:   "dummy",
                    DoubleSweepPhase.TRACE:   "trace",
                    DoubleSweepPhase.RETRACE: "retrace",
                }.get(self._phase, "")
                gvals = {"__sweep__": result.next_v}
                # Use context snapshot to avoid stale profile reference
                for row, alias, desc, cmd in self._ctx.active_measurements:
                    val = meas_map.get(row)
                    if val is not None:
                        gvals[desc] = val
                # Derivatives from shared channels (uses main_win's deriv_channel instances)
                mw = self._main_win
                for ch, key, fn in [
                    (mw._deriv_channel,  _DERIV_KEY,  mw._deriv_val_for_step),
                    (mw._deriv_channel2, _DERIV2_KEY, mw._deriv_val_for_step2),
                    (mw._deriv_channel3, _DERIV3_KEY, mw._deriv_val_for_step3),
                ]:
                    if ch._cfg.enabled:
                        dv = fn(result, meas_map)
                        gvals[key] = dv if dv is not None else float("nan")
                self._main_win._graph_window.append_point(GraphDataPoint(values=gvals, phase=_phase_str))
            except Exception as _e:
                self._main_win._log(f"  [Graph] append_point failed: {_e}", color="#f44747")

        self._last_write_value = result.next_v

        # 최신 측정값 캐시 갱신 (알람 트리거 평가용) — meas_map은 위에서 이미 계산됨
        for row, alias, desc, cmd in self._ctx.active_measurements:
            val = meas_map.get(row)
            if val is not None:
                self._last_meas_values[desc] = val

        # TimingWindow 갱신
        t_ui_done = time.perf_counter()
        self._main_win._timing_window.update_timing(result.timing, t_recv, t_ui_done)

        if result.is_done:
            self._advance_phase()
        elif result.measure_only:
            # 초기 상태 측정 완료 → 즉시 sweep 타이머 시작
            self._sweep_timer.start(0)
        else:
            t_before_timer = time.perf_counter()
            elapsed_ms = int((t_before_timer - result.timing.t_emit) * 1000)
            interval_ms = max(0, int(self._cfg.time_per_point * 1000) - elapsed_ms)
            self._sweep_timer.start(interval_ms)

    @Slot(str)
    def _on_step_error(self, msg: str):
        self._main_win._log(f"  [DoubleSweep] Step error: {msg}", color="#f44747")
        self._check_alarm(is_comm_error=True, extra_reason=f"Step error: {msg}")
        self._on_stop()

    # ------------------------------------------------------------------
    # Alarm
    # ------------------------------------------------------------------

    def _check_alarm(self, *, is_comm_error: bool = False, is_meas_error: bool = False,
                     is_sweep_complete: bool = False, extra_reason: str = ""):
        """
        알람 조건 평가 및 발동.

        is_comm_error=True    : 통신 오류/타임아웃 발생 시
        is_meas_error=True    : 측정 채널 ERR (None 반환) 발생 시
        is_sweep_complete=True: 모든 array 포인트 완료 시
        그 외 (기본)           : RETRACE 완료 후 measurement 트리거 체크
        """
        alarm_cfg = self._param_reg.double_sweep_config.alarm
        if not alarm_cfg.enabled:
            return

        reasons = []

        if is_sweep_complete:
            if alarm_cfg.fire_on_complete:
                reasons.append("Double Sweep 완료")
        elif is_comm_error:
            if self._alarm_manager.has_comm_error_trigger(alarm_cfg.triggers):
                reasons.append(extra_reason or "통신 오류 / Timeout")
        elif is_meas_error:
            if self._alarm_manager.has_meas_error_trigger(alarm_cfg.triggers):
                reasons.append(extra_reason or "측정값 ERR — 채널 실패")
        else:
            fired = self._alarm_manager.check_measurement_triggers(
                alarm_cfg.triggers, self._last_meas_values
            )
            reasons.extend(fired)

        if not reasons:
            return

        reason_str = " | ".join(reasons)
        self._alarm_manager.fire(alarm_cfg, reason_str)
        self._lbl_last_alarm.setText(reason_str)
        self._lbl_last_alarm.setStyleSheet("color: #ffa657; font-size: 10px; font-weight: bold;")
        self._main_win._log(
            f"  [Alarm] 트리거 발동: {reason_str}", color="#ffa657"
        )

    # ------------------------------------------------------------------
    # Glow animation
    # ------------------------------------------------------------------

    def _update_glow(self):
        self._glow_phase += 0.07
        intensity = (math.sin(self._glow_phase) + 1) / 2
        alpha = int(80 + intensity * 140)
        green = int(140 + intensity * 80)
        self._glow_frame.setStyleSheet(
            f"QFrame#dsGlowFrame {{"
            f"border: 3px solid rgba(40, {green}, 70, {alpha});"
            f"border-radius: 6px; }}"
        )

    def _stop_glow(self):
        self._glow_timer.stop()
        self._glow_frame.setStyleSheet(
            "QFrame#dsGlowFrame { border: 3px solid transparent; border-radius: 6px; }"
        )

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def _update_ds_save_path(self):
        """Trace 폴더 기준 실제 저장 경로 프리뷰 갱신."""
        from datetime import date as _date
        mw = self._main_win
        main_folder = mw._le_main_folder.text().strip()
        custom_folder = mw._le_custom_folder.text().strip()
        include_date = mw._cb_save_date.isChecked()

        if not main_folder:
            self._lbl_ds_save_path.setText("(Main Folder 미지정)")
            return

        # Second channel 정보
        channels = self._param_reg.main_ui_profile.second_sweep_channels
        cfg_idx = self._cfg.selected_channel_idx if self._cfg is not None else max(0, self._second_radio_group.checkedId())
        ch = channels[cfg_idx] if (0 <= cfg_idx < len(channels)) else None
        fig_ax = (ch.figure_axis or "").strip() if ch else ""

        # 경로 조립
        from pathlib import Path
        d = Path(main_folder)
        subfolder = f"{custom_folder}/trace" if custom_folder else "trace"
        d = d / subfolder
        if include_date:
            d = d / _date.today().strftime("%Y-%m-%d")

        # 파일명 템플릿 (step_val 자리는 placeholder)
        custom_word = mw._le_custom_word.text().strip()
        stem_parts = []
        if custom_word:
            stem_parts.append(custom_word)
        if include_date:
            stem_parts.append(_date.today().strftime("%Y%m%d"))
        stem = "".join(stem_parts)
        id_part = f"_{fig_ax}_<step>" if fig_ax else "_<step>"
        self._lbl_ds_save_path.setText(str(d / f"{stem}{id_part}.dat"))

    def _open_ds_folder(self):
        import subprocess, os
        from pathlib import Path
        path_text = self._lbl_ds_save_path.text()
        if not path_text or path_text.startswith("("):
            return
        folder = str(Path(path_text).parent)
        p = Path(folder)
        while p and not p.is_dir():
            p = p.parent
        if p and p.is_dir():
            subprocess.Popen(f'explorer "{p}"')

    def showEvent(self, event):
        # Worker threads are quit in closeEvent. Restart them if needed.
        if not self._worker_thread.isRunning():
            self._worker_thread.start()
        if not self._second_thread.isRunning():
            self._second_thread.start()
        self._update_ds_save_path()
        self._rebuild_second_channel_radios()
        self._update_unit_labels()
        # Refresh alarm panel measurements in case alarm_measurements changed
        alarm_meas = self._param_reg.main_ui_profile.alarm_measurements
        self._alarm_panel.refresh_measurements(alarm_meas)
        if self._phase == DoubleSweepPhase.IDLE:
            self._btn_start.setEnabled(not self._main_win._running)
        super().showEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._save_config()
            self.hide()
            event.accept()
            return
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_S):
            self._save_config()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._phase != DoubleSweepPhase.IDLE:
            reply = QMessageBox.question(
                self, "종료 확인",
                "Double Sweep이 실행 중입니다. 중단하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            self._on_stop()
        self._save_config()
        self._worker_thread.quit()
        self._worker_thread.wait()
        self._second_thread.quit()
        self._second_thread.wait()
        event.accept()
