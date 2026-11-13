# -*- coding: utf-8 -*-
"""
T1L (ADB Ping) 面板 — 由 Kedge_T1L.py 迁移。
按固定间隔通过 ADB 在设备侧 ping；全部成功则 reboot 设备，有失败则只记录并等下一轮。
作为「网络测试」页面（gui_net.py）中的一个 Tab 使用，通过 self.page 访问宿主页面。
"""

import logging
import os
import re
import subprocess
import threading
import time

from PyQt5.QtWidgets import (
    QWidget, QGroupBox, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QCheckBox, QPushButton, QGridLayout, QProgressBar, QFileDialog, QLabel
)
from PyQt5.QtCore import QThread, QTimer, pyqtSignal

import gui_common as gc
from gui_common import BasePage, StatCard, make_run_log_dir
from process_utils import run_cancellable_process


def run_cmd(cmd, timeout=60, stop_event=None):
    cmd_list = [cmd] if isinstance(cmd, str) else list(cmd)
    try:
        r = run_cancellable_process(cmd_list, stop_event=stop_event,
                                    timeout=timeout, label="T1L ADB")
        return r.success, r.output or r.error or r.reason
    except subprocess.TimeoutExpired:
        return False, "命令执行超时"
    except Exception as ex:
        return False, str(ex)


