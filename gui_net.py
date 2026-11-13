# -*- coding: utf-8 -*-
"""
网络测试页面 — 把「PC 端 Ping」与「ADB Ping (T1L)」两个面板合并到一个页面。

结构：
    NetPage(BasePage)
      ├─ QTabWidget
      │    ├─ "PC 端 Ping"     → PingPanel
      │    └─ "ADB Ping (T1L)" → T1LPanel
      └─ 共用日志面板（两个面板的 root logger 输出都进这里）

两个面板同属一个 NetPage，而 acquire_busy() 对同一页面对象可重复获取，
所以页面里额外维护 active_panel：同一时刻只允许一个面板在跑，
运行期间禁用另一个 Tab。
"""

import logging
import os

from PyQt5.QtWidgets import (
    QTabWidget, QVBoxLayout, QSpinBox, QLineEdit, QCheckBox, QComboBox
)

from gui_common import BasePage, load_gui_config, save_gui_sections
from gui_ping import PingPanel
from gui_t1l import T1LPanel


class NetPage(BasePage):
    def __init__(self, parent=None):
        super().__init__("net", "网络测试", parent)

        # 两个面板的配置各自沿用原来的 "ping" / "t1l" 分节，
        # 这样用户已有的 gui_config.json 不需要迁移
        self.active_panel = None
        self._last_panel = None      # 停止后仍能「打开日志目录」
        self.active_name = ""

        self._build_ui()
        self.restore_config()

        # 面板里的控件都挂在 NetPage 之下，统一接线自动保存
        self.bind_autosave(
            self.findChildren(QSpinBox)
            + self.findChildren(QLineEdit)
            + self.findChildren(QCheckBox)
            + self.findChildren(QComboBox)
        )

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self.tabs = QTabWidget()

        self.ping_panel = PingPanel(self)
        self.t1l_panel = T1LPanel(self)
        self.tabs.addTab(self.ping_panel, "PC 端 Ping")
        self.tabs.addTab(self.t1l_panel, "ADB Ping (T1L)")
        root.addWidget(self.tabs, 3)

        root.addWidget(self.build_log_panel(
            with_open_dir=True,
            log_dir_getter=self._current_log_dir), 2)

    def _current_log_dir(self):
        panel = self.active_panel or self._last_panel
        if panel is not None and panel._log_file:
            return os.path.dirname(panel._log_file)
        return None

    # ---------- 配置 ----------
    def restore_config(self):
        cfg = load_gui_config()
        self._suppress_autosave = True
        try:
            if isinstance(cfg.get("ping"), dict) and cfg["ping"]:
                self.ping_panel.apply_config(cfg["ping"])
            if isinstance(cfg.get("t1l"), dict) and cfg["t1l"]:
                self.t1l_panel.apply_config(cfg["t1l"])
        finally:
            self._suppress_autosave = False

    def _do_autosave(self):
        try:
            return save_gui_sections({
                "ping": self.ping_panel.collect(),
                "t1l": self.t1l_panel.collect(),
            })
        except Exception:
            logging.exception("保存网络测试配置失败")
            return False

    # ---------- 运行状态 ----------
    def begin_run(self, panel):
        """面板确认要启动时调用：登记活动面板并锁住另一个 Tab"""
        self.active_panel = panel
        self._last_panel = panel
        self.active_name = self.tabs.tabText(self.tabs.indexOf(panel))
        other = self.t1l_panel if panel is self.ping_panel else self.ping_panel
        self.tabs.setTabEnabled(self.tabs.indexOf(other), False)

    def end_run(self):
        self.active_panel = None
        self.active_name = ""
        for i in range(self.tabs.count()):
            self.tabs.setTabEnabled(i, True)

    @property
    def worker(self):
        return self.active_panel.worker if self.active_panel else None

    def prepare_stop(self):
        if self.active_panel is not None:
            try:
                self.active_panel.prepare_stop()
            except Exception:
                pass

    def refresh_project(self):
        refresh = getattr(self.ping_panel, "refresh_project", None)
        if callable(refresh):
            refresh()
        self.t1l_panel.refresh_project()

    def on_log_line(self, text):
        # 日志统一由 NetPage 打印，统计解析要转给当前活动的面板
        if self.active_panel is not None:
            try:
                self.active_panel.on_log_line(text)
            except Exception:
                pass
