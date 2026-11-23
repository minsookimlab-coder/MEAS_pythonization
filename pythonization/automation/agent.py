"""도구 호출 루프 (Messages API 수동 루프).

Anthropic 이 호스팅하는 Managed Agents 대신 앱 안에서 루프를 돈다. 측정은 수십 시간씩
걸리는데 그동안 원격 세션을 열어 둘 이유가 없고, 안전 검사·중지 버튼·장비 락이 모두
이 프로세스 안에 있어야 하기 때문이다. 도구가 실행되는 동안에는 API 비용이 들지 않는다.

규칙:
  - 대화 기록은 덧붙이기만 한다 (append-only). 응답 content 를 그대로 되돌려 보내야
    thinking 블록이 유효하게 남는다.
  - tool_use 하나에 tool_result 하나를 반드시 붙인다. 중지·거부·오류도 is_error 결과로.
  - 장비를 움직이는 도구는 '실행 전 승인' 모드에서 사용자 승인을 받는다.
  - 중지(stop_event)는 API 호출 사이, 도구 사이, 도구 안에서 확인한다.

이 모듈은 Qt 를 모른다. GUI 는 emit/approve 콜백으로 붙는다.
"""
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from pythonization.automation.llm import build_request, describe_api_error
from pythonization.automation.tools import APPROVAL_TOOLS, TOOL_NAMES

_MAX_API_RETRIES = 3
_RETRY_BASE_S = 15.0


@dataclass
class AgentEvent:
    """GUI·기록용 이벤트.

    kind: user | assistant | progress | tool_call | tool_result | approval | status |
          error | usage | finished
    """
    kind: str
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalDecision:
    approved: bool
    comment: str = ""


@dataclass
class ToolOutcome:
    content: Any            # JSON 직렬화 가능한 값
    is_error: bool = False


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    calls: int = 0

    def add(self, usage) -> None:
        if usage is None:
            return
        self.calls += 1
        for name in ("input_tokens", "output_tokens",
                     "cache_read_input_tokens", "cache_creation_input_tokens"):
            setattr(self, name, getattr(self, name) + int(getattr(usage, name, 0) or 0))


def _block_to_dict(block) -> dict:
    """기록용 직렬화. thinking 서명처럼 크고 쓸모없는 필드는 뺀다."""
    if isinstance(block, dict):
        d = dict(block)
    elif hasattr(block, "model_dump"):
        try:
            d = block.model_dump(exclude_none=True)
        except Exception:
            d = {"type": getattr(block, "type", "?")}
    else:
        d = {k: v for k, v in vars(block).items() if not k.startswith("_")}
    d.pop("signature", None)
    return d


