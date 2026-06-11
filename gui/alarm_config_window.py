"""
공유 알람 설정 — Double Sweep / VNA sweep 공용.

구성:
  TelegramPanel      : 텔레그램 봇 토큰 + 수신자 관리 (이전 double_sweep_window.py에서 이동)
  AlarmDeliveryPanel : enable / 사운드 / 이메일 / 고정 트리거(통신·측정 오류·완료)
  MeasCondPanel      : 측정값 조건 빌더 (Double Sweep 창에 그대로 남는 부분)
  AlarmConfigWindow  : Delivery + Telegram 을 묶은 다이얼로그 (측정값 조건은 보존)

정책:
  - Double Sweep / VNA 가 각자 자기 AlarmConfig를 보유한다 (별도 config).
  - AlarmConfigWindow는 전달받은 AlarmConfig의 measurement-kind 트리거를 건드리지 않고
    보존한다 (측정값 조건은 Double Sweep 창의 MeasCondPanel이 관리하기 때문).
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Signal, Slot, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget,
)

from config.config_models import (
    AlarmConfig, AlarmOperator, AlarmTrigger, InstantiatedMeasurement,
    TelegramContact,
)

_MONO = QFont("Consolas", 10)
_OP_LABELS = [">", "<", ">=", "<=", "==", "!="]


def _truncate(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ──────────────────────────────────────────────────────────
# Telegram panel
# ──────────────────────────────────────────────────────────

class TelegramPanel(QFrame):
    """텔레그램 봇 알람 설정 — 수신자 목록(이름 → Chat ID) 관리.

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
        idx = self._combo_contacts.currentIndex()
        if 0 <= idx < len(self._contacts):
            return self._contacts[idx]["chat_id"]
        return ""

    def _on_contact_selected(self, idx: int):
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

    def get_config(self) -> dict:
        return {
            "use_telegram": self._cb_enable.isChecked(),
            "telegram_bot_token": self._le_token.text().strip(),
            "telegram_chat_id": self._active_chat_id(),
            "telegram_contacts": list(self._contacts),
        }

    def load_config(self, cfg: AlarmConfig):
        self._cb_enable.setChecked(cfg.use_telegram)
        self._le_token.setText(cfg.telegram_bot_token)
        self._contacts = [{"name": c.name, "chat_id": c.chat_id}
                          for c in cfg.telegram_contacts]
        self._refresh_combo()
        if cfg.telegram_chat_id:
            for i, c in enumerate(self._contacts):
                if c["chat_id"] == cfg.telegram_chat_id:
                    self._combo_contacts.setCurrentIndex(i)
                    break


# ──────────────────────────────────────────────────────────
# Alarm delivery panel  (enable / sound / email / fixed triggers)
# ──────────────────────────────────────────────────────────

