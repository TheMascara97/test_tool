# -*- coding: utf-8 -*-
"""
FOTA 升降级测试页面 — 包装 NASH_FOTA.py 的 FotaTest。
配置项与 NASH_FOTA 顶部的用户配置区一一对应，运行前写回模块全局变量。
"""

import os
import re
import time
import traceback

from PyQt5.QtWidgets import (
    QWidget, QTabWidget, QGroupBox, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QCheckBox, QComboBox, QPushButton, QGridLayout, QProgressBar,
    QFileDialog, QToolButton, QLabel, QButtonGroup, QStyle, QPlainTextEdit
)
from PyQt5.QtCore import QThread, QTimer, pyqtSignal

import gui_common as gc
import device_session
from gui_common import BasePage, StatCard, make_port_combo, refresh_port_combo, make_run_log_dir


class FotaWorker(QThread):
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, mod, parent=None):
        super().__init__(parent)
        self.mod = mod
        self.test_instance = None

    def run(self):
        try:
            self.test_instance = self.mod.FotaTest()
            self.test_instance.run()
        except Exception:
            self.error_signal.emit(traceback.format_exc())
        finally:
            self.finished_signal.emit()

    def stop(self):
        if self.test_instance:
            self.test_instance.running = False
            try:
                if self.test_instance.downgrade_executor.current_process:
                    self.test_instance.downgrade_executor._terminate_process_tree(
                        self.test_instance.downgrade_executor.current_process)
            except Exception:
                pass


