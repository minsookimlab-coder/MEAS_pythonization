from pythonization.instruments.base import BaseInstrument


class GenericSCPIInstrument(BaseInstrument):
    """
    일반적인 SCPI 표준 규격(IEEE 488.2)을 완벽히 지원하는 계측기들의 공통 드라이버입니다.
    대상: Rhode&Schwarz SMC100A, Keysight E5071, Keithley 2636A 등.

    extra_params:
      split_semicolons (bool): True로 설정 시 세미콜론으로 연결된 명령어를
                               하나씩 분리해 전송합니다.
                               세미콜론 chaining을 지원하지 않고 '모든 명령에 응답을
                               돌려주는' 장비(예: Oxford Mercury iPS/iTC)에 사용.
                               이 모드에서는 strict request-response로 동작한다:
                                 · write 한 줄 → 응답 1줄(ack)을 반드시 읽어 버퍼를 비움
                                 · query 직전 → 남아있는 stale 응답을 flush해 동기 보정
                               이렇게 해야 한 번이라도 응답을 놓쳐 read가 한 칸씩 밀리는
                               (자기장이 0/NaN으로 찍히거나 거짓 HOLD가 잡히는) 문제를 막는다.
    """

    _default_timeout = 5000
    _default_read_termination = '\n'
    _default_write_termination = '\n'

    # split_semicolons(=Mercury류 strict req-resp) 모드 타임아웃.
    # 모든 명령이 STAT 응답을 돌려주므로 ack를 넉넉한 시간(_ACK_TIMEOUT_MS) 안에 확실히
    # 읽어 버퍼 동기를 유지한다. (기존 200ms는 응답이 조금만 늦어도 ack를 놓쳐 desync 발생)
    _ACK_TIMEOUT_MS   = 2000
    # query 직전 '이미 도착해 있는' stale 응답만 빠르게 비우는 flush 타임아웃.
    # 새 응답을 기다리지 않으므로 짧게 둔다(버퍼가 비어 있으면 이 시간만큼만 대기 후 종료).
    _FLUSH_TIMEOUT_MS = 80

    def _post_connect(self):
        # LAN Raw Socket 또는 RS232는 종단 문자를 명시적으로 설정해야 합니다.
        if (self.interface_type == "LAN" and self.port and self.port != 0) \
                or self.interface_type == "RS232":
            self.inst.read_termination = self.read_termination
            self.inst.write_termination = self.write_termination

        if self.interface_type == "RS232" and "baud_rate" in self.extra_params:
            self.inst.baud_rate = int(self.extra_params["baud_rate"])

        # Mercury류: 연결 직후 이전 세션의 잔여 응답이 남아 있을 수 있어 비우고 시작한다.
        if self.extra_params.get("split_semicolons"):
            self._flush_input()

    def _read_ack(self) -> None:
        """write 후 장비가 돌려주는 STAT 응답 1줄을 읽어 버퍼에서 제거(동기 유지).

        split_semicolons 장비는 모든 명령에 응답하므로 넉넉한 타임아웃으로 확실히 읽는다.
        (이례적으로) 응답이 없어 타임아웃하면 흡수하되, 동기는 이후 query의 flush가 보정한다.
        """
        orig = self.inst.timeout
        self.inst.timeout = self._ACK_TIMEOUT_MS
        try:
            self.inst.read()
        except Exception:
            pass
        finally:
            self.inst.timeout = orig

    def _flush_input(self) -> None:
        """수신 버퍼에 '이미 도착해 있는' stale 응답을 모두 읽어 버린다(동기 보정).

        매우 짧은 타임아웃으로, 새 응답을 기다리지 않고 이미 버퍼에 있는 줄만 비운다.
        버퍼가 비면 마지막 read가 타임아웃나며 종료한다.
        """
        orig = self.inst.timeout
        self.inst.timeout = self._FLUSH_TIMEOUT_MS
        try:
            for _ in range(64):   # 안전 상한: 장비가 계속 보내도 무한루프에 빠지지 않음
                self.inst.read()
        except Exception:
            pass
        finally:
            self.inst.timeout = orig

    def write(self, cmd: str):
        if not self.inst:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        if self.extra_params.get("split_semicolons"):
            for sub in (s.strip() for s in cmd.split(';') if s.strip()):
                self.inst.write(sub)
                self._read_ack()         # 응답(ack) 1줄 읽어 버퍼 비움 → 동기 유지
        else:
            self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        if not self.inst:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        if self.extra_params.get("split_semicolons"):
            parts = [s.strip() for s in cmd.split(';') if s.strip()]
            # 마지막 이전 명령: write + ack 읽기 (응답 버려도 됨)
            for sub in parts[:-1]:
                self.inst.write(sub)
                self._read_ack()
            # 동기 보정: 혹시 이전에 놓친 stale 응답이 있으면 비운 뒤, 마지막 명령을 query.
            # 이렇게 하면 한 번 어긋난 동기도 다음 query에서 스스로 복구된다.
            self._flush_input()
            return self.inst.query(parts[-1])
        return self.inst.query(cmd)
