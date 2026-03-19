"""
콘솔 명령어 파싱 및 실행 모듈.

지원 포맷:
  alias:<alias> VISA:<cmd> type:write
  alias:<alias> VISA:<cmd> type:query
  alias:<alias> VISA:      type:read
"""
import re
from dataclasses import dataclass
from typing import Literal, Optional

from core.instrument_session import InstrumentSession


@dataclass
class ConsoleCommand:
    """
    콘솔 입력 한 줄을 파싱한 결과를 담는 불변 데이터 객체.

    alias   : 장비 alias (instruments.yaml에 등록된 이름)
    visa_cmd: VISA에 전송할 명령어 문자열 (read 타입은 빈 문자열)
    cmd_type: "write" | "read" | "query"
    """
    alias: str
    visa_cmd: str
    cmd_type: Literal["write", "read", "query"]

    # alias:<value> VISA:<value> type:<write|read|query>
    # VISA 값은 공백 포함 가능, type: 직전까지 캡처
    _PATTERN = re.compile(
        r'alias:(\S+)\s+VISA:(.*?)\s*type:(write|read|query)\s*$',
        re.IGNORECASE
    )

    @classmethod
    def parse(cls, text: str) -> Optional["ConsoleCommand"]:
        """
        입력 문자열을 파싱하여 ConsoleCommand를 반환합니다.
        형식이 맞지 않으면 None을 반환합니다.
        """
        m = cls._PATTERN.match(text.strip())
        if not m:
            return None
        return cls(
            alias=m.group(1),
            visa_cmd=m.group(2).strip(),
            cmd_type=m.group(3).lower(),
        )


class ConsoleCommandHandler:
    """
    ConsoleCommand를 받아 InstrumentSession을 통해 실행합니다.
    연결은 열린 채로 유지되어 연속 명령에서 재사용됩니다.
    """

    def __init__(self, session: InstrumentSession):
        self._session = session

    def execute(self, cmd: ConsoleCommand) -> Optional[str]:
        """
        명령을 실행합니다.
        - write : None 반환
        - read  : 응답 문자열 반환
        - query : 응답 문자열 반환
        장비가 아직 연결되지 않았다면 자동으로 open합니다.
        """
        if not self._session.is_open(cmd.alias):
            self._session.open(cmd.alias)

        if cmd.cmd_type == "write":
            self._session.write(cmd.alias, cmd.visa_cmd)
            return None

        if cmd.cmd_type == "read":
            return self._session.read(cmd.alias)

        if cmd.cmd_type == "query":
            return self._session.query(cmd.alias, cmd.visa_cmd)
