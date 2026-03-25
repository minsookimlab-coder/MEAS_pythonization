"""
Config 창 — 전역 앱 설정 (AppConfig) 편집.
Settings → Config... 로 열림.
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from config.app_config import AppConfig, save_app_config


class ConfigWindow(QDialog):
    """전역 앱 설정 편집 창.

    apply_requested(AppConfig) — Apply/OK 클릭 시 emit.
    """

    apply_requested = Signal(object)   # AppConfig

    def __init__(self, app_config: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Config")
        self.setMinimumWidth(360)
        self._cfg = app_config.model_copy()
        self._build()
        self._load_to_ui()

    # ------------------------------------------------------------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 12)

        form = QFormLayout()
        form.setSpacing(8)

        # Global threshold — QLineEdit로 지수 표기 입력 지원 (예: 1e37)
        lbl = QLabel("Global Threshold")
        lbl.setToolTip(
            "측정값의 절댓값이 이 임계값보다 크면 nan으로 처리합니다.\n"
            "지수 표기 가능: 예) 1e37, 9.99e38\n"
            "그래프/파일에서 해당 지점은 공백(nan)으로 표시됩니다."
        )
        self._le_threshold = QLineEdit()
        self._le_threshold.setMinimumWidth(160)
        self._le_threshold.setPlaceholderText("예: 1e37")
        self._lbl_threshold_err = QLabel("")
        self._lbl_threshold_err.setStyleSheet("color: #f44747; font-size: 10px;")

        form.addRow(lbl, self._le_threshold)
        form.addRow("", self._lbl_threshold_err)

        root.addLayout(form)
        root.addStretch()

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_apply = QPushButton("Apply")
        btn_apply.setFixedWidth(80)
        btn_apply.clicked.connect(self._on_apply)
        btn_ok = QPushButton("OK")
        btn_ok.setFixedWidth(80)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setFixedWidth(80)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_apply)
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        root.addLayout(btn_row)

    def _load_to_ui(self):
        self._le_threshold.setText(f"{self._cfg.global_threshold:g}")
        self._lbl_threshold_err.setText("")

    def _parse_threshold(self) -> "float | None":
        """텍스트 → float 변환. 실패 시 None."""
        try:
            val = float(self._le_threshold.text().strip())
            if val <= 0:
                return None
            return val
        except ValueError:
            return None

    def _collect(self) -> "AppConfig | None":
        val = self._parse_threshold()
        if val is None:
            self._lbl_threshold_err.setText("유효한 양수를 입력하세요 (예: 1e37)")
            return None
        self._lbl_threshold_err.setText("")
        return AppConfig(global_threshold=val)

    def _on_apply(self):
        cfg = self._collect()
        if cfg is None:
            return
        self._cfg = cfg
        save_app_config(self._cfg)
        self.apply_requested.emit(self._cfg)

    def _on_ok(self):
        cfg = self._collect()
        if cfg is None:
            return
        self._cfg = cfg
        save_app_config(self._cfg)
        self.apply_requested.emit(self._cfg)
        self.accept()
