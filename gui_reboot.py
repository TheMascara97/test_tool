# -*- coding: utf-8 -*-
"""
开关机测试页面 — 由原 test_gui.py 迁移，沿用 reboot_test.py 的配置全局变量。
"""

import json
import os
import re
import time
import traceback

from PyQt5.QtWidgets import (
    QWidget, QTabWidget, QGroupBox, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QComboBox, QCheckBox, QPushButton, QGridLayout,
    QProgressBar, QFileDialog
)
from PyQt5.QtCore import QThread, QTimer, QRegExp, pyqtSignal
from PyQt5.QtGui import QRegExpValidator

import gui_common as gc
import device_session
from gui_common import BasePage, StatCard, make_port_combo, refresh_port_combo, make_run_log_dir


class RebootWorker(QThread):
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, ecu, parent=None):
        super().__init__(parent)
        self.ecu = ecu
        self.test_instance = None

    def run(self):
        try:
            self.test_instance = self.ecu.PowerCycleTest()
            self.test_instance.run()
        except Exception:
            self.error_signal.emit(traceback.format_exc())
        finally:
            self.finished_signal.emit()

    def stop(self):
        if self.test_instance:
            self.test_instance.running = False


class RebootPage(BasePage):
    def __init__(self, parent=None):
        super().__init__("reboot", "开关机测试", parent)
        self.ecu = None
        self.worker = None
        self._stat_cycle = 0
        self._stat_pass = 0
        self._stat_fail = 0
        self._boot_times = []
        self._verdict_cycles = set()
        self._start_time = 0
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._update_elapsed)
        self._build_ui()
        self._update_all_deps()
        self.restore_config()

    # ==================== UI ====================
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ── 左侧: 配置 + 按钮 ──
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)

        self.tabs = QTabWidget()
        left_lay.addWidget(self.tabs, 1)
        self._build_tab_serial()
        self._build_tab_network()
        self._build_tab_adb()
        self._build_tab_relay()
        self._build_tab_log()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.btn_start = QPushButton("▶  开始测试")
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

        # ── 右侧: 统计 + 进度 + 日志 ──
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
        self.card_avg_boot = StatCard("平均启动", "--", "#a29bfe")
        self.card_last_boot = StatCard("上次启动", "--", "#a29bfe")
        self.card_elapsed = StatCard("总运行时间", "00:00:00", "#b2bec3")
        cards = [self.card_cycle, self.card_status, self.card_pass, self.card_fail,
                 self.card_rate, self.card_avg_boot, self.card_last_boot, self.card_elapsed]
        for i, c in enumerate(cards):
            grid.addWidget(c, i // 4, i % 4)
        right_lay.addLayout(grid)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("就绪")
        right_lay.addWidget(self.progress_bar)
        right_lay.addWidget(self.build_log_panel(
            with_open_dir=True,
            log_dir_getter=lambda: self.ecu.LOG_DIR if self.ecu else ""), 1)

        root.addWidget(left, 1)
        root.addWidget(right, 2)

        # 信号
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_refresh_ports.clicked.connect(self._refresh_all_ports)
        self.btn_save_config.clicked.connect(self._save_config)
        self.btn_load_config.clicked.connect(self._load_config)

        self.bind_autosave([
            self.spin_boot_timeout, self.spin_max_cycles, self.spin_baudrate,
            self.spin_count_p1, self.spin_count_p2,
            self.spin_ping_pkt, self.spin_ping_interval, self.spin_ping_timeout, self.spin_ping_success,
            self.spin_adb_timeout, self.spin_adb_ping_count, self.spin_adb_ping_success,
            self.spin_relay_baud, self.spin_relay_channel,
            self.spin_power_on_delay, self.spin_min_on_time, self.spin_on_min, self.spin_on_max,
            self.spin_off_wait, self.spin_off_min, self.spin_off_max,
            self.chk_detect_p1, self.chk_log_p1, self.chk_detect_p2, self.chk_log_p2,
            self.chk_ping, self.chk_adb, self.chk_adb_ping,
            self.chk_random_on, self.chk_random_boot_detect, self.chk_random_off,
            self.edit_kw_p1, self.edit_kw_p2,
            self.edit_ping_source, self.edit_ping_target,
            self.edit_adb_path, self.edit_adb_serial, self.edit_adb_user, self.edit_adb_pass,
            self.edit_adb_ping_target,
            self.edit_relay_vid, self.edit_relay_pid, self.edit_hid_on, self.edit_hid_off,
            self.edit_log_dir, self.edit_log_file,
            self.edit_serial_log1, self.edit_serial_log2, self.edit_relay_log,
            self.edit_adb_log, self.edit_report_file,
            self.combo_com1, self.combo_com2, self.combo_relay_com,
            self.combo_port_count, self.combo_relay_type,
            self.relay_cmd_on[1], self.relay_cmd_on[2], self.relay_cmd_on[3], self.relay_cmd_on[4],
            self.relay_cmd_off[1], self.relay_cmd_off[2], self.relay_cmd_off[3], self.relay_cmd_off[4],
        ])

    # ---------- Tab 串口 ----------
    def _build_tab_serial(self):
        ecu = self.ecu_placeholder()
        scroll = self.scroll_container()
        lay = scroll.widget().layout()

        grp = QGroupBox("通用设置")
        form = QFormLayout(grp)
        form.setSpacing(8)
        self.spin_boot_timeout = self.spin(1, 3600, ecu.BOOT_TIMEOUT, "秒")
        form.addRow("最大等待启动时间:", self.spin_boot_timeout)
        self.spin_max_cycles = self.spin(0, 999999, ecu.MAX_CYCLES)
        self.spin_max_cycles.setToolTip("0=无限循环, >0=达到次数后自动停止")
        form.addRow("最大循环次数:", self.spin_max_cycles)
        self.combo_port_count = QComboBox()
        self.combo_port_count.addItems(["0 (不使用串口)", "1 (只用串口1)", "2 (串口1+串口2)"])
        self.combo_port_count.setCurrentIndex(ecu.DUT_SERIAL_PORT_COUNT)
        form.addRow("串口数量:", self.combo_port_count)
        self.spin_baudrate = self.spin(1200, 921600, ecu.DUT_BAUDRATE)
        form.addRow("波特率:", self.spin_baudrate)
        lay.addWidget(grp)

        self.grp_port1 = QGroupBox("串口1")
        p1 = QFormLayout(self.grp_port1)
        p1.setSpacing(8)
        self.combo_com1 = make_port_combo(ecu.DUT_COM_PORT1)
        p1.addRow("端口号:", self.combo_com1)
        self.chk_detect_p1 = QCheckBox("启用开机关键词检测")
        self.chk_detect_p1.setChecked(ecu.SERIAL_BOOT_DETECT_PORT1)
        p1.addRow(self.chk_detect_p1)
        self.edit_kw_p1 = QLineEdit(ecu.SERIAL_BOOT_KEYWORD_PORT1)
        p1.addRow("检测关键词:", self.edit_kw_p1)
        self.spin_count_p1 = self.spin(1, 999, ecu.SERIAL_BOOT_SUCCESS_COUNT_PORT1)
        p1.addRow("匹配次数:", self.spin_count_p1)
        self.chk_log_p1 = QCheckBox("打印匹配日志到主程序")
        self.chk_log_p1.setChecked(ecu.SERIAL_BOOT_LOG_TO_MAIN_PORT1)
        p1.addRow(self.chk_log_p1)
        lay.addWidget(self.grp_port1)

        self.grp_port2 = QGroupBox("串口2 (串口数量>=2时生效)")
        p2 = QFormLayout(self.grp_port2)
        p2.setSpacing(8)
        self.combo_com2 = make_port_combo(ecu.DUT_COM_PORT2)
        p2.addRow("端口号:", self.combo_com2)
        self.chk_detect_p2 = QCheckBox("启用开机关键词检测")
        self.chk_detect_p2.setChecked(ecu.SERIAL_BOOT_DETECT_PORT2)
        p2.addRow(self.chk_detect_p2)
        self.edit_kw_p2 = QLineEdit(ecu.SERIAL_BOOT_KEYWORD_PORT2)
        p2.addRow("检测关键词:", self.edit_kw_p2)
        self.spin_count_p2 = self.spin(1, 999, ecu.SERIAL_BOOT_SUCCESS_COUNT_PORT2)
        p2.addRow("匹配次数:", self.spin_count_p2)
        self.chk_log_p2 = QCheckBox("打印匹配日志到主程序")
        self.chk_log_p2.setChecked(ecu.SERIAL_BOOT_LOG_TO_MAIN_PORT2)
        p2.addRow(self.chk_log_p2)
        lay.addWidget(self.grp_port2)

        lay.addStretch()
        self.tabs.addTab(scroll, "  串口  ")
        self.combo_port_count.currentIndexChanged.connect(self._update_port_count_deps)
        self.chk_detect_p1.stateChanged.connect(self._update_port1_boot_deps)
        self.chk_detect_p2.stateChanged.connect(self._update_port2_boot_deps)

    # ---------- Tab 网口 ----------
    def _build_tab_network(self):
        ecu = self.ecu_placeholder()
        scroll = self.scroll_container()
        lay = scroll.widget().layout()

        grp = QGroupBox("网口 Ping 检测 (从PC端ping设备)")
        v = QVBoxLayout(grp)
        self.chk_ping = QCheckBox("启用 Ping 检测")
        self.chk_ping.setChecked(ecu.PING_ENABLED)
        v.addWidget(self.chk_ping)
        f = QFormLayout()
        f.setSpacing(8)
        self.edit_ping_source = QLineEdit(ecu.PING_SOURCE)
        self.edit_ping_source.setPlaceholderText("留空=系统自动选择")
        f.addRow("Ping 源地址:", self.edit_ping_source)
        self.edit_ping_target = QLineEdit(ecu.PING_TARGET)
        f.addRow("Ping 目标地址:", self.edit_ping_target)
        self.spin_ping_pkt = self.spin(1, 65500, ecu.PING_PACKET_SIZE)
        f.addRow("包大小 (字节):", self.spin_ping_pkt)
        self.spin_ping_interval = self.spin(1, 60, ecu.PING_INTERVAL)
        f.addRow("间隔 (秒):", self.spin_ping_interval)
        self.spin_ping_timeout = self.spin(1, 30, ecu.PING_TIMEOUT)
        f.addRow("超时 (秒):", self.spin_ping_timeout)
        self.spin_ping_success = self.spin(1, 999, ecu.PING_SUCCESS_COUNT)
        f.addRow("连续成功次数:", self.spin_ping_success)
        v.addLayout(f)
        self.ping_widgets = [self.edit_ping_source, self.edit_ping_target,
                             self.spin_ping_pkt, self.spin_ping_interval,
                             self.spin_ping_timeout, self.spin_ping_success]
        lay.addWidget(grp)
        lay.addStretch()
        self.tabs.addTab(scroll, "  网口  ")
        self.chk_ping.stateChanged.connect(self._update_ping_deps)

    # ---------- Tab ADB ----------
    def _build_tab_adb(self):
        ecu = self.ecu_placeholder()
        scroll = self.scroll_container()
        lay = scroll.widget().layout()

        grp = QGroupBox("ADB 连接配置")
        v = QVBoxLayout(grp)
        self.chk_adb = QCheckBox("启用 ADB")
        self.chk_adb.setChecked(ecu.ADB_ENABLED)
        v.addWidget(self.chk_adb)
        f = QFormLayout()
        f.setSpacing(8)
        self.edit_adb_path = QLineEdit(ecu.ADB_PATH)
        f.addRow("ADB 路径:", self.edit_adb_path)
        self.edit_adb_serial = QLineEdit(ecu.ADB_DEVICE_SERIAL)
        self.edit_adb_serial.setPlaceholderText("留空=自动选择")
        f.addRow("设备序列号:", self.edit_adb_serial)
        self.edit_adb_user = QLineEdit(ecu.ADB_LOGIN_USERNAME)
        f.addRow("登录用户名:", self.edit_adb_user)
        self.edit_adb_pass = QLineEdit(ecu.ADB_LOGIN_PASSWORD)
        self.edit_adb_pass.setEchoMode(QLineEdit.Password)
        f.addRow("登录密码:", self.edit_adb_pass)
        self.spin_adb_timeout = self.spin(1, 60, ecu.ADB_LOGIN_TIMEOUT)
        f.addRow("登录超时 (秒):", self.spin_adb_timeout)
        v.addLayout(f)
        self.adb_basic_widgets = [self.edit_adb_path, self.edit_adb_serial,
                                  self.edit_adb_user, self.edit_adb_pass, self.spin_adb_timeout]
        lay.addWidget(grp)

        grp2 = QGroupBox("ADB Shell 内部 Ping (从设备侧ping)")
        v2 = QVBoxLayout(grp2)
        self.chk_adb_ping = QCheckBox("启用 ADB Shell Ping")
        self.chk_adb_ping.setChecked(ecu.ADB_PING_ENABLED)
        v2.addWidget(self.chk_adb_ping)
        f2 = QFormLayout()
        f2.setSpacing(8)
        self.edit_adb_ping_target = QLineEdit(ecu.ADB_PING_TARGET)
        f2.addRow("Ping 目标:", self.edit_adb_ping_target)
        self.spin_adb_ping_count = self.spin(1, 100, ecu.ADB_PING_COUNT)
        f2.addRow("发包次数:", self.spin_adb_ping_count)
        self.spin_adb_ping_success = self.spin(1, 100, ecu.ADB_PING_SUCCESS_COUNT)
        f2.addRow("成功次数:", self.spin_adb_ping_success)
        v2.addLayout(f2)
        self.adb_ping_widgets = [self.edit_adb_ping_target, self.spin_adb_ping_count,
                                 self.spin_adb_ping_success]
        lay.addWidget(grp2)
        lay.addStretch()
        self.tabs.addTab(scroll, "  ADB  ")
        self.chk_adb.stateChanged.connect(self._update_adb_deps)
        self.chk_adb_ping.stateChanged.connect(self._update_adb_ping_deps)

    # ---------- Tab 继电器 ----------
    def _build_tab_relay(self):
        ecu = self.ecu_placeholder()
        scroll = self.scroll_container()
        lay = scroll.widget().layout()

        grp = QGroupBox("继电器硬件配置")
        v = QVBoxLayout(grp)
        top = QFormLayout()
        top.setSpacing(8)
        self.combo_relay_type = QComboBox()
        self.combo_relay_type.addItems(["SERIAL (串口控制)", "HID (USB HID)", "MOCK (模拟模式)"])
        self.combo_relay_type.setCurrentIndex({'SERIAL': 0, 'HID': 1, 'MOCK': 2}.get(ecu.RELAY_TYPE, 0))
        top.addRow("继电器类型:", self.combo_relay_type)
        v.addLayout(top)

        self.grp_serial_relay = QGroupBox("串口继电器配置")
        sr = QFormLayout(self.grp_serial_relay)
        sr.setSpacing(8)
        self.combo_relay_com = make_port_combo(ecu.RELAY_COM_PORT)
        sr.addRow("继电器串口:", self.combo_relay_com)
        self.spin_relay_baud = self.spin(1200, 921600, ecu.RELAY_BAUDRATE)
        sr.addRow("波特率:", self.spin_relay_baud)
        self.spin_relay_channel = self.spin(1, 4, ecu.RELAY_CHANNEL)
        sr.addRow("通道号:", self.spin_relay_channel)
        v.addWidget(self.grp_serial_relay)

        self.grp_relay_cmds = QGroupBox("通道指令配置 (十六进制)")
        cg = QGridLayout(self.grp_relay_cmds)
        cg.addWidget(QLabel("通道"), 0, 0)
        cg.addWidget(QLabel("通电指令 (HEX)"), 0, 1)
        cg.addWidget(QLabel("断电指令 (HEX)"), 0, 2)
        self.relay_cmd_on = {}
        self.relay_cmd_off = {}
        for ch in range(1, 5):
            cg.addWidget(QLabel(f"通道 {ch}"), ch, 0)
            on_edit = QLineEdit(ecu.RELAY_COMMANDS.get(ch, {}).get('on', ''))
            on_edit.setPlaceholderText("如: 68 09 00 FF 12 00 01 12 16")
            cg.addWidget(on_edit, ch, 1)
            self.relay_cmd_on[ch] = on_edit
            off_edit = QLineEdit(ecu.RELAY_COMMANDS.get(ch, {}).get('off', ''))
            off_edit.setPlaceholderText("如: 68 09 00 FF 12 00 00 11 16")
            cg.addWidget(off_edit, ch, 2)
            self.relay_cmd_off[ch] = off_edit
        v.addWidget(self.grp_relay_cmds)
        self.serial_relay_widgets = [self.combo_relay_com, self.spin_relay_baud,
                                     self.spin_relay_channel, self.grp_relay_cmds]

        self.grp_hid_relay = QGroupBox("HID 继电器配置")
        hid = QFormLayout(self.grp_hid_relay)
        hid.setSpacing(8)
        hex_v = QRegExpValidator(QRegExp("[0-9a-fA-FxX]{1,10}"))
        self.edit_relay_vid = QLineEdit(hex(ecu.RELAY_VID))
        self.edit_relay_vid.setValidator(hex_v)
        hid.addRow("VID:", self.edit_relay_vid)
        self.edit_relay_pid = QLineEdit(hex(ecu.RELAY_PID))
        self.edit_relay_pid.setValidator(hex_v)
        hid.addRow("PID:", self.edit_relay_pid)
        self.edit_hid_on = QLineEdit(' '.join(f'{b:02X}' for b in ecu.HID_ON_CMD))
        hid.addRow("通电指令 (HEX):", self.edit_hid_on)
        self.edit_hid_off = QLineEdit(' '.join(f'{b:02X}' for b in ecu.HID_OFF_CMD))
        hid.addRow("断电指令 (HEX):", self.edit_hid_off)
        v.addWidget(self.grp_hid_relay)
        self.hid_relay_widgets = [self.edit_relay_vid, self.edit_relay_pid,
                                  self.edit_hid_on, self.edit_hid_off]
        lay.addWidget(grp)

        grp2 = QGroupBox("继电器时序配置")
        tl = QVBoxLayout(grp2)
        f1 = QFormLayout()
        f1.setSpacing(8)
        self.spin_power_on_delay = self.spin(0, 300, ecu.POWER_ON_DELAY)
        f1.addRow("上电后等待 (秒):", self.spin_power_on_delay)
        self.spin_min_on_time = self.spin(1, 600, ecu.MIN_POWER_ON_TIME)
        f1.addRow("最小上电保持 (秒):", self.spin_min_on_time)
        tl.addLayout(f1)
        self.chk_random_on = QCheckBox("启用随机上电保持时间")
        self.chk_random_on.setChecked(ecu.POWER_ON_TIME_RANDOM)
        tl.addWidget(self.chk_random_on)
        f2 = QFormLayout()
        f2.setSpacing(8)
        self.spin_on_min = self.spin(1, 600, ecu.POWER_ON_TIME_MIN)
        f2.addRow("随机最小 (秒):", self.spin_on_min)
        self.spin_on_max = self.spin(1, 600, ecu.POWER_ON_TIME_MAX)
        f2.addRow("随机最大 (秒):", self.spin_on_max)
        tl.addLayout(f2)
        self.chk_random_boot_detect = QCheckBox("随机上电模式下做开机检测")
        self.chk_random_boot_detect.setChecked(ecu.RANDOM_MODE_BOOT_DETECT)
        self.chk_random_boot_detect.setToolTip("勾选后在随机等待期间同时进行开机检测")
        tl.addWidget(self.chk_random_boot_detect)
        f3 = QFormLayout()
        f3.setSpacing(8)
        self.spin_off_wait = self.spin(0, 300, ecu.POWER_OFF_WAIT_TIME)
        f3.addRow("断电后等待 (秒):", self.spin_off_wait)
        tl.addLayout(f3)
        self.chk_random_off = QCheckBox("启用随机断电等待时间")
        self.chk_random_off.setChecked(ecu.POWER_OFF_WAIT_RANDOM)
        tl.addWidget(self.chk_random_off)
        f4 = QFormLayout()
        f4.setSpacing(8)
        self.spin_off_min = self.spin(1, 600, ecu.POWER_OFF_WAIT_MIN)
        f4.addRow("随机最小 (秒):", self.spin_off_min)
        self.spin_off_max = self.spin(1, 600, ecu.POWER_OFF_WAIT_MAX)
        f4.addRow("随机最大 (秒):", self.spin_off_max)
        tl.addLayout(f4)
        lay.addWidget(grp2)
        lay.addStretch()
        self.tabs.addTab(scroll, " 继电器 ")
        self.combo_relay_type.currentIndexChanged.connect(self._update_relay_type_deps)
        self.chk_random_on.stateChanged.connect(self._update_random_on_deps)
        self.chk_random_off.stateChanged.connect(self._update_random_off_deps)

    # ---------- Tab 日志 ----------
    def _build_tab_log(self):
        ecu = self.ecu_placeholder()
        container = QWidget()
        lay = QVBoxLayout(container)

        grp = QGroupBox("日志存储目录 (留空=程序目录/logs/reboot_<时间戳>/)")
        h = QHBoxLayout(grp)
        self.edit_log_dir = QLineEdit(ecu.LOG_DIR)
        self.edit_log_dir.setPlaceholderText("留空则保存在程序运行目录")
        h.addWidget(self.edit_log_dir)
        btn = QPushButton("浏览...")
        btn.setFixedWidth(80)
        btn.clicked.connect(self._browse_log_dir)
        h.addWidget(btn)
        lay.addWidget(grp)

        grp2 = QGroupBox("日志文件名")
        f = QFormLayout(grp2)
        f.setSpacing(8)
        self.edit_log_file = QLineEdit(ecu.LOG_FILE)
        f.addRow("主程序日志:", self.edit_log_file)
        self.edit_serial_log1 = QLineEdit(ecu.SERIAL_LOG_FILE1)
        f.addRow("串口1日志:", self.edit_serial_log1)
        self.edit_serial_log2 = QLineEdit(ecu.SERIAL_LOG_FILE2)
        f.addRow("串口2日志:", self.edit_serial_log2)
        self.edit_relay_log = QLineEdit(ecu.RELAY_LOG_FILE)
        f.addRow("继电器日志:", self.edit_relay_log)
        self.edit_adb_log = QLineEdit(ecu.ADB_LOG_FILE)
        f.addRow("ADB日志:", self.edit_adb_log)
        self.edit_report_file = QLineEdit(ecu.REPORT_FILE)
        f.addRow("测试报告:", self.edit_report_file)
        lay.addWidget(grp2)
        lay.addStretch()
        self.tabs.addTab(container, "  日志  ")

    def ecu_placeholder(self):
        """构建 UI 时读取 reboot_test 的默认配置"""
        if self.ecu is None:
            import reboot_test as _mod
            self.ecu = _mod
        return self.ecu

    # ==================== 依赖 ====================
    def _update_all_deps(self):
        for fn in (self._update_port_count_deps, self._update_port1_boot_deps,
                   self._update_port2_boot_deps, self._update_ping_deps,
                   self._update_adb_deps, self._update_adb_ping_deps,
                   self._update_relay_type_deps, self._update_random_on_deps,
                   self._update_random_off_deps):
            fn()

    def _update_port_count_deps(self):
        c = self.combo_port_count.currentIndex()
        self.grp_port1.setEnabled(c >= 1)
        self.grp_port2.setEnabled(c >= 2)
        self.spin_baudrate.setEnabled(c >= 1)

    def _update_port1_boot_deps(self):
        e = self.chk_detect_p1.isChecked()
        for w in (self.edit_kw_p1, self.spin_count_p1, self.chk_log_p1):
            w.setEnabled(e)

    def _update_port2_boot_deps(self):
        e = self.chk_detect_p2.isChecked()
        for w in (self.edit_kw_p2, self.spin_count_p2, self.chk_log_p2):
            w.setEnabled(e)

    def _update_ping_deps(self):
        e = self.chk_ping.isChecked()
        for w in self.ping_widgets:
            w.setEnabled(e)

    def _update_adb_deps(self):
        on = self.chk_adb.isChecked()
        for w in self.adb_basic_widgets:
            w.setEnabled(on)
        self.chk_adb_ping.setEnabled(on)
        self._update_adb_ping_deps()

    def _update_adb_ping_deps(self):
        on = self.chk_adb.isChecked() and self.chk_adb_ping.isChecked()
        for w in self.adb_ping_widgets:
            w.setEnabled(on)

    def _update_relay_type_deps(self):
        rtype = ['SERIAL', 'HID', 'MOCK'][self.combo_relay_type.currentIndex()]
        self.grp_serial_relay.setEnabled(rtype == 'SERIAL')
        for w in self.serial_relay_widgets:
            w.setEnabled(rtype == 'SERIAL')
        self.grp_hid_relay.setEnabled(rtype == 'HID')
        for w in self.hid_relay_widgets:
            w.setEnabled(rtype == 'HID')

    def _update_random_on_deps(self):
        e = self.chk_random_on.isChecked()
        for w in (self.spin_on_min, self.spin_on_max, self.chk_random_boot_detect):
            w.setEnabled(e)

    def _update_random_off_deps(self):
        e = self.chk_random_off.isChecked()
        for w in (self.spin_off_min, self.spin_off_max):
            w.setEnabled(e)

    def _refresh_all_ports(self):
        for combo in (self.combo_com1, self.combo_com2, self.combo_relay_com):
            refresh_port_combo(combo)

    def _browse_log_dir(self):
        current = self.edit_log_dir.text().strip() or gc.app_dir()
        d = QFileDialog.getExistingDirectory(self, "选择日志存储目录", current)
        if d:
            self.edit_log_dir.setText(d)

    # ==================== 配置 ====================
    def collect(self):
        return {
            'BOOT_TIMEOUT': self.spin_boot_timeout.value(),
            'MAX_CYCLES': self.spin_max_cycles.value(),
            'DUT_SERIAL_PORT_COUNT': self.combo_port_count.currentIndex(),
            'DUT_BAUDRATE': self.spin_baudrate.value(),
            'DUT_COM_PORT1': self.combo_com1.currentText(),
            'SERIAL_BOOT_DETECT_PORT1': self.chk_detect_p1.isChecked(),
            'SERIAL_BOOT_KEYWORD_PORT1': self.edit_kw_p1.text(),
            'SERIAL_BOOT_SUCCESS_COUNT_PORT1': self.spin_count_p1.value(),
            'SERIAL_BOOT_LOG_TO_MAIN_PORT1': self.chk_log_p1.isChecked(),
            'DUT_COM_PORT2': self.combo_com2.currentText(),
            'SERIAL_BOOT_DETECT_PORT2': self.chk_detect_p2.isChecked(),
            'SERIAL_BOOT_KEYWORD_PORT2': self.edit_kw_p2.text(),
            'SERIAL_BOOT_SUCCESS_COUNT_PORT2': self.spin_count_p2.value(),
            'SERIAL_BOOT_LOG_TO_MAIN_PORT2': self.chk_log_p2.isChecked(),
            'PING_ENABLED': self.chk_ping.isChecked(),
            'PING_SOURCE': self.edit_ping_source.text(),
            'PING_TARGET': self.edit_ping_target.text(),
            'PING_PACKET_SIZE': self.spin_ping_pkt.value(),
            'PING_INTERVAL': self.spin_ping_interval.value(),
            'PING_TIMEOUT': self.spin_ping_timeout.value(),
            'PING_SUCCESS_COUNT': self.spin_ping_success.value(),
            'ADB_ENABLED': self.chk_adb.isChecked(),
            'ADB_PATH': self.edit_adb_path.text(),
            'ADB_DEVICE_SERIAL': self.edit_adb_serial.text(),
            'ADB_LOGIN_USERNAME': self.edit_adb_user.text(),
            'ADB_LOGIN_PASSWORD': self.edit_adb_pass.text(),
            'ADB_LOGIN_TIMEOUT': self.spin_adb_timeout.value(),
            'ADB_PING_ENABLED': self.chk_adb_ping.isChecked(),
            'ADB_PING_TARGET': self.edit_adb_ping_target.text(),
            'ADB_PING_COUNT': self.spin_adb_ping_count.value(),
            'ADB_PING_SUCCESS_COUNT': self.spin_adb_ping_success.value(),
            'RELAY_TYPE': self.combo_relay_type.currentIndex(),
            'RELAY_COM_PORT': self.combo_relay_com.currentText(),
            'RELAY_BAUDRATE': self.spin_relay_baud.value(),
            'RELAY_CHANNEL': self.spin_relay_channel.value(),
            'RELAY_COMMANDS': {str(ch): {'on': self.relay_cmd_on[ch].text(),
                                         'off': self.relay_cmd_off[ch].text()} for ch in range(1, 5)},
            'RELAY_VID': self.edit_relay_vid.text(),
            'RELAY_PID': self.edit_relay_pid.text(),
            'HID_ON_CMD': self.edit_hid_on.text(),
            'HID_OFF_CMD': self.edit_hid_off.text(),
            'POWER_ON_DELAY': self.spin_power_on_delay.value(),
            'MIN_POWER_ON_TIME': self.spin_min_on_time.value(),
            'POWER_ON_TIME_RANDOM': self.chk_random_on.isChecked(),
            'POWER_ON_TIME_MIN': self.spin_on_min.value(),
            'POWER_ON_TIME_MAX': self.spin_on_max.value(),
            'RANDOM_MODE_BOOT_DETECT': self.chk_random_boot_detect.isChecked(),
            'POWER_OFF_WAIT_TIME': self.spin_off_wait.value(),
            'POWER_OFF_WAIT_RANDOM': self.chk_random_off.isChecked(),
            'POWER_OFF_WAIT_MIN': self.spin_off_min.value(),
            'POWER_OFF_WAIT_MAX': self.spin_off_max.value(),
            'LOG_DIR': self.edit_log_dir.text(),
            'LOG_FILE': self.edit_log_file.text(),
            'SERIAL_LOG_FILE1': self.edit_serial_log1.text(),
            'SERIAL_LOG_FILE2': self.edit_serial_log2.text(),
            'RELAY_LOG_FILE': self.edit_relay_log.text(),
            'ADB_LOG_FILE': self.edit_adb_log.text(),
            'REPORT_FILE': self.edit_report_file.text(),
        }

    def apply_config(self, cfg):
        g = cfg.get
        self.spin_boot_timeout.setValue(g('BOOT_TIMEOUT', 120))
        self.spin_max_cycles.setValue(g('MAX_CYCLES', 0))
        self.combo_port_count.setCurrentIndex(g('DUT_SERIAL_PORT_COUNT', 1))
        self.spin_baudrate.setValue(g('DUT_BAUDRATE', 115200))
        self.combo_com1.setEditText(g('DUT_COM_PORT1', 'COM1'))
        self.chk_detect_p1.setChecked(g('SERIAL_BOOT_DETECT_PORT1', True))
        self.edit_kw_p1.setText(g('SERIAL_BOOT_KEYWORD_PORT1', 'login:'))
        self.spin_count_p1.setValue(g('SERIAL_BOOT_SUCCESS_COUNT_PORT1', 1))
        self.chk_log_p1.setChecked(g('SERIAL_BOOT_LOG_TO_MAIN_PORT1', True))
        self.combo_com2.setEditText(g('DUT_COM_PORT2', 'COM2'))
        self.chk_detect_p2.setChecked(g('SERIAL_BOOT_DETECT_PORT2', False))
        self.edit_kw_p2.setText(g('SERIAL_BOOT_KEYWORD_PORT2', 'login:'))
        self.spin_count_p2.setValue(g('SERIAL_BOOT_SUCCESS_COUNT_PORT2', 1))
        self.chk_log_p2.setChecked(g('SERIAL_BOOT_LOG_TO_MAIN_PORT2', True))
        self.chk_ping.setChecked(g('PING_ENABLED', False))
        self.edit_ping_source.setText(g('PING_SOURCE', ''))
        self.edit_ping_target.setText(g('PING_TARGET', ''))
        self.spin_ping_pkt.setValue(g('PING_PACKET_SIZE', 32))
        self.spin_ping_interval.setValue(g('PING_INTERVAL', 1))
        self.spin_ping_timeout.setValue(g('PING_TIMEOUT', 3))
        self.spin_ping_success.setValue(g('PING_SUCCESS_COUNT', 5))
        self.chk_adb.setChecked(g('ADB_ENABLED', False))
        self.edit_adb_path.setText(g('ADB_PATH', 'adb'))
        self.edit_adb_serial.setText(g('ADB_DEVICE_SERIAL', ''))
        self.edit_adb_user.setText(g('ADB_LOGIN_USERNAME', 'root'))
        self.edit_adb_pass.setText(g('ADB_LOGIN_PASSWORD', ''))
        self.spin_adb_timeout.setValue(g('ADB_LOGIN_TIMEOUT', 15))
        self.chk_adb_ping.setChecked(g('ADB_PING_ENABLED', False))
        self.edit_adb_ping_target.setText(g('ADB_PING_TARGET', ''))
        self.spin_adb_ping_count.setValue(g('ADB_PING_COUNT', 3))
        self.spin_adb_ping_success.setValue(g('ADB_PING_SUCCESS_COUNT', 2))
        self.combo_relay_type.setCurrentIndex(g('RELAY_TYPE', 0))
        self.combo_relay_com.setEditText(g('RELAY_COM_PORT', 'COM1'))
        self.spin_relay_baud.setValue(g('RELAY_BAUDRATE', 115200))
        self.spin_relay_channel.setValue(g('RELAY_CHANNEL', 1))
        cmds = g('RELAY_COMMANDS', {})
        for ch in range(1, 5):
            ch_data = cmds.get(str(ch), {})
            self.relay_cmd_on[ch].setText(ch_data.get('on', ''))
            self.relay_cmd_off[ch].setText(ch_data.get('off', ''))
        self.edit_relay_vid.setText(g('RELAY_VID', '0x4d8'))
        self.edit_relay_pid.setText(g('RELAY_PID', '0xdd3'))
        self.edit_hid_on.setText(g('HID_ON_CMD', ''))
        self.edit_hid_off.setText(g('HID_OFF_CMD', ''))
        self.spin_power_on_delay.setValue(g('POWER_ON_DELAY', 10))
        self.spin_min_on_time.setValue(g('MIN_POWER_ON_TIME', 30))
        self.chk_random_on.setChecked(g('POWER_ON_TIME_RANDOM', True))
        self.spin_on_min.setValue(g('POWER_ON_TIME_MIN', 30))
        self.spin_on_max.setValue(g('POWER_ON_TIME_MAX', 120))
        self.chk_random_boot_detect.setChecked(g('RANDOM_MODE_BOOT_DETECT', False))
        self.spin_off_wait.setValue(g('POWER_OFF_WAIT_TIME', 10))
        self.chk_random_off.setChecked(g('POWER_OFF_WAIT_RANDOM', False))
        self.spin_off_min.setValue(g('POWER_OFF_WAIT_MIN', 1))
        self.spin_off_max.setValue(g('POWER_OFF_WAIT_MAX', 60))
        self.edit_log_dir.setText(g('LOG_DIR', ''))
        self.edit_log_file.setText(g('LOG_FILE', 'power_cycle_debug.log'))
        self.edit_serial_log1.setText(g('SERIAL_LOG_FILE1', 'serial_output_port1.log'))
        self.edit_serial_log2.setText(g('SERIAL_LOG_FILE2', 'serial_output_port2.log'))
        self.edit_relay_log.setText(g('RELAY_LOG_FILE', 'relay_output.log'))
        self.edit_adb_log.setText(g('ADB_LOG_FILE', 'adb_output.log'))
        self.edit_report_file.setText(g('REPORT_FILE', 'Power_Test_Report.txt'))
        self._update_all_deps()

    def refresh_project(self):
        """Follow the active project for device connections and boot detection."""
        project = gc.get_active_project()
        ports = project.get('serial_ports', []) if project.get('serial_enabled') else []
        port_count = min(2, len(ports))

        self._suppress_autosave = True
        try:
            self.combo_port_count.setCurrentIndex(port_count)
            for index in range(2):
                slot = ports[index] if index < len(ports) else {}
                combo = (self.combo_com1, self.combo_com2)[index]
                detect = (self.chk_detect_p1, self.chk_detect_p2)[index]
                keyword = (self.edit_kw_p1, self.edit_kw_p2)[index]
                count = (self.spin_count_p1, self.spin_count_p2)[index]
                log_main = (self.chk_log_p1, self.chk_log_p2)[index]

                combo.setEditText(str(slot.get('port', '') or ''))
                if slot:
                    self.spin_baudrate.setValue(int(slot.get('baudrate', 115200)))
                detect.setChecked(bool(slot.get('boot_detect', False)))
                keyword.setText(str(slot.get('boot_keyword', '') or ''))
                count.setValue(int(slot.get('boot_success_count', 1)))
                log_main.setChecked(bool(slot.get('log_to_main', True)))

            if project.get('boot_detect_enabled'):
                self.spin_boot_timeout.setValue(int(project.get('boot_timeout', 120)))

            self.chk_adb.setChecked(bool(project.get('adb_enabled', False)))
            self.edit_adb_path.setText(str(project.get('adb_path', 'adb') or 'adb'))
            self.edit_adb_serial.setText(str(project.get('adb_serial', '') or ''))
            if project.get('adb_auth_enabled'):
                self.edit_adb_user.setText(str(project.get('adb_username', '') or ''))
                self.edit_adb_pass.setText(str(project.get('adb_password', '') or ''))
                self.spin_adb_timeout.setValue(int(project.get('adb_login_timeout', 15)))
            else:
                self.edit_adb_user.setText('')
                self.edit_adb_pass.setText('')
        finally:
            self._suppress_autosave = False
        self._update_all_deps()

    def _save_config(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出配置文件", os.path.join(gc.app_dir(), "reboot_config.json"),
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

    # ==================== 应用配置到 reboot_test 模块 ====================
    def _apply_to_module(self, ecu):
        c = self.collect()
        ecu.BOOT_TIMEOUT = c['BOOT_TIMEOUT']
        ecu.MAX_CYCLES = c['MAX_CYCLES']
        ecu.DUT_SERIAL_PORT_COUNT = c['DUT_SERIAL_PORT_COUNT']
        ecu.DUT_BAUDRATE = c['DUT_BAUDRATE']
        ecu.DUT_COM_PORT1 = c['DUT_COM_PORT1'].strip()
        ecu.SERIAL_BOOT_DETECT_PORT1 = c['SERIAL_BOOT_DETECT_PORT1']
        ecu.SERIAL_BOOT_KEYWORD_PORT1 = c['SERIAL_BOOT_KEYWORD_PORT1'].strip()
        ecu.SERIAL_BOOT_SUCCESS_COUNT_PORT1 = c['SERIAL_BOOT_SUCCESS_COUNT_PORT1']
        ecu.SERIAL_BOOT_LOG_TO_MAIN_PORT1 = c['SERIAL_BOOT_LOG_TO_MAIN_PORT1']
        ecu.DUT_COM_PORT2 = c['DUT_COM_PORT2'].strip()
        ecu.SERIAL_BOOT_DETECT_PORT2 = c['SERIAL_BOOT_DETECT_PORT2']
        ecu.SERIAL_BOOT_KEYWORD_PORT2 = c['SERIAL_BOOT_KEYWORD_PORT2'].strip()
        ecu.SERIAL_BOOT_SUCCESS_COUNT_PORT2 = c['SERIAL_BOOT_SUCCESS_COUNT_PORT2']
        ecu.SERIAL_BOOT_LOG_TO_MAIN_PORT2 = c['SERIAL_BOOT_LOG_TO_MAIN_PORT2']

        ecu.PING_ENABLED = c['PING_ENABLED']
        ecu.PING_SOURCE = c['PING_SOURCE'].strip()
        ecu.PING_TARGET = c['PING_TARGET'].strip()
        ecu.PING_PACKET_SIZE = c['PING_PACKET_SIZE']
        ecu.PING_INTERVAL = c['PING_INTERVAL']
        ecu.PING_TIMEOUT = c['PING_TIMEOUT']
        ecu.PING_SUCCESS_COUNT = c['PING_SUCCESS_COUNT']

        ecu.ADB_ENABLED = c['ADB_ENABLED']
        ecu.ADB_PATH = c['ADB_PATH'].strip()
        ecu.ADB_DEVICE_SERIAL = c['ADB_DEVICE_SERIAL'].strip()
        ecu.ADB_LOGIN_USERNAME = c['ADB_LOGIN_USERNAME'].strip()
        ecu.ADB_LOGIN_PASSWORD = c['ADB_LOGIN_PASSWORD']
        ecu.ADB_LOGIN_TIMEOUT = c['ADB_LOGIN_TIMEOUT']
        ecu.ADB_PING_ENABLED = c['ADB_PING_ENABLED']
        ecu.ADB_PING_TARGET = c['ADB_PING_TARGET'].strip()
        ecu.ADB_PING_COUNT = c['ADB_PING_COUNT']
        ecu.ADB_PING_SUCCESS_COUNT = c['ADB_PING_SUCCESS_COUNT']

        ecu.RELAY_TYPE = ['SERIAL', 'HID', 'MOCK'][c['RELAY_TYPE']]
        ecu.RELAY_COM_PORT = c['RELAY_COM_PORT'].strip()
        ecu.RELAY_BAUDRATE = c['RELAY_BAUDRATE']
        ecu.RELAY_CHANNEL = c['RELAY_CHANNEL']
        ecu.RELAY_COMMANDS = {ch: {'on': c['RELAY_COMMANDS'][str(ch)]['on'].strip(),
                                   'off': c['RELAY_COMMANDS'][str(ch)]['off'].strip()}
                              for ch in range(1, 5)}
        try:
            ecu.RELAY_VID = int(c['RELAY_VID'], 16)
        except ValueError:
            ecu.RELAY_VID = 0
        try:
            ecu.RELAY_PID = int(c['RELAY_PID'], 16)
        except ValueError:
            ecu.RELAY_PID = 0
        try:
            ecu.HID_ON_CMD = [int(x, 16) for x in c['HID_ON_CMD'].split()]
        except ValueError:
            ecu.HID_ON_CMD = []
        try:
            ecu.HID_OFF_CMD = [int(x, 16) for x in c['HID_OFF_CMD'].split()]
        except ValueError:
            ecu.HID_OFF_CMD = []

        ecu.POWER_ON_DELAY = c['POWER_ON_DELAY']
        ecu.MIN_POWER_ON_TIME = c['MIN_POWER_ON_TIME']
        ecu.POWER_ON_TIME_RANDOM = c['POWER_ON_TIME_RANDOM']
        ecu.POWER_ON_TIME_MIN = c['POWER_ON_TIME_MIN']
        ecu.POWER_ON_TIME_MAX = c['POWER_ON_TIME_MAX']
        ecu.RANDOM_MODE_BOOT_DETECT = c['RANDOM_MODE_BOOT_DETECT']
        ecu.POWER_OFF_WAIT_TIME = c['POWER_OFF_WAIT_TIME']
        ecu.POWER_OFF_WAIT_RANDOM = c['POWER_OFF_WAIT_RANDOM']
        ecu.POWER_OFF_WAIT_MIN = c['POWER_OFF_WAIT_MIN']
        ecu.POWER_OFF_WAIT_MAX = c['POWER_OFF_WAIT_MAX']

        # 每次运行使用独立的带时间戳日志目录
        log_dir = make_run_log_dir("reboot", c['LOG_DIR'])
        ecu.LOG_DIR = log_dir
        ecu.LOG_FILE = os.path.join(log_dir, c['LOG_FILE'].strip() or 'power_cycle_debug.log')
        ecu.SERIAL_LOG_FILE1 = os.path.join(log_dir, c['SERIAL_LOG_FILE1'].strip() or 'serial_output_port1.log')
        ecu.SERIAL_LOG_FILE2 = os.path.join(log_dir, c['SERIAL_LOG_FILE2'].strip() or 'serial_output_port2.log')
        ecu.RELAY_LOG_FILE = os.path.join(log_dir, c['RELAY_LOG_FILE'].strip() or 'relay_output.log')
        ecu.ADB_LOG_FILE = os.path.join(log_dir, c['ADB_LOG_FILE'].strip() or 'adb_output.log')
        ecu.REPORT_FILE = os.path.join(log_dir, c['REPORT_FILE'].strip() or 'Power_Test_Report.txt')
        return log_dir

    # ==================== 启动 / 停止 ====================
    def _on_start(self):
        if not self.try_acquire():
            return
        try:
            ecu = self.ecu_placeholder()
            log_dir = self._apply_to_module(ecu)
            project = gc.get_active_project() or {}
            device_session.start_logging(
                project, os.path.join(log_dir, "device_log.log"))

            self.clear_log()
            self._stat_cycle = 0
            self._stat_pass = 0
            self._stat_fail = 0
            self._boot_times = []
            self._verdict_cycles = set()
            self._start_time = time.time()
            self._reset_stats_display()

            self.attach_logging(os.path.join(log_dir, ecu.LOG_FILE))

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
            self.worker = RebootWorker(ecu)
            self.worker.finished_signal.connect(self._on_finished)
            self.worker.error_signal.connect(lambda m: self.append_log(f"测试出错: {m}"))
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
        self.card_avg_boot.set_value("--", "#a29bfe")
        self.card_last_boot.set_value("--", "#a29bfe")
        self.card_elapsed.set_value("00:00:00", "#b2bec3")

    def _update_elapsed(self):
        if self._start_time:
            self.card_elapsed.set_value(gc.format_duration(time.time() - self._start_time))

    def on_log_line(self, text):
        m = re.search(r'第\s*(\d+)\s*次循环', text)
        if m:
            self._stat_cycle = int(m.group(1))
            self.card_cycle.set_value(str(self._stat_cycle), "#74b9ff")
            max_cyc = self.spin_max_cycles.value()
            if max_cyc > 0:
                self.progress_bar.setValue(self._stat_cycle)
                self.progress_bar.setFormat(f"第 {self._stat_cycle} 次 / 共 {max_cyc} 次")

        m = re.search(r'第\s*(\d+)\s*次开机成功', text)
        if m:
            cyc = int(m.group(1))
            if cyc not in self._verdict_cycles:
                self._verdict_cycles.add(cyc)
                self._stat_pass += 1
                self.card_pass.set_value(str(self._stat_pass), "#00b894")
                bm = re.search(r'启动耗时:\s*([\d.]+)s', text) or re.search(r'耗时:\s*([\d.]+)s', text)
                if bm:
                    bt = float(bm.group(1))
                    self._boot_times.append(bt)
                    self.card_last_boot.set_value(f"{bt:.1f}s", "#a29bfe")
                    self.card_avg_boot.set_value(
                        f"{sum(self._boot_times) / len(self._boot_times):.1f}s", "#a29bfe")

        m = re.search(r'第\s*(\d+)\s*次开机失败', text)
        if m:
            cyc = int(m.group(1))
            if cyc not in self._verdict_cycles:
                self._verdict_cycles.add(cyc)
                self._stat_fail += 1
                self.card_fail.set_value(str(self._stat_fail), "#e17055")

        total = self._stat_pass + self._stat_fail
        if total > 0:
            rate = self._stat_pass / total * 100
            color = '#00b894' if rate >= 90 else ('#fdcb6e' if rate >= 70 else '#e17055')
            self.card_rate.set_value(f"{rate:.1f}%", color)
        if self.is_running:
            self.title_changed.emit(
                f"第{self._stat_cycle}次 | 成功:{self._stat_pass} 失败:{self._stat_fail}")
