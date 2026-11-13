# 设备测试工具箱 (ToolBox)

一个基于 PyQt5 的 Windows 桌面工具，把设备产测/可靠性测试的常用流程集中到同一个界面里。
打包产物是单文件 `ToolBox.exe`，无需安装 Python。

## 功能页面

| 页面 | 源码 | 说明 |
| --- | --- | --- |
| 项目管理 | `gui_project.py` | 集中维护 ADB / 串口 / SSH / 日志通道 / MSH / 开机检测，多项目切换 |
| 开关机测试 | `gui_reboot.py` + `reboot_test.py` | 继电器通断电循环 + 串口开机判定 + 可选 ping/ADB 检测 |
| FOTA 升降级 | `gui_fota.py` + `NASH_FOTA.py` | 反复升级/降级；降级可选 JFLASH、断电触发或 ADB/SSH/串口命令触发 |
| 网络测试 | `gui_net.py` | 两个 Tab：PC 端 Ping（`gui_ping.py`）、ADB Ping/T1L（`gui_t1l.py`） |
| 循环读取时间 | `gui_rtc.py` | 通过 ADB 周期性读取设备 RTC 并追加写文件 |
| Flash 读写 | `gui_flash.py` | ADB push/pull 循环读写 + SHA-256 校验 |

同一时间只允许一个测试任务运行（串口 / ADB / 日志互斥），运行期间项目管理页会被锁定。

### 共享日志通道

项目配置中的「日志通道」可选择 ADB、SSH 或串口。ADB/SSH 可配置持续日志命令，默认是
`logcat -v time`；串口使用项目第一路串口。

测试启动时先打开共享会话，后续 FOTA 命令触发、版本检查、reboot 和开关机串口开机检测都会
复用该会话。串口不会二次打开；ADB/SSH 不会重复登录。测试结束或用户停止后自动释放。

ADB/SSH 通道在设备重启导致连接断开时会自动重连并恢复日志命令；断开期间发送的命令会立即
返回「正在自动重连」，由测试流程按失败重试处理。ADB 启动前会预检设备在线状态。
MSH 认证依赖串口，日志通道为 ADB/SSH 时需在项目中关闭 MSH 或改用串口通道。

## 运行环境

```
Python 3.13.11 (Windows x64)
PyQt5 5.15.11 / Qt 5.15.2
pyserial 3.5
cryptography 46.0.3
```

完整依赖见 `requirements.txt`：

```bat
pip install -r requirements.txt
```

可选依赖 `hid`：仅在开关机测试使用「HID 继电器」模式时需要，未安装时该模式会提示，
串口继电器模式不受影响。

可选依赖 `paramiko`：项目日志通道或 FOTA 命令触发使用 SSH 时需要；ADB、串口和 JFLASH 模式不依赖它。

## 从源码运行

```bat
python gui_main.py
```

## 打包成 exe

```bat
build_exe.bat
```

脚本会依次运行测试、清理 `build` 和 PyInstaller 临时解包目录、重新打包。
仅打包可执行：

```bat
build_exe.bat --skip-tests --no-pause
```

产物为 `dist\ToolBox.exe`（单文件、无控制台窗口）。

打包注意事项：

- **不要**把 exe 改成中文文件名。PyQt5 在中文 exe 名下调用 `show()` 会在
  `ucrtbase.dll` 里 abort（0xC0000409），与本项目代码无关。
- `gui_config.json` 和 `logs` 目录生成在 **exe 同级目录**（不是临时解包目录）。
- 单文件运行时解包目录固定为打包机上的 `dist` 绝对路径；请勿单独拷贝 exe。
- 首次运行可能被 SmartScreen 拦截，选「更多信息 → 仍要运行」。
- PyInstaller 不会删除 exe 同级的 `gui_config.json` 和 `logs` 目录。

## 目录约定

```
gui_main.py        统一入口 / 左侧导航 / 状态栏 / 关闭时等待线程退出
gui_common.py      主题样式、配置读写、日志 Handler、统计卡片、页面基类 BasePage
process_utils.py   可取消的等待与子进程执行（进程树清理），不依赖 GUI
gui_*.py           各功能页面
reboot_test.py     开关机测试业务引擎（无 GUI）
NASH_FOTA.py       FOTA 升级降级业务引擎（无 GUI）
device_session.py  ADB / SSH / 串口共享日志与命令会话
rth_stdio_fix.py   PyInstaller 运行时钩子：无控制台时修复 stdout/stderr
build_exe.spec     PyInstaller 配置
```

运行期生成（不入版本库，见 `.gitignore`）：

```
gui_config.json    界面配置 + 项目档案（含明文口令，注意不要外发）
logs/<工具>_<时间戳>/   每次运行的日志与报告
```

## 已知问题

- 配置文件解析失败时会自动备份为 `gui_config.corrupt_<时间戳>.json`，随后使用默认配置继续运行。
- 开关机测试跟随项目配置的前两路串口；FOTA 跟随第一路串口。
- 配置与导出文件仍包含明文口令，请只保存在受控电脑上，不要外发。
