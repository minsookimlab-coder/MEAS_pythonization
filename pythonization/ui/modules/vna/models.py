"""
VNA 공유 데이터 모델 + 설정 IO.
"""
import re
import yaml
import numpy as np
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

from pythonization.config.models import AlarmConfig, SecondSweepAdvanceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_params(template: str) -> List[str]:
    """Format string에서 고유 placeholder 이름 추출 (순서 유지)."""
    return list(dict.fromkeys(re.findall(r'\{(\w+)\}', template)))


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------

class VnaParamSpec(BaseModel):
    name: str
    value: str = ""
    is_user_input: bool = False


class VnaPreAdvanceCmd(BaseModel):
    """Controlled Sweep의 advance 직전에 실행할 명령 (라이브러리 write 명령 기반).

    예: 기기 내부 ramp 속도 조절. params 중 is_user_input=True 인 1개가
    'advance 전 조절 파라미터'이며, VNA Control에서 sweep/dummy 단계별 값으로 채운다.
    나머지 params 는 Config에서 고정값으로 채워 둔다. 실제 적용 여부·값은 Control에서 정함.
    """
    alias:       str = ""
    description: str = ""        # 라이브러리 write 명령 description (template 조회용)
    params:      List[VnaParamSpec] = Field(default_factory=list)
    label:       str = ""        # 표시용 이름 (비면 description 사용)


class VnaAdvanceConfig(BaseModel):
    """Controlled Sweep 한 축의 '안정화(advance)' 설정.

    Parameter Manager의 second sweep channel과 같은 방식(SIMPLE_HOP/SWEEP/FEEDBACK/
    WAIT_FOR_TIME)을 재사용하되, safety ramp 관련 설정은 VNA에서는 사용하지 않는다.
    """
    advance_type: SecondSweepAdvanceType = SecondSweepAdvanceType.SIMPLE_HOP
    # SWEEP (점진 이동, safety ramp 없음)
    paired_read_cmd: str = ""
    sweep_rate: float = 1.0
    # FEEDBACK (목표 도달 + 안정화 폴링)
    feedback_read_cmd: str = ""
    feedback_poll_interval: float = 1.0
    feedback_tolerance_pct: float = 95.0
    feedback_std_window: int = 0
    feedback_noisefloor: float = 0.0
    feedback_std_threshold: float = 0.01
    # WAIT_FOR_TIME
    wait_time: float = 1.0
    # advance 직전 실행할 명령 템플릿(여러 개 가능) — 단방향 sweep의 dummy 복귀 등에서 사용
    pre_cmds: List[VnaPreAdvanceCmd] = Field(default_factory=list)


class VnaCommandEntry(BaseModel):
    alias: str = ""
    description: str = ""
    cmd_type: str = "write"         # "write" | "query"
    params: List[VnaParamSpec] = Field(default_factory=list)
    enabled: bool = True
    figure_axis: str = ""           # query 전용: 플롯 x/y 선택 레이블
    read_stride: int = 1            # query 전용: 데이터 추출 stride (1=전체, 2=짝수 인덱스만 …)
    units:       str = ""           # query 전용: 저장 파일 2행 units
    bind_id:     int = 0            # 묶기 그룹 ID (0=없음, 같은 값끼리 묶임)
    unit_type:   str = ""           # user_input 단위 타입: "Hz" | "sec" | ""
    unit_label:  str = ""           # 선택된 단위 레이블 저장 (예: "MHz", "ms")
    sweep_role:  str = ""           # "" | "start" | "stop" | "n_points" (acquire array 생성용)
    # ── VNA double sweep: sweep 축 종류 ────────────────────────────────────
    sweep_kind:  str = "general"    # "general" | "controlled" (Controlled Sweep)
    advance:     Optional[VnaAdvanceConfig] = None   # controlled일 때만 사용


class VnaSectionConfig(BaseModel):
    name: str = ""
    commands: List[VnaCommandEntry] = Field(default_factory=list)


class VnaAcquireConfig(BaseModel):
    sweep_cmds: List[VnaCommandEntry] = Field(default_factory=list)  # 매 스텝 앞에 실행, [SWEEP] param → 스텝 값
    start_cmds: List[VnaCommandEntry] = Field(default_factory=list)
    wait_cmds:  List[VnaCommandEntry] = Field(default_factory=list)
    read_cmds:  List[VnaCommandEntry] = Field(default_factory=list)
    filename:   str = "vna_data"
    sweep_start: float = 0.0
    sweep_stop:  float = 1.0
    sweep_n:     int   = 10
    # 시간 기반 sweep: 일정 간격마다 acquire 1회씩 N번 반복
    time_mode:     bool  = False # True면 Sweep 버튼이 시간 기반으로 동작
    time_interval: float = 1.0   # 스텝 간격 (초)
    time_count:    int   = 10    # 반복 횟수


