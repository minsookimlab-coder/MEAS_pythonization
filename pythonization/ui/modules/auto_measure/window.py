"""AI 자동 측정 창.

사용자가 자연어로 측정을 요청하면 LLM(Claude)이 프로그램의 내장 측정 기능을 도구로
조작해 측정을 끝까지 수행하고, 결과를 판단해 다음 측정을 정하고, 메신저로 보고한다.

AI 를 돌리는 방법 두 가지 — 도구·안전 검사·저장 경로는 똑같다:
  1. Claude Code (데스크톱 앱·CLI) — 이 창에서 MCP 서버를 켜면 Claude Code 가 붙어
     도구를 호출한다. API 키가 필요 없다 (Claude Code 로그인으로 돈다).
  2. 앱 내장 실행 — 이 창에 요청을 쓰고 [새 측정 시작]. Claude API 키가 필요하다.

공통:
  - 측정은 메인 창 sweep 경로를 그대로 쓴다 (그래프·.dat·메타데이터·자동 재개 동일).
  - 장비를 움직이는 조작은 '조작 전마다 승인' 모드에서 사람이 이 창에서 승인해야 한다.
  - 채널마다 허용 여부·범위·속도 상한·역할 메모를 표에서 정한다. 허용하지 않은 채널은
    AI 가 움직일 수 없다 (코드가 검사한다).
  - 실험 지식(현상 → 물리적 의미 → 대응, 관찰 기록)은 가이드로만 쓴다. 판단은 AI 가
    현재 데이터로 직접 한다.
  - 실행 기록은 Main Folder/ai_runs/{시각}/ 에 journal.jsonl (+ report.md) 로 남는다.

스레드:
  앱 내장 실행의 에이전트 루프와 MCP 작업은 데몬 스레드에서 돈다. 그쪽 → 창은
  _AgentSignals 의 신호를 bound @Slot 으로 받는다. 메인 창 조작은 MainWindowBridge.call 로
  GUI 스레드에 넘긴다.
"""
import json
import os
import secrets
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pythonization.app.logging_setup import get_logger
from pythonization.app.paths import SETTINGS_DIR
from pythonization.automation.agent import AgentEvent, AgentLoop, ApprovalDecision
from pythonization.automation.executor import LabExecutor
from pythonization.automation.knowledge import KIND_LABELS, KnowledgeStore
from pythonization.automation.lab_context import build_lab_context
from pythonization.automation.llm import (
    LlmUnavailable,
    has_env_credentials,
    make_client,
    sdk_available,
)
from pythonization.automation.mcp_server import (
    SERVER_NAME,
    McpLabServer,
    load_or_create_token,
    regenerate_token,
)
from pythonization.automation.prompts import SYSTEM_PROMPT, first_user_message
from pythonization.automation.remote import HELP_TEXT, parse_remote
from pythonization.automation.settings import (
    EFFORT_CHOICES,
    MODEL_CHOICES,
    AutomationSettings,
    LlmSettings,
    load_automation_settings,
    load_llm_settings,
    save_automation_settings,
    save_llm_settings,
)
from pythonization.automation.tools import TOOL_SPECS
from pythonization.config.app_config import load_alarm_delivery
from pythonization.instruments.factory import resolve_class_path
from pythonization.notify.alarm_manager import AlarmManager
from pythonization.notify.ntfy import NtfySubscriber, http_action
from pythonization.ui.modules.auto_measure.bridge import MainWindowBridge
from pythonization.ui.widgets.help_button import make_help_button

if TYPE_CHECKING:
    from pythonization.ui.main_window import MainWindow

_MONO = QFont("Consolas", 10)
_APPROVAL_REMIND_S = 120      # 승인 대기가 이만큼 길어지면 메신저로 알린다
_CLAUDE_CODE_ACTIVE_S = 30 * 60   # 이 시간 안에 Claude Code 호출이 있었으면 '대화 중'으로 본다
_STATUS_LABELS = {
    "finished": "완료", "stopped": "사용자 중지", "budget": "예산 소진",
    "error": "오류로 중단", "refusal": "모델 거절로 중단",
}
_LOG_COLORS = {
    "user": "#9cdcfe", "assistant": "#e6e6e6", "progress": "#8b949e",
    "tool_call": "#e3b341", "approval": "#c586c0", "status": "#8b949e",
    "error": "#f44747", "message": "#56d364", "finished": "#4ec9b0",
    "knowledge": "#d2a8ff", "ntfy": "#79c0ff",
}
_KIND_BY_LABEL = {v: k for k, v in KIND_LABELS.items()}


class _AgentSignals(QObject):
    """작업 스레드 → 창. emit 은 다른 스레드에서, 수신은 GUI 스레드 bound @Slot."""
    event = Signal(object)        # AgentEvent
    approval = Signal(object)     # _ApprovalRequest
    finished = Signal(str)        # 앱 내장 실행 종료 상태
    remote = Signal(str)          # 휴대폰(ntfy 지시 주제)에서 온 글


class _ApprovalRequest:
    def __init__(self, name: str, tool_input: dict, preview: str):
        self.name = name
        self.tool_input = tool_input
        self.preview = preview
        self.decision: Optional[ApprovalDecision] = None
        self.done = threading.Event()
        # 휴대폰에서 '승인 1234' 로 답할 때 쓰는 번호
        self.short_id = f"{secrets.randbelow(9000) + 1000}"

    def decide(self, approved: bool, comment: str = "") -> None:
        if not self.done.is_set():
            self.decision = ApprovalDecision(approved, comment)
            self.done.set()


def _num_text(v) -> str:
    return "" if v is None else f"{v:g}"


def _parse_opt_float(text: str, what: str):
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{what}: '{text}' 은(는) 숫자가 아닙니다.")


