"""
MetaDataManager: sweep 종료 시 메타 데이터(JSON)를 기록합니다.

두 가지 정보를 저장:
1. MetaDataConfig에서 활성화된 VISA 쿼리 항목 → 스윕 종료 시 한 번 쿼리
2. MeasType.TEMPERATURE / BFIELD로 분류된 active measurement →
   스윕 전체 측정값의 평균(mean)과 표준편차(std) 자동 계산

파일명: 해당 .dat 파일과 동일한 경로/이름, 확장자만 .json
"""
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pythonization.config.models import MetaDataConfig, MeasType, InstantiatedMeasurement
from pythonization.instruments.parameter import _parse_float

_TSP_PRINT_RE = re.compile(r"^\s*print\((.+)\)\s*$", re.DOTALL)


class MetaDataManager:

    def __init__(self, session):
        self._session = session
        # row_idx → 누적 float 값 목록 (T/B 전용)
        self._tb_buffer: Dict[int, List[float]] = {}
        # row_idx → (column_label, unit, description)
        self._tb_meta: Dict[int, Tuple[str, str, str]] = {}
        # T/B description 집합 (save()에서 빠른 조회용)
        self._tb_desc_set: set = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def configure(
        self,
        active_meas_indices: List[int],
        measurements: List[InstantiatedMeasurement],
        meas_labels: List[str],
        meas_type_overrides: Optional[Dict[int, "MeasType"]] = None,
    ) -> None:
        """
        Sweep 시작 시 호출.
        T/B 계열 active measurement를 등록하고 버퍼를 초기화합니다.

        meas_labels: active_meas_indices에 대응하는 .dat 컬럼 라벨 목록.
        meas_type_overrides: 런타임 콤보박스 값 {row_idx: MeasType} — 없으면 m.meas_type 사용.
        """
        self._tb_buffer.clear()
        self._tb_meta.clear()
        overrides = meas_type_overrides or {}
        for i, idx in enumerate(active_meas_indices):
            if idx < len(measurements):
                m = measurements[idx]
                runtime_type = overrides.get(idx, m.meas_type)
                if runtime_type in (MeasType.TEMPERATURE, MeasType.BFIELD):
                    label = meas_labels[i] if i < len(meas_labels) else (m.figure_axis or m.description)
                    self._tb_buffer[idx] = []
                    self._tb_meta[idx] = (label, m.unit, m.description)
        self._tb_desc_set: set = {meta[2] for meta in self._tb_meta.values()}

    def clear(self) -> None:
        """T/B 버퍼만 초기화 (Double Sweep의 각 step 시작 시 호출)."""
        for idx in self._tb_buffer:
            self._tb_buffer[idx] = []

    # ------------------------------------------------------------------
    # Data accumulation
    # ------------------------------------------------------------------

    def record_step(self, meas_results: List[Tuple[int, Optional[float]]]) -> None:
        """각 스텝마다 호출. T/B 측정값을 버퍼에 누적합니다."""
        meas_map = {row: val for row, val in meas_results}
        for idx in self._tb_buffer:
            val = meas_map.get(idx)
            if val is not None:
                self._tb_buffer[idx].append(val)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, cfg: MetaDataConfig, dat_filepath: Optional[Path],
             extra: Optional[dict] = None) -> None:
        """
        메타 데이터 JSON 파일을 저장합니다.
        dat_filepath: .dat 파일 경로 (확장자를 .json으로 교체).
                      None이면 저장하지 않습니다.
        extra: 측정에 사용된 추가 설정(예: second channel advance 파라미터)을
               담은 딕셔너리. 제공되면 metadata config 활성화 여부와 무관하게
               JSON에 병합 저장합니다.

        cfg.enabled=False 이고 extra도 없으면 저장하지 않습니다.
        cfg.enabled=False 이지만 extra가 있으면 extra + timestamp만 저장합니다.
        """
        if dat_filepath is None:
            return
        if not cfg.enabled and not extra:
            return

        json_path = Path(dat_filepath).with_suffix(".json")
        data: dict = {"timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}

        if not cfg.enabled:
            # metadata config는 꺼져 있지만 추가 설정만 기록
            if extra:
                data.update(extra)
            self._write_json(json_path, data)
            return

        # T/B 조건 3개 모두 만족하는 description 집합
        # (active 체크는 configure()에서 이미 필터됨 — _tb_buffer에 있으면 조건1 만족)
        # 조건3: cfg.entries에서 enabled=True인 description
        enabled_tb_descs = {
            entry.description
            for entry in cfg.entries
            if entry.enabled and entry.description in self._tb_desc_set
        }

        # 1. 설정된 VISA 쿼리 항목 (스윕 종료 후 즉시 쿼리)
        # T/B 조건 모두 만족하는 항목은 mean/std로 처리하므로 VISA 쿼리 생략
        for entry in cfg.entries:
            if not entry.enabled:
                continue
            if entry.description in enabled_tb_descs:
                continue   # T/B mean/std로 처리
            key = entry.figure_axis or entry.description
            try:
                if not self._session.is_open(entry.alias):
                    self._session.open(entry.alias)
                raw = self._session.query(entry.alias, entry.resolved_cmd).strip()
                # _parse_float: 단위 접미사(T, A…)·':' 구분 응답(IPS 등)도 안전 파싱
                val = _parse_float(raw)
                data[key] = {"value": val, "unit": entry.unit}
            except Exception as e:
                data[key] = {"error": str(e)}

        # 2. T/B 통계: 조건3(enabled) 만족 항목만 저장
        for idx, values in self._tb_buffer.items():
            if not values:
                continue
            label, unit, desc = self._tb_meta[idx]
            if desc not in enabled_tb_descs:
                continue   # 조건3 미충족 — 저장 안 함
            n = len(values)
            mean = sum(values) / n
            std = math.sqrt(sum((v - mean) ** 2 for v in values) / n) if n > 1 else 0.0
            data[f"{label}_mean"] = {"value": round(mean, 9), "unit": unit}
            data[f"{label}_std"] = {"value": round(std, 9), "unit": unit}

        # 3. 추가 설정 (second channel advance 파라미터 등)
        if extra:
            data.update(extra)

        self._write_json(json_path, data)

    @staticmethod
    def _write_json(json_path: Path, data: dict) -> None:
        try:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[MetaDataManager] Save failed: {e}")
