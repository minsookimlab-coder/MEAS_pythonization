"""AI 자동 측정 테스트용 가짜 장비·프로파일.

실제 VISA 없이 InstrumentSession 과 같은 인터페이스(is_open/open/write/query)를
흉내 낸다. 소스 값을 쓰면 측정값이 그 값의 함수로 바뀐다.
"""
import threading

from pythonization.automation.settings import (
    AutomationSettings,
    MeasurementPolicy,
    SecondChannelPolicy,
    SweepChannelPolicy,
    WriteCmdPolicy,
)
from pythonization.config.models import (
    InstantiatedMeasurement,
    InstantiatedSecondSweepChannel,
    InstantiatedSweepValue,
    InstantiatedWriteCmd,
    MainUIProfile,
    SecondSweepAdvanceType,
)


class FakeLab:
    """게이트 전압(SRC) · 락인(LIA) · 누설전류(SRC LEAK?) · 온도(ITC)."""

    def __init__(self, source: float = 0.0, temp: float = 300.0, leak_per_volt: float = 1e-11):
        self.source = source
        self.temp = temp
        self.leak_per_volt = leak_per_volt
        self.writes = []
        self.queries = []
        self._lock = threading.Lock()

    # InstrumentSession 인터페이스
    def is_open(self, alias):
        return True

    def open(self, alias):
        pass

    def write(self, alias, cmd):
        with self._lock:
            self.writes.append((alias, cmd))
            head, _, value = cmd.partition(" ")
            if head == "SET":
                self.source = float(value)
            elif head == "TSET":
                self.temp = float(value)

    def query(self, alias, cmd):
        with self._lock:
            self.queries.append((alias, cmd))
            if cmd == "READ:SRC?":
                return f"{self.source:.9g}"
            if cmd == "MEAS?":
                return f"{1e-3 * self.source:.9g}"
            if cmd == "LEAK?":
                return f"{self.leak_per_volt * abs(self.source):.9g}"
            if cmd == "TEMP?":
                return f"{self.temp:.9g}"
        raise RuntimeError(f"unknown query {cmd}")

    def source_writes(self):
        return [float(c.split()[1]) for a, c in self.writes if c.startswith("SET ")]


def make_profile() -> MainUIProfile:
    return MainUIProfile(
        sweep_values=[
            InstantiatedSweepValue(alias="SRC", description="smua voltage", cmd_set="SET {v}",
                                   paired_read_cmd="READ:SRC?", unit="V", figure_axis="Vg"),
            InstantiatedSweepValue(alias="SRC", description="smub voltage", cmd_set="SET {v}",
                                   paired_read_cmd="READ:SRC?", unit="V", figure_axis="Vb"),
        ],
        measurements=[
            InstantiatedMeasurement(alias="LIA", description="X", resolved_cmd="MEAS?",
                                    unit="V", figure_axis="X"),
            InstantiatedMeasurement(alias="SRC", description="leakage", resolved_cmd="LEAK?",
                                    unit="A", figure_axis="Ileak"),
        ],
        second_sweep_channels=[
            InstantiatedSecondSweepChannel(
                alias="ITC", description="VTI temperature", source_type="write_cmd",
                advance_type=SecondSweepAdvanceType.SIMPLE_HOP, cmd_set="TSET {v}",
                feedback_read_cmd="TEMP?", unit="K"),
        ],
        write_cmds=[
            InstantiatedWriteCmd(alias="SRC", description="output on", cmd_set="OUT 1"),
            InstantiatedWriteCmd(alias="SRC", description="range", cmd_set="RANGE {v}"),
        ],
    )


def make_settings(**overrides) -> AutomationSettings:
    cfg = AutomationSettings(
        sweep_channels=[
            SweepChannelPolicy(alias="SRC", description="smua voltage", allowed=True,
                               min_value=-20, max_value=20, max_rate=120, role="gate"),
            SweepChannelPolicy(alias="SRC", description="smub voltage", allowed=False,
                               min_value=-1, max_value=1),
        ],
        measurements=[
            MeasurementPolicy(alias="LIA", description="X", role="lock-in X"),
            MeasurementPolicy(alias="SRC", description="leakage", role="gate leakage",
                              abort_abs=1e-9),
        ],
        second_channels=[
            SecondChannelPolicy(alias="ITC", description="VTI temperature", allowed=True,
                                min_value=1.5, max_value=300),
        ],
        write_cmds=[
            WriteCmdPolicy(alias="SRC", description="output on", allowed=True),
            WriteCmdPolicy(alias="SRC", description="range", allowed=True,
                           min_value=0, max_value=10),
        ],
        min_time_per_point=0.05,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg
