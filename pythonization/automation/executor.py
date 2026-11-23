"""도구 실행기 — LLM 의 도구 호출을 프로그램 내장 기능으로 옮긴다.

측정 경로:
  run_sweep / run_time_trace  → 메인 창 sweep (LabBackend.run_main_sweep).
                                 그래프·Data 창·.dat 저장·메타데이터·통신 오류 자동 재개가
                                 사람이 Start 를 눌렀을 때와 똑같이 동작한다.
  move_channel                → SweepWorker 스텝 로직 (Cycle Sweep 의 PRE_INIT 과 같은 방식,
                                 safety ramp 포함). 기록 없음.
  set_second_axis             → SecondChannelWorker (Double Sweep 의 advance 4종 + 워치독).
  read_values                 → SweepWorker 측정 로직 (TSP batch·병렬 측정·임계값 처리).
  send_write_command          → 프로파일 write command 를 InstrumentSession 으로.

모든 명령은 프로파일(= VISA 라이브러리)에 등록된 것만 쓴다. LLM 이 명령 문자열을
만들 길은 없다 (규칙 2).

워커는 이 실행기를 부른 스레드(에이전트 스레드)에서 동기로 돈다. 신호는
DirectConnection 으로 받아 같은 스레드에서 처리한다 — GUI 위젯은 건드리지 않는다.
"""
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from PySide6.QtCore import Qt

from pythonization.automation.agent import AgentEvent, ToolOutcome
from pythonization.automation.knowledge import KIND_LABELS, KnowledgeStore
from pythonization.automation.lab_context import build_lab_context
from pythonization.automation.safety import (
    SafetyPolicy,
    SafetyViolation,
    sanitize_file_prefix,
    sanitize_sub_folder,
)
from pythonization.automation.settings import AutomationSettings
from pythonization.automation.summarize import summarize_dat
from pythonization.instruments.errors import humanize_error
from pythonization.instruments.parameter import MeasurementParameter
from pythonization.measurement.channel import TIME_CHANNEL, sweep_channel_from_instantiated
from pythonization.measurement.second_channel_worker import (
    SecondChannelRequest,
    SecondChannelWorker,
)
from pythonization.measurement.sweep_worker import StepRequest, StepResult, SweepWorker

#: 메인 창 Time 채널의 버튼 ID (MainWindow._TIME_ID 와 같다)
TIME_CHANNEL_ID = -2
_MAX_MESSAGES_PER_RUN = 40
_MAX_MESSAGE_CHARS = 3500
_MOVE_PROGRESS_INTERVAL_S = 10.0
_MAX_KNOWLEDGE_TEXT = 2000

#: 장비 I/O 를 하는 도구 — 한 번에 하나만 실행한다 (같은 워커 객체를 쓰므로)
INSTRUMENT_TOOLS = frozenset({
    "read_values", "move_channel", "run_sweep", "run_time_trace",
    "set_second_axis", "send_write_command",
})


@dataclass
class MainSweepSpec:
    """메인 창 sweep 한 번의 구성."""
    channel: int                    # sweep_values 인덱스, TIME_CHANNEL_ID 면 Time
    sweep_to: float
    rate: float                     # units/min (Time 이면 60 → x 가 초 단위)
    time_per_point: float
    measurements: List[int]
    sub_folder: str
    file_prefix: str
    abort_limits: Dict[int, float] = field(default_factory=dict)
    stable: Optional[Tuple[int, int, float]] = None   # (measurement, window, max_std)
    # 인덱스가 가리키는 항목의 (alias, description). 메인 창 프로파일이 실행 도중
    # 바뀌었으면 같은 인덱스가 다른 장비를 가리킬 수 있으므로 시작 직전에 대조한다.
    channel_key: Optional[Tuple[str, str]] = None
    measurement_keys: List[Tuple[str, str]] = field(default_factory=list)
    # 즉시 중단 메시지에 쓸 이름 {measurement index: 표시 이름}
    labels: Dict[int, str] = field(default_factory=dict)


@dataclass
class MainSweepOutcome:
    """reason: complete | stable | abort | user_stop | agent_stop | error | comm_error |
    save_error | start_failed"""
    reason: str
    detail: str = ""
    filepath: str = ""
    points: int = 0


class LabBackend(Protocol):
    """GUI 쪽 (메인 창 조작) 인터페이스 — ui/modules/auto_measure/bridge.py 가 구현."""

    def run_main_sweep(self, spec: MainSweepSpec) -> MainSweepOutcome: ...
    def busy_reason(self) -> str: ...
    def main_state(self) -> dict: ...


