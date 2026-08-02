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
        # 계측기(alias)별 VISA 통신 직렬화 락.
        # 같은 계측기(같은 TCP 연결)는 직렬화하지만, 서로 다른 계측기는
        # 독립 연결이라 동시 통신이 안전 → 병렬 측정을 가능하게 한다.
        # 통신 락은 '물리 리소스(주소)' 단위로 묶는다. 서로 다른 alias가 같은 장비를
        # 가리키면(예: instruments.yaml에 같은 IP를 두 alias가 공유) 병렬 측정 시 한 장비에
        # 동시 통신이 들어가 응답이 뒤섞여 데이터가 손상된다 → 같은 리소스면 같은 락으로 직렬화.
        self._alias_locks: Dict[str, threading.Lock] = {}
        self._resource_keys: Dict[str, str] = {}   # alias → 물리 리소스 키 (캐시)
        self._locks_guard = threading.Lock()  # _alias_locks dict 보호 (락 생성 직렬화)
        self._open_lock = threading.Lock()    # open/close 원자성 (중복 연결 방지)
        atexit.register(self.shutdown)        # 한 번만 등록

    def _resource_key(self, alias: str) -> str:
        """alias를 물리 리소스 키(interface|address|port)로 해석. 미상이면 alias로 폴백."""
        k = self._resource_keys.get(alias)
        if k is not None:
            return k
        try:
            cfg = self._registry.get_config(alias)
        except Exception:
            cfg = None
        if cfg is None:
            return alias   # 아직 등록정보 미상 → alias로 (캐시 안 함, 다음에 재시도)
        addr = (getattr(cfg, "address", "") or "").strip().lower()
        port = getattr(cfg, "port", "") or ""
        itype = (getattr(cfg, "interface_type", "") or "").strip().lower()
        k = f"{itype}|{addr}|{port}" if addr else alias
        self._resource_keys[alias] = k
        return k

    def _alias_lock(self, alias: str) -> threading.Lock:
        """물리 리소스별 통신 락을 lazily 생성/반환한다(같은 장비 = 같은 락)."""
        key = self._resource_key(alias)
        lk = self._alias_locks.get(key)
        if lk is None:
            with self._locks_guard:
                lk = self._alias_locks.get(key)
                if lk is None:
                    lk = threading.Lock()
                    self._alias_locks[key] = lk
        return lk

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
        if self._shut_down:
            raise RuntimeError("InstrumentSession is shut down")
        with self._open_lock:
            # 이미 연결된 경우 즉시 반환 (중복 연결 방지)
            if alias in self._instruments:
                return

            self._registry.reload()
            config = self._registry.get_config(alias)
            if config is None:
                raise KeyError(f"Alias '{alias}' not found in instruments.yaml")

            # viOpen은 같은 alias의 viClose(eviction/close)와 겹치면 NI-VISA 내부
            # 세션 테이블이 손상(heap corruption, 0xC0000374)되므로 alias 락으로
            # 직렬화한다. eviction의 동기 disconnect(아래)와 상호배제된다.
            with self._alias_lock(alias):
                try:
                    inst = self._factory.create_instrument(config)
                    inst.connect()
                except Exception as e:
                    # 통신 오류 + IP가 바뀐 정황이면 MAC으로 현재 IP를 재감지하고
                    # 저장된 IP를 갱신한 뒤 1회 재연결을 시도한다.
                    inst = self._reconnect_via_mac(alias, config, e)
                    if inst is None:
                        raise   # IP 변경 없음/재시도 실패 → 기존 방식대로 전파
                self._instruments[alias] = inst
            print(f"[Session] '{alias}' connected.")

    def _reconnect_via_mac(self, alias, config, exc):
        """
        통신 오류로 연결에 실패했을 때, 등록된 MAC 주소로 현재 IP를 다시 찾아
        저장된 IP와 다르면 instruments.yaml의 IP를 갱신한 뒤 재연결을 시도한다.

        기존 MAC 추적 로직(network_utils.resolve_address)은 '연결 시점'에 ARP로
        IP를 알아내 쓰지만 그 결과를 저장하지는 않는다. 이 메서드는 그 위에서
        '연결 실패 후' 변경된 IP를 영구 저장(yaml)하고 한 번 더 시도하는 보강 단계다.

        반환값: 재연결에 성공한 BaseInstrument, 아니면 None
                (None이면 호출부가 원래 예외를 그대로 전파 — 기존 방식대로 처리).
        """
        from core.visa_errors import is_comm_error
        from core.network_utils import find_ip_for_mac

        # 통신 오류가 아니거나, IP 재감지가 의미 없는 인터페이스/설정이면 패스
        if not is_comm_error(exc):
            return None
        if getattr(config, "interface_type", "") != "LAN":
            return None
        mac = (getattr(config, "mac_address", "") or "").strip()
        if not mac:
            return None

        saved_ip = (getattr(config, "address", "") or "").strip()
        new_ip = find_ip_for_mac(mac)
        if not new_ip or new_ip == saved_ip:
            # IP 변경이 확인되지 않음 → 기존 방식대로 처리
            return None

        print(f"[Session] '{alias}' 통신 오류 감지 → MAC({mac}) 기준 IP 재감지: "
              f"{saved_ip or '(없음)'} → {new_ip}. 저장된 IP를 갱신하고 재시도합니다.")
        self._registry.update_address(alias, new_ip)
        self._registry.reload()
        new_config = self._registry.get_config(alias) or config
        try:
            inst = self._factory.create_instrument(new_config)
            inst.connect()
            return inst
        except Exception as e2:
            print(f"[Session] '{alias}' IP 갱신 후 재연결도 실패: {e2}")
            return None

    def close(self, alias: str):
        """alias 장비 연결을 닫습니다."""
        with self._open_lock:
            # 진행 중인 해당 계측기 통신이 끝날 때까지 대기 후 제거
            with self._alias_lock(alias):
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

    def get_instrument(self, alias: str):
        """alias에 연결된 BaseInstrument 인스턴스를 반환합니다. 없으면 None."""
        return self._instruments.get(alias)

    # ------------------------------------------------------------------
    # VISA commands
    # ------------------------------------------------------------------

    def _evict_broken(self, alias: str, exc: Exception) -> None:
        """
        VISA 연결 수준의 오류 발생 시 세션을 제거합니다.
        다음 write/query 시 open()이 자동으로 호출되어 재연결됩니다.

        timeout(VI_ERROR_TMO)은 연결 자체는 살아있으므로 제거하지 않습니다.
        그 외 VisaIOError (연결 끊김, invalid object 등)는 제거합니다.
        호출 시점: 해당 alias 락 보유 중 — 그 락 안에서 동기로 disconnect한다.
        """
        if not isinstance(exc, pyvisa.errors.VisaIOError):
            return
        if exc.error_code == pyvisa.errors.StatusCode.error_timeout:
            return  # timeout은 세션 유지 (재연결 불필요)
        # 연결 오류: _instruments에서 제거 후 '동기'로 disconnect.
        # (예전엔 데몬 스레드로 viClose를 던졌는데, 그 close가 워커의 자동 재오픈
        #  viOpen(open())과 같은 NI-VISA 리소스에서 동시 실행되어 세션 테이블이
        #  손상(0xC0000374)되는 race였다. 호출자가 alias 락을 쥐고 있으므로 여기서
        #  동기로 닫으면 같은 alias의 viOpen은 이 락이 풀린 뒤에야 실행된다.)
        inst = self._instruments.pop(alias, None)
        if inst:
            self._safe_disconnect(inst)
            print(f"[Session] '{alias}' session evicted due to: {exc}")

    @staticmethod
    def _safe_disconnect(inst: BaseInstrument) -> None:
        try:
            inst.disconnect()
        except Exception:
            pass

    def write(self, alias: str, cmd: str):
        """alias 장비에 VISA 명령어를 전송합니다 (응답 없음)."""
        with self._alias_lock(alias):
            if self._shut_down:
                raise RuntimeError("InstrumentSession is shut down")
            try:
                inst = self._instruments.get(alias)
                if inst is None:                 # 다른 스레드가 pop/evict한 경우
                    raise ConnectionError(f"'{alias}' is not connected")
                inst.write(cmd)
                self._emit_log(alias, "write", cmd)
            except Exception as e:
                self._emit_log(alias, "write_err", cmd, f"{type(e).__name__}: {e}")
                self._evict_broken(alias, e)
                raise

    def read(self, alias: str) -> str:
        """alias 장비로부터 응답을 읽어 반환합니다."""
        with self._alias_lock(alias):
            if self._shut_down:
                raise RuntimeError("InstrumentSession is shut down")
            try:
                inst = self._instruments.get(alias)
                if inst is None:
                    raise ConnectionError(f"'{alias}' is not connected")
                result = inst.read()
                self._emit_log(alias, "read", "", result)
                return result
            except Exception as e:
                self._emit_log(alias, "read_err", "", f"{type(e).__name__}: {e}")
                self._evict_broken(alias, e)
                raise

    def query(self, alias: str, cmd: str) -> str:
        """alias 장비에 명령어를 전송하고 응답을 읽어 반환합니다."""
        with self._alias_lock(alias):
            if self._shut_down:
                raise RuntimeError("InstrumentSession is shut down")
            try:
                inst = self._instruments.get(alias)
                if inst is None:
                    raise ConnectionError(f"'{alias}' is not connected")
                result = inst.query(cmd)
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
