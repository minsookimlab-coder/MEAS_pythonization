"""
DataSaver: 측정 데이터를 .dat 파일로 저장합니다.
sweep 시작 시 헤더를 기록하고, 매 스텝마다 한 줄씩 append합니다.

데이터 신뢰성:
  - 매 append마다 flush + os.fsync → 강제 종료/크래시에도 기록된 행은 디스크에 남음.
  - start_session 실패 사유를 start_error()로 노출 → 호출자가 측정을 중단할 수 있음.
  - append_row는 성공 여부(bool)를 반환 → 연속 실패를 호출자가 감지 가능.
"""
import os
import re
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional, Tuple


class DataSaver:

    def __init__(self):
        self._main_folder: str = ""
        self._custom_folder: str = ""
        self._custom_word: str = ""
        self._include_date: bool = True
        self._enabled: bool = False
        self._columns: List[Tuple[str, str]] = []   # (figure_axis, unit)
        self._filepath: Optional[Path] = None
        self._error_callback: Optional[Callable[[str], None]] = None
        self._fixed_name: Optional[str] = None     # double sweep: overrides X{N:03d} suffix
        # 실패 진단: 저장이 '활성'인데 시작에 실패한 사유 (None = 정상 또는 의도적 비활성)
        self._start_error: Optional[str] = None

    def set_error_callback(self, cb: Callable[[str], None]):
        self._error_callback = cb

    def _report_error(self, msg: str):
        print(f"[DataSaver] {msg}")
        if self._error_callback:
            self._error_callback(msg)

    def is_enabled(self) -> bool:
        return self._enabled

    def start_error(self) -> Optional[str]:
        """저장이 활성화돼 있는데도 start_session이 실패한 사유. 정상이면 None."""
        return self._start_error

    # ------------------------------------------------------------------
    # Configuration setters
    # ------------------------------------------------------------------

    def set_main_folder(self, v: str):      self._main_folder = v
    def set_custom_folder(self, v: str):    self._custom_folder = v
    def set_custom_word(self, v: str):      self._custom_word = v
    def set_include_date(self, v: bool):    self._include_date = v
    def set_enabled(self, v: bool):         self._enabled = v
    def set_fixed_name(self, v: Optional[str]): self._fixed_name = v

    def set_columns(self, columns: List[Tuple[str, str]]):
        """(figure_axis, unit) 목록으로 컬럼 헤더 설정."""
        self._columns = columns

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------

    def start_session(self) -> Optional[str]:
        """
        Sweep 시작 시 호출.
        파일 경로를 확정하고 헤더를 기록합니다.

        반환:
          - 성공: 파일 경로 문자열
          - 저장 비활성화(_enabled=False): None  (start_error()도 None — 의도적)
          - 실패(폴더 없음·컬럼 없음·경로 충돌·쓰기 오류): None
            이 경우 start_error()가 사유 문자열을 반환 → 호출자가 측정을 중단해야 함.
        """
        self._filepath = None
        self._start_error = None
        if not self._enabled:
            return None   # 의도적 비활성 — start_error는 None
        if not self._main_folder.strip():
            self._start_error = "Main Folder가 지정되지 않았습니다."
            self._report_error(self._start_error)
            return None
        if not self._columns:
            self._start_error = "컬럼이 설정되지 않았습니다. Parameter Manager를 다시 적용하세요."
            self._report_error(self._start_error)
            return None
        target_dir = self._target_dir()
        conflict = self._find_path_conflict(target_dir)
        if conflict:
            self._start_error = f"경로에 파일이 존재하여 폴더를 만들 수 없습니다: {conflict}"
            self._report_error(self._start_error)
            return None
        self._filepath = self._resolve_filepath()
        try:
            self._filepath.parent.mkdir(parents=True, exist_ok=True)
            # 자동 번호 파일(_fixed_name 아님)은 'x'(배타 생성)로 연다 → 다른 프로세스가
            # 같은 XNNN을 동시에 골라 막 쓴 파일을 덮어쓰는 TOCTOU를 막는다.
            # 충돌하면 번호를 다시 산정해 재시도. fixed/resume 경로는 기존대로.
            mode = "w" if self._fixed_name else "x"
            f = None
            for _ in range(100):
                try:
                    f = open(self._filepath, mode, encoding="utf-8")
                    break
                except FileExistsError:
                    self._filepath = self._resolve_filepath()  # 다음 번호로
            if f is None:
                raise RuntimeError("자동 번호 파일명 충돌이 반복됩니다.")
            try:
                f.write("\t".join(c[0] for c in self._columns) + "\n")
                f.write("\t".join(c[1] for c in self._columns) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                f.close()
        except Exception as e:
            self._start_error = f"헤더 기록 실패: {e}"
            self._report_error(self._start_error)
            self._filepath = None
            return None
        return str(self._filepath)

    def resume_session(self, filepath: str) -> Optional[str]:
        """중단된 측정을 기존 .dat 파일에 이어쓰기 위해 세션을 복원합니다.

        헤더를 다시 쓰지 않고 파일 경로만 설정 → 이후 append_row가 이어붙입니다.
        파일이 존재하지 않으면 None 반환 (재개 불가).
        """
        if not filepath:
            return None
        p = Path(filepath)
        if not p.exists():
            self._report_error(f"재개할 데이터 파일이 없습니다: {filepath}")
            return None
        self._filepath = p
        return str(p)

    def append_row(self, values: List[str]) -> bool:
        """한 스텝의 데이터를 한 줄 append합니다.

        반환:
          True  — 정상 기록 (flush + fsync로 디스크에 내구성 보장)
          False — 저장 비활성/미시작(no-op)이거나 쓰기 실패
        """
        if not self._enabled or self._filepath is None:
            return False
        try:
            with open(self._filepath, "a", encoding="utf-8") as f:
                f.write("\t".join(values) + "\n")
                f.flush()
                os.fsync(f.fileno())   # 강제 종료/크래시에도 행 손실 방지
            return True
        except Exception as e:
            self._report_error(f"기록 실패: {e}")
            return False

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def get_filepath(self) -> Optional[Path]:
        """현재 세션의 파일 경로를 반환합니다. 세션이 없으면 None."""
        return self._filepath

    def preview_path(self) -> str:
        """현재 설정으로 생성될 파일 경로 미리보기."""
        if not self._main_folder.strip():
            return "(Main Folder 미지정)"
        try:
            return str(self._resolve_filepath())
        except Exception as e:
            return f"(오류: {e})"

    def _target_dir(self) -> Path:
        d = Path(self._main_folder.strip())
        if self._custom_folder.strip():
            d = d / self._custom_folder.strip()
        if self._include_date:
            d = d / date.today().strftime("%Y-%m-%d")
        return d

    def _stem(self) -> str:
        parts = []
        if self._custom_word.strip():
            parts.append(self._custom_word.strip())
        if self._include_date:
            parts.append(date.today().strftime("%Y%m%d"))
        return "".join(parts)

    def _next_number(self, directory: Path, stem: str) -> int:
        pattern = re.compile(
            rf"^{re.escape(stem)}X(\d+)\.dat$", re.IGNORECASE
        )
        max_n = 0
        if directory.exists():
            for f in directory.iterdir():
                m = pattern.match(f.name)
                if m:
                    max_n = max(max_n, int(m.group(1)))
        return max_n + 1

    def _resolve_filepath(self) -> Path:
        d = self._target_dir()
        if self._fixed_name:
            return d / self._fixed_name
        stem = self._stem()
        n = self._next_number(d, stem)
        return d / f"{stem}X{n:03d}.dat"

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _find_path_conflict(self, path: Path) -> Optional[Path]:
        """경로 및 상위 경로 중 파일(디렉토리 아님)이 존재하면 해당 경로 반환."""
        for p in [path, *path.parents]:
            if p.exists() and not p.is_dir():
                return p
        return None