class StepFailed(Exception):
    pass


class _StopRelay:
    """stop_event 가 켜지면 워커의 request_stop 을 계속 불러 준다.

    워커들은 작업 시작 시 자기 stop 플래그를 지우므로, 한 번만 부르면 그 직후 시작한
    작업이 중지를 놓칠 수 있다. 중지가 켜져 있는 동안 0.2초마다 다시 건다.
    """

    def __init__(self, stop_event: threading.Event, fn: Callable[[], None]):
        self._stop = stop_event
        self._fn = fn
        self._done = threading.Event()

    def _watch(self):
        while not self._done.is_set():
            if self._stop.wait(0.2):
                try:
                    self._fn()
                except Exception:
                    pass
                if self._done.wait(0.2):
                    return

    def __enter__(self):
        threading.Thread(target=self._watch, daemon=True).start()
        return self

    def __exit__(self, *_):
        self._done.set()


class _SyncStepper:
    """SweepWorker.run_step 을 호출 스레드에서 동기로 돌린다 — 메인 창과 같은 스텝 로직."""

    def __init__(self, session, threshold: float, parallel: bool):
        self._session = session
        self._threshold = threshold
        self._parallel = parallel
        self._worker: Optional[SweepWorker] = None
        self._result: Optional[StepResult] = None
        self._error: Optional[str] = None

    def _ensure(self) -> SweepWorker:
        if self._worker is None:
            w = SweepWorker()
            w.set_session(self._session)
            w.set_threshold(self._threshold)
            w.set_parallel(self._parallel)
            # 같은 스레드 안에서 바로 받는다 (GUI 가 아니라 이 실행기의 변수에 담을 뿐)
            w.step_done.connect(self._on_done, Qt.ConnectionType.DirectConnection)
            w.step_error.connect(self._on_error, Qt.ConnectionType.DirectConnection)
            self._worker = w
        return self._worker

    def _on_done(self, result):
        self._result = result

    def _on_error(self, msg):
        self._error = msg

    def request_stop(self):
        if self._worker is not None:
            self._worker.request_stop()

    def step(self, req: StepRequest) -> StepResult:
        worker = self._ensure()
        self._result, self._error = None, None
        worker.run_step(req)
        if self._error is not None:
            raise StepFailed(self._error)
        if self._result is None:
            raise StepFailed("스텝 결과가 없습니다.")
        return self._result

    def read_channel(self, channel) -> float:
        """sweep channel 의 현재값 (readback). 쓰기 없음."""
        r = self.step(StepRequest(sweep_channel=channel, sweep_to=0.0, sweep_rate=1.0,
                                  time_per_point=1.0, measure_only=True))
        return r.current

    def measure(self, profile, indices: List[int]) -> Tuple[Dict[int, Optional[float]],
                                                            Dict[int, str]]:
        ms = profile.measurements
        active = [(i, ms[i].alias, ms[i].description, ms[i].resolved_cmd) for i in indices]
        r = self.step(StepRequest(sweep_channel=TIME_CHANNEL, sweep_to=0.0, sweep_rate=1.0,
                                  time_per_point=1.0, active_measurements=active,
                                  measure_only=True))
        return dict(r.meas_results), dict(r.meas_errors)


