"""
SecondChannelModel — double sweep의 second-channel 값 배열에 대한 스레드 안전 단일 소스.

측정 워커(다른 스레드)는 매 행 값을 여기서 '새로' 읽고(value_at), GUI 스레드는 아직 측정
안 한(PENDING) 행을 편집/추가/삭제한다. Qt를 전혀 쓰지 않으므로(순수 파이썬 + Lock)
워커 스레드에서 호출해도 안전하다. 상태 전이는 모두 하나의 Lock으로 직렬화한다.

행 상태:
  PENDING(대기) — 아직 측정 안 함. GUI에서 값 수정/추가/삭제 가능.
  CURRENT(측정중) — 워커가 지금 처리 중. 잠금.
  DONE(완료) — 측정 끝. 잠금.

주의(설계 불변식):
  - 워커는 값을 '읽기'(value_at/count)와 int 플립(mark_current/mark_done)만 한다.
  - GUI는 PENDING 행만 수정/추가/삭제한다(완료·현재 행은 거부).
  - 어떤 메서드도 Lock을 쥔 채로 Qt를 만지거나 시그널을 emit하지 않는다.
  - 완료(DONE) 행은 항상 앞쪽 prefix에 모여 있고 절대 이동/삭제되지 않는다
    → 미래 행을 추가/삭제해도 워커의 현재 위치(CURRENT) 이후만 바뀌므로 안전하다.
"""
import threading
from typing import List, Optional, Tuple

PENDING, CURRENT, DONE = 0, 1, 2


class SecondChannelModel:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._vals: List[float] = []
        self._states: List[int] = []
        self._current_idx: int = -1

    # ---- 워커 스레드에서 호출 (Lock 보호, 순수 파이썬 반환) ----------------
    def value_at(self, idx: int) -> Tuple[Optional[float], bool]:
        """idx 위치의 값을 반환. 범위 밖이면 (None, False).

        절대 BlockingQueuedConnection으로 대체하지 말 것 — shutdown_threads의
        thread.wait()와 데드락난다. 이 동기 읽기가 그 자리를 대신한다.
        """
        with self._lock:
            if 0 <= idx < len(self._vals):
                return float(self._vals[idx]), True
            return None, False

    def count(self) -> int:
        with self._lock:
            return len(self._vals)

    def mark_current(self, idx: int) -> None:
        """이전 CURRENT를 DONE으로, idx를 CURRENT로."""
        with self._lock:
            c = self._current_idx
            if 0 <= c < len(self._states) and self._states[c] == CURRENT:
                self._states[c] = DONE
            if 0 <= idx < len(self._states):
                self._states[idx] = CURRENT
                self._current_idx = idx

    def mark_done(self, idx: int) -> None:
        with self._lock:
            if 0 <= idx < len(self._states):
                self._states[idx] = DONE
            if self._current_idx == idx:
                self._current_idx = -1

    def clear_running(self) -> None:
        """정지/완료 시: CURRENT로 멈춘 행을 PENDING으로 되돌린다."""
        with self._lock:
            c = self._current_idx
            if 0 <= c < len(self._states) and self._states[c] == CURRENT:
                self._states[c] = PENDING
            self._current_idx = -1

    # ---- GUI 스레드에서 호출 -------------------------------------------
    def reset_from(self, vals: List[float], done_prefix: int = 0) -> None:
        """값을 교체. [0..done_prefix-1]은 DONE(재개용), 나머지는 PENDING."""
        with self._lock:
            self._vals = [float(v) for v in vals]
            self._states = [DONE if i < done_prefix else PENDING
                            for i in range(len(self._vals))]
            self._current_idx = -1

    def rearm(self) -> None:
        """커스텀 테이블 재실행: 값은 유지, 모든 상태를 PENDING으로."""
        with self._lock:
            self._states = [PENDING] * len(self._vals)
            self._current_idx = -1

    def set_value(self, idx: int, v: float) -> bool:
        """PENDING 행만 값 수정. 성공 시 True."""
        with self._lock:
            if 0 <= idx < len(self._vals) and self._states[idx] == PENDING:
                self._vals[idx] = float(v)
                return True
            return False

    def append(self, v: float) -> None:
        """맨 뒤에 PENDING 행 추가 (측정 중에도 안전 — 워커가 count를 새로 읽음)."""
        with self._lock:
            self._vals.append(float(v))
            self._states.append(PENDING)

    def remove(self, idx: int) -> bool:
        """PENDING 행만 삭제. 완료·현재 행은 거부. 성공 시 True."""
        with self._lock:
            if 0 <= idx < len(self._vals) and self._states[idx] == PENDING:
                del self._vals[idx]
                del self._states[idx]
                if self._current_idx > idx:
                    self._current_idx -= 1
                return True
            return False

    def snapshot(self) -> Tuple[List[float], List[int], int]:
        """(values, states, current_idx) 복사본 — 테이블 렌더링용."""
        with self._lock:
            return list(self._vals), list(self._states), self._current_idx

    def values(self) -> List[float]:
        with self._lock:
            return list(self._vals)
