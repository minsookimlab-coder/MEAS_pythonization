"""측정 리포트를 '언제' 보낼지 판단하고 보낸다.

측정 코드(VNA·메인 sweep)는 갱신이 있을 때마다 `on_update()` 를 부르기만 하면 된다.
보낼지 말지(설정·주기), 어느 스레드에서 보낼지는 여기서 정한다.

왜 따로 두나
  - `report.py` 는 '문서를 만든다' 까지만 한다(순수 조립, 테스트 쉬움).
  - 전송은 네트워크라 **GUI 스레드에서 하면 창이 멈춘다.** 측정 갱신 신호는 GUI
    스레드에서 오므로, 여기서 데몬 스레드로 던진다.
  - 리포트를 만드는 비용(파일 읽기 + 그래프 그리기)도 GUI 스레드에서 치르면 안 된다.
    그래서 **조립까지 통째로** 워커 스레드에서 한다 — 호출부는 값만 넘긴다.

주기
  설정의 `ntfy_report_every_n` 만큼 갱신이 쌓일 때마다 1회 보낸다(1 이면 매번).
  `force=True`(측정 완료·오류)면 횟수와 무관하게 보낸다.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, Optional, Sequence

#: 동시에 보내는 리포트는 하나로 제한한다 — 갱신이 몰릴 때 스레드가 쌓이면
#: 느린 서버 하나로 메모리와 소켓이 늘어난다. 보내는 중이면 그 갱신은 건너뛴다.
_SEND_TIMEOUT_HINT = "이전 리포트를 보내는 중이라 이번 갱신은 건너뜁니다."


class ReportSender:
    """측정 갱신 → (주기 판단) → 리포트 조립·전송.

    스레드 안전하다. GUI 스레드에서 `on_update()` 를 불러도 즉시 돌아온다.
    """

    def __init__(self, alarm_manager=None, *,
                 delivery_loader: Optional[Callable[[], object]] = None,
                 on_error: Optional[Callable[[str], None]] = None):
        self._alarm = alarm_manager
        self._load_delivery = delivery_loader
        self._on_error = on_error or (lambda _m: None)
        self._lock = threading.Lock()
        self._count = 0
        self._busy = False
        self._last_error = ""

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """측정을 새로 시작할 때 — 갱신 횟수를 0 으로."""
        with self._lock:
            self._count = 0

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    # ------------------------------------------------------------------
    def _delivery(self):
        if self._load_delivery is not None:
            return self._load_delivery()
        from pythonization.config.app_config import load_alarm_delivery
        return load_alarm_delivery()

    def _manager(self):
        if self._alarm is not None:
            return self._alarm
        from pythonization.notify.alarm_manager import AlarmManager
        self._alarm = AlarmManager()
        return self._alarm

    def should_send(self, force: bool = False) -> bool:
        """설정과 주기를 보고 이번 갱신에 보낼지 판단한다 (횟수를 1 올린다)."""
        try:
            d = self._delivery()
        except Exception as e:
            self._on_error(f"리포트 설정을 읽지 못했습니다: {e}")
            return False
        if not getattr(d, "report_ready", lambda: False)():
            return False
        every = max(1, int(getattr(d, "ntfy_report_every_n", 1) or 1))
        with self._lock:
            self._count += 1
            n = self._count
        return bool(force or n % every == 0)

    # ------------------------------------------------------------------
    def on_update(self, *, title: str,
                  conditions: Optional[Dict[str, object]] = None,
                  data_file=None,
                  x_col: Optional[str] = None,
                  y_cols: Optional[Sequence[str]] = None,
                  instrument_values: Optional[Dict[str, object]] = None,
                  progress: str = "", note: str = "",
                  force: bool = False, urgent: bool = False) -> bool:
        """갱신 1회. 보낼 차례면 **백그라운드로** 조립·전송하고 True 를 돌려준다.

        돌려주는 값은 '보내기로 했는가' 지 '보냈는가' 가 아니다 — 전송 결과는
        비동기라 `last_error` 로 확인한다. 호출부(GUI)는 기다리지 않는다.
        """
        if not self.should_send(force=force):
            return False
        with self._lock:
            if self._busy:
                self._last_error = _SEND_TIMEOUT_HINT
                return False
            self._busy = True

        args = dict(title=title, conditions=conditions, data_file=data_file,
                    x_col=x_col, y_cols=y_cols,
                    instrument_values=instrument_values,
                    progress=progress, note=note)
        t = threading.Thread(target=self._work, args=(args, urgent),
                             name="report-sender", daemon=True)
        t.start()
        return True

    def _work(self, args: dict, urgent: bool) -> None:
        err = ""
        try:
            from pythonization.notify.report import build_report
            report = build_report(**args)
            errors = self._manager().send_report(report, urgent=urgent)
            err = "; ".join(errors)
        except Exception as e:                   # noqa: BLE001 — 알림 실패가 측정을 멈추면 안 된다
            err = f"리포트 전송 실패: {type(e).__name__}: {e}"
        finally:
            with self._lock:
                self._busy = False
                self._last_error = err
            if err:
                self._on_error(err)
