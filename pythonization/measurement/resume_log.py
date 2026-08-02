"""
ResumeLog: 통신 에러로 측정이 중단된 '재개 지점'을 영속 저장한다.

- 단일/더블 sweep 공통.
- 최근 10개까지 JSON 파일(SETTINGS_DIR/resume_points.json)에 보관 (FIFO).
- 각 지점은 측정을 그 위치에서 다시 시작하는 데 필요한 최소 상태를 담는다.

재개 정책 (사용자 합의):
  1. 통신 에러 → 10초 후 자동 재개 (세션 메모리 상태 그대로 이어감).
  2. 자동 재개 후 또 에러 → 측정 중단 + 재개 지점 저장 + 사용자가 수동 재개.
     (자동 재시도 예산은 정상 스텝 성공 시 0으로 리셋)
  3. 수동 재개는 기존 데이터 파일에 이어쓰기, 최근 10개 지점 중 선택.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from pythonization.app.paths import SETTINGS_DIR

_MAX_POINTS = 10
_LOG_PATH = SETTINGS_DIR / "resume_points.json"

# 통신 오류 판별은 pythonization.instruments.errors 로 일원화 (VISA error_code + 폭넓은 키워드).
# 하위호환을 위해 같은 이름으로 재노출.
from pythonization.instruments.errors import is_comm_error  # noqa: E402,F401  (re-export)


@dataclass
class ResumePoint:
    """측정 재개에 필요한 상태 스냅샷.

    sweep_type: "single" | "double"
    timestamp:  ISO8601 문자열 (외부에서 주입 — 스크립트 결정성 위해)
    label:      목록 표시용 한 줄 요약
    data_filepath: 이어쓸 .dat 파일 경로 (없으면 "")
    payload:    sweep_type별 재개 상태 딕셔너리
        single → {last_write_value, step_count, sweep_to, sweep_rate,
                  time_per_point, active_meas_indices}
        double → {array_idx, phase, last_write_value, second_value}
    """
    sweep_type: str
    timestamp: str
    label: str
    data_filepath: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ResumePoint":
        return cls(
            sweep_type=d.get("sweep_type", ""),
            timestamp=d.get("timestamp", ""),
            label=d.get("label", ""),
            data_filepath=d.get("data_filepath", ""),
            payload=d.get("payload", {}) or {},
        )


class ResumeLog:
    """resume_points.json 영속 관리 — 최근 _MAX_POINTS 개 보관."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _LOG_PATH

    def _load_raw(self) -> List[Dict[str, Any]]:
        try:
            if not self._path.exists():
                return []
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def all(self) -> List[ResumePoint]:
        """최신순(가장 최근이 먼저)으로 반환."""
        return [ResumePoint.from_dict(d) for d in reversed(self._load_raw())]

    def latest(self, sweep_type: Optional[str] = None) -> Optional[ResumePoint]:
        for p in self.all():
            if sweep_type is None or p.sweep_type == sweep_type:
                return p
        return None

    def add(self, point: ResumePoint) -> None:
        """새 지점을 추가하고 최대 _MAX_POINTS 개로 잘라 저장."""
        raw = self._load_raw()
        raw.append(point.to_dict())
        if len(raw) > _MAX_POINTS:
            raw = raw[-_MAX_POINTS:]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ResumeLog] save failed: {e}")
