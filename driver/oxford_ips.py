from core.instrument_base import BaseInstrument


class OxfordIPS(BaseInstrument):
    """
    Oxford Instruments IPS120 (Intelligent Power Supply) 드라이버입니다.
    ITC503과 동일한 비표준 Oxford 프로토콜과 CR(\\r) 종단 문자를 사용합니다.

    지원 인터페이스: GPIB, RS232
    - 종단 문자: CR('\\r') — Oxford 장비 공통
    - RS232 기본 보드레이트: 9600 (extra_params 'baud_rate'로 덮어쓰기 가능)
    - ISOBUS 사용 시: extra_params 'isobus' 에 장비 ID 지정 (예: isobus=3)

    주요 커맨드 (실제 측정/설정은 visa_libraries.yaml 에 정의):
      R{n}   — 변수 읽기
                 R0: 설정 전류 (A) — demand current / set point
                 R1: 설정 전압 (V) — demand voltage
                 R2: 측정 전류 (A) — measured current
                 R3: 측정 전압 (V) — measured voltage
                 R5: 목표 자기장 (T) — set point field
                 R7: 자기장 세기 (T) — persistent/current magnet field
                 R8: 목표 자기장 (T) — target field (sweeping toward)
                 R9: 스윕 속도 (A/min)
      S{val} — 목표 자기장/전류 설정, 예: S1.500 (단위: T 또는 A, 장비 모드 따라 다름)
      T{val} — 스윕 속도 설정 (A/min), 예: T0.100
      A{n}   — 활동(Activity) 제어
                 A0: Hold (현재 위치 유지)
                 A1: Go to Set Point (목표값으로 스윕)
                 A2: Go to Zero (0으로 스윕)
                 A4: Clamp (출력 차단)
      C{n}   — 제어 모드
                 C0: Local/Locked
                 C1: Remote/Unlocked  ← 측정 전 반드시 설정
                 C2: Local/Locked (대체)
                 C3: Remote/Locked
      H{n}   — Persistent 스위치 히터 제어
                 H0: Heater Off
                 H1: Heater On  (전환 전 반드시 magnet current = power supply current 확인!)
                 H2: Heater On (강제, 전류 확인 건너뜀)
      X      — 상태 읽기 (X-status word, 시스템 상태 비트필드)
      V      — 버전 읽기
    """

    _default_timeout = 5000
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
                    f"1. 장비가 Remote 모드인지 확인하세요 (C1 명령 필요).\n"
                    f"2. GUI에서 동적 변수로 baud_rate를 장비와 동일하게 설정했는지 확인하세요.\n"
                    f"3. ISOBUS 모드라면 'Key: isobus / Value: 장비ID' 를 동적 변수에 추가하세요."
                ) from e
            raise

    def test_connection(self) -> str:
        """
        Oxford IPS는 SCPI '*IDN?'을 지원하지 않으므로
        버전 읽기 커맨드 'V'로 연결을 확인합니다.

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
        if self.interface_type == "RS232":
            try:
                self.query("C1")
            except Exception:
                pass

        response = self.query("V")
        return f"Oxford IPS Response: {response}"
