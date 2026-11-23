"""LLM 이 부를 수 있는 도구 목록 (Anthropic tool 정의 형식).

도구는 '프로그램의 내장 기능'에 대응한다. LLM 은 VISA 명령 문자열을 만들 수 없고,
프로파일에 등록된 채널을 인덱스로 고를 뿐이다 (규칙 2 — 명령은 라이브러리에서만).

  get_lab_state       장비·채널 목록, 허용 범위, 예산, 메인 창 상태
  read_values         현재 측정값/채널 값을 한 번 읽기 (장비를 움직이지 않음)
  move_channel        sweep channel 을 목표값까지 천천히 이동 (기록 없음)
  run_sweep           메인 창 sweep 으로 측정·저장 (start 를 주면 먼저 그 값으로 이동)
  run_time_trace      메인 창 Time 채널로 시간에 따른 측정·저장 (안정화 대기 겸용)
  set_second_axis     온도·자기장 같은 second channel 을 목표로 보내고 도달까지 대기
  send_write_command  사용자가 허용한 write 명령 (출력 on/off 등)
  wait                정해진 시간 기다리기
  send_message        사용자에게 메신저(ntfy·텔레그램·이메일)로 직접 쓴 보고 보내기

  recall_knowledge    실험 지식 저장소에서 참고 규칙·과거 관찰 찾기 (가이드일 뿐)
  save_knowledge      현상 → 물리적 의미 → 대응 규칙 저장
  record_observation  이번 측정에서 본 수치·해석·근거를 관찰 기록으로 남기기
  analyze_data_file   Main Folder 안의 .dat 를 다시 요약 (과거 데이터 분석)

ACTUATING_TOOLS 는 장비 상태를 바꾸는 도구다. '실행 전 승인' 모드에서는 이 도구와
save_knowledge(앞으로의 판단에 영향을 주므로)를 부를 때마다 사용자가 승인해야 한다.
"""

ACTUATING_TOOLS = frozenset({
    "move_channel", "run_sweep", "set_second_axis", "send_write_command",
})
APPROVAL_TOOLS = ACTUATING_TOOLS | {"save_knowledge"}

_KINDS = ["interpretation", "safety", "procedure", "note"]
_STR_LIST = {"type": "array", "items": {"type": "string"}}

_INT_LIST = {"type": "array", "items": {"type": "integer"}}

_SAVE_PROPS = {
    "sub_folder": {
        "type": "string",
        "description": (
            "Main Folder 아래 하위 폴더 (상대 경로, '/' 로 최대 4단계). 시료·측정 종류·조건이 "
            "드러나게 짓는다. 예: 'sampleA/gate_sweep/T2K'. 빈 문자열이면 Main Folder 바로 아래."),
    },
    "file_prefix": {
        "type": "string",
        "description": (
            "파일명 앞부분. 뒤에 날짜(설정 시)와 X001 같은 번호가 자동으로 붙는다. "
            "조건이 드러나게 짓는다. 예: 'Vg_m20_p20_2K_'."),
    },
}

