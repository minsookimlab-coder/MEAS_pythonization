"""
경량 로깅 + 크래시 캡처.

목적: "이유 없이 프로그램이 중단됨"을 진단할 수 있게, 평소엔 거의 리소스를 쓰지 않으면서
중단 직전 상황과 크래시 원인을 파일에 남긴다.

구성요소
  - RotatingFileHandler 로거(app.log): 주요 이벤트(INFO)와 예외(ERROR)만 기록. 버퍼 I/O라 가볍다.
  - faulthandler(fault.log): segfault 등 '파이썬으로 못 잡는' 네이티브 크래시의 스택을 덤프.
    평소엔 아무 일도 하지 않으므로(치명 시그널에만 동작) 오버헤드 0.
  - sys.excepthook / threading.excepthook: 메인·워커 스레드의 미처리 예외 전체 스택 기록.
  - qInstallMessageHandler: Qt 경고/치명 메시지 기록(크래시 직전에 자주 나타남).

로그 위치: <data_dir>/logs/  (app.log, fault.log)
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

_LOGGER_NAME = "pythonization"
_initialized = False
_fault_fp = None   # faulthandler 파일 핸들(GC 방지용 보관)


def setup_logging(log_dir: Path) -> logging.Logger:
    """로깅·크래시 캡처를 1회 설정하고 로거를 반환한다. 중복 호출은 무시."""
    global _initialized, _fault_fp
    logger = logging.getLogger(_LOGGER_NAME)
    if _initialized:
        return logger

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    logger.setLevel(logging.INFO)
    try:
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(threadName)s %(message)s"))
        logger.addHandler(handler)
    except Exception:
        pass
    logger.propagate = False

    # 네이티브 크래시(segfault 등) 스택 덤프 — 치명 시그널에만 동작(평소 오버헤드 0)
    try:
        import faulthandler
        _fault_fp = open(log_dir / "fault.log", "a", encoding="utf-8")
        faulthandler.enable(file=_fault_fp, all_threads=True)
    except Exception:
        pass

    # 메인 스레드 미처리 예외
    def _excepthook(exc_type, exc, tb):
        try:
            logger.error("Uncaught exception:\n%s",
                         "".join(traceback.format_exception(exc_type, exc, tb)))
        finally:
            sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _excepthook

    # 워커 스레드 미처리 예외 (Python 3.8+)
    def _thread_excepthook(args):
        name = args.thread.name if args.thread else "?"
        logger.error("Uncaught thread exception in %s:\n%s", name,
                     "".join(traceback.format_exception(
                         args.exc_type, args.exc_value, args.exc_traceback)))
    try:
        threading.excepthook = _thread_excepthook
    except Exception:
        pass

    # Qt 메시지(경고/치명) 기록
    try:
        from PySide6.QtCore import qInstallMessageHandler, QtMsgType

        def _qt_handler(mode, ctx, msg):
            lvl = (logging.ERROR
                   if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg)
                   else logging.WARNING)
            logger.log(lvl, "Qt: %s", msg)
        qInstallMessageHandler(_qt_handler)
    except Exception:
        pass

    _initialized = True
    logger.info("=== logging started (faulthandler=%s) ===",
                "faulthandler" in sys.modules)
    return logger


def get_logger() -> logging.Logger:
    """설정된 로거 반환. setup 전이라도 안전(파일 핸들러 없으면 그냥 무동작)."""
    return logging.getLogger(_LOGGER_NAME)