def _num(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


class LabExecutor:
    """도구 이름 + 입력 → ToolOutcome. 에이전트 스레드에서 호출된다."""

    def __init__(self, *, session, profile, settings: AutomationSettings,
                 backend: LabBackend, stop_event: threading.Event,
                 emit: Callable[[AgentEvent], None],
                 messenger: Optional[Callable[[str, bool], List[str]]] = None,
                 threshold: float = 1e38, parallel: bool = False,
                 instruments: Optional[dict] = None, main_defaults: Optional[dict] = None,
                 save_root: str = "", knowledge: Optional[KnowledgeStore] = None,
                 profile_name: str = "", run_dir: str = ""):
        self._session = session
        self.knowledge = knowledge
        self.profile_name = profile_name
        self.run_dir = run_dir
        # True 면 save_knowledge 가 승인 절차를 거쳐 실행된 것 → 저장 즉시 '확인됨'
        self.confirm_knowledge_on_save = False
        self._instrument_lock = threading.Lock()
        self.profile = profile
        self.settings = settings
        self.policy = SafetyPolicy(settings, profile)
        self._backend = backend
        self._stop = stop_event
        self._emit = emit
        self._messenger = messenger
        self._stepper = _SyncStepper(session, threshold, parallel)
        self._instruments = instruments or {}
        self._main_defaults = main_defaults or {}
        self._save_root = save_root
        self.deadline: Optional[float] = None          # time.monotonic() 기준
        self.started_at = time.monotonic()
        self.status_provider: Callable[[], dict] = dict
        self.files: List[str] = []
        self._messages_sent = 0

    # ── 공통 ───────────────────────────────────────────────────────────
    def set_stop_event(self, stop_event: threading.Event) -> None:
        """새 중지 이벤트로 바꾼다 (이어서 지시할 때, MCP 작업마다). 메인 창 쪽에도 전달."""
        self._stop = stop_event
        setter = getattr(self._backend, "set_stop_event", None)
        if setter is not None:
            setter(stop_event)

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def update_settings(self, settings: AutomationSettings) -> None:
        """이어서 지시할 때 사용자가 바꾼 한계를 반영한다."""
        self.settings = settings
        self.policy = SafetyPolicy(settings, self.profile)

    def remaining_s(self) -> float:
        if self.deadline is None:
            return float("inf")
        return max(0.0, self.deadline - time.monotonic())

    def _check_budget(self, seconds: float, what: str) -> None:
        if seconds > self.remaining_s():
            raise SafetyViolation(
                f"{what} 예상 소요 {seconds / 60:.1f}분이 남은 실행 예산 "
                f"{self.remaining_s() / 60:.1f}분을 넘습니다.")

    def _check_not_busy(self) -> None:
        reason = self._backend.busy_reason()
        if reason:
            raise SafetyViolation(f"다른 측정이 실행 중이라 장비를 움직이지 않습니다: {reason}")

    def _status(self, text: str) -> None:
        self._emit(AgentEvent("status", text))

    def execute(self, name: str, tool_input: dict) -> ToolOutcome:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolOutcome({"error": f"알 수 없는 도구: {name}"}, True)
        try:
            if name in INSTRUMENT_TOOLS:
                with self._instrument_lock:
                    return handler(tool_input or {})
            return handler(tool_input or {})
        except SafetyViolation as exc:
            self._status(f"안전 검사 거부: {exc}")
            return ToolOutcome({"error": "안전 검사에서 거부됨", "reason": str(exc)}, True)
        except StepFailed as exc:
            return ToolOutcome({"error": "장비 통신 실패", "reason": humanize_error(str(exc)),
                                "detail": str(exc)}, True)
        except KeyError as exc:
            return ToolOutcome({"error": f"필수 입력이 빠졌습니다: {exc}"}, True)

    # ── 표시용 설명 (승인 카드·로그) ───────────────────────────────────
    def _sv_label(self, idx) -> str:
        try:
            sv = self.profile.sweep_values[idx]
            pol = self.settings.sweep_policy(sv.alias, sv.description)
            role = f" ({pol.role})" if pol and pol.role else ""
            return f"[{sv.alias}] {sv.description}{role}"
        except Exception:
            return f"sweep channel {idx}"

    def _unit(self, idx) -> str:
        try:
            return self.profile.sweep_values[idx].unit
        except Exception:
            return ""

    def _meas_labels(self, indices) -> str:
        names = []
        for i in indices or []:
            try:
                m = self.profile.measurements[i]
                names.append(f"{m.alias} {m.description}")
            except Exception:
                names.append(f"#{i}")
        return ", ".join(names) or "(없음)"

    def preview(self, name: str, a: dict) -> str:
        if name == "move_channel":
            u = self._unit(a.get("channel"))
            return (f"이동 (기록 없음): {self._sv_label(a.get('channel'))} → "
                    f"{_num(a.get('target'))} {u}, {_num(a.get('rate'))} {u}/min\n"
                    f"이유: {a.get('purpose', '')}")
        if name == "run_sweep":
            idx = a.get("channel")
            u = self._unit(idx)
            start = a.get("start")
            head = (f"먼저 {_num(start)} {u} 로 이동 후 " if start is not None else "현재값에서 ")
            try:
                rate = float(a.get("rate"))
                tpp = float(a.get("time_per_point"))
                origin = float(start) if start is not None else None
                est = ""
                if origin is not None and rate > 0 and tpp > 0:
                    n = int(abs(float(a.get("stop")) - origin) / (rate / 60 * tpp)) + 1
                    est = f", 약 {n}점·{abs(float(a.get('stop')) - origin) / rate:.1f}분"
            except Exception:
                est = ""
            sub = a.get("sub_folder", "")
            return (f"Sweep 측정: {self._sv_label(idx)}\n"
                    f"{head}{_num(a.get('stop'))} {u} 까지, {_num(a.get('rate'))} {u}/min, "
                    f"{_num(a.get('time_per_point'))} s/점{est}\n"
                    f"측정: {self._meas_labels(a.get('measurements'))}\n"
                    f"저장: {self._save_root or '(Main Folder)'} / {sub} / "
                    f"{a.get('file_prefix', '')}…\n"
                    f"이유: {a.get('purpose', '')}")
        if name == "run_time_trace":
            st = a.get("stop_when_stable")
            cond = (f", 안정화 시 조기 종료 (#{st.get('measurement')} 최근 {st.get('window')}점 "
                    f"std < {_num(st.get('max_std'))})" if isinstance(st, dict) else "")
            return (f"시간 측정: {_num(a.get('duration_s'))} s, {_num(a.get('time_per_point'))} "
                    f"s/점{cond}\n측정: {self._meas_labels(a.get('measurements'))}\n"
                    f"저장: {a.get('sub_folder', '')} / {a.get('file_prefix', '')}…\n"
                    f"이유: {a.get('purpose', '')}")
        if name == "set_second_axis":
            try:
                ch = self.profile.second_sweep_channels[a.get("channel")]
                label = (f"[{ch.alias}] {ch.description} ({ch.advance_type.value} 방식)")
                unit = ch.unit
            except Exception:
                label, unit = f"second channel {a.get('channel')}", ""
            return (f"Second 축 설정: {label} → {_num(a.get('value'))} {unit}, 도달까지 대기\n"
                    f"이유: {a.get('purpose', '')}")
        if name == "send_write_command":
            try:
                wc = self.profile.write_cmds[a.get("command")]
                cmd = wc.cmd_set
                if "{v}" in cmd and a.get("value") is not None:
                    cmd = cmd.format(v=float(a.get("value")))
                label = f"[{wc.alias}] {wc.description}  (명령: {cmd})"
            except Exception:
                label = f"write command {a.get('command')}"
            return f"명령 전송: {label}\n이유: {a.get('purpose', '')}"
        if name == "wait":
            return f"대기 {_num(a.get('seconds'))} s — {a.get('reason', '')}"
        if name == "send_message":
            return "메신저 보고: " + str(a.get("text", ""))[:200]
        if name == "read_values":
            return "현재값 읽기"
        if name == "get_lab_state":
            return "실험실 현황 확인"
        if name == "save_knowledge":
            kind = KIND_LABELS.get(a.get("kind"), a.get("kind"))
            head = "규칙 수정" if a.get("replaces_id") else "규칙 저장"
            return (f"실험 지식 {head} [{kind}] {a.get('title', '')}\n"
                    f"현상: {a.get('condition', '')}\n의미: {a.get('meaning', '')}\n"
                    f"대응: {a.get('action', '')}")
        if name == "record_observation":
            return "관찰 기록: " + str(a.get("summary", ""))[:200]
        if name == "recall_knowledge":
            return f"실험 지식 조회: {a.get('query', '')}"
        if name == "analyze_data_file":
            return f"데이터 파일 분석: {a.get('file', '')}"
        return f"{name}: {a}"

    # ── 도구 ───────────────────────────────────────────────────────────
    def _tool_get_lab_state(self, _a: dict) -> ToolOutcome:
        elapsed = time.monotonic() - self.started_at
        return ToolOutcome({
            "lab": build_lab_context(self.profile, self.settings, self._instruments,
                                     self._main_defaults),
            "run": {
                "elapsed_min": round(elapsed / 60, 1),
                "remaining_budget_min": (None if self.deadline is None
                                         else round(self.remaining_s() / 60, 1)),
                "files_written": list(self.files),
                **self.status_provider(),
            },
            "main_window": self._backend.main_state(),
            **({"knowledge": self.knowledge.digest()} if self.knowledge else {}),
        })

    def _tool_read_values(self, a: dict) -> ToolOutcome:
        out: Dict[str, object] = {}
        meas = self.policy.measurement_indices(a.get("measurements") or [], require=False)
        if meas:
            values, errors = self._stepper.measure(self.profile, meas)
            for i in meas:
                m = self.profile.measurements[i]
                key = f"measurement[{i}] {m.alias} {m.description} ({m.unit})"
                v = values.get(i)
                out[key] = (f"ERR: {humanize_error(errors.get(i, ''))}" if v is None
                            else (None if isinstance(v, float) and math.isnan(v) else v))
        for i in a.get("sweep_channels") or []:
            svs = self.profile.sweep_values
            if not isinstance(i, int) or not 0 <= i < len(svs):
                out[f"sweep_channel[{i}]"] = "인덱스 없음"
                continue
            sv = svs[i]
            key = f"sweep_channel[{i}] {sv.alias} {sv.description} ({sv.unit})"
            try:
                out[key] = self._stepper.read_channel(sweep_channel_from_instantiated(sv))
            except StepFailed as exc:
                out[key] = f"ERR: {humanize_error(str(exc))}"
        for i in a.get("second_channels") or []:
            chs = self.profile.second_sweep_channels
            if not isinstance(i, int) or not 0 <= i < len(chs):
                out[f"second_channel[{i}]"] = "인덱스 없음"
                continue
            ch = chs[i]
            key = f"second_channel[{i}] {ch.alias} {ch.description} ({ch.unit})"
            out[key] = self._read_second(ch)
        return ToolOutcome({"values": out})

    def _read_second(self, ch):
        cmd = ch.feedback_read_cmd or ch.paired_read_cmd
        if not cmd:
            return "readback 명령 없음"
        try:
            return MeasurementParameter(name=ch.description, cmd_query=cmd).read(
                self._session, ch.alias)
        except Exception as exc:     # noqa: BLE001
            return f"ERR: {humanize_error(str(exc))}"

    def _move(self, idx: int, target: float, rate: float, tpp: float) -> dict:
        """sweep channel 을 target 까지 기록 없이 이동 (SweepWorker 스텝 로직)."""
        sv = self.profile.sweep_values[idx]
        channel = sweep_channel_from_instantiated(sv)
        t0 = time.monotonic()
        with _StopRelay(self._stop, self._stepper.request_stop):
            current = self._stepper.read_channel(channel)
            self.policy.check_current_in_range(idx, current)
            self._check_budget(self.policy.estimate_seconds(current, target, rate), "이동")
            last, origin = current, current
            last_report = time.monotonic()
            while True:
                if self._stop.is_set():
                    return {"status": "stopped", "from": origin, "position": last}
                t_step = time.perf_counter()
                r = self._stepper.step(StepRequest(
                    sweep_channel=channel, sweep_to=target, sweep_rate=rate,
                    time_per_point=tpp, last_write_value=last,
                    safety_steps=sv.safety_steps, safety_interval_ms=sv.safety_interval_ms))
                if r.is_done:
                    break
                last = r.next_v
                if time.monotonic() - last_report >= _MOVE_PROGRESS_INTERVAL_S:
                    self._status(f"이동 중 {sv.description}: {last:.6g} → {target:g} {sv.unit}")
                    last_report = time.monotonic()
                remaining = tpp - (time.perf_counter() - t_step)
                if remaining > 0 and self._stop.wait(remaining):
                    return {"status": "stopped", "from": origin, "position": last}
        return {"status": "arrived", "from": origin, "position": last,
                "duration_s": round(time.monotonic() - t0, 1)}

    def _tool_move_channel(self, a: dict) -> ToolOutcome:
        idx = a["channel"]
        target = self.policy.check_sweep_value(idx, a["target"], "target")
        tpp = a.get("time_per_point") or 1.0
        rate, tpp = self.policy.check_rate(idx, a["rate"], tpp)
        self._check_not_busy()
        result = self._move(idx, target, rate, tpp)
        result["unit"] = self.profile.sweep_values[idx].unit
        return ToolOutcome(result, result["status"] != "arrived")

    def _tool_run_sweep(self, a: dict) -> ToolOutcome:
        idx = a["channel"]
        sv, _pol = self.policy.sweep_channel(idx)
        stop = self.policy.check_sweep_value(idx, a["stop"], "stop")
        start = a.get("start")
        if start is not None:
            start = self.policy.check_sweep_value(idx, start, "start")
        rate, tpp = self.policy.check_rate(idx, a["rate"], a["time_per_point"])
        approach = a.get("approach_rate") or rate
        approach, _ = self.policy.check_rate(idx, approach, 1.0)
        meas = self.policy.measurement_indices(a.get("measurements"))
        aborts = self.policy.abort_limits(meas, a.get("abort_if_abs_above"))
        sub = sanitize_sub_folder(a.get("sub_folder", ""))
        prefix = sanitize_file_prefix(a.get("file_prefix", ""))
        self._check_not_busy()

        current = self._stepper.read_channel(sweep_channel_from_instantiated(sv))
        self.policy.check_current_in_range(idx, current)
        origin = start if start is not None else current
        n_points = self.policy.check_points(origin, stop, rate, tpp)
        approach_s = (self.policy.estimate_seconds(current, start, approach)
                      if start is not None else 0.0)
        self._check_budget(approach_s + n_points * tpp, "sweep")

        moved = None
        if start is not None and abs(current - start) > 1e-12:
            self._status(f"시작점으로 이동: {sv.description} {current:.6g} → {start:g} {sv.unit}")
            moved = self._move(idx, start, approach, 1.0)
            if moved["status"] != "arrived":
                return ToolOutcome({"status": "stopped_before_start", "move": moved}, True)
        if self._stop.is_set():
            return ToolOutcome({"status": "stopped_before_start"}, True)

        self._status(f"Sweep 시작: {sv.description} → {stop:g} {sv.unit} (약 {n_points}점)")
        outcome = self._backend.run_main_sweep(MainSweepSpec(
            channel=idx, sweep_to=stop, rate=rate, time_per_point=tpp,
            measurements=meas, sub_folder=sub, file_prefix=prefix, abort_limits=aborts,
            channel_key=(sv.alias, sv.description), **self._meas_identity(meas)))
        return self._sweep_result(outcome, {
            "channel": f"{sv.alias} {sv.description}", "unit": sv.unit,
            "from": origin, "to": stop, "rate_per_min": rate, "time_per_point_s": tpp,
            **({"approach": moved} if moved else {}),
        })

    def _tool_run_time_trace(self, a: dict) -> ToolOutcome:
        try:
            duration = float(a["duration_s"])
        except (TypeError, ValueError):
            raise SafetyViolation("duration_s 가 숫자가 아닙니다.")
        if not math.isfinite(duration) or duration <= 0:
            raise SafetyViolation("duration_s 는 0보다 커야 합니다.")
        tpp = self.policy.check_time_per_point(a["time_per_point"])
        meas = self.policy.measurement_indices(a.get("measurements"))
        stable = None
        st = a.get("stop_when_stable")
        if isinstance(st, dict):
            try:
                s_idx, window, max_std = int(st["measurement"]), int(st["window"]), \
                    float(st["max_std"])
            except (KeyError, TypeError, ValueError):
                raise SafetyViolation("stop_when_stable 은 {measurement, window, max_std} 형식")
            if s_idx not in meas:
                raise SafetyViolation("stop_when_stable 의 measurement 가 기록 목록에 없습니다.")
            if window < 3 or not max_std > 0:
                raise SafetyViolation("stop_when_stable 은 window ≥ 3, max_std > 0 이어야 합니다.")
            stable = (s_idx, window, max_std)
        n_points = int(duration / tpp) + 1
        if n_points > self.settings.max_points_per_sweep:
            raise SafetyViolation(
                f"예상 점 수 {n_points} 이(가) 상한 {self.settings.max_points_per_sweep} 을 "
                "넘습니다. time_per_point 를 늘리세요.")
        self._check_budget(duration, "시간 측정")
        aborts = self.policy.abort_limits(meas)
        sub = sanitize_sub_folder(a.get("sub_folder", ""))
        prefix = sanitize_file_prefix(a.get("file_prefix", ""))
        self._check_not_busy()
        self._status(f"시간 측정 시작: {duration:g} s, {tpp:g} s/점")
        outcome = self._backend.run_main_sweep(MainSweepSpec(
            channel=TIME_CHANNEL_ID, sweep_to=duration, rate=60.0, time_per_point=tpp,
            measurements=meas, sub_folder=sub, file_prefix=prefix,
            abort_limits=aborts, stable=stable, **self._meas_identity(meas)))
        return self._sweep_result(outcome, {"x_axis": "time (s)", "duration_s": duration},
                                  ok_reasons=("complete", "stable"))

    def _meas_identity(self, meas: List[int]) -> dict:
        ms = self.profile.measurements
        return {
            "measurement_keys": [(ms[i].alias, ms[i].description) for i in meas],
            "labels": {i: f"{ms[i].alias} {ms[i].description}" for i in meas},
        }

    def _sweep_result(self, outcome: MainSweepOutcome, info: dict,
                      ok_reasons=("complete",)) -> ToolOutcome:
        result = {"status": outcome.reason, **info}
        if outcome.detail:
            result["detail"] = outcome.detail
        if outcome.filepath:
            result["file"] = outcome.filepath
            if outcome.filepath not in self.files:
                self.files.append(outcome.filepath)
            try:
                if Path(outcome.filepath).exists():
                    result["data"] = summarize_dat(outcome.filepath)
            except Exception as exc:     # noqa: BLE001
                result["data_error"] = f"요약 실패: {exc}"
        return ToolOutcome(result, outcome.reason not in ok_reasons)

    def _tool_set_second_axis(self, a: dict) -> ToolOutcome:
        idx = a["channel"]
        ch, _pol = self.policy.second_channel(idx)
        value = self.policy.check_second_value(idx, a["value"])
        self._check_not_busy()
        prev = self._read_second(ch)
        prev_v = prev if isinstance(prev, float) and math.isfinite(prev) else None
        if self._stop.is_set():
            return ToolOutcome({"status": "stopped"}, True)

        worker = SecondChannelWorker()
        worker.set_session(self._session)
        box: Dict[str, str] = {}
        # 이 실행기 스레드 안에서 바로 받는다 (GUI 접근 없음 → lambda 직접 연결 허용)
        direct = Qt.ConnectionType.DirectConnection
        worker.done.connect(lambda: box.setdefault("ok", "1"), direct)
        worker.error.connect(lambda m: box.setdefault("error", m), direct)
        worker.advance_failed.connect(lambda m: box.setdefault("failed", m), direct)
        worker.status.connect(lambda m: self._status(f"{ch.description}: {m}"), direct)
        self._status(f"{ch.description} → {value:g} {ch.unit} ({ch.advance_type.value})")
        t0 = time.monotonic()
        with _StopRelay(self._stop, worker.request_stop):
            worker.advance(SecondChannelRequest(channel=ch, next_value=value,
                                                prev_value=prev_v, time_per_point=1.0))
        elapsed = round(time.monotonic() - t0, 1)
        after = self._read_second(ch)
        info = {"channel": f"{ch.alias} {ch.description}", "unit": ch.unit, "target": value,
                "before": prev, "after": after, "elapsed_s": elapsed}
        if self._stop.is_set():
            return ToolOutcome({"status": "stopped", **info}, True)
        if "failed" in box:
            return ToolOutcome({"status": "timeout", "detail": box["failed"], **info}, True)
        if "error" in box:
            return ToolOutcome({"status": "error", "detail": humanize_error(box["error"]),
                                **info}, True)
        return ToolOutcome({"status": "reached", **info})

    def _tool_send_write_command(self, a: dict) -> ToolOutcome:
        wc, cmd = self.policy.write_command(a["command"], a.get("value"))
        self._check_not_busy()
        try:
            if not self._session.is_open(wc.alias):
                self._session.open(wc.alias)
            self._session.write(wc.alias, cmd)
        except Exception as exc:     # noqa: BLE001
            return ToolOutcome({"error": "전송 실패", "reason": humanize_error(str(exc))}, True)
        return ToolOutcome({"sent": True, "command": f"{wc.alias} {wc.description}",
                            "value": a.get("value")})

    def _tool_wait(self, a: dict) -> ToolOutcome:
        try:
            seconds = float(a["seconds"])
        except (TypeError, ValueError):
            raise SafetyViolation("seconds 가 숫자가 아닙니다.")
        if not math.isfinite(seconds) or seconds < 0:
            raise SafetyViolation("seconds 는 0 이상이어야 합니다.")
        self._check_budget(seconds, "대기")
        self._status(f"대기 {seconds:g} s — {a.get('reason', '')}")
        stopped = self._stop.wait(seconds)
        return ToolOutcome({"waited_s": seconds, "stopped": stopped}, stopped)

    def _tool_send_message(self, a: dict) -> ToolOutcome:
        text = str(a.get("text", "")).strip()
        if not text:
            raise SafetyViolation("보낼 text 가 비었습니다.")
        if len(text) > _MAX_MESSAGE_CHARS:
            text = text[:_MAX_MESSAGE_CHARS] + " …(생략)"
        urgent = bool(a.get("urgent", False))
        self._emit(AgentEvent("message", text, {"urgent": urgent}))
        if not self.settings.report_to_messenger or self._messenger is None:
            return ToolOutcome({"delivered": False,
                                "note": "메신저 보고가 꺼져 있어 화면에만 표시했습니다."})
        if self._messages_sent >= _MAX_MESSAGES_PER_RUN:
            return ToolOutcome({"delivered": False,
                                "note": f"이번 실행의 메시지 상한({_MAX_MESSAGES_PER_RUN})에 "
                                        "도달해 화면에만 표시했습니다."})
        self._messages_sent += 1
        errors = self._messenger(text, urgent)
        if errors:
            return ToolOutcome({"delivered": False, "errors": errors}, True)
        return ToolOutcome({"delivered": True})

    # ── 실험 지식 (가이드 — 판단은 LLM 이 데이터로 직접) ─────────────────
    def _need_knowledge(self) -> KnowledgeStore:
        if self.knowledge is None:
            raise SafetyViolation("실험 지식 저장소가 연결되지 않았습니다.")
        return self.knowledge

    @staticmethod
    def _text_field(a: dict, key: str, required: bool = False) -> str:
        value = str(a.get(key) or "").strip()
        if required and not value:
            raise SafetyViolation(f"{key} 가 비었습니다.")
        return value[:_MAX_KNOWLEDGE_TEXT]

    @staticmethod
    def _str_list(value, limit: int = 20) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip()[:200] for v in value if str(v).strip()][:limit]

    def _tool_recall_knowledge(self, a: dict) -> ToolOutcome:
        store = self._need_knowledge()
        try:
            limit = max(1, min(30, int(a.get("limit") or 8)))
        except (TypeError, ValueError):
            limit = 8
        kinds = [k for k in self._str_list(a.get("kinds")) if k in KIND_LABELS]
        return ToolOutcome(store.search(str(a.get("query", "")), kinds=kinds or None,
                                        tags=self._str_list(a.get("tags")), limit=limit))

    def _tool_save_knowledge(self, a: dict) -> ToolOutcome:
        store = self._need_knowledge()
        kind = str(a.get("kind", ""))
        if kind not in KIND_LABELS:
            raise SafetyViolation(f"kind 는 {list(KIND_LABELS)} 중 하나여야 합니다.")
        fields = {
            "kind": kind,
            "title": self._text_field(a, "title", required=True)[:200],
            "condition": self._text_field(a, "condition", required=True),
            "meaning": self._text_field(a, "meaning", required=True),
            "action": self._text_field(a, "action"),
            "tags": self._str_list(a.get("tags")),
            "sample": self._text_field(a, "sample")[:100],
            "source": "ai",
            # 사용자가 직접 기억하라고 했거나 승인 절차를 거쳤으면 확인됨, 아니면 가설
            "confirmed": bool(self.confirm_knowledge_on_save or a.get("user_requested")),
        }
        replaces = a.get("replaces_id")
        if replaces:
            entry = store.update_entry(str(replaces), **fields)
            if entry is None:
                raise SafetyViolation(f"고칠 규칙 id '{replaces}' 가 없습니다.")
        else:
            entry = store.add_entry(**fields)
        self._emit(AgentEvent("knowledge", f"실험 지식 저장: {entry.title}",
                              {"id": entry.id}))
        return ToolOutcome({"saved": entry.brief()})

    def _tool_record_observation(self, a: dict) -> ToolOutcome:
        store = self._need_knowledge()
        quantities = {}
        raw = a.get("quantities")
        if isinstance(raw, dict):
            for k, v in list(raw.items())[:50]:
                quantities[str(k)[:100]] = v if isinstance(v, (int, float)) else str(v)[:200]
        obs = store.add_observation(
            summary=self._text_field(a, "summary", required=True),
            quantities=quantities,
            interpretation=self._text_field(a, "interpretation"),
            files=self._str_list(a.get("files"), 50),
            tags=self._str_list(a.get("tags")),
            sample=self._text_field(a, "sample")[:100],
            supports=self._str_list(a.get("supports")),
            contradicts=self._str_list(a.get("contradicts")),
            profile=self.profile_name, run=self.run_dir)
        self._emit(AgentEvent("knowledge", f"관찰 기록: {obs.summary[:120]}", {"id": obs.id}))
        return ToolOutcome({"recorded": obs.id, "linked_supports": obs.supports,
                            "linked_contradicts": obs.contradicts})

    def _tool_analyze_data_file(self, a: dict) -> ToolOutcome:
        if not self._save_root:
            raise SafetyViolation("Main Folder 가 지정되지 않아 데이터 파일을 찾을 수 없습니다.")
        root = Path(self._save_root).resolve()
        path = Path(str(a.get("file", "")).strip())
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if path != root and root not in path.parents:
            raise SafetyViolation(f"Main Folder({root}) 밖의 파일은 읽지 않습니다: {path}")
        if path.suffix.lower() != ".dat" or not path.is_file():
            raise SafetyViolation(f".dat 파일이 아니거나 없습니다: {path}")
        try:
            points = max(10, min(200, int(a.get("max_curve_points") or 40)))
        except (TypeError, ValueError):
            points = 40
        return ToolOutcome({"file": str(path), "data": summarize_dat(path, points)})
