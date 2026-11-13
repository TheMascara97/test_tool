# -*- coding: utf-8 -*-
"""
设备测试工具箱 — 公共 GUI 组件

集中放置主题、路径工具、日志 Handler、统计卡片、串口下拉框、
页面基类（自动保存配置、日志面板、忙碌互斥）等。
"""

import json
import logging
import os
import shutil
import re
import sys
import threading
import time

from process_utils import sleep_interruptible  # 再导出：各页面通过 gc.sleep_interruptible 调用

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QLineEdit,
    QSpinBox, QCheckBox, QComboBox, QPushButton, QTextEdit, QPlainTextEdit,
    QFrame,
    QScrollArea, QSizePolicy
)
from PyQt5.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QTextCursor


# ===================== 全局深色主题样式表 =====================
DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #1a1a2e;
    color: #dfe6e9;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 10pt;
}
QTabWidget::pane {
    border: 1px solid #2d3436;
    border-radius: 4px;
    background-color: #16213e;
}
QTabBar::tab {
    background-color: #16213e;
    color: #b2bec3;
    padding: 8px 18px;
    border: 1px solid #2d3436;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
    font-size: 10pt;
}
QTabBar::tab:selected {
    background-color: #0f3460;
    color: #74b9ff;
    font-weight: bold;
    border-bottom: 2px solid #0984e3;
}
QTabBar::tab:hover:!selected {
    background-color: #1e2a45;
    color: #dfe6e9;
}
QGroupBox {
    font-weight: bold;
    color: #74b9ff;
    border: 1px solid #2d3436;
    border-radius: 6px;
    margin-top: 12px;
    padding: 12px 8px 8px 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 8px;
    color: #74b9ff;
}
QLabel {
    color: #dfe6e9;
    background: transparent;
}
QLineEdit {
    background-color: #2d3436;
    color: #dfe6e9;
    border: 1px solid #636e72;
    border-radius: 3px;
    padding: 4px 6px;
    selection-background-color: #0984e3;
}
QLineEdit:focus {
    border-color: #0984e3;
}
QLineEdit:disabled {
    background-color: #1e272e;
    color: #636e72;
}
QSpinBox {
    background-color: #2d3436;
    color: #dfe6e9;
    border: 1px solid #636e72;
    border-radius: 3px;
    padding: 3px 6px;
}
QSpinBox:focus {
    border-color: #0984e3;
}
QSpinBox:disabled {
    background-color: #1e272e;
    color: #636e72;
}
QComboBox {
    background-color: #2d3436;
    color: #dfe6e9;
    border: 1px solid #636e72;
    border-radius: 3px;
    padding: 4px 6px;
    min-width: 100px;
}
QComboBox:focus {
    border-color: #0984e3;
}
QComboBox:disabled {
    background-color: #1e272e;
    color: #636e72;
}
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QComboBox QAbstractItemView {
    background-color: #2d3436;
    color: #dfe6e9;
    border: 1px solid #636e72;
    selection-background-color: #0984e3;
}
QCheckBox {
    color: #dfe6e9;
    spacing: 6px;
    background: transparent;
}
QCheckBox:disabled {
    color: #636e72;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 3px;
    border: 1px solid #636e72;
    background-color: #2d3436;
}
QCheckBox::indicator:checked {
    background-color: #0984e3;
    border-color: #0984e3;
}
QPushButton {
    background-color: #636e72;
    color: #dfe6e9;
    border: none;
    border-radius: 4px;
    padding: 6px 16px;
    font-weight: bold;
    font-size: 10pt;
}
QPushButton:hover {
    background-color: #747d8c;
}
QPushButton:pressed {
    background-color: #535c68;
}
QPushButton:disabled {
    background-color: #2d3436;
    color: #636e72;
}
QTextEdit {
    background-color: #0d1117;
    color: #c9d1d9;
    border: 1px solid #2d3436;
    border-radius: 4px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 10pt;
    selection-background-color: #264f78;
}
QProgressBar {
    background-color: #2d3436;
    border: none;
    border-radius: 4px;
    text-align: center;
    color: #dfe6e9;
    font-weight: bold;
    height: 22px;
}
QProgressBar::chunk {
    background-color: #00b894;
    border-radius: 4px;
}
QStatusBar {
    background-color: #0f3460;
    color: #b2bec3;
    font-size: 9pt;
    border-top: 1px solid #2d3436;
}
QStatusBar::item {
    border: none;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background-color: #16213e;
    width: 10px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background-color: #636e72;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #747d8c;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar:horizontal {
    background-color: #16213e;
    height: 10px;
    border-radius: 5px;
}
QScrollBar::handle:horizontal {
    background-color: #636e72;
    border-radius: 5px;
}
QListWidget {
    background-color: #16213e;
    border: none;
    outline: none;
    color: #b2bec3;
    font-size: 11pt;
}
QListWidget::item {
    padding: 14px 12px;
    border-radius: 6px;
    margin: 2px 6px;
}
QListWidget::item:hover {
    background-color: #1e2a45;
    color: #dfe6e9;
}
QListWidget::item:selected {
    background-color: #0f3460;
    color: #74b9ff;
    font-weight: bold;
    border-left: 3px solid #0984e3;
}
QSplitter::handle {
    background-color: #1a1a2e;
}
"""

# 主按钮 / 停止按钮样式
STYLE_START_BTN = """
QPushButton { background-color: #00b894; color: #fff; font-weight: bold; font-size: 12pt;
              border-radius: 6px; }
