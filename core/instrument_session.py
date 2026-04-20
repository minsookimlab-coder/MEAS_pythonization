"""
InstrumentSession: alias 기반으로 장비에 연결을 유지하며
write / read / query 를 수행하는 세션 매니저.
"""
import atexit
import threading
import pyvisa
from typing import Dict, Optional

from core.instrument_base import BaseInstrument
from core.instrument_factory import InstrumentFactory
from core.instrument_registry import InstrumentRegistry


class InstrumentSession:
    """
    alias 기반 계측기 세션 매니저.

    사용 예시:
        session = InstrumentSession()
        session.open("M81")
        session.write("M81", "SOUR:FREQ 1e9")
        resp = session.query("M81", "*IDN?")
        val  = session.read("M81")
        session.close("M81")

    또는 with 블록:
        with InstrumentSession() as session:
            session.open("M81")
            print(session.query("M81", "*IDN?"))

    락 구조:
        _lock     : write / read / query 직렬화 (VISA 통신 보호)
        _open_lock: open / close 원자성 보장 (중복 연결 방지)
        두 락을 동시에 잡을 때는 항상 _open_lock → _lock 순서로 획득.
    """

    def __init__(self, registry: Optional[InstrumentRegistry] = None):
        self._registry = registry or InstrumentRegistry()
        self._rm = pyvisa.ResourceManager()
        self._factory = InstrumentFactory(self._rm)
        self._instruments: Dict[str, BaseInstrument] = {}
        self._log_callbacks = []
        self._shut_down = False
        self._log_enabled = True
        self._lock = threading.Lock()       # VISA 통신 직렬화
        self._open_lock = threading.Lock()  # open/close 원자성 (중복 연결 방지)
        atexit.register(self.shutdown)      # 한 번만 등록

    def set_log_enabled(self, enabled: bool):  # ← 새 메서드 추가
        """VISA 로깅 활성/비활성화"""
        self._log_enabled = enabled

    def add_log_callback(self, cb):
        """VISA 명령어 로그 콜백을 등록합니다. cb(alias, cmd_type, cmd, result)."""
        self._log_callbacks.append(cb)

    def _emit_log(self, alias: str, cmd_type: str, cmd: str, result=None):
        if not self._log_enabled:  # ← 조건 추가
            return  # 로그 비활성화 시 콜백 호출 건너뜀
        for cb in self._log_callbacks:
            try:
                cb(alias, cmd_type, cmd, result)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def open(self, alias: str):
        """alias 장비에 연결합니다. 이미 열려 있으면 재사용합니다."""
        with self._open_lock:
            # 이미 연결된 경우 즉시 반환 (중복 연결 방지)
            if alias in self._instruments:
                return

            self._registry.reload()
            config = self._registry.get_config(alias)
            if config is None:
                raise KeyError(f"Alias '{alias}' not found in instruments.yaml")

            inst = self._factory.create_instrument(config)
            inst.connect()
            self._instruments[alias] = inst
            print(f"[Session] '{alias}' connected.")

    def close(self, alias: str):
        """alias 장비 연결을 닫습니다."""
        with self._open_lock:
            # 진행 중인 VISA 통신이 끝날 때까지 대기 후 제거
            with self._lock:
                inst = self._instruments.pop(alias, None)
        if inst:
            try:
                inst.disconnect()
            except Exception:
                pass
            print(f"[Session] '{alias}' disconnected.")

    def close_all(self):
        """열린 모든 연결을 닫습니다."""
        for alias in list(self._instruments.keys()):
            self.close(alias)

    def shutdown(self):
        """
        모든 연결을 닫고 ResourceManager를 완전히 해제합니다.
        LabVIEW / NI-MAX 등 다른 프로그램이 즉시 리소스를 사용할 수 있습니다.
        atexit에 등록되어 있어 비정상 종료 시에도 자동 호출됩니다.
        """
        if self._shut_down:
            return
        self._shut_down = True

        self.close_all()

        try:
            self._rm.close()
            print("[Session] ResourceManager closed.")
        except Exception as e:
            print(f"[Session] ResourceManager close failed: {e}")

    def is_open(self, alias: str) -> bool:
        return alias in self._instruments

    # ------------------------------------------------------------------
    # VISA commands
    # ------------------------------------------------------------------

    def _evict_broken(self, alias: str, exc: Exception) -> None:
        """
        VISA 연결 수준의 오류 발생 시 세션을 제거합니다.
        다음 write/query 시 open()이 자동으로 호출되어 재연결됩니다.

        timeout(VI_ERROR_TMO)은 연결 자체는 살아있으므로 제거하지 않습니다.
        그 외 VisaIOError (연결 끊김, invalid object 등)는 제거합니다.
        호출 시점: _lock 보유 중 — disconnect는 락 해제 후 별도 처리.
        """
        if not isinstance(exc, pyvisa.errors.VisaIOError):
            return
        if exc.error_code == pyvisa.errors.StatusCode.error_timeout:
            return  # timeout은 세션 유지 (재연결 불필요)
        # 연결 오류: _instruments에서 제거 (락 보유 중이므로 dict.pop만 수행)
        inst = self._instruments.pop(alias, None)
        if inst:
            # disconnect는 데몬 스레드에서 — _lock을 오래 붙잡지 않도록
            import threading as _t
            _t.Thread(target=self._safe_disconnect, args=(inst,), daemon=True).start()
            print(f"[Session] '{alias}' session evicted due to: {exc}")

    @staticmethod
    def _safe_disconnect(inst: BaseInstrument) -> None:
        try:
            inst.disconnect()
        except Exception:
            pass

    def write(self, alias: str, cmd: str):
        """alias 장비에 VISA 명령어를 전송합니다 (응답 없음)."""
        with self._lock:
            try:
                self._instruments[alias].write(cmd)
                self._emit_log(alias, "write", cmd)
            except Exception as e:
                self._emit_log(alias, "write_err", cmd, f"{type(e).__name__}: {e}")
                self._evict_broken(alias, e)
                raise

    def read(self, alias: str) -> str:
        """alias 장비로부터 응답을 읽어 반환합니다."""
        with self._lock:
            try:
                result = self._instruments[alias].read()
                self._emit_log(alias, "read", "", result)
                return result
            except Exception as e:
                self._emit_log(alias, "read_err", "", f"{type(e).__name__}: {e}")
                self._evict_broken(alias, e)
                raise

    def query(self, alias: str, cmd: str) -> str:
        """alias 장비에 명령어를 전송하고 응답을 읽어 반환합니다."""
        with self._lock:
            try:
                result = self._instruments[alias].query(cmd)
                self._emit_log(alias, "query", cmd, result)
                return result
            except Exception as e:
                self._emit_log(alias, "query_err", cmd, f"{type(e).__name__}: {e}")
                self._evict_broken(alias, e)
                raise

    # ------------------------------------------------------------------
    # Convenience: open이 안 된 상태에서도 단발성으로 쓸 수 있는 버전
    # ------------------------------------------------------------------

    def query_once(self, alias: str, cmd: str) -> str:
        """
        연결 → query → 연결 해제를 한 번에 수행합니다.
        이미 열려 있는 경우 연결을 유지합니다.
        """
        already_open = self.is_open(alias)
        if not already_open:
            self.open(alias)
        try:
            return self.query(alias, cmd)
        finally:
            if not already_open:
                self.close(alias)

    def write_once(self, alias: str, cmd: str):
        """
        연결 → write → 연결 해제를 한 번에 수행합니다.
        이미 열려 있는 경우 연결을 유지합니다.
        """
        already_open = self.is_open(alias)
        if not already_open:
            self.open(alias)
        try:
            self.write(alias, cmd)
        finally:
            if not already_open:
                self.close(alias)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close_all()
