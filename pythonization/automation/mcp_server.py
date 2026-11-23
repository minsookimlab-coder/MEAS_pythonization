"""MCP 서버 — Claude Code (데스크톱 앱·CLI) 가 이 프로그램의 AI 측정 도구를 쓰게 한다.

계측기는 실행 중인 이 프로그램이 잡고 있으므로 다른 프로세스가 장비를 직접 열 수 없다.
그래서 프로그램 안에서 MCP 서버(streamable HTTP)를 띄우고, Claude Code 가 거기 붙어
앱 내장 AI 와 똑같은 도구(측정·메신저 보고·실험 지식)를 호출한다. 안전 검사·승인·저장
경로는 LabExecutor 를 그대로 공유한다. API 키가 필요 없다 (Claude Code 구독으로 돈다).

보안:
  - 127.0.0.1 에만 묶는다 (같은 PC 에서만 접속).
  - 모든 요청에 Bearer 토큰이 필요하다. 토큰은 SETTINGS_DIR/mcp_token.txt (저장소 밖).
    Claude Code 는 .mcp.json 의 headersHelper(mcp_auth.py)로 이 파일을 읽어 보낸다.
  - Origin 헤더가 localhost 가 아니면 거부한다 (브라우저를 통한 DNS rebinding 방어).

긴 작업:
  Claude Code 는 HTTP 도구 호출이 5분 동안 응답이 없으면 끊는다. 그래서 도구는 최대
  SYNC_WAIT_S 만 기다리고, 안 끝나면 {status: running, job_id} 를 돌려준다. Claude 는
  wait_job 으로 이어서 기다린다. 측정 자체는 그동안 계속 진행된다.

표준 라이브러리만 쓴다 (mcp 패키지·웹 프레임워크 불필요, PyInstaller 빌드에도 그대로).
SSE 스트림은 열지 않는다 — POST 마다 JSON 응답 하나 (명세상 허용되는 형태).
"""
import hmac
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

from pythonization.app.paths import SETTINGS_DIR
from pythonization.automation.agent import AgentEvent, ApprovalDecision, ToolOutcome
from pythonization.automation.prompts import MCP_INSTRUCTIONS
from pythonization.automation.tools import APPROVAL_TOOLS, TOOL_SPECS

SERVER_NAME = "pythonization-lab"
SERVER_VERSION = "1.12.0"
DEFAULT_PORT = 8765
SYNC_WAIT_S = 230.0          # Claude Code HTTP idle timeout(5분)보다 짧게
MAX_WAIT_S = 240.0
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

#: 다른 작업이 진행 중이어도 바로 실행해도 되는 도구 (장비 I/O 없음)
CONCURRENT_OK = frozenset({
    "get_lab_state", "recall_knowledge", "save_knowledge", "record_observation",
    "analyze_data_file", "send_message",
    "vna_get_state", "vna_run_section", "vna_acquire", "vna_stop",
})
_READ_ONLY = frozenset({"get_lab_state", "read_values", "recall_knowledge",
                        "analyze_data_file", "wait_job", "check_user_messages"})
REMOTE_PREFIX = "[사용자 원격 지시 (ntfy)]"