class _LlmSettingsDialog(QDialog):
    """API 키·모델·effort (앱 내장 실행용). 저장 위치는 SETTINGS_DIR/llm.yaml (저장소 밖)."""

    def __init__(self, cfg: LlmSettings, settings_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LLM 설정 (앱 내장 실행)")
        self.setMinimumWidth(520)
        self._cfg = cfg
        self._settings_dir = settings_dir
        form = QFormLayout(self)

        note0 = QLabel("Claude Code 로 돌릴 때는 필요 없습니다. 앱 안에서 직접 돌릴 때만 씁니다.")
        note0.setWordWrap(True)
        note0.setStyleSheet("color: #888888;")
        form.addRow(note0)
        self._le_key = QLineEdit(cfg.api_key)
        self._le_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._le_key.setPlaceholderText(
            "비우면 ANTHROPIC_API_KEY 환경변수 등 기본 자격증명 사용")
        form.addRow("API 키:", self._le_key)
        note = QLabel(f"저장 위치: {settings_dir / 'llm.yaml'}  (저장소에 올라가지 않음)")
        note.setStyleSheet("color: #888888; font-size: 10px;")
        note.setWordWrap(True)
        form.addRow("", note)

        self._cb_model = QComboBox()
        self._cb_model.setEditable(True)
        self._cb_model.addItems(MODEL_CHOICES)
        self._cb_model.setCurrentText(cfg.model)
        form.addRow("모델:", self._cb_model)

        self._cb_effort = QComboBox()
        self._cb_effort.addItems(EFFORT_CHOICES)
        self._cb_effort.setCurrentText(cfg.effort)
        self._cb_effort.setToolTip("판단의 깊이. 높을수록 신중하지만 느리고 비용이 늘어난다. "
                                   "장시간 자동 측정은 high 를 권장.")
        form.addRow("Effort:", self._cb_effort)

        self._sb_tokens = QSpinBox()
        self._sb_tokens.setRange(2000, 20000)
        self._sb_tokens.setSingleStep(1000)
        self._sb_tokens.setValue(cfg.max_tokens)
        form.addRow("응답 최대 토큰:", self._sb_tokens)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _on_ok(self):
        self._cfg.api_key = self._le_key.text().strip()
        self._cfg.model = self._cb_model.currentText().strip() or MODEL_CHOICES[0]
        self._cfg.effort = self._cb_effort.currentText()
        self._cfg.max_tokens = self._sb_tokens.value()
        try:
            save_llm_settings(self._cfg, self._settings_dir)
        except Exception as e:
            QMessageBox.critical(self, "저장 실패", str(e))
            return
        self.accept()


class _KnowledgeEditDialog(QDialog):
    """실험 지식 규칙 하나 추가/편집."""

    def __init__(self, parent=None, entry=None):
        super().__init__(parent)
        self.setWindowTitle("실험 지식 — 규칙")
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        self.cb_kind = QComboBox()
        for kind, label in KIND_LABELS.items():
            self.cb_kind.addItem(label, kind)
        self.le_title = QLineEdit()
        self.te_condition = QPlainTextEdit()
        self.te_meaning = QPlainTextEdit()
        self.te_action = QPlainTextEdit()
        for te in (self.te_condition, self.te_meaning, self.te_action):
            te.setFixedHeight(60)
        self.le_tags = QLineEdit()
        self.le_tags.setPlaceholderText("쉼표로 구분 — 예: graphene, gate, dirac_point")
        self.le_sample = QLineEdit()
        self.le_sample.setPlaceholderText("특정 시료에만 해당하면 이름 (비우면 공통)")
        self.cb_confirmed = QCheckBox("확인됨 (비우면 '가설'로 표시)")
        self.cb_confirmed.setChecked(True)
        form.addRow("종류:", self.cb_kind)
        form.addRow("제목:", self.le_title)
        form.addRow("현상 (조건):", self.te_condition)
        form.addRow("물리적 의미:", self.te_meaning)
        form.addRow("대응:", self.te_action)
        form.addRow("태그:", self.le_tags)
        form.addRow("시료:", self.le_sample)
        form.addRow(self.cb_confirmed)
        if entry is not None:
            self.cb_kind.setCurrentIndex(max(0, self.cb_kind.findData(entry.kind)))
            self.le_title.setText(entry.title)
            self.te_condition.setPlainText(entry.condition)
            self.te_meaning.setPlainText(entry.meaning)
            self.te_action.setPlainText(entry.action)
            self.le_tags.setText(", ".join(entry.tags))
            self.le_sample.setText(entry.sample)
            self.cb_confirmed.setChecked(entry.confirmed)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _on_ok(self):
        if not self.le_title.text().strip():
            QMessageBox.warning(self, "제목 없음", "제목을 입력하세요.")
            return
        self.accept()

    def fields(self) -> dict:
        return {
            "kind": self.cb_kind.currentData(),
            "title": self.le_title.text().strip(),
            "condition": self.te_condition.toPlainText().strip(),
            "meaning": self.te_meaning.toPlainText().strip(),
            "action": self.te_action.toPlainText().strip(),
            "tags": [t.strip() for t in self.le_tags.text().split(",") if t.strip()],
            "sample": self.le_sample.text().strip(),
            "confirmed": self.cb_confirmed.isChecked(),
        }


class AutoMeasureWindow(QDialog):
    """AI 자동 측정 창 (보관형 — 닫아도 숨기기만 하고 실행·서버는 계속된다)."""

    def __init__(self, main_win: "MainWindow", param_reg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 자동 측정")
        self.resize(1320, 860)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self._main_win = main_win
        self._param_reg = param_reg
        self._settings_dir: Path = getattr(param_reg, "settings_dir", None) or SETTINGS_DIR
        self._llm_cfg: LlmSettings = load_llm_settings(self._settings_dir)
        self._cfg: AutomationSettings = AutomationSettings()
        self._knowledge = KnowledgeStore(self._settings_dir / "knowledge")

        self._signals = _AgentSignals()
        self._signals.event.connect(self._on_agent_event)
        self._signals.approval.connect(self._on_approval_requested)
        self._signals.finished.connect(self._on_agent_finished)
        self._signals.remote.connect(self._on_remote_message)
        self._bridge = MainWindowBridge(main_win)
        self._alarm = AlarmManager()

        # 앱 내장 실행
        self._thread: Optional[threading.Thread] = None
        self._agent: Optional[AgentLoop] = None
        self._executor: Optional[LabExecutor] = None
        self._stop_event = threading.Event()
        self._run_dir: Optional[Path] = None
        self._run_request = ""
        self._profile_key = ""
        self._last_error = ""
        # Claude Code (MCP)
        self._mcp: Optional[McpLabServer] = None
        self._mcp_executor: Optional[LabExecutor] = None
        self._mcp_run_dir: Optional[Path] = None

        self._pending: Optional[_ApprovalRequest] = None
        self._journal_lock = threading.Lock()
        # 휴대폰(ntfy) 지시
        self._ntfy_sub: Optional[NtfySubscriber] = None
        self._ntfy_state = ""
        self._remote_inbox: List[str] = []          # Claude Code 에 넘길 지시
        self._inbox_lock = threading.Lock()
        self._recent = deque(maxlen=8)              # '상태' 답장용 최근 기록

        self._build_ui()
        self.reload_from_profile()
        self._update_status_label()
        self._set_running_ui(False)
        self._update_mcp_ui()
        self.on_ntfy_settings_changed()

    # ==================================================================
    # UI
    # ==================================================================
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.addLayout(self._build_title_row())
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([600, 720])
        root.addWidget(splitter, stretch=1)

    def _build_title_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title = QLabel("AI 자동 측정")
        title.setStyleSheet("font-weight: bold; font-size: 13px; color: #d2a8ff;")
        row.addWidget(title)
        row.addWidget(make_help_button(self._help_html(), "AI 자동 측정 도움말"))
        row.addStretch()
        return row

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.addWidget(self._build_mcp_box())
        lay.addWidget(self._build_inapp_box())

        row = QHBoxLayout()
        row.addWidget(QLabel("승인 방식:"))
        self._cb_approval = QComboBox()
        self._cb_approval.addItem("장비 조작 전마다 이 창에서 승인 (권장)", "approve")
        self._cb_approval.addItem("전자동 (안전 한계 안에서 승인 없이)", "auto")
        self._cb_approval.currentIndexChanged.connect(self._on_approval_mode_changed)
        row.addWidget(self._cb_approval, stretch=1)
        lay.addLayout(row)

        lay.addWidget(self._build_approval_card())

        tabs = QTabWidget()
        tabs.addTab(self._build_limits_tab(), "안전 한계·역할")
        tabs.addTab(self._build_knowledge_tab(), "실험 지식")
        tabs.addTab(self._build_notes_tab(), "실험 메모")
        tabs.addTab(self._build_budget_tab(), "보고·예산")
        lay.addWidget(tabs, stretch=1)
        return panel

    def _build_mcp_box(self) -> QGroupBox:
        box = QGroupBox("Claude Code 로 실행 (데스크톱 앱·CLI — API 키 불필요)")
        lay = QVBoxLayout(box)
        row = QHBoxLayout()
        self._btn_mcp = QPushButton("연결 서버 켜기")
        self._btn_mcp.setStyleSheet("font-weight: bold;")
        self._btn_mcp.clicked.connect(self._toggle_mcp)
        row.addWidget(self._btn_mcp)
        row.addWidget(QLabel("포트:"))
        self._sb_port = QSpinBox()
        self._sb_port.setRange(1024, 65535)
        self._sb_port.setValue(self._llm_cfg.mcp_port)
        row.addWidget(self._sb_port)
        self._cb_mcp_auto = QCheckBox("프로그램 시작 시 자동으로 켜기")
        self._cb_mcp_auto.setChecked(self._llm_cfg.mcp_enabled)
        self._cb_mcp_auto.toggled.connect(self._on_mcp_autostart_toggled)
        row.addWidget(self._cb_mcp_auto)
        row.addStretch()
        lay.addLayout(row)
        self._lbl_mcp = QLabel("")
        self._lbl_mcp.setWordWrap(True)
        self._lbl_mcp.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._lbl_mcp)
        row = QHBoxLayout()
        btn_copy = QPushButton("다른 폴더용 연결 명령 복사")
        btn_copy.setToolTip("이 저장소 폴더 밖에서 Claude Code 를 쓸 때 — 터미널에 붙여 넣어 "
                            "사용자 범위로 등록 (토큰 포함, 남에게 보내지 말 것)")
        btn_copy.clicked.connect(self._copy_mcp_command)
        row.addWidget(btn_copy)
        btn_token = QPushButton("토큰 새로 만들기")
        btn_token.clicked.connect(self._regenerate_token)
        row.addWidget(btn_token)
        row.addStretch()
        lay.addLayout(row)
        hint = QLabel("이 프로그램 폴더를 Claude Code 데스크톱 앱에서 열면 저장소의 .mcp.json 으로 "
                      "자동 연결됩니다 (처음 한 번 폴더 신뢰·서버 사용을 승인).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888;")
        lay.addWidget(hint)
        row = QHBoxLayout()
        self._lbl_ntfy = QLabel("")
        self._lbl_ntfy.setWordWrap(True)
        row.addWidget(self._lbl_ntfy, stretch=1)
        btn_ntfy = QPushButton("알림 (ntfy) 설정...")
        btn_ntfy.clicked.connect(self._main_win._open_ntfy_settings)
        row.addWidget(btn_ntfy)
        lay.addLayout(row)
        return box

    def _build_inapp_box(self) -> QGroupBox:
        box = QGroupBox("앱 안에서 직접 실행 (Claude API 키 필요)")
        lay = QVBoxLayout(box)
        row = QHBoxLayout()
        self._lbl_status = QLabel("")
        self._lbl_status.setStyleSheet("color: #888888;")
        row.addWidget(self._lbl_status, stretch=1)
        btn_llm = QPushButton("LLM 설정...")
        btn_llm.clicked.connect(self._open_llm_settings)
        row.addWidget(btn_llm)
        lay.addLayout(row)

        self._te_request = QPlainTextEdit()
        self._te_request.setPlaceholderText(
            "예) 게이트를 -20 V 에서 20 V 까지 0.5 V/min 으로 스윕하면서 락인 X, Y 를 "
            "측정해. 누설전류가 1 nA 를 넘으면 멈추고 알려줘. 끝나면 게이트를 0 V 로 "
            "되돌려.")
        self._te_request.setFixedHeight(90)
        lay.addWidget(self._te_request)

        row = QHBoxLayout()
        self._btn_start = QPushButton("▶  새 측정 시작")
        self._btn_start.setStyleSheet("font-weight: bold;")
        self._btn_start.clicked.connect(self._on_start_clicked)
        self._btn_stop = QPushButton("■  중지")
        self._btn_stop.setToolTip("앱 내장 실행과 Claude Code 작업을 모두 멈춘다")
        self._btn_stop.clicked.connect(self._on_stop_clicked)
        row.addWidget(self._btn_start)
        row.addWidget(self._btn_stop)
        lay.addLayout(row)

        row = QHBoxLayout()
        self._le_followup = QLineEdit()
        self._le_followup.setPlaceholderText(
            "추가 지시 — 실행 중이면 다음 단계에 반영, 끝난 뒤면 같은 대화로 이어서 실행")
        self._le_followup.returnPressed.connect(self._on_send_followup)
        self._btn_send = QPushButton("보내기")
        self._btn_send.clicked.connect(self._on_send_followup)
        row.addWidget(self._le_followup, stretch=1)
        row.addWidget(self._btn_send)
        lay.addLayout(row)
        return box

    def _build_approval_card(self) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet("QFrame { border: 2px solid #c586c0; border-radius: 4px; }")
        lay = QVBoxLayout(card)
        head = QLabel("승인 요청 — AI 가 다음 조작을 하려고 합니다")
        head.setStyleSheet("font-weight: bold; color: #c586c0; border: none;")
        lay.addWidget(head)
        self._lbl_approval = QLabel("")
        self._lbl_approval.setFont(_MONO)
        self._lbl_approval.setWordWrap(True)
        self._lbl_approval.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._lbl_approval.setStyleSheet("border: none;")
        lay.addWidget(self._lbl_approval)
        self._le_approval_comment = QLineEdit()
        self._le_approval_comment.setPlaceholderText(
            "메모 (선택) — 거부 사유나 승인 조건을 AI 에게 전달")
        lay.addWidget(self._le_approval_comment)
        row = QHBoxLayout()
        btn_ok = QPushButton("승인")
        btn_ok.setStyleSheet("font-weight: bold;")
        btn_ok.clicked.connect(self._on_approve_clicked)
        btn_no = QPushButton("거부")
        btn_no.clicked.connect(self._on_reject_clicked)
        row.addStretch()
        row.addWidget(btn_no)
        row.addWidget(btn_ok)
        lay.addLayout(row)
        card.setVisible(False)
        self._approval_card = card
        return card

    @staticmethod
    def _make_table(headers: List[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setMinimumHeight(90)
        return table

    def _build_limits_tab(self) -> QWidget:
        content = QWidget()
        lay = QVBoxLayout(content)
        hint = QLabel(
            "허용에 체크하고 최소·최대를 적은 채널만 AI 가 움직일 수 있습니다. "
            "역할 메모는 AI 가 요청 속 이름(게이트·바이어스·락인 등)을 채널과 맞추는 데 씁니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888;")
        lay.addWidget(hint)

        lay.addWidget(QLabel("Sweep channel"))
        self._tbl_sweep = self._make_table(
            ["허용", "장비", "항목", "단위", "최소", "최대", "최대 rate(/min)", "역할 메모"])
        lay.addWidget(self._tbl_sweep)
        lay.addWidget(QLabel("Measurement (중단 |값|: 넘으면 AI 판단 없이 즉시 sweep 중단)"))
        self._tbl_meas = self._make_table(["장비", "항목", "단위", "중단 |값| >", "역할 메모"])
        lay.addWidget(self._tbl_meas)
        lay.addWidget(QLabel("Second channel (온도·자기장 등)"))
        self._tbl_second = self._make_table(
            ["허용", "장비", "항목", "단위", "최소", "최대", "역할 메모"])
        lay.addWidget(self._tbl_second)
        lay.addWidget(QLabel("Write command (기본 금지)"))
        self._tbl_write = self._make_table(
            ["허용", "장비", "항목", "값", "최소", "최대", "역할 메모"])
        lay.addWidget(self._tbl_write)
        btn_apply = QPushButton("설정 저장·적용")
        btn_apply.setToolTip("표·메모·예산을 저장하고, 켜져 있는 Claude Code 연결에도 바로 적용")
        btn_apply.clicked.connect(self._on_apply_settings)
        lay.addWidget(btn_apply)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        return scroll

    def _build_knowledge_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        hint = QLabel(
            "현상 → 물리적 의미 → 대응을 적어 두면 AI 가 참고합니다. 가이드일 뿐이며 판단은 AI 가 "
            "현재 데이터로 직접 합니다 (해석 규칙은 데이터를 먼저 본 뒤에만 조회). 관찰이 규칙을 "
            "뒷받침했는지(+) 반박했는지(−)가 함께 쌓입니다. AI 와 대화하며 '기억해' 라고 하면 "
            "여기에 추가됩니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888;")
        lay.addWidget(hint)
        self._tbl_rules = self._make_table(
            ["확인", "종류", "제목", "현상", "의미", "대응", "근거 +/−", "출처"])
        self._tbl_rules.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tbl_rules.cellDoubleClicked.connect(lambda r, _c: self._edit_rule(r))
        self._tbl_rules.itemChanged.connect(self._on_rule_item_changed)
        lay.addWidget(self._tbl_rules, stretch=2)
        row = QHBoxLayout()
        for text, slot in (("+ 규칙 추가", self._add_rule), ("편집", self._edit_selected_rule),
                           ("삭제", self._delete_rule), ("새로고침", self._refresh_knowledge)):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(QLabel("관찰 기록 (최근 순)"))
        self._tbl_obs = self._make_table(["시각", "요약", "수치", "해석", "+ / −", "파일"])
        self._tbl_obs.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self._tbl_obs, stretch=1)
        row = QHBoxLayout()
        btn = QPushButton("관찰 삭제")
        btn.clicked.connect(self._delete_observation)
        row.addWidget(btn)
        lbl = QLabel(f"저장 위치: {self._knowledge.root}")
        lbl.setStyleSheet("color: #888888; font-size: 10px;")
        row.addWidget(lbl)
        row.addStretch()
        lay.addLayout(row)
        return w

    def _build_notes_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        hint = QLabel("시료·배선·관례·평소 쓰는 속도 등 AI 가 알아야 할 내용. "
                      "예) 시료 A (그래핀 홀바). smua = 게이트, SR830 = 4단자 전압. "
                      "게이트 sweep 은 평소 0.5 V/min, 1 s/점. 측정 뒤에는 게이트를 0 V 로.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888;")
        lay.addWidget(hint)
        self._te_notes = QPlainTextEdit()
        lay.addWidget(self._te_notes)
        return w

    def _build_budget_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._cb_report = QCheckBox("AI 의 보고를 메신저(텔레그램·이메일)로 보내기")
        form.addRow(self._cb_report)
        self._cb_report_finish = QCheckBox("앱 내장 실행이 끝나면 최종 보고를 자동으로 보내기")
        form.addRow(self._cb_report_finish)
        self._cb_restore = QCheckBox("끝나면 메인 창 sweep·저장 설정을 원래대로 되돌리기")
        form.addRow(self._cb_restore)
        note = QLabel("메신저 수신자·토큰은 기존 알람 설정(Double Sweep / VNA 의 알람 설정 창)을 "
                      "그대로 씁니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888888;")
        form.addRow(note)

        self._sb_hours = QDoubleSpinBox()
        self._sb_hours.setRange(0.1, 24 * 14)
        self._sb_hours.setDecimals(1)
        form.addRow("최대 실행 시간 (h):", self._sb_hours)
        self._sb_turns = QSpinBox()
        self._sb_turns.setRange(5, 1000)
        form.addRow("최대 LLM 호출 수 (앱 내장):", self._sb_turns)
        self._sb_points = QSpinBox()
        self._sb_points.setRange(10, 1_000_000)
        self._sb_points.setSingleStep(1000)
        form.addRow("sweep 당 최대 점 수:", self._sb_points)
        self._sb_min_tpp = QDoubleSpinBox()
        self._sb_min_tpp.setRange(0.01, 3600)
        self._sb_min_tpp.setDecimals(2)
        form.addRow("최소 Time/Point (s):", self._sb_min_tpp)
        return w

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(4, 0, 0, 0)
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setFont(_MONO)
        self._log_view.setStyleSheet("QTextEdit { background: #1e1e1e; color: #d4d4d4; }")
        lay.addWidget(self._log_view, stretch=1)

        row = QHBoxLayout()
        self._lbl_usage = QLabel("")
        self._lbl_usage.setStyleSheet("color: #888888;")
        row.addWidget(self._lbl_usage)
        row.addStretch()
        btn_graph = QPushButton("그래프")
        btn_graph.clicked.connect(self._main_win._open_graph_window)
        row.addWidget(btn_graph)
        btn_folder = QPushButton("실행 기록 폴더")
        btn_folder.clicked.connect(self._open_run_folder)
        row.addWidget(btn_folder)
        btn_clear = QPushButton("새 대화")
        btn_clear.setToolTip("앱 내장 실행의 이전 대화를 잊고 다음 요청을 새 실행으로 시작")
        btn_clear.clicked.connect(self._on_new_conversation)
        row.addWidget(btn_clear)
        lay.addLayout(row)
        return panel

    @staticmethod
    def _help_html() -> str:
        return (
            "<b>AI 자동 측정</b><br>"
            "자연어로 측정을 요청하면 AI(Claude)가 메인 창 sweep 등 내장 측정 기능을 조작해 "
            "측정하고, 결과를 보고 다음 측정을 정하고, 메신저로 보고합니다.<br><br>"
            "<b>Claude Code 로 실행 (권장)</b><ol>"
            "<li>[연결 서버 켜기]</li>"
            "<li>Claude Code 데스크톱 앱에서 이 프로그램 폴더를 열고 .mcp.json 서버 사용을 승인</li>"
            "<li>대화창에 측정을 요청 — 예: '게이트를 -20 V 에서 20 V 까지 스윕해'</li></ol>"
            "<b>공통 준비</b><ol>"
            "<li>메인 창 Save Settings 의 Main Folder 지정 — AI 는 그 아래에만 저장</li>"
            "<li>안전 한계 표에서 AI 가 움직여도 되는 채널을 허용하고 최소·최대·속도 상한 입력</li>"
            "<li>역할 메모와 실험 메모 작성 — 'smua = 게이트' 처럼</li></ol>"
            "<b>안전</b><ul>"
            "<li>허용 안 된 채널·범위 밖 값·속도 초과는 코드가 거부합니다.</li>"
            "<li>'승인' 모드에서는 장비를 움직이기 전마다 이 창에 승인 카드가 뜹니다 "
            "(Claude Code 호출에도 적용).</li>"
            "<li>중단 |값| 을 넘으면 AI 판단 없이 즉시 sweep 을 멈춥니다.</li>"
            "<li>■ 중지 는 진행 중인 이동·sweep·대기를 바로 멈춥니다.</li></ul>"
            "<b>실험 지식</b>: 현상→의미→대응 규칙과 관찰 기록. 가이드일 뿐, 판단은 AI 가 "
            "데이터로 직접 합니다.<br>"
            "실행 기록: Main Folder/ai_runs/{시각}/journal.jsonl")

    # ==================================================================
    # 설정 ↔ 표
    # ==================================================================
    def _profile_name(self) -> str:
        return getattr(self._param_reg, "active_name", "default")

    def reload_from_profile(self):
        """프로파일 전환·창 생성 시 — 설정을 다시 읽고 표를 다시 그린다."""
        if self._agent_running() or (self._mcp is not None and self._mcp.is_busy()):
            # 실행 중 프로파일이 바뀌면 채널 인덱스가 어긋난다 — 멈춘다
            self._append_log("error", "메인 창 프로파일이 바뀌어 AI 측정을 중지합니다.")
            self._on_stop_clicked()
        self._cfg = load_automation_settings(self._profile_name(), self._settings_dir)
        self._cfg.sync_with_profile(self._main_win._active_profile)
        self._populate_from_cfg()
        self._refresh_knowledge()
        if self._mcp is not None:
            self._mcp_executor = self._build_executor(for_mcp=True)

    def _populate_from_cfg(self):
        cfg, prof = self._cfg, self._main_win._active_profile
        idx = self._cb_approval.findData(cfg.approval_mode)
        self._cb_approval.blockSignals(True)
        self._cb_approval.setCurrentIndex(max(idx, 0))
        self._cb_approval.blockSignals(False)
        self._te_notes.setPlainText(cfg.lab_notes)
        self._cb_report.setChecked(cfg.report_to_messenger)
        self._cb_report_finish.setChecked(cfg.report_on_finish)
        self._cb_restore.setChecked(cfg.restore_main_ui)
        self._sb_hours.setValue(cfg.max_run_hours)
        self._sb_turns.setValue(cfg.max_turns)
        self._sb_points.setValue(cfg.max_points_per_sweep)
        self._sb_min_tpp.setValue(cfg.min_time_per_point)

        def fixed(text):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        def check(on):
            item = QTableWidgetItem("")
            item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            return item

        t = self._tbl_sweep
        t.setRowCount(len(prof.sweep_values))
        for r, sv in enumerate(prof.sweep_values):
            pol = cfg.sweep_policy(sv.alias, sv.description)
            for c, item in enumerate([
                    check(pol.allowed), fixed(sv.alias), fixed(sv.description), fixed(sv.unit),
                    QTableWidgetItem(_num_text(pol.min_value)),
                    QTableWidgetItem(_num_text(pol.max_value)),
                    QTableWidgetItem(_num_text(pol.max_rate)),
                    QTableWidgetItem(pol.role)]):
                t.setItem(r, c, item)

        t = self._tbl_meas
        t.setRowCount(len(prof.measurements))
        for r, m in enumerate(prof.measurements):
            pol = cfg.measurement_policy(m.alias, m.description)
            for c, item in enumerate([
                    fixed(m.alias), fixed(m.description), fixed(m.unit),
                    QTableWidgetItem(_num_text(pol.abort_abs)), QTableWidgetItem(pol.role)]):
                t.setItem(r, c, item)

        t = self._tbl_second
        t.setRowCount(len(prof.second_sweep_channels))
        for r, ch in enumerate(prof.second_sweep_channels):
            pol = cfg.second_policy(ch.alias, ch.description)
            for c, item in enumerate([
                    check(pol.allowed), fixed(ch.alias), fixed(ch.description), fixed(ch.unit),
                    QTableWidgetItem(_num_text(pol.min_value)),
                    QTableWidgetItem(_num_text(pol.max_value)),
                    QTableWidgetItem(pol.role)]):
                t.setItem(r, c, item)

        t = self._tbl_write
        t.setRowCount(len(prof.write_cmds))
        for r, wc in enumerate(prof.write_cmds):
            pol = cfg.write_policy(wc.alias, wc.description)
            for c, item in enumerate([
                    check(pol.allowed), fixed(wc.alias), fixed(wc.description),
                    fixed("있음" if "{v}" in wc.cmd_set else "—"),
                    QTableWidgetItem(_num_text(pol.min_value)),
                    QTableWidgetItem(_num_text(pol.max_value)),
                    QTableWidgetItem(pol.role)]):
                t.setItem(r, c, item)

    @staticmethod
    def _text(table: QTableWidget, r: int, c: int) -> str:
        item = table.item(r, c)
        return item.text() if item is not None else ""

    @staticmethod
    def _checked(table: QTableWidget, r: int, c: int = 0) -> bool:
        item = table.item(r, c)
        return item is not None and item.checkState() == Qt.CheckState.Checked

    def _collect_settings(self) -> AutomationSettings:
        """표·입력칸 → 설정. 숫자 형식이 틀리면 ValueError (메시지는 사용자용)."""
        cfg, prof = self._cfg, self._main_win._active_profile
        cfg.sync_with_profile(prof)
        cfg.approval_mode = self._cb_approval.currentData() or "approve"
        cfg.lab_notes = self._te_notes.toPlainText()
        cfg.report_to_messenger = self._cb_report.isChecked()
        cfg.report_on_finish = self._cb_report_finish.isChecked()
        cfg.restore_main_ui = self._cb_restore.isChecked()
        cfg.max_run_hours = self._sb_hours.value()
        cfg.max_turns = self._sb_turns.value()
        cfg.max_points_per_sweep = self._sb_points.value()
        cfg.min_time_per_point = self._sb_min_tpp.value()

        t = self._tbl_sweep
        for r, sv in enumerate(prof.sweep_values[:t.rowCount()]):
            pol = cfg.sweep_policy(sv.alias, sv.description)
            what = f"Sweep '{sv.description}'"
            pol.allowed = self._checked(t, r)
            pol.min_value = _parse_opt_float(self._text(t, r, 4), what + " 최소")
            pol.max_value = _parse_opt_float(self._text(t, r, 5), what + " 최대")
            pol.max_rate = _parse_opt_float(self._text(t, r, 6), what + " 최대 rate")
            pol.role = self._text(t, r, 7).strip()
            if pol.allowed and (pol.min_value is None or pol.max_value is None):
                raise ValueError(f"{what}: 허용하려면 최소·최대를 모두 적어야 합니다.")
            if pol.min_value is not None and pol.max_value is not None \
                    and pol.min_value > pol.max_value:
                raise ValueError(f"{what}: 최소가 최대보다 큽니다.")
        t = self._tbl_meas
        for r, m in enumerate(prof.measurements[:t.rowCount()]):
            pol = cfg.measurement_policy(m.alias, m.description)
            pol.abort_abs = _parse_opt_float(self._text(t, r, 3),
                                             f"Measurement '{m.description}' 중단 값")
            pol.role = self._text(t, r, 4).strip()
        t = self._tbl_second
        for r, ch in enumerate(prof.second_sweep_channels[:t.rowCount()]):
            pol = cfg.second_policy(ch.alias, ch.description)
            what = f"Second '{ch.description}'"
            pol.allowed = self._checked(t, r)
            pol.min_value = _parse_opt_float(self._text(t, r, 4), what + " 최소")
            pol.max_value = _parse_opt_float(self._text(t, r, 5), what + " 최대")
            pol.role = self._text(t, r, 6).strip()
            if pol.allowed and (pol.min_value is None or pol.max_value is None):
                raise ValueError(f"{what}: 허용하려면 최소·최대를 모두 적어야 합니다.")
        t = self._tbl_write
        for r, wc in enumerate(prof.write_cmds[:t.rowCount()]):
            pol = cfg.write_policy(wc.alias, wc.description)
            what = f"Write '{wc.description}'"
            pol.allowed = self._checked(t, r)
            pol.min_value = _parse_opt_float(self._text(t, r, 4), what + " 최소")
            pol.max_value = _parse_opt_float(self._text(t, r, 5), what + " 최대")
            pol.role = self._text(t, r, 6).strip()
        return cfg

    def _save_settings(self, show_error: bool = True) -> bool:
        try:
            cfg = self._collect_settings()
            save_automation_settings(cfg, self._profile_name(), self._settings_dir)
        except ValueError as e:
            if show_error:
                QMessageBox.warning(self, "설정 오류", str(e))
            return False
        except Exception as e:
            if show_error:
                QMessageBox.critical(self, "설정 저장 실패", str(e))
            return False
        if self._mcp_executor is not None:
            self._mcp_executor.update_settings(cfg)
            self._apply_mcp_approval()
        return True

    def _on_apply_settings(self):
        if self._save_settings():
            self._append_log("status", "설정을 저장했습니다" + (
                " — Claude Code 연결에도 적용됐습니다." if self._mcp is not None else "."))

    def _on_approval_mode_changed(self, *_):
        self._cfg.approval_mode = self._cb_approval.currentData() or "approve"
        self._apply_mcp_approval()

    # ==================================================================
    # 실험 지식 탭
    # ==================================================================
    def _refresh_knowledge(self):
        t = self._tbl_rules
        t.blockSignals(True)
        entries = self._knowledge.entries()
        t.setRowCount(len(entries))
        for r, e in enumerate(entries):
            chk = QTableWidgetItem("")
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                         | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked if e.confirmed else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, e.id)
            cells = [chk, KIND_LABELS.get(e.kind, e.kind), e.title, e.condition, e.meaning,
                     e.action, f"+{e.supports} / −{e.contradicts}",
                     "사용자" if e.source == "user" else "AI"]
            for c, cell in enumerate(cells):
                item = cell if isinstance(cell, QTableWidgetItem) else QTableWidgetItem(cell)
                if c and e.kind == "safety":
                    item.setForeground(Qt.GlobalColor.red)
                if c == 2 and e.sample:
                    item.setToolTip(f"시료: {e.sample}")
                t.setItem(r, c, item)
        t.blockSignals(False)

        o = self._tbl_obs
        obs = list(reversed(self._knowledge.observations(limit=200)))
        o.setRowCount(len(obs))
        for r, ob in enumerate(obs):
            qty = ", ".join(f"{k}={v}" for k, v in ob.quantities.items())
            cells = [ob.time.replace("T", " "), ob.summary, qty, ob.interpretation,
                     f"+{len(ob.supports)} / −{len(ob.contradicts)}", "; ".join(ob.files)]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, ob.id)
                o.setItem(r, c, item)

    def _rule_id(self, row: int) -> Optional[str]:
        item = self._tbl_rules.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_rule_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        entry_id = item.data(Qt.ItemDataRole.UserRole)
        if entry_id:
            self._knowledge.update_entry(
                entry_id, confirmed=item.checkState() == Qt.CheckState.Checked)

    def _add_rule(self):
        dlg = _KnowledgeEditDialog(self)
        if dlg.exec():
            self._knowledge.add_entry(source="user", **dlg.fields())
            self._refresh_knowledge()

    def _edit_rule(self, row: int):
        entry_id = self._rule_id(row)
        entry = self._knowledge.get(entry_id) if entry_id else None
        if entry is None:
            return
        dlg = _KnowledgeEditDialog(self, entry)
        if dlg.exec():
            self._knowledge.update_entry(entry_id, **dlg.fields())
            self._refresh_knowledge()

    def _edit_selected_rule(self):
        row = self._tbl_rules.currentRow()
        if row >= 0:
            self._edit_rule(row)

    def _delete_rule(self):
        row = self._tbl_rules.currentRow()
        entry_id = self._rule_id(row) if row >= 0 else None
        if not entry_id:
            return
        title = self._text(self._tbl_rules, row, 2)
        if QMessageBox.question(self, "규칙 삭제", f"'{title}' 규칙을 지울까요?") \
                == QMessageBox.StandardButton.Yes:
            self._knowledge.delete_entry(entry_id)
            self._refresh_knowledge()

    def _delete_observation(self):
        row = self._tbl_obs.currentRow()
        item = self._tbl_obs.item(row, 0) if row >= 0 else None
        obs_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if obs_id and QMessageBox.question(self, "관찰 삭제", "선택한 관찰 기록을 지울까요?") \
                == QMessageBox.StandardButton.Yes:
            self._knowledge.delete_observation(obs_id)
            self._refresh_knowledge()

    # ==================================================================
    # 상태 표시
    # ==================================================================
    def _update_status_label(self):
        if not sdk_available():
            text = "anthropic 패키지 없음 — pip install -r requirements-optional.txt"
        elif self._llm_cfg.api_key:
            text = f"모델 {self._llm_cfg.model} · effort {self._llm_cfg.effort} · API 키 설정됨"
        elif has_env_credentials():
            text = f"모델 {self._llm_cfg.model} · effort {self._llm_cfg.effort} · 환경변수 키"
        else:
            text = "API 키 없음 — [LLM 설정] 에서 입력"
        self._lbl_status.setText(text)

    def _open_llm_settings(self):
        dlg = _LlmSettingsDialog(self._llm_cfg, self._settings_dir, self)
        if dlg.exec():
            self._update_status_label()

    def _append_log(self, kind: str, text: str):
        color = _LOG_COLORS.get(kind, "#d4d4d4")
        prefix = {
            "user": "사용자 ▶ ", "assistant": "AI ▶ ", "progress": "  … ",
            "tool_call": "  ⚙ ", "approval": "  ✋ ", "status": "  · ",
            "error": "  ✗ ", "message": "  ✉ 메신저 ▶ ", "finished": "■ ",
            "knowledge": "  ✎ ", "ntfy": "  [ntfy] ",
        }.get(kind, "")
        stamp = datetime.now().strftime("%H:%M:%S")
        if kind in ("assistant", "tool_call", "error", "message", "finished"):
            first = text.splitlines()[0][:120] if text else ""
            self._recent.append(f"{stamp} {first}")
        safe = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>"))
        self._log_view.append(
            f'<span style="color:#555555">{stamp}</span> '
            f'<span style="color:{color}">{prefix}{safe}</span>')
        self._log_view.moveCursor(QTextCursor.MoveOperation.End)

    @staticmethod
    def _result_text(content) -> str:
        if not isinstance(content, dict):
            return str(content)[:600]
        head = []
        for key in ("status", "error", "reason", "detail", "file", "delivered", "note",
                    "recorded", "job_id"):
            if key in content:
                head.append(f"{key}={content[key]}")
        data = content.get("data")
        if isinstance(data, dict) and "points" in data:
            head.append(f"points={data['points']}")
        if "values" in content:
            head.append(json.dumps(content["values"], ensure_ascii=False)[:500])
        if "rules" in content:
            head.append(f"규칙 {len(content['rules'])}개, 관찰 "
                        f"{len(content.get('observations', []))}개")
        return "  ".join(head) or json.dumps(content, ensure_ascii=False, default=str)[:600]

    def _set_running_ui(self, running: bool):
        self._btn_start.setEnabled(not running)
        self._btn_stop.setEnabled(running or (self._mcp is not None and self._mcp.is_busy()))
        self._te_request.setReadOnly(running)
        self._btn_send.setEnabled(running or self._agent is not None)
        self._le_followup.setEnabled(running or self._agent is not None)

    # ==================================================================
    # 공통 — 실행 가능 여부, 실행기
    # ==================================================================
    def _agent_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_idle(self) -> bool:
        """앱 내장 실행도, Claude Code 작업도 돌고 있지 않은지 (메인 창이 Start 잠금에 쓴다)."""
        return not self._agent_running() and not (self._mcp is not None and self._mcp.is_busy())

    def _instrument_info(self) -> dict:
        info = {}
        reg = self._main_win._registry
        for alias in reg.list_aliases():
            cfg = reg.get_config(alias)
            if cfg is None:
                continue
            info[alias] = {
                "driver": resolve_class_path(cfg.class_name).rsplit(".", 1)[-1],
                "interface": cfg.interface_type,
            }
        return info

    def _main_defaults(self) -> dict:
        mw = self._main_win
        return {
            "rate_per_min": mw._sweep_config.sweep_rate,
            "time_per_point_s": mw._sweep_config.time_per_point,
            "main_folder": mw._le_main_folder.text(),
            "date_in_path": mw._cb_save_date.isChecked(),
        }

    def _profile_identity(self) -> str:
        return json.dumps(self._main_win._active_profile.model_dump(mode="json"),
                          sort_keys=True, ensure_ascii=False)

    def _new_run_dir(self, suffix: str = "") -> Path:
        folder = self._main_win._le_main_folder.text().strip()
        base = Path(folder) / "ai_runs" if folder else self._settings_dir / "logs" / "ai_runs"
        path = base / (datetime.now().strftime("%Y-%m-%d_%H%M%S") + suffix)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _build_executor(self, for_mcp: bool, stop_event: Optional[threading.Event] = None,
                        run_dir: Optional[Path] = None) -> LabExecutor:
        mw = self._main_win
        executor = LabExecutor(
            session=mw._session, profile=mw._active_profile.model_copy(deep=True),
            settings=self._cfg, backend=self._bridge,
            stop_event=stop_event or threading.Event(), emit=self._emit_from_thread,
            messenger=self._send_messenger,
            threshold=mw._app_config.global_threshold,
            parallel=mw._app_config.parallel_measurement,
            instruments=self._instrument_info(), main_defaults=self._main_defaults(),
            save_root=mw._le_main_folder.text().strip(), knowledge=self._knowledge,
            profile_name=self._profile_name(),
            run_dir=str(run_dir or (self._mcp_run_dir if for_mcp else "") or ""))
        executor.deadline = time.monotonic() + self._cfg.max_run_hours * 3600.0
        executor.confirm_knowledge_on_save = self._cfg.approval_mode == "approve"
        return executor

    # ==================================================================
    # Claude Code (MCP 서버)
    # ==================================================================
    def mcp_running(self) -> bool:
        return self._mcp is not None

    def _toggle_mcp(self):
        if self._mcp is None:
            self.start_mcp_server()
        else:
            self.stop_mcp_server()

    def start_mcp_server(self, show_error: bool = True) -> bool:
        if self._mcp is not None:
            return True
        if not self._save_settings(show_error=show_error):
            return False
        port = self._sb_port.value()
        try:
            token = load_or_create_token(self._settings_dir)
            self._mcp_run_dir = None
            self._mcp_executor = self._build_executor(for_mcp=True)
            server = McpLabServer(
                executor_provider=lambda: self._mcp_executor,
                token=token, port=port,
                busy_reason=lambda: ("앱 내장 AI 실행이 진행 중입니다"
                                     if self._agent_running() else ""),
                on_event=self._emit_from_thread, journal=self._mcp_journal,
                inbox=self.take_remote_messages)
            server.start()
        except OSError as e:
            self._mcp_executor = None
            if show_error:
                QMessageBox.critical(self, "연결 서버 시작 실패",
                                     f"포트 {port} 를 열 수 없습니다: {e}\n"
                                     "다른 프로그램이 쓰고 있으면 포트를 바꾸세요 "
                                     "(.mcp.json 의 주소도 함께).")
            get_logger().warning("[AI] MCP 서버 시작 실패: %s", e)
            return False
        self._mcp = server
        self._apply_mcp_approval()
        if self._llm_cfg.mcp_port != port:
            self._llm_cfg.mcp_port = port
            save_llm_settings(self._llm_cfg, self._settings_dir)
        self._append_log("status", f"Claude Code 연결 서버를 켰습니다: {server.url}")
        get_logger().info("[AI] MCP server started at %s", server.url)
        self._update_mcp_ui()
        return True

    def stop_mcp_server(self):
        server, self._mcp = self._mcp, None
        if server is None:
            return
        server.stop()
        self._mcp_executor = None
        if self._cfg.restore_main_ui and not self._agent_running() \
                and self._bridge.restore_snapshot():
            self._append_log("status", "메인 창 sweep·저장 설정을 원래대로 되돌렸습니다.")
        self._append_log("status", "Claude Code 연결 서버를 껐습니다.")
        self._update_mcp_ui()

    def _apply_mcp_approval(self):
        if self._mcp is not None:
            approve = self._cb_approval.currentData() == "approve"
            self._mcp.approve = self._approve_mcp if approve else None

    def _update_mcp_ui(self):
        on = self._mcp is not None
        self._btn_mcp.setText("연결 서버 끄기" if on else "연결 서버 켜기")
        self._sb_port.setEnabled(not on)
        if on:
            client = self._mcp.last_client or "아직 연결 없음"
            job = self._mcp.current_job_name()
            self._lbl_mcp.setText(
                f"켜짐 — {self._mcp.url}   ·   Claude Code: {client}"
                + (f"   ·   실행 중: {job}" if job else ""))
            self._lbl_mcp.setStyleSheet("color: #4ec9b0;")
        else:
            self._lbl_mcp.setText("꺼짐 — 켜면 Claude Code 가 이 프로그램의 측정 도구를 씁니다.")
            self._lbl_mcp.setStyleSheet("color: #888888;")
        self._btn_stop.setEnabled(self._agent_running() or (on and self._mcp.is_busy()))

    def _on_mcp_autostart_toggled(self, checked: bool):
        self._llm_cfg.mcp_enabled = checked
        try:
            save_llm_settings(self._llm_cfg, self._settings_dir)
        except Exception as e:
            get_logger().warning("[AI] llm.yaml 저장 실패: %s", e)

    def _mcp_add_command(self) -> str:
        token = load_or_create_token(self._settings_dir)
        url = self._mcp.url if self._mcp else f"http://127.0.0.1:{self._sb_port.value()}/mcp"
        return (f'claude mcp add --transport http --scope user {SERVER_NAME} {url} '
                f'--header "Authorization: Bearer {token}"')

    def _copy_mcp_command(self):
        QApplication.clipboard().setText(self._mcp_add_command())
        self._append_log("status", "연결 명령을 클립보드에 복사했습니다 (토큰 포함 — 남에게 "
                                   "보내지 마세요).")

    def _regenerate_token(self):
        if QMessageBox.question(
                self, "토큰 새로 만들기",
                "새 토큰을 만들면 지금 연결된 Claude Code 는 다시 연결해야 합니다.\n"
                "(저장소 .mcp.json 으로 연결한 경우 자동으로 새 토큰을 읽습니다.)\n계속할까요?") \
                != QMessageBox.StandardButton.Yes:
            return
        token = regenerate_token(self._settings_dir)
        if self._mcp is not None:
            self._mcp.set_token(token)
        self._append_log("status", "새 연결 토큰을 만들었습니다.")

    def _mcp_journal(self, record: dict):
        if self._mcp_run_dir is None:
            try:
                self._mcp_run_dir = self._new_run_dir("_claude-code")
                if self._mcp_executor is not None:
                    self._mcp_executor.run_dir = str(self._mcp_run_dir)
            except Exception as e:
                get_logger().warning("[AI] 실행 기록 폴더 생성 실패: %s", e)
                return
        self._write_journal(self._mcp_run_dir, record)

    def _approve_mcp(self, name: str, tool_input: dict, preview: str) -> ApprovalDecision:
        executor = self._mcp_executor
        stop = executor.stop_event if executor is not None else threading.Event()
        return self._wait_for_approval(name, tool_input, preview, stop)

    # ==================================================================
    # 앱 내장 실행
    # ==================================================================
    def _cannot_run_reason(self) -> str:
        """앱 내장 실행을 시작할 수 없는 사유 (대화상자 없이 — 휴대폰 지시에도 쓴다)."""
        if self._mcp is not None and self._mcp.is_busy():
            return "Claude Code 가 측정 작업을 진행 중입니다."
        if not sdk_available():
            return ("앱 내장 실행에는 anthropic 패키지가 필요합니다 "
                    "(pip install -r requirements-optional.txt). API 키 없이 쓰려면 "
                    "Claude Code 로 실행하세요.")
        busy = self._main_win.measurement_busy_reason()
        if busy:
            return f"{busy} 이(가) 실행 중입니다."
        if not self._main_win._le_main_folder.text().strip():
            return ("메인 창 Save Settings 의 Main Folder 가 비어 있습니다. AI 는 그 아래에만 "
                    "데이터를 저장합니다.")
        return ""

    def _check_can_run(self) -> bool:
        reason = self._cannot_run_reason()
        if reason:
            QMessageBox.warning(self, "시작할 수 없음", reason)
            return False
        if not self._llm_cfg.api_key and not has_env_credentials():
            answer = QMessageBox.question(
                self, "API 키 없음",
                "API 키가 설정되지 않았고 ANTHROPIC_API_KEY 환경변수도 없습니다.\n"
                "다른 자격증명(ant auth login 등)으로 시도할까요?\n\n"
                "API 키 없이 쓰려면 'Claude Code 로 실행' 을 쓰세요.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return False
        return True

    def _on_start_clicked(self):
        if self._agent_running():
            return
        request = self._te_request.toPlainText().strip()
        if not request:
            QMessageBox.information(self, "요청 없음", "측정 요청을 입력하세요.")
            return
        if not self._save_settings() or not self._check_can_run():
            return
        err = self._start_run(request)
        if err:
            QMessageBox.warning(self, "시작할 수 없음", err)

    def _start_run(self, request: str) -> str:
        """새 실행 시작 (검사는 끝난 상태). 반환: 실패 사유, 성공이면 빈 문자열."""
        cfg = self._cfg
        if not any(p.allowed for p in cfg.sweep_channels + cfg.second_channels):
            self._append_log("status", "AI 가 움직여도 되는 채널이 하나도 허용되지 않았습니다. "
                                       "읽기·시간 측정만 할 수 있습니다.")
        try:
            client = make_client(self._llm_cfg.api_key, self._llm_cfg.request_timeout_s)
        except LlmUnavailable as e:
            return str(e)
        try:
            run_dir = self._new_run_dir()
        except Exception as e:
            return f"실행 기록 폴더를 만들지 못했습니다: {e}"

        self._log_view.clear()
        self._run_dir = run_dir
        self._run_request = request
        self._profile_key = self._profile_identity()
        self._bridge.forget_snapshot()

        self._stop_event = threading.Event()
        executor = self._build_executor(for_mcp=False, stop_event=self._stop_event,
                                        run_dir=run_dir)
        agent = AgentLoop(
            client=client, model=self._llm_cfg.model, effort=self._llm_cfg.effort,
            max_tokens=self._llm_cfg.max_tokens, system_prompt=SYSTEM_PROMPT,
            tools=TOOL_SPECS, execute=executor.execute, preview=executor.preview,
            emit=self._emit_from_thread, approve=self._approve_from_thread,
            journal=self._journal_write, stop_event=self._stop_event)
        executor.status_provider = lambda: {"llm_calls_used": agent.turns_used,
                                            "llm_calls_max": agent.max_turns}
        self._executor, self._agent = executor, agent
        self._apply_budget()

        lab_state = build_lab_context(executor.profile, cfg, executor._instruments,
                                      executor._main_defaults)
        self._journal_write({"t": datetime.now().isoformat(timespec="seconds"),
                             "type": "run_start", "request": request,
                             "model": self._llm_cfg.model, "effort": self._llm_cfg.effort,
                             "approval_mode": cfg.approval_mode, "lab_state": lab_state})
        self._append_log("user", request)
        self._append_log("status", f"실행 기록: {run_dir}")
        self._launch(first_user_message(request, lab_state, self._knowledge.digest()))
        return ""

    def _apply_budget(self):
        cfg = self._cfg
        deadline = time.monotonic() + cfg.max_run_hours * 3600.0
        self._agent.approval_mode = cfg.approval_mode
        self._agent.max_turns = self._agent.turns_used + cfg.max_turns
        self._agent.deadline = deadline
        self._executor.deadline = deadline
        self._executor.confirm_knowledge_on_save = cfg.approval_mode == "approve"

    def _launch(self, text: str):
        self._last_error = ""
        agent = self._agent
        self._thread = threading.Thread(target=self._thread_main, args=(agent, text),
                                        name="ai-auto-measure", daemon=True)
        self._thread.start()
        self._set_running_ui(True)
        self._main_win._btn_start.setEnabled(False)

    def _on_send_followup(self):
        text = self._le_followup.text().strip()
        if not text or self._agent is None:
            return
        self._le_followup.clear()
        if self._agent_running():
            self._agent.steer(text)
            self._append_log("status", f"추가 지시를 다음 단계에 전달합니다: {text}")
            return
        if not self._save_settings() or not self._check_can_run():
            return
        err = self._continue_run(text)
        if err:
            QMessageBox.warning(self, "이어서 실행할 수 없음", err)

    def _continue_run(self, text: str) -> str:
        """끝난 대화를 같은 기록 위에서 이어 간다. 반환: 실패 사유, 성공이면 빈 문자열."""
        if self._agent is None:
            return "이어 갈 대화가 없습니다."
        if self._profile_identity() != self._profile_key:
            return ("메인 창 프로파일 구성이 바뀌어 이전 대화를 이어 갈 수 없습니다. "
                    "새 측정으로 시작하세요.")
        self._stop_event = threading.Event()
        self._agent.stop_event = self._stop_event
        self._executor.set_stop_event(self._stop_event)
        self._executor.update_settings(self._cfg)
        self._apply_budget()
        self._append_log("user", text)
        self._launch(text)
        return ""

    def _on_new_conversation(self):
        if self._agent_running():
            return
        self._agent = None
        self._executor = None
        self._log_view.clear()
        self._set_running_ui(False)

    def _on_stop_clicked(self) -> bool:
        stopped = False
        if self._agent_running():
            self._stop_event.set()
            stopped = True
        if self._mcp is not None and self._mcp.stop_current_job():
            stopped = True
        if self._pending is not None:
            self._pending.decide(False, "사용자가 실행을 중지했습니다.")
            self._approval_card.setVisible(False)
            self._pending = None
        if stopped:
            self._append_log("status", "중지 요청 — 진행 중인 조작을 멈추는 중입니다.")
        return stopped

    def shutdown_threads(self):
        """앱 종료 시 — 중지를 걸고 장비 조작이 끝날 때까지 잠시 기다린다."""
        self._save_settings(show_error=False)
        if self._ntfy_sub is not None:
            self._ntfy_sub.stop()
            self._ntfy_sub = None
        if self._pending is not None:
            self._pending.decide(False, "프로그램 종료")
        if self._mcp is not None:
            self._mcp.stop()
            self._mcp = None
        if self._agent_running():
            self._stop_event.set()
            self._thread.join(timeout=30.0)

    # ==================================================================
    # 휴대폰 (ntfy) — 지시 받기·답하기
    # ==================================================================
    def on_ntfy_settings_changed(self):
        """ntfy 설정이 바뀌었거나 창이 생길 때 — 지시 주제 구독을 (다시) 연다."""
        if self._ntfy_sub is not None:
            self._ntfy_sub.stop()
            self._ntfy_sub = None
        self._ntfy_state = ""
        try:
            d = load_alarm_delivery()
        except Exception:
            d = None
        if d is not None and d.ntfy_commands_ready():
            self._ntfy_sub = NtfySubscriber(
                d.ntfy_server, d.ntfy_command_topic, self._on_ntfy_message_thread,
                token=d.ntfy_token, on_status=self._on_ntfy_status_thread)
            self._ntfy_sub.start()
        self._update_ntfy_label()

    def _update_ntfy_label(self):
        try:
            d = load_alarm_delivery()
        except Exception:
            d = None
        if d is None or not (d.ntfy_ready() or d.ntfy_commands_ready()):
            self._lbl_ntfy.setText("휴대폰(ntfy): 꺼짐 — 설정하면 보고·승인 요청이 휴대폰으로 "
                                   "오고 휴대폰에서 지시할 수 있습니다.")
            self._lbl_ntfy.setStyleSheet("color: #888888;")
            return
        parts = [f"보고 → {d.ntfy_topic}" if d.ntfy_ready() else "보고 꺼짐"]
        if d.ntfy_commands_ready():
            state = {"connected": "연결됨"}.get(self._ntfy_state, self._ntfy_state or "연결 중")
            parts.append(f"지시 ← {d.ntfy_command_topic} ({state})")
        self._lbl_ntfy.setText("휴대폰(ntfy): " + "   ·   ".join(parts))
        self._lbl_ntfy.setStyleSheet(
            "color: #4ec9b0;" if self._ntfy_state in ("", "connected") else "color: #d7ba7d;")

    def _on_ntfy_message_thread(self, event: dict):
        self._signals.remote.emit(str(event.get("message", "")))

    def _on_ntfy_status_thread(self, status: str):
        state = "connected" if status == "connected" else "끊김 — 다시 연결 중"
        if state != self._ntfy_state:
            self._ntfy_state = state
            text = ("ntfy 지시 주제에 연결했습니다." if state == "connected"
                    else f"ntfy 연결이 끊겼습니다 ({status}). 다시 연결합니다.")
            self._signals.event.emit(AgentEvent("ntfy", text))

    def _reply_remote(self, text: str):
        """휴대폰 지시에 대한 답 — ntfy 로만 (작업 스레드에서 보내 GUI 를 막지 않는다)."""
        def run():
            err = self._alarm.send_ntfy(text, title="Pythonization AI", tags=["speech_balloon"])
            if err:
                get_logger().warning("[AI] ntfy 답장 실패: %s", err)
        threading.Thread(target=run, daemon=True).start()

    def take_remote_messages(self) -> List[str]:
        """Claude Code 로 넘길 휴대폰 지시를 꺼낸다 (MCP 서버가 도구 결과에 붙인다)."""
        with self._inbox_lock:
            messages, self._remote_inbox = self._remote_inbox, []
        return messages

    def _claude_code_active(self) -> bool:
        mcp = self._mcp
        if mcp is None:
            return False
        return bool(mcp.is_busy() or (mcp.last_activity and
                                      time.time() - mcp.last_activity < _CLAUDE_CODE_ACTIVE_S))

    def _status_summary(self) -> str:
        lines = []
        if self._agent_running():
            lines.append("앱 내장 AI 실행 중")
        if self._mcp is not None:
            job = self._mcp.current_job_name()
            lines.append(f"Claude Code 연결 켜짐 ({self._mcp.last_client or '연결 기록 없음'})"
                         + (f" — 작업: {job}" if job else ""))
        if not lines:
            lines.append("AI 대기 중")
        mw = self._main_win
        if mw._running:
            lines.append(f"메인 sweep 진행 중 → {mw._sweep_config.sweep_to:g} "
                         f"(현재 {mw._le_source_value.text()})")
        busy = mw.measurement_busy_reason()
        if busy and busy != "Main sweep":
            lines.append(f"다른 측정: {busy}")
        if self._pending is not None:
            lines.append(f"승인 대기 #{self._pending.short_id}: "
                         f"{self._pending.preview.splitlines()[0]}")
        if self._recent:
            lines.append("최근:")
            lines.extend(f"- {r}" for r in list(self._recent)[-5:])
        return "\n".join(lines)

    @Slot(str)
    def _on_remote_message(self, text: str):
        """휴대폰(ntfy 지시 주제)에서 온 글을 처리한다 (GUI 스레드)."""
        cmd = parse_remote(text)
        if cmd.kind == "empty":
            return
        self._append_log("user", f"(휴대폰) {text}")
        self._write_journal(self._run_dir or self._mcp_run_dir,
                            {"type": "remote", "text": text, "kind": cmd.kind})
        if cmd.kind == "stop":
            stopped = self._on_stop_clicked()
            self._reply_remote("중지했습니다." if stopped else "멈출 작업이 없습니다.")
        elif cmd.kind == "status":
            self._reply_remote(self._status_summary())
        elif cmd.kind == "help":
            self._reply_remote(HELP_TEXT)
        elif cmd.kind in ("approve", "reject"):
            req = self._pending
            if req is None or req.short_id != cmd.arg:
                self._reply_remote(f"#{cmd.arg} 번 승인 대기가 없습니다.")
                return
            self._le_approval_comment.setText(
                cmd.text if cmd.kind == "reject" else "휴대폰에서 승인")
            self._decide(cmd.kind == "approve")
            self._reply_remote(("승인했습니다" if cmd.kind == "approve" else "거부했습니다")
                               + f" (#{cmd.arg}).")
        else:
            self._route_instruction(cmd.text)

    def _route_instruction(self, text: str):
        """휴대폰 지시를 지금 일하고 있는 AI 에게 보낸다."""
        if self._agent_running():
            self._agent.steer(f"(휴대폰 ntfy) {text}")
            self._reply_remote("실행 중인 AI 에게 전달했습니다. 다음 단계에서 반영합니다.")
            return
        if self._claude_code_active():
            with self._inbox_lock:
                self._remote_inbox.append(text)
            self._reply_remote("Claude Code 에 전달합니다 — 다음 도구 호출 때 받습니다. "
                               "Claude Code 가 대기 중이면 데스크톱 앱에서 "
                               "'휴대폰 지시 확인해' 라고 한 번 말해 주세요.")
            return
        # 앱 내장 실행으로 시작하거나 이어 간다 (대화상자 없이)
        reason = self._cannot_run_reason()
        if not reason and not self._save_settings(show_error=False):
            reason = "AI 자동 측정 창의 설정에 오류가 있습니다."
        if not reason:
            reason = (self._continue_run(text) if self._agent is not None
                      else self._start_run(text))
        if not reason:
            self._reply_remote("지시를 받아 AI 가 시작했습니다. 진행 상황을 보고하겠습니다.")
            return
        if self._mcp is not None:
            with self._inbox_lock:
                self._remote_inbox.append(text)
            self._reply_remote(f"앱 내장 실행은 할 수 없습니다 ({reason}). Claude Code 연결로 "
                               "전달해 두었습니다 — 데스크톱 앱에서 이어서 지시해 주세요.")
        else:
            self._reply_remote(f"지시를 실행하지 못했습니다: {reason}")

    # ==================================================================
    # 작업 스레드에서 부르는 것들
    # ==================================================================
    def _emit_from_thread(self, event: AgentEvent):
        self._signals.event.emit(event)

    def _notify_approval_by_ntfy(self, req: "_ApprovalRequest") -> bool:
        """휴대폰 알림에 [승인]/[거부] 버튼을 달아 보낸다. 보냈으면 True."""
        try:
            d = load_alarm_delivery()
        except Exception:
            return False
        if not (d.ntfy_ready() and d.ntfy_commands_ready()):
            return False
        base = d.ntfy_public_url or d.ntfy_server
        actions = [
            http_action("승인", base, d.ntfy_command_topic, f"/approve {req.short_id}",
                        d.ntfy_token),
            http_action("거부", base, d.ntfy_command_topic, f"/reject {req.short_id}",
                        d.ntfy_token),
        ]
        err = self._alarm.send_ntfy(f"승인 요청 #{req.short_id}\n{req.preview}",
                                    title="AI 승인 요청", priority=4, tags=["raised_hand"],
                                    actions=actions)
        if err:
            get_logger().warning("[AI] 승인 요청 ntfy 전송 실패: %s", err)
        return err is None

    def _wait_for_approval(self, name: str, tool_input: dict, preview: str,
                           stop_event: threading.Event) -> ApprovalDecision:
        req = _ApprovalRequest(name, tool_input, preview)
        self._signals.approval.emit(req)
        sent_to_phone = self._notify_approval_by_ntfy(req)
        t0 = time.monotonic()
        reminded = sent_to_phone
        while not req.done.wait(0.2):
            if stop_event.is_set():
                req.decide(False, "사용자가 실행을 중지했습니다.")
                break
            if not reminded and time.monotonic() - t0 > _APPROVAL_REMIND_S:
                reminded = True
                if self._cfg.report_to_messenger:
                    self._send_messenger(f"AI 가 승인을 기다립니다 (#{req.short_id}):\n"
                                         f"{preview}", False)
        return req.decision or ApprovalDecision(False, "승인 없음")

    def _approve_from_thread(self, name: str, tool_input: dict,
                             preview: str) -> ApprovalDecision:
        return self._wait_for_approval(name, tool_input, preview, self._stop_event)

    def _send_messenger(self, text: str, urgent: bool) -> List[str]:
        errors = self._alarm.send_message(text, urgent=urgent, title="Pythonization AI")
        if errors:
            get_logger().warning("[AI] 메신저 전송 실패: %s", errors)
        return errors

    def _write_journal(self, run_dir: Optional[Path], record: dict):
        if run_dir is None:
            return
        record = {"t": datetime.now().isoformat(timespec="seconds"), **record}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._journal_lock:
            try:
                with open(run_dir / "journal.jsonl", "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                get_logger().warning("[AI] journal 기록 실패: %s", e)

    def _journal_write(self, record: dict):
        self._write_journal(self._run_dir, record)

    def _thread_main(self, agent: AgentLoop, text: str):
        status = "error"
        try:
            status = agent.run(text)
        except Exception as exc:     # noqa: BLE001 — 스레드 밖으로 새면 아무도 모른다
            get_logger().exception("AI auto-measure thread failed")
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._emit_from_thread(AgentEvent("error", self._last_error))
        finally:
            try:
                self._after_run(agent, status)
            except Exception:
                get_logger().exception("AI auto-measure finalize failed")
            self._signals.finished.emit(status)

    def _after_run(self, agent: AgentLoop, status: str):
        """최종 보고 전송 + report.md (작업 스레드 — 네트워크 대기가 GUI 를 막지 않게)."""
        cfg = self._cfg
        label = _STATUS_LABELS.get(status, status)
        files = list(self._executor.files) if self._executor else []
        if cfg.report_to_messenger:
            if status == "finished" and cfg.report_on_finish and agent.final_text:
                self._send_messenger(f"[최종 보고]\n{agent.final_text}", False)
            elif status in ("error", "budget", "refusal"):
                body = f"AI 자동 측정이 멈췄습니다: {label}"
                if self._last_error:
                    body += f"\n{self._last_error}"
                self._send_messenger(body, True)
        if self._run_dir is None:
            return
        u = agent.usage
        lines = [
            f"# AI 자동 측정 보고 — {datetime.now():%Y-%m-%d %H:%M}",
            "", f"- 상태: {label}",
            f"- 모델: {self._llm_cfg.model} (effort {self._llm_cfg.effort})",
            f"- LLM 호출: {u.calls}회, 입력 {u.input_tokens} / 출력 {u.output_tokens} 토큰 "
            f"(캐시 읽기 {u.cache_read_input_tokens})",
            "", "## 요청", "", self._run_request,
            "", "## 저장한 데이터", "",
            *([f"- {f}" for f in files] or ["- (없음)"]),
            "", "## AI 최종 보고", "", agent.final_text or "(없음)", "",
        ]
        try:
            with open(self._run_dir / "report.md", "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            get_logger().warning("[AI] report.md 기록 실패: %s", e)

    # ==================================================================
    # GUI 슬롯
    # ==================================================================
    @Slot(object)
    def _on_agent_event(self, ev: AgentEvent):
        if ev.kind == "usage":
            d = ev.data
            self._lbl_usage.setText(
                f"LLM {d.get('calls', 0)}회 · 입력 {d.get('input_tokens', 0):,} · "
                f"출력 {d.get('output_tokens', 0):,} · 캐시 {d.get('cache_read_input_tokens', 0):,}")
            return
        if ev.kind == "tool_result":
            data = ev.data
            kind = "error" if data.get("is_error") else "status"
            self._append_log(kind, f"{data.get('name')} 결과: "
                                   f"{self._result_text(data.get('content'))}")
            self._after_mcp_activity()
            return
        if ev.kind == "error":
            self._last_error = ev.text
        if ev.kind == "finished":
            return      # 최종 텍스트는 assistant 이벤트로 이미 표시됐다
        self._append_log(ev.kind, ev.text)
        if ev.kind == "knowledge":
            self._refresh_knowledge()
        if ev.kind == "ntfy":
            self._update_ntfy_label()
        if ev.kind == "tool_call":
            self._after_mcp_activity()

    def _after_mcp_activity(self):
        """Claude Code 작업 시작·끝에 맞춰 메인 창 Start 잠금과 표시를 갱신한다."""
        if self._mcp is None:
            return
        self._update_mcp_ui()
        if self._mcp.is_busy():
            self._main_win._btn_start.setEnabled(False)
        elif not self._agent_running() and not self._main_win._running:
            self._main_win._btn_start.setEnabled(True)
        # 작업 스레드가 아직 마무리 중일 수 있다 — 잠시 뒤 한 번 더 확인
        QTimer.singleShot(300, self._update_mcp_ui)

    @Slot(object)
    def _on_approval_requested(self, req: _ApprovalRequest):
        if req.done.is_set():
            return
        self._pending = req
        self._lbl_approval.setText(f"#{req.short_id}\n{req.preview}")
        self._le_approval_comment.clear()
        self._approval_card.setVisible(True)
        self.show()
        self.raise_()
        QApplication.alert(self)

    def _on_approve_clicked(self):
        self._decide(True)

    def _on_reject_clicked(self):
        self._decide(False)

    def _decide(self, approved: bool):
        req = self._pending
        if req is None:
            return
        req.decide(approved, self._le_approval_comment.text().strip())
        self._pending = None
        self._approval_card.setVisible(False)

    @Slot(str)
    def _on_agent_finished(self, status: str):
        self._thread = None
        self._pending = None
        self._approval_card.setVisible(False)
        if self._cfg.restore_main_ui and self._mcp is None and self._bridge.restore_snapshot():
            self._append_log("status", "메인 창 sweep·저장 설정을 원래대로 되돌렸습니다.")
        if not self._main_win._running and self.is_idle():
            self._main_win._btn_start.setEnabled(True)
        self._set_running_ui(False)
        self._append_log("finished", f"AI 자동 측정 종료 — {_STATUS_LABELS.get(status, status)}")

    def _open_run_folder(self):
        target = self._run_dir or self._mcp_run_dir
        if target is None:
            folder = self._main_win._le_main_folder.text().strip()
            target = Path(folder) / "ai_runs" if folder else None
        if target is None or not Path(target).exists():
            QMessageBox.information(self, "폴더 없음", "아직 실행 기록 폴더가 없습니다.")
            return
        try:
            os.startfile(str(target))    # type: ignore[attr-defined]  (Windows)
        except AttributeError:
            subprocess.Popen(["xdg-open", str(target)])

    # ==================================================================
    # 창 수명
    # ==================================================================
    def closeEvent(self, event):
        """닫아도 숨기기만 한다 — 실행 중인 AI 측정·연결 서버는 계속된다."""
        self._save_settings(show_error=False)
        if not self.is_idle() or self._mcp is not None:
            self._main_win._log("AI 자동 측정 창을 숨겼습니다. AI 실행·연결 서버는 계속됩니다.",
                                color="#d2a8ff")
        event.ignore()
        self.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            event.accept()      # 실수로 닫히지 않게
            return
        super().keyPressEvent(event)