class T1LWorker(QThread):
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, cfg, stop_event, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.stop_event = stop_event

    # ---- ADB 基础操作 ----
    def _adb(self, args, timeout=60):
        cmd = [self.cfg['adb_path']]
        if self.cfg['serial']:
            cmd += ['-s', self.cfg['serial']]
        cmd += args
        try:
            return run_cmd(cmd, timeout=timeout, stop_event=self.stop_event)
        except subprocess.TimeoutExpired:
            return False, "命令执行超时"
        except Exception as ex:
            return False, str(ex)

    def _shell(self, cmd, timeout=60):
        return self._adb(['shell', cmd], timeout=timeout)

    def check_device(self):
        ok, out = self._adb(['devices'], timeout=15)
        if not ok:
            return False
        for line in out.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == 'device':
                if not self.cfg['serial'] or parts[0] == self.cfg['serial']:
                    return True
        return False

    def configure_network(self):
        cmd = f"ifconfig {self.cfg['iface']} {self.cfg['dev_ip']}"
        logging.info(f">>> 配置 IP: {cmd}")
        ok, out = self._shell(cmd)
        logging.info(">>> IP 配置成功" if ok else f">>> IP 配置失败: {out.strip()}")
        return ok

    def do_ping(self):
        count = self.cfg['count']
        cmd = (f"ping -c {count} -W {self.cfg['ping_timeout']} "
               f"{self.cfg['target']} -I {self.cfg['iface']}")
        ok, out = self._shell(cmd, timeout=count * self.cfg['ping_timeout'] + 30)
        if not ok:
            return 0, count, out
        success = 0
        for line in out.split("\n"):
            if "packets transmitted" in line:
                for part in line.split(","):
                    if "received" in part:
                        try:
                            success = int(part.strip().split()[0])
                        except (ValueError, IndexError):
                            pass
        return success, count - success, out

    def do_reboot(self):
        if self.stop_event.is_set():
            return
        logging.info(">>> 执行 reboot 重启设备...")
        ok, out = self._shell("reboot", timeout=30)
        if not ok:
            raise RuntimeError(f"reboot 失败: {out.strip()}")
        logging.info(">>> reboot 命令已发送")

    def restart_adb_server(self):
        logging.info("  重启 ADB 服务...")
        if self.stop_event.is_set():
            return
        ok, out = run_cmd([self.cfg['adb_path'], 'kill-server'], timeout=15, stop_event=self.stop_event)
        if not ok:
            raise RuntimeError(f"停止 ADB 服务失败: {out.strip()}")
        ok, out = run_cmd([self.cfg['adb_path'], 'start-server'], timeout=30, stop_event=self.stop_event)
        if not ok:
            raise RuntimeError(f"启动 ADB 服务失败: {out.strip()}")
        logging.info("  ADB 服务已重启，等待设备重新连接...")

    def wait_device(self, max_wait, interval=5):
        waited = 0
        while waited < max_wait:
            if self.stop_event.is_set():
                return False
            if self.check_device():
                return True
            if gc.sleep_interruptible(self.stop_event, interval):
                return False
            waited += interval
        return False

    # ---- 主流程 ----
    def run(self):
        c = self.cfg
        try:
            logging.info("=" * 60)
            logging.info("T1L ADB Ping Monitor 启动")
            logging.info(f"  目标地址: {c['target']}")
            logging.info(f"  每轮包数: {c['count']}")
            logging.info(f"  轮次间隔: {c['interval']} 秒")
            logging.info(f"  网卡: {c['iface']}  设备IP: {c['dev_ip']}")
            logging.info("=" * 60)

            logging.info("等待 ADB 设备连接...")
            waited = 0
            while not self.check_device():
                if self.stop_event.is_set():
                    return
                if waited % 30 == 0:
                    logging.info("  设备未连接，5秒后重试...")
                if gc.sleep_interruptible(self.stop_event, 5):
                    return
                waited += 5
                if c['connect_timeout'] > 0 and waited >= c['connect_timeout']:
                    message = f"设备连接超时（{c['connect_timeout']}s），任务结束"
                    logging.error(message)
                    self.error_signal.emit(message)
                    return

            logging.info("设备已连接，配置 IP 地址...")
            if not self.configure_network():
                raise RuntimeError("设备 IP 配置失败，测试终止")

            round_num = 0
            while not self.stop_event.is_set():
                round_num += 1
                logging.info(f"--- 第 {round_num} 轮 ping 测试 ---")

                if not self.check_device():
                    logging.error("[错误] 未检测到 ADB 设备，等待下一轮...")
                    if gc.sleep_interruptible(self.stop_event, c['interval']):
                        break
                    continue

                success, fail, raw = self.do_ping()
                if self.stop_event.is_set():
                    break
                logging.info(f"  成功: {success}/{c['count']}, 失败: {fail}/{c['count']}")
                if raw.strip():
                    logging.info("  ping 原始输出:\n" + raw.strip())

                if fail == 0 and success == c['count']:
                    logging.info("[结果] 全部 ping 成功，准备重启设备")
                    self.do_reboot()

                    if c['restart_adb_server']:
                        self.restart_adb_server()

                    if c['post_reboot_wait'] > 0:
                        logging.info(f"  等待 {c['post_reboot_wait']}s 让设备重新上线...")
                        if gc.sleep_interruptible(self.stop_event, c['post_reboot_wait']):
                            break

                    if self.wait_device(c['reconnect_timeout'], 5):
                        logging.info("  设备已重新连接，重新配置 IP")
                        if not self.configure_network():
                            raise RuntimeError("重启后设备 IP 配置失败，测试终止")
                    else:
                        if not self.stop_event.is_set():
                            logging.warning(
                                f"  [警告] 设备在 {c['reconnect_timeout']}s 内未重新连接")
                else:
                    logging.info("[结果] 存在失败的 ping 包，不重启设备")

                logging.info(f"  等待 {c['interval']}s 后进行下一轮...")
                if gc.sleep_interruptible(self.stop_event, c['interval']):
                    break

            logging.info("T1L 监控已停止")
        except Exception as ex:
            import traceback
            if not self.stop_event.is_set():
                self.error_signal.emit(f"{ex}\n{traceback.format_exc()}")
        finally:
            self.finished_signal.emit()


