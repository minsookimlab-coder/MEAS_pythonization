"""
DataSaver: 측정 데이터를 .dat 파일로 저장합니다.
sweep 시작 시 파일 경로를 확정하고, 매 스텝마다 전체 파일을 overwrite합니다.
"""
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
        self._rows: List[List[str]] = []
        self._filepath: Optional[Path] = None
        self._error_callback: Optional[Callable[[str], None]] = None

    def set_error_callback(self, cb: Callable[[str], None]):
        self._error_callback = cb

    def _report_error(self, msg: str):
        print(f"[DataSaver] {msg}")
        if self._error_callback:
            self._error_callback(msg)

    # ------------------------------------------------------------------
    # Configuration setters
    # ------------------------------------------------------------------

    def set_main_folder(self, v: str):    self._main_folder = v
    def set_custom_folder(self, v: str):  self._custom_folder = v
    def set_custom_word(self, v: str):    self._custom_word = v
    def set_include_date(self, v: bool):  self._include_date = v
    def set_enabled(self, v: bool):       self._enabled = v

    def set_columns(self, columns: List[Tuple[str, str]]):
        """(figure_axis, unit) 목록으로 컬럼 헤더 설정."""
        self._columns = columns

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------

    def start_session(self) -> Optional[str]:
        """
        Sweep 시작 시 호출.
        파일 경로를 확정하고 내부 버퍼를 초기화합니다.
        Auto-save가 비활성화되거나 Main Folder가 없으면 None 반환.
        """
        self._rows.clear()
        self._filepath = None
        if not self._enabled or not self._main_folder.strip():
            return None
        if not self._columns:
            self._report_error("columns이 설정되지 않았습니다. Parameter Manager를 다시 적용하세요.")
            return None
        target_dir = self._target_dir()
        conflict = self._find_path_conflict(target_dir)
        if conflict:
            self._report_error(f"경로에 파일이 존재하여 폴더를 만들 수 없습니다: {conflict}")
            return None
        self._filepath = self._resolve_filepath()
        return str(self._filepath)

    def append_row(self, values: List[str]) -> None:
        """한 스텝의 데이터를 추가하고 파일을 전체 overwrite합니다."""
        if not self._enabled or self._filepath is None:
            return
        self._rows.append(values)
        self._write_file()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

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

    def _write_file(self) -> None:
        try:
            self._filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self._filepath, "w", encoding="utf-8") as f:
                f.write("\t".join(c[0] for c in self._columns) + "\n")
                f.write("\t".join(c[1] for c in self._columns) + "\n")
                for row in self._rows:
                    f.write("\t".join(row) + "\n")
        except Exception as e:
            self._report_error(f"Write failed: {e}")
