"""
Zurich Instruments MFLI lock-in amplifier 드라이버 (LabOne / zhinst 기반).

MFLI는 SCPI/VISA 장비가 아니라 **LabOne Data Server + zhinst 노드 트리**로 제어된다.
따라서 pyvisa ResourceManager를 쓰지 않고, 이미 실행 중인 Data Server(기본 localhost:8004)에
zhinst.core.ziDAQServer로 접속한 뒤 노드 경로를 get/set한다.

BaseInstrument의 문자열 in / 문자열 out 계약(write/query)을 그대로 만족하므로, 상위 계층
(InstrumentSession·MeasurementParameter·SweepParameter)은 이 장비를 다른 SCPI 장비와 동일하게
다룰 수 있다. "명령"은 노드 경로다:
  - set  : "/{dev}/oscs/0/freq = 1000.0"   → daq.setDouble(path, 1000.0)
  - get  : "/{dev}/demods/0/sample.r"      → str(daq.getDouble(path))
  '{dev}'는 extra_params["device_id"](예: "dev3025")로 치환된다.

주의:
  - 장비 세팅(필터·time constant·PSD/noise 노드 등)은 LabOne에서 하고, 이 드라이버는 측정만 한다.
  - Data Server를 새로 띄우지 않는다 — 이미 떠 있는 서버에 '접속'만 한다.
  - write(주파수 변경) 직후 settle_s만큼 대기해 lock-in 출력이 정착한 뒤 읽히도록 한다
    (GUI 타이밍은 write→read 사이를 정착시키지 않으므로 여기서 캡슐화).
"""
import time

from pythonization.instruments.base import BaseInstrument


class ZurichMFLI(BaseInstrument):
    """MFLI via LabOne Data Server (zhinst.core). 연결만 담당하고 노드 경로를 명령으로 번역한다."""

    _default_timeout = 5000

    # ------------------------------------------------------------------
    # 명령 문자열 ↔ 노드 경로 변환 헬퍼
    # ------------------------------------------------------------------
    def _subst_dev(self, s: str) -> str:
        """'{dev}' 자리표시자를 'devNNNN' 로 치환. 템플릿은 '/{dev}/...' 형식이므로
        여기서 앞에 '/'를 붙이지 않는다(안 그러면 '//devNNNN/...' 이중 슬래시)."""
        return s.replace("{dev}", self.dev_id)

    def _parse_set(self, cmd: str):
        """'path = value' → (치환된 path, float value)."""
        if "=" not in cmd:
            raise ValueError(f"MFLI write는 'path = value' 형식이어야 합니다: {cmd!r}")
        path, val = cmd.split("=", 1)
        return self._subst_dev(path.strip()), float(val.strip())

    # ------------------------------------------------------------------
    # 연결 관리 (pyvisa 대신 zhinst)
    # ------------------------------------------------------------------
    def connect(self):
        try:
            import zhinst.core  # LabOne Python API
        except ImportError as e:
            raise ImportError(
                "MFLI 드라이버는 'zhinst' 패키지가 필요합니다 (LabOne Python API). "
                "pip install zhinst 후 다시 시도하세요."
            ) from e

        self.dev_id      = str(self.extra_params.get("device_id", "dev0"))
        self.server_host = str(self.extra_params.get("server_host", self.address or "localhost"))
        self.server_port = int(self.extra_params.get("server_port", self.port or 8004))
        self.api_level   = int(self.extra_params.get("api_level", 6))
        self.iface       = str(self.extra_params.get("iface", "1GbE"))
        self._settle_s   = float(self.extra_params.get("settle_s", 0.1))

        # 이미 실행 중인 Data Server에 '접속'만 한다 (새 서버를 띄우지 않음).
        self.daq = zhinst.core.ziDAQServer(self.server_host, self.server_port, self.api_level)
        # BaseInstrument의 'if not self.inst' 가드를 통과시키는 sentinel.
        self.inst = self.daq

        # 핵심: 장비가 이미 데이터 서버에 연결돼 있으면(LabOne 사용 중, 또는 PCIe로 물려 있으면)
        # connectDevice가 불필요하고, 원격/PCIe에서 재연결을 시도하면 오래 걸리다 timeout난다.
        # → 먼저 노드를 읽어 '접근 가능'하면 그대로 끝내고, 안 될 때만 connectDevice를 fallback으로 쓴다.
        def _accessible():
            try:
                self.daq.getString(f"/{self.dev_id}/features/serial")
                return True
            except Exception:
                return False

        self._we_connected = False       # 우리가 connectDevice로 연결했는지(정리 시 필요)
        if _accessible():
            return   # 이미 접근 가능 → connectDevice 호출 안 함(=timeout 회피, 장비 소유 안 함)

        try:
            self.daq.connectDevice(self.dev_id, self.iface)
            self._we_connected = True
        except Exception as e:
            raise ConnectionError(
                f"[{self.alias}] 장비 {self.dev_id} 연결 실패(connectDevice, iface={self.iface}): "
                f"{type(e).__name__}: {e}. LabOne에서 장비가 연결돼 있는지, "
                f"server_host({self.server_host})/iface 설정이 맞는지 확인하세요.") from e
        if not _accessible():
            raise ConnectionError(
                f"[{self.alias}] connectDevice 후에도 {self.dev_id} 노드 접근 불가.")

    def disconnect(self):
        # 중요: 우리가 직접 connectDevice로 연결한 경우에만 disconnectDevice 한다.
        # 이미 연결돼 있던 장비(LabOne 사용 중)를 여기서 떼어내면 LabOne 측정을 방해하고
        # 장비가 'in use' 전이 상태가 되어 이후 접속이 막힌다. 그냥 우리 클라이언트 참조만 버린다.
        daq = getattr(self, "daq", None)
        if daq is not None and getattr(self, "_we_connected", False):
            try:
                daq.disconnectDevice(self.dev_id)
            except Exception:
                pass
        self.daq = None
        self.inst = None

    # ------------------------------------------------------------------
    # 노드 get/set
    # ------------------------------------------------------------------
    def write(self, cmd: str):
        """'path = value' 노드 설정. 주파수 변경 후 settle_s 대기(락인 정착)."""
        if getattr(self, "daq", None) is None:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        path, val = self._parse_set(cmd)
        self.daq.setDouble(path, val)
        self.daq.sync()                    # 장비에 적용될 때까지 대기
        if self._settle_s > 0:
            time.sleep(self._settle_s)     # lock-in 정착 대기 (이후 read가 정착값을 받도록)

    def query(self, cmd: str) -> str:
        """노드 경로를 읽어 스칼라 float 문자열로 반환."""
        if getattr(self, "daq", None) is None:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        path = self._subst_dev(cmd.strip())
        return str(float(self.daq.getDouble(path)))

    def test_connection(self) -> str:
        """연결 확인 — 장비 시리얼을 읽어 반환."""
        if getattr(self, "daq", None) is None:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        try:
            serial = self.daq.getString(f"/{self.dev_id}/features/serial")
        except Exception:
            serial = "?"
        return f"MFLI {self.dev_id} OK (serial {serial})"
