"""
Sweep step calculator.
"""
from dataclasses import dataclass
from typing import Optional, Tuple


def calculate_next_step(
    source_value: float,
    sweep_to: float,
    sweep_rate: float,
    time_per_point: float,
) -> Tuple[float, bool]:
    """
    다음 스텝 값과 완료 여부를 반환합니다.

    반환: (next_value, is_done)
      is_done=True  → source_value가 sweep_to에 정확히 도달했음
      is_done=False → 아직 진행 중 (클램핑 포함)
    """
    # 방향은 목표와의 차이(diff)로만 결정한다.
    diff = sweep_to - source_value
    # 목표값과의 차이가 아주 미세하면(1E-11) 목표값 반환 및 종료 신호(True)
    if abs(diff) < 1e-11:
        return sweep_to, True
    direction = 1 if diff > 0 else -1
    # 1. 증분량 = (|units/min| / 60) * |seconds|. 항상 양수 '크기'로 계산하고
    #    방향은 diff로만 적용한다(음수 rate가 목표 반대로 폭주하는 것을 방지).
    increment = (abs(sweep_rate) / 60.0) * abs(time_per_point)
    # rate<=0 또는 tpp<=0 → 진행 불가. 같은 값을 영원히 반환하는 무한 정지/쓰기 폭주
    # 대신 명시적 오류로 끊는다(호출부 워커가 step_error/advance error로 처리).
    if increment <= 0:
        raise ValueError(
            f"진행 불가: sweep_rate={sweep_rate}, time_per_point={time_per_point} "
            "(둘 다 0보다 커야 합니다).")
    # 다음 스텝 계산
    next_step = source_value + (increment * direction)
    # 4. 목표 초과 방지 로직 (False 케이스)
    # 다음 스텝이 목표를 넘어서면 그냥 목표값(sweep_to)을 반환
    if (direction == 1 and next_step > sweep_to) or (direction == -1 and next_step < sweep_to):
        return sweep_to, False
    else:
        return next_step, False


@dataclass
class SweepConfig:
    source_value: Optional[float] = None  # 측정 시작 시 기기에서 읽어옴
    sweep_to: float = 0.0
    sweep_rate: float = 1.0              # units / min
    time_per_point: float = 1.0          # sec

    def step_size(self) -> float:
        """한 스텝당 변화량 (units/step)."""
        return (self.sweep_rate / 60.0) * self.time_per_point

    def next_step(self) -> Tuple[Optional[float], bool]:
        """
        source_value에서 한 스텝 이동한 (next_value, is_done)을 반환합니다.
        source_value가 None이면 (None, False)를 반환합니다.
        """
        if self.source_value is None:
            return None, False
        return calculate_next_step(
            self.source_value, self.sweep_to, self.sweep_rate, self.time_per_point
        )
