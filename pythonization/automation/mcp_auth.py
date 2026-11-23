"""Claude Code 의 headersHelper — MCP 서버 인증 헤더를 표준 출력으로 낸다.

저장소의 .mcp.json 이 이 모듈을 부른다:
    "headersHelper": ".venv/Scripts/python.exe -m pythonization.automation.mcp_auth"

토큰은 SETTINGS_DIR/mcp_token.txt 에 있다 (저장소 밖). 없으면 새로 만든다 — 앱도 같은
파일을 읽으므로 둘이 항상 같은 토큰을 쓴다. Claude Code 는 10초 안에 끝나야 기다리므로
Qt 등 무거운 모듈은 import 하지 않는다.
"""
import json
import sys

from pythonization.automation.mcp_server import load_or_create_token


def main() -> int:
    try:
        token = load_or_create_token()
    except Exception as exc:     # noqa: BLE001
        sys.stderr.write(f"pythonization mcp_auth: {exc}\n")
        return 1
    sys.stdout.write(json.dumps({"Authorization": f"Bearer {token}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