EXTRA_TOOLS = [
    {
        "name": "wait_job",
        "description": (
            "status=running 으로 돌아온 작업(job_id)이 끝날 때까지 최대 max_wait_s 초 "
            "기다린다. 끝나면 그 도구의 결과를, 아니면 다시 running 과 진행 상태를 돌려준다. "
            "끝날 때까지 반복해서 부른다."),
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"},
                           "max_wait_s": {"type": "number"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "stop_job",
        "description": "진행 중인 측정·이동·대기 작업을 즉시 멈춘다 (메인 창 sweep 포함).",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "check_user_messages",
        "description": (
            "사용자가 휴대폰(ntfy)으로 보낸 새 지시를 가져온다. 다른 도구 결과에도 "
            "'[사용자 원격 지시]' 로 함께 붙어 오지만, 오래 기다리는 중이거나 작업을 마친 뒤 "
            "추가 지시가 있는지 확인할 때 부른다."),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


# ── 토큰 ──────────────────────────────────────────────────────────────

def token_path(settings_dir: Optional[Path] = None) -> Path:
    return (settings_dir or SETTINGS_DIR) / "mcp_token.txt"


def load_or_create_token(settings_dir: Optional[Path] = None) -> str:
    path = token_path(settings_dir)
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except FileNotFoundError:
        pass
    return regenerate_token(settings_dir)


def regenerate_token(settings_dir: Optional[Path] = None) -> str:
    path = token_path(settings_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


def mcp_tool_list() -> list:
    """Anthropic tool 정의 → MCP tools/list 형식."""
    out = []
    for spec in TOOL_SPECS + EXTRA_TOOLS:
        name = spec["name"]
        out.append({
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["input_schema"],
            "annotations": {
                "readOnlyHint": name in _READ_ONLY,
                "destructiveHint": name in APPROVAL_TOOLS,
                "openWorldHint": name == "send_message",
            },
        })
    return out


# ── 작업 ──────────────────────────────────────────────────────────────

@dataclass
class _Job:
    name: str
    args: dict
    stop_event: threading.Event
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    started: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    outcome: Optional[ToolOutcome] = None
    last_status: str = ""


class McpLabServer:
    """MCP JSON-RPC 처리 + HTTP 서버. handle() 은 HTTP 없이도 부를 수 있다 (테스트용)."""

    def __init__(self, *, executor_provider: Callable[[], object], token: str,
                 host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 instructions: str = MCP_INSTRUCTIONS,
                 busy_reason: Callable[[], str] = lambda: "",
                 approve: Optional[Callable[[str, dict, str], ApprovalDecision]] = None,
                 on_event: Optional[Callable[[AgentEvent], None]] = None,
                 journal: Optional[Callable[[dict], None]] = None,
                 inbox: Optional[Callable[[], list]] = None,
                 sync_wait_s: float = SYNC_WAIT_S):
        self._executor_provider = executor_provider
        self._token = token
        self.host = host
        self.port = port
        self._instructions = instructions
        self._busy_reason = busy_reason
        self.approve = approve              # None 이면 Claude Code 의 권한 확인에 맡긴다
        self._emit = on_event or (lambda _ev: None)
        self._journal = journal or (lambda _rec: None)
        self._inbox = inbox or (lambda: [])     # 휴대폰(ntfy)에서 온 지시 — 꺼내면 비워진다
        self._sync_wait_s = sync_wait_s
        self._jobs: Dict[str, _Job] = {}
        self._jobs_lock = threading.Lock()
        self._current: Optional[_Job] = None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.last_client = ""
        self.last_activity = 0.0

    # ── 수명 ───────────────────────────────────────────────────────────
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> None:
        if self._httpd is not None:
            return
        server = self

        class Handler(_Handler):
            mcp = server

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]     # port=0 이면 실제 포트
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="mcp-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_current_job()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    def set_token(self, token: str) -> None:
        self._token = token

    def check_token(self, header: str) -> bool:
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip().encode(), self._token.encode())

    # ── 작업 상태 ───────────────────────────────────────────────────────
    def is_busy(self) -> bool:
        job = self._current
        return job is not None and not job.done.is_set()

    def current_job_name(self) -> str:
        job = self._current
        return job.name if job is not None and not job.done.is_set() else ""

    def stop_current_job(self) -> bool:
        job = self._current
        if job is None or job.done.is_set():
            return False
        job.stop_event.set()
        return True

    # ── JSON-RPC ───────────────────────────────────────────────────────
    def handle(self, message: dict) -> Optional[dict]:
        """요청 하나 → 응답 dict (알림이면 None)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        method = message.get("method")
        msg_id = message.get("id")
        is_notification = "id" not in message
        params = message.get("params") or {}
        self.last_activity = time.time()
        try:
            if method == "initialize":
                requested = params.get("protocolVersion", "")
                info = params.get("clientInfo") or {}
                self.last_client = f"{info.get('name', '?')} {info.get('version', '')}".strip()
                self._journal({"type": "mcp_initialize", "client": self.last_client})
                self._emit(AgentEvent("status", f"Claude Code 연결: {self.last_client}"))
                result = {
                    "protocolVersion": (requested if requested in PROTOCOL_VERSIONS
                                        else PROTOCOL_VERSIONS[1]),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": self._instructions,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": mcp_tool_list()}
            elif method == "tools/call":
                result = self._call_tool(str(params.get("name", "")),
                                         params.get("arguments") or {})
                self._attach_remote_messages(result)
            elif is_notification:
                return None          # notifications/initialized, cancelled 등
            else:
                return _error(msg_id, -32601, f"Method not found: {method}")
        except Exception as exc:     # noqa: BLE001 — 서버 스레드가 죽지 않게
            return _error(msg_id, -32603, f"{type(exc).__name__}: {exc}")
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _attach_remote_messages(self, result: dict) -> None:
        """휴대폰에서 온 지시가 있으면 이번 도구 결과에 덧붙인다 (Claude 가 바로 읽는다)."""
        try:
            messages = list(self._inbox())
        except Exception:
            messages = []
        if messages:
            text = REMOTE_PREFIX + "\n" + "\n".join(f"- {m}" for m in messages)
            result.setdefault("content", []).append({"type": "text", "text": text})
            self._journal({"type": "remote_delivered", "messages": messages})

    def _call_tool(self, name: str, args: dict) -> dict:
        known = {s["name"] for s in TOOL_SPECS}
        if name == "check_user_messages":
            return _content({"note": "새 지시가 있으면 아래에 붙는다. 없으면 없음."}, False)
        if name == "wait_job":
            return self._wait_job(str(args.get("job_id", "")), args.get("max_wait_s"))
        if name == "stop_job":
            stopped = self.stop_current_job()
            return _content({"stopped": stopped}, False)
        if name not in known:
            return _content({"error": f"알 수 없는 도구: {name}"}, True)
        executor = self._executor_provider()
        if executor is None:
            return _content({"error": "AI 자동 측정 창의 MCP 서버가 준비되지 않았습니다."}, True)
        busy = self._busy_reason()
        if busy:
            return _content({"error": f"지금은 실행할 수 없습니다: {busy}"}, True)
        if self.is_busy() and name not in CONCURRENT_OK:
            job = self._current
            return _content({
                "error": f"다른 작업이 진행 중입니다: {job.name} (job_id={job.id}). "
                         "wait_job 으로 기다리거나 stop_job 으로 멈추세요."}, True)

        if name in CONCURRENT_OK:
            # 장비 I/O 가 없는 도구 — 진행 중 작업의 중지 이벤트를 건드리지 않고 바로 실행
            return self._run_now(executor, name, args)

        job = _Job(name=name, args=args, stop_event=threading.Event())
        with self._jobs_lock:
            self._jobs[job.id] = job
            self._current = job
        executor.set_stop_event(job.stop_event)
        threading.Thread(target=self._run_job, args=(executor, job),
                         name=f"mcp-job-{job.id}", daemon=True).start()
        return self._wait_job(job.id, self._sync_wait_s)

    def _preview(self, executor, name: str, args: dict) -> str:
        try:
            return executor.preview(name, args)
        except Exception:
            return f"{name}: {args}"

    def _approve_and_execute(self, executor, name: str, args: dict) -> ToolOutcome:
        """승인(설정된 경우) → 실행. 앱 창의 '조작 전마다 승인' 이 Claude Code 호출에도 적용된다."""
        preview = self._preview(executor, name, args)
        self._emit(AgentEvent("tool_call", "[Claude Code] " + preview,
                              {"name": name, "input": args}))
        if self.approve is not None and name in APPROVAL_TOOLS:
            decision = self.approve(name, args, preview)
            self._journal({"type": "approval", "name": name,
                           "approved": decision.approved, "comment": decision.comment})
            if not decision.approved:
                msg = "사용자가 이 조작을 거부했습니다."
                if decision.comment:
                    msg += f" 사유: {decision.comment}"
                self._emit(AgentEvent("approval", msg, {"approved": False}))
                return ToolOutcome({"error": msg}, True)
            self._emit(AgentEvent("approval", "승인됨" + (
                f" — {decision.comment}" if decision.comment else ""), {"approved": True}))
        # 승인 절차를 거쳐 저장한 지식은 '확인됨', 아니면 user_requested 에 따른다
        executor.confirm_knowledge_on_save = self.approve is not None
        return executor.execute(name, args)

    def _run_now(self, executor, name: str, args: dict) -> dict:
        try:
            outcome = self._approve_and_execute(executor, name, args)
        except Exception as exc:     # noqa: BLE001
            outcome = ToolOutcome({"error": f"{type(exc).__name__}: {exc}"}, True)
        self._record(name, args, outcome)
        return _content(outcome.content, outcome.is_error)

    def _run_job(self, executor, job: _Job) -> None:
        try:
            job.outcome = self._approve_and_execute(executor, job.name, job.args)
        except Exception as exc:     # noqa: BLE001
            job.outcome = ToolOutcome({"error": f"{type(exc).__name__}: {exc}"}, True)
        finally:
            if job.outcome is None:
                job.outcome = ToolOutcome({"error": "결과 없음"}, True)
            self._record(job.name, job.args, job.outcome)
            job.done.set()

    def _record(self, name: str, args: dict, outcome: ToolOutcome) -> None:
        self._journal({"type": "mcp_tool", "name": name, "input": args,
                       "is_error": outcome.is_error, "content": outcome.content})
        self._emit(AgentEvent("tool_result", "", {"name": name, "is_error": outcome.is_error,
                                                  "content": outcome.content}))

    def _wait_job(self, job_id: str, max_wait) -> dict:
        job = self._jobs.get(job_id)
        if job is None:
            return _content({"error": f"job_id '{job_id}' 가 없습니다."}, True)
        try:
            wait = float(max_wait) if max_wait is not None else MAX_WAIT_S
        except (TypeError, ValueError):
            wait = MAX_WAIT_S
        wait = max(0.0, min(MAX_WAIT_S, wait))
        if job.done.wait(wait):
            with self._jobs_lock:
                self._jobs.pop(job.id, None)
            return _content(job.outcome.content, job.outcome.is_error)
        return _content({
            "status": "running", "job_id": job.id, "tool": job.name,
            "elapsed_s": round(time.monotonic() - job.started, 1),
            "next": "wait_job 으로 계속 기다려라 (멈추려면 stop_job).",
        }, False)


def _content(payload, is_error: bool) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


class _Handler(BaseHTTPRequestHandler):
    mcp: McpLabServer = None     # 서버가 하위 클래스에서 채운다
    protocol_version = "HTTP/1.1"
    server_version = "PythonizationMCP/1.0"

    def log_message(self, *_args):
        pass    # 콘솔(cp949)에 쓰지 않는다

    def _send(self, status: int, body: Optional[dict] = None,
              headers: Optional[dict] = None) -> None:
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _guard(self) -> bool:
        if urlparse(self.path).path.rstrip("/") != "/mcp":
            self._send(404, {"error": "not found"})
            return False
        origin = self.headers.get("Origin")
        if origin and (urlparse(origin).hostname or "") not in _LOCAL_HOSTS:
            self._send(403, {"error": "origin not allowed"})
            return False
        if not self.mcp.check_token(self.headers.get("Authorization", "")):
            self._send(401, {"error": "unauthorized"},
                       {"WWW-Authenticate": 'Bearer realm="pythonization-lab"'})
            return False
        return True

    def do_POST(self):
        # 본문은 거부할 때도 먼저 다 읽는다 — 안 읽고 응답하면 keep-alive 연결에 남은
        # 바이트 때문에 Windows 에서 연결이 끊긴다 (WinError 10053).
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
        except Exception:
            self._send(400, _error(None, -32700, "Bad request body"))
            return
        if not self._guard():
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, _error(None, -32700, "Parse error"))
            return
        headers = {}
        if isinstance(message, list):
            replies = [r for r in (self.mcp.handle(m) for m in message) if r is not None]
            if not replies:
                self._send(202)
                return
            self._send(200, replies)
            return
        reply = self.mcp.handle(message)
        if isinstance(message, dict) and message.get("method") == "initialize":
            headers["Mcp-Session-Id"] = uuid.uuid4().hex
        if reply is None:
            self._send(202, None, headers)
        else:
            self._send(200, reply, headers)

    def do_GET(self):
        if not self._guard():
            return
        self._send(405, None, {"Allow": "POST, DELETE"})

    def do_DELETE(self):
        if not self._guard():
            return
        self._send(200, {})
