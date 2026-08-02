import re
from enum import Enum
from typing import Dict, Any, Literal, Optional, List, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def cast_extra_params(params: dict) -> dict:
    """
    extra_params의 문자열 값을 적절한 숫자 타입으로 자동 변환합니다.
    int로 변환 가능하면 int, float으로 변환 가능하면 float, 아니면 str 유지.
    """
    result = {}
    for k, v in params.items():
        if isinstance(v, str):
            try:
                if v.isdigit() or (v.startswith('-') and v[1:].isdigit()):
                    result[k] = int(v)
                else:
                    result[k] = float(v)
            except ValueError:
                result[k] = v
        else:
            result[k] = v
    return result


class CommandConfig(BaseModel):
    """visa_libraries.yaml의 커맨드 항목 하나를 나타내는 모델."""
    desc: str = ""
    long_name: str = ""
    type: Literal["Write", "Query (Read)"] = "Write"
    unit: str = ""
    proto: str = ""
    has_paired_read: bool = False
    paired_read_proto: str = ""
    params: List[str] = Field(default_factory=lambda: [""] * 5)
    comments: List[str] = Field(default_factory=lambda: [""] * 5)


class ActiveMeasConfig(BaseModel):
    """활성화된 측정(Measurement) 항목 모델."""
    long_name: str = ""
    original: str = ""
    proto: str = ""
    text: str = ""
    unit: str = ""


class ActiveSweepConfig(BaseModel):
    """활성화된 스윕(Sweep) 항목 모델."""
    long_name: str = ""
    original: str = ""
    sweep_from_cmd: str = ""
    text: str = ""
    unit: str = ""


class InstrumentCommandProfile(BaseModel):
    """visa_libraries.yaml의 장비 프로파일 하나 전체 모델."""
    commands: List[CommandConfig] = Field(default_factory=list)
    active_sweeps: List[ActiveSweepConfig] = Field(default_factory=list)
    active_meas: List[ActiveMeasConfig] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# VISA Library models
# ---------------------------------------------------------------------------

class MeasurementParamDef(BaseModel):
    """Measurement parameter 정의 (읽기 전용) — VISA 명령어 템플릿 카탈로그."""
    model_config = ConfigDict(extra='ignore')

    description: str = ""
    cmd_query: str                          # 템플릿 (예: "FETCh:SENSe{m}:LIA:X?")
    figure_axis: str = ""
    unit: str = ""


class SweepValueDef(BaseModel):
    """Sweep value 정의 — cmd_set과 paired_read_cmd를 plain 문자열로 저장."""
    description: str = ""
    cmd_set: str            # "smua.source.levelv = {v}"
    paired_read_cmd: str = ""   # paired read query template, e.g. "print(smua.measure.i())"
    figure_axis: str = ""
    unit: str = ""

    @model_validator(mode='before')
    @classmethod
    def _migrate_paired_read(cls, v):
        if isinstance(v, dict) and 'paired_read' in v and isinstance(v['paired_read'], dict):
            old = v['paired_read']
            cmd = old.get('cmd_query', '')
            params = old.get('params', {})
            if params:
                try:
                    v['paired_read_cmd'] = cmd.format(**params)
                except Exception:
                    v['paired_read_cmd'] = cmd
            else:
                v['paired_read_cmd'] = cmd
            v.pop('paired_read', None)
        return v


class WriteCmdDef(BaseModel):
    """Write-only 명령어 정의 — 기기에 값을 설정하거나 상태를 변경하는 단순 쓰기 명령."""
    description: str = ""
    cmd_set: str            # "smua.source.output = smua.OUTPUT_ON"
    figure_axis: str = ""
    unit: str = ""


class InstrumentCmdLibrary(BaseModel):
    """alias당 하나의 VISA 명령어 라이브러리."""
    measurements: List[MeasurementParamDef] = Field(default_factory=list)
    sweep_values: List[SweepValueDef] = Field(default_factory=list)
    write_cmds: List[WriteCmdDef] = Field(default_factory=list)


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Parameter Manager models
# ---------------------------------------------------------------------------

