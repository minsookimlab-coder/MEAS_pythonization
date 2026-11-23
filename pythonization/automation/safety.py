"""AI 가 내린 명령을 장비에 보내기 전에 코드가 검사한다.

LLM 은 이 검사를 우회할 수 없다. 도구 실행기는 장비에 쓰기 전에 반드시 여기를
거치고, 걸리면 SafetyViolation 을 도구 결과(is_error)로 돌려준다. LLM 은 그 사유를
읽고 한계 안에서 계획을 고치거나 사용자에게 알린다.

원칙:
  - 사용자가 '허용'한 채널만 움직인다 (기본값은 금지).
  - 쓰이는 모든 값은 [min, max] 안에 있어야 한다. sweep 은 현재값→목표값을 직선으로
    지나가므로 양 끝이 범위 안이면 경로 전체가 범위 안이다. 현재값이 범위 밖이면
    첫 스텝부터 범위 밖 값을 쓰게 되므로 시작하지 않는다.
  - 속도(rate)는 사용자 상한 이하, Time/Point 는 하한 이상.
  - 점 수·소요 시간은 예산 이하.
"""
import math
import re
from typing import Dict, Optional

from pythonization.automation.settings import AutomationSettings


class SafetyViolation(Exception):
    """안전 검사 실패. 메시지는 LLM 과 사용자가 그대로 읽는다."""


_INVALID_PATH_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                   *(f"COM{i}" for i in range(1, 10)),
                   *(f"LPT{i}" for i in range(1, 10))}
_MAX_COMPONENT_LEN = 60
_MAX_FOLDER_DEPTH = 4
_MAX_PREFIX_LEN = 80


def _clean_component(text: str) -> str:
    """경로 한 조각을 Windows 에서 안전한 이름으로 정리."""
    text = _INVALID_PATH_CHARS.sub("_", text.strip())
    text = re.sub(r"\s+", "_", text)
    text = text.rstrip(" .")[:_MAX_COMPONENT_LEN].rstrip(" .")
    if text.upper().split(".")[0] in _RESERVED_NAMES:
        text = f"_{text}"
    return text


def sanitize_sub_folder(text: str) -> str:
    """Main Folder 아래 상대 경로만 허용한다. 위로 올라가거나 드라이브를 바꾸지 못한다."""
    text = (text or "").strip()
    if not text:
        return ""
    if re.match(r"^[A-Za-z]:", text) or text.startswith(("/", "\\")):
        raise SafetyViolation(
            f"sub_folder 는 Main Folder 아래 상대 경로여야 합니다: '{text}'")
    parts = [p for p in re.split(r"[\\/]+", text) if p.strip()]
    if any(p.strip() in (".", "..") for p in parts):
        raise SafetyViolation(f"sub_folder 에 '.' / '..' 를 쓸 수 없습니다: '{text}'")
    if len(parts) > _MAX_FOLDER_DEPTH:
        raise SafetyViolation(
            f"sub_folder 는 {_MAX_FOLDER_DEPTH}단계까지만 허용합니다: '{text}'")
    cleaned = [_clean_component(p) for p in parts]
    return "/".join(c for c in cleaned if c)


def sanitize_file_prefix(text: str) -> str:
    """파일명 앞부분. 경로 구분자는 '_' 로 바꾼다."""
    text = re.sub(r"[\\/]+", "_", (text or "").strip())
    text = _INVALID_PATH_CHARS.sub("_", text)
    text = re.sub(r"\s+", "_", text)
    return text.rstrip(" .")[:_MAX_PREFIX_LEN].rstrip(" .")


