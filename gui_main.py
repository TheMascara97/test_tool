# -*- coding: utf-8 -*-
"""
设备测试工具箱 — 统一 GUI 入口

左侧功能列表：
  1. 项目管理     (gui_project.py)
  2. 开关机测试   (gui_reboot.py + reboot_test.py)
  3. FOTA 升降级  (gui_fota.py + NASH_FOTA.py)
  4. 网络测试     (gui_net.py: PC 端 Ping + ADB Ping/T1L)
  5. 循环读取时间 (gui_rtc.py)
  6. Flash 读写   (gui_flash.py)

打包：pyinstaller build_exe.spec  （或运行 build_exe.bat）
"""

import logging
import os
import sys
import traceback

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QListWidget, QListWidgetItem, QStackedWidget, QSplitter, QMessageBox,
    QStatusBar, QLabel
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIcon

import gui_common as gc


class ToolBoxWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("设备测试工具箱")
        self.setMinimumSize(1180, 720)
        self.resize(1440, 880)

        icon = gc.resource_path('2222.ico')
        if os.path.exists(icon):
            self.setWindowIcon(QIcon(icon))

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 4)
        layout.setSpacing(6)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)

        # ── 左侧功能列表 ──
        self.nav = QListWidget()
        self.nav.setFixedWidth(190)
        splitter.addWidget(self.nav)

        # ── 右侧页面堆叠 ──
        self.stack = QStackedWidget()
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self.pages = []
        self._build_pages()

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._status_label = QLabel("就绪")
        self._status_right = QLabel("")
        self.status_bar.addWidget(self._status_label)
        self.status_bar.addPermanentWidget(self._status_right)

        self.nav.currentRowChanged.connect(self._on_nav_changed)
        self.nav.setCurrentRow(0)

    def _build_pages(self):
        # 延迟导入各页面，减少启动时间
        from gui_project import ProjectPage
        from gui_reboot import RebootPage
        from gui_fota import FotaPage
        from gui_net import NetPage
        from gui_rtc import RtcPage
        from gui_flash import FlashPage

        page_defs = [
            ("⚙  项目管理", ProjectPage),
            ("🔄  开关机测试", RebootPage),
            ("📦  FOTA 升降级", FotaPage),
            ("🌐  网络测试", NetPage),
            ("🕒  循环读取时间", RtcPage),
            ("💾  Flash 读写", FlashPage),
        ]
        for title, cls in page_defs:
            item = QListWidgetItem(title)
            self.nav.addItem(item)
            page = cls()
            page.title_changed.connect(self._on_page_title)
            page.run_state_changed.connect(self._on_run_state)
            self.stack.addWidget(page)
            self.pages.append(page)
            if hasattr(page, "project_changed"):
                page.project_changed.connect(self._on_project_changed)

    def _on_project_changed(self, name):
        refresh_errors = []
        for page in self.pages:
            refresh = getattr(page, "refresh_project", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    refresh_errors.append(page.tool_name)
                    logging.warning("刷新项目配置失败:\n%s", traceback.format_exc())
        if refresh_errors:
            self._status_label.setText(
                f"当前项目: {name}；部分页面刷新失败: "
                + "、".join(refresh_errors))
        else:
            self._status_label.setText(f"当前项目: {name}")

    def _on_nav_changed(self, row):
        if row < 0 or row >= len(self.pages):
            return
        old_row = self.stack.currentIndex()
        if 0 <= old_row < len(self.pages) and old_row != row:
            old_page = self.pages[old_row]
            flush = getattr(old_page, "flush_pending_save", None)
            if callable(flush):
                try:
                    flush()
                except Exception:
                    logging.warning("切换页面前保存配置失败:\n%s",
                                    traceback.format_exc())
        page = self.pages[row]
        self.stack.setCurrentIndex(row)
        self.setWindowTitle(f"设备测试工具箱 — {page.tool_name}")

        # 运行中提示：任务运行时会占用串口/设备，建议停止后再切换
        busy = [p.tool_name for p in self.pages if p.is_running]
        if busy:
            self._status_label.setText("当前运行中: " + "、".join(busy))
        else:
            self._status_label.setText(f"当前: {page.tool_name}")
        # 页面首次显示时再加载其业务模块（NASH_FOTA / reboot_test）
        if hasattr(page, "mod_defaults"):
            try:
                page.mod_defaults()
            except Exception:
                logging.warning("加载业务模块失败:\n%s", traceback.format_exc())
        elif hasattr(page, "ecu_placeholder"):
            try:
                page.ecu_placeholder()
            except Exception:
                logging.warning("加载业务模块失败:\n%s", traceback.format_exc())
        refresh = getattr(page, "refresh_project", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                self._status_label.setText(
                    f"当前: {page.tool_name}；项目配置刷新失败")
                logging.warning("切换页面后刷新项目失败:\n%s",
                                traceback.format_exc())

    def _on_page_title(self, detail):
        page = self.sender()
        name = getattr(page, "tool_name", "")
        self.setWindowTitle(f"设备测试工具箱 — {name} [{detail}]")

    def _on_run_state(self, running, name):
        busy = [p.tool_name for p in self.pages if p.is_running]
        project_page = self.pages[0] if self.pages else None
        if project_page is not None and hasattr(project_page, "set_locked"):
            project_page.set_locked(bool(busy))
        if busy:
            self._status_right.setText(f"运行中: {name}")
            self._status_right.setStyleSheet("color:#00b894;font-weight:bold;")
        else:
            self._status_right.setText("空闲")
            self._status_right.setStyleSheet("color:#b2bec3;")

    def closeEvent(self, event):
        if getattr(self, '_closing_workers', None) is not None:
            if not self._close_workers_finished():
                event.ignore()
                return
        workers = []
        running = []
        for page in self.pages:
            worker = getattr(page, 'worker', None)
            alive = False
            if worker is not None:
                try:
                    alive = worker.isRunning()
                    if alive:
                        workers.append(worker)
                except RuntimeError:
                    pass
            if page.is_running or alive:
                running.append(page.tool_name)
        if running and getattr(self, '_closing_workers', None) is None:
            answer = QMessageBox.question(
                self, "任务正在运行",
                "以下任务正在运行：\n  " + "\n  ".join(running) +
                "\n\n确定退出并停止这些任务吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._closing_workers = workers
            for p in self.pages:
                if p.is_running:
                    try:
                        p.prepare_stop()
                    except Exception:
                        pass
            for p in self.pages:
                w = getattr(p, "worker", None)
                if w is None:
                    continue
                # PingWorker 等通过 prepare_stop 设置停止事件；
                # 其余线程同时调用 stop，随后统一异步等待。
                try:
                    w.stop()
                except Exception:
                    pass
            if not self._close_workers_finished():
                self._status_label.setText("正在停止任务，等待资源清理完成后退出...")
                self.centralWidget().setEnabled(False)
                event.ignore()
                QTimer.singleShot(100, self._poll_close_workers)
                return
        for page in self.pages:
            flush = getattr(page, "flush_pending_save", None)
            if callable(flush):
                try:
                    flush()
                except Exception:
                    logging.warning("退出前保存配置失败:\n%s",
                                    traceback.format_exc())
        event.accept()

    def _close_workers_finished(self):
        for worker in self._closing_workers:
            try:
                if not worker.wait(0):
                    return False
            except RuntimeError:
                # Qt 已销毁的线程对象已经结束。
                continue
        return True

    def _poll_close_workers(self):
        if self._close_workers_finished():
            self.close()
        else:
            QTimer.singleShot(100, self._poll_close_workers)


def _exception_hook(exc_type, exc_value, exc_tb):
    err = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logging.error(f"未处理的异常:\n{err}")
    # 无控制台打包时 sys.__stderr__ 为 None，不能直接写
    stream = sys.__stderr__ or sys.stderr
    if stream is not None:
        try:
            stream.write(err)
            stream.flush()
        except Exception:
            pass


def _patch_std_streams():
    """
    无控制台窗口打包时 sys.stdout / sys.stderr 为 None，
    业务脚本里的 print() 会抛 AttributeError。这里替换为空实现。
    """
    class _NullWriter:
        def write(self, *args, **kwargs):
            return 0

        def flush(self):
            pass

        def isatty(self):
            return False

        def close(self):
            pass

    if sys.stdout is None:
        sys.stdout = _NullWriter()
    if sys.stderr is None:
        sys.stderr = _NullWriter()


def main():
    _patch_std_streams()
    app = QApplication(sys.argv)
    app.setStyleSheet(gc.DARK_STYLESHEET)
    sys.excepthook = _exception_hook

    icon = gc.resource_path('2222.ico')
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))

    # 全局基础日志（各页面启动时会接管 root logger）
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')

    try:
        window = ToolBoxWindow()
        window.show()
        sys.exit(app.exec_())
    except Exception:
        err = traceback.format_exc()
        QMessageBox.critical(None, "程序启动出错",
                             f"发生未处理的异常，程序无法继续运行:\n\n{err}")
        sys.exit(1)


if __name__ == '__main__':
    main()
