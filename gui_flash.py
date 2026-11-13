# -*- coding: utf-8 -*-
"""ADB Flash 读写测试。"""

import hashlib
import os
import subprocess
import threading
import tempfile

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

import gui_common as gc


def _validate_pull_path(local_file, pull_file):
    if not pull_file.strip():
        raise ValueError("请填写回读文件路径")
    if os.path.normcase(os.path.realpath(local_file)) == os.path.normcase(os.path.realpath(pull_file)):
        raise ValueError("回读文件不能与本地源文件相同")
    if os.path.lexists(pull_file):
        raise ValueError("回读文件已存在，请选择新的文件路径，以免覆盖原文件")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FlashWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, cfg, stop_event):
        super().__init__()
        self.cfg = cfg
        self.stop_event = stop_event
        self.process = None

    def _run(self, args, timeout):
        if self.stop_event.is_set():
            raise InterruptedError("用户停止")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=creationflags)
        self.process = process
        try:
            out, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            out, _ = process.communicate()
            raise TimeoutError(out.strip() or "命令超时")
        finally:
            if self.process is process:
                self.process = None
        if self.stop_event.is_set():
            raise InterruptedError("用户停止")
        if process.returncode:
            raise RuntimeError(out.strip())
        return out.strip()

    def stop(self):
        self.stop_event.set()
        process = self.process
        if process and process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass

    def run(self):
        c = self.cfg
        cycles = c["cycles"]
        passed = 0
        staging = None
        remote_written = False
        try:
            _validate_pull_path(c["local_file"], c["pull_file"])
            if not os.path.isfile(c["local_file"]):
                raise FileNotFoundError("本地文件不存在")
            local_hash = _sha256(c["local_file"])
            local_size = os.path.getsize(c["local_file"])
            total = None if cycles == 0 else cycles
            run_no = 0
            pull_file = c["pull_file"]
            os.makedirs(os.path.dirname(os.path.abspath(pull_file)), exist_ok=True)
            staging = tempfile.TemporaryDirectory(
                prefix=".flash_readback_", dir=os.path.dirname(os.path.abspath(pull_file)))
            staged_file = os.path.join(staging.name, "readback.bin")
            while not self.stop_event.is_set() and (total is None or run_no < total):
                run_no += 1
                self.progress_signal.emit(run_no, cycles)
                self.log_signal.emit(f"第 {run_no} 次：push -> {c['remote_file']}")
                self._run(c["adb"] + ["push", c["local_file"], c["remote_file"]], c["timeout"])
                remote_written = True
                remote_hash = self._run(
                    c["adb"] + ["shell", "sha256sum", c["remote_file"]], c["timeout"]).split()[0]
                remote_size = self._run(
                    c["adb"] + ["shell", "stat", "-c", "%s", c["remote_file"]],
                    c["timeout"]).strip()
                if remote_hash.lower() != local_hash or remote_size != str(local_size):
                    raise RuntimeError("设备端 SHA-256 或文件大小校验失败")
                self.log_signal.emit("设备端校验通过，开始 pull")
                if os.path.exists(staged_file):
                    os.remove(staged_file)
                self._run(c["adb"] + ["pull", c["remote_file"], staged_file], c["timeout"])
                if not os.path.isfile(staged_file) or _sha256(staged_file) != local_hash:
                    raise RuntimeError("pull 文件本地校验失败")
                if passed == 0:
                    # 后续轮次失败或停止时，仍保留已校验成功的回读文件。
                    # 所有成功轮次均与源文件具有相同 SHA-256。
                    os.link(staged_file, pull_file)
                    self.log_signal.emit(f"回读文件已保存: {pull_file}")
                passed += 1
                self.log_signal.emit(f"[PASS] 第 {run_no} 次读写校验通过")
                if total is not None and run_no >= total:
                    break
                if gc.sleep_interruptible(self.stop_event, c["interval"]):
                    break
            if self.stop_event.is_set():
                self.finished_signal.emit(False, f"已停止，完成 {passed} 次")
            else:
                self.finished_signal.emit(True, f"完成 {passed} 次")
        except (InterruptedError, TimeoutError, FileNotFoundError, RuntimeError, OSError) as ex:
            self.finished_signal.emit(False, str(ex))
        except Exception as ex:
            self.finished_signal.emit(False, f"未处理错误: {ex}")
        finally:
            if staging is not None:
                staging.cleanup()
            if c.get("cleanup") and remote_written:
                try:
                    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    result = subprocess.run(c["adb"] + ["shell", "rm", "-f", c["remote_file"]],
                                   capture_output=True, timeout=10,
                                   creationflags=creationflags)
                    if result.returncode:
                        self.log_signal.emit("[WARNING] 设备测试文件清理失败，请手动检查")
                except Exception as ex:
                    self.log_signal.emit(f"[WARNING] 设备测试文件清理失败: {ex}")


