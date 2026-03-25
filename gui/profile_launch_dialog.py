"""
ProfileLaunchDialog: 앱 시작 시 프로파일 선택 창.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton,
    QInputDialog, QMessageBox,
)

from core.profile_registry import ProfileRegistry

_MONO = QFont("Consolas", 10)


class ProfileLaunchDialog(QDialog):
    """앱 시작 전 프로파일 선택 / 관리 창."""

    def __init__(self, registry: ProfileRegistry, parent=None):
        super().__init__(parent)
        self._reg = registry
        self.setWindowTitle("Profile Selection")
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.setFixedWidth(340)
        self._build()
        self._refresh()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)

        lay.addWidget(QLabel("프로파일을 선택하세요:"))

        self._lst = QListWidget()
        self._lst.setFont(_MONO)
        self._lst.setMinimumHeight(180)
        self._lst.itemDoubleClicked.connect(self._on_open)
        self._lst.currentRowChanged.connect(self._update_buttons)
        lay.addWidget(self._lst)

        # CRUD buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._btn_add    = QPushButton("Add")
        self._btn_copy   = QPushButton("Copy")
        self._btn_rename = QPushButton("Rename")
        self._btn_delete = QPushButton("Delete")
        for btn in (self._btn_add, self._btn_copy,
                    self._btn_rename, self._btn_delete):
            btn.setFixedHeight(26)
            btn_row.addWidget(btn)
        lay.addLayout(btn_row)

        self._btn_add.clicked.connect(self._on_add)
        self._btn_copy.clicked.connect(self._on_copy)
        self._btn_rename.clicked.connect(self._on_rename)
        self._btn_delete.clicked.connect(self._on_delete)

        # Confirm / Exit
        confirm_row = QHBoxLayout()
        confirm_row.addStretch()
        self._btn_open = QPushButton("Open")
        self._btn_open.setFixedSize(80, 30)
        self._btn_open.setDefault(True)
        self._btn_open.setStyleSheet(
            "QPushButton{background:#1f4e8c;color:white;"
            "border-radius:4px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        btn_exit = QPushButton("Exit")
        btn_exit.setFixedSize(60, 30)
        confirm_row.addWidget(self._btn_open)
        confirm_row.addWidget(btn_exit)
        lay.addLayout(confirm_row)

        self._btn_open.clicked.connect(self._on_open)
        btn_exit.clicked.connect(self.reject)

    # ------------------------------------------------------------------
    # List helpers
    # ------------------------------------------------------------------

    def _refresh(self, select_name: str = ""):
        self._lst.blockSignals(True)
        self._lst.clear()
        profiles = self._reg.list_profiles()
        active = select_name or self._reg.active_name
        select_row = 0
        for i, name in enumerate(profiles):
            item = QListWidgetItem(name)
            self._lst.addItem(item)
            if name == active:
                select_row = i
        self._lst.blockSignals(False)
        self._lst.setCurrentRow(select_row)
        self._update_buttons()

    def _selected_name(self) -> str:
        item = self._lst.currentItem()
        return item.text() if item else ""

    def _update_buttons(self):
        has = bool(self._selected_name())
        self._btn_open.setEnabled(has)
        self._btn_copy.setEnabled(has)
        self._btn_rename.setEnabled(has)
        self._btn_delete.setEnabled(
            has and len(self._reg.list_profiles()) > 1)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_add(self):
        name, ok = QInputDialog.getText(self, "Add Profile", "새 프로파일 이름:")
        if not ok or not name.strip():
            return
        actual = self._reg.add_profile(name.strip())
        self._refresh(select_name=actual)

    def _on_copy(self):
        src = self._selected_name()
        if not src:
            return
        new_name = self._reg.duplicate_profile(src)
        self._refresh(select_name=new_name)

    def _on_rename(self):
        old = self._selected_name()
        if not old:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename Profile", "새 이름:", text=old)
        if not ok or not new_name.strip() or new_name.strip() == old:
            return
        actual = self._reg.rename_profile(old, new_name.strip())
        self._refresh(select_name=actual)

    def _on_delete(self):
        name = self._selected_name()
        if not name:
            return
        reply = QMessageBox.question(
            self, "프로파일 삭제",
            f"'{name}' 프로파일을 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._reg.delete_profile(name)
        except ValueError as e:
            QMessageBox.warning(self, "삭제 불가", str(e))
            return
        self._refresh()

    def _on_open(self):
        name = self._selected_name()
        if not name:
            return
        self._reg.set_active(name)
        self.accept()
