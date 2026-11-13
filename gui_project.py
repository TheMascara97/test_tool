# -*- coding: utf-8 -*-
"""设备项目管理：集中维护项目级 ADB、串口、SSH、MSH 与开机检测。"""

from copy import deepcopy
import logging
import re

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

import gui_common as gc
import device_session


class ProjectPage(gc.BasePage):
    project_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__("project_manager", "项目管理")
        self.profiles = gc.get_project_profiles()
        self.projects = deepcopy(self.profiles.get("projects", []))
        self._loading = False
        self._locked = False
        self._current_index = -1
        self._project_buttons = []
        self._serial_rows = []
        self._build_ui()
        self._load_names(self.profiles.get("active", ""))
        self.set_locked(False)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.addWidget(QLabel("当前项目:"))
        self.combo_project = QComboBox()
        self.combo_project.setMinimumWidth(220)
        top.addWidget(self.combo_project)
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("项目名称")
        top.addWidget(self.edit_name, 1)
        root.addLayout(top)
        self.combo_project.currentIndexChanged.connect(self._on_project_changed)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.detail_layout = QVBoxLayout(content)
        self.detail_layout.setContentsMargins(0, 0, 4, 0)
        self.detail_layout.setSpacing(8)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        self._build_adb_group()
        self._build_log_group()
        self._build_serial_group()
        self._build_ssh_group()
        self._build_msh_group()
        self._build_boot_group()
        self.detail_layout.addStretch()

        actions = QHBoxLayout()
        for text, slot in (("新建项目", self._new_project),
                           ("复制项目", self._copy_project),
                           ("删除项目", self._delete_project)):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            self._project_buttons.append(btn)
            actions.addWidget(btn)
        actions.addStretch()
        self.btn_apply = QPushButton("保存并设为当前项目")
        self.btn_apply.setStyleSheet(gc.STYLE_START_BTN)
        self.btn_apply.clicked.connect(self._apply)
        actions.addWidget(self.btn_apply)
        root.addLayout(actions)

    def _build_adb_group(self):
        group = QGroupBox("ADB")
        form = QFormLayout(group)
        self.chk_adb = QCheckBox("项目启用 ADB")
        form.addRow(self.chk_adb)
        self.edit_adb_path = QLineEdit()
        self.edit_adb_path.setPlaceholderText("adb 或完整路径")
        form.addRow("ADB 路径:", self.edit_adb_path)
        self.edit_adb_serial = QLineEdit()
        self.edit_adb_serial.setPlaceholderText("留空时由 adb 自动选择唯一设备")
        form.addRow("设备序列号:", self.edit_adb_serial)
        self.chk_adb_auth = QCheckBox("需要用户名和密码")
        form.addRow(self.chk_adb_auth)
        self.edit_adb_username = QLineEdit()
        form.addRow("用户名:", self.edit_adb_username)
        self.edit_adb_password = QLineEdit()
        self.edit_adb_password.setEchoMode(QLineEdit.Password)
        form.addRow("密码:", self.edit_adb_password)
        self.spin_adb_timeout = self.spin(1, 600, 15, "秒")
        form.addRow("登录超时:", self.spin_adb_timeout)
        self.detail_layout.addWidget(group)

        self.bind_autosave([
            self.chk_adb, self.edit_adb_path, self.edit_adb_serial,
            self.chk_adb_auth, self.edit_adb_username, self.edit_adb_password,
            self.spin_adb_timeout,
        ])
        self.chk_adb.stateChanged.connect(self._update_dependency_states)
        self.chk_adb_auth.stateChanged.connect(self._update_dependency_states)

    def _build_log_group(self):
        group = QGroupBox("日志通道")
        form = QFormLayout(group)
        self.combo_log_channel = QComboBox()
        self.combo_log_channel.addItem("ADB", "adb")
        self.combo_log_channel.addItem("SSH", "ssh")
        self.combo_log_channel.addItem("串口", "serial")
        form.addRow("日志通道:", self.combo_log_channel)
        self.edit_log_command = QLineEdit()
        self.edit_log_command.setPlaceholderText("ADB/SSH 日志命令，例如 logcat -v time")
        form.addRow("日志命令:", self.edit_log_command)
        self.detail_layout.insertWidget(1, group)
        self.bind_autosave([self.combo_log_channel, self.edit_log_command])
        self.combo_log_channel.currentIndexChanged.connect(
            self._update_dependency_states)

    def _build_serial_group(self):
        self.serial_group = QGroupBox("串口")
        layout = QVBoxLayout(self.serial_group)

        header = QHBoxLayout()
        self.chk_serial = QCheckBox("项目启用串口")
        header.addWidget(self.chk_serial)
        header.addSpacing(12)
        header.addWidget(QLabel("串口数量:"))
        self.spin_serial_count = self.spin(0, 16, 0)
        self.spin_serial_count.setFixedWidth(90)
        header.addWidget(self.spin_serial_count)
        header.addStretch()
        self._port_buttons = []
        refresh_btn = QPushButton("刷新端口列表")
        refresh_btn.clicked.connect(self._refresh_ports)
        header.addWidget(refresh_btn)
        self._port_buttons.append(refresh_btn)
        layout.addLayout(header)

        note = QLabel("每个串口独立设置认证和开机检测；功能页按串口顺序读取配置。")
        note.setStyleSheet("color:#b2bec3;")
        layout.addWidget(note)

        self.serial_rows_host = QWidget()
        self.serial_rows_layout = QVBoxLayout(self.serial_rows_host)
        self.serial_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.serial_rows_layout.setSpacing(6)
        layout.addWidget(self.serial_rows_host)
        self.detail_layout.addWidget(self.serial_group)

        self.spin_serial_count.valueChanged.connect(self._on_serial_count_changed)
        self.bind_autosave([self.chk_serial, self.spin_serial_count])
        self.chk_serial.stateChanged.connect(self._update_dependency_states)

    def _build_ssh_group(self):
        group = QGroupBox("SSH")
        form = QFormLayout(group)
        self.chk_ssh = QCheckBox("项目支持 SSH")
        form.addRow(self.chk_ssh)
        self.edit_ssh_host = QLineEdit()
        self.edit_ssh_host.setPlaceholderText("设备 IP 地址或主机名")
        form.addRow("IP 地址:", self.edit_ssh_host)
        self.spin_ssh_port = self.spin(1, 65535, 22)
        form.addRow("端口:", self.spin_ssh_port)
        self.edit_ssh_username = QLineEdit()
        form.addRow("用户名:", self.edit_ssh_username)
        self.edit_ssh_password = QLineEdit()
        self.edit_ssh_password.setEchoMode(QLineEdit.Password)
        form.addRow("密码:", self.edit_ssh_password)
        self.detail_layout.addWidget(group)
        self.bind_autosave([
            self.chk_ssh, self.edit_ssh_host, self.spin_ssh_port,
            self.edit_ssh_username, self.edit_ssh_password,
        ])
        self.chk_ssh.stateChanged.connect(self._update_dependency_states)

    def _build_msh_group(self):
        group = QGroupBox("MSH 认证")
        form = QFormLayout(group)
        self.chk_msh = QCheckBox("项目启用 MSH 认证")
        form.addRow(self.chk_msh)

        pem_row = QHBoxLayout()
        self.edit_msh_pem = QLineEdit()
        self.edit_msh_pem.setPlaceholderText("PEM 私钥路径")
        pem_row.addWidget(self.edit_msh_pem, 1)
        btn_pem = QPushButton("选择")
        btn_pem.clicked.connect(self._choose_pem)
        pem_row.addWidget(btn_pem)
        form.addRow("PEM 私钥:", pem_row)

        self.edit_msh_seed = QLineEdit()
        form.addRow("种子正则:", self.edit_msh_seed)
        self.edit_msh_command = QLineEdit()
        self.edit_msh_command.setPlaceholderText("认证成功后执行的命令")
        form.addRow("认证后命令:", self.edit_msh_command)
        self.spin_msh_timeout = self.spin(1, 600, 15, "秒")
        form.addRow("认证超时:", self.spin_msh_timeout)
        self.edit_msh_success = QLineEdit()
        form.addRow("成功标志:", self.edit_msh_success)
        self.edit_msh_fail = QLineEdit()
        self.edit_msh_fail.setPlaceholderText("多个标志可用逗号分隔")
        form.addRow("失败标志:", self.edit_msh_fail)
        self.spin_msh_ready = self.spin(0, 600, 5, "秒")
        form.addRow("就绪等待:", self.spin_msh_ready)
        self.detail_layout.addWidget(group)
        self.bind_autosave([
            self.chk_msh, self.edit_msh_pem, self.edit_msh_seed,
            self.edit_msh_command, self.spin_msh_timeout,
            self.edit_msh_success, self.edit_msh_fail, self.spin_msh_ready,
        ])
        self.chk_msh.stateChanged.connect(self._update_dependency_states)

    def _build_boot_group(self):
        group = QGroupBox("项目级开机检测")
        form = QFormLayout(group)
        self.chk_boot_detect = QCheckBox("等待设备开机")
        form.addRow(self.chk_boot_detect)
        self.edit_boot_keyword = QLineEdit()
        self.edit_boot_keyword.setPlaceholderText("例如 login: 或系统启动完成标志")
        form.addRow("开机关键词:", self.edit_boot_keyword)
        self.spin_boot_timeout = self.spin(1, 7200, 180, "秒")
        form.addRow("开机超时:", self.spin_boot_timeout)
        self.detail_layout.addWidget(group)
        self.bind_autosave([
            self.chk_boot_detect, self.edit_boot_keyword,
            self.spin_boot_timeout,
        ])
        self.chk_boot_detect.stateChanged.connect(self._update_dependency_states)

    def _new_serial_row(self, port, index):
        box = QGroupBox(f"串口 {index + 1}")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(3, 1)

        edit_name = QLineEdit(str(port.get("name", "") or f"串口{index + 1}"))
        grid.addWidget(QLabel("名称:"), 0, 0)
        grid.addWidget(edit_name, 0, 1)

        combo_port = gc.make_port_combo(str(port.get("port", "") or ""))
        grid.addWidget(QLabel("端口:"), 0, 2)
        grid.addWidget(combo_port, 0, 3)

        spin_baud = self.spin(1, 921600, int(port.get("baudrate", 115200)))
        grid.addWidget(QLabel("波特率:"), 1, 0)
        grid.addWidget(spin_baud, 1, 1)

        chk_auth = QCheckBox("需要用户名和密码")
        chk_auth.setChecked(gc.parse_bool(port.get("auth_enabled", False)))
        grid.addWidget(chk_auth, 1, 2, 1, 2)

        edit_user = QLineEdit(str(port.get("username", "") or ""))
        edit_user.setPlaceholderText("用户名")
        grid.addWidget(QLabel("用户名:"), 2, 0)
        grid.addWidget(edit_user, 2, 1)
        edit_password = QLineEdit(str(port.get("password", "") or ""))
        edit_password.setEchoMode(QLineEdit.Password)
        edit_password.setPlaceholderText("密码")
        grid.addWidget(QLabel("密码:"), 2, 2)
        grid.addWidget(edit_password, 2, 3)

        spin_auth_timeout = self.spin(
            1, 600, int(port.get("auth_timeout", 15) or 15), "秒")
        grid.addWidget(QLabel("认证超时:"), 3, 0)
        grid.addWidget(spin_auth_timeout, 3, 1)

        chk_boot = QCheckBox("在该串口检测开机")
        chk_boot.setChecked(gc.parse_bool(port.get("boot_detect", False)))
        grid.addWidget(chk_boot, 3, 2, 1, 2)
        edit_keyword = QLineEdit(str(port.get("boot_keyword", "") or ""))
        edit_keyword.setPlaceholderText("开机关键词")
        grid.addWidget(QLabel("开机关键词:"), 4, 0)
        grid.addWidget(edit_keyword, 4, 1, 1, 3)

        spin_boot_count = self.spin(
            1, 100, int(port.get("boot_success_count", 1) or 1), "次")
        grid.addWidget(QLabel("关键词匹配次数:"), 5, 0)
        grid.addWidget(spin_boot_count, 5, 1)

        chk_log_to_main = QCheckBox("匹配日志输出到主程序")
        chk_log_to_main.setChecked(gc.parse_bool(
            port.get("log_to_main", True), True))
        grid.addWidget(chk_log_to_main, 5, 2)

        btn_remove = QPushButton("删除此串口")
        grid.addWidget(btn_remove, 5, 3)

        edit_log_file = QLineEdit(str(
            port.get("log_file", f"serial_output_port{index + 1}.log") or ""))
        edit_log_file.setPlaceholderText("串口日志文件")
        grid.addWidget(QLabel("日志文件:"), 6, 0)
        grid.addWidget(edit_log_file, 6, 1, 1, 3)

        row = {
            "box": box, "name": edit_name, "port": combo_port,
            "baudrate": spin_baud, "auth": chk_auth, "username": edit_user,
            "password": edit_password, "auth_timeout": spin_auth_timeout,
            "boot": chk_boot,
            "boot_keyword": edit_keyword, "boot_count": spin_boot_count,
            "log_to_main": chk_log_to_main, "log_file": edit_log_file,
            "remove": btn_remove,
        }
        btn_remove.clicked.connect(
            lambda _checked=False, item=row: self._remove_serial_row(item))
        chk_auth.stateChanged.connect(
            lambda _state=0, item=row: self._update_serial_auth_state(item))
        chk_boot.stateChanged.connect(self._update_dependency_states)
        self.bind_autosave([
            edit_name, combo_port, spin_baud, chk_auth, edit_user,
            edit_password, spin_auth_timeout, chk_boot, edit_keyword,
            spin_boot_count,
            chk_log_to_main, edit_log_file,
        ])
        self._serial_rows.append(row)
        self.serial_rows_layout.addWidget(box)
        self._update_serial_auth_state(row)
        return row

    def _add_serial_row(self, port=None, index=None):
        index = len(self._serial_rows) if index is None else index
        port = port or {"name": f"串口{index + 1}", "port": "",
                        "baudrate": 115200, "auth_enabled": False,
                        "boot_detect": False, "boot_success_count": 1,
                        "log_to_main": True,
                        "log_file": f"serial_output_port{index + 1}.log"}
        return self._new_serial_row(port, index)

    def _remove_serial_row(self, row):
        if row not in self._serial_rows:
            return
        self._serial_rows.remove(row)
        row["box"].setParent(None)
        row["box"].deleteLater()
        self.spin_serial_count.blockSignals(True)
        self.spin_serial_count.setValue(len(self._serial_rows))
        self.spin_serial_count.blockSignals(False)
        self._renumber_serial_rows()
        self._on_config_changed()

    def _renumber_serial_rows(self):
        for i, row in enumerate(self._serial_rows):
            row["box"].setTitle(f"串口 {i + 1}")

    def _on_serial_count_changed(self, count):
        if self._loading:
            return
        while len(self._serial_rows) < count:
            self._add_serial_row()
        while len(self._serial_rows) > count:
            row = self._serial_rows.pop()
            row["box"].setParent(None)
            row["box"].deleteLater()
        self._renumber_serial_rows()

    @staticmethod
    def _update_serial_auth_state(row):
        enabled = row["auth"].isChecked()
        row["username"].setEnabled(enabled)
        row["password"].setEnabled(enabled)
        row["auth_timeout"].setEnabled(enabled)

    def _update_dependency_states(self, *_args):
        """Apply project-level enable states and the temporary run lock."""
        if not hasattr(self, "chk_adb"):
            return
        unlocked = not self._locked
        log_channel = self.combo_log_channel.currentData()
        self.combo_log_channel.setEnabled(unlocked)
        self.edit_log_command.setEnabled(
            unlocked and log_channel in ("adb", "ssh"))
        adb_enabled = unlocked and self.chk_adb.isChecked()
        self.edit_adb_path.setEnabled(adb_enabled)
        self.edit_adb_serial.setEnabled(adb_enabled)
        self.chk_adb_auth.setEnabled(adb_enabled)
        self.spin_adb_timeout.setEnabled(adb_enabled)
        adb_auth = adb_enabled and self.chk_adb_auth.isChecked()
        self.edit_adb_username.setEnabled(adb_auth)
        self.edit_adb_password.setEnabled(adb_auth)

        serial_enabled = unlocked and self.chk_serial.isChecked()
        self.spin_serial_count.setEnabled(serial_enabled)
        for button in self._port_buttons:
            button.setEnabled(serial_enabled)
        for row in self._serial_rows:
            row["box"].setEnabled(serial_enabled)
            if serial_enabled:
                self._update_serial_auth_state(row)
                row["boot_keyword"].setEnabled(row["boot"].isChecked())
                row["boot_count"].setEnabled(row["boot"].isChecked())
                row["log_to_main"].setEnabled(row["boot"].isChecked())

        ssh_enabled = unlocked and self.chk_ssh.isChecked()
        for widget in (self.edit_ssh_host, self.spin_ssh_port,
                       self.edit_ssh_username, self.edit_ssh_password):
            widget.setEnabled(ssh_enabled)

        msh_enabled = unlocked and self.chk_msh.isChecked()
        for widget in (self.edit_msh_pem, self.edit_msh_seed,
                       self.edit_msh_command, self.spin_msh_timeout,
                       self.edit_msh_success, self.edit_msh_fail,
                       self.spin_msh_ready):
            widget.setEnabled(msh_enabled)

        boot_enabled = unlocked and self.chk_boot_detect.isChecked()
        self.edit_boot_keyword.setEnabled(boot_enabled)
        self.spin_boot_timeout.setEnabled(boot_enabled)

    def _refresh_ports(self):
        for row in self._serial_rows:
            gc.refresh_port_combo(row["port"], row["port"].currentText())

    def _choose_pem(self):
        start = self.edit_msh_pem.text().strip()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 PEM 私钥", start, "PEM 私钥 (*.pem);;所有文件 (*)")
        if path:
            self.edit_msh_pem.setText(path)

    def _load_names(self, active):
        self._loading = True
        try:
            self.combo_project.blockSignals(True)
            self.combo_project.clear()
            self.combo_project.addItems([p.get("name", "") for p in self.projects])
            idx = self.combo_project.findText(active)
            self.combo_project.setCurrentIndex(max(0, idx))
        finally:
            self.combo_project.blockSignals(False)
            self._loading = False
        self._current_index = self.combo_project.currentIndex()
        self._update_dependency_states()
        self._load_project(self._current_index)

    def _clear_serial_rows(self):
        while self._serial_rows:
            row = self._serial_rows.pop()
            row["box"].setParent(None)
            row["box"].deleteLater()

    def _load_project(self, index):
        if not (0 <= index < len(self.projects)):
            return
        project = gc.normalize_project(self.projects[index])
        self._loading = True
        try:
            self.edit_name.setText(project.get("name", ""))
            channel_index = self.combo_log_channel.findData(
                project.get("log_channel", "serial"))
            self.combo_log_channel.setCurrentIndex(
                channel_index if channel_index >= 0 else 2)
            self.edit_log_command.setText(project.get("log_command", ""))
            self.chk_adb.setChecked(project.get("adb_enabled", True))
            self.edit_adb_path.setText(project.get("adb_path", "adb"))
            self.edit_adb_serial.setText(project.get("adb_serial", ""))
            self.chk_adb_auth.setChecked(project.get("adb_auth_enabled", False))
            self.edit_adb_username.setText(project.get("adb_username", ""))
            self.edit_adb_password.setText(project.get("adb_password", ""))
            self.spin_adb_timeout.setValue(project.get("adb_login_timeout", 15))

            self.chk_serial.setChecked(project.get("serial_enabled", False))
            self._clear_serial_rows()
            for port in project.get("serial_ports", []):
                self._add_serial_row(port)
            self.spin_serial_count.setValue(len(self._serial_rows))

            self.chk_ssh.setChecked(project.get("ssh_enabled", False))
            self.edit_ssh_host.setText(project.get("ssh_host", ""))
            self.spin_ssh_port.setValue(project.get("ssh_port", 22))
            self.edit_ssh_username.setText(project.get("ssh_username", ""))
            self.edit_ssh_password.setText(project.get("ssh_password", ""))

            self.chk_msh.setChecked(project.get("msh_enabled", False))
            self.edit_msh_pem.setText(project.get("msh_pem", ""))
            self.edit_msh_seed.setText(project.get("msh_seed_pattern", ""))
            self.edit_msh_command.setText(project.get("msh_post_auth_cmd", ""))
            self.spin_msh_timeout.setValue(project.get("msh_timeout", 15))
            self.edit_msh_success.setText(project.get("msh_success_flag", ""))
            self.edit_msh_fail.setText(project.get("msh_fail_flags", ""))
            self.spin_msh_ready.setValue(project.get("msh_ready_wait", 5))

            self.chk_boot_detect.setChecked(
                project.get("boot_detect_enabled", False))
            self.edit_boot_keyword.setText(project.get("boot_keyword", ""))
            self.spin_boot_timeout.setValue(project.get("boot_timeout", 180))
        finally:
            self._loading = False
        self._current_index = index
        self._update_dependency_states()

    def _on_project_changed(self, index):
        if self._loading or not (0 <= index < len(self.projects)):
            return
        old_index = self._current_index
        if index == old_index:
            return
        projects = deepcopy(self.projects)
        if 0 <= old_index < len(self.projects):
            previous = self._form_project(old_index)
            if not previous.get("name", "").strip():
                self._show_name_conflict(old_index)
                return
            if self._name_conflict(
                    previous["name"], old_index, projects):
                self._show_name_conflict(old_index)
                return
            projects[old_index] = previous
        active = projects[index]["name"]
        if not self._persist_projects(active, emit=True, projects=projects):
            self._show_name_conflict(old_index)
            QMessageBox.warning(
                self, "项目管理",
                "项目切换失败，当前配置保存失败。")
            return
        self._load_project(index)

    def _collect_serial_ports(self):
        ports = []
        for i, row in enumerate(self._serial_rows):
            ports.append({
                "name": row["name"].text().strip() or f"串口{i + 1}",
                "port": row["port"].currentText().strip(),
                "baudrate": row["baudrate"].value(),
                "auth_enabled": row["auth"].isChecked(),
                "username": row["username"].text().strip(),
                "password": row["password"].text(),
                "auth_timeout": row["auth_timeout"].value(),
                "boot_detect": row["boot"].isChecked(),
                "boot_keyword": row["boot_keyword"].text().strip(),
                "boot_success_count": row["boot_count"].value(),
                "log_to_main": row["log_to_main"].isChecked(),
                "log_file": row["log_file"].text().strip(),
            })
        return ports

    def _form_project(self, index=None):
        index = self._current_index if index is None else index
        base = (deepcopy(self.projects[index])
                if 0 <= index < len(self.projects) else {})
        ports = self._collect_serial_ports()
        base.update({
            "name": self.edit_name.text().strip(),
            "log_channel": self.combo_log_channel.currentData() or "serial",
            "log_command": self.edit_log_command.text().strip(),
            "adb_enabled": self.chk_adb.isChecked(),
            "adb_path": self.edit_adb_path.text().strip() or "adb",
            "adb_serial": self.edit_adb_serial.text().strip(),
            "adb_auth_enabled": self.chk_adb_auth.isChecked(),
            "adb_username": self.edit_adb_username.text().strip(),
            "adb_password": self.edit_adb_password.text(),
            "adb_login_timeout": self.spin_adb_timeout.value(),
            "serial_enabled": self.chk_serial.isChecked(),
            "serial_port_count": len(ports),
            "serial_ports": ports,
            "ssh_enabled": self.chk_ssh.isChecked(),
            "ssh_host": self.edit_ssh_host.text().strip(),
            "ssh_port": self.spin_ssh_port.value(),
            "ssh_username": self.edit_ssh_username.text().strip(),
            "ssh_password": self.edit_ssh_password.text(),
            "msh_enabled": self.chk_msh.isChecked(),
            "msh_pem": self.edit_msh_pem.text().strip(),
            "msh_seed_pattern": self.edit_msh_seed.text().strip(),
            "msh_post_auth_cmd": self.edit_msh_command.text().strip(),
            "msh_timeout": self.spin_msh_timeout.value(),
            "msh_success_flag": self.edit_msh_success.text().strip(),
            "msh_fail_flags": self.edit_msh_fail.text().strip(),
            "msh_ready_wait": self.spin_msh_ready.value(),
            "boot_detect_enabled": self.chk_boot_detect.isChecked(),
            "boot_keyword": self.edit_boot_keyword.text().strip(),
            "boot_timeout": self.spin_boot_timeout.value(),
        })
        # Keep an empty name empty so the caller can reject it before saving.
        return gc.normalize_project(base, "")

    @staticmethod
    def _name_conflict(name, index, projects):
        folded = str(name or "").strip().casefold()
        return any(
            other.get("name", "").strip().casefold() == folded
            for i, other in enumerate(projects) if i != index
        )

    def _show_name_conflict(self, index=None):
        if index is not None:
            self.combo_project.blockSignals(True)
            try:
                self.combo_project.setCurrentIndex(index)
            finally:
                self.combo_project.blockSignals(False)
        QMessageBox.warning(
            self, "项目管理", "项目名称不能为空，也不能重复（不区分大小写）。")

    def _store_current(self, update_ui=True):
        index = self._current_index
        if not (0 <= index < len(self.projects)):
            return ""
        project = self._form_project(index)
        name = project.get("name", "").strip()
        if not name:
            logging.warning("项目名称不能为空，暂未保存")
            return ""
        if self._name_conflict(name, index, self.projects):
            return ""
        projects = deepcopy(self.projects)
        projects[index] = project
        if not gc.save_project_profiles({"active": name, "projects": projects}):
            logging.warning("项目配置保存失败: %s", name)
            return ""
        self.projects = projects
        self.profiles = {"active": name, "projects": deepcopy(projects)}
        if update_ui:
            self._suppress_autosave = True
            try:
                self.combo_project.setItemText(index, name)
                self.edit_name.setText(name)
            finally:
                self._suppress_autosave = False
        return name

    def _persist_projects(self, active, emit=False, projects=None):
        """Persist a candidate profile set before updating in-memory state."""
        source = self.projects if projects is None else projects
        candidate = [
            gc.normalize_project(project, "")
            for project in deepcopy(source)
            if isinstance(project, dict)
        ]
        names = [project.get("name", "") for project in candidate]
        active = str(active or "").strip()
        if not candidate or not all(names):
            logging.warning("项目配置保存失败: 项目名称为空")
            return False
        folded = [name.casefold() for name in names]
        if len(folded) != len(set(folded)):
            logging.warning("项目配置保存失败: 项目名称重复")
            return False
        for project in candidate:
            count = project.get("serial_port_count", 0)
            if count < 0 or count > 16:
                logging.warning("项目配置保存失败: 串口数量超出范围")
                return False
        selected = active
        for name in names:
            if name.casefold() == active.casefold():
                selected = name
                break
        else:
            selected = names[0]
        if not gc.save_project_profiles(
                {"active": selected, "projects": candidate}):
            logging.warning("项目配置保存失败: %s", selected)
            return False
        self.projects = candidate
        self.profiles = {
            "active": selected,
            "projects": deepcopy(candidate),
        }
        if emit:
            self.project_changed.emit(selected)
            self.title_changed.emit(selected)
        return True

    def _do_autosave(self):
        if self._loading or self._suppress_autosave:
            return True
        index = self._current_index
        if not (0 <= index < len(self.projects)):
            return False
        project = self._form_project(index)
        name = project.get("name", "").strip()
        if not name:
            logging.warning("项目名称不能为空，暂未自动保存")
            return False
        if self._name_conflict(name, index, self.projects):
            logging.warning("项目名称重复，暂未自动保存: %s", name)
            return False
        projects = deepcopy(self.projects)
        projects[index] = project
        return self._persist_projects(name, emit=True, projects=projects)

    def _new_project(self):
        if not self._store_current():
            self._show_name_conflict(self._current_index)
            return
        base, number = "新项目", 1
        names = {p["name"].casefold() for p in self.projects}
        while f"{base}{number}".casefold() in names:
            number += 1
        name = f"{base}{number}"
        projects = deepcopy(self.projects)
        projects.append(gc.normalize_project({
            "name": name,
            "adb_enabled": True,
            "adb_path": "adb",
            "serial_enabled": True,
            "serial_port_count": 1,
            "serial_ports": [{
                "name": "调试串口", "port": "", "baudrate": 115200,
                "auth_enabled": False, "boot_detect": True,
                "log_to_main": True,
                "log_file": "serial_output_port1.log",
            }],
        }))
        if not self._persist_projects(name, emit=True, projects=projects):
            QMessageBox.warning(self, "项目管理", "新项目保存失败。")
            return
        self._load_names(name)

    def _copy_project(self):
        name = self._store_current()
        if not name:
            self._show_name_conflict(self._current_index)
            return
        project = deepcopy(self.projects[self._current_index])
        base, number = f"{name}-副本", 2
        used = {p["name"].casefold() for p in self.projects}
        copy_name = base
        while copy_name.casefold() in used:
            copy_name = f"{base}{number}"
            number += 1
        project["name"] = copy_name
        projects = deepcopy(self.projects)
        projects.append(project)
        if not self._persist_projects(copy_name, emit=True, projects=projects):
            QMessageBox.warning(self, "项目管理", "复制项目保存失败。")
            return
        self._load_names(copy_name)

    def _delete_project(self):
        if len(self.projects) <= 1:
            QMessageBox.information(self, "项目管理", "至少保留一个项目。")
            return
        index = self.combo_project.currentIndex()
        if QMessageBox.question(self, "删除项目", "确定删除当前项目？") != QMessageBox.Yes:
            return
        projects = deepcopy(self.projects)
        deleted_name = projects.pop(index)["name"]
        next_index = min(index, len(projects) - 1)
        active = projects[next_index]["name"]
        if not self._persist_projects(active, emit=True, projects=projects):
            QMessageBox.warning(self, "项目管理", "删除项目保存失败。")
            return
        self._load_names(active)
        self.append_log(f"已删除项目: {deleted_name}", "#fdcb6e")

    def _apply(self):
        index = self._current_index
        if not (0 <= index < len(self.projects)):
            return
        project = self._form_project(index)
        errors = self._validate_project(project)
        if self._name_conflict(project["name"], index, self.projects):
            errors.append("项目名称不能重复（不区分大小写）")
        if errors:
            QMessageBox.warning(
                self, "项目配置不完整", "请先修正以下配置：\n\n" + "\n".join(
                    f"- {item}" for item in errors))
            return
        name = project["name"]
        projects = deepcopy(self.projects)
        projects[index] = project
        if self._persist_projects(name, emit=True, projects=projects):
            self._suppress_autosave = True
            try:
                self.combo_project.setItemText(index, name)
                self.edit_name.setText(name)
                self.combo_project.setCurrentIndex(index)
            finally:
                self._suppress_autosave = False
            self._current_index = index
            self.append_log(f"已应用项目配置: {name}", "#00b894")
        else:
            QMessageBox.warning(
                self, "项目管理",
                "配置保存失败，请检查项目名称是否重复或程序目录写权限。")

    def _validate_project(self, project):
        errors = []
        if not project.get("name", "").strip():
            errors.append("项目名称不能为空")
        try:
            device_session.manager.validate(project)
        except device_session.DeviceSessionError as exc:
            errors.append(str(exc))
        if project.get("log_channel") in ("adb", "ssh") and not project.get(
                "log_command", "").strip():
            errors.append("ADB/SSH 日志通道必须填写日志命令")
        if project.get("adb_enabled", False):
            if not project.get("adb_path", "").strip():
                errors.append("启用 ADB 时必须填写 ADB 路径")
            if project.get("adb_auth_enabled", False):
                if not project.get("adb_username", "").strip():
                    errors.append("ADB 认证用户名不能为空")
                if not project.get("adb_password", ""):
                    errors.append("ADB 认证密码不能为空")

        if project.get("serial_enabled", False):
            ports = project.get("serial_ports", [])
            if not ports:
                errors.append("启用串口时至少需要配置 1 路串口")
            for index, port in enumerate(ports):
                label = port.get("name", "").strip() or f"串口 {index + 1}"
                if not port.get("port", "").strip():
                    errors.append(f"{label} 未选择端口")
                if port.get("auth_enabled", False):
                    if not port.get("username", "").strip():
                        errors.append(f"{label} 认证用户名不能为空")
                    if not port.get("password", ""):
                        errors.append(f"{label} 认证密码不能为空")
                if port.get("boot_detect", False) and not port.get(
                        "boot_keyword", "").strip():
                    errors.append(f"{label} 已启用开机检测，但未填写关键词")

        if project.get("ssh_enabled", False):
            if not project.get("ssh_host", "").strip():
                errors.append("启用 SSH 时必须填写 IP 地址或主机名")
            if not project.get("ssh_username", "").strip():
                errors.append("SSH 用户名不能为空")
            if not project.get("ssh_password", ""):
                errors.append("SSH 密码不能为空")

        if project.get("msh_enabled", False):
            if not project.get("msh_pem", "").strip():
                errors.append("启用 MSH 时必须选择 PEM 私钥")
            seed_pattern = project.get("msh_seed_pattern", "").strip()
            if not seed_pattern:
                errors.append("MSH 种子正则不能为空")
            else:
                try:
                    re.compile(seed_pattern)
                except re.error as ex:
                    errors.append(f"MSH 种子正则无效: {ex}")
        if project.get("boot_detect_enabled", False) and not project.get(
                "boot_keyword", "").strip():
            errors.append("启用项目级开机检测时必须填写开机关键词")
        if project.get("boot_detect_enabled", False):
            if not project.get("serial_enabled", False):
                errors.append("启用项目级开机检测时必须先启用串口")
            ports = project.get("serial_ports", [])
            if not any(str(port.get("port", "")).strip() for port in ports):
                errors.append("启用项目级开机检测时至少需要一路有效串口端口")
            if not any(port.get("boot_detect", False) for port in ports):
                errors.append("启用项目级开机检测时至少需要在一路串口启用开机检测")
        return errors

    def set_locked(self, locked):
        """任务运行期间冻结连接配置，避免测试中途切换项目。"""
        self._locked = bool(locked)
        widgets = [
            self.combo_project, self.edit_name, self.chk_adb, self.edit_adb_path,
            self.edit_adb_serial, self.chk_adb_auth, self.edit_adb_username,
            self.edit_adb_password, self.spin_adb_timeout, self.chk_serial,
            self.spin_serial_count, self.chk_ssh, self.edit_ssh_host,
            self.spin_ssh_port, self.edit_ssh_username, self.edit_ssh_password,
            self.chk_msh, self.edit_msh_pem, self.edit_msh_seed,
            self.edit_msh_command, self.spin_msh_timeout,
            self.edit_msh_success, self.edit_msh_fail, self.spin_msh_ready,
            self.chk_boot_detect, self.edit_boot_keyword,
            self.spin_boot_timeout, self.btn_apply,
            self.combo_log_channel, self.edit_log_command,
        ]
        for row in self._serial_rows:
            for key in ("box", "name", "port", "baudrate", "auth", "username",
                        "password", "auth_timeout", "boot", "boot_keyword",
                        "boot_count",
                        "log_to_main", "log_file", "remove"):
                widgets.append(row[key])
        for widget in widgets:
            widget.setEnabled(not locked)
        for widget in self._project_buttons + self._port_buttons:
            widget.setEnabled(not locked)
        self._update_dependency_states()

    def apply_config(self, cfg):
        pass

    def collect(self):
        index = self._current_index
        projects = deepcopy(self.projects)
        project = None
        if 0 <= index < len(projects):
            project = self._form_project(index)
            name = project.get("name", "").strip()
            if name and not self._name_conflict(name, index, projects):
                projects[index] = project
        active = (projects[index]["name"]
                  if 0 <= index < len(projects)
                  else self.profiles.get("active", ""))
        if project and project.get("name", "").strip():
            active = project["name"].strip()
        return {"active": active, "projects": projects}
