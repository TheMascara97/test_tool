# -*- coding: utf-8 -*-
"""
PC 端持续 Ping 面板 — 由 ping_test.py 迁移，支持不间断 ping 与抖动检测。
作为「网络测试」页面（gui_net.py）中的一个 Tab 使用，通过 self.page 访问宿主页面。
"""

import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

from PyQt5.QtWidgets import (
    QWidget, QGroupBox, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QCheckBox, QPushButton, QGridLayout, QProgressBar, QFileDialog
)
from PyQt5.QtCore import QThread, QTimer, pyqtSignal

import gui_common as gc
from gui_common import BasePage, StatCard, make_run_log_dir


def run_ping(target, source="", size=32, timeout=2):
    """执行一次 ping，返回 (success, bytes, time_ms, ttl, raw)"""
    cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000))]
    if source:
        cmd += ["-S", source]
    if size:
        cmd += ["-l", str(size)]
    cmd.append(target)

    kwargs = {"capture_output": True, "text": True, "timeout": timeout + 5}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(cmd, **kwargs)
        output = result.stdout or ""
        if "TTL" in output or "ttl" in output:
            m_bytes = re.search(r"bytes[=<]\s*(\d+)|字节[=<]?\s*(\d+)", output)
            m_time = re.search(r"time[=<]\s*([\d]+)|时间[=<]?\s*([\d]+)", output)
            m_ttl = re.search(r"[Tt][Tt][Ll][=<]\s*(\d+)", output)
            b = (m_bytes.group(1) or m_bytes.group(2)) if m_bytes else str(size)
            t = (m_time.group(1) or m_time.group(2)) if m_time else "N/A"
            ttl = m_ttl.group(1) if m_ttl else "N/A"
            return True, b, t, ttl, output
        return False, None, None, None, output
    except subprocess.TimeoutExpired:
        return False, None, None, None, "命令执行超时"
    except Exception as ex:
        return False, None, None, None, str(ex)


class PingWorker(QThread):
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, cfg, stop_event, log_file, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.stop_event = stop_event
        self.log_file = log_file

    def run(self):
        c = self.cfg
        target = c['target']
        total = 0
        fail_streak = 0
        recent = []
        try:
            with open(self.log_file, 'a', encoding='utf-8') as fh:
                header = f"{target} 的 Ping 统计信息:"
                logging.info(header)
                fh.write(header + "\n")
                fh.flush()

                while not self.stop_event.is_set():
                    ok, b, t, ttl, raw = run_ping(
                        target, c['source'], c['size'], c['timeout'])
                    total += 1
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    recent.append(ok)
                    if len(recent) > 3:
                        recent.pop(0)
                    flap = (len(recent) == 3 and recent == [False, True, False])

                    if ok:
                        fail_streak = 0
                        line = f"{now}第{total}次ping包来自 {target} 的回复: 字节={b} 时间={t}ms TTL={ttl}"
                    else:
                        fail_streak += 1
                        line = f"{now}第{total}次ping包失败 (连续失败={fail_streak})"

                    logging.info(line)
                    fh.write(line + "\n")
                    if flap:
                        flap_line = f"{now} [抖动检测] 检测到超时->成功->超时模式"
                        logging.warning(flap_line)
                        fh.write(flap_line + "\n")
                    fh.flush()

                    if c['stop_on_fail'] and fail_streak >= c['max_fail']:
                        logging.warning(
                            f"连续失败 {fail_streak} 次，达到阈值 {c['max_fail']}，停止 ping")
                        break

                    if gc.sleep_interruptible(self.stop_event, c['interval']):
                        break
        except Exception as ex:
            import traceback
            self.error_signal.emit(f"{ex}\n{traceback.format_exc()}")
        finally:
            self.finished_signal.emit()


