"""
통신 오류 해석 — VISA/소켓/연결 예외를 사람이 읽을 수 있는 한국어 설명으로 변환.

목적:
  - Test 버튼·연결 테스트 등에서 긴 traceback 대신 '무엇이 왜 잘못됐는지'를 보여준다.
  - 다양한 통신 오류(타임아웃, 세션 중복/busy, 주소 오류, 연결 끊김 등)를 구분 감지한다.

제공:
  humanize_error(exc) -> str   : 짧은 한국어 원인 설명
  format_error(exc, context)   : 설명 + 한 줄 원본(상세). traceback 없음.
  is_comm_error(err)           : 통신 오류 여부 (재시도 판정용, 폭넓게 감지)
"""
from __future__ import annotations

from typing import Any, Optional

try:
    import pyvisa
    from pyvisa.errors import VisaIOError, StatusCode
except Exception:   # pyvisa 미존재 환경 방어
    pyvisa = None
    VisaIOError = ()           # isinstance 시 항상 False
    StatusCode = None


# ──────────────────────────────────────────────────────────
# VISA StatusCode → 한국어 설명
# ──────────────────────────────────────────────────────────

def _sc(name: str) -> Optional[int]:
    if StatusCode is None:
        return None
    code = getattr(StatusCode, name, None)
    return int(code) if code is not None else None


_VISA_CODE_MSG: dict = {}
for _name, _msg in [
    ("error_timeout",
     "응답 시간 초과(timeout) — 장비가 Remote 모드인지, 주소/명령/종단문자가 맞는지 확인하세요."),
    ("error_resource_busy",
     "장비가 이미 사용 중입니다(resource busy) — 다른 프로그램(NI-MAX·LabVIEW 등)이나 "
     "기존 연결이 이 장비를 잡고 있습니다. 그 연결을 닫고 다시 시도하세요."),
    ("error_resource_locked",
     "장비가 잠겨 있습니다(resource locked) — 다른 세션이 독점 잠금 중입니다. 해당 세션을 닫으세요."),
    ("error_resource_not_found",
     "장비를 찾을 수 없습니다(not found) — 주소·전원·케이블/네트워크 연결을 확인하세요."),
    ("error_invalid_resource_name",
     "주소 형식이 잘못되었습니다 — VISA 주소(TCPIP/GPIB/ASRL 등)를 확인하세요."),
    ("error_connection_lost",
     "연결이 끊겼습니다(connection lost) — 케이블/네트워크/전원을 확인하세요."),
    ("error_invalid_object",
     "세션이 유효하지 않습니다 — 연결이 이미 닫혔거나 재연결이 필요합니다."),
    ("error_resource_not_initialized",
     "리소스가 초기화되지 않았습니다 — 연결을 다시 여세요."),
]:
    _c = _sc(_name)
    if _c is not None:
        _VISA_CODE_MSG[_c] = _msg


# WinSock(OSError.winerror) → 한국어 설명
_WINSOCK_MSG: dict = {
    10048: "주소가 이미 사용 중입니다(WinError 10048) — 같은 포트를 다른 연결이 점유 중.",
    10053: "연결이 중단되었습니다(WinError 10053).",
    10054: "연결이 상대방에 의해 강제로 끊겼습니다(WinError 10054) — 장비 전원/네트워크 확인.",
    10060: "연결 시간 초과(WinError 10060) — 주소가 맞는지, 장비가 켜져 있는지 확인.",
    10061: "연결이 거부되었습니다(WinError 10061) — 포트가 맞는지, 이미 다른 연결이 점유 중인지 확인.",
    10065: "호스트에 도달할 수 없습니다(WinError 10065) — 네트워크/주소 확인.",
}


def _winerror_msg(exc: Exception) -> Optional[str]:
    code = getattr(exc, "winerror", None)
    if code is None:
        # 메시지에 'WinError 100xx' 가 들어있는 경우도 처리
        text = str(exc)
        for c, m in _WINSOCK_MSG.items():
            if f"WinError {c}" in text or f"WinError {c}" in str(getattr(exc, "strerror", "")):
                return m
        return None
    return _WINSOCK_MSG.get(int(code))


# ──────────────────────────────────────────────────────────
# Humanize
# ──────────────────────────────────────────────────────────