TOOL_SPECS = [
    {
        "name": "get_lab_state",
        "description": (
            "장비·채널 목록(인덱스, 장비, 설명, 단위, 사용자 역할 메모, AI 허용 범위), "
            "실험 메모, 예산 사용량, 메인 창 상태를 돌려준다. 계획을 세우기 전과 상황이 "
            "바뀐 뒤에 부른다."),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_values",
        "description": (
            "측정 항목과 채널의 현재값을 한 번 읽는다. 장비를 움직이지 않는다. "
            "인덱스 목록을 비우면 그 종류는 읽지 않는다."),
        "input_schema": {
            "type": "object",
            "properties": {
                "measurements": {**_INT_LIST, "description": "measurement 인덱스 목록"},
                "sweep_channels": {**_INT_LIST,
                                   "description": "현재값(readback)을 읽을 sweep channel 인덱스"},
                "second_channels": {**_INT_LIST,
                                    "description": "현재값을 읽을 second channel 인덱스"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "move_channel",
        "description": (
            "sweep channel 을 현재값에서 target 까지 rate 로 천천히 이동한다. 데이터를 "
            "기록하지 않는다. 측정 시작점으로 가거나 측정 후 0 으로 되돌릴 때 쓴다. "
            "허용 범위·속도 상한을 벗어나면 거부된다."),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "integer", "description": "sweep channel 인덱스"},
                "target": {"type": "number"},
                "rate": {"type": "number", "description": "단위/분"},
                "time_per_point": {"type": "number",
                                   "description": "이동 스텝 간격(초). 기본 1"},
                "purpose": {"type": "string", "description": "사용자에게 보일 한 줄 이유"},
            },
            "required": ["channel", "target", "rate", "purpose"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_sweep",
        "description": (
            "메인 창의 sweep 기능으로 측정한다: 선택한 sweep channel 을 stop 까지 rate 로 "
            "움직이며 매 점마다 고른 measurement 를 읽어 .dat 로 저장한다. start 를 주면 "
            "먼저 기록 없이 start 로 이동한 뒤 기록을 시작한다. 끝나면 저장 경로와 "
            "데이터 요약(통계·극값·가장 가파른 변화 위치·축약 곡선)을 돌려준다. "
            "abort_if_abs_above 로 더 엄격한 즉시 중단 조건을 추가할 수 있다."),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "integer", "description": "sweep channel 인덱스"},
                "start": {"type": ["number", "null"],
                          "description": "기록 시작값. null 이면 현재값에서 바로 시작"},
                "stop": {"type": "number", "description": "기록 끝값"},
                "rate": {"type": "number", "description": "단위/분"},
                "time_per_point": {"type": "number", "description": "점 간격(초)"},
                "approach_rate": {"type": ["number", "null"],
                                  "description": "start 로 이동할 때 속도. null 이면 rate"},
                "measurements": {**_INT_LIST, "description": "기록할 measurement 인덱스"},
                **_SAVE_PROPS,
                "abort_if_abs_above": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"measurement": {"type": "integer"},
                                       "limit": {"type": "number"}},
                        "required": ["measurement", "limit"],
                        "additionalProperties": False,
                    },
                    "description": "|값| 이 limit 을 넘으면 즉시 중단할 measurement",
                },
                "purpose": {"type": "string"},
            },
            "required": ["channel", "stop", "rate", "time_per_point", "measurements",
                         "sub_folder", "file_prefix", "purpose"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_time_trace",
        "description": (
            "메인 창의 Time 채널로 duration_s 동안 time_per_point 간격으로 measurement 를 "
            "기록한다. 장비를 움직이지 않는다. stop_when_stable 을 주면 그 측정값의 최근 "
            "window 점 표준편차가 max_std 아래로 내려가는 즉시 끝낸다 (온도 안정화 대기)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "duration_s": {"type": "number"},
                "time_per_point": {"type": "number"},
                "measurements": _INT_LIST,
                **_SAVE_PROPS,
                "stop_when_stable": {
                    "type": ["object", "null"],
                    "properties": {
                        "measurement": {"type": "integer"},
                        "window": {"type": "integer", "description": "최근 점 수 (3 이상)"},
                        "max_std": {"type": "number", "description": "측정 단위의 표준편차 상한"},
                    },
                    "required": ["measurement", "window", "max_std"],
                    "additionalProperties": False,
                },
                "purpose": {"type": "string"},
            },
            "required": ["duration_s", "time_per_point", "measurements",
                         "sub_folder", "file_prefix", "purpose"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_second_axis",
        "description": (
            "second channel(온도·자기장 등)을 value 로 설정하고, 프로파일에 정해진 advance "
            "방식(feedback/threshold/wait/sweep)대로 도달·안정화까지 기다린다. 오래 걸릴 수 "
            "있다 (장비 워치독: 미도달 시 1회 재전송 후 실패)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "integer", "description": "second channel 인덱스"},
                "value": {"type": "number"},
                "purpose": {"type": "string"},
            },
            "required": ["channel", "value", "purpose"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_write_command",
        "description": (
            "사용자가 허용한 write command 를 보낸다 (출력 on/off, 레인지 설정 등). "
            "takes_value 인 명령만 value 를 받는다."),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "integer", "description": "write command 인덱스"},
                "value": {"type": ["number", "null"]},
                "purpose": {"type": "string"},
            },
            "required": ["command", "purpose"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait",
        "description": "seconds 초 동안 기다린다 (장비를 건드리지 않음).",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["seconds", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_message",
        "description": (
            "사용자에게 메신저(ntfy 휴대폰 알림·텔레그램·이메일)로 직접 쓴 보고를 보낸다. 측정 시작·중요한 "
            "단계 완료·이상 징후·최종 결과를 알릴 때 쓴다. 사실과 숫자 위주로 짧게. "
            "urgent=true 면 알람 소리도 낸다 (이상 상황에만)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "urgent": {"type": "boolean"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "recall_knowledge",
        "description": (
            "실험 지식 저장소에서 참고 규칙(현상→의미→대응)과 과거 관찰을 키워드로 찾는다. "
            "가이드일 뿐이다: 먼저 데이터의 특징을 스스로 기술한 뒤에 부르고, 결과는 현재 "
            "데이터와 비교해 맞는지·다른 설명이 있는지 판단한다. 과거 관찰의 수치 추이를 "
            "분석할 때도 쓴다."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "현상·채널·시료 등 키워드"},
                "kinds": {"type": "array", "items": {"type": "string", "enum": _KINDS}},
                "tags": _STR_LIST,
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "save_knowledge",
        "description": (
            "규칙을 저장한다: condition(관찰되는 현상, 어떤 채널에서 어떤 값·모양) → "
            "meaning(물리적 의미) → action(대응). 사용자가 현상의 의미를 설명하거나 기억하라고 "
            "할 때 쓴다. 네가 스스로 세운 해석은 여러 관찰로 뒷받침될 때만 저장한다 "
            "(사용자 확인 전까지 '가설'로 표시된다). 기존 규칙을 고치려면 replaces_id 를 준다."),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": _KINDS},
                "title": {"type": "string"},
                "condition": {"type": "string"},
                "meaning": {"type": "string"},
                "action": {"type": "string"},
                "tags": _STR_LIST,
                "sample": {"type": "string", "description": "특정 시료에만 해당하면 이름"},
                "replaces_id": {"type": ["string", "null"]},
                "user_requested": {"type": "boolean",
                                   "description": "사용자가 직접 기억하라고 했으면 true"},
            },
            "required": ["kind", "title", "condition", "meaning", "action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_observation",
        "description": (
            "이번 측정에서 실제로 본 것을 기록한다: 핵심 수치(단위 포함), 데이터 파일, 해석과 "
            "확신 정도, 그리고 참고한 규칙을 뒷받침(supports)했는지 반박(contradicts)했는지. "
            "의미 있는 특징을 찾았거나 이상을 봤을 때마다 남긴다."),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "quantities": {"type": "object",
                               "description": "예: {\"Dirac point Vg (V)\": 0.8}",
                               "additionalProperties": {"type": ["number", "string"]}},
                "interpretation": {"type": "string"},
                "files": _STR_LIST,
                "tags": _STR_LIST,
                "sample": {"type": "string"},
                "supports": {**_STR_LIST, "description": "뒷받침한 규칙 id"},
                "contradicts": {**_STR_LIST, "description": "어긋난 규칙 id"},
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "analyze_data_file",
        "description": (
            "Main Folder 안에 저장된 .dat 파일을 다시 읽어 요약한다 (통계·극값·가장 가파른 "
            "변화·축약 곡선). 과거 측정을 분석하거나 비교할 때 쓴다. 장비를 건드리지 않는다."),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "전체 경로 또는 Main Folder 기준 상대 경로"},
                "max_curve_points": {"type": "integer"},
            },
            "required": ["file"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = frozenset(spec["name"] for spec in TOOL_SPECS)
