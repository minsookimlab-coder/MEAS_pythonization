"""
SweepChannel: sweep 루프에서 사용하는 단일 채널.
alias(기기) + SweepParameter(제어 파라미터)의 조합.
"""
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pythonization.instruments.parameter import MeasurementParameter, SweepParameter

if TYPE_CHECKING:
    from pythonization.instruments.session import InstrumentSession


@dataclass
class SweepChannel:
    """
    Sweep 제어 채널 — 기기(alias)와 제어 파라미터(SweepParameter)의 조합.

    사용 예시:
        ch = SweepChannel(
            alias="2636A",
            parameter=SweepParameter(
                name="smua source voltage",
                cmd_set="smua.source.levelv = {v}",
                readback=MeasurementParameter(
                    name="smua source voltage readback",
                    cmd_query="print(smua.source.levelv)",
                ),
            ),
        )
        current = ch.read_value(session)   # readback으로 현재값 확인
        ch.set_value(session, 1.5)         # 목표값 설정
    """

    alias: str
    parameter: SweepParameter

    def read_value(self, session: "InstrumentSession") -> float:
        """현재 설정값을 readback으로 읽어 반환합니다."""
        return self.parameter.read(session, self.alias)

    def set_value(self, session: "InstrumentSession", value: float) -> None:
        """목표값을 기기에 설정합니다."""
        self.parameter.write(session, self.alias, value)

    def describe(self) -> str:
        """UI 표시용 문자열."""
        return self.parameter.cmd_set


class TimeChannel:
    """
    VISA 통신 없이 0부터 시작하는 카운터 채널.
    read_value → 0 반환 (last_write_value=None일 때만 호출됨)
    set_value  → no-op
    """

    def read_value(self, session) -> float:
        return 0.0

    def set_value(self, session, value: float) -> None:
        pass

    def describe(self) -> str:
        return "Time (no VISA)"


TIME_CHANNEL = TimeChannel()


def sweep_channel_from_instantiated(inst: "InstantiatedSweepValue") -> "SweepChannel":
    """
    Main UI Profile의 InstantiatedSweepValue에서 런타임 SweepChannel을 생성합니다.
    cmd_set은 이미 {v}로 정규화되어 있습니다.
    """
    from pythonization.config.models import InstantiatedSweepValue
    return SweepChannel(
        alias=inst.alias,
        parameter=SweepParameter(
            name=inst.description,
            cmd_set=inst.cmd_set,
            readback=MeasurementParameter(
                name=inst.description + " readback",
                cmd_query=inst.paired_read_cmd,
            ),
        ),
    )


def sweep_channel_from_def(alias: str, defn: "SweepValueDef") -> "SweepChannel":
    """
    config-layer SweepValueDef에서 런타임 SweepChannel을 생성합니다.

    SweepParameter.write()는 cmd_set.format(v=value)로 고정되어 있으므로,
    SweepValueDef.cmd_set의 플레이스홀더 이름({i}, {a} 등)을 {v}로 정규화합니다.
    """
    match = re.search(r"\{(\w+)\}", defn.cmd_set)
    if match:
        placeholder = match.group(1)
        cmd_set_v = defn.cmd_set.replace(f"{{{placeholder}}}", "{v}")
    else:
        cmd_set_v = defn.cmd_set

    return SweepChannel(
        alias=alias,
        parameter=SweepParameter(
            name=defn.description,
            cmd_set=cmd_set_v,
            readback=MeasurementParameter(
                name=defn.paired_read.description,
                cmd_query=defn.paired_read.resolved_cmd(),
            ),
        ),
    )