class VnaPreCmdValue(BaseModel):
    """Control 화면에서 정하는 pre-advance 명령의 적용 여부·단계별 값.
    first channel의 advance.pre_cmds 순서에 1:1 대응한다."""
    enabled: bool = False
    sweep_value: str = ""   # sweep 단계에서 보낼 [parameter] 값
    dummy_value: str = ""   # dummy(복귀) 단계에서 보낼 [parameter] 값


class VnaFieldTimeConfig(BaseModel):
    """Double Sweep with Time — first 채널(자기장)을 '시간 기반'으로 측정하는 설정.

    first 채널 값을 N단계로 나누지 않고, 목표로 ramp를 시작한 뒤 interval마다
    acquire를 반복한다. 중간에 status_cmd로 장비 상태를 읽어 hold_token(예: HOLD)이
    응답에 나타나면 sweep 완료로 판단한다. 단방향이면 완료 후 first 채널의
    controlled advance(feedback)로 시작점에 복귀·안정화한다.
    """
    enabled:      bool = False
    interval:     float = 5.0     # acquire 간격(초)
    status_interval: float = 2.0  # 상태(HOLD/to-set) 폴링 간격(초) — acquire와 별개
    status_alias: str = ""        # 상태 폴링 장비 (비면 first 채널 alias 사용)
    status_cmd:   str = "READ:DEV:GRPZ:PSU:ACTN"   # 상태 읽기 명령
    hold_token:   str = "HOLD"    # 응답에 이 문자열이 있으면 sweep 완료(정지)
    # ramp 시작 직후 실행할 추가 명령(예: RTOS 트리거) — user_input엔 목표값이 채워짐
    forward_cmds: List[VnaPreAdvanceCmd] = Field(default_factory=list)


class VnaDoubleSweepControl(BaseModel):
    """VNA Control 화면의 double-sweep 설정 (first/second 채널·방향·pre-advance 값)."""
    first_idx:   int = 0          # sweep_cmds 내 first channel 인덱스
    first_start: float = 0.0
    first_stop:  float = 1.0
    first_n:     int   = 10
    direction:   str = "uni"      # "uni"(단방향) | "multi"(다중방향)
    second_enabled: bool = False
    second_idx:  int = 0
    second_start: float = 0.0
    second_stop:  float = 1.0
    second_n:    int   = 10
    sec_per_step: float = 1.0     # 시간 예상용: 1 acquire당 예상 소요(초)
    pre_values:  List[VnaPreCmdValue] = Field(default_factory=list)
    # Double Sweep with Time (first 채널 시간 기반 측정)
    field_time:  VnaFieldTimeConfig = Field(default_factory=VnaFieldTimeConfig)
    # 측정 중 Stop 시 실행할 명령(예: 자기장 HOLD로 ramp 정지) — 비우면 아무 것도 안 함
    stop_cmds:   List[VnaPreAdvanceCmd] = Field(default_factory=list)
    # Feature 1: 시작 시 second 테이블 초기화하지 않고 사용자 테이블 그대로 사용
    second_use_custom_table: bool = False
    # Feature 2: field-time에서 첫 target 대기 없이 현재 위치에서 field sweep 1회 선행
    field_initial_sweep:     bool = False
    # Feature 3: dummy(복귀) sweep도 측정할지 여부 (기본: 측정 안 함)
    dummy_measure:           bool = False


class VnaPlotCurveConfig(BaseModel):
    x_source:  str = "index"              # "index" | arr label
    y_source:  str = "arr_0"             # 하위호환 단일 y
    y_sources: List[str] = Field(default_factory=list)  # multi-y (우선)
    label:     str = ""


