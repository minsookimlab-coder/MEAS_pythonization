"""
창을 마우스로 끌거나 크기 조절하는 동안에도 Qt 이벤트를 계속 돌린다 (Windows 전용).

문제 — Windows 는 창 이동/크기조절이 시작되면 `WM_ENTERSIZEMOVE` 를 보내고
**OS 자신의 모달 메시지 루프**로 들어간다. 그 동안 Qt 의 이벤트 루프는 돌지 않으므로
**포스팅된 이벤트가 전달되지 않는다.**

이 프로그램의 측정 루프는 그 전달에 의존한다:

    스텝 타이머(GUI) -> _sweep_tick -> request_step  --큐잉-->  워커 스레드
    워커가 VISA 수행 -> step_done  --큐잉-->  _on_step_done(GUI) -> 다음 스텝 예약

즉 창을 끄는 동안에는 `step_done` 이 GUI 에 도달하지 못해 **다음 스텝이 예약되지
않고 측정이 멈춘다**. 놓았을 때 밀린 것이 한꺼번에 처리되면서 '랙' 으로 보인다.

해결 — 모달 루프도 `WM_TIMER` 는 디스패치한다. 그래서 `WM_ENTERSIZEMOVE` 에서
네이티브 타이머를 걸고, 그 `WM_TIMER` 마다 `processEvents()` 로 Qt 의 밀린 이벤트를
흘려보낸다. `WM_EXITSIZEMOVE` 에서 타이머를 없앤다.

사용자 입력 이벤트는 제외하고 흘린다 — 드래그 중에 클릭/키가 재진입으로 처리되면
엉뚱한 동작이 난다. 측정에 필요한 것은 큐잉된 시그널과 타이머뿐이다.

Windows 가 아니거나 설치에 실패하면 조용히 아무 일도 하지 않는다(기능 저하만 발생).
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QEventLoop

_WM_ENTERSIZEMOVE = 0x0231
_WM_EXITSIZEMOVE = 0x0232
_WM_TIMER = 0x0113

#: 모달 루프 중 이벤트를 흘리는 주기(ms). 측정 스텝(보통 ≥50 ms)보다 촘촘해야 한다.
_PUMP_INTERVAL_MS = 10
#: 우리 타이머 식별자 (같은 창의 다른 타이머와 겹치지 않게)
_TIMER_ID = 0xA17E


class _SizeMovePump(QAbstractNativeEventFilter):

    def __init__(self) -> None:
        super().__init__()
        import ctypes
        self._user32 = ctypes.windll.user32
        self._ctypes = ctypes
        self._active_hwnd = None

        class _POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class _MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint),
                ("pt", _POINT),
            ]

        self._MSG = _MSG

    def nativeEventFilter(self, event_type, message):
        try:
            msg = self._ctypes.cast(
                int(message), self._ctypes.POINTER(self._MSG)).contents
        except Exception:
            return False, 0

        if msg.message == _WM_ENTERSIZEMOVE:
            self._active_hwnd = msg.hwnd
            self._user32.SetTimer(self._ctypes.c_void_p(msg.hwnd),
                                  self._ctypes.c_size_t(_TIMER_ID),
                                  self._ctypes.c_uint(_PUMP_INTERVAL_MS), None)
        elif msg.message == _WM_EXITSIZEMOVE:
            if self._active_hwnd is not None:
                self._user32.KillTimer(self._ctypes.c_void_p(self._active_hwnd),
                                       self._ctypes.c_size_t(_TIMER_ID))
                self._active_hwnd = None
        elif msg.message == _WM_TIMER and msg.wParam == _TIMER_ID:
            # 모달 루프 안 — 밀린 Qt 이벤트를 흘려 측정 루프를 계속 돌린다.
            # 사용자 입력은 제외한다(드래그 중 재진입 방지).
            QCoreApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            return True, 0      # 이 타이머 메시지는 우리가 소비한다

        return False, 0


_installed: "_SizeMovePump | None" = None


def install(app) -> bool:
    """앱에 필터를 설치한다. 성공 여부를 반환(진단용)."""
    global _installed
    if _installed is not None:
        return True
    if not sys.platform.startswith("win"):
        return False
    try:
        pump = _SizeMovePump()
        app.installNativeEventFilter(pump)
        _installed = pump      # GC 로 사라지면 필터가 죽는다 — 참조를 유지한다
        return True
    except Exception as exc:
        print(f"[win_modal_pump] 설치 실패(기능 저하만 발생): {exc}")
        return False
