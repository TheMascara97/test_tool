import serial
import time
import logging
import sys
import signal
import os
import threading
import re
import queue
from datetime import datetime
from process_utils import (
    run_cancellable_process,
    sleep_interruptible,
    terminate_process_tree,
)
import device_session

# 密码学库 (用于 msh AUTH SEED 签名认证)
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

# ================= 用户配置区域 =================

# --- 被测设备 (DUT) 串口配置 ---
DUT_COM_PORT = 'COM9'       # 设备串口 (用于读取日志、检测版本和开机标志)
DUT_BAUDRATE = 115200        # 波特率

# --- 继电器硬件配置 ---
# 启用后：设备异常重启、降级脚本失败重试前，均通过继电器通断电重启设备
RELAY_ENABLED = True
RELAY_COM_PORT = 'COM8'     # 继电器串口 (独立于设备串口)
RELAY_BAUDRATE = 115200      # 继电器波特率
OTFDEC_ERROR_FLAG = 'OTFDEC external flash init failed: status=-108, prescaler=1'  # OTFDEC 错误标志
RELAY_POWER_OFF_TIME = 5     # 继电器断电持续时间 (秒)
RELAY_POWER_ON_WAIT = 3      # 继电器上电后等待硬件稳定的时间 (秒)

# --- 版本号配置 ---
# 升级和降级使用不同的版本号进行判断
# 支持普通字符串匹配，也支持正则表达式 (设置 USE_REGEX=True)
BOOT_VERSION = 'sw_version:Nash_DataCollection-0.1.1'    # 设备默认版本 (降级后的版本，开机时检测)
UPGRADE_VERSION = 'sw_version:Nash_DataCollection-99.9.9'     # FOTA 升级成功后的版本 (升级完成后检测)
USE_REGEX = False              # 是否使用正则匹配版本号 (True=正则, False=子串匹配)

# --- 开机成功标志 ---
# 从串口读取到此字符串时认为设备开机成功 (区分大小写)
BOOT_FLAG = 'System boot completed'   # 开机成功标志 (请根据实际串口输出修改)
BOOT_TIMEOUT = 180                     # 最大等待启动时间 (秒)
BOOT_VERSION_DETECT_TIMEOUT = 30       # 开机后未检测到任一版本号时的重启等待时间 (秒)

# --- FOTA 升级等待配置 ---
# 设备开机后会自动检测升级任务并执行升级
# 升级状态机: 开机 → upStatus":3 (升级开始) → upStatus":7 (升级成功) → 版本检测
# 异常处理: 若未检测到 upStatus":3 就直接出现 upStatus":7，说明升级未触发，需重启重试
FOTA_UPGRADE_START_FLAG = 'upStatus":3'    # FOTA 升级开始标志
FOTA_UPGRADE_COMPLETE_FLAG = 'upStatus":7'  # FOTA 升级完成标志
FOTA_UPGRADE_TIMEOUT = 600                  # 等待FOTA升级完成的最大时间 (秒)
FOTA_RETRY_MAX = 3                          # 升级未触发时最大重启重试次数

# --- 降级方式 ---
# jflash     : 执行下方脚本，兼容原有流程
# power_cycle: 通过继电器断电/上电触发设备降级
# command    : 通过 ADB / SSH / 串口发送降级命令
DOWNGRADE_MODE = 'jflash'

# --- JFLASH 降级配置 ---
# 降级脚本按列表顺序依次执行，数量不限（GUI 的「降级」页可以增删、排序）
#   path            : .bat 脚本路径
#   success_keyword : 在脚本输出中搜索到该字符串视为成功（留空则仅依赖返回码）
#   fail_keyword    : 在脚本输出中搜索到该字符串直接判定失败（留空则不检测）
DOWNGRADE_SCRIPTS = [
    {
        "path": r"E:\NASH\version\20260902\Nash_DataCollection-DataCollection.hw_v1-0.1.1-user-440081709\ROT_Provisioning\DA\regression_jlink.bat",
        "success_keyword": 'STM32H5 full regression Succeed',
        "fail_keyword": 'failed',
    },
    {
        "path": r"E:\NASH\version\20260902\Nash_DataCollection-DataCollection.hw_v1-0.1.1-user-440081709\ROT_Provisioning\STiROT_OEMuROT\provisioning_jlink.bat",
        "success_keyword": 'J-Link provisioning completed successfully.',
        "fail_keyword": 'failed',
    },
]
DOWNGRADE_TIMEOUT = 300       # 单个降级脚本的最大执行超时 (秒)
DOWNGRADE_SCRIPT_RETRIES = 3  # 每条脚本在同一轮内最多尝试次数
DOWNGRADE_MAX_ROUNDS = 3      # 整体降级的最大轮数 (含首次)

# 兼容旧命名：外部脚本 import 这两个常量时不会报错（由列表前两项派生）
DOWNGRADE_BAT_1 = DOWNGRADE_SCRIPTS[0]["path"] if len(DOWNGRADE_SCRIPTS) > 0 else ''
DOWNGRADE_BAT_2 = DOWNGRADE_SCRIPTS[1]["path"] if len(DOWNGRADE_SCRIPTS) > 1 else ''
DOWNGRADE_SUCCESS_KEYWORD_1 = DOWNGRADE_SCRIPTS[0]["success_keyword"] if len(DOWNGRADE_SCRIPTS) > 0 else ''
DOWNGRADE_SUCCESS_KEYWORD_2 = DOWNGRADE_SCRIPTS[1]["success_keyword"] if len(DOWNGRADE_SCRIPTS) > 1 else ''
DOWNGRADE_FAIL_KEYWORD_1 = DOWNGRADE_SCRIPTS[0]["fail_keyword"] if len(DOWNGRADE_SCRIPTS) > 0 else ''
DOWNGRADE_FAIL_KEYWORD_2 = DOWNGRADE_SCRIPTS[1]["fail_keyword"] if len(DOWNGRADE_SCRIPTS) > 1 else ''

# --- 命令触发降级配置 ---
DOWNGRADE_TRIGGER_CHANNEL = 'adb'       # adb / ssh / serial
DOWNGRADE_TRIGGER_CMD = ''              # 设备侧降级命令，支持每行一条
DOWNGRADE_TRIGGER_EXPECT = ''           # 可选成功输出关键词，留空只看命令执行结果
DOWNGRADE_TRIGGER_TIMEOUT = 30          # 命令执行超时 (秒)
DOWNGRADE_TRIGGER_BOOT_WAIT = 10        # 触发后等待设备进入降级流程 (秒)
DOWNGRADE_ADB_PATH = 'adb'
DOWNGRADE_ADB_SERIAL = ''
DOWNGRADE_SSH_HOST = ''
DOWNGRADE_SSH_PORT = 22
DOWNGRADE_SSH_USERNAME = ''
DOWNGRADE_SSH_PASSWORD = ''
DOWNGRADE_SSH_KEY_PATH = ''

# --- 降级后等待配置 ---
POST_DOWNGRADE_WAIT = 5       # 降级完成后等待时间 (秒)，给设备重启留出时间

# --- 降级失败重试配置 ---
# 降级脚本某一轮执行失败后，先通过继电器通断电重启设备，再执行下一轮降级脚本
DOWNGRADE_RETRY_RELAY_RESTART = True   # 降级失败重试前是否通过继电器通断电重启设备
DOWNGRADE_RETRY_BOOT_WAIT = 10         # 继电器重启后等待设备重新启动的时间 (秒)

# --- MSH 认证配置 ---
# 设备串口 msh 需要通过 AUTH SEED 挑战-应答认证后才能输入命令
# 流程: 输入回车 → 获取 AUTH SEED → 用私钥签名 → 输入签名 → 进入 msh → 执行命令
MSH_AUTH_ENABLED = True       # 是否启用 msh 认证
MSH_PEM_KEY_PATH = r'E:\NASH\version\20260902\Nash_DataCollection-DataCollection.hw_v1-0.1.1-user-440081709\ROT_Provisioning\STiROT_OEMuROT\User\Keys\MiniShell_Auth_Private.pem'
MSH_AUTH_SEED_PATTERN = r'AUTH SEED:\s*([0-9a-fA-F]+)'  # 匹配 AUTH SEED 的正则
MSH_POST_AUTH_CMD = 'set_dev_info SkyTest10002 v1 0.1.0 C'  # 认证通过后执行的命令
MSH_AUTH_TIMEOUT = 15         # 等待 AUTH SEED 出现的超时 (秒)
# 设备认证成功后不会输出固定成功标志；留空表示等待命令行就绪后直接发送命令。
# 若固件后续提供明确提示符（如 'msh>'），可在此填写以启用严格校验。
MSH_AUTH_SUCCESS_FLAG = ''
MSH_AUTH_READY_WAIT = 5       # 无成功标志时，提交签名后等待 msh 可接收命令的时间 (秒)
MSH_AUTH_FAIL_FLAGS = ('Authorization failed:', 'verify_failed')

# 检测到该日志说明设备未读取到已配置的 SN，需要立即通过 msh 写入一次。
SN_MISSING_LOG_FLAG = 'upload_tools_sn_init: dev_info_get failed, use default sn=SkyTest999999'

# --- 结果校验配置 (每轮升降级结束后执行，两项可同时启用) ---
# 串口校验: 通过设备串口发送命令，在回显中查找 EXPECT 关键字
CHECK_SERIAL_ENABLED = False          # 是否启用串口命令校验
CHECK_SERIAL_CMD = 'dev_info_get'     # 要发送的串口命令
CHECK_SERIAL_EXPECT = ''              # 期望在输出中出现的字符串 (留空=只要命令发出即通过)
CHECK_SERIAL_TIMEOUT = 20             # 等待 EXPECT 出现的超时 (秒)
# ADB 校验: 执行 `adb [-s SERIAL] shell <CMD>`，判断返回码与输出关键字
CHECK_ADB_ENABLED = False             # 是否启用 ADB 命令校验
CHECK_ADB_PATH = 'adb'                # adb 可执行文件路径
CHECK_ADB_SERIAL = ''                 # 设备序列号 (-s 参数)，留空则用唯一连接的设备
CHECK_ADB_CMD = 'getprop ro.build.version'   # 要执行的 shell 命令
CHECK_ADB_EXPECT = ''                 # 期望在输出中出现的字符串 (留空=仅要求返回码为 0)
CHECK_ADB_TIMEOUT = 20                # 命令执行超时 (秒)

# --- 循环次数配置 ---
MAX_CYCLES = 0                # 最大循环次数 (0=无限循环，直到 Ctrl+C 停止)

