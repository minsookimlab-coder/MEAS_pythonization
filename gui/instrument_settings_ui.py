import yaml
import pkgutil
import importlib
import inspect
from typing import Dict, List

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QSplitter, QListWidget, QLineEdit, QComboBox, QSpinBox, 
    QPushButton, QLabel, QMessageBox, QFrame, QScrollArea, QListWidgetItem, QAbstractItemView
)
from PySide6.QtCore import Qt

from config.config_models import InstrumentConfig, cast_extra_params
import driver
from core.instrument_base import BaseInstrument
from core.network_utils import find_mac_for_ip

# --- Exe 호환 경로 설정 처리 ---
from core.app_dirs import SETTINGS_DIR
SETTINGS_FILE = SETTINGS_DIR / "instruments.yaml"

class InstrumentSettingsUI(QMainWindow):
    """
    장비 설정 관리 메인 GUI (Instrument Settings Manager)
    
    이 클래스는 사용자가 측정 장비(예: SourceMeter, Multimeter 등)의 
    통신 설정(IP 주소, 통신 포트, VISA 이름, 파라미터 등)을 등록하고 
    YAML 파일(settings/instruments.yaml)로 저장/불러오기 하는 역할을 수행합니다.
    (모든 로직이 이 창 안에서 독립적으로 동작합니다.)
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Instrument Settings Manager")
        self.resize(800, 600)
        
        # Data storage
        self.instruments_data: Dict[str, dict] = {} # Key: alias, Value: config dict
        
        # State tracking for dynamic rows
        self.dynamic_variable_rows: List[tuple[QLineEdit, QLineEdit, QWidget]] = []
        self._current_editing_item = None
        
        # Initialize UI components
        self._setup_ui()
        self._load_settings()

    def _setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Creating a QSplitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)
        
        # --- Left Panel: Instrument List ---
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        
        self.instrument_list = QListWidget()
        self.instrument_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.instrument_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        left_layout.addWidget(QLabel("Instruments:"))
        left_layout.addWidget(self.instrument_list)
        
        btn_layout = QHBoxLayout()
        self.btn_add_instrument = QPushButton("+ Add")
        self.btn_add_instrument.clicked.connect(self._add_new_instrument)
        self.btn_remove_instrument = QPushButton("- Remove")
        self.btn_remove_instrument.clicked.connect(self._remove_instrument)
        
        btn_layout.addWidget(self.btn_add_instrument)
        btn_layout.addWidget(self.btn_remove_instrument)
        left_layout.addLayout(btn_layout)
        
        # --- Right Panel: Configuration Form ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        
        self.lbl_current_setting = QLabel("현재 편집 중인 장비: 없음 (목록에서 더블클릭)")
        self.lbl_current_setting.setStyleSheet("font-size: 16px; font-weight: bold; color: #2B5B84; margin-bottom: 5px;")
        right_layout.addWidget(self.lbl_current_setting)
        
        # Form Layout holding static attributes
        self.form_layout = QFormLayout()
        
        self.le_alias = QLineEdit()
        self.le_alias.textChanged.connect(self._sync_alias_to_list)
        self.form_layout.addRow("Alias:", self.le_alias)
        
        self.cb_driver = QComboBox()
        self.AVAILABLE_DRIVERS = self._discover_drivers()
        
        for name, cls_path in self.AVAILABLE_DRIVERS.items():
            self.cb_driver.addItem(name, cls_path)
        self.cb_driver.setEditable(True)
        self.lbl_driver = QLabel("Device Type (Driver):")
        self.form_layout.addRow(self.lbl_driver, self.cb_driver)
        
        self.cb_interface = QComboBox()
        self.cb_interface.addItems(["LAN", "GPIB", "RS232", "USB"])
        self.cb_interface.currentTextChanged.connect(self._on_interface_changed)
        self.form_layout.addRow("Interface Type:", self.cb_interface)
        
        self.le_address = QLineEdit()
        self.le_address.textChanged.connect(self._update_visa_preview)
        self.form_layout.addRow("Address (IP):", self.le_address)
        
        self.le_mac_address = QLineEdit()
        self.le_mac_address.setPlaceholderText("e.g. 5C-16-C7-00-00-00 (Auto-filled)")
        mac_layout = QHBoxLayout()
        mac_layout.addWidget(self.le_mac_address)
        self.btn_fetch_mac = QPushButton("Fetch MAC")
        self.btn_fetch_mac.setMaximumWidth(80)
        self.btn_fetch_mac.clicked.connect(self._auto_fetch_mac)
        mac_layout.addWidget(self.btn_fetch_mac)
        self.form_layout.addRow("MAC Address:", mac_layout)
        
        self.lbl_address_hint = QLabel("")
        self.lbl_address_hint.setStyleSheet("color: #777777; font-size: 11px;")
        self.lbl_address_hint.setWordWrap(True)
        self.form_layout.addRow("", self.lbl_address_hint)
        
        self.sb_port = QSpinBox()
        self.sb_port.setRange(0, 65535)
        self.sb_port.setSpecialValueText("None")
        self.sb_port.setValue(0)
        self.sb_port.valueChanged.connect(self._update_visa_preview)
        self.form_layout.addRow("Port (LAN only):", self.sb_port)
        
        self.lbl_visa_address = QLabel("VISA: (입력 대기중)")
        self.lbl_visa_address.setStyleSheet("color: #00AA00; font-weight: bold; margin-top: 5px;")
        self.form_layout.addRow("Real Resource:", self.lbl_visa_address)
        
        # Initialize the hint UI state
        self._on_interface_changed("LAN")
        
        right_layout.addLayout(self.form_layout)
        
        # Dynamic Variables Section
        right_layout.addWidget(self._create_separator())
        right_layout.addWidget(QLabel("<b>Dynamic Variables</b> (extra_params)"))
        
        # Scroll area for dynamic variables
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        self.variables_container = QWidget()
        self.variables_layout = QVBoxLayout(self.variables_container)
        self.variables_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll_area.setWidget(self.variables_container)
        right_layout.addWidget(scroll_area)
        
        self.btn_add_variable = QPushButton("+ Add Variable")
        self.btn_add_variable.clicked.connect(lambda: self._add_variable_row())
        right_layout.addWidget(self.btn_add_variable)
        
        # Action Buttons
        right_layout.addWidget(self._create_separator())
        action_layout = QHBoxLayout()
        
        self.btn_test = QPushButton("Test Connection")
        self.btn_test.setMinimumHeight(40)
        self.btn_test.setStyleSheet("font-weight: bold; font-size: 14px; color: #2B5B84;")
        self.btn_test.clicked.connect(self._test_connection)
        
        self.btn_save = QPushButton("Save & Apply")
        self.btn_save.setMinimumHeight(40)
        self.btn_save.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.btn_save.clicked.connect(self._save_settings)
        
        action_layout.addWidget(self.btn_test)
        action_layout.addWidget(self.btn_save)
        right_layout.addLayout(action_layout)
        
        # Add panels to splitter
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([250, 550])
        
    def _discover_drivers(self) -> Dict[str, str]:
        """
        [장비 드라이버 자동 검색 함수]
        'core' 폴더 내부에 정의된 모든 파이썬 스크립트를 탐색하여,
        BaseInstrument를 상속받은(subclass) 실제 장비 드라이버 클래스들을 동적으로 찾아냅니다.
        찾아낸 클래스들은 콤보박스(cb_driver)에 이름으로 등록되어 인스턴스화 될 때 사용됩니다.
        """
        drivers = {}
        # Iterate over all modules defined in the 'driver' package folder
        for _, module_name, _ in pkgutil.iter_modules(driver.__path__):
            try:
                # Dynamically import module
                module = importlib.import_module(f"driver.{module_name}")
                # Inspect all objects within the module to find BaseInstrument subclasses
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    # Check if it's a genuine subclass and not BaseInstrument itself
                    if issubclass(obj, BaseInstrument) and obj is not BaseInstrument:
                        # Extract the first line of the docstring as a short description, if available
                        doc = obj.__doc__.strip().split('\n')[0].strip() if obj.__doc__ else name

                        # Generate a readable display name and a fully qualified package path
                        display_name = f"{name} ({doc})" if obj.__doc__ else f"{name} ({module_name})"
                        class_path = f"driver.{module_name}.{name}"
                        drivers[display_name] = class_path
            except Exception as e:
                # Silently ignore broken modules during auto-discovery
                print(f"Failed to load module 'driver.{module_name}': {e}")
                pass
        
        # Sort alphabetically for convenience
        return dict(sorted(drivers.items()))

    def _create_separator(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _on_interface_changed(self, interface: str):
        """Updates placeholder and hint for address based on interface type."""
        if interface == "LAN":
            self.le_address.setPlaceholderText("e.g. 192.168.0.2")
            self.lbl_address_hint.setText("💡 <b>예시:</b> 192.168.0.2 (일반적인 IP 입력)")
            self.sb_port.setEnabled(True)
            self.le_mac_address.setEnabled(True)
            self.btn_fetch_mac.setEnabled(True)
        elif interface == "GPIB":
            self.le_address.setPlaceholderText("e.g. 10")
            self.lbl_address_hint.setText("💡 <b>예시:</b> 10 (내부에서 GPIB0::10::INSTR 로 처리됨)")
            self.sb_port.setEnabled(False)
            self.sb_port.setValue(0)
            self.le_mac_address.setEnabled(False)
            self.le_mac_address.clear()
            self.btn_fetch_mac.setEnabled(False)
        elif interface == "RS232":
            self.le_address.setPlaceholderText("e.g. COM3")
            self.lbl_address_hint.setText("💡 <b>예시:</b> COM3 (시리얼 포트 이름)")
            self.sb_port.setEnabled(False)
            self.sb_port.setValue(0)
            self.le_mac_address.setEnabled(False)
            self.le_mac_address.clear()
            self.btn_fetch_mac.setEnabled(False)
        elif interface == "USB":
            self.le_address.setPlaceholderText("e.g. USB0::0...::INSTR")
            self.lbl_address_hint.setText("💡 <b>예시:</b> USB0::0x1111::0x2222::INSTR (VISA 식별자 전체 기입)")
            self.sb_port.setEnabled(False)
            self.sb_port.setValue(0)
            self.le_mac_address.setEnabled(False)
            self.le_mac_address.clear()
            self.btn_fetch_mac.setEnabled(False)
            
        self._update_visa_preview()

    def _update_visa_preview(self, *_):
        """Calculates and updates the actual VISA resource string preview."""
        interface = self.cb_interface.currentText()
        address = self.le_address.text().strip()
        port = self.sb_port.value()
        
        if not address:
            self.lbl_visa_address.setText("VISA: (입력 대기중)")
            self.lbl_visa_address.setStyleSheet("color: #777777; font-weight: bold; margin-top: 5px;")
            return
            
        resource_str = ""
        if interface == "LAN":
            if port and port != 0:
                resource_str = f"TCPIP0::{address}::{port}::SOCKET"
            else:
                resource_str = f"TCPIP0::{address}::inst0::INSTR"
        elif interface == "GPIB":
            resource_str = f"GPIB0::{address}::INSTR"
        elif interface == "RS232" or interface == "USB":
            resource_str = address
            
        self.lbl_visa_address.setText(f"VISA: {resource_str}")
        self.lbl_visa_address.setStyleSheet("color: #009900; font-weight: bold; margin-top: 5px;")

    def _clear_form(self):
        """Clears all input fields in the right panel."""
        self.le_alias.blockSignals(True)
        self.le_alias.clear()
        self.le_alias.blockSignals(False)
        self.cb_driver.setCurrentIndex(0)
        self.cb_interface.setCurrentIndex(0)
        self.le_address.clear()
        self.le_mac_address.clear()
        self.sb_port.setValue(0)
        self._clear_dynamic_variables()

    def _clear_dynamic_variables(self):
        """Removes all dynamic variable rows."""
        for _, _, row_widget in self.dynamic_variable_rows:
            self.variables_layout.removeWidget(row_widget)
            row_widget.deleteLater()
        self.dynamic_variable_rows.clear()

    def _add_variable_row(self, key_text="", value_text=""):
        """Adds a single row for a dynamic variable."""
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        
        le_key = QLineEdit(key_text)
        le_key.setPlaceholderText("Key (e.g. baud_rate)")
        le_value = QLineEdit(str(value_text))
        le_value.setPlaceholderText("Value (e.g. 9600)")
        
        btn_delete = QPushButton("Delete")
        btn_delete.clicked.connect(lambda: self._remove_variable_row(row_widget))
        
        row_layout.addWidget(le_key)
        row_layout.addWidget(le_value)
        row_layout.addWidget(btn_delete)
        
        self.variables_layout.addWidget(row_widget)
        self.dynamic_variable_rows.append((le_key, le_value, row_widget))

    def _remove_variable_row(self, row_widget: QWidget):
        """Removes a specific dynamic variable row."""
        for i, (le_k, le_v, w) in enumerate(self.dynamic_variable_rows):
            if w == row_widget:
                self.variables_layout.removeWidget(w)
                w.deleteLater()
                self.dynamic_variable_rows.pop(i)
                break

    def _auto_fetch_mac(self):
        """Fetches the MAC address from the system ARP table based on the entered IP address."""
        address = self.le_address.text().strip()
        if not address or self.cb_interface.currentText() != "LAN":
            return
            
        mac = find_mac_for_ip(address)
        if mac:
            self.le_mac_address.setText(mac)
            self._save_settings(silent=True)
            QMessageBox.information(self, "MAC Fetched", f"Successfully found MAC Address: {mac}")
        else:
            QMessageBox.warning(self, "MAC Fetch Failed", f"Could not find MAC address for IP {address} in ARP table.\nPlease ensure the device is turned on and connected to the network.")

    def _on_item_double_clicked(self, item):
        """Fills the form when an instrument is double-clicked from the list."""
        self._store_current_form()  # Save previously editing item before loading new one
        
        self._current_editing_item = item
        alias_key = item.data(Qt.ItemDataRole.UserRole)
        data = self.instruments_data.get(alias_key, {})
        
        self.lbl_current_setting.setText(f"현재 편집 중인 장비: {alias_key}")
        
        self._clear_form()
        
        # Block signals to prevent item list text changing prematurely
        self.le_alias.blockSignals(True)
        self.le_alias.setText(data.get("alias", ""))
        self.le_alias.blockSignals(False)
        
        class_name = data.get("class_name", "")
        idx = -1
        for i in range(self.cb_driver.count()):
            if self.cb_driver.itemData(i) == class_name:
                idx = i
                break
                
        if idx >= 0:
            self.cb_driver.setCurrentIndex(idx)
        else:
            self.cb_driver.setCurrentText(class_name)
        
        interface = data.get("interface_type", "LAN")
        idx = self.cb_interface.findText(interface)
        if idx >= 0:
            self.cb_interface.setCurrentIndex(idx)
            
        self.le_address.setText(data.get("address", ""))
        self.le_mac_address.setText(data.get("mac_address", ""))
        
        port = data.get("port")
        if port is not None:
            self.sb_port.setValue(port)
        else:
            self.sb_port.setValue(0)
            
        extra_params = data.get("extra_params", {})
        for k, v in extra_params.items():
            self._add_variable_row(k, v)

    def _sync_alias_to_list(self, new_text):
        """Updates the list item text when the alias line edit changes."""
        selected_items = self.instrument_list.selectedItems()
        if selected_items:
            selected_items[0].setText(new_text if new_text else "<Unnamed>")

    def _store_current_form(self, skip_save=False):
        """
        [현재 입력 폼의 데이터를 메모리 딕셔너리에 임시 저장하는 함수]
        사용자가 오른쪽 패널에서 IP 주소나 장비 이름을 수정했을 때, 
        그 값들을 읽어와 self.instruments_data 딕셔너리에 업데이트합니다.
        목록에서 다른 장비를 클릭하거나 새 장비를 추가하기 직전에 항상 호출됩니다.
        """
        if not self._current_editing_item:
            return
            
        item = self._current_editing_item
        original_alias = item.data(Qt.ItemDataRole.UserRole)
        current_alias = self.le_alias.text().strip()
        
        # Prevent completely empty tracking keys
        if not current_alias:
            current_alias = original_alias 
            
        extra_params = {}
        for le_k, le_v, _ in self.dynamic_variable_rows:
            k = le_k.text().strip()
            v = le_v.text().strip()
            if k:
                extra_params[k] = v
                
        driver_text = self.cb_driver.currentText()
        idx = self.cb_driver.findText(driver_text)
        resolved_class_name = self.cb_driver.itemData(idx) if idx >= 0 else driver_text.strip()
        
        form_data = {
            "alias": current_alias,
            "class_name": resolved_class_name,
            "interface_type": self.cb_interface.currentText(),
            "address": self.le_address.text().strip(),
            "mac_address": self.le_mac_address.text().strip(),
            "port": self.sb_port.value() if self.sb_port.value() != 0 else None,
            "extra_params": extra_params
        }
        
        # Handle renaming alias keys safely
        if current_alias != original_alias:
            self.instruments_data[current_alias] = form_data
            if original_alias in self.instruments_data:
                del self.instruments_data[original_alias]
            item.setData(Qt.ItemDataRole.UserRole, current_alias)
            self.lbl_current_setting.setText(f"현재 편집 중인 장비: {current_alias}")
        else:
            self.instruments_data[original_alias] = form_data
            
        if not skip_save:
            self._save_settings(silent=True)

    def _add_new_instrument(self):
        """Adds a new blank instrument configuration."""
        self._store_current_form()
        
        new_alias = f"New_Instrument_{len(self.instruments_data) + 1}"
        self.instruments_data[new_alias] = {
            "alias": new_alias,
            "class_name": "driver.m81.M81Instrument", # Default back to valid M81 class
            "interface_type": "LAN",
            "address": "192.168.0.1",
            "mac_address": "",
            "port": None,
            "extra_params": {}
        }
        
        item = QListWidgetItem(new_alias)
        item.setData(Qt.ItemDataRole.UserRole, new_alias)
        self.instrument_list.addItem(item)
        
        # automatically edit the newly created item
        self._on_item_double_clicked(item)

    def _remove_instrument(self):
        """Removes the selected instrument configuration."""
        selected_items = self.instrument_list.selectedItems()
        if not selected_items:
            return
            
        item = selected_items[0]
        alias_key = item.data(Qt.ItemDataRole.UserRole)
        
        if self._current_editing_item == item:
            self._current_editing_item = None
            self.lbl_current_setting.setText("현재 편집 중인 장비: 없음 (목록에서 더블클릭)")
            self._clear_form()
        
        if alias_key in self.instruments_data:
            del self.instruments_data[alias_key]
            
        self.instrument_list.takeItem(self.instrument_list.row(item))
        self._save_settings(silent=True)

    def _load_settings(self):
        """
        [YAML 설정 파일 불러오기]
        프로그램 시작 시 settings/instruments.yaml 파일이 존재하면 내용을 읽어옵니다.
        Pydantic의 InstrumentConfig 모델을 사용하여 데이터 유효성을 검사한 뒤, 
        왼쪽 ListWidget과 내부 instruments_data 딕셔너리에 복원합니다.
        """
        if not SETTINGS_FILE.exists():
            return
            
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                
            if isinstance(data, list):
                self.instrument_list.clear()
                self.instruments_data.clear()
                
                for item_dict in data:
                    try:
                        # Validate using Pydantic model
                        valid_config = InstrumentConfig(**item_dict)
                        alias = valid_config.alias
                        self.instruments_data[alias] = valid_config.model_dump()
                        
                        list_item = QListWidgetItem(alias)
                        list_item.setData(Qt.ItemDataRole.UserRole, alias)
                        self.instrument_list.addItem(list_item)
                    except Exception as e:
                        print(f"Validation failed for loaded item: {e}")
                        
        except Exception as e:
            QMessageBox.warning(self, "Load Error", f"Failed to load settings file:\n{e}")
            return
            
        # --- 기존 장비 MAC 자동 등록 & 자동 저장 로직 ---
        needs_save = False
        for alias, data in self.instruments_data.items():
            if data.get("interface_type") == "LAN" and not data.get("mac_address"):
                ip = data.get("address", "")
                if ip.count('.') == 3 and all(p.isdigit() for p in ip.split('.')):
                    mac = find_mac_for_ip(ip)
                    if mac:
                        data["mac_address"] = mac
                        needs_save = True
                        print(f"[GUI Auto-Upgrade] Fetched MAC {mac} for {alias} ({ip})")
            
        if needs_save:
            self._save_settings(silent=True)

    def _save_settings(self, silent=False):
        """
        [YAML 설정 파일 영구 저장]
        메모리에 저장된 self.instruments_data 전체를 Pydantic 모델로 다시 검증(Validation)한 뒤,
        에러가 없다면 YAML 파일 형식으로 변환하여 instruments.yaml에 덮어씁니다.
        이 파일이 나중에 Factory에서 장비를 실제로 제어할 때 핵심 설정 파일로 사용됩니다.
        """
        if not silent:
            # We skip internal save to prevent loop when triggered manually
            self._store_current_form(skip_save=True)
        
        valid_configs = []
        errors = []
        
        for i in range(self.instrument_list.count()):
            list_item_alias = self.instrument_list.item(i).data(Qt.ItemDataRole.UserRole)
            dict_data = self.instruments_data.get(list_item_alias)
            if not dict_data:
                continue
            
            alias = dict_data.get("alias")
            if not alias:
                errors.append("Validation Error: An instrument has an empty alias.")
                continue
            
            try:
                dict_data["extra_params"] = cast_extra_params(dict_data.get("extra_params", {}))
                config = InstrumentConfig(**dict_data)
                valid_configs.append(config.model_dump(exclude_none=True))
            except Exception as e:
                errors.append(f"Validation Error in '{alias}':\n{e}")
                
        if errors and not silent:
            QMessageBox.critical(self, "Validation Failed", "\n\n".join(errors))
            return
        elif errors and silent:
            # Skip writing incomplete data if auto-saving
            return
            
        # Ensure directory exists
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                yaml.dump(valid_configs, f, default_flow_style=False, sort_keys=False)
            if not silent:
                QMessageBox.information(self, "Success", "Settings saved successfully.")
        except Exception as e:
            if not silent:
                QMessageBox.critical(self, "Save Error", f"Failed to save settings:\n{e}")

    def _test_connection(self):
        """Tests the connection and query command (*IDN?) dynamically."""
        import traceback
        from core.instrument_factory import InstrumentFactory
        
        # Try to initialize PyVISA ResourceManager
        try:
            import pyvisa
            rm = pyvisa.ResourceManager()
        except ImportError:
            # Fallback for dummy tests if pyvisa is not installed
            class MockRM:
                def __str__(self): return "<Mock PyVISA ResourceManager>"
            rm = MockRM()
        except Exception as e:
            QMessageBox.warning(self, "PyVISA Error", f"Failed to load PyVISA backend:\n{e}")
            return

        self._store_current_form()
        
        current_alias = self.le_alias.text().strip()
        if not current_alias or current_alias not in self.instruments_data:
            QMessageBox.warning(self, "Warning", "Please define and select a valid instrument first.")
            return
            
        dict_data = self.instruments_data[current_alias]
        
        try:
            dict_data["extra_params"] = cast_extra_params(dict_data.get("extra_params", {}))
            config = InstrumentConfig(**dict_data)
            factory = InstrumentFactory(resource_manager=rm)
            
            # Instantiate using factory
            instrument = factory.create_instrument(config)
            
            # Testing logic
            instrument.connect()
            response = instrument.test_connection()
            instrument.disconnect()
            
            QMessageBox.information(
                self, 
                "Test Successful", 
                f"Successfully connected to '{config.alias}'.\n\nResponse:\n{response}"
            )
                
        except Exception as e:
            QMessageBox.critical(
                self, 
                "Test Failed", 
                f"Failed to connect or query '{current_alias}':\n\n{str(e)}\n\nTraceback:\n{traceback.format_exc()}"
            )