class VnaResumeState(BaseModel):
    """VNA double sweep 재개 상태 — 중단(사용자/오류)된 측정을 second 채널 기준으로 이어감.

    매 second 값 완료 시 갱신·저장되어, 크래시로 프로그램이 죽어도 파일에 남는다.
    재개 시 completed_second_idx+1번째 second 값부터(=중단된 것 포함) 다시 시작한다.
    """
    active:        bool = False
    sweep_folder:  str = ""        # 이어서 저장할 폴더 (filename_xxx)
    second_values: List[float] = Field(default_factory=list)  # 전체 second 시퀀스
    completed_second_idx: int = -1  # 마지막으로 완료된 second 인덱스 (-1=없음)
    field_time:    bool = False
    filename:      str = ""
    figure_axis:   str = ""
    timestamp:     str = ""
    control:       "VnaDoubleSweepControl" = Field(default_factory=lambda: VnaDoubleSweepControl())


def resume_path_for(config_path: Path) -> Path:
    """vna 설정 경로 → 같은 위치의 resume 파일 경로 ({name}.resume.yaml)."""
    return config_path.parent / (config_path.stem + ".resume.yaml")


def load_resume_state(path: Path) -> VnaResumeState:
    if not path.exists():
        return VnaResumeState()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return VnaResumeState(**data)
    except Exception as e:
        print(f"[VNA] resume load failed ({path.name}): {e}")
        return VnaResumeState()


