"""Anthropic Claude API 연결.

`anthropic` 패키지는 선택 의존성이다 (requirements-optional.txt). 없어도 앱은 뜨고,
AI 자동 측정 창에서 시작할 때만 안내한다 — zhinst/scipy 와 같은 규칙으로 지역 import.

요청 형태:
  - client.beta.messages.create — 서버측 fallback(거절 시 다른 모델로 이어서 처리)과
    진행 상황 표시(thinking display "updates") 가 beta 기능이라 beta 경로를 쓴다.
  - thinking 은 adaptive. effort 는 설정값(기본 high — 장시간 도구 호출 작업).
  - 시스템 프롬프트와 도구 정의는 고정 → 자동 프롬프트 캐시(cache_control).
  - 비스트리밍, max_tokens 16000 — SDK 의 10분 요청 제한 아래.
"""
import os
from typing import Optional, Tuple

FALLBACK_BETA = "server-side-fallback-2026-07-01"
UPDATES_BETA = "thinking-display-updates-2026-08-18"


class LlmUnavailable(Exception):
    """SDK 가 없거나 자격증명이 없어 LLM 을 쓸 수 없음."""


def sdk_available() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def has_env_credentials() -> bool:
    """SDK 기본 경로(환경변수)에 자격증명이 있는지. 값은 읽어 보지 않는다."""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def make_client(api_key: str = "", timeout: float = 600.0, base_url: Optional[str] = None):
    """Anthropic 클라이언트. api_key 가 비면 SDK 기본 자격증명 경로를 쓴다."""
    try:
        import anthropic
    except Exception as exc:
        raise LlmUnavailable(
            "anthropic 패키지가 없습니다. 'pip install -r requirements-optional.txt' 로 "
            "설치한 뒤 다시 시작하세요.") from exc
    kwargs = {"timeout": timeout, "max_retries": 2}
    if api_key.strip():
        kwargs["api_key"] = api_key.strip()
    if base_url:
        kwargs["base_url"] = base_url
    try:
        return anthropic.Anthropic(**kwargs)
    except Exception as exc:
        raise LlmUnavailable(
            "Claude API 자격증명이 없습니다. AI 자동 측정 창의 [LLM 설정] 에 API 키를 "
            "넣거나 ANTHROPIC_API_KEY 환경변수를 설정하세요.") from exc


def build_request(*, model: str, effort: str, max_tokens: int, system: str,
                  tools: list, messages: list) -> dict:
    """client.beta.messages.create 에 넘길 인자."""
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "tools": tools,
        "messages": messages,
        # 생각 과정은 숨기고, 도구 호출 사이의 진행 메모만 텍스트로 받는다
        "thinking": {"type": "adaptive", "display": "updates"},
        "output_config": {"effort": effort},
        "betas": [FALLBACK_BETA, UPDATES_BETA],
        "fallbacks": "default",
        "cache_control": {"type": "ephemeral"},
    }


def describe_api_error(exc: Exception) -> Tuple[str, bool]:
    """API 예외 → (사람이 읽을 설명, 잠시 뒤 재시도할 만한지)."""
    try:
        import anthropic
    except Exception:
        return f"{type(exc).__name__}: {exc}", False

    if isinstance(exc, anthropic.AuthenticationError):
        return "API 키가 올바르지 않습니다 (401). [LLM 설정] 에서 키를 확인하세요.", False
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "이 API 키로는 해당 모델을 쓸 수 없습니다 (403).", False
    if isinstance(exc, anthropic.NotFoundError):
        return "모델 이름이 잘못됐습니다 (404). [LLM 설정] 에서 모델을 확인하세요.", False
    if isinstance(exc, anthropic.BadRequestError):
        return f"요청 형식 오류 (400): {getattr(exc, 'message', exc)}", False
    if isinstance(exc, anthropic.RateLimitError):
        return "API 사용량 한도에 걸렸습니다 (429). 잠시 뒤 다시 시도합니다.", True
    if isinstance(exc, anthropic.APIStatusError):
        code = getattr(exc, "status_code", 0) or 0
        if code >= 500:
            return f"Claude API 서버 오류 ({code}). 잠시 뒤 다시 시도합니다.", True
        return f"Claude API 오류 ({code}): {getattr(exc, 'message', exc)}", False
    if isinstance(exc, anthropic.APITimeoutError):
        return "Claude API 응답 시간 초과. 다시 시도합니다.", True
    if isinstance(exc, anthropic.APIConnectionError):
        return "Claude API 에 연결할 수 없습니다 (네트워크). 다시 시도합니다.", True
    return f"{type(exc).__name__}: {exc}", False