class FlashPage(gc.BasePage):
    def __init__(self):
        super().__init__("flash", "Flash 读写")
        self.worker = None
        self.stop_event = threading.Event()
        self._worker_result = None
        self._build_ui()
        self.restore_config()
        self.refresh_project()
        self.bind_autosave([
            self.edit_local, self.edit_remote, self.edit_pull, self.spin_cycles,
            self.spin_interval, self.spin_timeout, self.chk_cleanup,
        ])

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        left = QWidget()
        left.setMinimumWidth(390)
        lv = QVBoxLayout(left)
        scroll = gc.BasePage.scroll_container()
        form_host = scroll.widget()
        form = QFormLayout()
        self.lbl_project = QLabel()
        form.addRow("当前项目:", self.lbl_project)
        self.edit_local = QLineEdit()
        local_row = QHBoxLayout()
        local_row.addWidget(self.edit_local, 1)
        btn = QPushButton("选择文件")
        btn.clicked.connect(self._browse)
        local_row.addWidget(btn)
        form.addRow("本地文件:", local_row)
        self.edit_remote = QLineEdit("/data/local/tmp/flash_test.bin")
        form.addRow("设备文件:", self.edit_remote)
        self.edit_pull = QLineEdit(os.path.join(gc.app_dir(), "logs", "flash_pull.bin"))
        form.addRow("回读文件:", self.edit_pull)
        self.spin_cycles = self.spin(0, 1000000, 1)
        self.spin_cycles.setSpecialValueText("无限循环")
        form.addRow("循环次数:", self.spin_cycles)
        self.spin_interval = self.spin(0, 86400, 1, "秒")
        form.addRow("循环间隔:", self.spin_interval)
        self.spin_timeout = self.spin(5, 3600, 120, "秒")
        form.addRow("单步超时:", self.spin_timeout)
        self.chk_cleanup = QCheckBox("结束后删除设备文件")
        self.chk_cleanup.setChecked(True)
        form.addRow(self.chk_cleanup)
        form_host.layout().addLayout(form)
        form_host.layout().addStretch()
        lv.addWidget(scroll, 1)
        row = QHBoxLayout()
        self.btn_start = QPushButton("开始读写")
        self.btn_start.setStyleSheet(gc.STYLE_START_BTN)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setStyleSheet(gc.STYLE_STOP_BTN)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        lv.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("就绪")
        lv.addWidget(self.progress)
        root.addWidget(left, 1)
        root.addWidget(self.build_log_panel(), 2)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Flash 测试文件", gc.app_dir())
        if path:
            self.edit_local.setText(path)

    def collect(self):
        return {"local_file": self.edit_local.text(), "remote_file": self.edit_remote.text(),
                "pull_file": self.edit_pull.text(), "cycles": self.spin_cycles.value(),
                "interval": self.spin_interval.value(), "timeout": self.spin_timeout.value(),
                "cleanup": self.chk_cleanup.isChecked()}

    def apply_config(self, cfg):
        if not cfg:
            return
        self.edit_local.setText(cfg.get("local_file", ""))
        self.edit_remote.setText(cfg.get("remote_file", "/data/local/tmp/flash_test.bin"))
        self.edit_pull.setText(cfg.get("pull_file", self.edit_pull.text()))
        self.spin_cycles.setValue(int(cfg.get("cycles", 1)))
        self.spin_interval.setValue(int(cfg.get("interval", 1)))
        self.spin_timeout.setValue(int(cfg.get("timeout", 120)))
        self.chk_cleanup.setChecked(bool(cfg.get("cleanup", True)))

    def refresh_project(self):
        project = gc.get_active_project()
        self.lbl_project.setText(project.get("name", "未配置"))

    def _start(self):
        project = gc.get_active_project()
        if not project.get("adb_enabled", True):
            self.append_log("当前项目未启用 ADB，无法执行 Flash 测试。", "#fdcb6e")
            return
        if not self.edit_local.text().strip() or not self.edit_remote.text().strip():
            self.append_log("请选择本地文件并填写设备文件路径。", "#fdcb6e")
            return
        try:
            _validate_pull_path(self.edit_local.text(), self.edit_pull.text())
        except (ValueError, OSError) as ex:
            self.append_log(str(ex), "#fdcb6e")
            return
        if not self.try_acquire():
            return
        cfg = self.collect()
        cfg["adb"] = gc.project_adb_args(project)[0]
        self.stop_event.clear()
        self.worker = FlashWorker(cfg, self.stop_event)
        self.worker.log_signal.connect(lambda text: self.append_log(text))
        self.worker.progress_signal.connect(self._on_progress)
        self.worker.finished_signal.connect(self._on_worker_result)
        self.worker.finished.connect(self._on_thread_finished)
        self._worker_result = None
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        cycles = self.spin_cycles.value()
        if cycles > 0:
            self.progress.setRange(0, cycles)
            self.progress.setValue(0)
            self.progress.setFormat(f"读写测试运行中... 0/{cycles}")
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat("读写测试运行中... 无限循环")
        self.worker.start()

    def _on_progress(self, run_no, cycles):
        if cycles <= 0:
            self.progress.setFormat(f"第 {run_no} 次（无限循环）")
            return
        self.progress.setRange(0, cycles)
        self.progress.setValue(run_no)
        self.progress.setFormat(f"第 {run_no}/{cycles} 次")

    def _stop(self):
        self.stop_event.set()
        if self.worker:
            self.worker.stop()
        self.btn_stop.setEnabled(False)
        self.append_log("正在停止 Flash 测试...", "#fdcb6e")

    def _on_worker_result(self, success, message):
        self._worker_result = (success, message)

    def _on_thread_finished(self):
        worker = self.worker
        success, message = self._worker_result or (False, "Flash 测试异常结束")
        self.worker = None
        self._worker_result = None
        if worker is not None:
            worker.deleteLater()
        self.release_run()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if success else 0)
        self.progress.setFormat(message)
        self.append_log(message, "#00b894" if success else "#e17055", bold=True)

    def prepare_stop(self):
        self._stop()
