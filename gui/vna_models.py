"""
VNA 공유 데이터 모델 + 설정 IO.
"""
import re
import yaml
import numpy as np
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field


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


class VnaPlotCurveConfig(BaseModel):
    x_source:  str = "index"              # "index" | arr label
    y_source:  str = "arr_0"             # 하위호환 단일 y
    y_sources: List[str] = Field(default_factory=list)  # multi-y (우선)
    label:     str = ""


class VnaConfigData(BaseModel):
    sections:    List[VnaSectionConfig]    = Field(default_factory=list)
    acquire:     VnaAcquireConfig          = Field(default_factory=VnaAcquireConfig)
    plot_curves: List[VnaPlotCurveConfig]  = Field(default_factory=list)
    main_folder: str = ""
    sub_folder:  str = ""
    save_enabled: bool = True


# ---------------------------------------------------------------------------
# Config IO
# ---------------------------------------------------------------------------

def load_vna_config(path: Path) -> VnaConfigData:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return VnaConfigData(**data)
    except Exception:
        pass
    return VnaConfigData()


def save_vna_config(cfg: VnaConfigData, path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(cfg.model_dump(), f, allow_unicode=True)
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
    """쉼표/세미콜론으로 구분된 VNA 응답 문자열을 numpy 배열로 변환."""
    try:
        cleaned = raw.replace(";", ",").replace(" ", "")
        return np.fromstring(cleaned, dtype=float, sep=",")
    except Exception:
        return np.array([])


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
