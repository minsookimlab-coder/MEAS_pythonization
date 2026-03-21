from core.instrument_base import BaseInstrument


class OxfordITC(BaseInstrument):
    """
    Oxford Instruments ITC (Intelligent Temperature Controller) 드라이버입니다.
    비표준(Non-SCPI) 커맨드셋과 CR(\\r) 종단 문자를 사용합니다.
    """

    _default_timeout = 3000
    _default_read_termination = '\r'
    _default_write_termination = '\r'

    def _post_connect(self):
        # Oxford 장비는 인터페이스 종류에 관계없이 항상 CR 종단 문자를 사용합니다.
        self.inst.read_termination = self.read_termination
        self.inst.write_termination = self.write_termination

        # RS232 접속 시 보통 9600 보드레이트 사용 (파라미터로 덮어쓰기 권장)
        if self.interface_type == "RS232":
            self.inst.baud_rate = int(self.extra_params.get("baud_rate", 9600))

    def query(self, cmd: str) -> str:
        if not self.inst:
            raise ConnectionError(f"[{self.alias}] Not connected.")

        # ISOBUS 번호가 세팅되어 있다면 커맨드 앞에 주소 접두어를 붙입니다.
        isobus_id = self.extra_params.get("isobus", None)
        if isobus_id is not None:
            cmd = f"@{isobus_id}{cmd}"

        # Oxford 장비는 커맨드 전송 후 응답 생성까지 약간의 지연이 필요합니다.
        delay = float(self.extra_params.get("delay", 0.1))

        try:
            return self.inst.query(cmd, delay=delay)
        except Exception as e:
            import pyvisa
            if isinstance(e, pyvisa.errors.VisaIOError) and \
                    e.error_code == pyvisa.errors.StatusCode.error_timeout:
                raise TimeoutError(
                    f"응답 대기 시간 초과! (명령어 '{cmd}'에 대답이 없습니다.)\n\n"
                    f"체크포인트:\n"
                    f"1. 장비 전면부 버튼이 'Local' 이 아니라 'Remote' (혹은 RS232) 모드인지 확인하세요.\n"
                    f"2. GUI에서 동적 변수로 baud_rate를 (1200, 4800, 9600 등) 장비와 동일하게 추가했는지 확인하세요.\n"
                    f"3. 뒷면에 여러 대가 묶여있는 ISOBUS 모드라면, 동적 변수에 'Key: isobus / Value: 1' 과 같이 장비 고유 ID를 추가해보세요."
                ) from e
            raise

    def test_connection(self) -> str:
        """
        Oxford ITC는 SCPI '*IDN?'을 지원하지 않으므로
        버전 읽기 커맨드 'V'를 전송하여 응답을 받습니다.
        """
        self.inst.clear()
        response = self.query("V")
        return f"Oxford Response: {response}"
