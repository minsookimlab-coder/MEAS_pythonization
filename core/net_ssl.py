"""
외부 HTTPS/SMTP 연결에 쓸 TLS 컨텍스트.

왜 따로 있나 — Windows 의 Python 은 `ssl.create_default_context()` 를 만들 때
Windows 인증서 저장소의 **그 시점 스냅샷**을 OpenSSL 로 넘긴다. Windows 는 필요한
루트 인증서를 처음 쓸 때 자동으로 내려받는데(automatic root update), 아직 받아오지
않은 상태면 OpenSSL 은 체인을 못 세우고 이렇게 실패한다:

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    self-signed certificate in certificate chain

같은 서버를 Windows 자체 검증기로 확인하면 정상(SslPolicyErrors.None)인데 Python 만
실패하므로, 사용자 눈에는 '알람 Test 가 가끔 된다'로 보인다. 실제로 이 PC 에서
api.telegram.org 가 그랬다.

그래서 검증을 끄지 않고(예전에 CERT_NONE 으로 껐다가 봇 토큰이 MITM 에 노출됐다),
검증 주체를 더 믿을 만한 쪽으로 옮긴다:

  1. truststore  — 검증을 Windows 자체 API 에 위임한다. 저장소 스냅샷 문제가 없고
                   사내 프록시가 설치한 CA 도 그대로 인정된다. (권장)
  2. certifi     — 최신 공인 CA 번들. OS 저장소가 낡았을 때의 차선책.
  3. 표준 라이브러리 기본값 — 위 둘이 없을 때.

두 패키지는 선택 의존성이다. 없으면 3번으로 동작하며, 그 경우 위 증상이 재발할 수 있다.
설치: pip install truststore certifi   (pythonization.bat 이 자동 설치한다)
"""
import ssl
from typing import Optional, Tuple

_cached: Optional[Tuple[ssl.SSLContext, str]] = None


def _build() -> Tuple[ssl.SSLContext, str]:
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), "truststore(OS 저장소)"
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where()), "certifi 번들"
    except Exception:
        pass
    return ssl.create_default_context(), "표준 라이브러리 기본값"


def make_ssl_context() -> ssl.SSLContext:
    """검증이 켜진 TLS 컨텍스트. 프로세스당 한 번만 만든다."""
    global _cached
    if _cached is None:
        _cached = _build()
    return _cached[0]


def backend_name() -> str:
    """현재 쓰는 검증 백엔드 이름 (오류 메시지·진단용)."""
    if _cached is None:
        make_ssl_context()
    return _cached[1]


def explain_ssl_error(exc: BaseException) -> Optional[str]:
    """인증서 검증 실패면 사용자가 할 수 있는 조치를 담은 안내문, 아니면 None."""
    text = f"{type(exc).__name__}: {exc}"
    if "CERTIFICATE_VERIFY" not in text and "certificate verify failed" not in text:
        return None
    return (
        f"서버 인증서를 검증하지 못했습니다 (검증 백엔드: {backend_name()}).\n\n"
        f"{text}\n\n"
        "확인해 보세요:\n"
        "  1. truststore 패키지 설치 — 검증을 Windows 에 맡겨 대부분 해결됩니다.\n"
        "       pip install truststore certifi\n"
        "  2. 사내 방화벽·백신이 HTTPS 를 가로채는 환경이면, 그 프로그램의 루트\n"
        "     인증서를 Windows '신뢰할 수 있는 루트 인증 기관' 에 설치하세요.\n"
        "  3. 시스템 날짜·시간이 맞는지 확인하세요 (틀리면 인증서가 만료로 보입니다).\n\n"
        "보안을 위해 인증서 검증을 끄지는 않습니다 — 끄면 봇 토큰·메일 비밀번호가\n"
        "중간자 공격에 노출됩니다."
    )
