"""
Config 창 — 전역 앱 설정 (AppConfig) 편집.
Settings → Config... 로 열림.
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from config.app_config import AppConfig, save_app_config
from core.app_dirs import DEFAULT_DATA_DIR, GLOBAL_CONFIG_PATH


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

        # 병렬 측정 토글
        self._cb_parallel = QCheckBox("병렬 측정 (서로 다른 계측기 동시 측정)")
        self._cb_parallel.setToolTip(
            "여러 계측기를 측정할 때 각 계측기를 동시에 쿼리해 측정 시간을 줄입니다.\n"
            "서로 다른 계측기만 병렬화되며, 같은 계측기는 안전하게 순차 처리됩니다.\n"
            "VISA 백엔드 동시성 문제가 의심되면 끄고 사용하세요 (기존처럼 순차 측정)."
        )
        form.addRow("", self._cb_parallel)

        root.addLayout(form)

        # ── 사용자 데이터 폴더 ──────────────────────────────
        dd_lbl = QLabel("데이터 폴더 (프로파일·계측기·VNA·resume 저장 위치)")
        dd_lbl.setStyleSheet("color: #79c0ff; font-weight: bold;")
        root.addWidget(dd_lbl)
        dd_row = QHBoxLayout()
        self._le_data_dir = QLineEdit()
        self._le_data_dir.setMinimumWidth(280)
        self._le_data_dir.setPlaceholderText(str(DEFAULT_DATA_DIR))
        self._le_data_dir.setToolTip(
            "개인 정보가 담길 수 있는 사용자 데이터 파일의 저장 폴더입니다.\n"
            "비워두면 기본값(내 문서)을 사용합니다.\n"
            "변경은 프로그램을 재시작해야 적용됩니다.\n"
            f"전역 설정은 프로그램 폴더에 저장됩니다:\n{GLOBAL_CONFIG_PATH}"
        )
        dd_row.addWidget(self._le_data_dir, stretch=1)
        btn_browse = QPushButton("…"); btn_browse.setFixedWidth(32)
        btn_browse.clicked.connect(self._browse_data_dir)
        dd_row.addWidget(btn_browse)
        root.addLayout(dd_row)
        dd_hint = QLabel("※ 변경 후 재시작해야 적용됩니다. 기존 데이터는 자동 이동되지 않으니 필요 시 직접 복사하세요.")
        dd_hint.setStyleSheet("color: #888; font-size: 10px;")
        dd_hint.setWordWrap(True)
        root.addWidget(dd_hint)

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
        self._cb_parallel.setChecked(self._cfg.parallel_measurement)
        self._le_data_dir.setText(self._cfg.data_dir)
        self._lbl_threshold_err.setText("")

    def _browse_data_dir(self):
        start = self._le_data_dir.text().strip() or str(DEFAULT_DATA_DIR)
        folder = QFileDialog.getExistingDirectory(self, "데이터 폴더 선택", start)
        if folder:
            self._le_data_dir.setText(folder)

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
        return AppConfig(
            global_threshold=val,
            parallel_measurement=self._cb_parallel.isChecked(),
            data_dir=self._le_data_dir.text().strip(),
        )

    def _apply_common(self) -> "AppConfig | None":
        cfg = self._collect()
        if cfg is None:
            return None
        data_dir_changed = (cfg.data_dir != self._cfg.data_dir)
        self._cfg = cfg
        save_app_config(self._cfg)
        self.apply_requested.emit(self._cfg)
        if data_dir_changed:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "데이터 폴더 변경",
                "데이터 폴더 변경은 프로그램을 재시작해야 적용됩니다.\n"
                "기존 데이터는 자동으로 이동되지 않으니, 필요하면 이전 폴더의 파일을 "
                "새 폴더로 직접 복사하세요.",
            )
        return cfg

    def _on_apply(self):
        self._apply_common()

    def _on_ok(self):
        if self._apply_common() is not None:
            self.accept()
