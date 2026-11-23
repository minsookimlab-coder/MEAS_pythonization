"""ntfy 연동 — 휴대폰 푸시 알림 보내기 + 메시지 받기.

보내기 (publish):  알람, AI 의 보고, 승인 요청(휴대폰 알림의 [승인]/[거부] 버튼).
받기 (subscribe):  AI 에게 보내는 지시 — 사용자가 ntfy 앱·웹에서 '지시용 주제'에 쓴 글.

ntfy API (docs.ntfy.sh):
  - JSON 발행: 서버 루트 URL 에 {"topic", "message", "title", "tags", "priority",
    "actions", "click", "markdown"} 를 POST. 본문이 UTF-8 이라 한글 제목도 그대로 된다.
  - 접근 토큰: Authorization: Bearer tk_...
  - 구독: GET /<topic>/json — 한 줄에 JSON 하나 (event: open/keepalive/message …).
    since=<unix 시각 | 메시지 id> 로 놓친 메시지부터 다시 받는다.
  - 메시지 본문은 4,096 바이트를 넘으면 첨부로 바뀐다 → 그 아래로 자른다.

보안: ntfy.sh 의 주제는 이름을 아는 누구나 읽고 쓸 수 있다. 주제 이름은 추측할 수
없게 무작위로 만들고(random_topic), 가능하면 접근 토큰을 쓴다. 지시용 주제로 들어온
글도 AI 의 안전 검사·승인 절차를 그대로 거친다.

Qt 를 쓰지 않는다. 구독은 데몬 스레드에서 돌고 콜백으로 넘긴다.
"""
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, Optional

from pythonization.util.net_ssl import explain_ssl_error, make_ssl_context

DEFAULT_SERVER = "https://ntfy.sh"
_MAX_BODY_BYTES = 3800          # 4,096 바이트 한도 아래 (제목·JSON 여유)
_RECONNECT_MIN_S = 3.0
_RECONNECT_MAX_S = 60.0
_READ_TIMEOUT_S = 120.0         # 서버 keepalive(약 45초)보다 넉넉하게


def random_topic(prefix: str = "pyz") -> str:
    """추측할 수 없는 주제 이름 (ntfy 허용 문자: 영문·숫자·-·_, 64자 이하)."""
    return f"{prefix}-{secrets.token_urlsafe(18)}"


def _clip_utf8(text: str, limit: int = _MAX_BODY_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit - 12].decode("utf-8", errors="ignore") + " …(생략)"


def _context(url: str):
    return make_ssl_context() if url.lower().startswith("https") else None


def _headers(token: str) -> dict:
    headers = {"User-Agent": "pythonization-ntfy/1.0"}
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def http_action(label: str, server: str, topic: str, body: str, token: str = "") -> dict:
    """알림의 버튼 — 누르면 휴대폰이 server/topic 에 body 를 POST 한다."""
    action = {"action": "http", "label": label,
              "url": f"{server.rstrip('/')}/{topic}", "method": "POST",
              "body": body, "clear": True}
    if token.strip():
        action["headers"] = {"Authorization": f"Bearer {token.strip()}"}
    return action


def publish(server: str, topic: str, message: str, *, title: str = "",
            priority: int = 3, tags: Optional[List[str]] = None,
            actions: Optional[List[dict]] = None, token: str = "",
            timeout: float = 10.0) -> Optional[str]:
    """메시지 하나 발행. 성공이면 None, 실패면 사유 문자열."""
    if not server.strip() or not topic.strip():
        return "ntfy 서버 또는 주제가 비어 있습니다."
    payload = {"topic": topic.strip(), "message": _clip_utf8(message or " ")}
    if title:
        payload["title"] = title[:200]
    if priority and priority != 3:
        payload["priority"] = max(1, min(5, int(priority)))
    if tags:
        payload["tags"] = list(tags)[:5]
    if actions:
        payload["actions"] = list(actions)[:3]
    url = server.strip().rstrip("/") + "/"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST",
                                 headers={**_headers(token), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_context(url)) as resp:
            resp.read()
        return None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            detail = ""
        hint = " (주제 접근 권한·토큰 확인)" if e.code in (401, 403) else ""
        return f"ntfy HTTP {e.code}{hint} {detail}".strip()
    except Exception as e:
        return explain_ssl_error(e) or f"ntfy 전송 실패: {e}"


class NtfySubscriber:
    """주제를 구독해 새 메시지마다 on_message(dict) 를 부른다. 끊기면 다시 붙는다."""

    def __init__(self, server: str, topic: str, on_message: Callable[[dict], None], *,
                 token: str = "", on_status: Optional[Callable[[str], None]] = None,
                 since: Optional[str] = None):
        self.server = server.strip().rstrip("/")
        self.topic = topic.strip()
        self._token = token
        self._on_message = on_message
        self._on_status = on_status or (lambda _s: None)
        # 처음에는 '지금 이후' 메시지만 — 예전 지시가 다시 실행되면 안 된다
        self._since = since or str(int(time.time()))
        self._stop = threading.Event()
        self._resp = None
        self._thread: Optional[threading.Thread] = None
        self.connected = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="ntfy-subscriber", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        resp = self._resp
        if resp is not None:
            try:
                resp.close()        # 막혀 있는 read 를 깨운다
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def _url(self) -> str:
        return (f"{self.server}/{urllib.parse.quote(self.topic)}/json?"
                + urllib.parse.urlencode({"since": self._since}))

    def _run(self) -> None:
        delay = _RECONNECT_MIN_S
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(self._url(), headers=_headers(self._token))
                self._resp = urllib.request.urlopen(req, timeout=_READ_TIMEOUT_S,
                                                    context=_context(self.server))
                self.connected = True
                self._on_status("connected")
                delay = _RECONNECT_MIN_S
                for raw in self._resp:
                    if self._stop.is_set():
                        break
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except Exception:
                        continue
                    if event.get("event") != "message":
                        continue
                    if event.get("id"):
                        self._since = event["id"]     # 다시 붙을 때 여기부터
                    try:
                        self._on_message(event)
                    except Exception:
                        pass
            except Exception as e:     # noqa: BLE001 — 네트워크 오류는 다시 붙어 회복
                if self._stop.is_set():
                    break
                self._on_status(f"disconnected: {explain_ssl_error(e) or e}")
            finally:
                self.connected = False
                resp, self._resp = self._resp, None
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
            if self._stop.wait(delay):
                break
            delay = min(_RECONNECT_MAX_S, delay * 2)
