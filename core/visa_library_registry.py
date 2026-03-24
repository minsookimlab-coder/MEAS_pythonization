"""
VisaLibraryRegistry: visa_libraries.yaml 로드/저장/조회.
"""
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from config.config_models import InstrumentCmdLibrary, MeasurementParamDef, SweepValueDef

from core.app_dirs import SETTINGS_DIR
_DEFAULT_PATH = SETTINGS_DIR / "visa_libraries.yaml"


class VisaLibraryRegistry:

    def __init__(self, path: Optional[Path] = None):
        self._path = path or _DEFAULT_PATH
        self._data: Dict[str, InstrumentCmdLibrary] = {}
        self._load()

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def _load(self):
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for alias, block in raw.items():
            try:
                self._data[alias] = InstrumentCmdLibrary.model_validate(block)
            except Exception:
                self._data[alias] = InstrumentCmdLibrary()

    def save(self):
        out: Dict = {}
        for alias, lib in self._data.items():
            out[alias] = lib.model_dump()
        with open(self._path, "w", encoding="utf-8") as f:
            yaml.dump(out, f, allow_unicode=True, sort_keys=False)

    def reload(self):
        self._data.clear()
        self._load()

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def list_aliases(self) -> List[str]:
        return list(self._data.keys())

    def get_library(self, alias: str) -> InstrumentCmdLibrary:
        return self._data.get(alias, InstrumentCmdLibrary())

    def set_library(self, alias: str, library: InstrumentCmdLibrary):
        self._data[alias] = library

    def add_instrument(self, alias: str):
        if alias not in self._data:
            self._data[alias] = InstrumentCmdLibrary()
