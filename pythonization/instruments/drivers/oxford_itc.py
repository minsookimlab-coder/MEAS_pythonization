from pythonization.instruments.base import BaseInstrument


class OxfordITC(BaseInstrument):
    """
    Oxford Instruments ITC503 (Intelligent Temperature Controller) 드라이버입니다.
    비표준(Non-SCPI) Oxford 프로토콜과 CR(\\r) 종단 문자를 사용합니다.

    지원 인터페이스: GPIB, RS232
    - 종단 문자: CR('\\r') — Oxford 장비 공통
    - RS232 기본 보드레이트: 9600 (extra_params 'baud_rate'로 덮어쓰기 가능)
    - ISOBUS 사용 시: extra_params 'isobus' 에 장비 ID 지정 (예: isobus=2)

    주요 커맨드 (실제 측정/설정은 visa_libraries.yaml 에 정의):
      R{n}   — 변수 읽기
                 R0: 설정 온도 (K)
                 R1: 센서 1 온도 (K) — 보통 제어 센서
                 R2: 센서 2 온도 (K)
                 R3: 센서 3 온도 (K)
                 R4: 온도 오차 (설정값 - 실측값) (K)
                 R5: 히터 출력 (% of max)
                 R6: 히터 출력 (V)
                 R7: 가스 유량 설정값 (%)
                 R8: 가스 유량 실측값 (%)
                 R9: PID - 비례 대역 (P)
                 R10: PID - 적분 시간 (I)
                 R11: PID - 미분 시간 (D)
      S{val} — 목표 온도 설정 (K), 예: S4.200
      H{val} — 히터 출력 수동 설정 (%), 예: H25.0
      G{val} — 가스 유량 수동 설정 (%), 예: G50.0
      O{n}   — 출력 제어 모드  0=자동/자동  1=수동/자동  2=자동/수동  3=수동/수동
      C{n}   — 제어 모드       0=Local  1=Remote  2=Local+Locked  3=Remote+Locked
      P{val} — PID 비례 대역 설정
      I{val} — PID 적분 시간 설정
      D{val} — PID 미분 시간 설정
      X      — 상태 읽기 (X-status word)
      V      — 버전 읽기
    """

    _default_timeout = 3000
    _default_read_termination = '\r'
    _default_write_termination = '\r'

    def _post_connect(self):
        # Oxford 장비는 인터페이스 종류에 관계없이 항상 CR 종단 문자를 사용합니다.
        self.inst.read_termination = self.read_termination
        self.inst.write_termination = self.write_termination

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

        GPIB: clear() (SDC) 후 장비 복구 대기가 필요합니다.
        RS232: C1(Remote) 설정 후 V를 조회합니다.
        """
        import time

        try:
            self.inst.clear()
            # GPIB SDC 후 Oxford 장비 복구 대기 (최소 500ms)
            time.sleep(0.5)
        except Exception:
            pass

        # RS232의 경우 Remote 모드(C1) 설정 시도
        # C1은 응답을 에코하므로 query()로 호출; 실패해도 계속 진행
        if self.interface_type == "RS232":
            try:
                self.query("C1")
            except Exception:
                pass

        response = self.query("V")
        return f"Oxford ITC Response: {response}"