class T1LPanel(QWidget):
    """ADB Ping（T1L）面板，page 为宿主 NetPage。"""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.worker = None
        self.stop_event = threading.Event()
        self._round = 0
        self._ok_rounds = 0
        self._bad_rounds = 0
        self._reboots = 0
        self._start_time = 0
        self._log_file = ""
        self._stop_requested = False
        self._worker_error = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._build_ui()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)

        scroll = BasePage.scroll_container()
        form_host = scroll.widget()
        form_layout = form_host.layout()

        grp = QGroupBox("当前项目")
        f = QFormLayout(grp)
        f.setSpacing(8)
        self.lbl_project = QLabel("未配置")
        f.addRow("设备连接:", self.lbl_project)
        self.spin_connect_timeout = BasePage.spin(0, 36000, 0, "秒")
        self.spin_connect_timeout.setToolTip("等待设备连接的最长时间，0=一直等待")
        f.addRow("等待连接超时:", self.spin_connect_timeout)
        form_layout.addWidget(grp)

        grp2 = QGroupBox("Ping 参数")
        f2 = QFormLayout(grp2)
        f2.setSpacing(8)
        self.edit_target = QLineEdit("192.168.20.10")
        f2.addRow("目标地址:", self.edit_target)
        self.edit_iface = QLineEdit("eth0")
        f2.addRow("网卡接口:", self.edit_iface)
        self.edit_dev_ip = QLineEdit("192.168.20.20")
        f2.addRow("设备侧 IP:", self.edit_dev_ip)
        self.spin_count = BasePage.spin(1, 1000, 10, "包")
        f2.addRow("每轮包数:", self.spin_count)
        self.spin_ping_timeout = BasePage.spin(1, 60, 5, "秒")
        f2.addRow("单包超时 (-W):", self.spin_ping_timeout)
        self.spin_interval = BasePage.spin(5, 86400, 360, "秒")
        f2.addRow("轮次间隔:", self.spin_interval)
        form_layout.addWidget(grp2)

        grp3 = QGroupBox("重启后处理")
        v3 = QVBoxLayout(grp3)
        f3 = QFormLayout()
        f3.setSpacing(8)
        self.chk_restart_adb = QCheckBox("reboot 后重启 ADB 服务")
        self.chk_restart_adb.setChecked(True)
        f3.addRow(self.chk_restart_adb)
        self.spin_post_reboot = BasePage.spin(0, 3600, 60, "秒")
        f3.addRow("reboot 后等待:", self.spin_post_reboot)
        self.spin_reconnect = BasePage.spin(10, 3600, 150, "秒")
        f3.addRow("重连超时:", self.spin_reconnect)
        v3.addLayout(f3)
        form_layout.addWidget(grp3)

        grp4 = QGroupBox("日志")
        h = QHBoxLayout(grp4)
        self.edit_log_dir = QLineEdit("")
        self.edit_log_dir.setPlaceholderText("留空=程序目录/logs/t1l_<时间戳>/")
        h.addWidget(self.edit_log_dir)
        btn = QPushButton("浏览...")
        btn.setFixedWidth(80)
        btn.clicked.connect(self._browse_dir)
        h.addWidget(btn)
        form_layout.addWidget(grp4)
        form_layout.addStretch()
        ll.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.btn_start = QPushButton("▶  开始监控")
        self.btn_start.setFixedHeight(44)
        self.btn_start.setStyleSheet(gc.STYLE_START_BTN)
        self.btn_stop = QPushButton("■  停止")
        self.btn_stop.setFixedHeight(44)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(gc.STYLE_STOP_BTN)
        btn_row.addWidget(self.btn_start, 2)
        btn_row.addWidget(self.btn_stop, 1)
        ll.addLayout(btn_row)
        ll.addStretch()

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.card_round = StatCard("当前轮次", "0", "#74b9ff")
        self.card_status = StatCard("状态", "就绪", "#636e72")
        self.card_ok = StatCard("全成功轮次", "0", "#00b894")
        self.card_bad = StatCard("有失败轮次", "0", "#e17055")
        self.card_reboot = StatCard("重启次数", "0", "#fdcb6e")
        self.card_dev = StatCard("设备", "未连接", "#b2bec3")
        self.card_elapsed = StatCard("运行时长", "00:00:00", "#b2bec3")
        self.card_interval = StatCard("下一轮", "--", "#a29bfe")
        for i, c in enumerate([self.card_round, self.card_status, self.card_ok, self.card_bad,
                               self.card_reboot, self.card_dev, self.card_elapsed, self.card_interval]):
            grid.addWidget(c, i // 4, i % 4)
        rl.addLayout(grid)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("就绪")
        rl.addWidget(self.progress_bar)
        rl.addStretch()

        root.addWidget(left, 1)
        root.addWidget(right, 2)

        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.refresh_project()

    def _browse_dir(self):
        cur = self.edit_log_dir.text().strip() or gc.app_dir()
        d = QFileDialog.getExistingDirectory(self, "选择日志根目录", cur)
        if d:
            self.edit_log_dir.setText(d)

    def collect(self):
        return {
            'CONNECT_TIMEOUT': self.spin_connect_timeout.value(),
            'TARGET': self.edit_target.text().strip(),
            'IFACE': self.edit_iface.text().strip(),
            'DEV_IP': self.edit_dev_ip.text().strip(),
            'COUNT': self.spin_count.value(),
            'PING_TIMEOUT': self.spin_ping_timeout.value(),
            'INTERVAL': self.spin_interval.value(),
            'RESTART_ADB': self.chk_restart_adb.isChecked(),
            'POST_REBOOT_WAIT': self.spin_post_reboot.value(),
            'RECONNECT_TIMEOUT': self.spin_reconnect.value(),
            'LOG_DIR': self.edit_log_dir.text(),
        }

    def apply_config(self, cfg):
        g = cfg.get
        self.spin_connect_timeout.setValue(g('CONNECT_TIMEOUT', 0))
        self.edit_target.setText(g('TARGET', '192.168.20.10'))
        self.edit_iface.setText(g('IFACE', 'eth0'))
        self.edit_dev_ip.setText(g('DEV_IP', '192.168.20.20'))
        self.spin_count.setValue(g('COUNT', 10))
        self.spin_ping_timeout.setValue(g('PING_TIMEOUT', 5))
        self.spin_interval.setValue(g('INTERVAL', 360))
        self.chk_restart_adb.setChecked(g('RESTART_ADB', True))
        self.spin_post_reboot.setValue(g('POST_REBOOT_WAIT', 60))
        self.spin_reconnect.setValue(g('RECONNECT_TIMEOUT', 150))
        self.edit_log_dir.setText(g('LOG_DIR', ''))

    def refresh_project(self):
        project = gc.get_active_project()
        name = project.get("name", "未配置")
        if not project:
            self.lbl_project.setText("未配置项目")
        elif not project.get("adb_enabled", True):
            self.lbl_project.setText(f"{name} | ADB 未启用")
        else:
            serial = project.get("adb_serial", "") or "自动选择"
            self.lbl_project.setText(f"{name} | {serial}")

    # ==================== 运行 ====================
    def _on_start(self):
        if self.page.is_running:
            self.page.append_log(
                f"已有网络测试正在运行（{self.page.active_name}），请先停止", '#fdcb6e')
            return
        if not self.edit_target.text().strip():
            self.page.append_log("请先填写 Ping 目标地址", '#fdcb6e')
            return
        project = gc.get_active_project()
        adb_args, adb_enabled = gc.project_adb_args(project)
        if not project or not adb_enabled:
            self.page.append_log("当前项目未启用 ADB，请先在项目管理中配置。", '#fdcb6e')
            return
        if not self.page.try_acquire():
            return

        c = self.collect()
        log_dir = make_run_log_dir("t1l", c['LOG_DIR'])
        self._log_file = os.path.join(log_dir, "t1l_monitor.log")

        self.page.begin_run(self)
        self.page.clear_log()
        self._round = self._ok_rounds = self._bad_rounds = self._reboots = 0
        self._start_time = time.time()
        self._stop_requested = False
        self._worker_error = False
        self._reset_stats()

        self.page.attach_logging(self._log_file)
        self.stop_event.clear()
        self.cfg = {
            'adb_path': adb_args[0],
            'serial': adb_args[2] if len(adb_args) > 2 else '',
            'connect_timeout': c['CONNECT_TIMEOUT'],
            'target': c['TARGET'],
            'iface': c['IFACE'],
            'dev_ip': c['DEV_IP'],
            'count': c['COUNT'],
            'ping_timeout': c['PING_TIMEOUT'],
            'interval': c['INTERVAL'],
            'restart_adb_server': c['RESTART_ADB'],
            'post_reboot_wait': c['POST_REBOOT_WAIT'],
            'reconnect_timeout': c['RECONNECT_TIMEOUT'],
        }

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("监控运行中...")
        self.card_status.set_value("等待设备", "#fdcb6e")
        self._timer.start(1000)

        self.worker = T1LWorker(self.cfg, self.stop_event)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.error_signal.connect(self._on_worker_error)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def prepare_stop(self):
        self._stop_requested = True
        self.stop_event.set()

    def _on_stop(self):
        self._stop_requested = True
        self.stop_event.set()
        self.btn_stop.setEnabled(False)
        self.card_status.set_value("停止中...", "#fdcb6e")
        self.page.append_log("正在停止监控...", '#fdcb6e')

    def _on_worker_error(self, message):
        self._worker_error = True
        self.page.append_log(f"监控出错: {message}", '#e17055')

    def _on_finished(self):
        self._timer.stop()
        self.page.detach_logging()
        self.worker = None
        self.page.release_run()
        self.page.end_run()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        if self._worker_error:
            status, color = "出错", "#e17055"
            message = "监控出错"
        elif self._stop_requested:
            status, color = "已停止", "#636e72"
            message = "监控已停止"
        else:
            status, color = "已完成", "#00b894"
            message = "监控已完成"
        self.progress_bar.setFormat(
            f"{status} | 轮次 {self._round} | 全成功 {self._ok_rounds} | 有失败 {self._bad_rounds}")
        self.card_status.set_value(status, color)
        self.page.append_log(message, color, bold=True)

    def _reset_stats(self):
        self.card_round.set_value("0", "#74b9ff")
        self.card_status.set_value("就绪", "#636e72")
        self.card_ok.set_value("0", "#00b894")
        self.card_bad.set_value("0", "#e17055")
        self.card_reboot.set_value("0", "#fdcb6e")
        self.card_dev.set_value("未连接", "#b2bec3")
        self.card_elapsed.set_value("00:00:00", "#b2bec3")
        self.card_interval.set_value("--", "#a29bfe")

    def _tick(self):
        if self._start_time:
            self.card_elapsed.set_value(gc.format_duration(time.time() - self._start_time))
            self.page.title_changed.emit(
                f"第{self._round}轮 | 全成功:{self._ok_rounds} 有失败:{self._bad_rounds}")

    def on_log_line(self, text):
        m = re.search(r'--- 第 (\d+) 轮 ping 测试 ---', text)
        if m:
            self._round = int(m.group(1))
            self.card_round.set_value(str(self._round), "#74b9ff")
            self.card_status.set_value("测试中", "#74b9ff")

        if '设备已连接' in text:
            self.card_dev.set_value("已连接", "#00b894")
        elif '设备未连接' in text or '未检测到 ADB 设备' in text:
            self.card_dev.set_value("未连接", "#e17055")
            self.card_status.set_value("等待设备", "#fdcb6e")

        sm = re.search(r'成功: (\d+)/(\d+), 失败: (\d+)/(\d+)', text)
        if sm:
            ok, total = int(sm.group(1)), int(sm.group(2))
            if ok == total:
                self._ok_rounds += 1
                self.card_ok.set_value(str(self._ok_rounds), "#00b894")
            else:
                self._bad_rounds += 1
                self.card_bad.set_value(str(self._bad_rounds), "#e17055")

        if '全部 ping 成功，准备重启设备' in text:
            self._reboots += 1
            self.card_reboot.set_value(str(self._reboots), "#fdcb6e")
        if '等待' in text and '后进行下一轮' in text:
            self.card_status.set_value("等待下一轮", "#a29bfe")
