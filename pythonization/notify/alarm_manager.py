"""
AlarmManager: Double Sweep 알람 발생 시 사운드 재생 및 이메일 전송.
"""
import ssl
import threading
from typing import Dict, List, Optional, TYPE_CHECKING

from pythonization.config.models import AlarmOperator

from pythonization.util.net_ssl import explain_ssl_error, make_ssl_context

if TYPE_CHECKING:
    from pythonization.config.models import AlarmConfig, AlarmTrigger


class AlarmManager:
    """
    알람 조건 체크 + 발동(사운드/이메일).

    check_and_fire()는 메인 스레드에서 호출.
    이메일 전송은 daemon thread에서 비동기 실행하여 UI를 블로킹하지 않음.
    """

    def __init__(self):
        self._last_reason: str = ""

    @property
    def last_reason(self) -> str:
        return self._last_reason

    # ------------------------------------------------------------------
    # Trigger evaluation
    # ------------------------------------------------------------------

    def check_measurement_triggers(
        self,
        triggers: List["AlarmTrigger"],
        meas_values: Dict[str, float],
    ) -> List[str]:
        """
        활성화된 measurement 트리거를 평가해 발동된 조건 설명 목록 반환.
        meas_values: {description: value}
        """
        fired = []
        _ops = {
            AlarmOperator.GT:  lambda v, t: v > t,
            AlarmOperator.LT:  lambda v, t: v < t,
            AlarmOperator.GTE: lambda v, t: v >= t,
            AlarmOperator.LTE: lambda v, t: v <= t,
            AlarmOperator.EQ:  lambda v, t: abs(v - t) < 1e-12,
            AlarmOperator.NEQ: lambda v, t: abs(v - t) >= 1e-12,
        }
        for trig in triggers:
            if not trig.enabled or trig.kind != "measurement":
                continue
            val = meas_values.get(trig.meas_description)
            if val is None:
                continue
            fn = _ops.get(trig.operator)
            if fn and fn(val, trig.threshold):
                fired.append(
                    f"{trig.meas_description} {trig.operator.value} {trig.threshold}"
                    f"  (actual: {val:.5g})"
                )
        return fired

    def has_comm_error_trigger(self, triggers: List["AlarmTrigger"]) -> bool:
        return any(t.enabled and t.kind == "comm_error" for t in triggers)

    def has_meas_error_trigger(self, triggers: List["AlarmTrigger"]) -> bool:
        return any(t.enabled and t.kind == "meas_error" for t in triggers)

    # ------------------------------------------------------------------
    # Fire alarm
    # ------------------------------------------------------------------

    def fire(self, cfg: "AlarmConfig", reason: str) -> None:
        """알람 발동: 사운드 + 이메일 + 텔레그램.

        on/off는 컨텍스트별 cfg.enabled로 판단하고, '전송 수단'(소리·이메일·텔레그램)은
        VNA·Double Sweep가 공유하는 전역 설정에서 읽는다. 전역 로드 실패 시 cfg로 폴백.
        """
        if not cfg.enabled:
            return
        self._last_reason = reason
        # 사운드(winsound.Beep ~1s 블로킹) + 설정 YAML 로드 + 이메일/텔레그램을 한 데몬
        # 스레드로 오프로드한다. fire()는 GUI 스레드 슬롯에서 호출되므로, 여기서 블로킹하면
        # 알람마다 메인 이벤트 루프가 ~1초씩 얼어붙는다(label 갱신은 호출부가 따로 처리).
        threading.Thread(target=self._fire_async, args=(cfg, reason), daemon=True).start()

    def _fire_async(self, cfg: "AlarmConfig", reason: str) -> None:
        try:
            from pythonization.config.app_config import load_alarm_delivery
            d = load_alarm_delivery()
            # 전역 전송설정이 '비어 있으면'(아직 마이그레이션 안 된 기존 사용자) 컨텍스트
            # cfg의 이메일/텔레그램 설정으로 폴백한다 — 안 그러면 조용히 미발송된다.
            if not d.is_configured():
                d = cfg
        except Exception:
            d = cfg   # 폴백: 전역 로드 실패 시 cfg의 delivery 사용 (필드명 동일)
        if d.use_sound:
            self._play_sound()
        if d.use_email and d.email_to.strip():
            self._send_email(d, reason)
        if d.use_telegram and d.telegram_bot_token.strip() and d.telegram_chat_id.strip():
            self._send_telegram(d.telegram_bot_token, d.telegram_chat_id, reason)
        # ntfy — 전역 전송설정에만 있다 (컨텍스트 AlarmConfig 폴백에는 없는 필드)
        if getattr(d, "ntfy_ready", lambda: False)():
            err = self._ntfy_publish(d, reason, title="Pythonization Alarm", priority=5,
                                     tags=["rotating_light"])
            if err:
                self._log_send_fail(f"ntfy failed: {err}")

    # ------------------------------------------------------------------
    # ntfy
    # ------------------------------------------------------------------

    @staticmethod
    def _ntfy_publish(d, text: str, title: str, priority: int = 3,
                      tags: Optional[List[str]] = None,
                      actions: Optional[List[dict]] = None) -> Optional[str]:
        from pythonization.notify.ntfy import publish
        return publish(d.ntfy_server, d.ntfy_topic, text, title=title, priority=priority,
                       tags=tags, actions=actions, token=d.ntfy_token)

    def send_ntfy(self, text: str, title: str = "Pythonization AI", priority: int = 3,
                  tags: Optional[List[str]] = None,
                  actions: Optional[List[dict]] = None) -> Optional[str]:
        """ntfy 로만 보낸다 (승인 버튼이 달린 알림 등). 동기 — GUI 스레드에서 부르지 말 것.

        반환: 실패 사유 (None = 성공). ntfy 가 설정돼 있지 않으면 그 사유.
        """
        try:
            from pythonization.config.app_config import load_alarm_delivery
            d = load_alarm_delivery()
        except Exception as e:
            return f"알람 전송 설정을 읽지 못했습니다: {e}"
        if not d.ntfy_ready():
            return "ntfy 가 설정되지 않았습니다."
        return self._ntfy_publish(d, text, title, priority, tags, actions)

    # ------------------------------------------------------------------
    # Sound
    # ------------------------------------------------------------------

    def _play_sound(self) -> None:
        try:
            import winsound
            winsound.Beep(880,  350)
            winsound.Beep(1100, 350)
            winsound.Beep(880,  350)
        except Exception:
            # Non-Windows: 콘솔 벨 시도
            try:
                print("\a", end="", flush=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------

    @staticmethod
    def _tg_ssl_ctx():
        # 검증된 TLS 컨텍스트. (예전엔 CERT_NONE+check_hostname=False로 인증서 검증을
        # 꺼서 봇 토큰이 MITM에 노출됐다. 검증은 켜둔 채 검증 주체만 OS 로 옮긴다 —
        # 이유는 pythonization/util/net_ssl.py 참고.)
        return make_ssl_context()

    def _send_telegram(self, token: str, chat_id: str, reason: str) -> None:
        err = self._telegram_post(token, chat_id, f"[Pythonization Alarm]\n{reason}")
        if err:
            self._log_send_fail(f"Telegram failed: {err}")

    def _telegram_post(self, token: str, chat_id: str, text: str) -> Optional[str]:
        """Telegram sendMessage 1회. 성공 시 None, 실패 시 사유 문자열."""
        try:
            import urllib.request, urllib.parse, json as _json
            payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=10, context=self._tg_ssl_ctx()) as resp:
                result = _json.loads(resp.read())
                if not result.get("ok"):
                    return f"Telegram error: {result}"
            return None
        except Exception as e:
            return explain_ssl_error(e) or str(e)

    # Telegram 메시지 길이 상한은 4096자 — 여유를 두고 나눈다
    _TELEGRAM_CHUNK = 3800

    def send_message(self, text: str, urgent: bool = False,
                     title: str = "Pythonization AI") -> List[str]:
        """사람에게 보내는 보고 — 알람 트리거와 무관하게 전역 전송 설정으로 보낸다.

        AI 자동 측정이 직접 쓴 문장을 그대로 텔레그램·이메일로 전달한다. 호출한
        스레드에서 동기로 보낸다 (GUI 스레드에서 부르지 말 것). 소리는 urgent 일 때만.
        반환: 실패 사유 목록 (빈 목록 = 모두 성공, 설정된 수단이 없으면 그 사유 하나).
        """
        try:
            from pythonization.config.app_config import load_alarm_delivery
            d = load_alarm_delivery()
        except Exception as e:
            return [f"알람 전송 설정을 읽지 못했습니다: {e}"]
        errors: List[str] = []
        sent_any = False
        if urgent and d.use_sound:
            self._play_sound()
        if d.use_telegram and d.telegram_bot_token.strip() and d.telegram_chat_id.strip():
            body = f"[{title}]\n{text}"
            for i in range(0, len(body), self._TELEGRAM_CHUNK):
                err = self._telegram_post(d.telegram_bot_token, d.telegram_chat_id,
                                          body[i:i + self._TELEGRAM_CHUNK])
                if err:
                    errors.append(f"Telegram: {err}")
                    self._log_send_fail(f"Telegram failed: {err}")
                    break
            else:
                sent_any = True
        if d.use_email and d.email_to.strip():
            first_line = text.strip().splitlines()[0] if text.strip() else ""
            err = self._smtp_send(d, f"[{title}] {first_line[:80]}", text)
            if err:
                errors.append(f"Email: {err}")
            else:
                sent_any = True
        if d.ntfy_ready():
            err = self._ntfy_publish(d, text, title, priority=5 if urgent else 3,
                                     tags=["warning"] if urgent else ["memo"])
            if err:
                errors.append(f"ntfy: {err}")
                self._log_send_fail(f"ntfy failed: {err}")
            else:
                sent_any = True
        if not sent_any and not errors:
            errors.append("ntfy·텔레그램·이메일 전송 수단이 설정되지 않았습니다 "
                          "(Settings → 알림 (ntfy) 에서 설정).")
        return errors

    @staticmethod
    def _log_send_fail(msg: str) -> None:
        # 패키징된 앱은 console=False라 print가 안 보임 → 로그파일에 남긴다.
        try:
            from pythonization.app.logging_setup import get_logger
            get_logger().error("[AlarmManager] %s", msg)
        except Exception:
            print(f"[AlarmManager] {msg}")

    def send_telegram_test(self, token: str, chat_id: str) -> Optional[str]:
        """테스트 메시지 전송. 성공 시 None, 실패 시 에러 문자열 반환."""
        try:
            import urllib.request, urllib.parse, json as _json
            text = "[Pythonization] Telegram 알람 연결 테스트"
            payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=10, context=self._tg_ssl_ctx()) as resp:
                result = _json.loads(resp.read())
                if result.get("ok"):
                    return None
                return str(result)
        except Exception as e:
            return explain_ssl_error(e) or str(e)

    def _send_email(self, cfg: "AlarmConfig", reason: str) -> None:
        body = (
            f"Pythonization — Alarm Triggered\n\n"
            f"Trigger: {reason}\n\n"
            f"This is an automated notification from Pythonization."
        )
        err = self._smtp_send(cfg, f"[Pythonization Alarm] {reason[:80]}", body)
        if err:
            # 이메일 실패는 조용히 기록 (UI 스레드 아님)
            print(f"[AlarmManager] Email failed: {err}")

    @staticmethod
    def _smtp_send(cfg, subject: str, body: str) -> Optional[str]:
        """SMTP 로 한 통 보낸다. 성공 시 None, 실패 시 사유 문자열."""
        try:
            import smtplib
            from email.mime.text import MIMEText

            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"]    = cfg.smtp_user or cfg.email_to
            msg["To"]      = cfg.email_to

            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as s:
                s.ehlo()
                s.starttls(context=make_ssl_context())
                s.ehlo()
                if cfg.smtp_user and cfg.smtp_password:
                    s.login(cfg.smtp_user, cfg.smtp_password)
                s.send_message(msg)
            return None
        except Exception as e:
            return explain_ssl_error(e) or str(e)
