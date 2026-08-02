"""
MFLI Noise Sweep 모듈 데이터 모델 + 설정 IO.

VNA 모듈([gui/vna_models.py])과 같은 방식(pydantic + YAML, 원자적 저장)이나, MFLI 주파수
noise sweep에 필요한 것만 담아 훨씬 단순하다. 세션 한정이 아니라 프로파일별로 영속화된다.
"""
import os
import yaml
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field


class MfliAuxRead(BaseModel):
    """각 주파수 포인트마다 함께 읽을 보조 계측기 값 1개 (예: ITC 온도, M81 전압)."""
    alias:     str = ""          # 계측기 alias (instruments.yaml)
    query_cmd: str = ""          # 읽기 명령 (SCPI 또는 노드 경로)
    label:     str = ""          # 저장 열 라벨 / 플롯 소스 이름
    unit:      str = ""          # 저장 파일 2행 단위
    enabled:   bool = True


class MfliAcquireConfig(BaseModel):
    """MFLI 주파수 sweep + noise 측정 설정."""
    mfli_alias: str = "MFLI"
    # 측정 방식:
    #   False = 이 프로그램이 주파수를 직접 sweep (driven).
    #   True  = LabOne(Sweeper 등)이 주파수를 몰고, 우리는 시간 기반으로 따라 읽기(follow).
    follow_external: bool = False
    # follow 모드: 폴링 간격(초)과 샘플 수(0 = Stop 누를 때까지)
    poll_interval: float = 1.0
    poll_count:    int   = 0
    # 주파수 sweep (LINEAR) — driven 모드에서만 사용
    sweep_start: float = 1e6     # Hz
    sweep_stop:  float = 1e7     # Hz
    sweep_n:     int   = 11      # 포인트 수 (>=1)
    # MFLI 노드 명령 ('{dev}'는 드라이버가 device_id로 치환)
    freq_write_cmd: str = "/{dev}/oscs/0/freq"   # worker가 " = {value}"를 덧붙여 set
    noise_node:  str = "/{dev}/demods/0/sample.r"  # LabOne에서 구성한 스칼라 noise/PSD 노드
    noise_label: str = "noise"
    noise_unit:  str = "V/sqrtHz"
    # 각 포인트마다 함께 읽을 보조값 (온도·M81 전압 등). {M}은 m_value로 치환됨.
    aux_reads: List[MfliAuxRead] = Field(default_factory=lambda: [
        MfliAuxRead(alias="ITC", query_cmd="READ:DEV:DB8.T1:TEMP:SIG:TEMP",
                    label="probe_T", unit="K", enabled=True),
        MfliAuxRead(alias="M81", query_cmd="FETCh:SENSe{M}:DC?",
                    label="M81_V", unit="V", enabled=True),
    ])
    # 명령 안의 '{M}' 자리표시자에 넣을 값(예: M81 sense 모듈 번호). 전송 직전 치환.
    m_value: str = "1"
    filename: str = "mfli_noise"


class MfliPlotCurveConfig(BaseModel):
    x_source:  str = "frequency"
    y_source:  str = "noise"
    y_sources: List[str] = Field(default_factory=list)
    log_x:     bool = False
    log_y:     bool = False


class MfliConfigData(BaseModel):
    acquire:      MfliAcquireConfig = Field(default_factory=MfliAcquireConfig)
    plot_curves:  List[MfliPlotCurveConfig] = Field(default_factory=list)
    main_folder:  str = ""
    sub_folder:   str = ""
    save_enabled: bool = True
    # LabOne CSV + 우리 .dat sweep별 병합(core.mfli_merge) 경로 기억용
    merge_labone: str = ""    # LabOne CSV 파일 또는 autosave 폴더
    merge_ours:   str = ""    # 우리 모듈 .dat
    merge_outdir: str = ""    # 병합 결과 저장 폴더


# ---------------------------------------------------------------------------
# Config IO
# ---------------------------------------------------------------------------

def load_mfli_config(path: Path) -> MfliConfigData:
    if not path.exists():
        return MfliConfigData()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return MfliConfigData(**data)
    except Exception as e:
        print(f"[MFLI] Config load failed ({path.name}): {type(e).__name__}: {e}")
        try:
            import shutil
            shutil.copy2(path, path.with_suffix(path.suffix + ".loadfail-bak"))
        except Exception:
            pass
        return MfliConfigData()


def save_mfli_config(cfg: MfliConfigData, path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                import shutil
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            except Exception:
                pass
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(cfg.model_dump(mode="json"), f, allow_unicode=True)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[MFLI] Config save failed: {e}")
