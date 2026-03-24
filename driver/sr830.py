from core.instrument_base import BaseInstrument


class SR830(BaseInstrument):
    """
    Stanford Research Systems SR830 Lock-in Amplifier 드라이버.

    지원 인터페이스: GPIB, RS232
    - GPIB : SR830은 EOI 신호로 응답을 종단합니다.
             base class가 설정한 '\\n' 종단 문자를 비활성화(= '')하여
             EOI 전용 종단으로 동작시킵니다.
    - RS232: 종단 문자 '\\r', baud_rate는 extra_params에 지정 (기본 9600).

    실제 측정/설정 커맨드는 visa_libraries.yaml에 정의합니다.
    이 드라이버는 연결 설정만 담당합니다.
    """

    _default_timeout = 5000
    _default_read_termination = '\n'
    _default_write_termination = '\n'

    def _post_connect(self):
        if self.interface_type == "GPIB":
            # GPIB: EOI 전용 종단. 문자 기반 종단을 비활성화합니다.
            # base class connect()가 '\n'을 설정한 것을 여기서 덮어씁니다.
            self.inst.read_termination = ''

        elif self.interface_type == "RS232":
            # RS232: SR830 응답 종단은 CR('\r')
            self.inst.read_termination = '\r'
            self.inst.write_termination = '\r'
            self.inst.baud_rate = int(self.extra_params.get("baud_rate", 9600))

        elif self.interface_type == "LAN" and self.port and self.port != 0:
            self.inst.read_termination = self.read_termination
            self.inst.write_termination = self.write_termination

    def test_connection(self) -> str:
        """
        연결 테스트. 쿼리 전에 버퍼를 비워 잔여 데이터로 인한
        타임아웃을 방지합니다.
        """
        self.inst.clear()
        return self.query("*IDN?")