QPushButton:hover { background-color: #00a381; }
QPushButton:disabled { background-color: #2d3436; color: #636e72; }
"""

STYLE_STOP_BTN = """
QPushButton { background-color: #e17055; color: #fff; font-weight: bold; font-size: 12pt;
              border-radius: 6px; }
QPushButton:hover { background-color: #d63031; }
QPushButton:disabled { background-color: #2d3436; color: #636e72; }
"""


# ===================== 路径工具 =====================
def app_dir():
    """
    程序运行目录（用于读写配置 / 日志 / 报告）：
    - 源码运行: 脚本所在目录
    - PyInstaller 打包后: exe 所在目录
      （打包后 __file__ 指向 _MEIPASS 临时解包目录，程序退出即删除）
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """只读资源文件路径（图标等）：优先打包临时目录，其次程序运行目录"""
    for base in (getattr(sys, '_MEIPASS', None), app_dir()):
        if base:
            path = os.path.join(base, name)
            if os.path.exists(path):
                return path
    return os.path.join(app_dir(), name)


def default_log_base():
    """默认日志根目录（程序目录下的 logs）"""
    return os.path.join(app_dir(), "logs")


def make_run_log_dir(tool_name, user_base=""):
    """为一次运行创建带时间戳的日志目录"""
    base = user_base.strip() or default_log_base()
    path = os.path.join(base, "%s_%s" % (tool_name, time.strftime("%Y%m%d_%H%M%S")))
    os.makedirs(path, exist_ok=True)
    return path


# ===================== 配置持久化 =====================
CFG_FILENAME = "gui_config.json"
_cfg_lock = threading.RLock()


def _cfg_path():
    return os.path.join(app_dir(), CFG_FILENAME)


def _backup_invalid_config(reason):
    """Keep an unreadable config recoverable before any default is persisted."""
    path = _cfg_path()
    backup = os.path.join(
        app_dir(),
        "gui_config.corrupt_%s.json" % time.strftime("%Y%m%d_%H%M%S"),
    )
    try:
        shutil.copy2(path, backup)
        logging.error("配置文件读取失败，已备份到 %s: %s", backup, reason)
        return backup
    except Exception as ex:
        logging.error("配置文件读取失败且无法备份: %s; 原因: %s", ex, reason)
        return ""


def load_gui_config():
    with _cfg_lock:
        path = _cfg_path()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            _backup_invalid_config("root is %s, expected object" % type(data).__name__)
            return {}
        except FileNotFoundError:
            return {}
        except Exception as ex:
            _backup_invalid_config(ex)
            return {}


def _json_write_atomic(cfg):
    path = _cfg_path()
    tmp = path + ".tmp"
    with _cfg_lock:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def save_gui_sections(sections):
    """原子地更新多个分节，避免同一页面连续保存时互相覆盖。"""
    if not isinstance(sections, dict):
        return False
    with _cfg_lock:
        cfg = load_gui_config()
        cfg.update(sections)
        try:
            _json_write_atomic(cfg)
            return True
        except Exception:
            return False


def save_gui_section(section, data):
    return save_gui_sections({section: data})


def parse_bool(value, default=False):
    """Parse persisted booleans without treating the string "false" as true."""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().casefold()
    if text in ('true', '1', 'on', 'yes', 'y', 'enabled', 'enable'):
        return True
    if text in ('false', '0', 'off', 'no', 'n', 'disabled', 'disable', ''):
        return False
    return bool(default)


def _safe_int(value, default, minimum=None, maximum=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _legacy_project_values(cfg):
    """Build migration defaults from the old feature-page configuration."""
    reboot = cfg.get('reboot', {}) if isinstance(cfg.get('reboot'), dict) else {}
    fota = cfg.get('fota', {}) if isinstance(cfg.get('fota'), dict) else {}

    has_explicit_count = 'DUT_SERIAL_PORT_COUNT' in reboot
    if has_explicit_count:
        # An explicitly disabled serial setup must stay disabled during migration.
        count = _safe_int(
            reboot.get('DUT_SERIAL_PORT_COUNT'), 0, minimum=0, maximum=16)
    else:
        configured_slots = [
            i for i in range(1, 17)
            if str(reboot.get(f'DUT_COM_PORT{i}', '') or '').strip()
        ]
        count = max(configured_slots, default=0)
        if count == 0 and str(fota.get('DUT_COM_PORT', '') or '').strip():
            count = 1

    ports = []
    for i in range(count):
        port = str(reboot.get(f'DUT_COM_PORT{i + 1}', '') or '').strip()
        if not port and i == 0:
            port = str(fota.get('DUT_COM_PORT', '') or '').strip()
        ports.append({
            'name': '调试串口' if i == 0 else f'串口{i + 1}',
            'port': port,
            'baudrate': _safe_int(
                reboot.get(f'DUT_BAUDRATE{i + 1}',
                           reboot.get(f'BAUDRATE{i + 1}',
                                      fota.get('DUT_BAUDRATE', 115200))),
                115200, minimum=1, maximum=921600),
            'username': str(reboot.get(f'SERIAL_USERNAME{i + 1}', '') or ''),
            'password': str(reboot.get(f'SERIAL_PASSWORD{i + 1}', '') or ''),
            'auth_enabled': parse_bool(
                reboot.get(f'SERIAL_AUTH_ENABLED{i + 1}',
                           bool(reboot.get(f'SERIAL_USERNAME{i + 1}') or
                                reboot.get(f'SERIAL_PASSWORD{i + 1}')))),
            'auth_timeout': _safe_int(
                reboot.get(f'SERIAL_AUTH_TIMEOUT{i + 1}', 15), 15,
                minimum=1, maximum=600),
            'boot_detect': parse_bool(
                reboot.get(f'SERIAL_BOOT_DETECT_PORT{i + 1}',
                           reboot.get('SERIAL_BOOT_DETECT', False))),
            'boot_keyword': str(
                reboot.get(f'SERIAL_BOOT_KEYWORD_PORT{i + 1}',
                           fota.get('BOOT_FLAG', '')) or ''),
            'boot_success_count': _safe_int(
                reboot.get(f'SERIAL_BOOT_SUCCESS_COUNT_PORT{i + 1}', 1),
                1, minimum=1, maximum=100),
            'log_to_main': parse_bool(
                reboot.get(f'SERIAL_BOOT_LOG_TO_MAIN_PORT{i + 1}', True), True),
            'log_file': str(
                reboot.get(f'SERIAL_LOG_FILE{i + 1}',
                           f'serial_output_port{i + 1}.log') or ''),
        })

    return {
        'log_channel': 'serial',
        'log_command': 'logcat -v time',
        'adb_enabled': parse_bool(reboot.get('ADB_ENABLED', True), True),
        'adb_path': str(reboot.get('ADB_PATH',
                                   fota.get('CHECK_ADB_PATH', 'adb')) or 'adb'),
        'adb_serial': str(reboot.get('ADB_DEVICE_SERIAL',
                                     fota.get('CHECK_ADB_SERIAL', '')) or ''),
        'adb_username': str(reboot.get('ADB_LOGIN_USERNAME', '') or ''),
        'adb_password': str(reboot.get('ADB_LOGIN_PASSWORD', '') or ''),
        'adb_auth_enabled': parse_bool(
            reboot.get('ADB_AUTH_ENABLED',
                       bool(reboot.get('ADB_LOGIN_USERNAME') or
                            reboot.get('ADB_LOGIN_PASSWORD')))),
        'adb_login_timeout': _safe_int(
            reboot.get('ADB_LOGIN_TIMEOUT', 15), 15, minimum=1, maximum=600),
        'serial_enabled': parse_bool(
            reboot.get('SERIAL_ENABLED', bool(ports)), bool(ports)),
        'serial_ports': ports,
        'ssh_enabled': parse_bool(reboot.get('SSH_ENABLED', False)),
        'ssh_host': str(reboot.get('SSH_HOST', reboot.get('IP_ADDRESS', '')) or ''),
        'ssh_port': _safe_int(reboot.get('SSH_PORT', 22), 22,
                              minimum=1, maximum=65535),
        'ssh_username': str(reboot.get('SSH_USERNAME', '') or ''),
        'ssh_password': str(reboot.get('SSH_PASSWORD', '') or ''),
        'msh_enabled': parse_bool(fota.get(
            'MSH_AUTH_ENABLED', reboot.get('MSH_AUTH_ENABLED', False))),
        'msh_pem': str(fota.get(
            'MSH_PEM_KEY_PATH', reboot.get('MSH_PEM_KEY_PATH', '')) or ''),
        'msh_seed_pattern': str(fota.get(
            'MSH_AUTH_SEED_PATTERN', r'AUTH SEED:\s*([0-9a-fA-F]+)') or
            r'AUTH SEED:\s*([0-9a-fA-F]+)'),
        'msh_post_auth_cmd': str(fota.get('MSH_POST_AUTH_CMD', '') or ''),
        'msh_timeout': _safe_int(
            fota.get('MSH_AUTH_TIMEOUT', 15), 15, minimum=1, maximum=600),
        'msh_success_flag': str(fota.get('MSH_AUTH_SUCCESS_FLAG', '') or ''),
        'msh_fail_flags': str(fota.get('MSH_AUTH_FAIL_FLAGS', '') or ''),
        'msh_ready_wait': _safe_int(
            fota.get('MSH_AUTH_READY_WAIT', 5), 5, minimum=0, maximum=600),
        'boot_detect_enabled': parse_bool(fota.get(
            'BOOT_DETECT_ENABLED', reboot.get('BOOT_DETECT_ENABLED', False))),
        'boot_keyword': str(fota.get(
            'BOOT_FLAG', reboot.get('BOOT_SUCCESS_FLAG', '')) or ''),
        'boot_timeout': _safe_int(
            fota.get('BOOT_TIMEOUT', reboot.get('BOOT_TIMEOUT', 180)),
            180, minimum=1, maximum=7200),
    }


def _default_project(cfg):
    project = {"name": "默认项目"}
    project.update(_legacy_project_values(cfg))
    return normalize_project(project)


def normalize_project(project, fallback_name="默认项目", legacy=None):
    p = dict(project) if isinstance(project, dict) else {}
    defaults = _legacy_project_values(legacy) if isinstance(legacy, dict) else {}
    p['name'] = str(p.get('name') or fallback_name).strip() or fallback_name

    for key in (
            'adb_enabled', 'adb_path', 'adb_serial', 'adb_auth_enabled',
            'adb_username', 'adb_password', 'adb_login_timeout', 'serial_enabled',
            'serial_port_count', 'ssh_enabled', 'ssh_host', 'ssh_port',
            'ssh_username', 'ssh_password', 'msh_enabled', 'msh_pem',
            'msh_seed_pattern', 'msh_post_auth_cmd', 'msh_timeout',
            'msh_success_flag', 'msh_fail_flags', 'msh_ready_wait',
            'boot_detect_enabled', 'boot_keyword', 'boot_timeout'):
        if key not in p and key in defaults:
            p[key] = defaults[key]

    raw_channel = str(p.get('log_channel') or defaults.get(
        'log_channel', 'serial') or 'serial').strip().lower()
    p['log_channel'] = raw_channel if raw_channel in (
        'adb', 'ssh', 'serial') else 'serial'
    if 'log_command' not in p and 'log_command' in defaults:
        p['log_command'] = defaults['log_command']
    p['log_command'] = str(p.get('log_command') or '').strip()

    p['adb_enabled'] = parse_bool(p.get('adb_enabled', True), True)
    p['adb_path'] = str(p.get('adb_path') or 'adb').strip() or 'adb'
    p['adb_serial'] = str(p.get('adb_serial') or '').strip()
    p['adb_auth_enabled'] = parse_bool(
        p.get('adb_auth_enabled',
              bool(p.get('adb_username') or p.get('adb_password'))))
    p['adb_username'] = str(p.get('adb_username') or '').strip()
    p['adb_password'] = str(p.get('adb_password') or '')
    p['adb_login_timeout'] = _safe_int(
        p.get('adb_login_timeout', 15), 15, minimum=1, maximum=600)

    raw_ports = p.get('serial_ports', [])
    if not isinstance(raw_ports, list):
        raw_ports = []
    requested_present = 'serial_port_count' in p
    requested_count = _safe_int(
        p.get('serial_port_count', len(raw_ports)), len(raw_ports),
        minimum=0, maximum=16)
    count = requested_count if requested_present else len(raw_ports)
    ports = []
    for i in range(count):
        raw = raw_ports[i] if i < len(raw_ports) else {}
        if isinstance(raw, dict):
            port_data = dict(raw)
        else:
            port_data = {'port': str(raw or '')}
        port_data['name'] = str(
            port_data.get('name') or
            ('调试串口' if i == 0 else f'串口{i + 1}')).strip()
        port_data['port'] = str(port_data.get('port') or '').strip()
        port_data['baudrate'] = _safe_int(
            port_data.get('baudrate', 115200), 115200,
            minimum=1, maximum=921600)
        port_data['username'] = str(port_data.get('username') or '').strip()
        port_data['password'] = str(port_data.get('password') or '')
        port_data['auth_enabled'] = parse_bool(
            port_data.get('auth_enabled',
                          bool(port_data['username'] or port_data['password'])))
        legacy_slot = (defaults.get('serial_ports') or [])
        legacy_slot = legacy_slot[i] if i < len(legacy_slot) else {}
        if 'auth_timeout' not in port_data:
            port_data['auth_timeout'] = _safe_int(
                legacy_slot.get('auth_timeout', 15), 15,
                minimum=1, maximum=600)
        else:
            port_data['auth_timeout'] = _safe_int(
                port_data['auth_timeout'], 15, minimum=1, maximum=600)
        if 'boot_detect' not in port_data:
            port_data['boot_detect'] = parse_bool(
                legacy_slot.get('boot_detect', p.get('boot_detect_enabled', False)))
        else:
            port_data['boot_detect'] = parse_bool(port_data['boot_detect'])
        if 'boot_keyword' not in port_data:
            port_data['boot_keyword'] = str(
                legacy_slot.get('boot_keyword', p.get('boot_keyword', '')) or '')
        else:
            port_data['boot_keyword'] = str(port_data['boot_keyword'] or '')
        if 'boot_success_count' not in port_data:
            port_data['boot_success_count'] = _safe_int(
                legacy_slot.get('boot_success_count', 1), 1,
                minimum=1, maximum=100)
        else:
            port_data['boot_success_count'] = _safe_int(
                port_data['boot_success_count'], 1,
                minimum=1, maximum=100)
        if 'log_to_main' not in port_data:
            port_data['log_to_main'] = parse_bool(
                legacy_slot.get('log_to_main', True), True)
        else:
            port_data['log_to_main'] = parse_bool(port_data['log_to_main'], True)
        if 'log_file' not in port_data:
            port_data['log_file'] = str(
                legacy_slot.get('log_file',
                                f'serial_output_port{i + 1}.log') or '')
        else:
            port_data['log_file'] = str(port_data['log_file'] or '')
        ports.append(port_data)

    p['serial_ports'] = ports
    p['serial_port_count'] = len(ports)
    p['serial_enabled'] = parse_bool(
        p.get('serial_enabled', bool(ports)), bool(ports))
    p['ssh_enabled'] = parse_bool(p.get('ssh_enabled', False))
    p['ssh_host'] = str(p.get('ssh_host') or '').strip()
    p['ssh_port'] = _safe_int(
        p.get('ssh_port', 22), 22, minimum=1, maximum=65535)
    p['ssh_username'] = str(p.get('ssh_username') or '').strip()
    p['ssh_password'] = str(p.get('ssh_password') or '')
    p['msh_enabled'] = parse_bool(p.get('msh_enabled', False))
    p['msh_pem'] = str(p.get('msh_pem') or '').strip()
    p['msh_seed_pattern'] = str(
        p.get('msh_seed_pattern') or r'AUTH SEED:\s*([0-9a-fA-F]+)')
    p['msh_post_auth_cmd'] = str(p.get('msh_post_auth_cmd') or '')
    p['msh_timeout'] = _safe_int(
        p.get('msh_timeout', 15), 15, minimum=1, maximum=600)
    p['msh_success_flag'] = str(p.get('msh_success_flag') or '')
    p['msh_fail_flags'] = str(p.get('msh_fail_flags') or '')
    p['msh_ready_wait'] = _safe_int(
        p.get('msh_ready_wait', 5), 5, minimum=0, maximum=600)
    p['boot_detect_enabled'] = parse_bool(p.get('boot_detect_enabled', False))
    p['boot_keyword'] = str(p.get('boot_keyword') or '')
    p['boot_timeout'] = _safe_int(
        p.get('boot_timeout', 180), 180, minimum=1, maximum=7200)
    p.pop('relay_port', None)
    p.pop('relay_baudrate', None)
    return p


def ensure_device_profiles():
    """迁移旧配置；已有 device_profiles 原样保留，迁移幂等。"""
    with _cfg_lock:
        cfg = load_gui_config()
        changed = False
        profiles = cfg.get('device_profiles')
        if not isinstance(profiles, dict) or not isinstance(profiles.get('projects'), list):
            project = _default_project(cfg)
            cfg['device_profiles'] = {"active": project['name'], "projects": [project]}
            changed = True
        else:
            projects = [normalize_project(p, f"项目{i + 1}", cfg)
                        for i, p in enumerate(profiles['projects'])]
            projects = [p for p in projects if p['name']]
            if not projects:
                projects = [normalize_project(_default_project(cfg))]
            used = set()
            for project in projects:
                base = project['name']
                name = base
                number = 2
                while name.casefold() in used:
                    name = f"{base} ({number})"
                    number += 1
                project['name'] = name
                used.add(name.casefold())
            active = profiles.get('active')
            if not any(p['name'].casefold() == str(active or '').casefold()
                       for p in projects):
                active = projects[0]['name']
            normalized = {"active": active, "projects": projects}
            if normalized != profiles:
                cfg['device_profiles'] = normalized
                changed = True
        if changed:
            _json_write_atomic(cfg)
        return cfg.get('device_profiles', {"active": "", "projects": []})


def get_project_profiles():
    return ensure_device_profiles()


def get_active_project():
    profiles = ensure_device_profiles()
    active = profiles.get('active', '')
    for project in profiles.get('projects', []):
        if project.get('name') == active:
            return project
    projects = profiles.get('projects', [])
    return projects[0] if projects else {}


def save_project_profiles(profiles):
    raw_profiles = dict(profiles or {})
    raw_projects = raw_profiles.get('projects', [])
    if not isinstance(raw_projects, list) or not raw_projects:
        return False

    for project in raw_projects:
        if not isinstance(project, dict):
            return False
        if not str(project.get('name') or '').strip():
            return False
        if 'serial_port_count' in project:
            try:
                count = int(project.get('serial_port_count'))
            except (TypeError, ValueError):
                return False
            if count < 0 or count > 16:
                return False
            ports = project.get('serial_ports', [])
            if isinstance(ports, list) and len(ports) > 16:
                return False

    raw_names = [str(p.get('name') or '').strip() for p in raw_projects]
    folded = [name.casefold() for name in raw_names]
    if len(folded) != len(set(folded)):
        return False

    profiles = {
        'active': raw_profiles.get('active', ''),
        'projects': [normalize_project(p, '', None) for p in raw_projects],
    }
    names = [p['name'] for p in profiles['projects']]
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        return False
    active_name = str(profiles.get('active') or '')
    for name in names:
        if name.casefold() == active_name.casefold():
            profiles['active'] = name
            break
    else:
        profiles['active'] = names[0]
    return save_gui_sections({'device_profiles': profiles})


def project_adb_args(project=None):
    p = project or get_active_project()
    p = normalize_project(p)
    path = p.get('adb_path', 'adb') or 'adb'
    serial = p.get('adb_serial', '')
    return ([path, '-s', serial] if serial else [path]), p.get('adb_enabled', True)


def project_serial(project=None, index=0, default_port="", default_baud=115200):
    p = project or get_active_project()
    p = normalize_project(p)
    if not p.get('serial_enabled', False):
        return "", default_baud
    ports = p.get('serial_ports', [])
    if 0 <= index < len(ports):
        port = str(ports[index].get('port', '') or '').strip()
        if port:
            return (port, _safe_int(ports[index].get('baudrate'), default_baud,
                                    minimum=1, maximum=921600))
    return "", default_baud


def project_serial_port(project=None, index=0):
    """Return a copy of one configured serial slot, or an empty slot."""
    p = normalize_project(project or get_active_project())
    ports = p.get('serial_ports', [])
    if 0 <= index < len(ports) and p.get('serial_enabled', False):
        return dict(ports[index])
    return {}


def project_ssh_config(project=None):
    p = normalize_project(project or get_active_project())
    return {
        'enabled': p.get('ssh_enabled', False),
        'host': p.get('ssh_host', ''),
        'port': p.get('ssh_port', 22),
        'username': p.get('ssh_username', ''),
        'password': p.get('ssh_password', ''),
    }


def project_msh_config(project=None):
    p = normalize_project(project or get_active_project())
    return {
        'enabled': p.get('msh_enabled', False),
        'pem': p.get('msh_pem', ''),
        'seed_pattern': p.get('msh_seed_pattern',
                              r'AUTH SEED:\s*([0-9a-fA-F]+)'),
        'post_auth_cmd': p.get('msh_post_auth_cmd', ''),
        'timeout': p.get('msh_timeout', 15),
        'success_flag': p.get('msh_success_flag', ''),
        'ready_wait': p.get('msh_ready_wait', 5),
        'fail_flags': p.get('msh_fail_flags', ''),
    }


# ===================== 串口工具 =====================
def get_available_ports():
    try:
        import serial.tools.list_ports as slp
        return sorted([p.device for p in slp.comports()])
    except Exception:
        return []


def make_port_combo(default_port=""):
    combo = QComboBox()
    combo.setEditable(True)
    combo.setMinimumWidth(160)
    refresh_port_combo(combo, default_port)
    return combo


def refresh_port_combo(combo, select_port=""):
    current = combo.currentText() if not select_port else select_port
    combo.clear()
    ports = get_available_ports()
    if ports:
        combo.addItems(ports)
        idx = combo.findText(current)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(current)
    else:
        combo.setEditText(current)


# ===================== Qt 日志 Handler =====================
class SensitiveLogFilter(logging.Filter):
    """Mask credentials before records reach files or the on-screen log."""

    _PEM_BLOCK = re.compile(
        r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
        re.IGNORECASE | re.DOTALL)
    _KEY_VALUE = re.compile(
        r'(?i)\b(password|passwd|pwd|token|secret|authorization|api[_-]?key|'
        r'private[_-]?key)\b(\s*[:=]\s*)([^\s,;]+)')

    @classmethod
    def sanitize(cls, text):
        text = cls._PEM_BLOCK.sub('-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----',
                                  str(text))
        return cls._KEY_VALUE.sub(r'\1\2***', text)

    def filter(self, record):
        record.msg = self.sanitize(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                self.sanitize(arg) if isinstance(arg, str) else arg
                for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: self.sanitize(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


class QtLogHandler(QObject, logging.Handler):
    log_signal = pyqtSignal(str)

    def __init__(self):
        QObject.__init__(self)
        logging.Handler.__init__(self)

    def emit(self, record):
        try:
            self.log_signal.emit(self.format(record))
        except Exception:
            pass


# ===================== 统计卡片 =====================
class StatCard(QFrame):
    """单个统计指标卡片"""

    def __init__(self, title, value="--", color="#636e72"):
        super().__init__()
        self.setStyleSheet("""
            StatCard {
                background-color: #16213e;
                border-radius: 6px;
                border: 1px solid #2d3436;
                padding: 4px;
            }
        """)
        self.setFixedHeight(68)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)

        self._title = QLabel(title)
        self._title.setStyleSheet("color: #b2bec3; font-size: 8pt; font-weight: normal; background: transparent;")
        layout.addWidget(self._title)

        self._value = QLabel(str(value))
        self._value.setStyleSheet(f"color: {color}; font-size: 14pt; font-weight: bold; background: transparent;")
        self._value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self._value)

    def set_value(self, value, color=None):
        self._value.setText(str(value))
        if color:
            self._value.setStyleSheet(
                f"color: {color}; font-size: 14pt; font-weight: bold; background: transparent;")

    def set_title(self, title):
        self._title.setText(title)


# ===================== 全局忙碌互斥 =====================
_busy_owner = {"page": None}


def acquire_busy(page):
    """同一时间只允许一个测试运行（串口 / adb / 日志互不干扰）"""
    if _busy_owner["page"] in (None, page):
        _busy_owner["page"] = page
        return True
    return False


def release_busy(page):
    if _busy_owner["page"] is page:
        _busy_owner["page"] = None


def busy_owner():
    return _busy_owner["page"]


def format_duration(seconds):
    seconds = int(max(0, seconds))
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


# ===================== 页面基类 =====================
class BasePage(QWidget):
    """
    工具页面基类：
    - 配置自动保存/恢复（gui_config.json 中按 tool_key 分节）
    - 统一日志面板 + 着色 + 行数限制
    - root logger 接入（同时写文件与界面）
    - 运行状态互斥
    """

    MAX_LOG_LINES = 5000
    title_changed = pyqtSignal(str)
    run_state_changed = pyqtSignal(bool, str)   # (running, page_name)

    def __init__(self, tool_key, tool_name, parent=None):
        super().__init__(parent)
        self.tool_key = tool_key
        self.tool_name = tool_name
        self._log_handler = None
        self._file_handler = None
        self._log_line_count = 0
        self.is_running = False
        self._cfg = load_gui_config().get(tool_key, {})
        if not isinstance(self._cfg, dict):
            self._cfg = {}

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(600)
        self._autosave_timer.timeout.connect(self._do_autosave)
        self._suppress_autosave = False

    # ---------- 配置 ----------
    def collect(self):
        """返回当前配置字典（子类实现）"""
        return {}

    def apply_config(self, cfg):
        """把配置字典还原到控件（子类实现）"""
        pass

    def restore_config(self):
        if self._cfg:
            self._suppress_autosave = True
            try:
                self.apply_config(self._cfg)
            finally:
                self._suppress_autosave = False

    def bind_autosave(self, widgets):
        for w in widgets:
            if isinstance(w, QCheckBox):
                w.stateChanged.connect(self._on_config_changed)
            elif isinstance(w, QSpinBox):
                w.valueChanged.connect(self._on_config_changed)
            elif isinstance(w, QLineEdit):
                w.textChanged.connect(self._on_config_changed)
            elif isinstance(w, (QTextEdit, QPlainTextEdit)):
                w.textChanged.connect(self._on_config_changed)
            elif isinstance(w, QComboBox):
                w.currentTextChanged.connect(self._on_config_changed)
                w.currentIndexChanged.connect(self._on_config_changed)

    def _on_config_changed(self, *args):
        if self._suppress_autosave:
            return
        self._autosave_timer.start()

    def _do_autosave(self):
        try:
            if not save_gui_section(self.tool_key, self.collect()):
                logging.warning("自动保存配置失败: %s", self.tool_key)
                return False
            return True
        except Exception:
            logging.warning("自动保存配置异常: %s", self.tool_key, exc_info=True)
            return False

    def flush_pending_save(self, force=False):
        """Persist a pending autosave before switching pages or exiting."""
        pending = self._autosave_timer.isActive()
        self._autosave_timer.stop()
        if pending or force:
            return bool(self._do_autosave())
        return True

    # ---------- 日志 ----------
    def build_log_panel(self, with_open_dir=False, log_dir_getter=None):
        box = QGroupBox("运行日志")
        lay = QVBoxLayout(box)
        lay.setSpacing(6)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))
        lay.addWidget(self.log_text, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        if with_open_dir:
            self.btn_open_log_dir = QPushButton("📁 打开日志目录")
            self.btn_open_log_dir.setFixedHeight(30)
            self.btn_open_log_dir.clicked.connect(
                lambda: self.open_log_dir(log_dir_getter() if log_dir_getter else None))
            row.addWidget(self.btn_open_log_dir)
        row.addStretch()
        btn_clear = QPushButton("🗑 清除日志")
        btn_clear.setFixedHeight(30)
        btn_clear.clicked.connect(self.clear_log)
        row.addWidget(btn_clear)
        lay.addLayout(row)
        return box

    def clear_log(self):
        self.log_text.clear()
        self._log_line_count = 0

    def append_log(self, text, color=None, bold=False):
        if color is None:
            color = self._color_for(text)
        escaped = (text.replace('&', '&amp;').replace('<', '&lt;')
                   .replace('>', '&gt;').replace('\n', '<br>'))
        self.log_text.append(
            f'<span style="color:{color};{"font-weight:bold;" if bold else ""}">{escaped}</span>')
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

        self._log_line_count += 1
        if self._log_line_count > self.MAX_LOG_LINES:
            cursor = self.log_text.textCursor()
            cursor.movePosition(QTextCursor.Start)
            cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor, 500)
            cursor.removeSelectedText()
            self._log_line_count -= 500

        self.on_log_line(text)

    @staticmethod
    def _color_for(text):
        if 'ERROR' in text or '❌' in text or '🔴' in text or '[FAIL]' in text:
            return '#e17055'
        if 'WARNING' in text or '⚠️' in text or '⚠' in text or '[TIMEOUT]' in text:
            return '#fdcb6e'
        if '✅' in text or '🟢' in text or '[PASS]' in text or '成功' in text:
            return '#00b894'
        if '第' in text and '次循环' in text:
            return '#74b9ff'
        if '阶段' in text or '----' in text or '====' in text:
            return '#a29bfe'
        return '#c9d1d9'

    def on_log_line(self, text):
        """子类可重写，用于解析统计信息"""
        pass

    def attach_logging(self, log_file):
        """把 root logger 接到界面日志 + 文件日志"""
        self.detach_logging()
        formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
        root = logging.getLogger()
        root.setLevel(logging.INFO)

        try:
            self._file_handler = logging.FileHandler(log_file, encoding='utf-8')
            self._file_handler.setFormatter(formatter)
            self._file_handler.addFilter(SensitiveLogFilter())
            root.addHandler(self._file_handler)
        except Exception as ex:
            self.append_log(f"[WARNING] 无法创建日志文件 {log_file}: {ex}")

        self._log_handler = QtLogHandler()
        self._log_handler.setFormatter(formatter)
        self._log_handler.addFilter(SensitiveLogFilter())
        self._log_handler.log_signal.connect(self.append_log)
        root.addHandler(self._log_handler)

    def detach_logging(self):
        root = logging.getLogger()
        if self._log_handler is not None:
            try:
                root.removeHandler(self._log_handler)
            except Exception:
                pass
            self._log_handler = None
        if self._file_handler is not None:
            try:
                root.removeHandler(self._file_handler)
                self._file_handler.close()
            except Exception:
                pass
            self._file_handler = None

    # ---------- 运行状态 ----------
    def try_acquire(self):
        if acquire_busy(self):
            self.is_running = True
            self.run_state_changed.emit(True, self.tool_name)
            return True
        owner = busy_owner()
        owner_name = owner.tool_name if owner is not None else "其他任务"
        self.append_log(f"[WARNING] 已有任务在运行（{owner_name}），请先停止后再启动")
        return False

    def release_run(self):
        self.is_running = False
        release_busy(self)
        self.run_state_changed.emit(False, self.tool_name)

    def prepare_stop(self):
        """子类重写：在关闭窗口时发起停止（不阻塞）"""
        pass

    # ---------- 控件工具 ----------
    @staticmethod
    def spin(min_v, max_v, default, suffix=""):
        s = QSpinBox()
        s.setMinimum(min_v)
        s.setMaximum(max_v)
        s.setValue(default)
        if suffix:
            s.setSuffix(f" {suffix}")
        return s

    @staticmethod
    def scroll_container():
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        w = QWidget()
        w.setLayout(QVBoxLayout(w))
        w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        scroll.setWidget(w)
        return scroll

    @staticmethod
    def open_log_dir(path):
        if not path:
            path = default_log_base()
        if not os.path.isdir(path):
            return
        try:
            os.startfile(path)
        except Exception:
            try:
                import subprocess
                subprocess.Popen(['explorer', path])
            except Exception:
                pass
