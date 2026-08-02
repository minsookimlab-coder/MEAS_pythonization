"""
InstrumentParameter: 기기 내 개별 파라미터의 읽기/쓰기 추상화.

MeasurementParameter — 읽기 전용 (측정값)
SweepParameter       — 쓰기 + 1:1 대응 readback
"""
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.instrument_session import InstrumentSession

# 응답 문자열에서 부동소수점 숫자를 추출하는 정규식.
# Mercury iTC/iPS 등은 'STAT:DEV:MB1.T1:TEMP:SIG:TEMP:235.7446K' 형식으로 응답.
_NUMERIC_RE = re.compile(r'[-+]?\d+\.?\d*(?:[eE][-+]?\d+)?')


def _parse_float(raw: str) -> float:
    """
    응답 문자열을 float로 변환합니다.

    1. 직접 float() 변환 시도.
    2. 실패 시 Mercury iTC/iPS 형식 처리:
       'STAT:DEV:MB1.T1:TEMP:SIG:TEMP:235.7446K' → 마지막 ':' 이후 숫자 추출.
    """
    try:
        return float(raw)
    except ValueError:
        # 마지막 ':' 구분자 이후 토큰에서 '선행 숫자 + 단위 접미사' 형태만 허용한다.
        # (예: '235.7446K' → 235.7446). 남는 부분에 또 다른 숫자/연산자가 있으면
        #  복합·모호 응답('12.5/3.0', '1.2.3', '3 of 5')이므로 잘못된 값을 뽑지 말고 에러.
        last_token = raw.split(":")[-1].strip()
        m = re.match(r'[-+]?\d+\.?\d*(?:[eE][-+]?\d+)?', last_token)
        if m and not re.search(r'[\d./]', last_token[m.end():]):
            return float(m.group())
        raise ValueError(f"숫자 해석 실패(모호하거나 비숫자): {raw!r}")


@dataclass
class MeasurementParameter:
    """
    Measurement parameter — 단일 query 명령으로 float 반환.

    TSP:  cmd_query = "print(smua.measure.i())"
    SCPI: cmd_query = "MEAS:CURR?"
    Mercury iTC/iPS: 응답이 'PATH:VALUE[UNIT]' 형식이어도 자동 파싱.
    """
    name: str
    cmd_query: str

    def read(self, session: "InstrumentSession", alias: str) -> float:
        if not session.is_open(alias):
            session.open(alias)
        return _parse_float(session.query(alias, self.cmd_query).strip())


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