class AlarmDeliveryPanel(QFrame):
    """알람 전달 설정 — enable, 사운드, 이메일, 고정 트리거(통신·측정 오류·완료).

    측정값 조건(measurement-kind 트리거)은 다루지 않는다.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
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

        root.addStretch()
        self._update_enabled_state(False)

    def _on_email_toggled(self, checked: bool):
        self._email_widget.setVisible(checked)

    def _update_enabled_state(self, enabled: bool):
        for w in (self._cb_sound, self._cb_email, self._cb_comm_error,
                  self._cb_meas_error, self._cb_on_complete):
            w.setEnabled(enabled)

    # ------------------------------------------------------------------

    def fixed_triggers(self) -> List[AlarmTrigger]:
        triggers = []
        if self._cb_comm_error.isChecked():
            triggers.append(AlarmTrigger(kind="comm_error", enabled=True))
        if self._cb_meas_error.isChecked():
            triggers.append(AlarmTrigger(kind="meas_error", enabled=True))
        return triggers

    def apply_to(self, cfg: AlarmConfig) -> AlarmConfig:
        """delivery 필드를 cfg에 반영하되 measurement-kind 트리거는 보존."""
        kept_meas = [t for t in cfg.triggers if t.kind == "measurement"]
        return cfg.model_copy(update={
            "enabled": self._cb_enable.isChecked(),
            "use_sound": self._cb_sound.isChecked(),
            "use_email": self._cb_email.isChecked(),
            "email_to": self._le_email_to.text().strip(),
            "smtp_host": self._le_smtp_host.text().strip(),
            "smtp_port": self._sb_smtp_port.value(),
            "smtp_user": self._le_smtp_user.text().strip(),
            "smtp_password": self._le_smtp_pass.text(),
            "fire_on_complete": self._cb_on_complete.isChecked(),
            "triggers": self.fixed_triggers() + kept_meas,
        })

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
        self._cb_comm_error.setChecked(
            any(t.kind == "comm_error" and t.enabled for t in cfg.triggers))
        self._cb_meas_error.setChecked(
            any(t.kind == "meas_error" and t.enabled for t in cfg.triggers))
        self._update_enabled_state(cfg.enabled)


# ──────────────────────────────────────────────────────────
# Measurement-condition panel  (stays in Double Sweep window)
# ──────────────────────────────────────────────────────────

class MeasCondPanel(QFrame):
    """측정값 조건 빌더 — alarm_measurements 목록에서 조건(>, < …) 추가/삭제.

    measurement-kind AlarmTrigger 목록만 관리한다.
    Double Sweep 창에 그대로 남는다.
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
        cur = self._combo_meas.currentText()
        self._combo_meas.blockSignals(True)
        self._combo_meas.clear()
        self._combo_meas.addItems([m.description for m in measurements])
        idx = self._combo_meas.findText(cur)
        if idx >= 0:
            self._combo_meas.setCurrentIndex(idx)
        self._combo_meas.blockSignals(False)

    def setEnabled(self, enabled: bool):  # noqa: N802 (Qt 호환)
        for w in (self._trig_scroll, self._combo_meas, self._combo_op, self._le_thresh):
            w.setEnabled(enabled)

    def measurement_triggers(self) -> List[AlarmTrigger]:
        triggers = []
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
        return triggers

    def load_triggers(self, cfg: AlarmConfig):
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


# ──────────────────────────────────────────────────────────
# Alarm config dialog  (Delivery + Telegram)
# ──────────────────────────────────────────────────────────

class AlarmConfigWindow(QDialog):
    """알람 설정 다이얼로그 — Delivery + Telegram.

    측정값 조건(measurement-kind 트리거)은 입력 cfg에서 그대로 보존된다.
    apply_requested(AlarmConfig) — Apply/OK 시 emit.
    """

    apply_requested = Signal(object)   # AlarmConfig

    def __init__(self, alarm_cfg: AlarmConfig, alarm_manager, parent=None,
                 title: str = "Alarm Config"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self._cfg = alarm_cfg.model_copy(deep=True)   # measurement 트리거 보존용 스냅샷
        self._build_ui(alarm_manager)
        self._load()

    def _build_ui(self, alarm_manager):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        panels = QHBoxLayout()
        panels.setSpacing(8)
        self._delivery = AlarmDeliveryPanel()
        self._telegram = TelegramPanel(alarm_manager)
        panels.addWidget(self._delivery, stretch=1)
        panels.addWidget(self._telegram, stretch=1)
        root.addLayout(panels)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_apply = QPushButton("Apply"); btn_apply.setFixedWidth(80)
        btn_apply.clicked.connect(self._on_apply)
        btn_ok = QPushButton("OK"); btn_ok.setFixedWidth(80); btn_ok.setDefault(True)
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel = QPushButton("Cancel"); btn_cancel.setFixedWidth(80)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_apply); btn_row.addWidget(btn_ok); btn_row.addWidget(btn_cancel)
        root.addLayout(btn_row)

    def _load(self):
        self._delivery.load_config(self._cfg)
        self._telegram.load_config(self._cfg)

    def get_config(self) -> AlarmConfig:
        """Delivery + Telegram + (보존된) 측정값 조건을 합친 AlarmConfig."""
        cfg = self._delivery.apply_to(self._cfg)   # delivery + 고정트리거 + 보존된 meas 트리거
        tg = self._telegram.get_config()
        contacts = [TelegramContact(**c) for c in tg.pop("telegram_contacts", [])]
        return cfg.model_copy(update={**tg, "telegram_contacts": contacts})

    def _on_apply(self):
        self._cfg = self.get_config()
        self.apply_requested.emit(self._cfg)

    def _on_ok(self):
        self._cfg = self.get_config()
        self.apply_requested.emit(self._cfg)
        self.accept()
