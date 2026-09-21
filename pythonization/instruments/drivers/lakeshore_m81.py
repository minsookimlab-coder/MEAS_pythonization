from pythonization.instruments.base import BaseInstrument


class M81Instrument(BaseInstrument):
    """
    PyVISA를 이용한 M81 계측기(또는 일반 LAN 장비) 실전 제어 클래스 템플릿입니다.
    """

    _default_timeout = 2000
    _default_read_termination = '\n'
    _default_write_termination = '\n'

    def _post_connect(self):
        # Raw Socket (포트 번호 존재)일 경우에만 종료 문자 설정이 필요합니다.
        if self.interface_type == "LAN" and self.port and self.port != 0:
            self.inst.read_termination = self.read_termination
            self.inst.write_termination = self.write_termination

    def query(self, cmd: str) -> str:
        """M81 쿼리 후 버퍼에 남은 잔여 데이터를 제거합니다.

        M81은 하나의 쿼리에 여러 줄을 응답하는 경우가 있습니다.
        첫 번째 줄만 유효한 값이므로 나머지는 다음 쿼리 오염을 막기 위해 버립니다.
        """
        if not self.inst:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        self.inst.write(cmd)
        response = self.inst.read()
        # 버퍼에 남은 잔여 데이터 제거 (짧은 타임아웃으로 이미 도착한 데이터만 드레인)
        saved = self.inst.timeout
        self.inst.timeout = 10  # 10 ms — 버퍼가 비어있으면 즉시 타임아웃
        try:
            for _ in range(256):   # 안전 상한: 장비가 계속 보내도 무한루프 방지
                self.inst.read()
        except Exception:
            pass
        finally:
            self.inst.timeout = saved
        return response