def save_resume_state(state: VnaResumeState, path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(state.model_dump(mode="json"), f, allow_unicode=True)
        import os
        os.replace(tmp, path)
    except Exception as e:
        print(f"[VNA] resume save failed: {e}")


def clear_resume_state(path: Path):
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


class VnaConfigData(BaseModel):
    sections:    List[VnaSectionConfig]    = Field(default_factory=list)
    acquire:     VnaAcquireConfig          = Field(default_factory=VnaAcquireConfig)
    plot_curves: List[VnaPlotCurveConfig]  = Field(default_factory=list)
    main_folder: str = ""
    sub_folder:  str = ""
    save_enabled: bool = True
    alarm:       AlarmConfig = Field(default_factory=AlarmConfig)  # VNA sweep 알람 (텔레그램)
    ds_control:  VnaDoubleSweepControl = Field(default_factory=VnaDoubleSweepControl)  # double sweep control 상태


# ---------------------------------------------------------------------------
# Config IO
# ---------------------------------------------------------------------------

def load_vna_config(path: Path) -> VnaConfigData:
    if not path.exists():
        return VnaConfigData()
    text = None
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        return VnaConfigData(**data)
    except Exception as e_safe:
        # 폴백: 구버전이 enum을 python/object 태그로 저장한 파일(로컬 신뢰 파일) 복구.
        # safe_load는 이 태그를 못 읽어 실패하므로 UnsafeLoader로 한 번 더 시도한다.
        # 성공하면 다음 저장 시 정상(JSON) 형식으로 다시 기록된다.
        try:
            if text is not None:
                data = yaml.load(text, Loader=yaml.UnsafeLoader) or {}
                cfg = VnaConfigData(**data)
                print(f"[VNA] {path.name}: 구형식(enum 태그) 로드 — 다음 저장 시 정상 형식으로 변환됨")
                return cfg
        except Exception:
            pass
        # 진짜 파싱 불가 → 손상 추적용 백업 후 빈 기본값. (빈 값으로 덮어쓰는 것은 save가 막음)
        print(f"[VNA] Config load failed ({path.name}): {type(e_safe).__name__}: {e_safe}")
        try:
            import shutil
            shutil.copy2(path, path.with_suffix(path.suffix + ".loadfail-bak"))
        except Exception:
            pass
        return VnaConfigData()


def _vna_cfg_is_empty(cfg: VnaConfigData) -> bool:
    """섹션·acquire 명령이 모두 비어 있으면 '내용 없음'으로 판단."""
    a = cfg.acquire
    return (not cfg.sections and not a.sweep_cmds and not a.read_cmds
            and not a.start_cmds and not a.wait_cmds)


def save_vna_config(cfg: VnaConfigData, path: Path):
    import os
    import shutil
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # ── 데이터 유실 방지 안전장치 ──────────────────────────────────────
        # '명령이 전부 빈' 설정으로 '내용이 있는' 기존 파일을 덮어쓰지 않는다.
        # (load 실패 → 빈 기본값 → 그대로 save 되는 사고로 설정이 통째로
        #  지워지던 문제를 차단.)
        if path.exists() and _vna_cfg_is_empty(cfg):
            try:
                existing = load_vna_config(path)
            except Exception:
                existing = None
            if existing is not None and not _vna_cfg_is_empty(existing):
                try:
                    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
                except Exception:
                    pass
                print(f"[VNA] 빈 설정으로 덮어쓰기 거부 — 기존 내용 보존: {path.name}")
                return
        # 직전 버전 백업 + 원자적 저장(임시파일 → 교체)으로 부분쓰기 손상 방지
        if path.exists():
            try:
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            except Exception:
                pass
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            # mode='json': Enum(advance_type 등)을 문자열로 직렬화 → yaml.safe_load 가능.
            # (mode 미지정 시 enum이 !!python/object 태그로 저장돼 다시 못 읽는 문제 방지)
            yaml.dump(cfg.model_dump(mode="json"), f, allow_unicode=True)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[VNA] Config save failed: {e}")


# ---------------------------------------------------------------------------
# Library lookup helpers
# ---------------------------------------------------------------------------

def _all_lib_entries(lib):
    """write_cmds + sweep_values + measurements 합쳐서 반환."""
    return list(lib.write_cmds) + list(lib.sweep_values) + list(lib.measurements)


def get_template(lib, entry: VnaCommandEntry) -> Optional[str]:
    """Visa library에서 명령어 템플릿 반환."""
    if entry.cmd_type == "write":
        for e in list(lib.write_cmds) + list(lib.sweep_values):
            if e.description == entry.description:
                return e.cmd_set
    else:
        for e in lib.measurements:
            if e.description == entry.description:
                return e.cmd_query
    return None


def get_figure_axis(lib, entry: VnaCommandEntry) -> str:
    """figure_axis 반환. 없으면 description 반환."""
    for e in _all_lib_entries(lib):
        if e.description == entry.description:
            fa = getattr(e, "figure_axis", "") or ""
            return fa if fa else entry.description
    return entry.description


def format_label(figure_axis: str, params: List[VnaParamSpec],
                 user_value: str = "") -> str:
    """figure_axis에 params를 채워 label 생성.
    user_input param은 user_value로 치환 (비어있으면 '[name]' placeholder)."""
    if not figure_axis:
        return ""
    d = {}
    for p in params:
        if p.is_user_input:
            d[p.name] = user_value if user_value else f"[{p.name}]"
        else:
            d[p.name] = p.value
    try:
        return figure_axis.format(**d)
    except Exception:
        return figure_axis


def parse_vna_array(raw: str) -> np.ndarray:
    """쉼표/세미콜론으로 구분된 VNA 응답 문자열을 numpy 배열로 변환.

    일반 숫자 배열(예: '1.0,2.0,3.0')은 빠르게 변환하고,
    Mercury iTC/iPS 형식의 스칼라 응답
    (예: 'STAT:DEV:MB1.T1:TEMP:SIG:TEMP:287.2650K')처럼 콜론 경로·단위 접미사가
    붙어 있어 그냥은 float이 안 되는 경우 토큰별로 숫자만 추출한다.
    """
    cleaned = raw.replace(";", ",").replace(" ", "")
    # 1) 일반 숫자 배열 빠른 경로
    try:
        arr = np.fromstring(cleaned, dtype=float, sep=",")
    except Exception:
        arr = np.array([])
    if arr.size > 0:
        return arr
    # 2) 폴백: 토큰별로 Mercury 형식(콜론 경로 + 단위 접미사) 숫자 추출
    from pythonization.instruments.parameter import _parse_float
    vals = []
    for tok in cleaned.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            vals.append(_parse_float(tok))
        except Exception:
            pass
    return np.array(vals, dtype=float)


def next_dat_path(folder: Path, base: str) -> Path:
    """folder/base_x001.dat → 다음 번호 경로 반환."""
    folder.mkdir(parents=True, exist_ok=True)
    idx = 1
    while True:
        p = folder / f"{base}_x{idx:03d}.dat"
        if not p.exists():
            return p
        idx += 1


def next_sweep_folder(base_dir: Path, filename: str) -> Path:
    """base_dir/filename_001 → 다음 번호 sweep 폴더 경로 반환 (미생성)."""
    base_dir.mkdir(parents=True, exist_ok=True)
    idx = 1
    while True:
        p = base_dir / f"{filename}_{idx:03d}"
        if not p.exists():
            return p
        idx += 1


def build_cmd(template: str, params: List[VnaParamSpec],
              user_value: str = "") -> str:
    """모든 params를 채워 최종 명령어 문자열 반환."""
    d = {p.name: (user_value if p.is_user_input else p.value) for p in params}
    try:
        return template.format(**d)
    except Exception as e:
        raise ValueError(f"Command format error '{template}': {e}")