class AgentLoop:
    """대화 하나. run() 을 여러 번 불러 이어서 지시할 수 있다 (같은 기록 위에)."""

    def __init__(self, *, client, model: str, effort: str, max_tokens: int,
                 system_prompt: str, tools: list,
                 execute: Callable[[str, dict], ToolOutcome],
                 preview: Callable[[str, dict], str],
                 emit: Callable[[AgentEvent], None],
                 approve: Callable[[str, dict, str], ApprovalDecision],
                 journal: Optional[Callable[[dict], None]] = None,
                 stop_event: Optional[threading.Event] = None):
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._system = system_prompt
        self._tools = tools
        self._execute = execute
        self._preview = preview
        self._emit = emit
        self._approve = approve
        self._journal = journal or (lambda _rec: None)
        self.stop_event = stop_event or threading.Event()

        self.messages: List[dict] = []
        self.usage = UsageTotals()
        self.approval_mode = "approve"
        self.max_turns = 80
        self.deadline: Optional[float] = None       # time.monotonic() 기준
        self.turns_used = 0
        self.final_text = ""
        self._steer: "queue.Queue[str]" = queue.Queue()

    # ── 외부 제어 ──────────────────────────────────────────────────────
    def steer(self, text: str) -> None:
        """실행 중 사용자 추가 지시 — 다음 tool_result 와 함께 전달된다."""
        if text.strip():
            self._steer.put(text.strip())

    def _drain_steer(self) -> List[dict]:
        blocks = []
        while True:
            try:
                text = self._steer.get_nowait()
            except queue.Empty:
                break
            blocks.append({"type": "text", "text": f"[사용자 추가 지시] {text}"})
            self._emit(AgentEvent("user", text))
            self._record("user", text=text)
        return blocks

    def _record(self, kind: str, **fields) -> None:
        try:
            self._journal({"t": datetime.now().isoformat(timespec="seconds"),
                           "type": kind, **fields})
        except Exception:
            pass

    # ── 루프 ───────────────────────────────────────────────────────────
    def run(self, user_text: str) -> str:
        """user_text 로 시작해 모델이 끝낼 때까지 돈다.

        반환: finished | stopped | budget | error | refusal
        """
        self.final_text = ""
        self.messages.append({"role": "user", "content": [{"type": "text", "text": user_text}]})
        self._record("user", text=user_text)

        retries = 0
        while True:
            if self.stop_event.is_set():
                return "stopped"
            if self.turns_used >= self.max_turns:
                self._emit(AgentEvent("error", f"LLM 호출 횟수 상한({self.max_turns})에 "
                                               "도달해 멈췄습니다."))
                return "budget"
            if self.deadline is not None and time.monotonic() > self.deadline:
                self._emit(AgentEvent("error", "실행 시간 예산을 다 써서 멈췄습니다."))
                return "budget"

            try:
                response = self._client.beta.messages.create(**build_request(
                    model=self._model, effort=self._effort, max_tokens=self._max_tokens,
                    system=self._system, tools=self._tools, messages=self.messages))
            except Exception as exc:     # noqa: BLE001 — SDK 예외는 아래에서 분류
                text, retryable = describe_api_error(exc)
                self._record("api_error", text=text)
                if retryable and retries < _MAX_API_RETRIES:
                    retries += 1
                    delay = _RETRY_BASE_S * retries
                    self._emit(AgentEvent("status", f"{text} ({delay:.0f}초 뒤 재시도 "
                                                    f"{retries}/{_MAX_API_RETRIES})"))
                    if self.stop_event.wait(delay):
                        return "stopped"
                    continue
                self._emit(AgentEvent("error", text))
                return "error"
            retries = 0
            self.turns_used += 1
            self.usage.add(getattr(response, "usage", None))
            self._emit(AgentEvent("usage", data=vars(self.usage).copy()))

            content = list(getattr(response, "content", []) or [])
            stop_reason = getattr(response, "stop_reason", None)
            self.messages.append({"role": "assistant", "content": content})
            self._record("assistant", stop_reason=stop_reason,
                         content=[_block_to_dict(b) for b in content])
            texts = self._emit_blocks(content)

            tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]

            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                if tool_uses:   # 이어서 지시할 수 있게 대화를 유효한 형태로 닫는다
                    self.messages.append({"role": "user", "content": [
                        self._result_block(tu.id, {"error": "실행하지 않음 (거절된 응답)"}, True)
                        for tu in tool_uses]})
                self._emit(AgentEvent(
                    "error", f"모델이 요청을 거절했습니다 (분류: {category or '알 수 없음'})."))
                return "refusal"

            if stop_reason == "max_tokens":
                # 잘린 tool_use 는 실행하지 않는다. 결과를 붙여 대화를 유효하게 유지한다.
                results = [self._result_block(tu.id, {"error": "응답이 길이 제한에서 잘려 "
                                                               "실행하지 않았습니다. 다시 "
                                                               "호출하세요."}, True)
                           for tu in tool_uses]
                note = {"type": "text",
                        "text": "(응답이 max_tokens 에서 잘렸습니다. 더 짧게 이어서 진행하세요.)"}
                self.messages.append({"role": "user", "content": results + [note]})
                continue

            if not tool_uses:
                if stop_reason == "pause_turn":
                    continue
                steer = self._drain_steer()
                if steer:
                    self.messages.append({"role": "user", "content": steer})
                    continue
                self.final_text = "\n".join(texts).strip()
                self._emit(AgentEvent("finished", self.final_text))
                return "finished"

            results = []
            for tu in tool_uses:
                if self.stop_event.is_set():
                    results.append(self._result_block(
                        tu.id, {"error": "사용자가 실행을 중지했습니다."}, True))
                    continue
                outcome = self._run_tool(tu.name, tu.input or {})
                results.append(self._result_block(tu.id, outcome.content, outcome.is_error))
            self.messages.append({"role": "user", "content": results + self._drain_steer()})
            if self.stop_event.is_set():
                return "stopped"

    def _emit_blocks(self, content) -> List[str]:
        texts = []
        for block in content:
            kind = getattr(block, "type", None)
            if kind == "text" and getattr(block, "text", "").strip():
                texts.append(block.text)
                self._emit(AgentEvent("assistant", block.text))
            elif kind == "thinking" and (getattr(block, "thinking", "") or "").strip():
                # display "updates" — 비어 있지 않은 thinking 블록은 진행 메모다
                self._emit(AgentEvent("progress", block.thinking))
        return texts

    @staticmethod
    def _result_block(tool_use_id: str, content: Any, is_error: bool) -> dict:
        text = content if isinstance(content, str) else json.dumps(
            content, ensure_ascii=False, default=str)
        block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
        if is_error:
            block["is_error"] = True
        return block

    def _run_tool(self, name: str, tool_input: dict) -> ToolOutcome:
        if name not in TOOL_NAMES:
            return ToolOutcome({"error": f"알 수 없는 도구: {name}"}, True)
        try:
            preview = self._preview(name, tool_input)
        except Exception as exc:     # noqa: BLE001
            preview = f"{name}: {tool_input} ({exc})"
        self._emit(AgentEvent("tool_call", preview, {"name": name, "input": tool_input}))
        self._record("tool_call", name=name, input=tool_input)

        if name in APPROVAL_TOOLS and self.approval_mode == "approve":
            decision = self._approve(name, tool_input, preview)
            self._record("approval", name=name, approved=decision.approved,
                         comment=decision.comment)
            if not decision.approved:
                msg = "사용자가 이 조작을 거부했습니다."
                if decision.comment:
                    msg += f" 사유: {decision.comment}"
                self._emit(AgentEvent("approval", msg, {"approved": False}))
                return ToolOutcome({"error": msg}, True)
            self._emit(AgentEvent("approval", "승인됨" + (
                f" — {decision.comment}" if decision.comment else ""), {"approved": True}))
            if decision.comment:
                # 승인하면서 남긴 말은 모델에게도 전달한다
                self.steer(f"(승인 시 메모) {decision.comment}")

        try:
            outcome = self._execute(name, tool_input)
        except Exception as exc:     # noqa: BLE001 — 도구 오류는 모델이 읽고 판단한다
            outcome = ToolOutcome({"error": f"{type(exc).__name__}: {exc}"}, True)
        self._record("tool_result", name=name, is_error=outcome.is_error,
                     content=outcome.content)
        self._emit(AgentEvent("tool_result", "", {"name": name, "is_error": outcome.is_error,
                                                  "content": outcome.content}))
        return outcome
