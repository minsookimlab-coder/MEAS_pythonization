"""AI 자동 측정 설정.

두 파일로 나눈다 (둘 다 SETTINGS_DIR 아래 — 저장소에 들어가지 않는다):

  llm.yaml                        API 키·모델·effort. 프로파일과 무관한 전역값.
                                  API 키가 담기므로 저장소 밖에만 둔다(규칙 3).
  profiles/automation/{name}.yaml 프로파일별 — 채널 허용·한계·역할 메모, 실험 메모,
                                  승인 방식, 예산, 보고 설정.

채널 정책은 프로파일 항목의 (alias, description) 으로 찾는다. 인덱스로 저장하면
Parameter Manager 에서 순서를 바꿨을 때 엉뚱한 채널의 한계가 적용된다.

기본값은 '아무것도 허용하지 않음'이다. 사용자가 채널을 직접 허용하고 범위를 적어야
AI 가 그 채널을 움직일 수 있다.
"""
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field

from pythonization.app.paths import SETTINGS_DIR

#: 모델 선택지. 첫 항목이 기본값이다.
MODEL_CHOICES = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2")
EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")


class LlmSettings(BaseModel):
    """전역 LLM 연결 설정 — SETTINGS_DIR/llm.yaml."""
    api_key: str = ""                 # 비어 있으면 ANTHROPIC_API_KEY 등 SDK 기본 경로
    model: str = MODEL_CHOICES[0]
    effort: str = "high"              # 장시간 도구 호출 작업 — high 가 기본
    max_tokens: int = 16000           # 비스트리밍 요청 상한 (SDK 10분 제한 아래)
    request_timeout_s: float = 600.0
    # Claude Code(데스크톱 앱·CLI) 연결용 MCP 서버 — 127.0.0.1 전용
    mcp_enabled: bool = False         # 프로그램 시작 시 자동으로 켠다
    mcp_port: int = 8765


class SweepChannelPolicy(BaseModel):
    """메인 sweep channel 하나에 대한 AI 허용 범위."""
    alias: str
    description: str
    allowed: bool = False
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    max_rate: Optional[float] = None      # units/min. None 이면 제한 없음
    role: str = ""                        # 사용자 메모 — 예: "게이트 (smua)"


class SecondChannelPolicy(BaseModel):
    """Second sweep channel(온도·자기장 등) 하나에 대한 AI 허용 범위."""
    alias: str
    description: str
    allowed: bool = False
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    role: str = ""


class MeasurementPolicy(BaseModel):
    """Measurement 항목 하나 — 역할 메모와 즉시 중단 임계."""
    alias: str
    description: str
    role: str = ""
    # |값| 이 이 값을 넘으면 AI 판단을 기다리지 않고 sweep 을 즉시 멈춘다
    # (예: 게이트 누설전류). None 이면 검사하지 않는다.
    abort_abs: Optional[float] = None


class WriteCmdPolicy(BaseModel):
    """Write command 하나 — 기본은 금지. 허용한 것만 AI 가 보낼 수 있다."""
    alias: str
    description: str
    allowed: bool = False
    min_value: Optional[float] = None     # {v} 가 있는 명령의 값 범위
    max_value: Optional[float] = None
    role: str = ""


class AutomationSettings(BaseModel):
    """프로파일별 AI 자동 측정 설정."""
    approval_mode: Literal["approve", "auto"] = "approve"
    max_run_hours: float = 24.0
    max_turns: int = 80                   # LLM 호출 횟수 상한
    max_points_per_sweep: int = 20000
    min_time_per_point: float = 0.2       # sec
    report_to_messenger: bool = True      # send_message 를 텔레그램/이메일로 보낸다
    report_on_finish: bool = True         # 끝나면 최종 보고를 자동 전송
    restore_main_ui: bool = True          # 끝나면 메인 창 sweep/저장 설정을 원래대로
    lab_notes: str = ""                   # 시료·배선·관례 등 자유 메모
    sweep_channels: List[SweepChannelPolicy] = Field(default_factory=list)
    second_channels: List[SecondChannelPolicy] = Field(default_factory=list)
    measurements: List[MeasurementPolicy] = Field(default_factory=list)
    write_cmds: List[WriteCmdPolicy] = Field(default_factory=list)

    # ── (alias, description) 조회 ─────────────────────────────────────
    @staticmethod
    def _find(items, alias: str, description: str):
        for item in items:
            if item.alias == alias and item.description == description:
                return item
        return None

    def sweep_policy(self, alias: str, description: str) -> Optional[SweepChannelPolicy]:
        return self._find(self.sweep_channels, alias, description)

    def second_policy(self, alias: str, description: str) -> Optional[SecondChannelPolicy]:
        return self._find(self.second_channels, alias, description)

    def measurement_policy(self, alias: str, description: str) -> Optional[MeasurementPolicy]:
        return self._find(self.measurements, alias, description)

    def write_policy(self, alias: str, description: str) -> Optional[WriteCmdPolicy]:
        return self._find(self.write_cmds, alias, description)

    def sync_with_profile(self, profile) -> None:
        """프로파일 항목마다 정책 행이 하나씩 있게 맞춘다.

        새 항목은 '허용 안 함'으로 추가하고, 프로파일에서 사라진 항목의 정책은
        지우지 않는다 — 항목을 잠시 뺐다 다시 넣어도 사용자가 적은 한계가 남는다.
        """
        for sv in profile.sweep_values:
            if self.sweep_policy(sv.alias, sv.description) is None:
                self.sweep_channels.append(
                    SweepChannelPolicy(alias=sv.alias, description=sv.description))
        for ch in profile.second_sweep_channels:
            if self.second_policy(ch.alias, ch.description) is None:
                self.second_channels.append(
                    SecondChannelPolicy(alias=ch.alias, description=ch.description))
        for m in profile.measurements:
            if self.measurement_policy(m.alias, m.description) is None:
                self.measurements.append(
                    MeasurementPolicy(alias=m.alias, description=m.description))
        for wc in profile.write_cmds:
            if self.write_policy(wc.alias, wc.description) is None:
                self.write_cmds.append(
                    WriteCmdPolicy(alias=wc.alias, description=wc.description))


# ── 파일 IO ───────────────────────────────────────────────────────────

def llm_settings_path(settings_dir: Optional[Path] = None) -> Path:
    return (settings_dir or SETTINGS_DIR) / "llm.yaml"


def automation_settings_path(profile_name: str,
                             settings_dir: Optional[Path] = None) -> Path:
    safe = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c
                   for c in profile_name).strip().strip(".") or "profile"
    return (settings_dir or SETTINGS_DIR) / "profiles" / "automation" / f"{safe}.yaml"


def _load_yaml(path: Path) -> dict:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def load_llm_settings(settings_dir: Optional[Path] = None) -> LlmSettings:
    try:
        return LlmSettings(**_load_yaml(llm_settings_path(settings_dir)))
    except Exception:
        return LlmSettings()


def save_llm_settings(cfg: LlmSettings, settings_dir: Optional[Path] = None) -> None:
    _save_yaml(llm_settings_path(settings_dir), cfg.model_dump())


def load_automation_settings(profile_name: str,
                             settings_dir: Optional[Path] = None) -> AutomationSettings:
    try:
        return AutomationSettings(
            **_load_yaml(automation_settings_path(profile_name, settings_dir)))
    except Exception:
        return AutomationSettings()


def save_automation_settings(cfg: AutomationSettings, profile_name: str,
                             settings_dir: Optional[Path] = None) -> None:
    _save_yaml(automation_settings_path(profile_name, settings_dir),
               cfg.model_dump(mode="json"))
