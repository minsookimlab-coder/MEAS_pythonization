import pyvisa
from pythonization.instruments.base import BaseInstrument


class Keithley2636A(BaseInstrument):
    """
    Keithley 2636A SourceMeter 전용 드라이버.
    TSP(Test Script Processor) 명령어 체계를 사용합니다.
    SCPI와 달리 응답을 받으려면 print()로 감싸야 합니다.
    """

    _default_timeout = 5000
    _default_read_termination = '\n'
    _default_write_termination = '\n'

    def _post_connect(self):
        # TSP 응답은 항상 \n 종단이므로 인터페이스 종류에 관계없이 명시적으로 설정
        self.inst.read_termination = self.read_termination
        self.inst.write_termination = self.write_termination

        # [필수] TSP 인터랙티브 프롬프트(TSP>) 비활성화.
        # 활성 상태에서는 print() 응답에 "TSP>" 문자열이 섞여 read가 타임아웃 납니다.
        self.write("localnode.prompts = 0")

        # [필수] 버퍼 클리어.
        # localnode.prompts = 0 전송 시점에 prompts가 아직 ON 상태이므로
        # 장비가 "TSP>\n" 을 수신 버퍼에 씁니다.
        # clear()로 이 stale 데이터를 제거해야 이후 query가 올바른 응답을 읽습니다.
        self.inst.clear()

        # 이전 세션에서 남은 에러 큐 초기화
        self.write("errorqueue.clear()")

    # ------------------------------------------------------------------
    # TSP 핵심 래퍼
    # ------------------------------------------------------------------

    def tsp_query(self, cmd: str) -> str:
        """
        TSP 표현식을 print()로 감싸 전송하고 응답을 반환합니다.
        타임아웃/IO 에러 발생 시 버퍼를 자동으로 복구합니다.

        예시:
            tsp_query("smub.measure.v()")
            → VISA로 "print(smub.measure.v())" 전송
            → "1.23456e-03" 수신 및 반환
        """
        try:
            return self.query(f"print({cmd})").strip()
        except pyvisa.errors.VisaIOError:
            # 타임아웃/부분 수신 후 버퍼에 잔여 데이터가 남을 수 있음.
            # clear()로 버퍼를 비워 다음 step이 오염되지 않도록 복구.
            self.inst.clear()
            raise

    # ------------------------------------------------------------------
    # 전압/전류 측정
    # ------------------------------------------------------------------

    def measure_voltage(self, channel: str = "smua") -> float:
        """채널의 전압을 측정하여 float으로 반환합니다."""
        return float(self.tsp_query(f"{channel}.measure.v()"))

    def measure_current(self, channel: str = "smua") -> float:
        """채널의 전류를 측정하여 float으로 반환합니다."""
        return float(self.tsp_query(f"{channel}.measure.i()"))

    def measure_resistance(self, channel: str = "smua") -> float:
        """채널의 저항을 측정하여 float으로 반환합니다."""
        return float(self.tsp_query(f"{channel}.measure.r()"))

    def measure_power(self, channel: str = "smua") -> float:
        """채널의 전력을 측정하여 float으로 반환합니다."""
        return float(self.tsp_query(f"{channel}.measure.p()"))

    # ------------------------------------------------------------------
    # 소스 설정
    # ------------------------------------------------------------------

    def source_voltage(self, channel: str, voltage: float):
        """채널을 전압 소스 모드로 설정하고 전압값을 설정합니다."""
        self.write(f"{channel}.source.func = {channel}.OUTPUT_DCVOLTS")
        self.write(f"{channel}.source.levelv = {voltage}")

    def source_current(self, channel: str, current: float):
        """채널을 전류 소스 모드로 설정하고 전류값을 설정합니다."""
        self.write(f"{channel}.source.func = {channel}.OUTPUT_DCAMPS")
        self.write(f"{channel}.source.leveli = {current}")

    # ------------------------------------------------------------------
    # 출력 제어
    # ------------------------------------------------------------------

    def output_on(self, channel: str):
        """채널 출력을 켭니다."""
        self.write(f"{channel}.source.output = {channel}.OUTPUT_ON")

    def output_off(self, channel: str):
        """채널 출력을 끕니다."""
        self.write(f"{channel}.source.output = {channel}.OUTPUT_OFF")

    # ------------------------------------------------------------------
    # 기타
    # ------------------------------------------------------------------

    def reset(self):
        """장비를 초기화합니다."""
        self.write("reset()")

    def beep(self, frequency: float = 2400, duration: float = 0.5):
        """알림음을 냅니다. frequency(Hz), duration(초)."""
        self.write(f"beeper.beep({duration}, {frequency})")

    def test_connection(self) -> str:
        """2636A는 *IDN?를 지원합니다."""
        return self.query("*IDN?")

    def drain_error_queue(self) -> list[tuple[int, str]]:
        """에러 큐를 전부 읽고 비웁니다. (code, msg) 리스트 반환."""
        errors = []
        while True:
            count = int(float(self.query("print(errorqueue.count)").strip()))
            if count == 0:
                break
            raw = self.query(
                'do local c,m,s,n = errorqueue.next()'
                ' print(tostring(c) .. "\\t" .. tostring(m)) end'
            ).strip()
            parts = raw.split("\t", 1)
            try:
                code = int(float(parts[0]))
                msg = parts[1] if len(parts) > 1 else ""
                errors.append((code, msg))
            except (ValueError, IndexError):
                break
        return errors
