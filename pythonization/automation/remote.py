"""휴대폰(ntfy 지시 주제)에서 온 글을 명령으로 해석한다.

정해진 짧은 명령만 따로 처리하고, 나머지 글은 모두 AI 에게 보내는 지시다.

  상태 / status / /status            지금 무엇을 하는지 보고
  중지 / 멈춰 / stop / /stop          진행 중인 조작을 멈춤
  승인 <번호> / /approve <번호>        승인 대기 중인 조작 승인 (알림의 [승인] 버튼이 보내는 글)
  거부 <번호> [사유] / /reject …       거부
  도움말 / help / /help               사용법
  그 밖의 글                          AI 에게 지시

'중지' 는 글 전체가 그 단어일 때만 명령이다. "측정 중지하고 0 V 로" 같은 문장은
AI 에게 지시로 간다 (AI 가 판단해 멈추고 되돌린다).
"""
import re
from dataclasses import dataclass

_STOP = {"중지", "멈춰", "정지", "stop", "/stop"}
_STATUS = {"상태", "status", "/status"}
_HELP = {"도움말", "help", "/help", "?"}
_APPROVE = re.compile(r"^/?(?:approve|승인)\s+#?(\w+)\s*$", re.IGNORECASE)
_REJECT = re.compile(r"^/?(?:reject|거부)\s+#?(\w+)\s*(.*)$", re.IGNORECASE | re.DOTALL)

HELP_TEXT = (
    "지시 주제에 쓸 수 있는 글:\n"
    "- 자유 문장: AI 에게 지시 (예: 게이트를 -20 V 에서 20 V 까지 스윕해)\n"
    "- 상태: 지금 하는 일\n"
    "- 중지: 진행 중인 조작을 멈춤\n"
    "- 승인 <번호> / 거부 <번호> <사유>: 승인 요청에 답 (알림 버튼으로도 가능)")


@dataclass
class RemoteCommand:
    kind: str           # stop | status | help | approve | reject | instruction | empty
    arg: str = ""       # 승인 번호
    text: str = ""      # 거부 사유 또는 지시 본문


def parse_remote(text: str) -> RemoteCommand:
    t = (text or "").strip()
    if not t:
        return RemoteCommand("empty")
    low = t.lower()
    if low in _STOP:
        return RemoteCommand("stop")
    if low in _STATUS:
        return RemoteCommand("status")
    if low in _HELP:
        return RemoteCommand("help")
    m = _APPROVE.match(t)
    if m:
        return RemoteCommand("approve", arg=m.group(1))
    m = _REJECT.match(t)
    if m:
        return RemoteCommand("reject", arg=m.group(1), text=m.group(2).strip())
    return RemoteCommand("instruction", text=t)
