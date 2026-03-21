from core.instrument_base import BaseInstrument


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