class PingPanel(QWidget):
    """
    PC 端 Ping 面板。

    page: 宿主 NetPage，提供 try_acquire / release_run / attach_logging /
          append_log / clear_log / title_changed / begin_run / end_run。
    """

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.worker = None
        self.stop_event = threading.Event()
        self._stat_total = 0
        self._stat_ok = 0
        self._stat_fail = 0
        self._stat_flap = 0
        self._last_streak = 0
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

        grp = QGroupBox("Ping 参数")
        f = QFormLayout(grp)
        f.setSpacing(8)
        self.edit_target = QLineEdit("")
        self.edit_target.setPlaceholderText("从当前项目 SSH 配置读取")
        f.addRow("目标地址:", self.edit_target)
        self.edit_source = QLineEdit("")
        self.edit_source.setPlaceholderText("留空=系统自动选择网卡")
        f.addRow("源地址 (-S):", self.edit_source)
        self.spin_size = BasePage.spin(1, 65500, 32, "字节")
        f.addRow("包大小 (-l):", self.spin_size)
        self.spin_timeout = BasePage.spin(1, 30, 2, "秒")
        f.addRow("单包超时:", self.spin_timeout)
        self.spin_interval = BasePage.spin(1, 3600, 1, "秒")
        f.addRow("发包间隔:", self.spin_interval)
        ll.addWidget(grp)

        grp2 = QGroupBox("停止条件")
        v = QVBoxLayout(grp2)
        self.chk_stop_on_fail = QCheckBox("连续失败达到阈值时自动停止")
        self.chk_stop_on_fail.setChecked(False)
        v.addWidget(self.chk_stop_on_fail)
        f2 = QFormLayout()
        f2.setSpacing(8)
        self.spin_max_fail = BasePage.spin(1, 9999, 10, "次")
        f2.addRow("连续失败阈值:", self.spin_max_fail)
        v.addLayout(f2)
        ll.addWidget(grp2)

        grp3 = QGroupBox("日志")
        h = QHBoxLayout(grp3)
        self.edit_log_dir = QLineEdit("")
        self.edit_log_dir.setPlaceholderText("留空=程序目录/logs/ping_<时间戳>/")
        h.addWidget(self.edit_log_dir)
        btn = QPushButton("浏览...")
        btn.setFixedWidth(80)
        btn.clicked.connect(self._browse_dir)
        h.addWidget(btn)
        ll.addWidget(grp3)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.btn_start = QPushButton("▶  开始 Ping")
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
        self.card_total = StatCard("已发送", "0", "#74b9ff")
        self.card_ok = StatCard("成功", "0", "#00b894")
        self.card_fail = StatCard("失败", "0", "#e17055")
        self.card_loss = StatCard("丢包率", "--", "#fdcb6e")
        self.card_lat = StatCard("最新延迟", "--", "#a29bfe")
        self.card_streak = StatCard("连续失败", "0", "#e17055")
        self.card_flap = StatCard("抖动次数", "0", "#fdcb6e")
        self.card_elapsed = StatCard("运行时长", "00:00:00", "#b2bec3")
        for i, c in enumerate([self.card_total, self.card_ok, self.card_fail, self.card_loss,
                               self.card_lat, self.card_streak, self.card_flap, self.card_elapsed]):
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
        self.chk_stop_on_fail.stateChanged.connect(
            lambda: self.spin_max_fail.setEnabled(self.chk_stop_on_fail.isChecked()))

    def _browse_dir(self):
        cur = self.edit_log_dir.text().strip() or gc.app_dir()
        d = QFileDialog.getExistingDirectory(self, "选择日志根目录", cur)
        if d:
            self.edit_log_dir.setText(d)

    def collect(self):
        return {
            'TARGET': self.edit_target.text().strip(),
            'SOURCE': self.edit_source.text().strip(),
            'SIZE': self.spin_size.value(),
            'TIMEOUT': self.spin_timeout.value(),
            'INTERVAL': self.spin_interval.value(),
            'STOP_ON_FAIL': self.chk_stop_on_fail.isChecked(),
            'MAX_FAIL': self.spin_max_fail.value(),
            'LOG_DIR': self.edit_log_dir.text(),
        }

    def apply_config(self, cfg):
        g = cfg.get
        self.edit_target.setText(g('TARGET', ''))
        self.edit_source.setText(g('SOURCE', ''))
        self.spin_size.setValue(g('SIZE', 32))
        self.spin_timeout.setValue(g('TIMEOUT', 2))
        self.spin_interval.setValue(g('INTERVAL', 1))
        self.chk_stop_on_fail.setChecked(g('STOP_ON_FAIL', False))
        self.spin_max_fail.setValue(g('MAX_FAIL', 10))
        self.edit_log_dir.setText(g('LOG_DIR', ''))
        self.spin_max_fail.setEnabled(self.chk_stop_on_fail.isChecked())

    def refresh_project(self):
        ssh = gc.project_ssh_config()
        self.edit_target.setText(ssh.get("host", ""))
        enabled = ssh.get("enabled", False)
        self.edit_target.setEnabled(enabled)
        self.edit_target.setToolTip(
            f"当前项目 SSH 地址: {ssh.get('host', '')}" if enabled
            else "当前项目未启用 SSH，请先在项目管理中配置")

    # ==================== 运行 ====================
    def _on_start(self):
        if self.page.is_running:
            self.page.append_log(
                f"已有网络测试正在运行（{self.page.active_name}），请先停止", '#fdcb6e')
            return
        ssh = gc.project_ssh_config()
        if not ssh.get("enabled"):
            self.page.append_log("当前项目未启用 SSH，无法启动 Ping", '#e17055')
            logging.warning("Ping 启动被阻止: 当前项目未启用 SSH")
            return
        target = str(ssh.get("host", "")).strip()
        if not target:
            self.page.append_log("当前项目 SSH 地址为空，无法启动 Ping", '#e17055')
            logging.warning("Ping 启动被阻止: 当前项目 SSH 地址为空")
            return
        self.edit_target.setText(target)
        if not self.page.try_acquire():
            return

        c = self.collect()
        c['TARGET'] = target
        log_dir = make_run_log_dir("ping", c['LOG_DIR'])
        self._log_file = os.path.join(log_dir, "ping_test.log")

        self.page.begin_run(self)
        self.page.clear_log()
        self._stat_total = self._stat_ok = self._stat_fail = self._stat_flap = 0
        self._last_streak = 0
        self._start_time = time.time()
        self._stop_requested = False
        self._worker_error = False
        self._reset_stats()

        self.page.attach_logging(self._log_file)
        logging.info(f"Ping {c['TARGET']}  源: {c['SOURCE'] or '自动'}  "
                     f"包: {c['SIZE']}B  间隔: {c['INTERVAL']}s  超时: {c['TIMEOUT']}s")
        logging.info(f"日志文件: {self._log_file}")

        self.stop_event.clear()
        self.cfg_dict = {
            'target': c['TARGET'],
            'source': c['SOURCE'],
            'size': c['SIZE'],
            'timeout': c['TIMEOUT'],
            'interval': c['INTERVAL'],
            'stop_on_fail': c['STOP_ON_FAIL'],
            'max_fail': c['MAX_FAIL'],
        }

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("Ping 运行中...")
        self._timer.start(1000)

        self.worker = PingWorker(self.cfg_dict, self.stop_event, self._log_file)
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
        self.page.append_log("正在停止 Ping...", '#fdcb6e')

    def _on_worker_error(self, message):
        self._worker_error = True
        self.page.append_log(f"Ping 出错: {message}", '#e17055')

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
            message = "Ping 出错"
        elif self._stop_requested:
            status, color = "已停止", "#636e72"
            message = "Ping 已停止"
        else:
            status, color = "已完成", "#00b894"
            message = "Ping 已完成"
        self.progress_bar.setFormat(
            f"{status} | 发送 {self._stat_total} | 成功 {self._stat_ok} | 失败 {self._stat_fail}")
        self.page.append_log(message, color, bold=True)

    def _reset_stats(self):
        for card, val, color in [
                (self.card_total, "0", "#74b9ff"), (self.card_ok, "0", "#00b894"),
                (self.card_fail, "0", "#e17055"), (self.card_loss, "--", "#fdcb6e"),
                (self.card_lat, "--", "#a29bfe"), (self.card_streak, "0", "#e17055"),
                (self.card_flap, "0", "#fdcb6e"), (self.card_elapsed, "00:00:00", "#b2bec3")]:
            card.set_value(val, color)

    def _tick(self):
        if self._start_time:
            self.card_elapsed.set_value(gc.format_duration(time.time() - self._start_time))
            self.page.title_changed.emit(
                f"PC Ping 发送 {self._stat_total} | 失败 {self._stat_fail}")

    def on_log_line(self, text):
        m = re.search(r'第(\d+)次ping包', text)
        if m:
            self._stat_total = int(m.group(1))
            self.card_total.set_value(str(self._stat_total), "#74b9ff")
        if 'ping包来自' in text:
            self._stat_ok += 1
            self._last_streak = 0
            self.card_ok.set_value(str(self._stat_ok), "#00b894")
            self.card_streak.set_value("0", "#00b894")
            tm = re.search(r'时间=([\d]+)ms', text)
            if tm:
                self.card_lat.set_value(f"{tm.group(1)} ms", "#a29bfe")
        elif 'ping包失败' in text:
            self._stat_fail += 1
            self.card_fail.set_value(str(self._stat_fail), "#e17055")
            sm = re.search(r'连续失败=(\d+)', text)
            if sm:
                self._last_streak = int(sm.group(1))
                self.card_streak.set_value(str(self._last_streak),
                                           "#d63031" if self._last_streak >= 3 else "#fdcb6e")
        if '抖动检测' in text:
            self._stat_flap += 1
            self.card_flap.set_value(str(self._stat_flap), "#fdcb6e")

        if self._stat_total > 0:
            loss = self._stat_fail / self._stat_total * 100
            color = '#00b894' if loss == 0 else ('#fdcb6e' if loss < 5 else '#e17055')
            self.card_loss.set_value(f"{loss:.1f}%", color)