class FotaPage(BasePage):
    def __init__(self, parent=None):
        super().__init__("fota", "FOTA 升降级", parent)
        self.mod = None
        self.worker = None
        self._start_time = 0
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._update_elapsed)
        self._build_ui()
        self._update_all_deps()
        self.restore_config()

    # ==================== UI ====================
    def mod_defaults(self):
        if self.mod is None:
            import NASH_FOTA as m
            self.mod = m
        return self.mod

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)

        self.tabs = QTabWidget()
        left_lay.addWidget(self.tabs, 1)
        self._build_tab_device()
        self._build_tab_upgrade()
        self._build_tab_check()
        self._build_tab_msh()
        self._build_tab_log()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.btn_start = QPushButton("▶  开始 FOTA 测试")
        self.btn_start.setFixedHeight(44)
        self.btn_start.setStyleSheet(gc.STYLE_START_BTN)
        self.btn_stop = QPushButton("■  停止")
        self.btn_stop.setFixedHeight(44)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(gc.STYLE_STOP_BTN)
        btn_row.addWidget(self.btn_start, 2)
        btn_row.addWidget(self.btn_stop, 1)
        left_lay.addLayout(btn_row)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.btn_refresh_ports = QPushButton("🔄 刷新串口")
        self.btn_save_config = QPushButton("💾 导出配置")
        self.btn_load_config = QPushButton("📂 导入配置")
        for b in (self.btn_refresh_ports, self.btn_save_config, self.btn_load_config):
            b.setFixedHeight(32)
            row2.addWidget(b)
        left_lay.addLayout(row2)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(6)

        grid = QGridLayout()
        grid.setSpacing(6)
        self.card_cycle = StatCard("当前循环", "0", "#74b9ff")
        self.card_status = StatCard("状态", "就绪", "#636e72")
        self.card_pass = StatCard("成功", "0", "#00b894")
        self.card_fail = StatCard("失败", "0", "#e17055")
        self.card_rate = StatCard("成功率", "--", "#fdcb6e")
        self.card_stage = StatCard("当前阶段", "--", "#a29bfe")
        self.card_msg = StatCard("最新结果", "--", "#a29bfe")
        self.card_elapsed = StatCard("总运行时间", "00:00:00", "#b2bec3")
        for i, c in enumerate([self.card_cycle, self.card_status, self.card_pass, self.card_fail,
                               self.card_rate, self.card_stage, self.card_msg, self.card_elapsed]):
            grid.addWidget(c, i // 4, i % 4)
        right_lay.addLayout(grid)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("就绪")
        right_lay.addWidget(self.progress_bar)
        right_lay.addWidget(self.build_log_panel(
            with_open_dir=True,
            log_dir_getter=lambda: self.mod._LOG_DIR if self.mod else ""), 1)

        root.addWidget(left, 5)
        root.addWidget(right, 4)

        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_refresh_ports.clicked.connect(lambda: refresh_port_combo(self.combo_dut_port))
        self.btn_save_config.clicked.connect(self._save_config)
        self.btn_load_config.clicked.connect(self._load_config)

        self.bind_autosave([
            self.combo_dut_port, self.spin_baud, self.chk_relay_enabled,
            self.combo_relay_port, self.spin_relay_baud,
            self.spin_relay_off_time, self.spin_relay_on_wait,
            self.edit_otfdec, self.edit_boot_ver, self.edit_upgrade_ver,
            self.chk_use_regex, self.edit_boot_flag, self.spin_boot_timeout,
            self.spin_boot_ver_timeout, self.edit_fota_start_flag, self.edit_fota_done_flag,
            self.spin_fota_timeout, self.spin_fota_retry, self.spin_max_cycles,
            self.combo_downgrade_mode, self.combo_downgrade_channel,
            self.spin_downgrade_boot_wait,
            self.edit_downgrade_cmd, self.edit_downgrade_expect,
            self.spin_downgrade_trigger_timeout,
            self.edit_downgrade_adb_path, self.edit_downgrade_adb_serial,
            self.edit_downgrade_ssh_host, self.spin_downgrade_ssh_port,
            self.edit_downgrade_ssh_user, self.edit_downgrade_ssh_password,
            self.edit_downgrade_ssh_key,
            self.spin_downgrade_timeout, self.spin_script_retries, self.spin_max_rounds,
            self.spin_post_downgrade_wait, self.chk_retry_relay, self.spin_retry_boot_wait,
            self.chk_msh_enabled, self.edit_pem, self.edit_seed_pattern,
            self.edit_post_auth_cmd, self.spin_msh_timeout, self.edit_msh_ok_flag,
            self.spin_msh_ready_wait, self.edit_msh_fail_flags, self.edit_sn_flag,
            self.chk_check_serial, self.edit_check_serial_cmd,
            self.edit_check_serial_expect, self.spin_check_serial_timeout,
            self.chk_check_adb, self.edit_check_adb_path, self.edit_check_adb_serial,
            self.edit_check_adb_cmd, self.edit_check_adb_expect, self.spin_check_adb_timeout,
            self.edit_log_base,
        ])

    # ---------- Tab 设备 / 继电器 ----------
    def _build_tab_device(self):
        m = self.mod_defaults()
        scroll = self.scroll_container()
        lay = scroll.widget().layout()

        grp = QGroupBox("被测设备串口")
        f = QFormLayout(grp)
        f.setSpacing(8)
        self.combo_dut_port = make_port_combo(m.DUT_COM_PORT)
        f.addRow("设备串口:", self.combo_dut_port)
        self.spin_baud = self.spin(1200, 921600, m.DUT_BAUDRATE)
        f.addRow("波特率:", self.spin_baud)
        lay.addWidget(grp)

        grp2 = QGroupBox("继电器 (用于异常 / 降级失败时通断电重启)")
        v = QVBoxLayout(grp2)
        self.chk_relay_enabled = QCheckBox("启用继电器")
        self.chk_relay_enabled.setChecked(m.RELAY_ENABLED)
        v.addWidget(self.chk_relay_enabled)
        f2 = QFormLayout()
        f2.setSpacing(8)
        self.combo_relay_port = make_port_combo(m.RELAY_COM_PORT)
        f2.addRow("继电器串口:", self.combo_relay_port)
        self.spin_relay_baud = self.spin(1200, 921600, m.RELAY_BAUDRATE)
        f2.addRow("波特率:", self.spin_relay_baud)
        self.spin_relay_off_time = self.spin(0, 300, m.RELAY_POWER_OFF_TIME, "秒")
        f2.addRow("断电持续时间:", self.spin_relay_off_time)
        self.spin_relay_on_wait = self.spin(0, 300, m.RELAY_POWER_ON_WAIT, "秒")
        f2.addRow("上电稳定等待:", self.spin_relay_on_wait)
        self.edit_otfdec = QLineEdit(m.OTFDEC_ERROR_FLAG)
        f2.addRow("OTFDEC 错误标志:", self.edit_otfdec)
        v.addLayout(f2)
        self.relay_widgets = [self.combo_relay_port, self.spin_relay_baud,
                              self.spin_relay_off_time, self.spin_relay_on_wait, self.edit_otfdec]
        lay.addWidget(grp2)
        lay.addStretch()
        self.tabs.addTab(scroll, "  设备  ")
        self.chk_relay_enabled.stateChanged.connect(self._update_relay_deps)

    # ---------- Tab FOTA / 降级 ----------
    def _build_tab_upgrade(self):
        m = self.mod_defaults()
        scroll = self.scroll_container()
        lay = scroll.widget().layout()

        grp = QGroupBox("版本号与开机标志")
        f = QFormLayout(grp)
        f.setSpacing(8)
        self.edit_boot_ver = QLineEdit(m.BOOT_VERSION)
        f.addRow("降级后 / 开机版本:", self.edit_boot_ver)
        self.edit_upgrade_ver = QLineEdit(m.UPGRADE_VERSION)
        f.addRow("升级成功版本:", self.edit_upgrade_ver)
        self.chk_use_regex = QCheckBox("使用正则表达式匹配版本")
        self.chk_use_regex.setChecked(m.USE_REGEX)
        f.addRow(self.chk_use_regex)
        self.edit_boot_flag = QLineEdit(m.BOOT_FLAG)
        f.addRow("开机成功标志:", self.edit_boot_flag)
        self.spin_boot_timeout = self.spin(1, 3600, m.BOOT_TIMEOUT, "秒")
        f.addRow("最大等待启动:", self.spin_boot_timeout)
        self.spin_boot_ver_timeout = self.spin(1, 600, m.BOOT_VERSION_DETECT_TIMEOUT, "秒")
        f.addRow("未检测到版本时等待:", self.spin_boot_ver_timeout)
        lay.addWidget(grp)

        grp2 = QGroupBox("FOTA 升级状态机")
        f2 = QFormLayout(grp2)
        f2.setSpacing(8)
        self.edit_fota_start_flag = QLineEdit(m.FOTA_UPGRADE_START_FLAG)
        f2.addRow("升级开始标志:", self.edit_fota_start_flag)
        self.edit_fota_done_flag = QLineEdit(m.FOTA_UPGRADE_COMPLETE_FLAG)
        f2.addRow("升级完成标志:", self.edit_fota_done_flag)
        self.spin_fota_timeout = self.spin(1, 7200, m.FOTA_UPGRADE_TIMEOUT, "秒")
        f2.addRow("升级等待超时:", self.spin_fota_timeout)
        self.spin_fota_retry = self.spin(1, 20, m.FOTA_RETRY_MAX)
        f2.addRow("未触发时重启重试:", self.spin_fota_retry)
        lay.addWidget(grp2)

        grp_mode = QGroupBox("降级方式")
        fmode = QFormLayout(grp_mode)
        fmode.setSpacing(8)
        self.combo_downgrade_mode = QComboBox()
        self.combo_downgrade_mode.addItem("JFLASH 烧录", "jflash")
        self.combo_downgrade_mode.addItem("断电触发", "power_cycle")
        self.combo_downgrade_mode.addItem("命令触发", "command")
        mode_default = getattr(m, 'DOWNGRADE_MODE', 'jflash')
        mode_index = self.combo_downgrade_mode.findData(mode_default)
        self.combo_downgrade_mode.setCurrentIndex(mode_index if mode_index >= 0 else 0)
        fmode.addRow("降级执行方式:", self.combo_downgrade_mode)
        self.spin_downgrade_boot_wait = self.spin(
            0, 600, getattr(m, 'DOWNGRADE_TRIGGER_BOOT_WAIT', 10), "秒")
        self.spin_downgrade_boot_wait.setToolTip(
            "断电/命令触发成功后，等待设备重启或进入降级流程的时间")
        fmode.addRow("触发后等待:", self.spin_downgrade_boot_wait)
        lay.addWidget(grp_mode)

        grp = QGroupBox("JFLASH 降级脚本 (按顺序执行)")
        self.grp_downgrade_jflash = grp
        v = QVBoxLayout(grp)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.btn_add_script = QPushButton("添加")
        self.btn_add_script.setToolTip("添加一条降级脚本")
        self.btn_del_script = QPushButton("删除")
        self.btn_del_script.setToolTip("删除当前选中的降级脚本")
        self.btn_up_script = QPushButton("上移")
        self.btn_up_script.setToolTip("将当前脚本向上移动")
        self.btn_down_script = QPushButton("下移")
        self.btn_down_script.setToolTip("将当前脚本向下移动")
        for b in (self.btn_add_script, self.btn_del_script, self.btn_up_script,
                  self.btn_down_script):
            b.setFixedHeight(28)
            btn_row.addWidget(b)
        btn_row.addStretch()
        v.addLayout(btn_row)

        self.script_rows_layout = QVBoxLayout()
        self.script_rows_layout.setSpacing(8)
        v.addLayout(self.script_rows_layout)
        self.script_rows = []
        self.script_selection_group = QButtonGroup(self)
        self.script_selection_group.setExclusive(True)
        self._active_script_index = -1
        self._script_candidates = []

        f = QFormLayout()
        f.setSpacing(8)
        self.spin_downgrade_timeout = self.spin(1, 7200, m.DOWNGRADE_TIMEOUT, "秒")
        f.addRow("单脚本超时:", self.spin_downgrade_timeout)
        self.spin_script_retries = self.spin(1, 50, getattr(m, 'DOWNGRADE_SCRIPT_RETRIES', 3), "次")
        f.addRow("每条脚本重试次数:", self.spin_script_retries)
        self.spin_max_rounds = self.spin(1, 50, getattr(m, 'DOWNGRADE_MAX_ROUNDS', 3), "轮")
        f.addRow("整体重试轮数:", self.spin_max_rounds)
        v.addLayout(f)
        lay.addWidget(grp)

        grp_cmd = QGroupBox("命令触发降级")
        self.grp_downgrade_command = grp_cmd
        fc = QFormLayout(grp_cmd)
        fc.setSpacing(8)
        self.combo_downgrade_channel = QComboBox()
        self.combo_downgrade_channel.addItem("ADB", "adb")
        self.combo_downgrade_channel.addItem("SSH", "ssh")
        self.combo_downgrade_channel.addItem("串口", "serial")
        channel_default = getattr(m, 'DOWNGRADE_TRIGGER_CHANNEL', 'adb')
        channel_index = self.combo_downgrade_channel.findData(channel_default)
        self.combo_downgrade_channel.setCurrentIndex(
            channel_index if channel_index >= 0 else 0)
        fc.addRow("命令通道:", self.combo_downgrade_channel)
        self.edit_downgrade_cmd = QPlainTextEdit()
        self.edit_downgrade_cmd.setPlainText(getattr(m, 'DOWNGRADE_TRIGGER_CMD', ''))
        self.edit_downgrade_cmd.setPlaceholderText(
            "每行一条设备侧命令；按顺序执行")
        self.edit_downgrade_cmd.setFixedHeight(76)
        fc.addRow("降级命令:", self.edit_downgrade_cmd)
        self.edit_downgrade_expect = QLineEdit(
            getattr(m, 'DOWNGRADE_TRIGGER_EXPECT', ''))
        self.edit_downgrade_expect.setPlaceholderText("留空=ADB/SSH只看返回码，串口只看发送结果")
        fc.addRow("成功关键词:", self.edit_downgrade_expect)
        self.spin_downgrade_trigger_timeout = self.spin(
            1, 7200, getattr(m, 'DOWNGRADE_TRIGGER_TIMEOUT', 30), "秒")
        fc.addRow("命令超时:", self.spin_downgrade_trigger_timeout)

        self.edit_downgrade_adb_path = QLineEdit(
            getattr(m, 'DOWNGRADE_ADB_PATH', 'adb'))
        fc.addRow("ADB 路径:", self.edit_downgrade_adb_path)
        self.edit_downgrade_adb_serial = QLineEdit(
            getattr(m, 'DOWNGRADE_ADB_SERIAL', ''))
        self.edit_downgrade_adb_serial.setPlaceholderText("留空=使用当前连接设备")
        fc.addRow("ADB 序列号:", self.edit_downgrade_adb_serial)

        self.edit_downgrade_ssh_host = QLineEdit(
            getattr(m, 'DOWNGRADE_SSH_HOST', ''))
        fc.addRow("SSH 主机:", self.edit_downgrade_ssh_host)
        self.spin_downgrade_ssh_port = self.spin(
            1, 65535, getattr(m, 'DOWNGRADE_SSH_PORT', 22))
        fc.addRow("SSH 端口:", self.spin_downgrade_ssh_port)
        self.edit_downgrade_ssh_user = QLineEdit(
            getattr(m, 'DOWNGRADE_SSH_USERNAME', ''))
        fc.addRow("SSH 用户名:", self.edit_downgrade_ssh_user)
        self.edit_downgrade_ssh_password = QLineEdit(
            getattr(m, 'DOWNGRADE_SSH_PASSWORD', ''))
        self.edit_downgrade_ssh_password.setEchoMode(QLineEdit.Password)
        fc.addRow("SSH 密码:", self.edit_downgrade_ssh_password)
        self.edit_downgrade_ssh_key = QLineEdit(
            getattr(m, 'DOWNGRADE_SSH_KEY_PATH', ''))
        self.edit_downgrade_ssh_key.setPlaceholderText("密码和私钥至少配置一项")
        fc.addRow("SSH 私钥路径:", self.edit_downgrade_ssh_key)
        lay.addWidget(grp_cmd)
        self.downgrade_common_command_widgets = [
            self.combo_downgrade_channel, self.edit_downgrade_cmd,
            self.edit_downgrade_expect, self.spin_downgrade_trigger_timeout,
        ]
        self.downgrade_command_widgets = [
            *self.downgrade_common_command_widgets,
            self.edit_downgrade_adb_path, self.edit_downgrade_adb_serial,
            self.edit_downgrade_ssh_host, self.spin_downgrade_ssh_port,
            self.edit_downgrade_ssh_user, self.edit_downgrade_ssh_password,
            self.edit_downgrade_ssh_key,
        ]

        grp4 = QGroupBox("降级后处理")
        f4 = QFormLayout(grp4)
        f4.setSpacing(8)
        self.spin_post_downgrade_wait = self.spin(0, 600, m.POST_DOWNGRADE_WAIT, "秒")
        f4.addRow("降级完成后等待:", self.spin_post_downgrade_wait)
        self.chk_retry_relay = QCheckBox("降级失败重试前用继电器重启设备")
        self.chk_retry_relay.setChecked(m.DOWNGRADE_RETRY_RELAY_RESTART)
        f4.addRow(self.chk_retry_relay)
        self.spin_retry_boot_wait = self.spin(0, 600, m.DOWNGRADE_RETRY_BOOT_WAIT, "秒")
        f4.addRow("继电器重启后等待:", self.spin_retry_boot_wait)
        lay.addWidget(grp4)

        grp5 = QGroupBox("循环")
        f5 = QFormLayout(grp5)
        f5.setSpacing(8)
        self.spin_max_cycles = self.spin(0, 999999, m.MAX_CYCLES)
        self.spin_max_cycles.setToolTip("0=无限循环，直到点击停止")
        f5.addRow("最大循环次数:", self.spin_max_cycles)
        lay.addWidget(grp5)
        lay.addStretch()
        self.tabs.addTab(scroll, "  FOTA / 降级  ")

        self.btn_add_script.clicked.connect(self._on_add_script)
        self.btn_del_script.clicked.connect(self._on_del_script)
        self.btn_up_script.clicked.connect(lambda: self._move_script(-1))
        self.btn_down_script.clicked.connect(lambda: self._move_script(1))
        self.combo_downgrade_mode.currentIndexChanged.connect(
            self._update_downgrade_deps)
        self.combo_downgrade_channel.currentIndexChanged.connect(
            self._update_downgrade_deps)
        self._load_scripts(getattr(m, 'DOWNGRADE_SCRIPTS', []))

    # ---- 降级脚本行 ----
    def _append_script_row(self, path='', success='', fail=''):
        was_suppressed = self._suppress_autosave
        self._suppress_autosave = True
        try:
            row_widget = QWidget()
            row_lay = QVBoxLayout(row_widget)
            row_lay.setContentsMargins(0, 3, 0, 3)
            row_lay.setSpacing(5)

            file_row = QHBoxLayout()
            file_row.setSpacing(6)
            selector = QToolButton()
            selector.setCheckable(True)
            selector.setFixedSize(28, 28)
            self.script_selection_group.addButton(selector)
            file_row.addWidget(selector)

            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.NoInsert)
            combo.setMinimumWidth(120)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
            combo.lineEdit().setPlaceholderText("选择或输入降级脚本路径")
            combo.addItems(self._script_candidates)
            combo.setCurrentText(path)
            file_row.addWidget(combo, 1)

            browse = QToolButton()
            browse.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
            browse.setToolTip("选择降级脚本文件")
            browse.setFixedSize(30, 28)
            file_row.addWidget(browse)
            row_lay.addLayout(file_row)

            keyword_row = QFormLayout()
            keyword_row.setContentsMargins(0, 0, 0, 0)
            keyword_row.setHorizontalSpacing(6)
            keyword_row.setVerticalSpacing(5)
            success_edit = QLineEdit(success)
            success_edit.setPlaceholderText("留空=仅依据返回码")
            keyword_row.addRow("成功关键词:", success_edit)
            fail_edit = QLineEdit(fail)
            fail_edit.setPlaceholderText("留空=不检测")
            keyword_row.addRow("失败关键词:", fail_edit)
            row_lay.addLayout(keyword_row)

            self.script_rows_layout.addWidget(row_widget)
            info = {
                'widget': row_widget,
                'selector': selector,
                'combo': combo,
                'success': success_edit,
                'fail': fail_edit,
                'browse': browse,
            }
            self.script_rows.append(info)

            selector.clicked.connect(
                lambda _checked=False, row_info=info: self._select_script_info(row_info))
            combo.activated.connect(
                lambda _index, row_info=info: self._select_script_info(row_info))
            combo.lineEdit().textEdited.connect(
                lambda _text, row_info=info: self._select_script_info(row_info))
            combo.lineEdit().editingFinished.connect(
                lambda row_info=info: self._remember_script_from_row(row_info))
            success_edit.textEdited.connect(
                lambda _text, row_info=info: self._select_script_info(row_info))
            fail_edit.textEdited.connect(
                lambda _text, row_info=info: self._select_script_info(row_info))
            browse.clicked.connect(
                lambda _checked=False, row_info=info: self._browse_script_info(row_info))

            self.bind_autosave([combo, success_edit, fail_edit])
            self._renumber_script_rows()
            if self._active_script_index < 0:
                self._select_script_row(len(self.script_rows) - 1)
            return info
        finally:
            self._suppress_autosave = was_suppressed

    def _load_scripts(self, scripts):
        """用给定脚本列表重建配置行（保持顺序）。"""
        was_suppressed = self._suppress_autosave
        self._suppress_autosave = True
        try:
            self._clear_script_rows()
            scripts = [s for s in (scripts or []) if isinstance(s, dict)]
            self._script_candidates = self._discover_script_paths(scripts)
            if not scripts:
                scripts = [{'path': '', 'success_keyword': '', 'fail_keyword': ''}]
            for script in scripts:
                self._append_script_row(script.get('path', ''),
                                        script.get('success_keyword', ''),
                                        script.get('fail_keyword', ''))
            self._renumber_script_rows()
            if self.script_rows:
                self._select_script_row(0)
        finally:
            self._suppress_autosave = was_suppressed

    def _clear_script_rows(self):
        if hasattr(self, 'script_selection_group'):
            for button in list(self.script_selection_group.buttons()):
                self.script_selection_group.removeButton(button)
            self.script_selection_group.deleteLater()
        self.script_selection_group = QButtonGroup(self)
        self.script_selection_group.setExclusive(True)

        while self.script_rows_layout.count():
            item = self.script_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.script_rows = []
        self._active_script_index = -1

    def _discover_script_paths(self, scripts):
        candidates = []
        seen = set()

        def add(path):
            path = str(path or '').strip()
            if not path:
                return
            key = os.path.normcase(os.path.normpath(path))
            if key not in seen:
                seen.add(key)
                candidates.append(path)

        search_dirs = []
        for script in scripts:
            path = script.get('path', '')
            add(path)
            directory = os.path.dirname(str(path).strip())
            if directory and os.path.isdir(directory):
                search_dirs.append(directory)

        local_scripts = os.path.join(gc.app_dir(), 'scripts')
        if os.path.isdir(local_scripts):
            search_dirs.append(local_scripts)

        discovered = []
        seen_dirs = set()
        for directory in search_dirs:
            directory = os.path.normpath(directory)
            dir_key = os.path.normcase(directory)
            if dir_key in seen_dirs:
                continue
            seen_dirs.add(dir_key)
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.is_file() and entry.name.lower().endswith('.bat'):
                            discovered.append(entry.path)
            except OSError:
                continue
        for path in sorted(discovered, key=str.casefold):
            add(path)
        return candidates

    def _script_row_index(self, row_info):
        for index, info in enumerate(self.script_rows):
            if info is row_info:
                return index
        return -1

    def _select_script_info(self, row_info):
        index = self._script_row_index(row_info)
        if index >= 0:
            self._select_script_row(index)

    def _select_script_row(self, index):
        if index < 0 or index >= len(self.script_rows):
            return
        self._active_script_index = index
        self.script_rows[index]['selector'].setChecked(True)

    def _renumber_script_rows(self):
        for index, info in enumerate(self.script_rows):
            selector = info['selector']
            selector.setText(str(index + 1))
            selector.setToolTip(f"选择第 {index + 1} 条降级脚本")
            selector.setChecked(index == self._active_script_index)

    def _remember_script_from_row(self, row_info):
        index = self._script_row_index(row_info)
        if index >= 0:
            self._remember_script_path(self.script_rows[index]['combo'].currentText())

    def _remember_script_path(self, path):
        path = path.strip()
        if not path:
            return
        if any(path == candidate for candidate in self._script_candidates):
            return
        self._script_candidates.append(path)
        for info in self.script_rows:
            combo = info['combo']
            if all(combo.itemText(i) != path for i in range(combo.count())):
                current = combo.currentText()
                combo.addItem(path)
                combo.setEditText(current)

    def _on_add_script(self):
        info = self._append_script_row('', '', '')
        self._select_script_info(info)
        info['combo'].setFocus()
        self._on_config_changed()

    def _on_del_script(self):
        index = self._active_script_index
        if index < 0 or index >= len(self.script_rows):
            return
        was_suppressed = self._suppress_autosave
        self._suppress_autosave = True
        try:
            info = self.script_rows.pop(index)
            self.script_selection_group.removeButton(info['selector'])
            self.script_rows_layout.removeWidget(info['widget'])
            info['widget'].deleteLater()
            self._active_script_index = -1
            self._rebuild_script_layout()
            if self.script_rows:
                self._select_script_row(min(index, len(self.script_rows) - 1))
        finally:
            self._suppress_autosave = was_suppressed
        self._on_config_changed()

    def _move_script(self, delta):
        index = self._active_script_index
        target = index + delta
        if index < 0 or target < 0 or target >= len(self.script_rows):
            return
        was_suppressed = self._suppress_autosave
        self._suppress_autosave = True
        try:
            self.script_rows[index], self.script_rows[target] = (
                self.script_rows[target], self.script_rows[index])
            self._rebuild_script_layout()
            self._select_script_row(target)
        finally:
            self._suppress_autosave = was_suppressed
        self._on_config_changed()

    def _rebuild_script_layout(self):
        while self.script_rows_layout.count():
            self.script_rows_layout.takeAt(0)
        for info in self.script_rows:
            self.script_rows_layout.addWidget(info['widget'])
        self._renumber_script_rows()

    def _browse_script_info(self, row_info):
        index = self._script_row_index(row_info)
        if index < 0:
            return
        self._select_script_row(index)
        combo = self.script_rows[index]['combo']
        start = os.path.dirname(combo.currentText().strip()) or gc.app_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择降级脚本", start, "批处理 (*.bat);;所有文件 (*)")
        if path:
            self._remember_script_path(path)
            combo.setEditText(path)
            self._on_config_changed()

    def _row_values(self, row):
        info = self.script_rows[row]
        return [
            info['combo'].currentText(),
            info['success'].text(),
            info['fail'].text(),
        ]

    def _read_scripts(self):
        return [
            {"path": v[0].strip(), "success_keyword": v[1],
             "fail_keyword": v[2]}
            for v in (self._row_values(r) for r in range(len(self.script_rows)))
            if v[0].strip() or v[1] or v[2]
        ]

    def _with_browse(self, edit, file_filter="批处理 (*.bat);;所有文件 (*)"):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(edit, 1)
        btn = QToolButton()
        btn.setText("...")
        btn.setFixedWidth(30)
        btn.clicked.connect(lambda: self._pick_file(edit, file_filter))
        h.addWidget(btn)
        return w

    def _pick_file(self, edit, file_filter="批处理 (*.bat);;所有文件 (*)"):
        start = os.path.dirname(edit.text().strip()) or gc.app_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择文件", start, file_filter)
        if path:
            edit.setText(path)

    # ---------- Tab 校验 ----------
    def _build_tab_check(self):
        m = self.mod_defaults()
        scroll = self.scroll_container()
        lay = scroll.widget().layout()

        grp = QGroupBox("串口命令校验")
        v = QVBoxLayout(grp)
        self.chk_check_serial = QCheckBox("启用串口命令校验")
        self.chk_check_serial.setChecked(getattr(m, 'CHECK_SERIAL_ENABLED', False))
        v.addWidget(self.chk_check_serial)
        f = QFormLayout()
        f.setSpacing(8)
        self.edit_check_serial_cmd = QLineEdit(getattr(m, 'CHECK_SERIAL_CMD', ''))
        f.addRow("发送命令:", self.edit_check_serial_cmd)
        self.edit_check_serial_expect = QLineEdit(getattr(m, 'CHECK_SERIAL_EXPECT', ''))
        self.edit_check_serial_expect.setPlaceholderText("留空=只要命令发出即通过")
        f.addRow("期望输出包含:", self.edit_check_serial_expect)
        self.spin_check_serial_timeout = self.spin(
            1, 600, getattr(m, 'CHECK_SERIAL_TIMEOUT', 20), "秒")
        f.addRow("等待超时:", self.spin_check_serial_timeout)
        v.addLayout(f)
        self.serial_check_widgets = [self.edit_check_serial_cmd,
                                     self.edit_check_serial_expect,
                                     self.spin_check_serial_timeout]
        lay.addWidget(grp)

        grp2 = QGroupBox("ADB 命令校验")
        v2 = QVBoxLayout(grp2)
        self.chk_check_adb = QCheckBox("启用 ADB 命令校验")
        self.chk_check_adb.setChecked(getattr(m, 'CHECK_ADB_ENABLED', False))
        v2.addWidget(self.chk_check_adb)
        f2 = QFormLayout()
        f2.setSpacing(8)
        self.edit_check_adb_path = QLineEdit(getattr(m, 'CHECK_ADB_PATH', 'adb'))
        f2.addRow("adb 路径:", self._with_browse(
            self.edit_check_adb_path, "可执行文件 (*.exe);;所有文件 (*)"))
        self.edit_check_adb_serial = QLineEdit(getattr(m, 'CHECK_ADB_SERIAL', ''))
        self.edit_check_adb_serial.setPlaceholderText("留空=使用唯一连接的设备")
        f2.addRow("设备序列号 (-s):", self.edit_check_adb_serial)
        self.edit_check_adb_cmd = QLineEdit(getattr(m, 'CHECK_ADB_CMD', ''))
        f2.addRow("shell 命令:", self.edit_check_adb_cmd)
        self.edit_check_adb_expect = QLineEdit(getattr(m, 'CHECK_ADB_EXPECT', ''))
        self.edit_check_adb_expect.setPlaceholderText("留空=仅要求返回码为 0")
        f2.addRow("期望输出包含:", self.edit_check_adb_expect)
        self.spin_check_adb_timeout = self.spin(
            1, 600, getattr(m, 'CHECK_ADB_TIMEOUT', 20), "秒")
        f2.addRow("执行超时:", self.spin_check_adb_timeout)
        v2.addLayout(f2)
        self.adb_check_widgets = [self.edit_check_adb_path, self.edit_check_adb_serial,
                                  self.edit_check_adb_cmd, self.edit_check_adb_expect,
                                  self.spin_check_adb_timeout]
        lay.addWidget(grp2)

        hint = QLabel("两项可同时启用；任一启用项失败，本轮结果记为 VERIFY_FAIL。")
        hint.setStyleSheet("color:#b2bec3;")
        lay.addWidget(hint)
        lay.addStretch()
        self.tabs.addTab(scroll, "  校验  ")

        self.chk_check_serial.stateChanged.connect(self._update_check_deps)
        self.chk_check_adb.stateChanged.connect(self._update_check_deps)

    # ---------- Tab MSH ----------
    def _build_tab_msh(self):
        m = self.mod_defaults()
        scroll = self.scroll_container()
        lay = scroll.widget().layout()

        grp = QGroupBox("MSH 认证 (AUTH SEED 挑战-应答)")
        v = QVBoxLayout(grp)
        self.chk_msh_enabled = QCheckBox("启用 MSH 认证")
        self.chk_msh_enabled.setChecked(m.MSH_AUTH_ENABLED)
        v.addWidget(self.chk_msh_enabled)
        f = QFormLayout()
        f.setSpacing(8)
        self.edit_pem = QLineEdit(m.MSH_PEM_KEY_PATH)
        f.addRow("私钥 PEM:", self._with_pem_browse(self.edit_pem))
        self.edit_seed_pattern = QLineEdit(m.MSH_AUTH_SEED_PATTERN)
        f.addRow("AUTH SEED 正则:", self.edit_seed_pattern)
        self.edit_post_auth_cmd = QLineEdit(m.MSH_POST_AUTH_CMD)
        f.addRow("认证后执行命令:", self.edit_post_auth_cmd)
        self.spin_msh_timeout = self.spin(1, 300, m.MSH_AUTH_TIMEOUT, "秒")
        f.addRow("等待 AUTH SEED 超时:", self.spin_msh_timeout)
        self.edit_msh_ok_flag = QLineEdit(m.MSH_AUTH_SUCCESS_FLAG)
        self.edit_msh_ok_flag.setPlaceholderText("留空=不校验成功标志")
        f.addRow("认证成功标志:", self.edit_msh_ok_flag)
        self.spin_msh_ready_wait = self.spin(0, 300, m.MSH_AUTH_READY_WAIT, "秒")
        f.addRow("认证后等待可执行:", self.spin_msh_ready_wait)
        self.edit_msh_fail_flags = QLineEdit(','.join(m.MSH_AUTH_FAIL_FLAGS))
        f.addRow("认证失败标志(逗号分隔):", self.edit_msh_fail_flags)
        v.addLayout(f)
        self.msh_widgets = [self.edit_pem, self.edit_seed_pattern, self.edit_post_auth_cmd,
                            self.spin_msh_timeout, self.edit_msh_ok_flag,
                            self.spin_msh_ready_wait, self.edit_msh_fail_flags]
        lay.addWidget(grp)

        grp2 = QGroupBox("SN 缺失自动写入")
        f2 = QFormLayout(grp2)
        f2.setSpacing(8)
        self.edit_sn_flag = QLineEdit(m.SN_MISSING_LOG_FLAG)
        f2.addRow("SN 缺失日志标志:", self.edit_sn_flag)
        lay.addWidget(grp2)
        lay.addStretch()
        self.tabs.addTab(scroll, "  MSH  ")
        self.chk_msh_enabled.stateChanged.connect(self._update_msh_deps)

    def _with_pem_browse(self, edit):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(edit, 1)
        btn = QToolButton()
        btn.setText("...")
        btn.setFixedWidth(30)
        btn.clicked.connect(lambda: self._pick_pem(edit))
        h.addWidget(btn)
        return w

    def _pick_pem(self, edit):
        start = os.path.dirname(edit.text().strip()) or gc.app_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择私钥 PEM", start, "PEM 私钥 (*.pem);;所有文件 (*)")
        if path:
            edit.setText(path)

    # ---------- Tab 日志 ----------
    def _build_tab_log(self):
        m = self.mod_defaults()
        container = QWidget()
        lay = QVBoxLayout(container)

        grp = QGroupBox("日志根目录 (留空=程序目录/logs/fota_<时间戳>/)")
        h = QHBoxLayout(grp)
        self.edit_log_base = QLineEdit("")
        self.edit_log_base.setPlaceholderText("留空则保存在程序运行目录")
        h.addWidget(self.edit_log_base)
        btn = QPushButton("浏览...")
        btn.setFixedWidth(80)
        btn.clicked.connect(self._browse_log_dir)
        h.addWidget(btn)
        lay.addWidget(grp)

        grp2 = QGroupBox("本次运行将生成的文件")
        f = QFormLayout(grp2)
        f.setSpacing(6)
        f.addRow("主程序日志:", QLabel("fota_test.log"))
        f.addRow("串口日志:", QLabel("fota_serial_output.log"))
        f.addRow("降级日志:", QLabel("fota_downgrade.log"))
        f.addRow("降级状态快照:", QLabel("fota_downgrade_status.txt"))
        f.addRow("降级脚本原始输出:", QLabel("bat_output/*.log"))
        f.addRow("测试报告:", QLabel("fota_test_report.txt"))
        lay.addWidget(grp2)
        lay.addStretch()
        self.tabs.addTab(container, "  日志  ")

    def _browse_log_dir(self):
        current = self.edit_log_base.text().strip() or gc.app_dir()
        d = QFileDialog.getExistingDirectory(self, "选择日志根目录", current)
        if d:
            self.edit_log_base.setText(d)

    # ==================== 依赖 ====================
    def _update_all_deps(self):
        self._update_relay_deps()
        self._update_msh_deps()
        self._update_check_deps()
        self._update_downgrade_deps()

    def _update_downgrade_deps(self):
        mode = self.combo_downgrade_mode.currentData() or 'jflash'
        self.grp_downgrade_jflash.setVisible(mode == 'jflash')
        self.grp_downgrade_command.setVisible(mode == 'command')
        channel = self.combo_downgrade_channel.currentData() or 'adb'
        channel_widgets = {
            'adb': [self.edit_downgrade_adb_path, self.edit_downgrade_adb_serial],
            'ssh': [
                self.edit_downgrade_ssh_host, self.spin_downgrade_ssh_port,
                self.edit_downgrade_ssh_user, self.edit_downgrade_ssh_password,
                self.edit_downgrade_ssh_key,
            ],
            'serial': [],
        }
        form = self.grp_downgrade_command.layout()
        for widget in self.downgrade_command_widgets:
            visible = (
                widget in self.downgrade_common_command_widgets or
                widget in channel_widgets.get(channel, [])
            )
            widget.setVisible(visible)
            label = form.labelForField(widget)
            if label:
                label.setVisible(visible)

    def _update_relay_deps(self):
        on = self.chk_relay_enabled.isChecked()
        for w in self.relay_widgets:
            w.setEnabled(on)

    def _update_msh_deps(self):
        on = self.chk_msh_enabled.isChecked()
        for w in self.msh_widgets:
            w.setEnabled(on)

    def _update_check_deps(self):
        for w in self.serial_check_widgets:
            w.setEnabled(self.chk_check_serial.isChecked())
        for w in self.adb_check_widgets:
            w.setEnabled(self.chk_check_adb.isChecked())

    # ==================== 配置 ====================
    def collect(self):
        return {
            'DUT_COM_PORT': self.combo_dut_port.currentText(),
            'DUT_BAUDRATE': self.spin_baud.value(),
            'RELAY_ENABLED': self.chk_relay_enabled.isChecked(),
            'RELAY_COM_PORT': self.combo_relay_port.currentText(),
            'RELAY_BAUDRATE': self.spin_relay_baud.value(),
            'RELAY_POWER_OFF_TIME': self.spin_relay_off_time.value(),
            'RELAY_POWER_ON_WAIT': self.spin_relay_on_wait.value(),
            'OTFDEC_ERROR_FLAG': self.edit_otfdec.text(),
            'BOOT_VERSION': self.edit_boot_ver.text(),
            'UPGRADE_VERSION': self.edit_upgrade_ver.text(),
            'USE_REGEX': self.chk_use_regex.isChecked(),
            'BOOT_FLAG': self.edit_boot_flag.text(),
            'BOOT_TIMEOUT': self.spin_boot_timeout.value(),
            'BOOT_VERSION_DETECT_TIMEOUT': self.spin_boot_ver_timeout.value(),
            'FOTA_UPGRADE_START_FLAG': self.edit_fota_start_flag.text(),
            'FOTA_UPGRADE_COMPLETE_FLAG': self.edit_fota_done_flag.text(),
            'FOTA_UPGRADE_TIMEOUT': self.spin_fota_timeout.value(),
            'FOTA_RETRY_MAX': self.spin_fota_retry.value(),
            'MAX_CYCLES': self.spin_max_cycles.value(),
            'DOWNGRADE_MODE': self.combo_downgrade_mode.currentData() or 'jflash',
            'DOWNGRADE_TRIGGER_BOOT_WAIT': self.spin_downgrade_boot_wait.value(),
            'DOWNGRADE_TRIGGER_CHANNEL': self.combo_downgrade_channel.currentData() or 'adb',
            'DOWNGRADE_TRIGGER_CMD': self.edit_downgrade_cmd.toPlainText(),
            'DOWNGRADE_TRIGGER_EXPECT': self.edit_downgrade_expect.text(),
            'DOWNGRADE_TRIGGER_TIMEOUT': self.spin_downgrade_trigger_timeout.value(),
            'DOWNGRADE_ADB_PATH': self.edit_downgrade_adb_path.text(),
            'DOWNGRADE_ADB_SERIAL': self.edit_downgrade_adb_serial.text(),
            'DOWNGRADE_SSH_HOST': self.edit_downgrade_ssh_host.text(),
            'DOWNGRADE_SSH_PORT': self.spin_downgrade_ssh_port.value(),
            'DOWNGRADE_SSH_USERNAME': self.edit_downgrade_ssh_user.text(),
            'DOWNGRADE_SSH_PASSWORD': self.edit_downgrade_ssh_password.text(),
            'DOWNGRADE_SSH_KEY_PATH': self.edit_downgrade_ssh_key.text(),
            'DOWNGRADE_SCRIPTS': self._read_scripts(),
            'DOWNGRADE_TIMEOUT': self.spin_downgrade_timeout.value(),
            'DOWNGRADE_SCRIPT_RETRIES': self.spin_script_retries.value(),
            'DOWNGRADE_MAX_ROUNDS': self.spin_max_rounds.value(),
            'POST_DOWNGRADE_WAIT': self.spin_post_downgrade_wait.value(),
            'DOWNGRADE_RETRY_RELAY_RESTART': self.chk_retry_relay.isChecked(),
            'DOWNGRADE_RETRY_BOOT_WAIT': self.spin_retry_boot_wait.value(),
            'MSH_AUTH_ENABLED': self.chk_msh_enabled.isChecked(),
            'MSH_PEM_KEY_PATH': self.edit_pem.text(),
            'MSH_AUTH_SEED_PATTERN': self.edit_seed_pattern.text(),
            'MSH_POST_AUTH_CMD': self.edit_post_auth_cmd.text(),
            'MSH_AUTH_TIMEOUT': self.spin_msh_timeout.value(),
            'MSH_AUTH_SUCCESS_FLAG': self.edit_msh_ok_flag.text(),
            'MSH_AUTH_READY_WAIT': self.spin_msh_ready_wait.value(),
            'MSH_AUTH_FAIL_FLAGS': self.edit_msh_fail_flags.text(),
            'SN_MISSING_LOG_FLAG': self.edit_sn_flag.text(),
            'CHECK_SERIAL_ENABLED': self.chk_check_serial.isChecked(),
            'CHECK_SERIAL_CMD': self.edit_check_serial_cmd.text(),
            'CHECK_SERIAL_EXPECT': self.edit_check_serial_expect.text(),
            'CHECK_SERIAL_TIMEOUT': self.spin_check_serial_timeout.value(),
            'CHECK_ADB_ENABLED': self.chk_check_adb.isChecked(),
            'CHECK_ADB_PATH': self.edit_check_adb_path.text(),
            'CHECK_ADB_SERIAL': self.edit_check_adb_serial.text(),
            'CHECK_ADB_CMD': self.edit_check_adb_cmd.text(),
            'CHECK_ADB_EXPECT': self.edit_check_adb_expect.text(),
            'CHECK_ADB_TIMEOUT': self.spin_check_adb_timeout.value(),
            'LOG_BASE': self.edit_log_base.text(),
        }

    def apply_config(self, cfg):
        g = cfg.get
        self.combo_dut_port.setEditText(g('DUT_COM_PORT', 'COM9'))
        self.spin_baud.setValue(g('DUT_BAUDRATE', 115200))
        self.chk_relay_enabled.setChecked(g('RELAY_ENABLED', True))
        self.combo_relay_port.setEditText(g('RELAY_COM_PORT', 'COM8'))
        self.spin_relay_baud.setValue(g('RELAY_BAUDRATE', 115200))
        self.spin_relay_off_time.setValue(g('RELAY_POWER_OFF_TIME', 5))
        self.spin_relay_on_wait.setValue(g('RELAY_POWER_ON_WAIT', 3))
        self.edit_otfdec.setText(g('OTFDEC_ERROR_FLAG', ''))
        self.edit_boot_ver.setText(g('BOOT_VERSION', ''))
        self.edit_upgrade_ver.setText(g('UPGRADE_VERSION', ''))
        self.chk_use_regex.setChecked(g('USE_REGEX', False))
        self.edit_boot_flag.setText(g('BOOT_FLAG', ''))
        self.spin_boot_timeout.setValue(g('BOOT_TIMEOUT', 180))
        self.spin_boot_ver_timeout.setValue(g('BOOT_VERSION_DETECT_TIMEOUT', 30))
        self.edit_fota_start_flag.setText(g('FOTA_UPGRADE_START_FLAG', ''))
        self.edit_fota_done_flag.setText(g('FOTA_UPGRADE_COMPLETE_FLAG', ''))
        self.spin_fota_timeout.setValue(g('FOTA_UPGRADE_TIMEOUT', 600))
        self.spin_fota_retry.setValue(g('FOTA_RETRY_MAX', 3))
        self.spin_max_cycles.setValue(g('MAX_CYCLES', 0))
        scripts = cfg.get('DOWNGRADE_SCRIPTS')
        if not isinstance(scripts, list):
            # 老配置迁移：由 DOWNGRADE_BAT_1/2 + 四个关键字键合成脚本列表
            scripts = []
            for path_key, ok_key, fail_key in [
                    ('DOWNGRADE_BAT_1', 'DOWNGRADE_SUCCESS_KEYWORD_1', 'DOWNGRADE_FAIL_KEYWORD_1'),
                    ('DOWNGRADE_BAT_2', 'DOWNGRADE_SUCCESS_KEYWORD_2', 'DOWNGRADE_FAIL_KEYWORD_2')]:
                path = g(path_key, '')
                if path:
                    scripts.append({'path': path,
                                    'success_keyword': g(ok_key, ''),
                                    'fail_keyword': g(fail_key, '')})
        self._load_scripts(scripts)
        mode_value = g('DOWNGRADE_MODE', 'jflash')
        mode_index = self.combo_downgrade_mode.findData(mode_value)
        self.combo_downgrade_mode.setCurrentIndex(mode_index if mode_index >= 0 else 0)
        self.spin_downgrade_boot_wait.setValue(
            g('DOWNGRADE_TRIGGER_BOOT_WAIT', 10))
        channel_value = g('DOWNGRADE_TRIGGER_CHANNEL', 'adb')
        channel_index = self.combo_downgrade_channel.findData(channel_value)
        self.combo_downgrade_channel.setCurrentIndex(
            channel_index if channel_index >= 0 else 0)
        self.edit_downgrade_cmd.setPlainText(g('DOWNGRADE_TRIGGER_CMD', ''))
        self.edit_downgrade_expect.setText(g('DOWNGRADE_TRIGGER_EXPECT', ''))
        self.spin_downgrade_trigger_timeout.setValue(
            g('DOWNGRADE_TRIGGER_TIMEOUT', 30))
        self.edit_downgrade_adb_path.setText(g('DOWNGRADE_ADB_PATH', 'adb'))
        self.edit_downgrade_adb_serial.setText(g('DOWNGRADE_ADB_SERIAL', ''))
        self.edit_downgrade_ssh_host.setText(g('DOWNGRADE_SSH_HOST', ''))
        self.spin_downgrade_ssh_port.setValue(g('DOWNGRADE_SSH_PORT', 22))
        self.edit_downgrade_ssh_user.setText(g('DOWNGRADE_SSH_USERNAME', ''))
        self.edit_downgrade_ssh_password.setText(g('DOWNGRADE_SSH_PASSWORD', ''))
        self.edit_downgrade_ssh_key.setText(g('DOWNGRADE_SSH_KEY_PATH', ''))
        self.spin_downgrade_timeout.setValue(g('DOWNGRADE_TIMEOUT', 300))
        self.spin_script_retries.setValue(g('DOWNGRADE_SCRIPT_RETRIES', 3))
        self.spin_max_rounds.setValue(g('DOWNGRADE_MAX_ROUNDS', 3))
        self.spin_post_downgrade_wait.setValue(g('POST_DOWNGRADE_WAIT', 5))
        self.chk_retry_relay.setChecked(g('DOWNGRADE_RETRY_RELAY_RESTART', True))
        self.spin_retry_boot_wait.setValue(g('DOWNGRADE_RETRY_BOOT_WAIT', 10))
        self.chk_msh_enabled.setChecked(g('MSH_AUTH_ENABLED', True))
        self.edit_pem.setText(g('MSH_PEM_KEY_PATH', ''))
        self.edit_seed_pattern.setText(g('MSH_AUTH_SEED_PATTERN', r'AUTH SEED:\s*([0-9a-fA-F]+)'))
        self.edit_post_auth_cmd.setText(g('MSH_POST_AUTH_CMD', ''))
        self.spin_msh_timeout.setValue(g('MSH_AUTH_TIMEOUT', 15))
        self.edit_msh_ok_flag.setText(g('MSH_AUTH_SUCCESS_FLAG', ''))
        self.spin_msh_ready_wait.setValue(g('MSH_AUTH_READY_WAIT', 5))
        self.edit_msh_fail_flags.setText(g('MSH_AUTH_FAIL_FLAGS', ''))
        self.edit_sn_flag.setText(g('SN_MISSING_LOG_FLAG', ''))
        self.chk_check_serial.setChecked(g('CHECK_SERIAL_ENABLED', False))
        self.edit_check_serial_cmd.setText(g('CHECK_SERIAL_CMD', 'dev_info_get'))
        self.edit_check_serial_expect.setText(g('CHECK_SERIAL_EXPECT', ''))
        self.spin_check_serial_timeout.setValue(g('CHECK_SERIAL_TIMEOUT', 20))
        self.chk_check_adb.setChecked(g('CHECK_ADB_ENABLED', False))
        self.edit_check_adb_path.setText(g('CHECK_ADB_PATH', 'adb'))
        self.edit_check_adb_serial.setText(g('CHECK_ADB_SERIAL', ''))
        self.edit_check_adb_cmd.setText(g('CHECK_ADB_CMD', 'getprop ro.build.version'))
        self.edit_check_adb_expect.setText(g('CHECK_ADB_EXPECT', ''))
        self.spin_check_adb_timeout.setValue(g('CHECK_ADB_TIMEOUT', 20))
        self.edit_log_base.setText(g('LOG_BASE', ''))
        self._update_all_deps()

    def refresh_project(self):
        """Follow the active project's serial, boot, MSH, and ADB settings."""
        project = gc.get_active_project()
        serial = gc.project_serial_port(project, 0)

        self._suppress_autosave = True
        try:
            self.combo_dut_port.setEditText(str(serial.get('port', '') or ''))
            if serial:
                self.spin_baud.setValue(int(serial.get('baudrate', 115200)))

            if project.get('boot_detect_enabled'):
                self.edit_boot_flag.setText(str(project.get('boot_keyword', '') or ''))
                self.spin_boot_timeout.setValue(int(project.get('boot_timeout', 180)))

            msh = gc.project_msh_config(project)
            self.chk_msh_enabled.setChecked(bool(msh.get('enabled', False)))
            self.edit_pem.setText(str(msh.get('pem', '') or ''))
            self.edit_seed_pattern.setText(str(msh.get('seed_pattern', '') or ''))
            self.edit_post_auth_cmd.setText(str(msh.get('post_auth_cmd', '') or ''))
            self.spin_msh_timeout.setValue(int(msh.get('timeout', 15)))
            self.edit_msh_ok_flag.setText(str(msh.get('success_flag', '') or ''))
            self.spin_msh_ready_wait.setValue(int(msh.get('ready_wait', 5)))
            self.edit_msh_fail_flags.setText(str(msh.get('fail_flags', '') or ''))

            self.edit_check_adb_path.setText(str(project.get('adb_path', 'adb') or 'adb'))
            self.edit_check_adb_serial.setText(str(project.get('adb_serial', '') or ''))

            self.edit_downgrade_adb_path.setText(
                str(project.get('adb_path', 'adb') or 'adb'))
            self.edit_downgrade_adb_serial.setText(
                str(project.get('adb_serial', '') or ''))
            ssh = gc.project_ssh_config(project)
            if ssh.get('enabled'):
                self.edit_downgrade_ssh_host.setText(str(ssh.get('host', '') or ''))
                self.spin_downgrade_ssh_port.setValue(int(ssh.get('port', 22)))
                self.edit_downgrade_ssh_user.setText(str(ssh.get('username', '') or ''))
                self.edit_downgrade_ssh_password.setText(str(ssh.get('password', '') or ''))
        finally:
            self._suppress_autosave = False
        self._update_all_deps()

    def _save_config(self):
        import json
        path, _ = QFileDialog.getSaveFileName(
            self, "导出配置文件", os.path.join(gc.app_dir(), "fota_config.json"),
            "JSON 配置文件 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.collect(), f, ensure_ascii=False, indent=2)
            self.append_log(f"配置已导出到: {path}", '#74b9ff')
        except Exception as ex:
            self.append_log(f"导出配置失败: {ex}")

    def _load_config(self):
        import json
        path, _ = QFileDialog.getOpenFileName(
            self, "导入配置文件", gc.app_dir(), "JSON 配置文件 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            self._suppress_autosave = True
            self.apply_config(cfg)
            self._suppress_autosave = False
            self.append_log(f"配置已导入: {path}", '#74b9ff')
        except Exception as ex:
            self._suppress_autosave = False
            self.append_log(f"导入配置失败: {ex}")

    # ==================== 写回模块 ====================
    def _apply_to_module(self, m):
        c = self.collect()
        m.DUT_COM_PORT = c['DUT_COM_PORT'].strip()
        m.DUT_BAUDRATE = c['DUT_BAUDRATE']
        m.RELAY_ENABLED = c['RELAY_ENABLED']
        m.RELAY_COM_PORT = c['RELAY_COM_PORT'].strip()
        m.RELAY_BAUDRATE = c['RELAY_BAUDRATE']
        m.RELAY_POWER_OFF_TIME = c['RELAY_POWER_OFF_TIME']
        m.RELAY_POWER_ON_WAIT = c['RELAY_POWER_ON_WAIT']
        m.OTFDEC_ERROR_FLAG = c['OTFDEC_ERROR_FLAG']
        m.BOOT_VERSION = c['BOOT_VERSION']
        m.UPGRADE_VERSION = c['UPGRADE_VERSION']
        m.USE_REGEX = c['USE_REGEX']
        m.BOOT_FLAG = c['BOOT_FLAG']
        m.BOOT_TIMEOUT = c['BOOT_TIMEOUT']
        m.BOOT_VERSION_DETECT_TIMEOUT = c['BOOT_VERSION_DETECT_TIMEOUT']
        m.FOTA_UPGRADE_START_FLAG = c['FOTA_UPGRADE_START_FLAG']
        m.FOTA_UPGRADE_COMPLETE_FLAG = c['FOTA_UPGRADE_COMPLETE_FLAG']
        m.FOTA_UPGRADE_TIMEOUT = c['FOTA_UPGRADE_TIMEOUT']
        m.FOTA_RETRY_MAX = c['FOTA_RETRY_MAX']
        m.MAX_CYCLES = c['MAX_CYCLES']
        m.DOWNGRADE_MODE = c['DOWNGRADE_MODE']
        m.DOWNGRADE_TRIGGER_BOOT_WAIT = c['DOWNGRADE_TRIGGER_BOOT_WAIT']
        m.DOWNGRADE_TRIGGER_CHANNEL = c['DOWNGRADE_TRIGGER_CHANNEL']
        m.DOWNGRADE_TRIGGER_CMD = c['DOWNGRADE_TRIGGER_CMD']
        m.DOWNGRADE_TRIGGER_EXPECT = c['DOWNGRADE_TRIGGER_EXPECT']
        m.DOWNGRADE_TRIGGER_TIMEOUT = c['DOWNGRADE_TRIGGER_TIMEOUT']
        m.DOWNGRADE_ADB_PATH = c['DOWNGRADE_ADB_PATH'].strip() or 'adb'
        m.DOWNGRADE_ADB_SERIAL = c['DOWNGRADE_ADB_SERIAL'].strip()
        m.DOWNGRADE_SSH_HOST = c['DOWNGRADE_SSH_HOST'].strip()
        m.DOWNGRADE_SSH_PORT = c['DOWNGRADE_SSH_PORT']
        m.DOWNGRADE_SSH_USERNAME = c['DOWNGRADE_SSH_USERNAME'].strip()
        m.DOWNGRADE_SSH_PASSWORD = c['DOWNGRADE_SSH_PASSWORD']
        m.DOWNGRADE_SSH_KEY_PATH = c['DOWNGRADE_SSH_KEY_PATH'].strip()
        m.DOWNGRADE_SCRIPTS = [dict(s) for s in c['DOWNGRADE_SCRIPTS']]
        # 兼容别名同步（外部脚本可能 import 这些名字）
        for i, (bkey, okkey, failkey) in enumerate([
                ('DOWNGRADE_BAT_1', 'DOWNGRADE_SUCCESS_KEYWORD_1', 'DOWNGRADE_FAIL_KEYWORD_1'),
                ('DOWNGRADE_BAT_2', 'DOWNGRADE_SUCCESS_KEYWORD_2', 'DOWNGRADE_FAIL_KEYWORD_2')]):
            script = m.DOWNGRADE_SCRIPTS[i] if i < len(m.DOWNGRADE_SCRIPTS) else {}
            setattr(m, bkey, script.get('path', ''))
            setattr(m, okkey, script.get('success_keyword', ''))
            setattr(m, failkey, script.get('fail_keyword', ''))
        m.DOWNGRADE_TIMEOUT = c['DOWNGRADE_TIMEOUT']
        m.DOWNGRADE_SCRIPT_RETRIES = c['DOWNGRADE_SCRIPT_RETRIES']
        m.DOWNGRADE_MAX_ROUNDS = c['DOWNGRADE_MAX_ROUNDS']
        m.POST_DOWNGRADE_WAIT = c['POST_DOWNGRADE_WAIT']
        m.DOWNGRADE_RETRY_RELAY_RESTART = c['DOWNGRADE_RETRY_RELAY_RESTART']
        m.DOWNGRADE_RETRY_BOOT_WAIT = c['DOWNGRADE_RETRY_BOOT_WAIT']
        m.MSH_AUTH_ENABLED = c['MSH_AUTH_ENABLED']
        m.MSH_PEM_KEY_PATH = c['MSH_PEM_KEY_PATH'].strip()
        m.MSH_AUTH_SEED_PATTERN = c['MSH_AUTH_SEED_PATTERN']
        m.MSH_POST_AUTH_CMD = c['MSH_POST_AUTH_CMD']
        m.MSH_AUTH_TIMEOUT = c['MSH_AUTH_TIMEOUT']
        m.MSH_AUTH_SUCCESS_FLAG = c['MSH_AUTH_SUCCESS_FLAG']
        m.MSH_AUTH_READY_WAIT = c['MSH_AUTH_READY_WAIT']
        m.MSH_AUTH_FAIL_FLAGS = tuple(x.strip() for x in c['MSH_AUTH_FAIL_FLAGS'].split(',') if x.strip())
        m.SN_MISSING_LOG_FLAG = c['SN_MISSING_LOG_FLAG']
        m.CHECK_SERIAL_ENABLED = c['CHECK_SERIAL_ENABLED']
        m.CHECK_SERIAL_CMD = c['CHECK_SERIAL_CMD']
        m.CHECK_SERIAL_EXPECT = c['CHECK_SERIAL_EXPECT']
        m.CHECK_SERIAL_TIMEOUT = c['CHECK_SERIAL_TIMEOUT']
        m.CHECK_ADB_ENABLED = c['CHECK_ADB_ENABLED']
        m.CHECK_ADB_PATH = c['CHECK_ADB_PATH'].strip() or 'adb'
        m.CHECK_ADB_SERIAL = c['CHECK_ADB_SERIAL'].strip()
        m.CHECK_ADB_CMD = c['CHECK_ADB_CMD']
        m.CHECK_ADB_EXPECT = c['CHECK_ADB_EXPECT']
        m.CHECK_ADB_TIMEOUT = c['CHECK_ADB_TIMEOUT']

        return m.set_log_dir(make_run_log_dir("fota", c['LOG_BASE']))

    # ==================== 启动 / 停止 ====================
    def _on_start(self):
        if not self.try_acquire():
            return
        try:
            m = self.mod_defaults()
            log_dir = self._apply_to_module(m)
            project = gc.get_active_project() or {}
            device_session.start_logging(
                project, os.path.join(log_dir, "device_log.log"))

            self.clear_log()
            self._start_time = time.time()
            self._reset_stats_display()

            self.attach_logging(os.path.join(log_dir, "fota_test.log"))

            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self.tabs.setEnabled(False)
            self.card_status.set_value("运行中", "#00b894")

            max_cyc = self.spin_max_cycles.value()
            if max_cyc > 0:
                self.progress_bar.setRange(0, max_cyc)
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat("第 0 次 / 共 %d 次" % max_cyc)
            else:
                self.progress_bar.setRange(0, 0)
                self.progress_bar.setFormat("运行中...")

            self._status_timer.start(1000)
            self.worker = FotaWorker(m)
            self.worker.finished_signal.connect(self._on_finished)
            self.worker.error_signal.connect(lambda msg: self.append_log(f"测试出错: {msg}"))
            self.worker.start()
        except Exception:
            device_session.stop_logging()
            self.detach_logging()
            self.append_log(f"启动测试失败:\n{traceback.format_exc()}")
            self._on_finished()

    def prepare_stop(self):
        if self.worker:
            self.worker.stop()

    def _on_stop(self):
        if self.worker:
            self.worker.stop()
            self.append_log("正在停止测试...", '#fdcb6e')
            self.btn_stop.setEnabled(False)
            self.card_status.set_value("停止中...", '#fdcb6e')

    def _on_finished(self):
        self._status_timer.stop()
        self.detach_logging()
        device_session.stop_logging()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.tabs.setEnabled(True)
        self.worker = None
        self.release_run()
        self.card_status.set_value("已结束", "#636e72")
        self.append_log("测试已结束", '#74b9ff', bold=True)

    def _reset_stats_display(self):
        self.card_cycle.set_value("0", "#74b9ff")
        self.card_pass.set_value("0", "#00b894")
        self.card_fail.set_value("0", "#e17055")
        self.card_rate.set_value("--", "#fdcb6e")
        self.card_stage.set_value("--", "#a29bfe")
        self.card_msg.set_value("--", "#a29bfe")
        self.card_elapsed.set_value("00:00:00", "#b2bec3")

    def _update_elapsed(self):
        if self._start_time:
            self.card_elapsed.set_value(gc.format_duration(time.time() - self._start_time))

    def on_log_line(self, text):
        m = re.search(r'第\s*(\d+)\s*次循环', text)
        if m:
            cyc = int(m.group(1))
            self.card_cycle.set_value(str(cyc), "#74b9ff")
            max_cyc = self.spin_max_cycles.value()
            if max_cyc > 0:
                self.progress_bar.setValue(min(cyc, max_cyc))
                self.progress_bar.setFormat(f"第 {cyc} 次 / 共 {max_cyc} 次")
            else:
                self.progress_bar.setFormat(f"第 {cyc} 次（无限循环）")

        if '阶段1' in text:
            self.card_stage.set_value("升级", "#74b9ff")
        elif '阶段2' in text:
            self.card_stage.set_value("降级", "#fdcb6e")
        elif '阶段3' in text:
            self.card_stage.set_value("MSH 配置", "#a29bfe")

        if 'FOTA 升级验证通过' in text:
            self.card_msg.set_value("升级验证通过", "#00b894")
        elif 'JFLASH 降级执行成功' in text:
            self.card_msg.set_value("降级成功", "#00b894")
        elif 'MSH 认证通过' in text:
            self.card_msg.set_value("MSH 认证通过", "#00b894")
        elif '升级未完成' in text or '开机失败' in text or '降级执行失败' in text or '认证失败' in text:
            self.card_msg.set_value("本轮失败", "#e17055")

        sm = re.search(r'累计统计: 总计 (\d+) 次 \| 成功 (\d+) 次 \| 失败 (\d+) 次', text)
        if sm:
            total, ok, bad = int(sm.group(1)), int(sm.group(2)), int(sm.group(3))
            self.card_pass.set_value(str(ok), "#00b894")
            self.card_fail.set_value(str(bad), "#e17055")
            if total > 0:
                rate = ok / total * 100
                color = '#00b894' if rate >= 90 else ('#fdcb6e' if rate >= 70 else '#e17055')
                self.card_rate.set_value(f"{rate:.1f}%", color)
            if self.is_running:
                self.title_changed.emit(f"第{self.card_cycle._value.text()}次 | 成功:{ok} 失败:{bad}")