def humanize_error(exc: Any) -> str:
    """예외(또는 메시지)를 짧은 한국어 원인 설명으로 변환."""
    if exc is None:
        return "알 수 없는 오류."

    # 문자열만 들어온 경우: 키워드 기반 간이 해석
    if not isinstance(exc, BaseException):
        return _humanize_text(str(exc))

    # 1) VISA 오류 — error_code로 정밀 분류
    if VisaIOError and isinstance(exc, VisaIOError):
        try:
            code = int(exc.error_code)
        except Exception:
            code = None
        if code is not None and code in _VISA_CODE_MSG:
            return _VISA_CODE_MSG[code]
        abbr = getattr(exc, "abbreviation", "") or ""
        desc = getattr(exc, "description", "") or str(exc)
        return f"VISA 통신 오류 {abbr}: {desc}".strip()

    # 2) 소켓/연결 계열
    if isinstance(exc, ConnectionResetError):
        return _winerror_msg(exc) or "연결이 상대방에 의해 끊겼습니다(connection reset) — 장비 전원/네트워크 확인."
    if isinstance(exc, ConnectionRefusedError):
        return _winerror_msg(exc) or "연결이 거부되었습니다(refused) — 포트/점유 상태 확인."
    if isinstance(exc, ConnectionAbortedError):
        return _winerror_msg(exc) or "연결이 중단되었습니다(aborted)."
    if isinstance(exc, TimeoutError):
        return "응답 시간 초과(timeout) — 장비 상태/주소/명령을 확인하세요."
    if isinstance(exc, OSError):
        return _winerror_msg(exc) or f"네트워크/입출력 오류: {exc}"

    # 3) 설정 관련
    if isinstance(exc, KeyError):
        return (f"설정에 없는 장비 이름입니다({exc}). "
                "Instrument Settings(instruments.yaml)에 등록되어 있는지 확인하세요.")

    # 4) 기타 — 메시지 키워드로 한 번 더 시도
    text_msg = _humanize_text(f"{type(exc).__name__}: {exc}")
    if text_msg:
        return text_msg
    return f"{type(exc).__name__}: {exc}"


# 문자열 키워드 기반 보조 해석 (예외 객체가 없을 때)
_TEXT_RULES = [
    (("resource busy", "rsrc_busy", "vi_error_rsrc_busy", "in use"),
     "장비가 이미 사용 중입니다(resource busy) — 다른 프로그램/연결이 잡고 있는지 확인하세요."),
    (("rsrc_nfound", "resource not found", "vi_error_rsrc_nfound"),
     "장비를 찾을 수 없습니다(not found) — 주소·전원·연결을 확인하세요."),
    (("rsrc_locked", "resource locked"),
     "장비가 잠겨 있습니다(locked) — 다른 세션이 독점 잠금 중."),
    (("timeout", "timed out", "vi_error_tmo", "tmo"),
     "응답 시간 초과(timeout) — 장비 상태/주소/명령 확인."),
    (("connection reset", "10054"),
     "연결이 끊겼습니다(reset) — 장비 전원/네트워크 확인."),
    (("connection refused", "10061"),
     "연결이 거부되었습니다(refused) — 포트/점유 확인."),
    (("connection lost", "conn_lost"),
     "연결이 끊겼습니다(connection lost) — 케이블/네트워크 확인."),
    (("invalid resource", "inv_rsrc", "invalid object", "inv_object"),
     "주소/세션이 유효하지 않습니다 — 주소 형식·재연결 확인."),
    (("not connected", "session not open"),
     "연결되어 있지 않습니다 — 먼저 장비에 연결하세요."),
]


def _humanize_text(text: str) -> str:
    low = text.lower()
    for keys, msg in _TEXT_RULES:
        if any(k in low for k in keys):
            return msg
    return ""


def format_error(exc: Any, context: str = "") -> str:
    """사람이 읽을 설명 + 한 줄 원본(상세). traceback은 포함하지 않는다."""
    human = humanize_error(exc)
    if isinstance(exc, BaseException):
        raw = f"{type(exc).__name__}: {exc}"
    else:
        raw = str(exc)
    head = f"{context}\n\n" if context else ""
    # 설명과 원본이 거의 같으면 원본 줄 생략
    if raw.strip() and raw.strip() not in human:
        return f"{head}{human}\n\n[상세] {raw}"
    return f"{head}{human}"


# ──────────────────────────────────────────────────────────
# Comm-error detection (폭넓게)
# ──────────────────────────────────────────────────────────

_COMM_KEYWORDS = (
    "visaioerror", "vi_error", "timeout", "timed out",
    "connection", "connectionreset", "connectionaborted", "connectionrefused",
    "broken pipe", "socket", "not connected", "session not open",
    "winerror 10", "gpib", "i/o error", "io error",
    "resource busy", "rsrc_busy", "in use",
    "rsrc_nfound", "resource not found", "rsrc_locked", "resource locked",
    "conn_lost", "connection lost", "unreachable",
)


def is_comm_error(err: Any) -> bool:
    """통신 오류 여부 — 재시도/자동 재개 판정용. 폭넓게 감지한다.

    측정값 파싱 오류(ValueError 등)와 구분하기 위해 통신/연결 계열만 True.
    """
    if err is None:
        return False
    # 예외 객체: 타입으로 우선 판정 (가장 신뢰도 높음)
    if isinstance(err, BaseException):
        if VisaIOError and isinstance(err, VisaIOError):
            return True
        if isinstance(err, (ConnectionError, TimeoutError, OSError, BrokenPipeError)):
            return True
        text = f"{type(err).__name__}: {err}".lower()
    else:
        text = str(err).lower()
    return any(k in text for k in _COMM_KEYWORDS)
