# -*- coding: utf-8 -*-
"""
PyInstaller 运行时钩子 — 在导入任何模块（含 PyQt5）之前执行。

无控制台（console=False）打包时，Windows 子系统程序没有标准句柄，
Python 的 sys.stdout / sys.stderr 为 None，且 C 运行库的 fd 1/2 无效。
Qt5 与 MSVC 运行库在写 stderr 时会走 CRT 的 invalid parameter handler 直接
abort()，表现为崩溃退出码 0xC0000409（事件日志里挂在 ucrtbase.dll）。

这里把 fd 1/2 重定向到 NUL，并给 sys.stdout/sys.stderr 接上真实文件对象，
使 C 层 fprintf 与 Python 层 print 都安全。
"""

import os
import sys

if sys.stdout is None or sys.stderr is None:
    try:
        fd = os.open(os.devnull, os.O_RDWR)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        if fd > 2:
            os.close(fd)
    except OSError:
        pass

    if sys.stdout is None:
        sys.stdout = open(1, 'w', encoding='utf-8', errors='replace',
                          buffering=1, closefd=False)
    if sys.stderr is None:
        sys.stderr = open(2, 'w', encoding='utf-8', errors='replace',
                          buffering=1, closefd=False)