def _finite(value, name: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise SafetyViolation(f"{name} 가 숫자가 아닙니다: {value!r}")
    if not math.isfinite(v):
        raise SafetyViolation(f"{name} 가 유한한 숫자가 아닙니다: {value!r}")
    return v


class SafetyPolicy:
    """프로파일 스냅샷 + 사용자 설정으로 만든 검사기. 측정 한 번(run) 동안 고정."""

    def __init__(self, settings: AutomationSettings, profile):
        self.settings = settings
        self.profile = profile

    # ── 인덱스 → 항목 ──────────────────────────────────────────────────
    def _sweep_value(self, idx: int):
        svs = self.profile.sweep_values
        if not isinstance(idx, int) or not 0 <= idx < len(svs):
            raise SafetyViolation(
                f"sweep channel 인덱스 {idx!r} 가 없습니다 (0..{len(svs) - 1}).")
        sv = svs[idx]
        if getattr(sv, "needs_fix", ""):
            raise SafetyViolation(
                f"sweep channel {idx} '{sv.description}' 은(는) 라이브러리 오류로 "
                f"쓸 수 없습니다: {sv.needs_fix}")
        return sv

    def sweep_channel(self, idx: int):
        """허용된 sweep channel 과 그 정책. 허용 안 됐거나 범위가 없으면 거부."""
        sv = self._sweep_value(idx)
        pol = self.settings.sweep_policy(sv.alias, sv.description)
        if pol is None or not pol.allowed:
            raise SafetyViolation(
                f"sweep channel {idx} '[{sv.alias}] {sv.description}' 은(는) AI 조작이 "
                "허용되지 않았습니다. 사용자가 AI 자동 측정 창의 안전 한계 표에서 "
                "허용해야 합니다.")
        if pol.min_value is None or pol.max_value is None:
            raise SafetyViolation(
                f"sweep channel {idx} '{sv.description}' 의 최소/최대 값이 설정되지 "
                "않았습니다. 범위 없이 움직이지 않습니다.")
        return sv, pol

    def check_sweep_value(self, idx: int, value, name: str = "target") -> float:
        sv, pol = self.sweep_channel(idx)
        v = _finite(value, name)
        if not pol.min_value <= v <= pol.max_value:
            raise SafetyViolation(
                f"{name}={v:g} {sv.unit} 이(가) '{sv.description}' 허용 범위 "
                f"[{pol.min_value:g}, {pol.max_value:g}] {sv.unit} 밖입니다.")
        return v

    def check_current_in_range(self, idx: int, current: float) -> None:
        """현재값이 범위 밖이면 첫 스텝부터 범위 밖 값을 쓴다 — 시작하지 않는다."""
        sv, pol = self.sweep_channel(idx)
        if not math.isfinite(current) or not pol.min_value <= current <= pol.max_value:
            raise SafetyViolation(
                f"'{sv.description}' 의 현재값 {current:g} {sv.unit} 이(가) 허용 범위 "
                f"[{pol.min_value:g}, {pol.max_value:g}] 밖입니다. 장비를 건드리지 "
                "않았습니다. 사용자가 직접 범위 안으로 옮겨야 합니다.")

    def check_rate(self, idx: int, rate, time_per_point) -> tuple:
        sv, pol = self.sweep_channel(idx)
        r = _finite(rate, "rate")
        tpp = _finite(time_per_point, "time_per_point")
        if r <= 0:
            raise SafetyViolation(f"rate 는 0보다 커야 합니다 (받은 값 {r:g}).")
        if pol.max_rate is not None and r > pol.max_rate:
            raise SafetyViolation(
                f"rate={r:g} {sv.unit}/min 이(가) '{sv.description}' 상한 "
                f"{pol.max_rate:g} {sv.unit}/min 을 넘습니다.")
        self.check_time_per_point(tpp)
        return r, tpp

    def check_time_per_point(self, tpp) -> float:
        t = _finite(tpp, "time_per_point")
        if t < self.settings.min_time_per_point:
            raise SafetyViolation(
                f"time_per_point={t:g} s 가 하한 {self.settings.min_time_per_point:g} s "
                "보다 작습니다.")
        return t

    def check_points(self, start: float, stop: float, rate: float, tpp: float) -> int:
        """예상 점 수. 상한을 넘으면 거부."""
        step = rate / 60.0 * tpp
        n = int(math.ceil(abs(stop - start) / step)) + 1 if step > 0 else 0
        if n > self.settings.max_points_per_sweep:
            raise SafetyViolation(
                f"예상 점 수 {n} 이(가) 상한 {self.settings.max_points_per_sweep} 을 "
                "넘습니다. rate 를 키우거나 time_per_point 를 늘리거나 구간을 나누세요.")
        return n

    @staticmethod
    def estimate_seconds(start: float, stop: float, rate: float) -> float:
        return abs(stop - start) / rate * 60.0 if rate > 0 else float("inf")

    # ── second channel ──────────────────────────────────────────────────
    def second_channel(self, idx: int):
        chs = self.profile.second_sweep_channels
        if not isinstance(idx, int) or not 0 <= idx < len(chs):
            raise SafetyViolation(
                f"second channel 인덱스 {idx!r} 가 없습니다 (0..{len(chs) - 1}).")
        ch = chs[idx]
        pol = self.settings.second_policy(ch.alias, ch.description)
        if pol is None or not pol.allowed:
            raise SafetyViolation(
                f"second channel {idx} '[{ch.alias}] {ch.description}' 은(는) AI 조작이 "
                "허용되지 않았습니다.")
        if pol.min_value is None or pol.max_value is None:
            raise SafetyViolation(
                f"second channel {idx} '{ch.description}' 의 최소/최대 값이 설정되지 "
                "않았습니다.")
        return ch, pol

    def check_second_value(self, idx: int, value) -> float:
        ch, pol = self.second_channel(idx)
        v = _finite(value, "value")
        if not pol.min_value <= v <= pol.max_value:
            raise SafetyViolation(
                f"value={v:g} {ch.unit} 이(가) '{ch.description}' 허용 범위 "
                f"[{pol.min_value:g}, {pol.max_value:g}] 밖입니다.")
        return v

    # ── write command ──────────────────────────────────────────────────
    def write_command(self, idx: int, value):
        """허용된 write command 와 최종 명령 문자열. 값 범위도 검사한다."""
        wcs = self.profile.write_cmds
        if not isinstance(idx, int) or not 0 <= idx < len(wcs):
            raise SafetyViolation(
                f"write command 인덱스 {idx!r} 가 없습니다 (0..{len(wcs) - 1}).")
        wc = wcs[idx]
        if getattr(wc, "needs_fix", ""):
            raise SafetyViolation(f"write command '{wc.description}' 은(는) 쓸 수 없습니다: "
                                  f"{wc.needs_fix}")
        pol = self.settings.write_policy(wc.alias, wc.description)
        if pol is None or not pol.allowed:
            raise SafetyViolation(
                f"write command {idx} '[{wc.alias}] {wc.description}' 은(는) AI 사용이 "
                "허용되지 않았습니다.")
        needs_value = "{v}" in wc.cmd_set
        if not needs_value:
            if value is not None:
                raise SafetyViolation(
                    f"'{wc.description}' 은(는) 값을 받지 않는 명령입니다. value 를 비우세요.")
            return wc, wc.cmd_set
        v = _finite(value, "value")
        if pol.min_value is None or pol.max_value is None:
            raise SafetyViolation(
                f"'{wc.description}' 의 값 범위가 설정되지 않아 값을 보낼 수 없습니다.")
        if not pol.min_value <= v <= pol.max_value:
            raise SafetyViolation(
                f"value={v:g} 이(가) '{wc.description}' 허용 범위 "
                f"[{pol.min_value:g}, {pol.max_value:g}] 밖입니다.")
        return wc, wc.cmd_set.format(v=v)

    # ── measurement ─────────────────────────────────────────────────────
    def measurement_indices(self, indices, require: bool = True) -> list:
        ms = self.profile.measurements
        if indices is None:
            indices = []
        if not isinstance(indices, list):
            raise SafetyViolation("measurements 는 정수 목록이어야 합니다.")
        out = []
        for idx in indices:
            if not isinstance(idx, int) or not 0 <= idx < len(ms):
                raise SafetyViolation(
                    f"measurement 인덱스 {idx!r} 가 없습니다 (0..{len(ms) - 1}).")
            if getattr(ms[idx], "needs_fix", ""):
                raise SafetyViolation(
                    f"measurement {idx} '{ms[idx].description}' 은(는) 쓸 수 없습니다: "
                    f"{ms[idx].needs_fix}")
            if idx not in out:
                out.append(idx)
        if require and not out:
            raise SafetyViolation("기록할 measurement 를 하나 이상 고르세요.")
        return out

    def abort_limits(self, indices: list, extra: Optional[list] = None) -> Dict[int, float]:
        """측정 중 즉시 중단 임계 {measurement index: |값| 상한}.

        사용자 설정이 기본이고, LLM 은 더 엄격한(작은) 값만 추가할 수 있다.
        """
        limits: Dict[int, float] = {}
        ms = self.profile.measurements
        for idx in indices:
            pol = self.settings.measurement_policy(ms[idx].alias, ms[idx].description)
            if pol is not None and pol.abort_abs is not None:
                limits[idx] = abs(pol.abort_abs)
        for item in extra or []:
            try:
                idx = int(item["measurement"])
                lim = abs(_finite(item["limit"], "limit"))
            except (KeyError, TypeError, ValueError):
                raise SafetyViolation(
                    "abort_if_abs_above 항목은 {measurement, limit} 형식이어야 합니다.")
            if idx not in indices:
                raise SafetyViolation(
                    f"abort_if_abs_above 의 measurement {idx} 가 기록 목록에 없습니다.")
            limits[idx] = min(limits.get(idx, lim), lim)
        return limits
