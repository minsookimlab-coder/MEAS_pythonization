from core.instrument_base import BaseInstrument


class GenericSCPIInstrument(BaseInstrument):
    """
    일반적인 SCPI 표준 규격(IEEE 488.2)을 완벽히 지원하는 계측기들의 공통 드라이버입니다.
    대상: Rhode&Schwarz SMC100A, Keysight E5071, Keithley 2636A 등.
    """

    _default_timeout = 5000
    _default_read_termination = '\n'
    _default_write_termination = '\n'

    def _post_connect(self):
        # LAN Raw Socket 또는 RS232는 종단 문자를 명시적으로 설정해야 합니다.
        if (self.interface_type == "LAN" and self.port and self.port != 0) \
                or self.interface_type == "RS232":
            self.inst.read_termination = self.read_termination
            self.inst.write_termination = self.write_termination

        if self.interface_type == "RS232" and "baud_rate" in self.extra_params:
            self.inst.baud_rate = int(self.extra_params["baud_rate"])
