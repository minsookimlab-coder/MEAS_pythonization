"""
DerivativeChannel: 슬라이딩 윈도우 기반 실시간 dA1/dA2 계산.

설계 원칙:
- GUI 스레드에서 직접 호출 (numpy 연산 < 1 µs on 10-20 points)
  → 별도 워커 스레드 없이도 측정 루프에 부하 없음
- scipy 없으면 자동으로 Linear Regression fallback
- div/zero 방지: min_delta 임계값으로 guard
- 메서드:
    linear  — np.polyfit(A2, A1, 1)[0]  = dA1/dA2 (직접 회귀)
    savgol  — SG 1차 미분 비율: (dA1/d_idx) / (dA2/d_idx)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

# ──────────────────────────────────────────────────────────
# Column key used in GraphDataPoint.values and DataSaver
# ──────────────────────────────────────────────────────────
OUTPUT_KEY = "__deriv__"


# ──────────────────────────────────────────────────────────
# Config  (stored in FullProfile)
# ──────────────────────────────────────────────────────────

@dataclass
class DerivativeConfig:
    """사용자가 설정하는 파생 채널 파라미터."""
    enabled: bool = False
    numerator_key: str = ""       # A1 column key  (e.g. "__sweep__")
    denominator_key: str = ""     # A2 column key  (e.g. "smua_current")
    output_label: str = ""        # e.g. "dV/dI"   (빈 문자열이면 자동 생성)
    output_unit: str = ""         # e.g. "Ω"
    window_size: int = 10         # sliding window 길이 (3 ≤ N ≤ 50)
    method: str = "linear"        # "linear" | "savgol"
    min_delta: float = 1e-10      # |ΔA2| 임계값 — 이 미만이면 None 반환


# ──────────────────────────────────────────────────────────
# Worker
# ──────────────────────────────────────────────────────────

class DerivativeChannel:
    """슬라이딩 윈도우 방식으로 dA1/dA2를 실시간 계산.

    사용법:
        ch = DerivativeChannel(cfg)
        ch.reset()                      # sweep 시작 시 버퍼 초기화
        val = ch.push(a1, a2)           # 매 스텝마다 호출
        # val: float or None (< 3 points, or ΔA2 too small)
    """

    def __init__(self, config: DerivativeConfig) -> None:
        self._cfg = config
        n = max(3, min(config.window_size, 50))
        self._buf_a1: deque = deque(maxlen=n)
        self._buf_a2: deque = deque(maxlen=n)

    def reconfigure(self, config: DerivativeConfig) -> None:
        """설정 변경 시 버퍼 재생성."""
        self._cfg = config
        n = max(3, min(config.window_size, 50))
        self._buf_a1 = deque(maxlen=n)
        self._buf_a2 = deque(maxlen=n)

    def reset(self) -> None:
        """sweep 시작 시 버퍼 초기화."""
        self._buf_a1.clear()
        self._buf_a2.clear()

    def push(self, a1: float, a2: float) -> Optional[float]:
        """새 (A1, A2) 쌍 추가 후 dA1/dA2 반환.

        None을 반환하는 경우:
          - 버퍼에 점이 3개 미만
          - A2 변화량이 min_delta 미만 (divide-by-zero 방지)
          - 수치 오류
        """
        self._buf_a1.append(a1)
        self._buf_a2.append(a2)
        if len(self._buf_a1) < 3:
            return None

        a1_arr = np.asarray(self._buf_a1, dtype=np.float64)
        a2_arr = np.asarray(self._buf_a2, dtype=np.float64)

        if self._cfg.method == "savgol":
            result = self._savgol_deriv(a1_arr, a2_arr)
            if result is not None:
                return result
        # linear (default + savgol fallback)
        return self._linear_deriv(a1_arr, a2_arr)

    # ── Computation methods ────────────────────────────────────────────

    def _linear_deriv(self, a1: np.ndarray, a2: np.ndarray) -> Optional[float]:
        """A2 vs A1 linear regression → slope = dA1/dA2."""
        delta = float(np.max(a2) - np.min(a2))
        if delta < self._cfg.min_delta:
            return None     # A2 너무 평탄 → meaningless ratio
        try:
            slope = float(np.polyfit(a2, a1, 1)[0])
            return slope
        except (np.linalg.LinAlgError, ValueError):
            return None

    def _savgol_deriv(self, a1: np.ndarray, a2: np.ndarray) -> Optional[float]:
        """(dA1/d_idx) / (dA2/d_idx) 를 SG 필터로 추정 → dA1/dA2."""
        try:
            from scipy.signal import savgol_filter  # type: ignore
        except ImportError:
            return None         # scipy 없으면 caller가 linear로 fallback

        n = len(a1)
        wl = n if n % 2 == 1 else n - 1    # window_length 는 홀수
        if wl < 5:
            return None
        poly = min(2, wl - 1)
        try:
            da1 = savgol_filter(a1, wl, polyorder=poly, deriv=1)
            da2 = savgol_filter(a2, wl, polyorder=poly, deriv=1)
        except Exception:
            return None

        mid = n // 2
        if abs(da2[mid]) < self._cfg.min_delta:
            return None
        return float(da1[mid] / da2[mid])

    # ── Metadata helpers ───────────────────────────────────────────────

    @property
    def output_key(self) -> str:
        return OUTPUT_KEY

    def col_info(self) -> Tuple[str, str, str]:
        """(key, display_label, unit) — GraphWindow.begin_session() 용."""
        if self._cfg.output_label:
            lbl = self._cfg.output_label
        elif self._cfg.numerator_key and self._cfg.denominator_key:
            n = self._cfg.numerator_key.lstrip("_")
            d = self._cfg.denominator_key.lstrip("_")
            lbl = f"d{n}/d{d}"
        else:
            lbl = "deriv"
        return (OUTPUT_KEY, lbl, self._cfg.output_unit)