# --- 日志配置 ---
def _app_dir():
    """程序运行目录：源码运行=脚本目录；PyInstaller 打包后=exe 所在目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


_SCRIPT_DIR = _app_dir()
_LOG_DIR = os.path.join(_SCRIPT_DIR, "logs",
                        "fota_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
# 注意：这里不创建目录。GUI 启动时会导入本模块读取默认配置，
# 若在此建目录，每次打开工具箱都会留下一个空日志目录。
# 实际目录由 set_log_dir() / setup_logging() 在开始测试时创建。

LOG_FILE = os.path.join(_LOG_DIR, "fota_test.log")                # 主程序运行日志
SERIAL_LOG_FILE = os.path.join(_LOG_DIR, "fota_serial_output.log")  # 串口输出日志
REPORT_FILE = os.path.join(_LOG_DIR, "fota_test_report.txt")      # 测试报告
DOWNGRADE_LOG_FILE = os.path.join(_LOG_DIR, "fota_downgrade.log")   # 降级操作日志
DOWNGRADE_STATUS_FILE = os.path.join(_LOG_DIR, "fota_downgrade_status.txt")  # 降级执行状态快照


def set_log_dir(log_dir):
    """切换日志目录 (GUI 每次运行使用独立目录时调用，需在创建 FotaTest 之前)"""
    global _LOG_DIR, LOG_FILE, SERIAL_LOG_FILE, REPORT_FILE
    global DOWNGRADE_LOG_FILE, DOWNGRADE_STATUS_FILE
    _LOG_DIR = log_dir
    os.makedirs(_LOG_DIR, exist_ok=True)
    LOG_FILE = os.path.join(_LOG_DIR, "fota_test.log")
    SERIAL_LOG_FILE = os.path.join(_LOG_DIR, "fota_serial_output.log")
    REPORT_FILE = os.path.join(_LOG_DIR, "fota_test_report.txt")
    DOWNGRADE_LOG_FILE = os.path.join(_LOG_DIR, "fota_downgrade.log")
    DOWNGRADE_STATUS_FILE = os.path.join(_LOG_DIR, "fota_downgrade_status.txt")
    return _LOG_DIR


# ===============================================

def setup_logging():
    """配置日志系统"""
    formatter = logging.Formatter(
        '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    os.makedirs(_LOG_DIR, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers = []
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)


def match_version(line, version_pattern, use_regex=False):
    """
    在一行文本中检测版本号
    返回: (matched: bool, matched_text: str)
    """
    if use_regex:
        m = re.search(version_pattern, line)
        if m:
            return True, m.group(0)
        return False, ''
    else:
        if version_pattern in line:
            return True, version_pattern
        return False, ''


class RelayController:
    """继电器控制器 - 用于 OTFDEC 错误时断电重启设备"""

    def __init__(self, com_port, baudrate, stop_event=None):
        self.com_port = com_port
        self.baudrate = baudrate
        self.handle = None
        self.stop_event = stop_event if stop_event is not None else threading.Event()

    def _stopped(self):
        return self.stop_event.is_set()

    def _connect(self, force=False):
        """连接继电器串口"""
        if self._stopped() and not force:
            return False
        try:
            if self.handle and self.handle.is_open:
                return True
            self.handle = serial.Serial(self.com_port, self.baudrate, timeout=1)
            logging.info(f"[继电器] 已连接: {self.com_port} @ {self.baudrate}")
            return True
        except Exception as e:
            logging.error(f"[继电器] ❌ 连接失败 {self.com_port}: {e}")
            self.handle = None
            return False

    def power_on(self):
        """继电器通电，返回 (ok, cancelled)。"""
        if self._stopped():
            logging.info("[继电器] 已收到停止请求，跳过通电")
            return False, True
        if not self._connect():
            logging.error("[继电器] ❌ 通电失败：无法连接串口")
            return False, self._stopped()
        try:
            if self._stopped():
                return False, True
            cmd = bytes.fromhex('68 09 00 FF 12 03 01 15 16')
            self.handle.write(cmd)
            self.handle.flush()
            logging.info("[继电器] >>> 通电 (指令: 68 09 00 FF 12 03 01 15 16)")
            if sleep_interruptible(self.stop_event, 0.1):
                return False, True
            return True, False
        except Exception as e:
            logging.error(f"[继电器] ❌ 发送通电指令失败: {e}")
            return False, self._stopped()

    def power_off(self, force=False):
        """继电器断电 (发送两次确保可靠断电)，返回 (ok, cancelled)。"""
        if self._stopped() and not force:
            logging.info("[继电器] 已收到停止请求，跳过断电指令")
            return False, True
        if not self._connect(force=force):
            logging.error("[继电器] ❌ 断电失败：无法连接串口")
            return False, self._stopped()
        try:
            if self._stopped() and not force:
                return False, True
            cmd = bytes.fromhex('68 09 00 FF 12 03 00 14 16')
            self.handle.write(cmd)
            self.handle.flush()
            logging.info("[继电器] <<< 断电 (第1次)")
            if (not force
                    and sleep_interruptible(self.stop_event, 0.1)):
                return False, True
            if force:
                sleep_interruptible(None, 0.1)
            elif self._stopped():
                return False, True
            self.handle.write(cmd)
            self.handle.flush()
            logging.info("[继电器] <<< 断电 (第2次)")
            if (not force
                    and sleep_interruptible(self.stop_event, 0.1)):
                return False, True
            if force:
                sleep_interruptible(None, 0.1)
            return True, False
        except Exception as e:
            logging.error(f"[继电器] ❌ 发送断电指令失败: {e}")
            return False, self._stopped()

    def close(self):
        """关闭继电器串口"""
        if self.handle:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None

    def power_cycle(self, off_time=RELAY_POWER_OFF_TIME):
        """断电再上电，返回 (ok, cancelled)。"""
        if self._stopped():
            logging.info("[继电器] 已收到停止请求，跳过断电重启")
            return False, True
        logging.info(f"[继电器] 🔄 开始断电重启 (断电持续: {off_time}s)...")
        off_ok, cancelled = self.power_off()
        if cancelled:
            return False, True
        if sleep_interruptible(self.stop_event, off_time) or self._stopped():
            logging.info("[继电器] 停止请求已生效，不再执行上电")
            return False, True
        on_ok, cancelled = self.power_on()
        if cancelled:
            return False, True
        if sleep_interruptible(self.stop_event, RELAY_POWER_ON_WAIT):
            logging.info("[继电器] 上电后收到停止请求")
            return False, True
        if off_ok and on_ok:
            logging.info("[继电器] ✅ 断电重启完成")
            return True, False
        logging.error("[继电器] ⚠️ 断电重启未完全成功 (断电/上电指令有失败)")
        return False, False


class SerialMonitor:
    """
    串口监控器 - 后台线程持续读取串口，支持标志检测
    """

    def __init__(self, port, baudrate, log_file_path, stop_event=None):
        self.port = port
        self.baudrate = baudrate
        self.log_file_path = log_file_path
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.ser = None
        self.log_handle = None
        self.thread = None
        self._running = False
        self._received_lines = queue.Queue(maxsize=2000)
        self._write_lock = threading.Lock()

        # 检测结果
        self.boot_detected = False
        self.boot_time = 0.0
        self.boot_version_detected = False    # 开机版本 (0.1.0-user)
        self.upgrade_version_detected = False  # 升级版本 (90.9.9)
        self.upgrade_started = False        # upStatus":3 升级开始
        self.upgrade_completed = False      # upStatus":7 升级完成
        self.otfdec_error_detected = False   # OTFDEC flash 初始化失败标志
        self.upgrade_started_time = 0.0
        self.otfdec_error_time = 0.0
        self.sn_missing_detected = False     # dev_info_get 失败，需写入 SN
        self._start_time = 0.0

        # 线程安全
        self._lock = threading.Lock()

    def open(self):
        """打开串口和日志文件"""
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.ser = serial.Serial(self.port, self.baudrate, timeout=0.5)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            logging.info(f"串口已打开: {self.port} @ {self.baudrate}")
        except Exception as e:
            logging.error(f"❌ 打开串口 {self.port} 失败：{e}")
            self.ser = None
            return False

        try:
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log_handle = open(self.log_file_path, 'a', encoding='utf-8')
            self.log_handle.write(f"\n{'=' * 60}\n")
            self.log_handle.write(f"串口日志记录开始: {start_time}\n")
            self.log_handle.write(f"串口: {self.port} @ {self.baudrate}\n")
            self.log_handle.write(f"{'=' * 60}\n")
            self.log_handle.flush()
            logging.info(f"串口日志文件就绪：{self.log_file_path}")
        except Exception as e:
            logging.error(f"❌ 无法创建串口日志文件：{e}")
            self.log_handle = None
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
            return False

        return True

    def reset_flags(self):
        """重置检测标志 (每轮循环开始时调用)"""
        with self._lock:
            self.boot_detected = False
            self.boot_time = 0.0
            self.boot_version_detected = False
            self.upgrade_version_detected = False
            self.upgrade_started = False
            self.upgrade_completed = False
            self.otfdec_error_detected = False
            self.upgrade_started_time = 0.0
            self.otfdec_error_time = 0.0
            self.sn_missing_detected = False
            self._start_time = time.time()

    def start(self):
        """启动后台读取线程"""
        self._running = True
        self.reset_flags()
        self.thread = threading.Thread(target=self._read_loop, name="SerialReader", daemon=True)
        self.thread.start()
        logging.info("📖 串口后台读取线程已启动")

    def _read_loop(self):
        """后台持续读取串口数据"""
        while self._running and not self.stop_event.is_set():
            if self.ser and self.ser.in_waiting:
                try:
                    raw_line = self.ser.readline()
                    if raw_line:
                        line = raw_line.decode('utf-8', errors='ignore').strip()
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

                        # 写入日志文件
                        if self.log_handle:
                            self.log_handle.write(f"[{timestamp}] {line}\n")
                            self.log_handle.flush()

                        # 认证等需要响应内容的流程从队列读取，串口只由本线程读取。
                        try:
                            self._received_lines.put_nowait((timestamp, line))
                        except queue.Full:
                            try:
                                self._received_lines.get_nowait()
                                self._received_lines.put_nowait((timestamp, line))
                            except queue.Empty:
                                pass

                        # 检测开机标志
                        with self._lock:
                            if not self.boot_detected and BOOT_FLAG in line:
                                self.boot_detected = True
                                self.boot_time = time.time() - self._start_time
                                logging.info(f"✅ [串口线程] 检测到开机标志：'{BOOT_FLAG}' (耗时: {self.boot_time:.1f}s)")

                            # 检测开机版本 (0.1.0-user)
                            boot_ver, _ = match_version(line, BOOT_VERSION, USE_REGEX)
                            if boot_ver and not self.boot_version_detected:
                                self.boot_version_detected = True
                                logging.info(f"✅ [串口线程] 检测到开机版本：'{BOOT_VERSION}'")

                            # 检测升级版本 (90.9.9)
                            upg_ver, _ = match_version(line, UPGRADE_VERSION, USE_REGEX)
                            if upg_ver and not self.upgrade_version_detected:
                                self.upgrade_version_detected = True
                                logging.info(f"✅ [串口线程] 检测到升级版本：'{UPGRADE_VERSION}'")

                            # 检测 FOTA 升级开始 (upStatus":3)
                            if not self.upgrade_started and FOTA_UPGRADE_START_FLAG in line:
                                self.upgrade_started = True
                                self.upgrade_started_time = time.time()
                                logging.info(f"✅ [串口线程] 检测到 FOTA 升级开始: '{FOTA_UPGRADE_START_FLAG}'")

                            # 检测 FOTA 升级完成 (upStatus":7)
                            if not self.upgrade_completed and FOTA_UPGRADE_COMPLETE_FLAG in line:
                                self.upgrade_completed = True
                                logging.info(f"✅ [串口线程] 检测到 FOTA 升级完成: '{FOTA_UPGRADE_COMPLETE_FLAG}'")

                            # 检测 OTFDEC 错误
                            if not self.otfdec_error_detected and OTFDEC_ERROR_FLAG in line:
                                self.otfdec_error_detected = True
                                self.otfdec_error_time = time.time()
                                logging.warning(f"⚠️ [串口线程] 检测到 OTFDEC 错误: '{OTFDEC_ERROR_FLAG}'")

                            # dev_info 未配置时，通知主流程执行一次 set_dev_info。
                            if not self.sn_missing_detected and SN_MISSING_LOG_FLAG in line:
                                self.sn_missing_detected = True
                                logging.warning(
                                    "⚠️ [串口线程] 检测到设备 SN 未配置，"
                                    "将执行一次 set_dev_info"
                                )

                except Exception as e:
                    if self._running:
                        logging.warning(f"读取串口数据异常：{e}")
            else:
                sleep_interruptible(self.stop_event, 0.05)

    def get_boot_status(self):
        """获取开机检测结果 (线程安全)"""
        with self._lock:
            return self.boot_detected, self.boot_time

    def get_boot_version_status(self):
        """获取开机版本检测结果 (线程安全)"""
        with self._lock:
            return self.boot_version_detected

    def get_upgrade_version_status(self):
        """获取升级版本检测结果 (线程安全)"""
        with self._lock:
            return self.upgrade_version_detected

    def get_upgrade_status(self):
        """获取 FOTA 升级状态 (线程安全)"""
        with self._lock:
            return self.upgrade_started, self.upgrade_completed

    def get_otfdec_error(self):
        """获取 OTFDEC 错误检测结果 (线程安全)"""
        with self._lock:
            return self.otfdec_error_detected

    def get_otfdec_error_during_upgrade(self):
        """仅在已检测到 upStatus":3 后返回 OTFDEC 错误。"""
        with self._lock:
            return (
                self.upgrade_started
                and self.otfdec_error_detected
                and self.otfdec_error_time >= self.upgrade_started_time
            )

    def get_sn_missing_status(self):
        """获取设备 SN 未配置检测结果 (线程安全)"""
        with self._lock:
            return self.sn_missing_detected

    def clear_received_lines(self):
        """清空已被后台线程接收的历史串口行。"""
        while True:
            try:
                self._received_lines.get_nowait()
            except queue.Empty:
                return

    def get_received_line(self, timeout=0.1):
        """获取后台串口线程已读取的一行数据。"""
        try:
            return self._received_lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def write_bytes(self, data):
        """向保持打开状态的设备串口写入数据，不影响后台读取线程。"""
        try:
            with self._write_lock:
                if not self.ser or not self.ser.is_open:
                    return False
                self.ser.write(data)
                self.ser.flush()
            return True
        except Exception as e:
            logging.error(f"发送串口数据失败: {e}")
            return False

    def send_command(self, cmd):
        """发送命令，后台串口读取线程始终保持运行。"""
        return self.write_bytes((cmd + '\r\n').encode('utf-8'))

    def wait_for_pattern(self, pattern, timeout=20, use_regex=False, stop_check=None):
        """
        在后台线程已接收的串口行中等待 pattern 出现。

        参数:
            pattern:    要查找的字符串（use_regex=True 时为正则）
            timeout:    超时秒数
            use_regex:  是否按正则匹配
            stop_check: 可选的 callable，返回 False 时提前放弃
        返回: (matched: bool, matched_line: str, cancelled: bool)
        """
        if not pattern:
            return True, '', False
        matcher = re.compile(pattern).search if use_regex else None
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.stop_event.is_set() or (
                    stop_check is not None and not stop_check()):
                return False, '', True
            received = self.get_received_line(timeout=0.1)
            if not received:
                continue
            _, line = received
            hit = matcher(line) if matcher else (pattern in line)
            if hit:
                return True, line, False
        return False, '', self.stop_event.is_set()

    def stop(self):
        """停止读取并关闭资源"""
        self._running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)

        if self.log_handle:
            try:
                end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.log_handle.write(f"\n{'=' * 60}\n")
                self.log_handle.write(f"串口日志记录结束: {end_time}\n")
                self.log_handle.write(f"{'=' * 60}\n")
                self.log_handle.close()
            except Exception:
                pass
            self.log_handle = None

        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

        logging.info("串口监控已停止")


