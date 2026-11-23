"""AI 자동 측정 — LLM 이 프로그램의 내장 측정 기능을 조작해 측정을 수행한다.

이 패키지는 Qt 위젯을 import 하지 않는다. 창(ui/modules/auto_measure)은 여기의
도구 실행기와 에이전트 루프를 가져다 쓴다.

계층:
    settings    사용자 설정 — 채널별 허용·한계·역할 메모, 승인 방식, 예산
    safety      코드가 강제하는 안전 검사 (LLM 은 이것을 우회할 수 없다)
    lab_context LLM 에게 보여 줄 장비·채널 목록 (하네스의 핵심)
    summarize   측정 결과를 LLM 이 읽을 수 있는 크기로 요약
    tools       도구 정의(JSON schema)
    prompts     시스템 프롬프트
    llm         Anthropic SDK 연결 (선택 의존성, 지역 import)
    agent       도구 호출 루프 — 승인·중지·예산·기록
    executor    도구 실행 — 측정은 메인 창 sweep 경로를 그대로 쓴다
"""
