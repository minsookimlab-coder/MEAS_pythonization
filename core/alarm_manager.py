"""
AlarmManager: Double Sweep 알람 발생 시 사운드 재생 및 이메일 전송.
"""
import threading
from typing import Dict, List, Optional, TYPE_CHECKING  # noqa: F401

if TYPE_CHECKING:
    from config.config_models import AlarmConfig, AlarmTrigger


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
        from config.config_models import AlarmOperator
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
        """알람 발동: 사운드 + 이메일 + 텔레그램 (설정에 따라)."""
        if not cfg.enabled:
            return
        self._last_reason = reason
        if cfg.use_sound:
            self._play_sound()
        if cfg.use_email and cfg.email_to.strip():
            threading.Thread(
                target=self._send_email,
                args=(cfg, reason),
                daemon=True,
            ).start()
        if cfg.use_telegram and cfg.telegram_bot_token.strip() and cfg.telegram_chat_id.strip():
            threading.Thread(
                target=self._send_telegram,
                args=(cfg.telegram_bot_token, cfg.telegram_chat_id, reason),
                daemon=True,
            ).start()

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
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _send_telegram(self, token: str, chat_id: str, reason: str) -> None:
        try:
            import urllib.request, urllib.parse, json as _json
            text = f"[Pythonization Alarm]\n{reason}"
            payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=10, context=self._tg_ssl_ctx()) as resp:
                result = _json.loads(resp.read())
                if not result.get("ok"):
                    print(f"[AlarmManager] Telegram error: {result}")
        except Exception as e:
            print(f"[AlarmManager] Telegram failed: {e}")

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
            return str(e)

    def _send_email(self, cfg: "AlarmConfig", reason: str) -> None:
        try:
            import smtplib
            from email.mime.text import MIMEText

            body = (
                f"Pythonization — Alarm Triggered\n\n"
                f"Trigger: {reason}\n\n"
                f"This is an automated notification from Pythonization."
            )
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = f"[Pythonization Alarm] {reason[:80]}"
            msg["From"]    = cfg.smtp_user or cfg.email_to
            msg["To"]      = cfg.email_to

            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                if cfg.smtp_user and cfg.smtp_password:
                    s.login(cfg.smtp_user, cfg.smtp_password)
                s.send_message(msg)
        except Exception as e:
            # 이메일 실패는 조용히 기록 (UI 스레드 아님)
            print(f"[AlarmManager] Email failed: {e}")