class DowngradeExecutor:
    """
    降级执行器 - 按 JFLASH / 断电 / 命令触发模式执行
    """

    def __init__(self, log_file_path, relay_controller=None, stop_event=None):
        self.log_file_path = log_file_path
        self.log_handle = None
        self.relay = relay_controller
        self.current_process = None
        self.status_file_path = DOWNGRADE_STATUS_FILE
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.owner = None

    @property
    def running(self):
        """降级编排是否仍在运行，只由自己的停止事件决定。"""
        return not self.stop_event.is_set()

    @running.setter
    def running(self, value):
        if value:
            self.stop_event.clear()
        else:
            self.stop_event.set()

    def open_log(self):
        """打开降级日志文件"""
        try:
            self.log_handle = open(self.log_file_path, 'a', encoding='utf-8')
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log_handle.write(f"\n{'=' * 60}\n")
            self.log_handle.write(f"降级操作日志记录开始: {start_time}\n")
            self.log_handle.write(f"降级脚本共 {len(DOWNGRADE_SCRIPTS)} 个:\n")
            for i, script in enumerate(DOWNGRADE_SCRIPTS, 1):
                self.log_handle.write(f"  脚本{i}: {script['path']}\n")
            self.log_handle.write(f"{'=' * 60}\n")
            self.log_handle.flush()
            logging.info(f"降级日志文件就绪：{self.log_file_path}")
            return True
        except Exception as e:
            logging.error(f"❌ 无法创建降级日志文件：{e}")
            self.log_handle = None
            return False

    def _log(self, msg):
        """写入降级日志"""
        if self.log_handle:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self.log_handle.write(f"[{timestamp}] {msg}\n")
            self.log_handle.flush()

    def _write_status(self, step_name, status, pid=None, elapsed=0.0,
                      return_code=None, log_file='', detail='',
                      last_output_time=None):
        """写入降级执行状态快照，便于外部检测当前脚本是否仍在运行"""
        try:
            with open(self.status_file_path, 'w', encoding='utf-8') as f:
                f.write(f"更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"步骤: {step_name}\n")
                f.write(f"状态: {status}\n")
                if pid is not None:
                    f.write(f"PID: {pid}\n")
                f.write(f"已运行: {elapsed:.1f}s\n")
                f.write(f"超时: {DOWNGRADE_TIMEOUT}s\n")
                if last_output_time:
                    f.write(f"最后输出时间: {last_output_time}\n")
                if return_code is not None:
                    f.write(f"返回码: {return_code}\n")
                if log_file:
                    f.write(f"输出日志: {log_file}\n")
                if detail:
                    f.write(f"详情: {detail}\n")
        except Exception as e:
            logging.warning(f"⚠️ 写入降级状态文件失败: {e}")

    def run_bat(self, bat_path, step_name, success_keyword='', fail_keyword=''):
        """
        运行一个 .bat 文件并捕获输出
        success_keyword: 成功关键字（运行中检测到即视为该步骤成功）
        fail_keyword: 失败关键字（匹配则直接判定失败，优先级高于成功关键字）
        输出会实时保存到独立的 log 文件，并在运行中检测关键字。
        命中成功关键字后会主动结束 bat 及其子进程，避免脚本持续占用进程。
        返回: (success, return_code, output, reason)
        """
        logging.info(f"🔧 [{step_name}] 开始执行降级脚本: {bat_path}")
        self._log(f"[{step_name}] 开始执行: {bat_path}")

        if self.stop_event.is_set():
            return False, -1, '', 'cancelled'
        if not os.path.exists(bat_path):
            msg = f"❌ [{step_name}] 脚本文件不存在: {bat_path}"
            logging.error(msg)
            self._log(msg)
            self._write_status(step_name, 'MISSING_SCRIPT', return_code=-1, detail=msg)
            return False, -1, msg, 'missing'

        bat_log_dir = os.path.join(_LOG_DIR, "bat_output")
        os.makedirs(bat_log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bat_log_file = os.path.join(bat_log_dir, f"{step_name}_{timestamp}.log")

        start_time = time.time()
        output_parts = []
        success_marker_detected = False
        failure_marker_detected = False
        last_heartbeat = start_time
        last_output_time = None
        process_pid = None
        success_keyword_normalized = (
            success_keyword.replace('\r\n', '\n').replace('\r', '\n').lower()
        )
        fail_keyword_normalized = (
            fail_keyword.replace('\r\n', '\n').replace('\r', '\n').lower()
        )
        live_output_normalized = ''

        try:
            with open(bat_log_file, 'w', encoding='utf-8') as f:
                f.write(f"脚本路径: {bat_path}\n")
                f.write(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n")
                f.flush()

                def on_start(process):
                    nonlocal process_pid
                    process_pid = process.pid
                    self.current_process = process
                    logging.info(f"📄 [{step_name}] 实时输出日志: {bat_log_file}")
                    logging.info(
                        f"🔎 [{step_name}] 进程已启动 PID={process.pid}，"
                        f"状态文件: {self.status_file_path}"
                    )
                    self._log(f"[{step_name}] PID={process.pid} | 输出日志: {bat_log_file}")
                    self._write_status(
                        step_name, 'RUNNING', pid=process.pid, elapsed=0.0,
                        log_file=bat_log_file, detail='脚本正在执行'
                    )
                    return False

                def on_output(line):
                    nonlocal success_marker_detected
                    nonlocal failure_marker_detected
                    nonlocal last_output_time
                    nonlocal live_output_normalized

                    output_parts.append(line)
                    f.write(line)
                    f.flush()
                    last_output_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    live_output_normalized += (
                        line.replace('\r\n', '\n').replace('\r', '\n').lower()
                    )

                    if fail_keyword_normalized and fail_keyword_normalized in live_output_normalized:
                        failure_marker_detected = True
                        marker_msg = (
                            f"❌ [{step_name}] 运行中检测到失败关键字 "
                            f"'{fail_keyword}'，正在结束脚本进程..."
                        )
                        logging.error(marker_msg)
                        self._log(marker_msg)
                        f.write(f"\n[FAILURE_MARKER] {fail_keyword}\n")
                        f.flush()
                        self._write_status(
                            step_name, 'FAILED_DETECTED', pid=process_pid,
                            elapsed=time.time() - start_time, log_file=bat_log_file,
                            detail=f"检测到失败关键字: {fail_keyword}",
                            last_output_time=last_output_time
                        )
                        return True

                    if success_keyword_normalized and success_keyword_normalized in live_output_normalized:
                        success_marker_detected = True
                        marker_msg = (
                            f"✅ [{step_name}] 运行中检测到成功关键字 "
                            f"'{success_keyword}'，正在结束脚本进程..."
                        )
                        logging.info(marker_msg)
                        self._log(marker_msg)
                        f.write(f"\n[SUCCESS_MARKER] {success_keyword}\n")
                        f.flush()
                        self._write_status(
                            step_name, 'SUCCESS_DETECTED', pid=process_pid,
                            elapsed=time.time() - start_time, log_file=bat_log_file,
                            detail=f"检测到成功关键字: {success_keyword}",
                            last_output_time=last_output_time
                        )
                        return True
                    return False

                def on_poll(elapsed):
                    nonlocal last_heartbeat
                    if elapsed - (last_heartbeat - start_time) < 10:
                        return
                    heartbeat_msg = (
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"[HEARTBEAT] {step_name} still running, "
                        f"elapsed={elapsed:.1f}s, pid={process_pid}\n"
                    )
                    f.write(heartbeat_msg)
                    f.flush()
                    logging.info(
                        f"⏳ [{step_name}] 仍在执行... 已运行 {elapsed:.1f}s "
                        f"(PID={process_pid})"
                    )
                    self._write_status(
                        step_name, 'RUNNING', pid=process_pid, elapsed=elapsed,
                        log_file=bat_log_file, detail='脚本正在执行',
                        last_output_time=last_output_time
                    )
                    last_heartbeat = time.time()

                def on_finish(_process):
                    self.current_process = None

                result = run_cancellable_process(
                    ['cmd.exe', '/c', bat_path],
                    stop_event=self.stop_event,
                    timeout=DOWNGRADE_TIMEOUT,
                    cwd=os.path.dirname(bat_path),
                    encoding='utf-8',
                    errors='ignore',
                    on_output=on_output,
                    on_poll=on_poll,
                    on_start=on_start,
                    on_finish=on_finish,
                    label=f"{step_name}-降级脚本",
                )

                elapsed = time.time() - start_time
                output = ''.join(output_parts)
                return_code = result.return_code if result.return_code is not None else -1
                f.write("\n" + "=" * 60 + "\n")
                f.write(f"结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"结束原因: {result.reason}\n")
                f.write(f"返回码: {return_code}\n")
                f.write(f"耗时: {elapsed:.1f}s\n")
                f.flush()

            logging.info(f"📄 [{step_name}] 输出已保存到: {bat_log_file}")
            self._log(
                f"[{step_name}] 完成 | 原因: {result.reason} | "
                f"返回码: {return_code} | 耗时: {elapsed:.1f}s"
            )
            self._log(f"[{step_name}] 输出日志: {bat_log_file}")

            if result.cancelled or self.stop_event.is_set():
                self._write_status(
                    step_name, 'CANCELLED', pid=process_pid, elapsed=elapsed,
                    return_code=return_code, log_file=bat_log_file,
                    detail='用户停止', last_output_time=last_output_time
                )
                return False, return_code, output, 'cancelled'

            if result.timed_out:
                msg = f"❌ [{step_name}] 执行超时 ({DOWNGRADE_TIMEOUT}s)，已强制终止"
                logging.error(msg)
                self._log(f"[{step_name}] TIMEOUT after {elapsed:.1f}s")
                self._write_status(
                    step_name, 'TIMEOUT', pid=process_pid, elapsed=elapsed,
                    return_code=-2, log_file=bat_log_file, detail=msg,
                    last_output_time=last_output_time
                )
                return False, -2, output, 'timeout'

            if result.reason == 'error':
                msg = f"❌ [{step_name}] 执行异常: {result.error}"
                logging.error(msg)
                self._write_status(
                    step_name, 'EXCEPTION', pid=process_pid, elapsed=elapsed,
                    return_code=-3, log_file=bat_log_file, detail=msg,
                    last_output_time=last_output_time
                )
                return False, -3, output, 'error'

            if failure_marker_detected:
                success = False
            elif success_marker_detected:
                success = True
            elif fail_keyword_normalized and fail_keyword_normalized in live_output_normalized:
                success = False
            else:
                success = return_code == 0
                if success and success_keyword_normalized:
                    success = success_keyword_normalized in live_output_normalized

            if success:
                result_detail = (
                    f"检测到成功关键字并已结束脚本进程: {success_keyword}"
                    if success_marker_detected else '脚本执行成功'
                )
                logging.info(
                    f"✅ [{step_name}] 降级脚本执行成功 "
                    f"(返回码: {return_code}, 耗时: {elapsed:.1f}s)"
                )
                self._write_status(
                    step_name, 'SUCCESS', pid=process_pid, elapsed=elapsed,
                    return_code=return_code, log_file=bat_log_file,
                    detail=result_detail, last_output_time=last_output_time
                )
                return True, return_code, output, 'success'

            logging.error(
                f"❌ [{step_name}] 降级脚本执行失败 "
                f"(返回码: {return_code}, 耗时: {elapsed:.1f}s)"
            )
            logging.error(f"   输出末尾:\n{output[-500:]}")
            self._write_status(
                step_name, 'FAILED', pid=process_pid, elapsed=elapsed,
                return_code=return_code, log_file=bat_log_file,
                detail='脚本执行失败', last_output_time=last_output_time
            )
            return False, return_code, output, 'failed'

        except Exception as e:
            elapsed = time.time() - start_time
            msg = f"❌ [{step_name}] 执行异常: {e}"
            logging.error(msg, exc_info=True)
            self._log(f"[{step_name}] EXCEPTION after {elapsed:.1f}s: {e}")
            self._write_status(step_name, 'EXCEPTION', elapsed=elapsed,
                               return_code=-3, detail=msg)
            terminate_process_tree(self.current_process, step_name)
            self.current_process = None
            return False, -3, ''.join(output_parts) + str(e), 'error'

    def restart_device_by_relay(self, next_step_desc):
        """
        降级脚本执行失败后，先通过继电器通断电重启设备，再执行下一轮降级脚本
        参数:
            next_step_desc: 即将执行的下一步描述 (仅用于日志)
        返回: (restarted: bool, cancelled: bool)
        """
        if not DOWNGRADE_RETRY_RELAY_RESTART:
            logging.info(f"ℹ️ 降级失败重试前继电器重启已禁用，直接执行 {next_step_desc}")
            return False, False
        if not self.relay:
            logging.warning(f"⚠️ 继电器未启用，无法通断电重启，直接执行 {next_step_desc}")
            return False, False
        if not self.running:
            return False, True

        msg = f"🔌 降级脚本执行失败，先通过继电器通断电重启设备，再执行 {next_step_desc}"
        logging.warning(msg)
        self._log(msg)
        self._write_status('继电器重启', 'RELAY_RESTART',
                           detail=f'降级失败，通过继电器通断电重启设备后执行 {next_step_desc}')

        ok, cancelled = self.relay.power_cycle()
        if cancelled or not self.running:
            return False, True
        if not ok:
            logging.warning("⚠️ 继电器通断电未完全成功，仍继续执行后续降级流程")

        if DOWNGRADE_RETRY_BOOT_WAIT > 0:
            logging.info(f"⏳ 等待 {DOWNGRADE_RETRY_BOOT_WAIT}s 让设备重新启动...")
            self._log(f"继电器重启后等待 {DOWNGRADE_RETRY_BOOT_WAIT}s")
            if sleep_interruptible(self.stop_event, DOWNGRADE_RETRY_BOOT_WAIT):
                return False, True

        logging.info(f"✅ 设备已通过继电器重启，开始 {next_step_desc}")
        self._log(f"设备已通过继电器重启，开始 {next_step_desc}")
        return True, False

    def execute_power_cycle_downgrade(self):
        """通过继电器通断电触发降级。"""
        if not self.running:
            return False, "用户停止", True
        if not self.relay:
            msg = "断电触发降级失败: 继电器未启用"
            logging.error(f"🔴 {msg}")
            self._log(msg)
            return False, msg, False

        rounds = max(1, int(DOWNGRADE_MAX_ROUNDS))
        for attempt in range(1, rounds + 1):
            if not self.running:
                return False, "用户停止", True
            logging.info(f"🔌 断电触发降级 (第 {attempt}/{rounds} 次)...")
            self._log(f"POWER-CYCLE attempt {attempt}/{rounds}")
            self._write_status(
                f"断电触发-{attempt}", 'RUNNING',
                detail='通过继电器通断电触发设备降级')
            ok, cancelled = self.relay.power_cycle()
            if cancelled or not self.running:
                return False, "用户停止", True
            if ok:
                wait = max(0, int(DOWNGRADE_TRIGGER_BOOT_WAIT))
                if wait:
                    logging.info(f"⏳ 通断电完成，等待 {wait}s...")
                    if sleep_interruptible(self.stop_event, wait):
                        return False, "用户停止", True
                msg = "断电触发降级完成"
                logging.info(f"🟢 {msg}")
                self._log(msg)
                self._write_status(f"断电触发-{attempt}", 'SUCCESS', detail=msg)
                return True, msg, False

            if attempt >= rounds or not DOWNGRADE_RETRY_RELAY_RESTART:
                break
            if sleep_interruptible(self.stop_event, DOWNGRADE_RETRY_BOOT_WAIT):
                return False, "用户停止", True

        msg = f"继电器通断电失败 (共尝试 {rounds} 次)"
        logging.error(f"🔴 {msg}")
        self._log(msg)
        self._write_status('断电触发', 'FAILED', detail=msg)
        return False, msg, False

    def execute_command_downgrade(self):
        """通过 ADB / SSH / 串口发送降级命令。"""
        commands = self._downgrade_commands()
        if not commands:
            msg = "命令触发降级失败: 未配置降级命令"
            logging.error(f"🔴 {msg}")
            self._log(msg)
            return False, msg, False

        rounds = max(1, int(DOWNGRADE_MAX_ROUNDS))
        last_reason = ''
        for attempt in range(1, rounds + 1):
            if not self.running:
                return False, "用户停止", True
            channel = (DOWNGRADE_TRIGGER_CHANNEL or 'adb').strip().upper()
            logging.info(
                f"🔧 通过 {channel} 执行 {len(commands)} 条降级命令 "
                f"(第 {attempt}/{rounds} 次)")
            for index, cmd in enumerate(commands, 1):
                logging.info(f"   命令{index}: {cmd}")
            self._log(
                f"COMMAND [{channel}] attempt {attempt}/{rounds}: "
                + " && ".join(commands))
            self._write_status(
                f"命令触发-{attempt}", 'RUNNING',
                detail=f'{channel}: ' + " && ".join(commands))
            ok, reason, cancelled = self._run_downgrade_commands(commands)
            if cancelled or not self.running:
                return False, "用户停止", True
            if ok:
                wait = max(0, int(DOWNGRADE_TRIGGER_BOOT_WAIT))
                if wait:
                    logging.info(f"⏳ 降级命令已发送，等待 {wait}s...")
                    if sleep_interruptible(self.stop_event, wait):
                        return False, "用户停止", True
                reason = reason or "命令触发降级完成"
                logging.info(f"🟢 {reason}")
                self._log("COMMAND trigger completed")
                self._write_status(f"命令触发-{attempt}", 'SUCCESS', detail=reason)
                return True, reason, False

            last_reason = reason or "命令执行失败"
            logging.error(f"🔴 降级命令执行失败: {last_reason}")
            self._log(f"COMMAND failed: {last_reason}")
            self._write_status(f"命令触发-{attempt}", 'FAILED', detail=last_reason)
            if attempt >= rounds:
                break
            if DOWNGRADE_RETRY_RELAY_RESTART:
                restarted, retry_cancelled = self.restart_device_by_relay(
                    "下一轮降级命令")
                if retry_cancelled or not self.running:
                    return False, "用户停止", True
                if not restarted:
                    logging.warning("ℹ️ 设备未重启，继续尝试下一轮降级命令")
            elif sleep_interruptible(self.stop_event, DOWNGRADE_RETRY_BOOT_WAIT):
                return False, "用户停止", True

        return False, last_reason or "命令触发降级失败", False

    def _downgrade_commands(self):
        """读取多行命令配置，去掉空行和首尾空白。"""
        return [
            line.strip()
            for line in (DOWNGRADE_TRIGGER_CMD or '').splitlines()
            if line.strip()
        ]

    def _run_downgrade_command(self, cmd):
        """执行命令触发；返回 (ok, reason, cancelled)。"""
        channel = (DOWNGRADE_TRIGGER_CHANNEL or 'adb').strip().lower()
        if channel == 'adb':
            return self._run_adb_downgrade_command(cmd)
        if channel == 'serial':
            monitor = self.owner.serial_monitor if self.owner else None
            if not monitor:
                return False, "设备串口监控不可用", False
            if not monitor.send_command(cmd):
                return False, "串口命令发送失败", False
            matched, line, cancelled = monitor.wait_for_pattern(
                DOWNGRADE_TRIGGER_EXPECT or '',
                timeout=max(1, int(DOWNGRADE_TRIGGER_TIMEOUT)),
                stop_check=self.running)
            if cancelled or not self.running:
                return False, "用户停止", True
            if DOWNGRADE_TRIGGER_EXPECT and not matched:
                return False, f"串口输出中未找到成功关键词: {line}", False
            return True, line or "串口命令发送成功", False
        if channel == 'ssh':
            return self._run_ssh_downgrade_command(cmd)
        return False, f"不支持的命令通道: {channel}", False

    def _run_downgrade_commands(self, commands):
        """顺序执行多行命令；ADB/SSH 输出合并后检查成功关键词。"""
        channel = (DOWNGRADE_TRIGGER_CHANNEL or 'adb').strip().lower()
        outputs = []
        total = len(commands)
        for index, cmd in enumerate(commands, 1):
            if not self.running:
                return False, "用户停止", True
            if channel == 'adb':
                shared = getattr(self.owner, 'shared_session', None)
                if shared and shared.channel == 'adb':
                    result = shared.run_command(
                        cmd, timeout=max(1, int(DOWNGRADE_TRIGGER_TIMEOUT)),
                        check=False)
                    ok = bool(result.get('ok'))
                    reason = (result.get('output') or
                              result.get('error') or "ADB 命令执行成功")
                    cancelled = self.stop_event.is_set()
                else:
                    ok, reason, cancelled = self._run_adb_downgrade_command(
                        cmd, check_expect=False)
            elif channel == 'ssh':
                shared = getattr(self.owner, 'shared_session', None)
                if shared and shared.channel == 'ssh':
                    result = shared.run_command(
                        cmd, timeout=max(1, int(DOWNGRADE_TRIGGER_TIMEOUT)),
                        check=False)
                    ok = bool(result.get('ok'))
                    reason = (result.get('output') or
                              result.get('error') or "SSH 命令执行成功")
                    cancelled = self.stop_event.is_set()
                else:
                    ok, reason, cancelled = self._run_ssh_downgrade_command(
                        cmd, check_expect=False)
            elif channel == 'serial':
                monitor = self.owner.serial_monitor if self.owner else None
                if not monitor:
                    return False, "设备串口监控不可用", False
                if not monitor.send_command(cmd):
                    return False, f"第 {index} 条串口命令发送失败: {cmd}", False
                if index < total and sleep_interruptible(self.stop_event, 0.1):
                    return False, "用户停止", True
                ok, reason, cancelled = True, f"命令已发送: {cmd}", False
            else:
                return False, f"不支持的命令通道: {channel}", False

            if cancelled or not self.running:
                return False, "用户停止", True
            if not ok:
                return False, f"第 {index} 条命令执行失败: {reason}", False
            if reason:
                outputs.append(reason)

        text = '\n'.join(part for part in outputs if part).strip()
        expect = (DOWNGRADE_TRIGGER_EXPECT or '').strip()
        if channel == 'serial':
            monitor = self.owner.serial_monitor if self.owner else None
            matched, line, cancelled = (monitor.wait_for_pattern(
                expect, timeout=max(1, int(DOWNGRADE_TRIGGER_TIMEOUT)),
                stop_check=self.running) if monitor else (False, '', False))
            if cancelled or not self.running:
                return False, "用户停止", True
            if expect and not matched:
                return False, f"串口输出中未找到成功关键词: {line}", False
            if matched:
                outputs.append(line)
        elif expect and expect not in text:
            channel_name = 'ADB' if channel == 'adb' else 'SSH'
            return False, (
                f"{channel_name}输出中未找到成功关键词: {text}")

        if outputs:
            return True, '\n'.join(part for part in outputs if part), False
        return True, f"共执行 {total} 条命令成功", False

    def _run_adb_downgrade_command(self, cmd, check_expect=True):
        """执行 ADB 降级命令，避免复用结果校验的 ADB 配置。"""
        adb = (DOWNGRADE_ADB_PATH or 'adb').strip()
        args = [adb]
        if (DOWNGRADE_ADB_SERIAL or '').strip():
            args += ['-s', DOWNGRADE_ADB_SERIAL.strip()]
        args += ['shell'] + (cmd.split() if cmd.strip() else ['echo', ''])
        result = run_cancellable_process(
            args,
            stop_event=self.stop_event,
            timeout=max(1, int(DOWNGRADE_TRIGGER_TIMEOUT)),
            label="ADB 降级命令",
        )
        output = (result.output or '').strip()
        if result.cancelled or not self.running:
            return False, "用户停止", True
        if result.timed_out:
            return False, f"ADB 命令执行超时 ({DOWNGRADE_TRIGGER_TIMEOUT}s)", False
        if result.reason == 'error':
            return False, result.error or output or f"无法启动 adb: {adb}", False
        if check_expect and result.success and DOWNGRADE_TRIGGER_EXPECT and (
                DOWNGRADE_TRIGGER_EXPECT not in output):
            return False, f"ADB 输出中未找到成功关键词: {output}", False
        return result.success, output or "ADB 命令执行成功", False

    def _run_ssh_downgrade_command(self, cmd, check_expect=True):
        """使用 paramiko 执行 SSH 降级命令。"""
        try:
            import paramiko
        except ImportError:
            return False, "缺少 SSH 依赖 paramiko，请先安装", False

        host = (DOWNGRADE_SSH_HOST or '').strip()
        username = (DOWNGRADE_SSH_USERNAME or '').strip()
        key_path = (DOWNGRADE_SSH_KEY_PATH or '').strip()
        if not host:
            return False, "SSH 主机未配置", False
        if not username:
            return False, "SSH 用户名未配置", False
        if not (key_path or DOWNGRADE_SSH_PASSWORD):
            return False, "SSH 密码或私钥至少需要配置一项", False

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            timeout = max(1, int(DOWNGRADE_TRIGGER_TIMEOUT))
            client.connect(
                hostname=host,
                port=int(DOWNGRADE_SSH_PORT or 22),
                username=username,
                password=DOWNGRADE_SSH_PASSWORD or None,
                key_filename=key_path or None,
                timeout=timeout,
                banner_timeout=timeout,
                auth_timeout=timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            channel = client.get_transport().open_session(timeout=timeout)
            channel.settimeout(0.2)
            channel.exec_command(cmd)
            output_parts = []
            start = time.monotonic()
            while self.running and not channel.exit_status_ready():
                while channel.recv_ready():
                    output_parts.append(channel.recv(4096).decode(
                        'utf-8', errors='replace'))
                if time.monotonic() - start >= timeout:
                    channel.close()
                    return False, f"SSH 命令执行超时 ({timeout}s)", False
                if sleep_interruptible(self.stop_event, 0.1):
                    channel.close()
                    return False, "用户停止", True

            while channel.recv_ready():
                output_parts.append(channel.recv(4096).decode(
                    'utf-8', errors='replace'))
            status = channel.recv_exit_status()
            output = ''.join(output_parts).strip()
            if status != 0:
                return False, output or f"SSH 命令返回码: {status}", False
            if check_expect and DOWNGRADE_TRIGGER_EXPECT and (
                    DOWNGRADE_TRIGGER_EXPECT not in output):
                return False, f"SSH 输出中未找到成功关键词: {output}", False
            return True, output or "SSH 命令执行成功", False
        except Exception as ex:
            return False, f"SSH 连接/执行失败: {ex}", False
        finally:
            try:
                client.close()
            except Exception:
                pass

    def execute_downgrade(self):
        """按当前模式分发降级流程。"""
        mode = (DOWNGRADE_MODE or 'jflash').strip().lower()
        if mode == 'power_cycle':
            return self.execute_power_cycle_downgrade()
        if mode == 'command':
            return self.execute_command_downgrade()
        return self.execute_jflash_downgrade()

    def execute_jflash_downgrade(self):
        """
        按 DOWNGRADE_SCRIPTS 的顺序依次执行降级脚本（数量不限）。

        每轮流程：
            逐条执行脚本 → 每条脚本最多尝试 DOWNGRADE_SCRIPT_RETRIES 次
            → 某条脚本用尽次数仍失败则本轮中止
        本轮中止后：先通过继电器通断电重启设备，再进入下一轮
        最多 DOWNGRADE_MAX_ROUNDS 轮；最后一轮失败后再做一次完整重试。
        返回: (success: bool, details: str, cancelled: bool)
        """
        scripts = [dict(s) for s in DOWNGRADE_SCRIPTS]
        if not scripts:
            detail = "未配置任何降级脚本"
            logging.error(f"🔴 {detail}")
            self._log(detail)
            return False, detail, False
        if not self.running:
            return False, "用户停止", True

        total = len(scripts)
        logging.info("=" * 40)
        logging.info("🔧 开始执行降级流程...")
        for i, s in enumerate(scripts, 1):
            logging.info(f"   脚本{i}: {s.get('path', '')}")
        logging.info("=" * 40)
        self._log("=" * 40)
        self._log(f"开始降级流程 (共 {total} 个脚本)")

        rounds = max(1, int(DOWNGRADE_MAX_ROUNDS))
        retries = max(1, int(DOWNGRADE_SCRIPT_RETRIES))

        def run_script(script, step_name):
            """执行单条脚本，返回 (ok, code, reason)。"""
            ok, code, _output, reason = self.run_bat(
                script.get('path', ''), step_name,
                script.get('success_keyword', '') or '',
                script.get('fail_keyword', '') or ''
            )
            return ok, code, reason

        def run_sequence(prefix):
            """
            顺序执行全部脚本（每条一次，不重试）。
            返回 (ok, results, failed_index, code, cancelled)
            results 为已成功步骤的 code 列表
            """
            results = []
            for idx, script in enumerate(scripts):
                if not self.running:
                    return False, results, idx, -1, True
                ok, code, reason = run_script(
                    script, f"{prefix}-步骤{idx + 1}")
                if reason == 'cancelled' or not self.running:
                    return False, results, idx, code, True
                if not ok:
                    return False, results, idx, code, False
                results.append(code)
                if idx < total - 1:
                    if sleep_interruptible(self.stop_event, 2):
                        return False, results, idx, code, True
            return True, results, -1, -1, False

        for round_num in range(1, rounds + 1):
            logging.info(f"{'=' * 30} 降级第 {round_num} 轮 {'=' * 30}")
            self._log(f"降级第 {round_num} 轮开始")

            success = True
            results = []
            for idx, script in enumerate(scripts):
                if not self.running:
                    detail = "用户停止"
                    logging.warning(f"⚠️ 降级已停止: {detail}")
                    self._log(f"降级停止: {detail}")
                    return False, detail, True

                ok, code = False, -1
                for attempt in range(1, retries + 1):
                    ok, code, reason = run_script(
                        script, f"第{round_num}轮-步骤{idx + 1}-第{attempt}次")
                    if reason == 'cancelled' or not self.running:
                        detail = "用户停止"
                        logging.warning(f"⚠️ 降级已停止: {detail}")
                        self._log(f"降级停止: {detail}")
                        return False, detail, True
                    if ok:
                        break
                    if attempt < retries:
                        logging.warning(
                            f"⚠️ 第{round_num}轮 步骤{idx + 1} 第{attempt}次执行失败，等待 5 秒后重试...")
                        self._log(f"第{round_num}轮 步骤{idx + 1} 第{attempt}次失败，重试中...")
                        if sleep_interruptible(self.stop_event, 5):
                            return False, "用户停止", True

                if not ok:
                    detail = (f"第{round_num}轮 步骤{idx + 1} 连续 {retries} 次失败 "
                              f"(返回码: {code})")
                    logging.warning(f"⚠️ {detail}")
                    self._log(detail)
                    success = False
                    break

                results.append(code)
                if idx < total - 1:
                    if sleep_interruptible(self.stop_event, 2):
                        return False, "用户停止", True

            if success:
                detail = ", ".join(
                    f"步骤{i + 1} OK (码:{c})" for i, c in enumerate(results))
                detail = f"第{round_num}轮 {detail}"
                logging.info(f"🟢 降级流程全部完成：{detail}")
                self._log(f"降级成功: {detail}")
                return True, detail, False

            if round_num < rounds:
                logging.warning(
                    f"⚠️ 第{round_num}轮降级失败，从步骤1重新开始第 {round_num + 1} 轮")
                self._log(f"第{round_num}轮失败，从步骤1重新开始")
                _, cancelled = self.restart_device_by_relay(
                    f"第 {round_num + 1} 轮降级 (从步骤1重新开始)")
                if cancelled:
                    return False, "用户停止", True

        # 所有轮次均失败 → 最后一次完整重试（每条脚本各执行一次）
        logging.warning(f"⚠️ {rounds} 轮降级均失败，进行最后一次完整重试...")
        self._log(f"{rounds} 轮降级均失败，进行最后完整重试...")
        _, cancelled = self.restart_device_by_relay("最后一次完整重试 (步骤1)")
        if cancelled:
            return False, "用户停止", True

        ok, results, failed_idx, code, cancelled = run_sequence("最后重试")
        if cancelled:
            return False, "用户停止", True
        if not ok:
            detail = f"最后重试步骤{failed_idx + 1}仍失败 (返回码: {code})"
            logging.error(f"🔴 降级失败：{detail}")
            self._log(f"降级失败: {detail}")
            return False, detail, False

        detail = "最后重试 " + ", ".join(
            f"步骤{i + 1} OK (码:{c})" for i, c in enumerate(results))
        logging.info(f"🟢 降级流程全部完成：{detail}")
        self._log(f"降级成功: {detail}")
        return True, detail, False

    def close(self):
        """关闭降级日志"""
        if self.log_handle:
            try:
                end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.log_handle.write(f"\n{'=' * 60}\n")
                self.log_handle.write(f"降级操作日志记录结束: {end_time}\n")
                self.log_handle.write(f"{'=' * 60}\n")
                self.log_handle.close()
            except Exception:
                pass
            self.log_handle = None


class FotaTest:
    """
    FOTA 反复升级/降级测试控制器
    """

    def __init__(self, stop_event=None):
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.report_handle = None

        # 优先复用项目管理页启动的共享日志/命令会话，避免重复打开设备。
        shared = device_session.active_session()
        self.shared_session = shared['session'] if shared else None
        if self.shared_session:
            self.shared_session.version_rules = [
                (BOOT_VERSION, 'boot_version_detected'),
                (UPGRADE_VERSION, 'upgrade_version_detected'),
            ]
            self.shared_session.boot_keyword = BOOT_FLAG
            self.shared_session.boot_success_count = 1
            self.shared_session.flag_rules = [
                {'attr': 'upgrade_started', 'pattern': FOTA_UPGRADE_START_FLAG,
                 'time_attr': 'upgrade_started_time', 'label': 'FOTA 升级开始'},
                {'attr': 'upgrade_completed', 'pattern': FOTA_UPGRADE_COMPLETE_FLAG,
                 'label': 'FOTA 升级完成'},
                {'attr': 'otfdec_error_detected', 'pattern': OTFDEC_ERROR_FLAG,
                 'time_attr': 'otfdec_error_time', 'label': 'OTFDEC 错误'},
                {'attr': 'sn_missing_detected', 'pattern': SN_MISSING_LOG_FLAG,
                 'label': '设备 SN 未配置'},
            ]
            self.serial_monitor = self.shared_session
        else:
            self.serial_monitor = SerialMonitor(
                DUT_COM_PORT, DUT_BAUDRATE, SERIAL_LOG_FILE, self.stop_event)

        # 继电器控制器：RELAY_ENABLED=False 时不连接继电器串口，重启统一走串口 reboot
        self.relay = (
            RelayController(RELAY_COM_PORT, RELAY_BAUDRATE, self.stop_event)
            if RELAY_ENABLED else None
        )

        # 降级执行器（owner 让降级编排能跟随 self.running 提前退出）
        self.downgrade_executor = DowngradeExecutor(
            DOWNGRADE_LOG_FILE, self.relay, self.stop_event)
        self.downgrade_executor.owner = self

        # 统计
        self.total_cycles = 0
        self.success_count = 0
        self.fail_count = 0
        self.sn_write_attempted = False
        self.verify_ok = True
        self.verify_detail = ''

        # 注册中断信号 (GUI 模式下在子线程运行，signal 只能在主线程注册)
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self.stop_handler)
            signal.signal(signal.SIGTERM, self.stop_handler)

    @property
    def running(self):
        return not self.stop_event.is_set()

    @running.setter
    def running(self, value):
        if value:
            self.stop_event.clear()
        else:
            self.stop_event.set()

    def stop_handler(self, _sig, _frame):
        logging.warning("\n⚠️ 收到中断信号 (Ctrl+C)，正在安全停止测试...")
        self.running = False

    def init_report_file(self):
        """初始化报告文件"""
        try:
            self.report_handle = open(REPORT_FILE, 'a', encoding='utf-8')
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            separator = f"\n{'=' * 20} FOTA测试会话开始：{start_time} {'=' * 20}\n"
            self.report_handle.write(separator)
            self.report_handle.write(f"设备串口: {DUT_COM_PORT} @ {DUT_BAUDRATE}\n")
            self.report_handle.write(f"开机版本: {BOOT_VERSION}\n")
            self.report_handle.write(f"升级版本: {UPGRADE_VERSION}\n")
            self.report_handle.write(f"开机标志: {BOOT_FLAG}\n")
            mode_names = {
                'jflash': 'JFLASH烧录', 'power_cycle': '断电触发',
                'command': '命令触发'
            }
            mode = (DOWNGRADE_MODE or 'jflash').lower()
            self.report_handle.write(
                f"降级方式: {mode_names.get(mode, mode)}\n")
            if mode == 'jflash':
                self.report_handle.write(f"降级脚本共 {len(DOWNGRADE_SCRIPTS)} 个:\n")
                for i, script in enumerate(DOWNGRADE_SCRIPTS, 1):
                    self.report_handle.write(f"  脚本{i}: {script['path']}\n")
            elif mode == 'command':
                commands = [
                    line.strip() for line in (DOWNGRADE_TRIGGER_CMD or '').splitlines()
                    if line.strip()
                ]
                self.report_handle.write(
                    f"触发通道: {(DOWNGRADE_TRIGGER_CHANNEL or '').upper()}\n")
                self.report_handle.write(f"触发命令共 {len(commands)} 条:\n")
                for index, command in enumerate(commands, 1):
                    self.report_handle.write(f"  命令{index}: {command}\n")
            self.report_handle.write(f"最大循环: {MAX_CYCLES if MAX_CYCLES > 0 else '无限'}\n")
            self.report_handle.flush()
            logging.info(f"报告文件就绪：{REPORT_FILE}")
        except Exception as e:
            logging.error(f"❌ 无法创建报告文件：{e}")
            self.report_handle = None

    def write_result(self, cycle_num, status, boot_time=0, version='', downgrade_detail='',
                     verify_detail=''):
        """写入测试结果"""
        if self.report_handle:
            try:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                line = (
                    f"[{timestamp}] 第 {cycle_num:04d} 次 | "
                    f"结果：{status.upper():8s} | "
                    f"启动耗时：{boot_time:.1f}s | "
                    f"版本：{version}"
                )
                if downgrade_detail:
                    line += f" | 降级：{downgrade_detail}"
                if verify_detail:
                    line += f" | 校验：{verify_detail}"
                line += "\n"
                self.report_handle.write(line)
                self.report_handle.flush()
            except Exception as e:
                logging.error(f"写入报告失败：{e}")

    def wait_for_boot(self, timeout):
        """
        等待设备开机成功 (从串口检测 BOOT_FLAG)
        返回: (success: bool, boot_time: float, cancelled: bool)
        """
        logging.info(f"👀 等待设备开机 (超时: {timeout}s)...")
        logging.info(f"   开机标志: '{BOOT_FLAG}'")

        start_time = time.time()
        while time.time() - start_time < timeout:
            if not self.running:
                return False, 0.0, True

            boot_detected, boot_time = self.serial_monitor.get_boot_status()
            if boot_detected:
                return True, boot_time, False

            if sleep_interruptible(self.stop_event, 0.5):
                return False, 0.0, True

        elapsed = time.time() - start_time
        logging.error(f"❌ 开机超时：{elapsed:.1f}s 内未检测到开机标志 '{BOOT_FLAG}'")
        return False, elapsed, False

    def reboot_device(self, use_relay=False):
        """
        重启设备
        参数:
            use_relay: True 时优先通过继电器通断电重启；继电器不可用或重启失败时回退为串口 reboot
        返回: (ok: bool, cancelled: bool)
        """
        if use_relay and self.relay:
            logging.info("🔄 正在通过继电器通断电重启设备...")
            relay_ok, cancelled = self.relay.power_cycle()
            if cancelled:
                logging.info("[重启] 用户停止，取消后续串口 reboot")
                return False, True
            if relay_ok:
                return True, False
            logging.warning("⚠️ 继电器重启失败，回退为串口 reboot 重启设备")

        if not self.running:
            return False, True
        logging.info("🔄 正在重启设备...")
        ok = self.serial_monitor.send_command('reboot')
        if ok:
            logging.info("📤 reboot 命令已发送")
            if sleep_interruptible(self.stop_event, 5):
                return False, True
        else:
            logging.warning("⚠️ 串口 reboot 发送失败，设备未重启")
        return ok, False

    def configure_missing_sn(self):
        """检测到 dev_info 缺失时，通过 msh 写入一次 SN 配置并重启设备。"""
        if self.sn_write_attempted or not self.serial_monitor.get_sn_missing_status():
            return False, False

        self.sn_write_attempted = True
        logging.warning(
            f"⚠️ 检测到 dev_info_get 失败，立即执行一次: {MSH_POST_AUTH_CMD}"
        )
        if not MSH_AUTH_ENABLED:
            logging.error("❌ MSH 认证未启用，无法自动写入 SN")
            return False, False

        ok, _detail, cancelled = self.authenticate_msh(
            run_cmd=True, do_reboot=True)
        if cancelled:
            return False, True
        if ok:
            logging.info("✅ SN 写入命令已执行，已通过串口 reboot 触发 FOTA 检测")
            return True, False
        logging.error("❌ SN 写入失败，本轮不会重复执行 set_dev_info")
        return False, False

    def wait_for_boot_and_version(self, timeout):
        """
        FOTA 升级状态机 - 完整流程:
            开机 + 版本检测
            - 如果是开机版本(0.1.0-user) → FOTA 升级流程
            - 首次检测到升级版本(90.9.9) → 直接进入降级流程，
              不校验开机标志、升级开始或升级完成标志
        异常处理:
            - OTFDEC 错误: 通过继电器通断电重启后跳到等待升级版本状态
            - upStatus":7 先于 upStatus":3: 通过继电器通断电重启，回到初始状态
        返回: (boot_ok: bool, upgrade_ok: bool, boot_time: float,
               already_upgraded: bool, cancelled: bool)
        """
        logging.info(f"👀 FOTA 升级状态机启动 (超时: {timeout}s)...")
        logging.info(f"   开机标志: '{BOOT_FLAG}'")
        logging.info(f"   开机版本: '{BOOT_VERSION}'")
        logging.info(f"   升级版本: '{UPGRADE_VERSION}'")
        logging.info(f"   升级开始: '{FOTA_UPGRADE_START_FLAG}'")
        logging.info(f"   升级完成: '{FOTA_UPGRADE_COMPLETE_FLAG}'")

        # 状态定义
        STATE_WAIT_BOOT = 0          # 等待开机 + 版本检测
        STATE_WAIT_FOTA_START = 1    # 等待 upStatus":3
        STATE_WAIT_FOTA_DONE = 2     # 等待 upStatus":7

        state = STATE_WAIT_BOOT
        boot_ok = False
        upgrade_ok = False
        already_upgraded = False  # 标记设备已经是升级版本
        boot_time = 0.0
        retry_count = 0
        start_time = time.time()
        version_wait_start_time = start_time
        last_heartbeat = start_time  # 用于周期性状态输出

        state_names = {
            STATE_WAIT_BOOT: f'等待开机 + 版本检测',
            STATE_WAIT_FOTA_START: f'等待升级开始({FOTA_UPGRADE_START_FLAG})',
            STATE_WAIT_FOTA_DONE: f'等待升级完成({FOTA_UPGRADE_COMPLETE_FLAG})',
        }
        logging.info(f"📍 进入状态: {state_names[state]}")

        while self.running:
            # 检测到 upStatus":3 后必须等待 upStatus":7；不因通用超时而进入降级。
            if state != STATE_WAIT_FOTA_DONE and time.time() - start_time >= timeout:
                break

            if not self.running:
                return False, False, 0.0, False, True

            # 后台线程发现 SN 未配置后，在主线程独占串口完成写入并重启设备。
            sn_handled, cancelled = self.configure_missing_sn()
            if cancelled:
                return False, False, boot_time, already_upgraded, True
            if sn_handled:
                self.serial_monitor.reset_flags()
                state = STATE_WAIT_BOOT
                boot_ok = False
                boot_time = 0.0
                start_time = time.time()
                version_wait_start_time = start_time
                logging.info(f"📍 SN 写入后设备已重启，回到状态: {state_names[state]} 等待 FOTA 检测")
                continue

            # upStatus":3 可能早于开机标志或版本日志出现；一旦检测到，
            # 后续只能等待 upStatus":7，不能因版本超时重启或检测到 90.9.9 而降级。
            upgrade_started, _ = self.serial_monitor.get_upgrade_status()
            if state in (STATE_WAIT_BOOT, STATE_WAIT_FOTA_START) and upgrade_started:
                state = STATE_WAIT_FOTA_DONE
                boot_ok = True
                logging.info(
                    f"📍 已检测到 {FOTA_UPGRADE_START_FLAG}，"
                    f"进入状态: {state_names[state]}（跳过版本检测）"
                )

            # ---- 升级中: OTFDEC 错误 → 串口 reboot 后继续等待 upStatus":7 ----
            if (state == STATE_WAIT_FOTA_DONE
                    and self.serial_monitor.get_otfdec_error_during_upgrade()):
                logging.warning(
                    "⚠️ 升级等待过程中检测到 OTFDEC 错误，"
                    "通过继电器通断电重启设备后继续等待升级完成..."
                )
                self.serial_monitor.reset_flags()
                # 重启后仍沿用升级路径，仅等待 upStatus":7。
                state = STATE_WAIT_FOTA_DONE
                boot_ok = True  # 标记开机已完成
                boot_time = 0.0
                _reboot_ok, cancelled = self.reboot_device(use_relay=True)
                if cancelled:
                    return False, False, boot_time, already_upgraded, True
                start_time = time.time()
                logging.info(f"📍 重启后进入状态: {state_names[state]}，继续等待 upStatus\":7")
                continue

            # ---- 状态0: 等待开机 + 版本检测 ----
            if state == STATE_WAIT_BOOT:
                # 首次开机已是目标升级版本时，设备已经可以输出版本信息，
                # 直接执行降级，不再等待 BOOT_FLAG 或校验 FOTA 完成状态。
                if self.serial_monitor.get_upgrade_version_status():
                    logging.info(
                        f"✅ 首次开机检测到升级版本: '{UPGRADE_VERSION}' "
                        "→ 跳过升级成功校验，直接进入降级流程"
                    )
                    boot_ok = True
                    already_upgraded = True
                    break

                # 开机后持续未输出任一版本号时，重新上电以恢复设备启动流程。
                if (not self.serial_monitor.get_boot_version_status()
                        and time.time() - version_wait_start_time >= BOOT_VERSION_DETECT_TIMEOUT):
                    logging.warning(
                        f"⚠️ 开机 {BOOT_VERSION_DETECT_TIMEOUT}s 未检测到版本号，"
                        "通过继电器通断电重启设备..."
                    )
                    self.serial_monitor.reset_flags()
                    _reboot_ok, cancelled = self.reboot_device(use_relay=True)
                    if cancelled:
                        return False, False, boot_time, already_upgraded, True
                    state = STATE_WAIT_BOOT
                    boot_ok = False
                    boot_time = 0.0
                    start_time = time.time()
                    version_wait_start_time = start_time
                    logging.info(f"📍 回到状态: {state_names[state]} (等待重启后的版本信息)")
                    continue

                boot_detected, bt = self.serial_monitor.get_boot_status()
                if boot_detected and not boot_ok:
                    boot_ok = True
                    boot_time = bt
                    logging.info(f"✅ 开机成功 (耗时: {boot_time:.1f}s)")

                if boot_ok:
                    # 检查是否是开机版本 (0.1.0)
                    if self.serial_monitor.get_boot_version_status():
                        logging.info(f"✅ 检测到开机版本: '{BOOT_VERSION}' → 进入升级流程")
                        state = STATE_WAIT_FOTA_START
                        logging.info(f"📍 进入状态: {state_names[state]}")

            # ---- 状态1: 等待 upStatus":3 ----
            elif state == STATE_WAIT_FOTA_START:
                upgrade_started, upgrade_completed = self.serial_monitor.get_upgrade_status()

                if upgrade_started:
                    state = STATE_WAIT_FOTA_DONE
                    logging.info(f"📍 进入状态: {state_names[state]}")

                elif upgrade_completed:
                    # upStatus":7 先于 upStatus":3 → 升级未触发 → 继电器通断电重启
                    retry_count += 1
                    logging.warning(
                        f"⚠️ upStatus\":7 先于 upStatus\":3，升级未触发！"
                        f"通过继电器通断电重启设备 ({retry_count}/{FOTA_RETRY_MAX})"
                    )
                    if retry_count > FOTA_RETRY_MAX:
                        logging.error(f"❌ 重试已达上限 ({FOTA_RETRY_MAX})，放弃")
                        return boot_ok, False, boot_time, False, False

                    _reboot_ok, cancelled = self.reboot_device(use_relay=True)
                    if cancelled:
                        return False, False, boot_time, already_upgraded, True
                    self.serial_monitor.reset_flags()
                    state = STATE_WAIT_BOOT
                    boot_ok = False
                    boot_time = 0.0
                    start_time = time.time()
                    version_wait_start_time = start_time
                    logging.info(f"📍 回到状态: {state_names[state]} (重试第{retry_count}次)")

            # ---- 状态2: 等待 upStatus":7 ----
            elif state == STATE_WAIT_FOTA_DONE:
                _, upgrade_completed = self.serial_monitor.get_upgrade_status()
                if upgrade_completed:
                    upgrade_ok = True
                    logging.info("✅ FOTA 升级完成 (upStatus\":7)")
                    break

            # 周期性状态输出 (每30秒)
            now = time.time()
            if now - last_heartbeat >= 30:
                elapsed = now - start_time
                boot_detected, _ = self.serial_monitor.get_boot_status()
                logging.info(
                    f"⏳ 等待中... 已耗时: {elapsed:.0f}s | "
                    f"状态: {state_names[state]} | "
                    f"开机: {'✓' if boot_detected else '✗'} | "
                    f"开机版本: {'✓' if self.serial_monitor.get_boot_version_status() else '✗'} | "
                    f"升级版本: {'✓' if self.serial_monitor.get_upgrade_version_status() else '✗'}"
                )
                last_heartbeat = now

            if sleep_interruptible(self.stop_event, 0.5):
                return False, False, boot_time, already_upgraded, True

        if self.stop_event.is_set():
            return False, False, boot_time, already_upgraded, True
        elapsed = time.time() - start_time
        if not boot_ok:
            logging.error(f"❌ 未在 {elapsed:.1f}s 内检测到开机标志")
        elif not upgrade_ok:
            us, uc = self.serial_monitor.get_upgrade_status()
            uv = self.serial_monitor.get_upgrade_version_status()
            bv = self.serial_monitor.get_boot_version_status()
            logging.error(
                f"❌ 未在 {elapsed:.1f}s 内完成升级 "
                f"(开机版本:{bv}, 升级开始:{us}, 升级版本:{uv}, 升级完成:{uc})"
            )

        return boot_ok, upgrade_ok, boot_time, already_upgraded, False

    def authenticate_msh(self, run_cmd=True, do_reboot=True, before_reboot=None):
        """
        通过串口进行 msh AUTH SEED 挑战-应答认证
        参数:
            run_cmd:   是否执行 MSH_POST_AUTH_CMD 命令 (降级后需要, 升级后不需要)
            do_reboot: 是否等待 10s 后发送 reboot (降级后需要, 升级后不需要)
            before_reboot: 可选回调 verify_cb() -> (ok, detail)，在 reboot 之前执行；
                           用于结果校验（此时设备仍在运行，串口/ADB 都可用）
        流程:
            1. 发送回车触发 AUTH SEED
            2. 读取串口获取 AUTH SEED (十六进制字符串)
            3. 用 PEM 私钥生成 ECDSA P-256 原始 r||s 签名
            4. 将签名结果 (十六进制) 输入串口
            5. 等待 msh 就绪；若配置了成功标志则严格校验，否则仅检查失败回执
            6. (可选) 执行 MSH_POST_AUTH_CMD 命令
            7. (可选) 等待 10 秒后发送 reboot 重启设备
            返回: (success: bool, detail: str, cancelled: bool)
        """
        if not CRYPTO_AVAILABLE:
            logging.error("❌ 未安装 cryptography 库，无法执行 msh 认证")
            logging.error("   请运行: pip install cryptography")
            return False, "未安装 cryptography 库", False
        if self.stop_event.is_set():
            return False, "用户停止", True

        logging.info("🔐 开始 msh AUTH SEED 认证流程...")

        ser = getattr(self.serial_monitor, 'ser', None)
        if not ser or not ser.is_open:
            channel = getattr(self.serial_monitor, 'channel', 'serial')
            if channel != 'serial':
                msg = (f"当前日志通道为 {channel.upper()}，msh 认证需要串口，"
                       "请在项目中启用串口或关闭 MSH 认证")
            else:
                msg = "串口未打开"
            logging.error(f"❌ {msg}，无法进行 msh 认证")
            return False, msg, False

        try:
            # 串口始终由后台线程读取，认证流程从接收队列获取新的响应。
            self.serial_monitor.clear_received_lines()
            if sleep_interruptible(self.stop_event, 0.2):
                return False, "用户停止", True

            # 步骤1: 发送回车触发 AUTH SEED
            logging.info("📤 发送回车触发 AUTH SEED...")
            if not self.serial_monitor.write_bytes(b'\r\n'):
                cancelled = self.stop_event.is_set()
                return False, "发送回车失败", cancelled

            # 步骤2: 读取串口等待 AUTH SEED
            seed_hex = None
            start_time = time.time()
            logging.info(f"👀 等待 AUTH SEED 出现 (超时: {MSH_AUTH_TIMEOUT}s)...")

            while time.time() - start_time < MSH_AUTH_TIMEOUT and self.running:
                received = self.serial_monitor.get_received_line(timeout=0.1)
                if not received:
                    continue
                _, line = received
                match = re.search(MSH_AUTH_SEED_PATTERN, line)
                if match:
                    seed_hex = match.group(1)
                    logging.info(f"✅ 获取到 AUTH SEED: {seed_hex}")
                    break

            if not seed_hex:
                if self.stop_event.is_set():
                    return False, "用户停止", True
                logging.error(f"❌ 未在 {MSH_AUTH_TIMEOUT}s 内获取到 AUTH SEED")
                return False, f"{MSH_AUTH_TIMEOUT}s 内未获取到 AUTH SEED", False

            # 步骤3: 用私钥签名
            logging.info(f"🔑 使用私钥签名: {MSH_PEM_KEY_PATH}")
            try:
                with open(MSH_PEM_KEY_PATH, 'rb') as f:
                    private_key = serialization.load_pem_private_key(
                        f.read(), password=None
                    )

                seed_bytes = bytes.fromhex(seed_hex)

                # 设备要求 P-256 原始 r||s，而 cryptography 默认输出 ASN.1 DER。
                signature_der = private_key.sign(
                    seed_bytes,
                    ec.ECDSA(hashes.SHA256())
                )
                if private_key.curve.key_size != 256:
                    raise ValueError(
                        f"私钥曲线必须为 P-256，当前为 {private_key.curve.name} "
                        f"({private_key.curve.key_size} 位)"
                    )
                r, s = utils.decode_dss_signature(signature_der)
                signature_raw = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
                signature_hex = signature_raw.hex()
                logging.info(f"✅ 签名完成 (原始 r||s 长度: {len(signature_raw)} 字节)")
                logging.info(f"   签名结果: {signature_hex[:64]}{'...' if len(signature_hex) > 64 else ''}")
            except FileNotFoundError:
                logging.error(f"❌ 私钥文件不存在: {MSH_PEM_KEY_PATH}")
                return False, f"私钥文件不存在: {MSH_PEM_KEY_PATH}", False
            except Exception as e:
                logging.error(f"❌ 签名过程出错: {e}", exc_info=True)
                return False, f"签名过程出错: {e}", False

            # 步骤4: 发送签名结果
            logging.info(f"📤 发送签名结果到串口...")
            if not self.serial_monitor.write_bytes((signature_hex + '\r\n').encode('utf-8')):
                cancelled = self.stop_event.is_set()
                return False, "发送签名失败", cancelled

            # 步骤5: 等待 msh 就绪。设备无固定成功文本时，仅检查明确失败回执。
            auth_success = False
            start_time = time.time()
            auth_timeout = 10 if MSH_AUTH_SUCCESS_FLAG else MSH_AUTH_READY_WAIT

            if MSH_AUTH_SUCCESS_FLAG:
                logging.info(f"👀 等待认证成功标志 '{MSH_AUTH_SUCCESS_FLAG}' (超时: {auth_timeout}s)...")
            else:
                logging.info(
                    f"👀 设备无认证成功标志，等待 {auth_timeout}s 后进入 msh 命令输入流程..."
                )

            while time.time() - start_time < auth_timeout and self.running:
                received = self.serial_monitor.get_received_line(timeout=0.1)
                if not received:
                    continue
                _, line = received

                if any(flag.lower() in line.lower() for flag in MSH_AUTH_FAIL_FLAGS):
                    logging.error(f"❌ msh 认证失败回执: {line}")
                    return False, f"msh 认证失败回执: {line}", False

                if MSH_AUTH_SUCCESS_FLAG and MSH_AUTH_SUCCESS_FLAG in line:
                    auth_success = True
                    logging.info(f"✅ msh 认证成功！检测到标志: '{MSH_AUTH_SUCCESS_FLAG}'")
                    break

            if not self.running:
                return False, "用户停止", True

            if not MSH_AUTH_SUCCESS_FLAG:
                auth_success = True
                logging.info("✅ 未检测到认证失败回执，按无提示符模式进入 msh 命令输入流程")

            if not auth_success:
                logging.error(f"❌ msh 认证失败：未检测到成功标志 '{MSH_AUTH_SUCCESS_FLAG}'")
                return False, f"未检测到成功标志: {MSH_AUTH_SUCCESS_FLAG}", False

            # 步骤6: 执行配置命令 (仅降级后需要)
            if run_cmd and MSH_POST_AUTH_CMD:
                logging.info(f"📤 执行命令: {MSH_POST_AUTH_CMD}")
                if sleep_interruptible(self.stop_event, 0.5):
                    return False, "用户停止", True
                if not self.serial_monitor.write_bytes((MSH_POST_AUTH_CMD + '\r\n').encode('utf-8')):
                    cancelled = self.stop_event.is_set()
                    return False, "发送配置命令失败", cancelled

                # 响应由后台串口线程持续打印和保存。
                if sleep_interruptible(self.stop_event, 2):
                    return False, "用户停止", True

                logging.info(f"✅ 命令已执行: {MSH_POST_AUTH_CMD}")

                # 步骤6.5: 重启前的设备侧校验 (设备仍在运行)
                if before_reboot is not None:
                    try:
                        callback_result = before_reboot()
                        if len(callback_result) == 3:
                            ok, detail, callback_cancelled = callback_result
                        else:
                            ok, detail = callback_result
                            callback_cancelled = False
                        self.verify_ok = ok
                        self.verify_detail = detail
                        if callback_cancelled or self.stop_event.is_set():
                            return False, "用户停止", True
                    except Exception as ex:
                        logging.error(f"[校验] 执行异常: {ex}", exc_info=True)
                        self.verify_ok = False
                        self.verify_detail = f"校验异常: {ex}"

                # 步骤7: 等待 10 秒后发送 reboot 重启设备 (仅降级后需要)
                if do_reboot:
                    logging.info("⏳ 等待 10 秒后发送 reboot 重启设备...")
                    if sleep_interruptible(self.stop_event, 10):
                        return False, "用户停止", True

                    if self.running:
                        logging.info("📤 发送 reboot 命令...")
                        if not self.serial_monitor.write_bytes(b'reboot\r\n'):
                            cancelled = self.stop_event.is_set()
                            return False, "发送 reboot 失败", cancelled

                        # reboot 响应由后台串口线程持续打印和保存。
                        if sleep_interruptible(self.stop_event, 2):
                            return False, "用户停止", True

                        logging.info("✅ reboot 命令已发送，设备正在重启")
            else:
                logging.info("ℹ️ 跳过 set_dev_info 和 reboot (自动升级阶段不需要)")

            return True, "msh 认证流程完成", False

        except Exception as e:
            logging.error(f"💥 msh 认证流程异常: {e}", exc_info=True)
            return False, f"msh 认证流程异常: {e}", self.stop_event.is_set()

    # =============================
    # 结果校验 (串口命令 / ADB 命令)
    # =============================
    def run_adb(self, cmd, timeout=20):
        """
        通过 ADB 执行一条 shell 命令
        返回: (ok: bool, output: str, cancelled: bool)
        """
        if self.shared_session and self.shared_session.channel == 'adb':
            result = self.shared_session.run_command(cmd, timeout=timeout, check=True)
            if self.stop_event.is_set():
                return False, "用户停止", True
            return bool(result.get('ok')), (result.get('output') or
                                            result.get('error') or ''), False

        adb = (CHECK_ADB_PATH or 'adb').strip()
        args = [adb]
        if CHECK_ADB_SERIAL.strip():
            args += ['-s', CHECK_ADB_SERIAL.strip()]
        args += ['shell'] + (cmd.split() if cmd.strip() else ['echo', ''])

        result = run_cancellable_process(
            args,
            stop_event=self.stop_event,
            timeout=timeout,
            label="ADB 校验",
        )
        output = (result.output or '').strip()
        if result.cancelled:
            return False, "用户停止", True
        if result.timed_out:
            return False, f"命令执行超时 ({timeout}s)", False
        if result.reason == 'error':
            detail = result.error or output or f"无法启动 adb: {adb}"
            return False, detail, False
        return result.success, output, False

    def verify_result(self):
        """
        每轮升降级结束后的结果校验，按配置执行已启用的检查项。
        返回: (ok: bool, detail: str, cancelled: bool)
        """
        details = []
        all_ok = True
        if self.stop_event.is_set():
            return False, "用户停止", True

        # --- 串口命令校验 ---
        if CHECK_SERIAL_ENABLED:
            cmd = CHECK_SERIAL_CMD.strip()
            if not cmd:
                details.append("串口校验: 未配置命令")
                all_ok = False
            else:
                logging.info(f"🔎 [校验] 串口发送: {cmd}")
                self.serial_monitor.clear_received_lines()
                if not self.serial_monitor.send_command(cmd):
                    if self.stop_event.is_set():
                        return False, "用户停止", True
                    details.append("串口校验失败: 串口未打开")
                    all_ok = False
                elif not CHECK_SERIAL_EXPECT.strip():
                    details.append("串口校验通过 (未配置期望值)")
                else:
                    matched, line, cancelled = self.serial_monitor.wait_for_pattern(
                        CHECK_SERIAL_EXPECT, CHECK_SERIAL_TIMEOUT,
                        stop_check=lambda: self.running)
                    if cancelled:
                        return False, "用户停止", True
                    if matched:
                        logging.info(f"🔎 [校验] 串口匹配到: {line.strip()[:120]}")
                        details.append("串口校验通过")
                    else:
                        details.append(
                            f"串口校验失败: {CHECK_SERIAL_TIMEOUT}s 内未出现 "
                            f"'{CHECK_SERIAL_EXPECT}'")
                        all_ok = False

        # --- ADB 命令校验 ---
        if CHECK_ADB_ENABLED:
            cmd = CHECK_ADB_CMD.strip()
            if not cmd:
                details.append("ADB 校验: 未配置命令")
                all_ok = False
            else:
                logging.info(f"🔎 [校验] ADB 执行: {cmd}")
                ok, output, cancelled = self.run_adb(cmd, CHECK_ADB_TIMEOUT)
                if cancelled:
                    return False, "用户停止", True
                expect = CHECK_ADB_EXPECT.strip()
                if not ok:
                    tail = output.replace('\n', ' ')[-120:]
                    details.append(f"ADB 校验失败: {tail}")
                    all_ok = False
                elif expect and expect not in output:
                    tail = output.replace('\n', ' ')[-120:]
                    details.append(f"ADB 校验失败: 输出中未找到 '{expect}' ({tail})")
                    all_ok = False
                else:
                    logging.info(f"🔎 [校验] ADB 输出: {output[:120]}")
                    details.append("ADB 校验通过")

        if not details:
            return True, '', False

        detail = ' | '.join(details)
        if all_ok:
            logging.info(f"🟢 [校验] {detail}")
        else:
            logging.error(f"🔴 [校验] {detail}")
        return all_ok, detail, False

    def run_cycle(self, cycle_num):
        """执行一次 FOTA 升级/降级循环"""
        logging.info(f"\n{'#' * 25} 第 {cycle_num} 次循环 {'#' * 25}")

        status = "fail"
        boot_time = 0.0
        downgrade_detail = ''
        downgrade_ok = False

        try:
            # =============================
            # 阶段1: FOTA 升级 (自动，从平台检测)
            # =============================
            logging.info("📱 阶段1: 等待设备开机 + FOTA 自动升级...")
            logging.info(f"   开机版本: {BOOT_VERSION} → 升级版本: {UPGRADE_VERSION}")

            # 重置串口检测标志
            self.serial_monitor.reset_flags()
            self.sn_write_attempted = False

            boot_ok, upgrade_ok, boot_time, already_upgraded, cancelled = \
                self.wait_for_boot_and_version(FOTA_UPGRADE_TIMEOUT)
            if cancelled:
                logging.info("用户停止，本轮不记录结果")
                return False, True

            if already_upgraded:
                status = "already_upgraded"
                logging.info(
                    f"🟡 设备已是升级版本 ({UPGRADE_VERSION})，本轮未执行升级，"
                    "按非升级成功记录"
                )
            elif boot_ok and upgrade_ok:
                status = "pass"
                logging.info(f"🟢 FOTA 升级验证通过！启动耗时: {boot_time:.1f}s")
            elif boot_ok and not upgrade_ok:
                status = "upgrade_fail"
                logging.error(f"🔴 开机成功但 FOTA 升级未完成！")
            elif not boot_ok:
                status = "boot_fail"
                logging.error(f"🔴 开机失败！")

            # =============================
            # 阶段2: 按 JFLASH / 断电 / 命令触发方式降级
            # =============================
            can_downgrade = already_upgraded or upgrade_ok
            if self.running and can_downgrade:
                logging.info("")
                mode_names = {
                    'jflash': 'JFLASH', 'power_cycle': '断电触发',
                    'command': '命令触发'
                }
                downgrade_mode_name = mode_names.get(
                    (DOWNGRADE_MODE or 'jflash').lower(),
                    DOWNGRADE_MODE or 'JFLASH')
                logging.info(f"🔧 阶段2: 执行 {downgrade_mode_name} 降级...")

                if POST_DOWNGRADE_WAIT > 0:
                    logging.info(f"⏳ 等待 {POST_DOWNGRADE_WAIT}s 后开始降级...")
                    if sleep_interruptible(self.stop_event, POST_DOWNGRADE_WAIT):
                        return False, True

                if self.running:
                    try:
                        downgrade_ok, downgrade_detail, cancelled = (
                            self.downgrade_executor.execute_downgrade())
                    except Exception as e:
                        downgrade_ok = False
                        downgrade_detail = f"降级流程异常: {e}"
                        cancelled = self.stop_event.is_set()
                        logging.error(
                            f"🔴 {downgrade_mode_name}降级流程异常: {e} "
                            "(不影响本轮升级结果)",
                            exc_info=True,
                        )
                    if cancelled:
                        logging.info("用户停止，本轮不记录结果")
                        return False, True

                    if downgrade_ok:
                        logging.info(f"🟢 {downgrade_mode_name}降级执行成功")
                    else:
                        logging.error(
                            f"🔴 {downgrade_mode_name}降级执行失败: {downgrade_detail} "
                            "(不影响本轮升级结果)"
                        )
            elif self.running:
                logging.warning(
                    "⏳ 未检测到 upStatus\":7，升级未确认成功，"
                    "本轮跳过降级流程"
                )

            # =============================
            # 阶段3: 结果校验 (串口命令 / ADB 命令，均可选)
            # 必须在设备 reboot 之前执行：MSH 认证流程结束时会主动 reboot，
            # 若放到 reboot 之后，串口与 ADB 都处于断开状态会误判失败。
            # =============================
            verify_detail = ''
            check_needed = CHECK_SERIAL_ENABLED or CHECK_ADB_ENABLED
            self.verify_ok = True   # 本轮回调没被调用时（未启用校验）保持通过

            def _do_verify():
                logging.info("🔎 阶段3: 结果校验...")
                return self.verify_result()

            # =============================
            # 阶段4: msh 认证 + set_dev_info (降级后配置，为下一轮升级做准备)
            # =============================
            if self.running and MSH_AUTH_ENABLED and downgrade_ok:
                logging.info("")
                if self.sn_write_attempted:
                    logging.info("🔐 降级脚本已完成，重新执行 MSH 认证 + set_dev_info...")
                else:
                    logging.info("🔐 阶段4: MSH 认证 + set_dev_info (降级后配置)...")

                msh_ok, msh_detail, cancelled = self.authenticate_msh(
                    run_cmd=True, do_reboot=True,
                    before_reboot=_do_verify if check_needed else None)
                if cancelled:
                    logging.info("用户停止，本轮不记录结果")
                    return False, True
                if check_needed:
                    verify_detail = self.verify_detail
                if msh_ok:
                    logging.info("🟢 MSH 认证通过，set_dev_info 已执行，已通过串口 reboot 重启")
                else:
                    logging.error(
                        f"🔴 MSH 认证失败：{msh_detail} (不影响本轮升级结果)"
                    )

            # MSH 认证未执行（认证关闭 / 本轮没走降级）时，校验单独执行一次
            if check_needed and not (MSH_AUTH_ENABLED and downgrade_ok):
                logging.info("")
                ok, verify_detail, cancelled = _do_verify()
                if cancelled:
                    logging.info("用户停止，本轮不记录结果")
                    return False, True
                self.verify_ok = ok

            if check_needed and not self.verify_ok:
                logging.warning("🔴 结果校验失败，但本轮结果仅由 FOTA 升级阶段决定")

            # =============================
            # 记录结果
            # =============================
            if self.stop_event.is_set():
                logging.info("用户停止，本轮不记录结果")
                return False, True
            self.write_result(cycle_num, status, boot_time,
                              UPGRADE_VERSION if upgrade_ok else '', downgrade_detail,
                              verify_detail)

            # 更新统计
            self.total_cycles += 1
            if status == "pass":
                self.success_count += 1
            else:
                self.fail_count += 1

            logging.info(f"\n📊 累计统计: 总计 {self.total_cycles} 次 | "
                         f"成功 {self.success_count} 次 | "
                         f"失败 {self.fail_count} 次")
            return True, False

        except Exception as e:
            if self.stop_event.is_set():
                logging.info(f"用户停止导致第 {cycle_num} 次循环退出: {e}")
                return False, True
            logging.error(f"💥 第 {cycle_num} 次循环异常：{e}", exc_info=True)
            status = "error"
            self.write_result(cycle_num, status, boot_time, '', str(e))
            self.fail_count += 1
            return True, False

    def run(self):
        """主运行入口"""
        logging.info("=" * 70)
        logging.info("🚀 FOTA 反复升级/降级测试程序已启动")
        logging.info("=" * 70)
        logging.info(f"📡 设备串口: {DUT_COM_PORT} @ {DUT_BAUDRATE}")
        logging.info(f"🎯 开机版本: {BOOT_VERSION} → 升级版本: {UPGRADE_VERSION}")
        logging.info(f"🏁 开机标志: {BOOT_FLAG}")
        logging.info(f"⏱️ 开机超时: {BOOT_TIMEOUT}s")
        logging.info(f"🔄 FOTA升级: 开始={FOTA_UPGRADE_START_FLAG}, "
                     f"完成={FOTA_UPGRADE_COMPLETE_FLAG} (超时: {FOTA_UPGRADE_TIMEOUT}s)")
        logging.info(f"🔌 继电器控制: {'启用' if RELAY_ENABLED else '禁用，重启统一走串口 reboot'}")
        downgrade_mode = (DOWNGRADE_MODE or 'jflash').strip().lower()
        mode_names = {
            'jflash': 'JFLASH烧录', 'power_cycle': '断电触发',
            'command': '命令触发'
        }
        logging.info(f"🛠️ 降级方式: {mode_names.get(downgrade_mode, downgrade_mode)}")
        if downgrade_mode == 'jflash':
            logging.info(f"📂 降级脚本 ({len(DOWNGRADE_SCRIPTS)} 个):")
            for i, script in enumerate(DOWNGRADE_SCRIPTS, 1):
                logging.info(f"     {i}. {script['path']}")
            logging.info(f"⏱️ 降级超时: {DOWNGRADE_TIMEOUT}s / 每个脚本")
        elif downgrade_mode == 'command':
            commands = [
                line.strip() for line in (DOWNGRADE_TRIGGER_CMD or '').splitlines()
                if line.strip()
            ]
            logging.info(
                f"📡 触发通道: {(DOWNGRADE_TRIGGER_CHANNEL or '').upper()}, "
                f"命令共 {len(commands)} 条")
            for index, command in enumerate(commands, 1):
                logging.info(f"     命令{index}: {command}")
            logging.info(f"⏱️ 触发超时: {DOWNGRADE_TRIGGER_TIMEOUT}s")
        logging.info(f"🔁 最大循环: {MAX_CYCLES if MAX_CYCLES > 0 else '无限 (Ctrl+C 停止)'}")
        logging.info("-" * 70)
        logging.info(f"📂 日志目录: {_LOG_DIR}")
        logging.info(f"🔎 降级状态文件: {DOWNGRADE_STATUS_FILE}")
        logging.info("=" * 70)

        # 检查当前降级方式的关键配置
        bat_warnings = []
        if downgrade_mode == 'jflash':
            if not DOWNGRADE_SCRIPTS:
                bat_warnings.append("⚠️ 未配置任何降级脚本")
            for i, script in enumerate(DOWNGRADE_SCRIPTS, 1):
                if not os.path.exists(script.get('path', '')):
                    bat_warnings.append(
                        f"⚠️ 降级脚本{i}不存在: {script.get('path', '')}")
        elif downgrade_mode == 'power_cycle':
            if not RELAY_ENABLED:
                bat_warnings.append("⚠️ 断电触发降级需要启用继电器")
        elif downgrade_mode == 'command':
            if not [line.strip() for line in
                    (DOWNGRADE_TRIGGER_CMD or '').splitlines() if line.strip()]:
                bat_warnings.append("⚠️ 未配置命令触发降级的命令")
            if (DOWNGRADE_TRIGGER_CHANNEL or '').lower() == 'adb' and not (
                    DOWNGRADE_ADB_PATH or '').strip():
                bat_warnings.append("⚠️ ADB 路径未配置")
            if (DOWNGRADE_TRIGGER_CHANNEL or '').lower() == 'ssh':
                if not (DOWNGRADE_SSH_HOST or '').strip():
                    bat_warnings.append("⚠️ SSH 主机未配置")
                if not (DOWNGRADE_SSH_USERNAME or '').strip():
                    bat_warnings.append("⚠️ SSH 用户名未配置")
                if not ((DOWNGRADE_SSH_PASSWORD or '').strip() or
                        (DOWNGRADE_SSH_KEY_PATH or '').strip()):
                    bat_warnings.append("⚠️ SSH 密码或私钥未配置")
        if bat_warnings:
            for w in bat_warnings:
                logging.warning(w)
            logging.warning("⚠️ 降级配置不完整，降级步骤可能会失败，请检查配置！")
            logging.warning("")

        try:
            # 初始化
            self.init_report_file()
            self.downgrade_executor.open_log()

            # 共享日志会话已在测试启动前打开，这里只处理独立串口。
            if not self.shared_session:
                if not self.serial_monitor.open():
                    logging.error("无法打开串口，程序退出")
                    return
                self.serial_monitor.start()

            # 启动前通过继电器上电，确保设备处于可控的初始状态
            if self.relay:
                logging.info("🔌 通过继电器上电，确保设备处于上电状态...")
                power_ok, cancelled = self.relay.power_on()
                if cancelled:
                    logging.info("用户停止，取消启动前的上电等待")
                elif power_ok and RELAY_POWER_ON_WAIT > 0:
                    logging.info(
                        f"⏳ 等待 {RELAY_POWER_ON_WAIT}s 等待设备上电稳定...")
                    sleep_interruptible(self.stop_event, RELAY_POWER_ON_WAIT)
            else:
                logging.info("🔌 继电器未启用，跳过启动前继电器上电")

            logging.info("")
            logging.info("📋 测试流程说明:")
            logging.info("   阶段1 [升级]: 设备开机 → 自动检测FOTA升级")
            logging.info("      upStatus\":3 (开始) → upStatus\":7 (完成) → 版本确认")
            logging.info("      若 upStatus\":7 先于 upStatus\":3 → 继电器通断电重启重试")
            logging.info("      若 OTFDEC 错误 → 继电器通断电重启后继续等待")
            if downgrade_mode == 'jflash':
                logging.info("   阶段2 [降级]: JFLASH " + " → ".join(
                    f"脚本{i + 1}" for i in range(len(DOWNGRADE_SCRIPTS))) +
                    " (失败自动重试)")
                logging.info(
                    "      每轮降级失败 → 继电器通断电重启设备 → 执行下一轮降级")
            elif downgrade_mode == 'power_cycle':
                logging.info("   阶段2 [降级]: 继电器断电/上电触发设备降级")
            else:
                logging.info(
                    f"   阶段2 [降级]: {(DOWNGRADE_TRIGGER_CHANNEL or '').upper()} "
                    "命令触发设备降级")
            logging.info("   阶段3 [校验]: 串口命令校验 / ADB 命令校验 (均可在 GUI 中启用)")
            logging.info("      (在 reboot 之前执行，此时设备仍在运行)")
            logging.info("   阶段4 [配置]: msh 认证 → set_dev_info → 等待10s → reboot")
            logging.info("      (为下一轮 FOTA 升级做准备，自动升级阶段不需要 set_dev_info)")
            logging.info("")

            # 主循环
            cycle = 0
            while self.running:
                cycle += 1
                if MAX_CYCLES > 0 and cycle > MAX_CYCLES:
                    logging.info(f"\n✅ 已达到最大循环次数 ({MAX_CYCLES})，测试完成！")
                    break

                _recorded, cancelled = self.run_cycle(cycle)
                if cancelled:
                    logging.info("用户停止，退出测试主循环")
                    break
        except Exception as ex:
            logging.error(f"💥 FOTA 测试主流程异常：{ex}", exc_info=True)
        finally:
            logging.info("\n🛑 测试循环已终止")
            logging.info(f"📊 最终统计: 总计 {self.total_cycles} 次 | "
                         f"成功 {self.success_count} 次 | "
                         f"失败 {self.fail_count} 次")

            if not self.shared_session:
                try:
                    self.serial_monitor.stop()
                except Exception as ex:
                    logging.warning(f"关闭设备串口失败: {ex}")

            try:
                self.downgrade_executor.close()
            except Exception as ex:
                logging.warning(f"关闭降级日志失败: {ex}")

            if self.relay:
                try:
                    self.relay.power_off(force=True)
                except Exception as ex:
                    logging.warning(f"测试结束时继电器断电失败: {ex}")
                try:
                    self.relay.close()
                except Exception as ex:
                    logging.warning(f"关闭继电器失败: {ex}")

            if self.report_handle:
                try:
                    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.report_handle.write(
                        f"{'=' * 20} FOTA测试会话结束：{end_time} "
                        f"| 总计:{self.total_cycles} 成功:{self.success_count} "
                        f"失败:{self.fail_count} {'=' * 20}\n"
                    )
                    self.report_handle.close()
                    logging.info(f"📝 测试报告已保存：{REPORT_FILE}")
                except Exception as ex:
                    logging.warning(f"关闭测试报告失败: {ex}")
                finally:
                    self.report_handle = None


if __name__ == "__main__":
    # 依赖检查
    missing_libs = []
    try:
        import serial
    except ImportError:
        missing_libs.append("pyserial")

    try:
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        missing_libs.append("cryptography")

    if missing_libs:
        print(f"\n❌ 错误：缺少必要的库：{', '.join(missing_libs)}")
        print(f"💡 请运行以下命令安装：pip install {' '.join(missing_libs)}\n")
        sys.exit(1)

    # 初始化日志
    setup_logging()

    # 启动测试
    test = FotaTest()
    test.run()