class SelectedEntry(BaseModel):
    """Parameter Manager에서 선택된 항목 — alias + description으로 라이브러리 항목을 식별."""
    alias: str
    description: str


class ParameterManagerProfile(BaseModel):
    """Parameter Manager의 현재 선택 상태 (라이브러리 포인터만 저장)."""
    sweep_values: List[SelectedEntry] = Field(default_factory=list)
    measurements: List[SelectedEntry] = Field(default_factory=list)
    write_cmds: List[SelectedEntry] = Field(default_factory=list)
    second_sweep_channels: List[SelectedEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Main UI Profile — 파라미터가 채워진 인스턴스화된 항목
# ---------------------------------------------------------------------------

class MeasType(str, Enum):
    """측정 채널 종류 — 데이터 저장 컬럼 이름 규칙에 영향."""
    NONE        = "none"
    CONTACT     = "contact"
    TEMPERATURE = "temperature"
    BFIELD      = "bfield"


class InstantiatedMeasurement(BaseModel):
    """파라미터가 모두 채워진 측정 항목."""
    alias: str
    description: str
    resolved_cmd: str       # 모든 파라미터 적용 완료된 query 명령
    figure_axis: str = ""
    unit: str = ""
    fill_params: Dict[str, str] = Field(default_factory=dict)  # 원본 입력값 (재편집용)
    axis_suffix: str = ""   # user-defined suffix appended to figure_axis in data files
    meas_type: MeasType = MeasType.NONE  # contact → column = "contact_{suffix}"
    checked: bool = True    # main UI 체크박스 상태 — 프로파일에 저장됨


class InstantiatedSweepValue(BaseModel):
    """파라미터가 채워진 sweep value 항목 — cmd_set에 {v} 하나만 남아있음."""
    alias: str
    description: str
    cmd_set: str            # 비-sweep 파라미터 채워짐, sweep 파라미터 → {v}
    paired_read_cmd: str    # 완전히 resolved된 paired measurement 명령
    figure_axis: str = ""
    unit: str = ""
    safety_steps: int = 0           # 0 = safety 없음
    safety_interval_ms: float = 0.0 # sub-step 사이 대기 시간 (ms)
    fill_params: Dict[str, str] = Field(default_factory=dict)  # 원본 입력값 (재편집용)


class InstantiatedWriteCmd(BaseModel):
    """파라미터가 채워진 write 항목 — sweep 파라미터가 있으면 {v}, 없으면 고정 명령."""
    alias: str
    description: str
    cmd_set: str            # {v} 있을 수도 없을 수도 있음
    figure_axis: str = ""
    unit: str = ""
    fill_params: Dict[str, str] = Field(default_factory=dict)  # 원본 입력값 (재편집용)


class SecondSweepAdvanceType(str, Enum):
    SIMPLE_HOP     = "simple_hop"
    SWEEP          = "sweep"
    FEEDBACK       = "feedback"
    WAIT_FOR_TIME  = "wait_for_time"
    THRESHOLD_TIME = "threshold_time"   # 목표 band 도달 후 고정 시간 대기 (std 안정화 대신)


class InstantiatedSecondSweepChannel(BaseModel):
    """Double sweep의 second sweep channel — advance type에 따른 파라미터 포함."""
    alias: str
    description: str
    source_type: Literal["sweep_value", "write_cmd"]
    advance_type: SecondSweepAdvanceType = SecondSweepAdvanceType.SIMPLE_HOP
    cmd_set: str                        # {v} 포함
    # SWEEP type
    paired_read_cmd: str = ""
    sweep_rate: float = 1.0
    safety_steps: int = 0
    safety_interval_ms: float = 0.0
    # FEEDBACK type
    feedback_read_cmd: str = ""
    feedback_poll_interval: float = 1.0   # seconds
    feedback_tolerance_pct: float = 95.0  # %: |V_read-V_prev|/|V_next-V_prev| >= pct/100
    # FEEDBACK stability check: after reaching tolerance, keep polling and require
    # stability metric = SD / (max(|mean|, |next_v|) + noisefloor) < std_threshold
    feedback_std_window: int = 0          # 0 = disabled; N > 0 activates stability check
    feedback_noisefloor: float = 0.0      # same units as measurement; prevents /0 near zero
    feedback_std_threshold: float = 0.01  # dimensionless; e.g. 0.01 = 1 %
    # WAIT_FOR_TIME type
    wait_time: float = 1.0
    figure_axis: str = ""
    unit: str = ""


class AlarmOperator(str, Enum):
    """측정값 비교 연산자."""
    GT  = ">"
    LT  = "<"
    GTE = ">="
    LTE = "<="
    EQ  = "=="
    NEQ = "!="


class AlarmTrigger(BaseModel):
    """알람 트리거 조건 하나."""
    enabled: bool = True
    kind: Literal["measurement", "comm_error", "meas_error"] = "comm_error"
    # kind == "measurement" 일 때만 사용
    meas_description: str = ""
    operator: AlarmOperator = AlarmOperator.GT
    threshold: float = 0.0


class TelegramContact(BaseModel):
    """Telegram 알람 수신자 — 이름과 Chat ID."""
    name: str = ""
    chat_id: str = ""


class AlarmDeliveryConfig(BaseModel):
    """알람 '전송 수단' 설정 — VNA·Double Sweep가 공유하는 전역 값.

    소리/이메일/SMTP/텔레그램 토큰·연락처처럼 어디서 알람을 켜든 동일해야 하는
    항목만 담는다. on/off(enabled)와 트리거는 각 컨텍스트의 AlarmConfig에 따로 보관.
    """
    use_sound: bool = True
    use_email: bool = False
    email_to: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    use_telegram: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_contacts: List[TelegramContact] = Field(default_factory=list)

    def is_configured(self) -> bool:
        """전역 전송설정이 실제로 채워져 있는지(최초 시드 판단용)."""
        return bool(self.telegram_bot_token.strip() or self.email_to.strip()
                    or self.smtp_user.strip() or self.telegram_contacts)


class AlarmConfig(BaseModel):
    """알람 전체 설정 (Double Sweep 전용)."""
    enabled: bool = False
    use_sound: bool = True
    use_email: bool = False
    email_to: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    use_telegram: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_contacts: List[TelegramContact] = Field(default_factory=list)
    fire_on_complete: bool = False   # 모든 array 완료 시 알람
    triggers: List[AlarmTrigger] = Field(default_factory=list)


class MetaDataEntry(BaseModel):
    """메타 데이터 기록용 VISA 쿼리 항목 — 스윕 종료 시 한 번 쿼리."""
    alias: str
    description: str
    resolved_cmd: str
    figure_axis: str = ""
    unit: str = ""
    enabled: bool = True
    meas_type: MeasType = MeasType.NONE


class MetaDataConfig(BaseModel):
    """메타 데이터 전체 설정 (Single/Double Sweep 공통)."""
    enabled: bool = False
    entries: List[MetaDataEntry] = Field(default_factory=list)


class DoubleSweepConfig(BaseModel):
    """Double Sweep 창의 파라미터 설정 (double_sweep.yaml에 저장)."""
    start_point: float = 0.0
    stop_point: float = 1.0
    rate_trace: float = 1.0
    rate_retrace: float = 1.0
    rate_dummy: float = 1.0
    time_per_point: float = 1.0
    array_from: float = 0.0
    array_to: float = 1.0
    array_step: float = 0.1
    selected_channel_idx: int = 0
    retrace_to_zero: bool = False  # if True, RETRACE sweeps to 0 instead of start_point
    to_zero_at_last: bool = False  # if True, send second channel to 0 after all array steps
    # SWEEP-type second channel params (adjustable in DoubleSweepWindow)
    second_sweep_rate: float = 1.0
    second_use_safety: bool = False
    second_safety_steps: int = 0
    second_safety_interval_ms: float = 0.0
    # FEEDBACK-type second channel params (adjustable in DoubleSweepWindow)
    second_feedback_read_cmd: str = ""
    second_feedback_poll_interval: float = 1.0
    second_feedback_tolerance_pct: float = 95.0
    second_feedback_std_window: int = 0
    second_feedback_noisefloor: float = 0.0
    second_feedback_std_threshold: float = 0.01
    # WAIT_FOR_TIME-type second channel params (adjustable in DoubleSweepWindow)
    second_wait_time: float = 1.0
    # Alarm
    alarm: AlarmConfig = Field(default_factory=AlarmConfig)


class MainUIProfile(BaseModel):
    """Main UI에 등록된 인스턴스화된 항목 전체."""
    sweep_values: List[InstantiatedSweepValue] = Field(default_factory=list)
    measurements: List[InstantiatedMeasurement] = Field(default_factory=list)
    write_cmds: List[InstantiatedWriteCmd] = Field(default_factory=list)
    second_sweep_channels: List[InstantiatedSecondSweepChannel] = Field(default_factory=list)
    # 알람 계층 — Parameter Manager에서 지정, Double Sweep AlarmPanel combo 소스
    alarm_measurements: List[InstantiatedMeasurement] = Field(default_factory=list)
    # 메타 데이터 계층 — Parameter Manager에서 지정, MetaDataConfig.entries로 변환됨
    meta_data_measurements: List[InstantiatedMeasurement] = Field(default_factory=list)


class DerivConfigData(BaseModel):
    """Derivative channel 설정 — FullProfile 저장용."""
    enabled: bool = False
    numerator_key: str = ""
    denominator_key: str = ""
    output_label: str = ""
    output_unit: str = ""
    window_size: int = 10
    method: str = "linear"        # "linear" | "savgol"
    min_delta: float = 1e-10


class FullProfile(BaseModel):
    """사용자별 프로파일 — 실험 간에 달라지는 모든 설정."""
    parameter_manager: ParameterManagerProfile = Field(default_factory=ParameterManagerProfile)
    main_ui: MainUIProfile = Field(default_factory=MainUIProfile)
    # Sweep parameters
    sweep_to: float = 0.0
    sweep_rate: float = 1.0
    time_per_point: float = 1.0
    active_sweep_channel_idx: int = -2   # -2 = Time channel
    # Save settings
    main_folder: str = ""
    custom_folder: str = ""
    custom_word: str = ""
    include_date: bool = True
    save_enabled: bool = False
    # Double sweep
    double_sweep: DoubleSweepConfig = Field(default_factory=DoubleSweepConfig)
    # Meta data
    meta_data: MetaDataConfig = Field(default_factory=MetaDataConfig)
    # Derivative channels
    deriv1: DerivConfigData = Field(default_factory=DerivConfigData)
    deriv2: DerivConfigData = Field(default_factory=DerivConfigData)
    deriv3: DerivConfigData = Field(default_factory=DerivConfigData)


# ---------------------------------------------------------------------------

class InstrumentConfig(BaseModel):
    """
    Pydantic model for instrument configuration validation.
    """
    alias: str = Field(..., description="A unique readable name/alias for the instrument.")
    class_name: str = Field(
        ...,
        description=(
            "동적 로딩용 전체 클래스 경로 "
            "(예: pythonization.instruments.drivers.dummy.DummyInstrument). "
            "구 레이아웃 경로는 InstrumentFactory.resolve_class_path 가 옮겨준다."
        ),
    )
    interface_type: Literal["LAN", "GPIB", "RS232", "USB"] = Field(..., description="Hardware communication interface type.")
    address: str = Field(..., description="Hardware address or VISA resource string.")
    mac_address: Optional[str] = Field(default="", description="MAC address for auto-tracking dynamic IP environments.")
    port: Optional[int] = Field(default=None, description="Communication port, if applicable.")
    extra_params: Dict[str, Any] = Field(default_factory=dict, description="Dictionary to store arbitrary dynamic variables.")
