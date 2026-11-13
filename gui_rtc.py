# -*- coding: utf-8 -*-
"""
循环读取时间页面 — 由 循环读取时间.py 迁移。
通过 ADB 周期性读取设备 RTC（默认 rf_test -f ext_rtc get），按时间戳追加写入文件。
已把 adbutils 依赖改为直接调用 adb 命令行，便于打包为独立 exe。
"""

import logging
import os
import subprocess
import threading
import time
from datetime import datetime

from PyQt5.QtWidgets import (
    QWidget, QGroupBox, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QPushButton, QGridLayout, QProgressBar, QFileDialog, QLabel
)
from PyQt5.QtCore import QThread, QTimer, pyqtSignal

import gui_common as gc
from gui_common import BasePage, StatCard, make_run_log_dir
from process_utils import run_cancellable_process


class RtcWorker(QThread):
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)
    sample_signal = pyqtSignal(str, str)   # (pc_time, rtc_value)

    def __init__(self, cfg, stop_event, output_file, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.stop_event = stop_event
        self.output_file = output_file

    def _adb(self, args, timeout=30):
        cmd = [self.cfg['adb_path']]
        if self.cfg['serial']:
            cmd += ['-s', self.cfg['serial']]
        cmd += args
        try:
            r = run_cancellable_process(cmd, stop_event=self.stop_event,
                                        timeout=timeout, label="RTC ADB")
            return r.success, (r.output if r.success else r.output or r.error or r.reason).strip()
        except subprocess.TimeoutExpired:
            return False, "命令执行超时"
        except Exception as ex:
            return False, str(ex)

    def _shell(self, cmd, timeout=30):
        exports = [value.strip() for value in self.cfg['exports'] if value.strip()]
        return self._adb(['shell', ' && '.join(exports + [cmd])], timeout=timeout)

    def check_device(self):
        ok, out = self._adb(['devices'], timeout=15)
        if not ok:
            return False, out

        serial = self.cfg['serial']
        for line in out.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) < 2 or parts[1] != 'device':
                continue
            if not serial or parts[0] == serial:
                return True, parts[0]
        if serial:
            return False, f"未检测到项目配置的 ADB 设备: {serial}"
        return False, "未检测到可用的 ADB 设备"

    def run(self):
        c = self.cfg
        try:
            logging.info("=" * 60)
            logging.info("RTC 循环读取启动")
            logging.info(f"  读取命令: {c['rtc_cmd']}")
            logging.info(f"  采样间隔: {c['interval']}s")
            logging.info(f"  总时长: {c['total']}s" if c['total'] > 0 else "  总时长: 无限（手动停止）")
            logging.info(f"  输出文件: {self.output_file}")
            logging.info("=" * 60)

            connected, device_info = self.check_device()
            if self.stop_event.is_set():
                return
            if not connected:
                message = f"ADB 设备检查失败: {device_info}"
                logging.error(message)
                self.error_signal.emit(message)
                return
            logging.info(f"ADB 设备检查通过: {device_info}")

            logging.info("每次读取将在同一 shell 中设置环境变量并执行命令")

            with open(self.output_file, 'a', encoding='utf-8') as fh:
                fh.write(f"# 会话开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                         f"| 命令: {c['rtc_cmd']}\n")
                fh.flush()

                start_time = time.time()
                count = 0
                while not self.stop_event.is_set():
                    if c['total'] > 0 and (time.time() - start_time) >= c['total']:
                        logging.info("已达到设定总时长，读取结束")
                        break

                    pc_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ok, rtc_value = self._shell(
                        c['rtc_cmd'], timeout=c['timeout'])
                    if self.stop_event.is_set():
                        break
                    if not ok or not rtc_value:
                        rtc_value = "(读取失败)"

                    line = f"{pc_time} {rtc_value}"
                    fh.write(line + "\n")
                    fh.flush()
                    count += 1
                    logging.info(line)
                    self.sample_signal.emit(pc_time, rtc_value)

                    # 间隔期间可被打断
                    if gc.sleep_interruptible(self.stop_event, c['interval']):
                        break

            logging.info(f"读取结束，本次共采样 {count} 次")
        except Exception as ex:
            import traceback
            self.error_signal.emit(f"{ex}\n{traceback.format_exc()}")
        finally:
            self.finished_signal.emit()


class RtcPage(BasePage):
    def __init__(self, parent=None):
        super().__init__("rtc", "循环读取时间", parent)
        self.worker = None
        self.stop_event = threading.Event()
        self._count = 0
        self._errors = 0
        self._start_time = 0
        self._output_file = ""
        self._stop_requested = False
        self._worker_error = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._build_ui()
        self.restore_config()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)

        scroll = self.scroll_container()
        form_host = scroll.widget()
        form_layout = form_host.layout()

        grp = QGroupBox("当前项目")
        f = QFormLayout(grp)
        f.setSpacing(8)
        self.lbl_project = QLabel("未配置")
        f.addRow("设备连接:", self.lbl_project)
        form_layout.addWidget(grp)

        grp2 = QGroupBox("读取命令")
        f2 = QFormLayout(grp2)
        f2.setSpacing(8)
        self.edit_rtc_cmd = QLineEdit("rf_test -f ext_rtc get")
        f2.addRow("RTC 读取命令:", self.edit_rtc_cmd)
        self.edit_export1 = QLineEdit("export PATH=/oemdata/exe:/usr/bin:$PATH")
        f2.addRow("环境变量 1:", self.edit_export1)
        self.edit_export2 = QLineEdit(
            "export LD_LIBRARY_PATH=/oemdata/lib:/usr/lib:$LD_LIBRARY_PATH")
        f2.addRow("环境变量 2:", self.edit_export2)
        self.spin_cmd_timeout = self.spin(1, 300, 15, "秒")
        f2.addRow("单次命令超时:", self.spin_cmd_timeout)
        form_layout.addWidget(grp2)

        grp3 = QGroupBox("采样")
        f3 = QFormLayout(grp3)
        f3.setSpacing(8)
        self.spin_interval = self.spin(1, 86400, 5, "秒")
        f3.addRow("采样间隔:", self.spin_interval)
        self.spin_total = self.spin(0, 1000000, 24 * 60, "分钟")
        self.spin_total.setToolTip("0 = 一直读取直到手动停止")
        f3.addRow("总时长:", self.spin_total)
        form_layout.addWidget(grp3)

        grp4 = QGroupBox("输出")
        h = QHBoxLayout(grp4)
        self.edit_out_dir = QLineEdit("")
        self.edit_out_dir.setPlaceholderText("留空=程序目录/logs/rtc_<时间戳>/")
        h.addWidget(self.edit_out_dir)
        btn = QPushButton("浏览...")
        btn.setFixedWidth(80)
        btn.clicked.connect(self._browse_dir)
        h.addWidget(btn)
        form_layout.addWidget(grp4)
        f4 = QFormLayout()
        f4.setSpacing(8)
        self.edit_out_name = QLineEdit("output.txt")
        f4.addRow("文件名:", self.edit_out_name)
        form_layout.addLayout(f4)
        form_layout.addStretch()
        ll.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.btn_start = QPushButton("▶  开始读取")
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
        self.card_count = StatCard("采样次数", "0", "#74b9ff")
        self.card_status = StatCard("状态", "就绪", "#636e72")
        self.card_last = StatCard("最新 RTC", "--", "#00b894")
        self.card_last_pc = StatCard("PC 时间", "--", "#b2bec3")
        self.card_errors = StatCard("读取失败", "0", "#e17055")
        self.card_elapsed = StatCard("运行时长", "00:00:00", "#b2bec3")
        for i, c in enumerate([self.card_count, self.card_status, self.card_last,
                               self.card_last_pc, self.card_errors, self.card_elapsed]):
            grid.addWidget(c, i // 3, i % 3)
        rl.addLayout(grid)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("就绪")
        rl.addWidget(self.progress_bar)
        rl.addWidget(self.build_log_panel(
            with_open_dir=True, log_dir_getter=lambda: os.path.dirname(self._output_file)), 1)

        root.addWidget(left, 1)
        root.addWidget(right, 2)

        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.bind_autosave([self.edit_rtc_cmd,
                            self.edit_export1, self.edit_export2, self.spin_cmd_timeout,
                            self.spin_interval, self.spin_total,
                            self.edit_out_dir, self.edit_out_name])
        self.refresh_project()

    def _browse_dir(self):
        cur = self.edit_out_dir.text().strip() or gc.app_dir()
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", cur)
        if d:
            self.edit_out_dir.setText(d)

    def collect(self):
        return {
            'RTC_CMD': self.edit_rtc_cmd.text().strip(),
            'EXPORT1': self.edit_export1.text(),
            'EXPORT2': self.edit_export2.text(),
            'CMD_TIMEOUT': self.spin_cmd_timeout.value(),
            'INTERVAL': self.spin_interval.value(),
            'TOTAL_MIN': self.spin_total.value(),
            'OUT_DIR': self.edit_out_dir.text(),
            'OUT_NAME': self.edit_out_name.text().strip() or 'output.txt',
        }

    def apply_config(self, cfg):
        g = cfg.get
        self.edit_rtc_cmd.setText(g('RTC_CMD', 'rf_test -f ext_rtc get'))
        self.edit_export1.setText(g('EXPORT1', 'export PATH=/oemdata/exe:/usr/bin:$PATH'))
        self.edit_export2.setText(
            g('EXPORT2', 'export LD_LIBRARY_PATH=/oemdata/lib:/usr/lib:$LD_LIBRARY_PATH'))
        self.spin_cmd_timeout.setValue(g('CMD_TIMEOUT', 15))
        self.spin_interval.setValue(g('INTERVAL', 5))
        self.spin_total.setValue(g('TOTAL_MIN', 24 * 60))
        self.edit_out_dir.setText(g('OUT_DIR', ''))
        self.edit_out_name.setText(g('OUT_NAME', 'output.txt'))

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
        if not self.edit_rtc_cmd.text().strip():
            self.append_log("请先填写 RTC 读取命令", '#fdcb6e')
            return
        project = gc.get_active_project()
        adb_args, adb_enabled = gc.project_adb_args(project)
        if not project or not adb_enabled:
            self.append_log("当前项目未启用 ADB，请先在项目管理中配置。", '#fdcb6e')
            return
        if not self.try_acquire():
            return

        c = self.collect()
        out_dir = make_run_log_dir("rtc", c['OUT_DIR'])
        self._output_file = os.path.join(out_dir, c['OUT_NAME'])

        self.clear_log()
        self._count = 0
        self._errors = 0
        self._start_time = time.time()
        self._stop_requested = False
        self._worker_error = False
        self._reset_stats()

        self.attach_logging(os.path.join(out_dir, "rtc_read.log"))
        self.stop_event.clear()
        self.cfg = {
            'adb_path': adb_args[0],
            'serial': adb_args[2] if len(adb_args) > 2 else '',
            'rtc_cmd': c['RTC_CMD'],
            'exports': [c['EXPORT1'], c['EXPORT2']],
            'timeout': c['CMD_TIMEOUT'],
            'interval': c['INTERVAL'],
            'total': c['TOTAL_MIN'] * 60,
        }

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        if self.cfg['total'] > 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("读取中... 0%")
        else:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("读取中...")
        self.card_status.set_value("读取中", "#00b894")
        self._timer.start(1000)

        self.worker = RtcWorker(self.cfg, self.stop_event, self._output_file)
        self.worker.sample_signal.connect(self._on_sample)
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

    def _on_worker_error(self, message):
        self._worker_error = True
        self.append_log(f"读取出错: {message}", '#e17055')

    def _on_finished(self):
        self._timer.stop()
        self.detach_logging()
        self.worker = None
        self.release_run()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        if self._worker_error:
            status, color = "出错", "#e17055"
            message = "读取出错"
        elif self._stop_requested:
            status, color = "已停止", "#636e72"
            message = "读取已停止"
        else:
            status, color = "已完成", "#00b894"
            message = "读取已完成"
        self.progress_bar.setFormat(
            f"{status} | 采样 {self._count} 次 | 读取失败 {self._errors} 次")
        self.card_status.set_value(status, color)
        self.append_log(
            f"{message}，输出文件: {self._output_file}", color, bold=True)

    def _reset_stats(self):
        self.card_count.set_value("0", "#74b9ff")
        self.card_status.set_value("就绪", "#636e72")
        self.card_last.set_value("--", "#00b894")
        self.card_last_pc.set_value("--", "#b2bec3")
        self.card_errors.set_value("0", "#e17055")
        self.card_elapsed.set_value("00:00:00", "#b2bec3")

    def _on_sample(self, pc_time, value):
        self._count += 1
        self.card_count.set_value(str(self._count), "#74b9ff")
        self.card_last_pc.set_value(pc_time.split(" ")[-1], "#b2bec3")
        if value and value != "(读取失败)" and "error" not in value.lower():
            self.card_last.set_value(value[:28], "#00b894")
        else:
            self._errors += 1
            self.card_errors.set_value(str(self._errors), "#e17055")
            self.card_last.set_value("读取失败", "#e17055")

    def _tick(self):
        if self._start_time:
            elapsed = time.time() - self._start_time
            self.card_elapsed.set_value(gc.format_duration(elapsed))
            if self.cfg.get('total', 0) > 0:
                pct = min(100, int(elapsed / self.cfg['total'] * 100))
                self.progress_bar.setValue(pct)
                self.progress_bar.setFormat(f"读取中... {pct}%")
            self.title_changed.emit(f"采样 {self._count} 次")
