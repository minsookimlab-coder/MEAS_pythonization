"""알림 (ntfy) 설정 — 알람·AI 보고를 휴대폰으로 받고, 휴대폰에서 AI 에게 지시한다.

전역 알람 전송 설정(app_config.yaml 의 alarm_delivery)에 ntfy 항목만 저장한다.
다른 전송 수단(소리·이메일·텔레그램) 값은 건드리지 않는다.

ntfy 서버는 실험실 PC 에 구축하는 것을 기본으로 한다:
  - 이 프로그램은 같은 PC 의 서버에 붙는다 (예: http://127.0.0.1:2586)
  - 휴대폰은 랩 네트워크·VPN 으로 그 PC 에 붙는다 (예: http://192.168.0.20:2586)
    → 알림의 [승인]/[거부] 버튼이 이 주소로 요청을 보내므로 '휴대폰 접속 주소'가 따로 필요하다.
"""
import threading

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QSpinBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pythonization.config.app_config import load_alarm_delivery, save_alarm_delivery
from pythonization.notify.ntfy import publish, random_topic
from pythonization.ui.widgets.help_button import make_help_button


class _TestRelay(QObject):
    done = Signal(str)


def _help_html() -> str:
    return (
        "<b>ntfy 연동</b><br>"
        "알람(Double Sweep·VNA)과 AI 의 보고가 ntfy 로 휴대폰에 옵니다. 지시용 주제에 글을 "
        "쓰면 AI 에게 지시로 전달됩니다. AI 가 승인을 기다리면 알림에 [승인]/[거부] 버튼이 "
        "붙습니다.<br><br>"
        "<b>실험실 PC 에 ntfy 서버 구축 (요약)</b><ol>"
        "<li>ntfy.exe 를 받아 PATH 에 둔다 (또는 <code>scoop install ntfy</code>)</li>"
        "<li><code>%ProgramData%\\ntfy\\server.yml</code>:<br>"
        "<code>base-url: \"http://&lt;이 PC IP&gt;:2586\"</code><br>"
        "<code>listen-http: \":2586\"</code><br>"
        "<code>cache-file: \"C:\\ProgramData\\ntfy\\cache.db\"</code><br>"
        "<code>auth-file: \"C:\\ProgramData\\ntfy\\user.db\"</code><br>"
        "<code>auth-default-access: \"deny-all\"</code></li>"
        "<li>사용자·권한·토큰: <code>ntfy user add lab</code>, "
        "<code>ntfy access lab &lt;주제&gt; rw</code> (두 주제 모두), "
        "<code>ntfy token add lab</code> → 토큰을 아래에 입력</li>"
        "<li>서비스로 실행: <code>sc create ntfy binPath= \"C:\\경로\\ntfy.exe serve\"</code></li>"
        "<li>휴대폰 ntfy 앱에서 서버 주소(휴대폰 접속 주소)와 계정으로 두 주제를 구독</li></ol>"
        "자세한 내용은 사용자 매뉴얼의 'AI 자동 측정 — ntfy' 절.")


class NtfySettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("알림 (ntfy) 설정")
        self.setMinimumWidth(620)
        self._delivery = load_alarm_delivery()
        d = self._delivery
        self._relay = _TestRelay()
        self._relay.done.connect(self._on_test_done)

        root = QVBoxLayout(self)
        head = QHBoxLayout()
        title = QLabel("ntfy — 휴대폰 알림 + AI 원격 지시")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        head.addWidget(title)
        head.addWidget(make_help_button(_help_html(), "ntfy 도움말"))
        head.addStretch()
        root.addLayout(head)

        form = QFormLayout()
        root.addLayout(form)
        self._cb_use = QCheckBox("ntfy 로 알람·AI 보고 보내기")
        self._cb_use.setChecked(d.use_ntfy)
        form.addRow(self._cb_use)

        self._le_server = QLineEdit(d.ntfy_server)
        self._le_server.setPlaceholderText("이 프로그램이 붙는 주소 — 예: http://127.0.0.1:2586")
        form.addRow("서버 주소:", self._le_server)
        self._le_public = QLineEdit(d.ntfy_public_url)
        self._le_public.setPlaceholderText(
            "휴대폰이 붙는 주소 — 예: http://192.168.0.20:2586 (비우면 서버 주소)")
        form.addRow("휴대폰 접속 주소:", self._le_public)

        self._le_topic = QLineEdit(d.ntfy_topic)
        self._le_topic.setPlaceholderText("알람·보고를 받을 주제")
        form.addRow("보고 주제:", self._with_random(self._le_topic, "pyz-report"))

        self._le_token = QLineEdit(d.ntfy_token)
        self._le_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._le_token.setPlaceholderText("tk_... (서버가 deny-all 이면 필수)")
        form.addRow("접근 토큰:", self._le_token)

        self._cb_report = QCheckBox("측정 리포트 보내기 (그래프·장비 상태·측정 조건)")
        self._cb_report.setToolTip(
            "측정이 갱신될 때마다 마지막 그래프 이미지와 장비 값·측정 조건을 묶어 보고 주제로 보냅니다. 측정이 끝나거나 중단되면 주기와 무관하게 한 번 보냅니다.")
        self._cb_report.setChecked(d.ntfy_report_enabled)
        form.addRow(self._cb_report)
        self._sb_report_n = QSpinBox()
        self._sb_report_n.setRange(1, 100)
        self._sb_report_n.setValue(max(1, int(d.ntfy_report_every_n or 1)))
        self._sb_report_n.setToolTip("1 이면 갱신마다. 긴 측정은 값을 올려 알림을 줄입니다.")
        form.addRow("리포트 주기 (갱신 N 회마다):", self._sb_report_n)

        self._cb_cmd = QCheckBox("지시용 주제의 글을 AI 에게 지시로 전달 (승인 버튼 포함)")
        self._cb_cmd.setChecked(d.ntfy_commands_enabled)
        form.addRow(self._cb_cmd)
        self._le_cmd = QLineEdit(d.ntfy_command_topic)
        self._le_cmd.setPlaceholderText("AI 에게 지시를 보낼 주제 (보고 주제와 다르게)")
        form.addRow("지시 주제:", self._with_random(self._le_cmd, "pyz-cmd"))

        note = QLabel(
            "지시 주제에 쓸 수 있는 글: 자유 문장(AI 에게 지시), '상태', '중지', "
            "'승인 <번호>', '거부 <번호> <사유>'. 지시로 온 글도 안전 한계·승인 절차를 그대로 "
            "거칩니다. 주제·토큰은 이 PC 의 설정 파일에만 저장됩니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888888;")
        root.addWidget(note)

        row = QHBoxLayout()
        self._btn_test = QPushButton("테스트 전송")
        self._btn_test.clicked.connect(self._on_test)
        row.addWidget(self._btn_test)
        self._lbl_test = QLabel("")
        self._lbl_test.setWordWrap(True)
        row.addWidget(self._lbl_test, stretch=1)
        root.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _with_random(self, field: QLineEdit, prefix: str):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(field, stretch=1)
        btn = QPushButton("무작위 이름")
        btn.setToolTip("추측할 수 없는 주제 이름 — 공용 서버(ntfy.sh)를 쓸 때는 필수")
        btn.clicked.connect(lambda: field.setText(random_topic(prefix)))
        row.addWidget(btn)
        w = QWidget()
        w.setLayout(row)
        return w

    def _values(self) -> dict:
        return {
            "use_ntfy": self._cb_use.isChecked(),
            "ntfy_server": self._le_server.text().strip().rstrip("/"),
            "ntfy_public_url": self._le_public.text().strip().rstrip("/"),
            "ntfy_topic": self._le_topic.text().strip(),
            "ntfy_token": self._le_token.text().strip(),
            "ntfy_report_enabled": self._cb_report.isChecked(),
            "ntfy_report_every_n": int(self._sb_report_n.value()),
            "ntfy_commands_enabled": self._cb_cmd.isChecked(),
            "ntfy_command_topic": self._le_cmd.text().strip(),
        }

    def _validate(self, v: dict) -> str:
        if v["use_ntfy"] or v["ntfy_commands_enabled"]:
            if not v["ntfy_server"].lower().startswith(("http://", "https://")):
                return "서버 주소는 http:// 또는 https:// 로 시작해야 합니다."
        if v["use_ntfy"] and not v["ntfy_topic"]:
            return "보고 주제를 입력하세요."
        if v["ntfy_commands_enabled"]:
            if not v["ntfy_command_topic"]:
                return "지시 주제를 입력하세요."
            if v["ntfy_command_topic"] == v["ntfy_topic"]:
                return "지시 주제는 보고 주제와 달라야 합니다 (보고가 지시로 되돌아오지 않게)."
        for key in ("ntfy_topic", "ntfy_command_topic"):
            topic = v[key]
            if topic and not all(c.isalnum() or c in "-_" for c in topic):
                return f"주제 이름에는 영문·숫자·-·_ 만 쓸 수 있습니다: {topic}"
        return ""

    def _on_test(self):
        v = self._values()
        if not v["ntfy_server"] or not v["ntfy_topic"]:
            self._lbl_test.setText("서버 주소와 보고 주제를 먼저 입력하세요.")
            return
        self._btn_test.setEnabled(False)
        self._lbl_test.setText("전송 중…")

        def run():
            err = publish(v["ntfy_server"], v["ntfy_topic"],
                          "Pythonization ntfy 연결 테스트입니다.", title="Pythonization",
                          tags=["white_check_mark"], token=v["ntfy_token"])
            self._relay.done.emit(err or "")

        threading.Thread(target=run, daemon=True).start()

    @Slot(str)
    def _on_test_done(self, err: str):
        self._btn_test.setEnabled(True)
        if err:
            self._lbl_test.setText(f"실패: {err}")
            self._lbl_test.setStyleSheet("color: #f44747;")
        else:
            self._lbl_test.setText("보냈습니다 — 휴대폰에 알림이 왔는지 확인하세요.")
            self._lbl_test.setStyleSheet("color: #4ec9b0;")

    def _on_save(self):
        v = self._values()
        err = self._validate(v)
        if err:
            QMessageBox.warning(self, "ntfy 설정", err)
            return
        # 다른 전송 수단 값은 그대로 두고 ntfy 항목만 바꾼다
        save_alarm_delivery(load_alarm_delivery().model_copy(update=v))
        self.accept()
