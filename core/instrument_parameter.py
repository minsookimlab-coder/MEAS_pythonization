"""
InstrumentParameter: 기기 내 개별 파라미터의 읽기/쓰기 추상화.

MeasurementParameter — 읽기 전용 (측정값)
SweepParameter       — 쓰기 + 1:1 대응 readback
"""
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.instrument_session import InstrumentSession


@dataclass
class MeasurementParameter:
    """
    Measurement parameter — 단일 query 명령으로 float 반환.

    TSP:  cmd_query = "print(smua.measure.i())"
    SCPI: cmd_query = "MEAS:CURR?"
    """
    name: str
    cmd_query: str

    def read(self, session: "InstrumentSession", alias: str) -> float:
        if not session.is_open(alias):
            session.open(alias)
        return float(session.query(alias, self.cmd_query).strip())


@dataclass
class SweepParameter:
    """
    Sweep parameter — 값을 기기에 설정하고, 짝꿍 MeasurementParameter로 현재값 확인.
    write와 readback은 항상 1:1 대응.
    """
    name: str
    cmd_set: str                      # {v} 자리에 목표값이 삽입되는 설정 명령
    readback: MeasurementParameter    # write한 값을 확인하는 짝꿍 read

    def write(self, session: "InstrumentSession", alias: str, value: float) -> None:
        if not session.is_open(alias):
            session.open(alias)
        session.write(alias, self.cmd_set.format(v=value))

    def read(self, session: "InstrumentSession", alias: str) -> float:
        return self.readback.read(session, alias)
