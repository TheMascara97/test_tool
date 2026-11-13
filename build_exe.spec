# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 — 设备测试工具箱

用法（在 fota_test 目录下）：
    pyinstaller build_exe.spec --noconfirm
或直接双击运行 build_exe.bat

产物：
    dist/ToolBox.exe             （单文件，无控制台窗口）
"""

import importlib.util
import os

# SPECPATH 由 PyInstaller 注入（spec 所在目录），
# 这样从任意工作目录调用都能正确定位源码与图标
HERE = globals().get('SPECPATH') or os.path.abspath(os.getcwd())
ICON = os.path.join(HERE, '2222.ico')

# 业务模块由 GUI 延迟导入（函数体内 import），
# 静态分析有时抓不到，这里显式声明为隐藏导入
hidden_imports = [
    'gui_project',
    'gui_flash',
    'gui_reboot',
    'gui_fota',
    'gui_net',
    'gui_ping',
    'gui_t1l',
    'gui_rtc',
    'reboot_test',
    'NASH_FOTA',
    'serial',
    'serial.tools',
    'serial.tools.list_ports',
    'cryptography',
    'cryptography.hazmat.primitives.hashes',
    'cryptography.hazmat.primitives.serialization',
    'cryptography.hazmat.primitives.asymmetric.ec',
    'cryptography.hazmat.primitives.asymmetric.utils',
]

# paramiko 是 FOTA 命令触发降级 SSH 通道的可选依赖；
# 开发环境未安装时保留运行时提示，不影响其它降级方式。
if importlib.util.find_spec('paramiko') is not None:
    hidden_imports.append('paramiko')

# hidapi 是继电器 HID 模式的可选依赖；未安装时跳过（程序运行时给出安装提示）
if importlib.util.find_spec('hid') is not None:
    hidden_imports.append('hid')

a = Analysis(
    [os.path.join(HERE, 'gui_main.py')],
    pathex=[HERE],
    binaries=[],
    datas=[(ICON, '.')],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(HERE, 'rth_stdio_fix.py')],
    excludes=[
        'tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy',
        'IPython', 'jupyter', 'pytest',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ToolBox',           # 必须用 ASCII 名：中文 exe 名会让 PyQt5 在 show() 时
                              # 在 ucrtbase.dll 里 abort（0xC0000409），与本项目代码无关
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX 压缩容易触发杀软误报，关闭
    upx_exclude=[],
    # Keep one-file extraction beside the exe. The Windows bootloader needs an
    # absolute runtime path; locked-down machines may block writes into %TEMP%.
    runtime_tmpdir=os.path.join(HERE, 'dist'),
    console=False,            # GUI 程序：不弹出控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
