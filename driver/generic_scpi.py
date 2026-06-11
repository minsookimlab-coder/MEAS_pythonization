import time

from core.instrument_base import BaseInstrument


class GenericSCPIInstrument(BaseInstrument):
    """
    일반적인 SCPI 표준 규격(IEEE 488.2)을 완벽히 지원하는 계측기들의 공통 드라이버입니다.
    대상: Rhode&Schwarz SMC100A, Keysight E5071, Keithley 2636A 등.

    extra_params:
      split_semicolons (bool): True로 설정 시 세미콜론으로 연결된 명령어를
                               하나씩 분리해 전송합니다.
                               세미콜론 chaining을 지원하지 않는 장비(예: Oxford Mercury iPS)에 사용.
                               각 write 후 장비 응답을 drain하여 수신 버퍼 오염을 방지합니다.
    """

    _default_timeout = 5000
    _default_read_termination = '\n'
    _default_write_termination = '\n'

    # split_semicolons 모드에서 응답 drain에 사용하는 짧은 타임아웃 (ms)
    # - Oxford Mercury iPS는 보통 50 ms 이내에 응답 → 200 ms로 충분
    # - 응답이 없는 장비는 이 시간 후 예외를 흡수하고 fallback sleep으로 진행
    _DRAIN_TIMEOUT_MS = 200
    _DRAIN_FALLBACK_SLEEP = 0.05  # drain 실패 시 대기 시간 (초)

    def _post_connect(self):
        # LAN Raw Socket 또는 RS232는 종단 문자를 명시적으로 설정해야 합니다.
        if (self.interface_type == "LAN" and self.port and self.port != 0) \
                or self.interface_type == "RS232":
            self.inst.read_termination = self.read_termination
            self.inst.write_termination = self.write_termination

        if self.interface_type == "RS232" and "baud_rate" in self.extra_params:
            self.inst.baud_rate = int(self.extra_params["baud_rate"])

    def _drain_response(self) -> None:
        """write 후 장비가 돌려주는 응답을 읽어 수신 버퍼에서 제거한다.

        Oxford Mercury iPS처럼 모든 SET 명령에 STAT:... 응답을 보내는 장비에서
        응답을 읽지 않으면 이후 query() 호출이 엉뚱한 응답을 반환한다.
        짧은 타임아웃(_DRAIN_TIMEOUT_MS)을 사용하므로 응답이 없는 장비에서도
        오래 블로킹되지 않는다.
        """
        orig = self.inst.timeout
        self.inst.timeout = self._DRAIN_TIMEOUT_MS
        try:
            self.inst.read()
        except Exception:
            time.sleep(self._DRAIN_FALLBACK_SLEEP)
        finally:
            self.inst.timeout = orig

    def write(self, cmd: str):
        if not self.inst:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        if self.extra_params.get("split_semicolons") and ';' in cmd:
            for sub in (s.strip() for s in cmd.split(';') if s.strip()):
                self.inst.write(sub)
                self._drain_response()   # 응답 drain → 버퍼 오염 방지
        else:
            self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        if not self.inst:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        if self.extra_params.get("split_semicolons") and ';' in cmd:
            parts = [s.strip() for s in cmd.split(';') if s.strip()]
            # 마지막 이전 명령: write + drain (응답 버려도 됨)
            for sub in parts[:-1]:
                self.inst.write(sub)
                self._drain_response()
            # 마지막 명령: query로 실제 응답 반환
            return self.inst.query(parts[-1])
        return self.inst.query(cmd)
