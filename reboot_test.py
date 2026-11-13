import serial
import time
import logging
import sys
import os
import signal
import threading
import random
import subprocess
from datetime import datetime
import queue

from process_utils import run_cancellable_process, sleep_interruptible, terminate_process_tree
import device_session

# ================= 用户配置区域 =================

# --- 开机检测配置 ---
BOOT_TIMEOUT = 120  # 最大等待启动时间 (秒)
MAX_CYCLES = 0  # 最大循环次数 (0=无限循环，>0=达到次数后自动停止)

# 被测设备串口 & 开机检测配置 (每个串口独立配置串口号和开机关键词检测)
DUT_SERIAL_PORT_COUNT = 1  # 使用的串口数量：0 / 1 / 2（0=不使用串口，1=只用串口1，2=同时用串口1和串口2）
DUT_BAUDRATE = 115200  # 波特率 (所有串口共用)
# -- 串口1 --
DUT_COM_PORT1 = 'COM79'                # 串口1端口号（仅当 DUT_SERIAL_PORT_COUNT>=1 时生效）
SERIAL_BOOT_DETECT_PORT1 = True        # 是否启用串口1的开机关键词检测
SERIAL_BOOT_KEYWORD_PORT1 = 'login:'   # 串口1开机检测关键词（不区分大小写）
SERIAL_BOOT_SUCCESS_COUNT_PORT1 = 1    # 串口1关键词出现次数达到此值即判定开机成功
SERIAL_BOOT_LOG_TO_MAIN_PORT1 = True   # 是否将串口1匹配到的日志行打印到主程序日志
# -- 串口2 (仅当 DUT_SERIAL_PORT_COUNT>=2 时生效) --
DUT_COM_PORT2 = 'COM36'                # 串口2端口号
SERIAL_BOOT_DETECT_PORT2 = False       # 是否启用串口2的开机关键词检测
SERIAL_BOOT_KEYWORD_PORT2 = 'login:'   # 串口2开机检测关键词（不区分大小写）
SERIAL_BOOT_SUCCESS_COUNT_PORT2 = 1    # 串口2关键词出现次数达到此值即判定开机成功
SERIAL_BOOT_LOG_TO_MAIN_PORT2 = True   # 是否将串口2匹配到的日志行打印到主程序日志

# 网口 Ping 检测 (从PC端直接ping设备，开机判定的主要依据)
PING_ENABLED = False  # 是否启用ping检测（设为False则跳过ping，仅依靠串口检测/ADB或等待超时判定开机）
PING_SOURCE = ''  # ping源地址（PC端网卡IP，留空则系统自动选择；多网卡时建议填写以确保走正确的网口）
PING_TARGET = '192.168.10.101'  # ping目的地址（设备网口IP地址，必填，请根据实际设备IP修改）
PING_PACKET_SIZE = 32  # ping包大小 (字节)，Windows默认32字节，可设为更大值测试大包连通性（如1472、1500）
PING_INTERVAL = 1  # ping间隔时间 (秒)
PING_TIMEOUT = 3  # 单次ping超时时间 (秒)
PING_SUCCESS_COUNT = 5  # 连续ping成功次数，达到此值判定设备开机成功

# ADB 检测 (可选功能，用于辅助诊断或开机判定)
ADB_ENABLED = False  # 是否启用ADB（设为False则完全跳过ADB相关操作）
ADB_PATH = 'adb'  # ADB可执行文件路径，如已加入环境变量直接填'adb'，否则填完整路径
ADB_DEVICE_SERIAL = ''  # ADB设备序列号 (多设备时填写，留空则自动选择唯一设备)
ADB_LOGIN_USERNAME = 'root'      # ADB shell 登录用户名 (留空则跳过登录)
ADB_LOGIN_PASSWORD = ''   # ADB shell 登录密码 (请在项目管理页填写，勿硬编码)
ADB_LOGIN_TIMEOUT = 15           # 登录交互超时时间 (秒)

# ADB Shell 内部 Ping (从设备侧ping目标IP，验证设备网络连通性)
ADB_PING_ENABLED = False         # 是否在ADB shell内执行ping检测
ADB_PING_TARGET = '192.168.10.1' # ADB shell内ping的目标IP
ADB_PING_COUNT = 3               # ADB shell内ping发包次数
ADB_PING_SUCCESS_COUNT = 2       # ping成功次数达到此值即判定网络连通

# --- 继电器硬件配置 ---
# 选项：'HID' (USB HID 继电器), 'SERIAL' (串口控制继电器), 'MOCK' (仅测试逻辑不控继电器)
RELAY_TYPE = 'SERIAL'
RELAY_COM_PORT = 'COM72'  # 如果继电器是串口控制的，填写此端口
RELAY_BAUDRATE = 115200  # 串口继电器波特率
RELAY_CHANNEL = 1  # 继电器通道号 (1/2/3/4)，不同通道对应不同的控制指令

# 各通道的开/关指令定义 (仅当 RELAY_TYPE='SERIAL' 时生效)
# 根据实际继电器协议修改对应通道的指令
RELAY_COMMANDS = {
    1: {'on': '68 09 00 FF 12 00 01 12 16', 'off': '68 09 00 FF 12 00 00 11 16'},
    2: {'on': '68 09 00 FF 12 01 01 13 16', 'off': '68 09 00 FF 12 01 00 12 16'},
    3: {'on': '68 09 00 FF 12 02 01 14 16', 'off': '68 09 00 FF 12 02 00 13 16'},
    4: {'on': '68 09 00 FF 12 03 01 15 16', 'off': '68 09 00 FF 12 03 00 14 16'},
}

# HID 继电器特定配置 (仅当 RELAY_TYPE='HID' 时有效)
RELAY_VID = 0x04d8  # 示例 VID，请根据实际硬件修改
RELAY_PID = 0x0dd3  # 示例 PID，请根据实际硬件修改
# 注意：不同品牌 HID 继电器的开/关指令不同，下方指令仅为示例
HID_ON_CMD = [0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
HID_OFF_CMD = [0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]

# --- 继电器时序配置 ---
POWER_ON_DELAY = 10  # 继电器上电后等待时间 (秒)，等设备开始启动再开串口和ping
MIN_POWER_ON_TIME = 30  # 继电器上电后最小保持时间 (秒) (包含启动时间)
POWER_ON_TIME_RANDOM = True  # 是否启用随机上电保持时间
POWER_ON_TIME_MIN = 30  # 随机上电保持最小时间 (秒)
POWER_ON_TIME_MAX = 120  # 随机上电保持最大时间 (秒)
RANDOM_MODE_BOOT_DETECT = False  # 随机上电模式下是否做开机检测 (True=在随机等待期间做开机检测，False=跳过开机检测)
POWER_OFF_WAIT_TIME = 10  # 继电器断电后等待时间 (秒) (让电容放电)
POWER_OFF_WAIT_RANDOM = False  # 是否启用随机断电等待时间
POWER_OFF_WAIT_MIN = 1  # 随机断电等待最小时间 (秒)
POWER_OFF_WAIT_MAX = 60  # 随机断电等待最大时间 (秒)

# --- 日志配置 ---
LOG_DIR = ""  # 日志存储目录（留空=程序运行目录，填写绝对路径可指定其他位置）
LOG_FILE = "power_cycle_debug.log"           # 主程序运行日志
SERIAL_LOG_FILE1 = "serial_output_port1.log"  # 串口1输出日志
SERIAL_LOG_FILE2 = "serial_output_port2.log"  # 串口2输出日志
RELAY_LOG_FILE = "relay_output.log"           # 继电器操作日志
ADB_LOG_FILE = "adb_output.log"              # ADB操作日志（仅当 ADB_ENABLED=True 时生成）
REPORT_FILE = "Power_Test_Report.txt"        # 测试报告


# ===============================================

def _app_dir():
    """
    程序运行目录（用于存放日志/报告/配置）：
    - 源码运行: 脚本所在目录
    - PyInstaller 打包后: exe 所在目录
      （打包后 __file__ 指向 _MEIPASS 临时解包目录，程序退出即删除，
        用它存日志会导致日志丢失）
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


_SCRIPT_DIR = _app_dir()


def _resolve_path(path):
    """将相对路径解析为相对于程序运行目录的绝对路径"""
    if os.path.isabs(path):
        return path
    return os.path.join(_SCRIPT_DIR, path)


class ColorFormatter(logging.Formatter):
    """控制台彩色日志格式化器"""
    COLORS = {
        'DEBUG': '\033[36m',     # 青色
        'INFO': '\033[37m',      # 白色
        'WARNING': '\033[33m',   # 黄色
        'ERROR': '\033[31m',     # 红色
        'CRITICAL': '\033[1;31m',  # 粗体红色
    }
    RESET = '\033[0m'

    def format(self, record):
        msg = super().format(record)
        color = self.COLORS.get(record.levelname, '')
        return f"{color}{msg}{self.RESET}" if color else msg


def setup_logging():
    """配置日志系统"""
    global LOG_FILE, SERIAL_LOG_FILE1, SERIAL_LOG_FILE2
    global RELAY_LOG_FILE, ADB_LOG_FILE, REPORT_FILE

    # 启用 Windows 控制台 ANSI 颜色支持
    if sys.platform == 'win32':
        os.system('')

    # 将相对路径解析为脚本目录下的绝对路径
    LOG_FILE = _resolve_path(LOG_FILE)
    SERIAL_LOG_FILE1 = _resolve_path(SERIAL_LOG_FILE1)
    SERIAL_LOG_FILE2 = _resolve_path(SERIAL_LOG_FILE2)
    RELAY_LOG_FILE = _resolve_path(RELAY_LOG_FILE)
    ADB_LOG_FILE = _resolve_path(ADB_LOG_FILE)
    REPORT_FILE = _resolve_path(REPORT_FILE)

    plain_formatter = logging.Formatter('%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    color_formatter = ColorFormatter('%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s', datefmt='%H:%M:%S')

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(color_formatter)

    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(plain_formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers = []
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)


def build_adb_cmd(base_cmd):
    """构建ADB命令，自动添加设备序列号"""
    cmd = [ADB_PATH]
    if ADB_DEVICE_SERIAL:
        cmd.extend(['-s', ADB_DEVICE_SERIAL])
    cmd.extend(base_cmd)
    return cmd


class ADBShellSession:
    """
    通过CMD窗口打开一个持久化的adb shell会话，
    自动登录并在同一shell里执行ping/ifconfig等命令
    """

    def __init__(self, adb_log_handle=None, stop_event=None):
        self.proc = None
        self.reader_thread = None
        self.output_queue = queue.Queue()
        self.log_buffer = []
        self.adb_log_handle = adb_log_handle
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self._running = False
        self._lock = threading.Lock()
        self._cmd_counter = 0

    def _cancelled(self):
        return self.stop_event.is_set()

    def start(self, timeout=30):
        """启动adb shell进程并等待登录完成"""
        if self.proc:
            return True, "会话已存在", self._cancelled()
        if self._cancelled():
            return False, "用户中断", True

        cmd = build_adb_cmd(['shell'])
        cmd_str = ' '.join(cmd)
        self._log_adb(f"启动ADB Shell会话: {cmd_str}")
        logging.info(f"[ADB] 启动Shell会话: {cmd_str}")

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except Exception as e:
            msg = f"无法启动adb shell: {e}"
            self._log_adb(f"启动失败: {msg}")
            logging.error(f"[ADB] {msg}")
            return False, msg, self._cancelled()

        self._running = True
        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.reader_thread.start()

        # 等待初始提示符，然后登录
        init_output = self._wait_for_prompt(timeout=timeout)
        self._log_adb(f"初始输出:\n{init_output}")
        logging.info(f"[ADB] Shell初始输出: {init_output[:200]}")

        if self._cancelled():
            self.close()
            return False, "用户中断", True

        # 登录
        if ADB_LOGIN_USERNAME:
            return self._login()
        return True, "无需登录", False

    def _login(self):
        """在shell中发送用户名和密码"""
        if self._cancelled():
            return False, "用户中断", True
        logging.info(f"[ADB] 发送登录用户名: {ADB_LOGIN_USERNAME}")
        self._log_adb(f"发送用户名: {ADB_LOGIN_USERNAME}")

        self._send(ADB_LOGIN_USERNAME + '\n')
        if sleep_interruptible(self.stop_event, 1):
            return False, "用户中断", True

        pwd_prompt = self._drain(2.0)
        if self._cancelled():
            return False, "用户中断", True
        self._log_adb(f"密码提示: {pwd_prompt}")
        logging.info(f"[ADB] 密码提示: {pwd_prompt}")

        logging.info(f"[ADB] 发送密码: {'*' * len(ADB_LOGIN_PASSWORD)}")
        self._log_adb(f"发送密码: {'*' * len(ADB_LOGIN_PASSWORD)}")
        self._send(ADB_LOGIN_PASSWORD + '\n')
        if sleep_interruptible(self.stop_event, 1):
            return False, "用户中断", True

        login_result = self._drain(2.0)
        if self._cancelled():
            return False, "用户中断", True
        self._log_adb(f"登录结果: {login_result}")
        logging.info(f"[ADB] 登录响应: {login_result}")

        if 'incorrect' in login_result.lower() or 'denied' in login_result.lower():
            return False, "登录失败：用户名或密码错误", False

        return True, "登录成功", False

    def run_command(self, cmd, timeout=10):
        """在shell中执行命令并捕获输出"""
        if self._cancelled():
            return False, "用户中断", True
        if not self.proc or self.proc.poll() is not None:
            return False, "Shell会话未运行", False

        self._cmd_counter += 1
        marker = f'__PY_MARK_{self._cmd_counter}__'
        end_marker = f'__PY_END_{self._cmd_counter}__'

        self._send(f'echo {marker}\n')
        if sleep_interruptible(self.stop_event, 0.1):
            return False, "用户中断", True
        self._send(cmd + '\n')
        if sleep_interruptible(self.stop_event, 0.1):
            return False, "用户中断", True
        self._send(f'echo {end_marker}\n')

        output = self._wait_for_marker(end_marker, timeout=timeout)
        if self._cancelled():
            return False, output, True

        # 去掉首尾的echo标记行
        lines = output.split('\n')
        clean_lines = [l for l in lines if marker not in l and end_marker not in l]
        clean_output = '\n'.join(clean_lines).strip()

        # 记录日志
        self._log_adb(f"[CMD #{self._cmd_counter}] {cmd}\n[OUTPUT]\n{clean_output}")
        logging.info(f"   CMD: {cmd}")
        logging.info(f"   OUTPUT:\n{clean_output}")

        return True, clean_output, False

    def close(self):
        """关闭shell会话"""
        self._running = False
        if self.proc:
            terminate_process_tree(self.proc, "ADB Shell")
            self.proc = None
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=2)
        logging.info("[ADB] Shell会话已关闭")

    # ── 内部方法 ──

    def _send(self, data):
        """向shell发送数据"""
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write(data.encode('utf-8'))
                self.proc.stdin.flush()
            except Exception as e:
                logging.warning(f"发送数据到shell失败: {e}")

    def _read_loop(self):
        """后台读取shell输出"""
        while self._running:
            try:
                byte = self.proc.stdout.read(1)
                if byte:
                    self.output_queue.put(byte)
                    with self._lock:
                        self.log_buffer.append(byte)
                        if len(self.log_buffer) > 10000:
                            self.log_buffer = self.log_buffer[-5000:]
                else:
                    break
            except Exception:
                break

    def _drain_queue(self):
        """排空队列，返回累积的字节"""
        data = b''
        while True:
            try:
                data += self.output_queue.get_nowait()
            except queue.Empty:
                break
        return data

    def _drain(self, wait_time):
        """排空队列并等待一段时间，返回解码后的文本"""
        sleep_interruptible(self.stop_event, wait_time)
        data = self._drain_queue()
        return data.decode('utf-8', errors='ignore').strip()

    def _wait_for_prompt(self, timeout=15):
        """等待shell提示符出现"""
        buf = b''
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cancelled():
                break
            try:
                byte = self.output_queue.get(timeout=0.2)
                buf += byte
                decoded = buf.decode('utf-8', errors='ignore')
                if (decoded.endswith('# ') or decoded.endswith('$ ') or
                        decoded.endswith(': ') or 'login:' in decoded.lower()):
                    return decoded.strip()
            except queue.Empty:
                pass
        return buf.decode('utf-8', errors='ignore').strip()

    def _wait_for_marker(self, marker, timeout=10):
        """等待特定标记出现，返回累积输出"""
        buf = b''
        deadline = time.time() + timeout
        marker_bytes = marker.encode('utf-8')
        while time.time() < deadline:
            if self._cancelled():
                break
            try:
                byte = self.output_queue.get(timeout=0.2)
                buf += byte
                if marker_bytes in buf:
                    return buf.decode('utf-8', errors='ignore')
            except queue.Empty:
                pass
        return buf.decode('utf-8', errors='ignore')

    def _log_adb(self, msg):
        """写入ADB日志文件"""
        if self.adb_log_handle:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self.adb_log_handle.write(f"[{timestamp}] {msg}\n")
            self.adb_log_handle.flush()


def adb_check_connected(stop_event=None):
    """检查ADB设备是否已连接"""
    result = run_cancellable_process(
        build_adb_cmd(['get-state']),
        stop_event=stop_event,
        timeout=5,
        label='ADB连接检查',
    )
    if result.cancelled:
        return False, result.output.strip(), True
    if result.reason == 'error':
        return False, result.error, False
    output = result.output.strip()
    return result.success and output == 'device', output, False


def adb_log_session_event(adb_log_handle, event_msg):
    """记录ADB会话级别事件（连接、断开等）"""
    if adb_log_handle:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        adb_log_handle.write(f"[{timestamp}] {event_msg}\n")
        adb_log_handle.flush()


class SerialLogger:
    """串口日志记录器，支持在后台线程中持续读取串口数据并写入日志文件"""

    def __init__(self, port_name, baudrate, log_file_path, label="",
                 boot_keyword="", boot_success_count=1, boot_log_to_main=True):
        self.port_name = port_name
        self.baudrate = baudrate
        self.log_file_path = log_file_path
        self.label = label or port_name
        self.ser = None
        self.log_handle = None
        self.thread = None
        self._running = False
        # 开机检测相关
        self.boot_keyword = boot_keyword
        self.boot_success_count = boot_success_count
        self.boot_log_to_main = boot_log_to_main
        self._boot_detect_count = 0
        self._boot_detected = False
        self.read_bytes = 0  # 累计从串口收到的字节数（超时诊断用：区分"串口无数据"和"关键词不匹配"）

    def open(self):
        """打开串口和日志文件"""
        try:
            self.ser = serial.Serial(self.port_name, self.baudrate, timeout=0.5)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            logging.info(f"[{self.label}] 串口已打开: {self.port_name} @ {self.baudrate}")
        except Exception as e:
            logging.error(f"[{self.label}] 打开串口 {self.port_name} 失败: {e}")
            self.ser = None
            return False

        try:
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log_handle = open(self.log_file_path, 'a', encoding='utf-8')
            self.log_handle.write(f"\n{'=' * 50}\n")
            self.log_handle.write(f"[{self.label}] 串口日志记录开始: {start_time}\n")
            self.log_handle.write(f"串口: {self.port_name} @ {self.baudrate}\n")
            self.log_handle.write(f"{'=' * 50}\n")
            self.log_handle.flush()
            logging.info(f"[{self.label}] 日志文件就绪: {self.log_file_path}")
        except Exception as e:
            logging.error(f"[{self.label}] 无法创建日志文件: {e}")
            self.log_handle = None
            return False

        return True

    def start_reading(self):
        """启动后台读取线程"""
        self._running = True
        self.thread = threading.Thread(target=self._read_loop, name=f"SerialReader-{self.label}", daemon=True)
        self.thread.start()
        logging.info(f"[{self.label}] 后台读取线程已启动")

    def is_boot_detected(self):
        """检查是否检测到开机关键词"""
        if not self.boot_keyword or self.boot_success_count <= 0:
            return False
        return self._boot_detected

    def reset_boot_detect(self):
        """重置开机检测状态（每轮循环开始前调用）"""
        self._boot_detect_count = 0
        self._boot_detected = False
        self.read_bytes = 0

    def get_boot_detect_info(self):
        """获取开机检测状态信息"""
        return {
            'enabled': bool(self.boot_keyword) and self.boot_success_count > 0,
            'detected': self._boot_detected,
            'count': self._boot_detect_count,
            'required': self.boot_success_count,
            'keyword': self.boot_keyword,
        }

    def _read_loop(self):
        """后台持续读取串口数据并写入日志"""
        while self._running:
            if self.ser and self.ser.in_waiting:
                try:
                    raw_line = self.ser.readline()
                    if raw_line:
                        self.read_bytes += len(raw_line)
                        line = raw_line.decode('utf-8', errors='ignore').strip()
                        if self.log_handle:
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                            self.log_handle.write(f"[{timestamp}] {line}\n")
                            self.log_handle.flush()
                        # 开机关键词检测（检测到后不再重复打印）
                        if not self._boot_detected and self.boot_keyword and self.boot_keyword.lower() in line.lower():
                            self._boot_detect_count += 1
                            if self.boot_log_to_main:
                                logging.info(f"[{self.label}] 检测到开机关键词 (第{self._boot_detect_count}/{self.boot_success_count}次): {line}")
                            if self._boot_detect_count >= self.boot_success_count:
                                self._boot_detected = True
                                if self.boot_log_to_main:
                                    logging.info(f"[{self.label}] [PASS] 开机关键词检测通过 (共匹配{self._boot_detect_count}次)")
                except Exception as e:
                    if self._running:
                        logging.warning(f"[{self.label}] 读取串口数据异常：{e}")
            else:
                time.sleep(0.05)

    def stop(self):
        """停止读取并关闭资源"""
        self._running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)

        if self.log_handle:
            try:
                end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.log_handle.write(f"\n{'=' * 50}\n")
                self.log_handle.write(f"[{self.label}] 串口日志记录结束: {end_time}\n")
                self.log_handle.write(f"{'=' * 50}\n")
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

        logging.info(f"[{self.label}] 串口和日志已关闭")


class RelayLogger:
    """继电器操作日志记录器"""

    def __init__(self, log_file_path):
        self.log_file_path = log_file_path
        self.log_handle = None

    def open(self):
        """打开继电器日志文件"""
        try:
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log_handle = open(self.log_file_path, 'a', encoding='utf-8')
            self.log_handle.write(f"\n{'=' * 50}\n")
            self.log_handle.write(f"继电器操作日志记录开始: {start_time}\n")
            self.log_handle.write(f"继电器类型: {RELAY_TYPE}\n")
            self.log_handle.write(f"继电器端口: {RELAY_COM_PORT}\n")
            self.log_handle.write(f"{'=' * 50}\n")
            self.log_handle.flush()
            logging.info(f"[继电器] 日志文件就绪: {self.log_file_path}")
            return True
        except Exception as e:
            logging.error(f"[继电器] 无法创建日志文件: {e}")
            self.log_handle = None
            return False

    def log(self, action, detail="", success=True):
        """记录一条继电器操作"""
        if self.log_handle:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            status = "OK" if success else "FAIL"
            line = f"[{timestamp}] [{status}] {action}"
            if detail:
                line += f" | {detail}"
            self.log_handle.write(line + "\n")
            self.log_handle.flush()

    def close(self):
        """关闭继电器日志文件"""
        if self.log_handle:
            try:
                end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.log_handle.write(f"\n{'=' * 50}\n")
                self.log_handle.write(f"继电器操作日志记录结束: {end_time}\n")
                self.log_handle.write(f"{'=' * 50}\n")
                self.log_handle.close()
            except Exception:
                pass
            self.log_handle = None


class RelayController:
    def __init__(self, relay_type, com_port=None, vid=None, pid=None, relay_logger=None, relay_channel=1):
        self.type = relay_type
        self.com_port = com_port
        self.vid = vid
        self.pid = pid
        self.handle = None
        self.relay_logger = relay_logger
        self.relay_channel = relay_channel
        # 根据通道号获取对应指令
        if relay_type == 'SERIAL' and relay_channel in RELAY_COMMANDS:
            self.power_on_cmd = bytes.fromhex(RELAY_COMMANDS[relay_channel]['on'])
            self.power_off_cmd = bytes.fromhex(RELAY_COMMANDS[relay_channel]['off'])
        else:
            self.power_on_cmd = None
            self.power_off_cmd = None
        self._init_hardware()

    def _init_hardware(self):
        if self.type == 'MOCK':
            logging.info("[继电器] 模式: MOCK (模拟模式)")
            if self.relay_logger:
                self.relay_logger.log("初始化", "MOCK模拟模式")

        elif self.type == 'SERIAL':
            if not self.com_port:
                logging.error("[继电器] 配置错误: 串口继电器未指定 COM 端口")
                if self.relay_logger:
                    self.relay_logger.log("初始化", "配置错误: 未指定COM端口", success=False)
                return
            if self.relay_channel not in RELAY_COMMANDS:
                logging.error(f"[继电器] 配置错误: 通道 {self.relay_channel} 未定义指令 (可用: {list(RELAY_COMMANDS.keys())})")
                if self.relay_logger:
                    self.relay_logger.log("初始化", f"配置错误: 通道{self.relay_channel}未定义", success=False)
                return
            try:
                self.handle = serial.Serial(self.com_port, RELAY_BAUDRATE, timeout=1)
                logging.info(f"[继电器] 串口继电器已初始化: {self.com_port} @ {RELAY_BAUDRATE} (通道: {self.relay_channel})")
                if self.relay_logger:
                    self.relay_logger.log("初始化", f"串口继电器 {self.com_port} @ {RELAY_BAUDRATE} 通道{self.relay_channel}")
            except Exception as e:
                logging.error(f"[继电器] 串口初始化失败: {e}")
                if self.relay_logger:
                    self.relay_logger.log("初始化", f"串口初始化失败: {e}", success=False)
                self.handle = None

        elif self.type == 'HID':
            try:
                import hid
                self.handle = hid.device()
                if self.vid and self.pid:
                    self.handle.open(self.vid, self.pid)
                    logging.info(f"[继电器] HID 继电器已连接: VID={hex(self.vid)}, PID={hex(self.pid)}")
                    if self.relay_logger:
                        self.relay_logger.log("初始化", f"HID VID={hex(self.vid)}, PID={hex(self.pid)}")
                else:
                    logging.warning("[继电器] HID 模式未配置 VID/PID")
                    if self.relay_logger:
                        self.relay_logger.log("初始化", "HID未配置VID/PID")
            except ImportError:
                logging.error("[继电器] 未安装 hidapi 库，请运行: pip install hidapi")
                if self.relay_logger:
                    self.relay_logger.log("初始化", "缺少hidapi库", success=False)
            except Exception as e:
                logging.error(f"[继电器] HID 初始化失败: {e}")
                if self.relay_logger:
                    self.relay_logger.log("初始化", f"HID初始化失败: {e}", success=False)
                self.handle = None

    def power_on(self):
        if self.type == 'MOCK':
            logging.info("⚡ [继电器] 通电 (模拟)")
            if self.relay_logger:
                self.relay_logger.log("通电", "模拟")
        elif self.type == 'SERIAL' and self.handle:
            if not self.power_on_cmd:
                logging.error(f"[继电器] 通道 {self.relay_channel} 通电指令未定义")
                if self.relay_logger:
                    self.relay_logger.log("通电", f"通道{self.relay_channel}指令未定义", success=False)
                return
            try:
                self.handle.write(self.power_on_cmd)
                cmd_hex = self.power_on_cmd.hex(' ')
                logging.info(f"⚡ [继电器] 通道{self.relay_channel} 通电 (指令: {cmd_hex})")
                if self.relay_logger:
                    self.relay_logger.log("通电", f"通道{self.relay_channel} 指令: {cmd_hex}")
                time.sleep(0.1)
            except Exception as e:
                logging.error(f"[继电器] 发送通电指令失败: {e}")
                if self.relay_logger:
                    self.relay_logger.log("通电", f"发送失败: {e}", success=False)
        elif self.type == 'HID' and self.handle:
            try:
                report = HID_ON_CMD + [0] * (64 - len(HID_ON_CMD))
                self.handle.write(report)
                logging.info("⚡ [继电器] 通电 (HID)")
                if self.relay_logger:
                    self.relay_logger.log("通电", f"HID指令: {HID_ON_CMD}")
            except Exception as e:
                logging.error(f"[继电器] 发送 HID 通电指令失败: {e}")
                if self.relay_logger:
                    self.relay_logger.log("通电", f"HID发送失败: {e}", success=False)

    def power_off(self):
        if self.type == 'MOCK':
            logging.info("⚡ [继电器] 断电 (模拟)")
            if self.relay_logger:
                self.relay_logger.log("断电", "模拟")
        elif self.type == 'SERIAL' and self.handle:
            if not self.power_off_cmd:
                logging.error(f"[继电器] 通道 {self.relay_channel} 断电指令未定义")
                if self.relay_logger:
                    self.relay_logger.log("断电", f"通道{self.relay_channel}指令未定义", success=False)
                return
            try:
                self.handle.write(self.power_off_cmd)
                cmd_hex = self.power_off_cmd.hex(' ')
                logging.info(f"⚡ [继电器] 通道{self.relay_channel} 断电 (指令: {cmd_hex})")
                if self.relay_logger:
                    self.relay_logger.log("断电", f"通道{self.relay_channel} 指令: {cmd_hex}")
                time.sleep(0.1)
            except Exception as e:
                logging.error(f"[继电器] 发送断电指令失败: {e}")
                if self.relay_logger:
                    self.relay_logger.log("断电", f"发送失败: {e}", success=False)
        elif self.type == 'HID' and self.handle:
            try:
                report = HID_OFF_CMD + [0] * (64 - len(HID_OFF_CMD))
                self.handle.write(report)
                logging.info("⚡ [继电器] 断电 (HID)")
                if self.relay_logger:
                    self.relay_logger.log("断电", f"HID指令: {HID_OFF_CMD}")
            except Exception as e:
                logging.error(f"[继电器] 发送 HID 断电指令失败: {e}")
                if self.relay_logger:
                    self.relay_logger.log("断电", f"HID发送失败: {e}", success=False)

    def close(self):
        if self.handle:
            try:
                self.handle.close()
                logging.info("[继电器] 硬件句柄已关闭")
                if self.relay_logger:
                    self.relay_logger.log("关闭", "硬件句柄已关闭")
            except Exception:
                pass
        self.handle = None


class PowerCycleTest:
    def __init__(self):
        # 初始化继电器日志
        self.relay_logger = RelayLogger(RELAY_LOG_FILE)
        self.relay_logger.open()

        # 初始化ADB日志
        self.adb_log_handle = None

        self.relay = RelayController(RELAY_TYPE, RELAY_COM_PORT, RELAY_VID, RELAY_PID,
                                     relay_logger=self.relay_logger, relay_channel=RELAY_CHANNEL)

        # 根据配置创建串口记录器（支持0、1或2个串口）
        self.serial_loggers = []
        shared = device_session.active_session()
        shared_serial = shared['session'] if (
                shared and shared.get('channel') == 'serial') else None
        if DUT_SERIAL_PORT_COUNT >= 1:
            boot_kw1 = SERIAL_BOOT_KEYWORD_PORT1 if SERIAL_BOOT_DETECT_PORT1 else ""
            if shared_serial and str(DUT_COM_PORT1).strip() == shared_serial.port:
                self.serial_logger1 = device_session.SharedSerialLoggerAdapter(
                    shared_serial, label="串口1", boot_keyword=boot_kw1,
                    boot_success_count=SERIAL_BOOT_SUCCESS_COUNT_PORT1,
                    boot_log_to_main=SERIAL_BOOT_LOG_TO_MAIN_PORT1)
            else:
                self.serial_logger1 = SerialLogger(DUT_COM_PORT1, DUT_BAUDRATE, SERIAL_LOG_FILE1, label="串口1",
                                                   boot_keyword=boot_kw1, boot_success_count=SERIAL_BOOT_SUCCESS_COUNT_PORT1,
                                                   boot_log_to_main=SERIAL_BOOT_LOG_TO_MAIN_PORT1)
            self.serial_loggers.append(self.serial_logger1)
        else:
            self.serial_logger1 = None
        if DUT_SERIAL_PORT_COUNT >= 2:
            boot_kw2 = SERIAL_BOOT_KEYWORD_PORT2 if SERIAL_BOOT_DETECT_PORT2 else ""
            self.serial_logger2 = SerialLogger(DUT_COM_PORT2, DUT_BAUDRATE, SERIAL_LOG_FILE2, label="串口2",
                                               boot_keyword=boot_kw2, boot_success_count=SERIAL_BOOT_SUCCESS_COUNT_PORT2,
                                               boot_log_to_main=SERIAL_BOOT_LOG_TO_MAIN_PORT2)
            self.serial_loggers.append(self.serial_logger2)
        else:
            self.serial_logger2 = None

        self.running = True
        self.report_handle = None
        self._serial_boot_logged = set()  # 跟踪已打印检测结果的串口，避免重复
        self._random_boot_logged = False  # 随机上电模式下是否已检测到开机

        # signal.signal() 只能在主线程中调用，GUI 模式下会在子线程运行
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self.stop_handler)
            signal.signal(signal.SIGTERM, self.stop_handler)

    def stop_handler(self, _sig, _frame):
        logging.warning("收到中断信号 (Ctrl+C)，正在安全停止测试...")
        self.running = False

    def init_report_file(self):
        """初始化报告文件"""
        try:
            self.report_handle = open(REPORT_FILE, 'a', encoding='utf-8')
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            separator = f"\n{'=' * 20} 测试会话开始：{start_time} {'=' * 20}\n"
            self.report_handle.write(separator)
            self.report_handle.flush()
            logging.info(f"报告文件就绪: {REPORT_FILE}")
        except Exception as e:
            logging.error(f"无法创建报告文件: {e}")
            self.report_handle = None

    def init_adb_log_file(self):
        """初始化ADB日志文件（仅当ADB_ENABLED=True时创建）"""
        if not ADB_ENABLED:
            self.adb_log_handle = None
            return
        try:
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.adb_log_handle = open(ADB_LOG_FILE, 'a', encoding='utf-8')
            self.adb_log_handle.write(f"\n{'=' * 50}\n")
            self.adb_log_handle.write(f"ADB操作日志记录开始: {start_time}\n")
            self.adb_log_handle.write(f"ADB路径: {ADB_PATH}\n")
            self.adb_log_handle.write(f"设备序列号: {ADB_DEVICE_SERIAL or '自动选择'}\n")
            self.adb_log_handle.write(f"Ping目标: {PING_TARGET}\n")
            self.adb_log_handle.write(f"{'=' * 50}\n")
            self.adb_log_handle.flush()
            logging.info(f"[ADB] 日志文件就绪: {ADB_LOG_FILE}")
        except Exception as e:
            logging.error(f"[ADB] 无法创建日志文件: {e}")
            self.adb_log_handle = None

    def write_result(self, cycle_num, status, duration=0, boot_time=0, ping_details=""):
        """写入测试结果"""
        if self.report_handle:
            try:
                timestamp = datetime.now().strftime("%H:%M:%S")
                line = (
                    f"[{timestamp}] 第 {cycle_num:04d} 次 | "
                    f"结果：{status.upper():4s} | "
                    f"启动耗时：{boot_time:.2f}s | "
                    f"总上电时长：{duration:.2f}s"
                )
                if ping_details:
                    line += f" | {ping_details}"
                line += "\n"
                self.report_handle.write(line)
                self.report_handle.flush()
            except Exception as e:
                logging.error(f"写入报告失败：{e}")

    def open_serial_ports(self):
        """打开串口并启动后台读取（根据 DUT_SERIAL_PORT_COUNT 配置打开对应数量的串口）"""
        all_ok = True
        port_names = [DUT_COM_PORT1]
        if DUT_SERIAL_PORT_COUNT >= 2:
            port_names.append(DUT_COM_PORT2)

        for i, logger in enumerate(self.serial_loggers):
            ok = logger.open()
            if not ok:
                logging.error(f"{logger.label} ({port_names[i]}) 打开失败")
                all_ok = False

        if not all_ok:
            return False

        for logger in self.serial_loggers:
            logger.start_reading()
            logger.reset_boot_detect()

        return True

    def close_serial_ports(self):
        """关闭所有串口"""
        for logger in self.serial_loggers:
            logger.stop()

    def _check_serial_boot(self):
        """检查所有串口是否都检测到开机关键词（AND逻辑），返回 (detected: bool, detail: str)"""
        if not self.serial_loggers:
            return False, ""
        details = []
        all_detected = True
        for logger in self.serial_loggers:
            if logger.is_boot_detected():
                info = logger.get_boot_detect_info()
                detail = f"{logger.label}检测到'{info['keyword']}'(匹配{info['count']}次)"
                # 只打印一次，避免重复刷屏
                if logger.label not in self._serial_boot_logged:
                    self._serial_boot_logged.add(logger.label)
                    logging.info(f"[PASS] {detail}")
                details.append(detail)
            else:
                info = logger.get_boot_detect_info()
                if info['enabled']:
                    all_detected = False
        if all_detected and details:
            return True, "；".join(details)
        return False, ""

    def _serial_boot_hint(self):
        """生成串口开机检测进度摘要，如 '串口1 1/2 | 串口2 0/1'；无启用的检测时返回 ''"""
        parts = []
        for logger in self.serial_loggers:
            info = logger.get_boot_detect_info()
            if info['enabled']:
                parts.append(f"{logger.label} {info['count']}/{info['required']}")
        return " | ".join(parts)

    def _serial_boot_fail_detail(self):
        """生成串口开机检测失败诊断信息（关键词、已匹配次数、串口是否收到数据）"""
        parts = []
        for logger in self.serial_loggers:
            info = logger.get_boot_detect_info()
            if not info['enabled']:
                continue
            rx = '无数据' if logger.read_bytes <= 0 else f'已收到{logger.read_bytes}字节'
            parts.append(f"{logger.label} 关键词'{info['keyword']}' "
                         f"匹配{info['count']}/{info['required']}次 (串口{rx})")
        return "；".join(parts)

    def ping_from_pc(self):
        """从PC端直接ping设备IP，返回 (success: bool, output: str)"""
        timeout_ms = PING_TIMEOUT * 1000
        cmd = ['ping']
        if PING_SOURCE:
            cmd.extend(['-S', PING_SOURCE])
        cmd.extend(['-n', '1', '-w', str(timeout_ms), '-l', str(PING_PACKET_SIZE), PING_TARGET])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=PING_TIMEOUT + 5)
            output = result.stdout.strip()
            success = result.returncode == 0 and 'TTL=' in output.upper()
            return success, output
        except Exception as e:
            return False, str(e)

    def wait_boot_ready(self, timeout=BOOT_TIMEOUT):
        """
        等待设备开机就绪：
        - PING_ENABLED=True: 从PC端通过网口ping设备IP判定开机
        - PING_ENABLED=False + ADB_ENABLED=True: 通过ADB连接判定开机
        - PING_ENABLED=False + ADB_ENABLED=False:
            启用了串口关键词检测 -> 等待期间以关键词判定，超时未匹配则判定失败
            未启用任何检测      -> 纯时序模式，等待 timeout 秒后判定成功
        返回: (success: bool, boot_time: float, details: str)
        """
        start_time = time.time()
        consecutive_success = 0
        total_ping_attempts = 0
        total_ping_success = 0
        total_ping_fail = 0
        shell = None  # ADBShellSession instance

        if ADB_ENABLED:
            adb_log_session_event(self.adb_log_handle,
                                  f"{'=' * 30} 新一轮检测开始 {'=' * 30}")

        # ── 串口开机检测（初始检查）──
        serial_detected, serial_detail = self._check_serial_boot()
        if serial_detected:
            boot_time = time.time() - start_time
            return True, boot_time, serial_detail

        # ── PING_ENABLED=False 分支 ──
        if not PING_ENABLED:
            if ADB_ENABLED:
                return self._wait_adb_boot(start_time, timeout)
            else:
                # 纯时序模式：等待BOOT_TIMEOUT（期间持续检测串口开机关键词）
                waited = 0
                active_detect = [l for l in self.serial_loggers
                                 if l.boot_keyword and l.boot_success_count > 0]
                if active_detect:
                    kw_desc = ", ".join(f"{l.label}:'{l.boot_keyword}'x{l.boot_success_count}"
                                        for l in active_detect)
                    logging.info(f"[等待开机] 串口开机检测 | 关键词: {kw_desc} | 超时: {timeout}s")
                    # 倒计时较长时降低打印频率，避免刷屏
                    report_step = 5 if timeout <= 60 else 10
                    last_report = 0
                    while waited < timeout and self.running:
                        serial_detected, serial_detail = self._check_serial_boot()
                        if serial_detected:
                            boot_time = time.time() - start_time
                            detail = f"串口开机检测通过 (耗时: {boot_time:.1f}s) | {serial_detail}"
                            logging.info(f"[PASS] 串口开机检测通过 (耗时: {boot_time:.1f}s)")
                            return True, boot_time, detail
                        sleep_step = min(1, timeout - waited)
                        time.sleep(sleep_step)
                        waited += sleep_step
                        if waited - last_report >= report_step:
                            last_report = waited
                            pct = int(waited / timeout * 100)
                            logging.info(f"[等待开机] 未检测到关键词 ({self._serial_boot_hint()}) "
                                         f"| {int(waited)}s / {timeout}s ({pct}%)")

                    if not self.running:
                        return False, waited, "用户中断"
                    # 超时：启用了关键词检测但始终未匹配 -> 判定开机失败
                    boot_time = time.time() - start_time
                    detail = f"超时{timeout}s未检测到开机关键词 | {self._serial_boot_fail_detail()}"
                    logging.error(f"[FAIL] 串口开机检测超时: {detail}")
                    return False, boot_time, detail

                # 未启用任何开机检测：纯时序等待，到点即判定通过
                logging.info(f"[等待开机] 纯时序模式（未启用开机检测），等待 {timeout}s 后判定开机")
                boot_time = timeout
                while waited < timeout and self.running:
                    sleep_step = min(1, timeout - waited)
                    time.sleep(sleep_step)
                    waited += sleep_step
                    if waited % 10 == 0:
                        pct = int(waited / timeout * 100)
                        logging.info(f"[等待开机] 纯时序等待 {int(waited)}s / {timeout}s ({pct}%)")
                if not self.running:
                    return False, waited, "用户中断"
                detail = f"等待{timeout}s (纯时序模式)"
                logging.info(f"[PASS] 纯时序等待完成，判定开机成功 (耗时: {boot_time:.1f}s)")
                return True, boot_time, detail

        # ── PING_ENABLED=True 分支（正常ping检测） ──
        logging.info(f"[等待开机] 网口ping目标: {PING_TARGET}, 超时: {timeout}s")

        try:
            while time.time() - start_time < timeout:
                if not self.running:
                    return False, time.time() - start_time, "用户中断"

                # ── 阶段1：从PC端ping设备IP ──
                total_ping_attempts += 1
                success, ping_out = self.ping_from_pc()

                # 记录ping日志
                if ADB_ENABLED and self.adb_log_handle:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    self.adb_log_handle.write(f"\n[{ts}] PING #{total_ping_attempts} (from PC)\n")
                    self.adb_log_handle.write(f"TARGET: {PING_TARGET}\n")
                    self.adb_log_handle.write(f"OUTPUT:\n{ping_out}\n")
                    self.adb_log_handle.write(f"RESULT: {'SUCCESS' if success else 'FAIL'}\n")
                    self.adb_log_handle.flush()

                if success:
                    consecutive_success += 1
                    total_ping_success += 1
                    logging.info(
                        f"  Ping [{total_ping_attempts}] 成功 (连续: {consecutive_success}/{PING_SUCCESS_COUNT})"
                    )

                    if consecutive_success >= PING_SUCCESS_COUNT:
                        boot_time = time.time() - start_time
                        detail = f"ping成功{total_ping_success}/{total_ping_attempts}次"

                        # ── 阶段2（可选）：ADB诊断 ──
                        if ADB_ENABLED:
                            adb_log_session_event(self.adb_log_handle,
                                                  f"网口ping成功: {detail}, 耗时: {boot_time:.1f}s")
                            logging.info(f"[PASS] 网口ping成功 (耗时: {boot_time:.1f}s)，开始ADB诊断...")
                            self._adb_diagnostics(shell)
                        else:
                            logging.info(f"[PASS] 设备开机成功! 连续ping {PING_SUCCESS_COUNT}次通过 (耗时: {boot_time:.1f}s)")

                        return True, boot_time, detail
                else:
                    consecutive_success = 0
                    total_ping_fail += 1
                    elapsed = int(time.time() - start_time)
                    pct = int(elapsed / timeout * 100)
                    logging.info(
                        f"  Ping [{total_ping_attempts}] 失败 (累计失败: {total_ping_fail}) [{elapsed}s/{timeout}s {pct}%]"
                    )
                    # Ping失败时也检查串口开机检测
                    serial_detected, serial_detail = self._check_serial_boot()
                    if serial_detected:
                        boot_time = time.time() - start_time
                        detail = f"串口开机检测通过 | {serial_detail}"
                        logging.info(f"[PASS] Ping未通但串口已检测到开机 (耗时: {boot_time:.1f}s)")
                        return True, boot_time, detail

                time.sleep(PING_INTERVAL)

            # 超时（while循环正常结束）
            boot_time = time.time() - start_time
            detail = f"ping尝试{total_ping_attempts}次(成功{total_ping_success}/失败{total_ping_fail})"
            if ADB_ENABLED:
                adb_log_session_event(self.adb_log_handle,
                                      f"启动超时: {detail}")
            logging.error(f"[FAIL] 启动超时: {timeout}s内未能成功ping通设备 ({detail})")
            return False, boot_time, detail

        finally:
            if shell:
                shell.close()

    def _adb_diagnostics(self, shell):
        """ADB辅助诊断：连接检查 + shell登录 + ifconfig（仅当ADB_ENABLED=True时调用）"""
        try:
            if not adb_check_connected():
                logging.warning("[ADB] 设备未连接，跳过ADB诊断")
                adb_log_session_event(self.adb_log_handle, "ADB设备未连接，跳过诊断")
                return

            logging.info("[ADB] 设备已连接，启动Shell会话...")
            shell = self._make_adb_shell()
            shell_ok, shell_msg = shell.start()
            if shell_ok:
                logging.info(f"[ADB] Shell会话就绪: {shell_msg}")
                adb_log_session_event(self.adb_log_handle, f"ADB Shell会话就绪: {shell_msg}")

                # 执行ifconfig查询IP地址
                logging.info("[ADB] 执行ifconfig查询IP地址...")
                ifconfig_ok, ifconfig_out = shell.run_command('ifconfig', timeout=10)
                if ifconfig_ok:
                    ip_lines = [l.strip() for l in ifconfig_out.split('\n')
                                if 'inet addr' in l.lower() or 'inet ' in l.lower()]
                    if ip_lines:
                        logging.info("[ADB] 检测到的IP地址:")
                        for line in ip_lines:
                            logging.info(f"   {line}")
                    else:
                        logging.info("[ADB] ifconfig输出中未找到IP地址")
                adb_log_session_event(self.adb_log_handle,
                                      f"ifconfig结果: {'成功' if ifconfig_ok else '失败'}")

                # ADB Shell 内部 Ping 检测
                if ADB_PING_ENABLED and ADB_PING_TARGET:
                    logging.info(f"[ADB] Shell内执行ping检测 (目标: {ADB_PING_TARGET}, 发包: {ADB_PING_COUNT}次)")
                    ping_cmd = f'ping -c {ADB_PING_COUNT} {ADB_PING_TARGET}'
                    ping_ok, ping_out = shell.run_command(ping_cmd, timeout=ADB_PING_COUNT * 3 + 5)
                    if ping_ok:
                        # 统计成功次数
                        success_lines = [l for l in ping_out.split('\n') if 'ttl=' in l.lower() or 'time=' in l.lower()]
                        success_count = len(success_lines)
                        ping_pass = success_count >= ADB_PING_SUCCESS_COUNT
                        status = '连通' if ping_pass else '未达标'
                        logging.info(f"[ADB] Ping结果: 成功{success_count}/{ADB_PING_COUNT}次 ({status})")
                        adb_log_session_event(self.adb_log_handle,
                                              f"ADB Ping: {ADB_PING_TARGET} 成功{success_count}/{ADB_PING_COUNT}次 {'通过' if ping_pass else '未通过'}")
                    else:
                        logging.info("[ADB] Ping执行失败")
                        adb_log_session_event(self.adb_log_handle, "ADB Ping执行失败")
            else:
                logging.error(f"[ADB] Shell启动失败: {shell_msg}")
                adb_log_session_event(self.adb_log_handle, f"ADB Shell启动失败: {shell_msg}")

            shell.close()
        except Exception as e:
            logging.warning(f"[ADB] 诊断异常: {e}")
            adb_log_session_event(self.adb_log_handle, f"ADB诊断异常: {e}")

    def _make_adb_shell(self):
        """优先复用共享 ADB 日志会话；没有共享会话时才另开 adb shell。"""
        shared = device_session.active_session()
        if shared and shared.get('channel') == 'adb':
            logging.info("[ADB] 复用共享 ADB 会话执行诊断命令")
            return device_session.SharedAdbShellSession(shared['session'])
        return ADBShellSession(adb_log_handle=self.adb_log_handle)

    def _wait_adb_boot(self, start_time, timeout):
        """等待ADB连接作为开机判定（仅当PING_ENABLED=False且ADB_ENABLED=True时调用）"""
        logging.info(f"[等待开机] Ping已禁用，等待ADB连接 (超时: {timeout}s)")

        while time.time() - start_time < timeout:
            if not self.running:
                return False, time.time() - start_time, "用户中断"

            elapsed = time.time() - start_time

            if adb_check_connected():
                boot_time = time.time() - start_time
                adb_log_session_event(self.adb_log_handle,
                                      f"ADB设备已连接 (耗时: {boot_time:.1f}s)")
                logging.info(f"[PASS] ADB设备已连接，判定开机成功 (耗时: {boot_time:.1f}s)")

                # 做ADB诊断（shell登录 + ifconfig）
                shell = self._make_adb_shell()
                self._adb_diagnostics(shell)

                detail = f"ADB连接成功 (耗时: {boot_time:.1f}s)"
                return True, boot_time, detail
            else:
                elapsed_int = int(elapsed)
                if elapsed_int % 5 == 0 and elapsed_int > 0:
                    pct = int(elapsed / timeout * 100)
                    logging.info(f"[等待ADB] {elapsed_int}s / {timeout}s ({pct}%)")
                time.sleep(PING_INTERVAL)

        # 超时
        boot_time = time.time() - start_time
        detail = f"ADB连接超时 ({timeout}s)"
        adb_log_session_event(self.adb_log_handle, f"启动超时: {detail}")
        logging.error(f"[FAIL] 启动超时: {timeout}s内ADB未连接")
        return False, boot_time, detail

    def run_cycle(self, cycle_num):
        """执行一次开关机循环"""
        logging.info("")
        logging.info(f"{'─' * 15} 第 {cycle_num:04d} 次循环 {'─' * 15}")
        self._serial_boot_logged.clear()  # 每轮循环重置检测日志状态
        self._random_boot_logged = False  # 重置随机模式开机检测标记
        status = "fail"
        total_duration = 0.0
        boot_time = 0.0
        ping_details = ""
        cycle_start_time = time.time()

        # 记录本轮循环到继电器日志
        if self.relay_logger:
            self.relay_logger.log("循环开始", f"第{cycle_num}次")

        try:
            # 1. 继电器上电
            self.relay.power_on()

            # 2. 等待设备开始启动
            logging.info(f"[启动等待] {POWER_ON_DELAY}s...")
            time.sleep(POWER_ON_DELAY)

            # 3. 打开串口并启动后台读取
            serial_ok = self.open_serial_ports()
            if not serial_ok:
                logging.warning("串口打开失败，继续等待设备开机 (ping不受影响)")

            if POWER_ON_TIME_RANDOM:
                # ── 随机上电模式 ──
                target_on_time = random.randint(POWER_ON_TIME_MIN, POWER_ON_TIME_MAX)
                status = "pass"
                boot_time = 0.0

                if RANDOM_MODE_BOOT_DETECT:
                    logging.info(f"[随机上电] 保持上电 {target_on_time}s ({POWER_ON_TIME_MIN}-{POWER_ON_TIME_MAX}s)，含开机检测")
                    ping_details = f"随机上电{target_on_time}s (含开机检测)"
                else:
                    ping_details = f"随机上电{target_on_time}s (不判定开机)"
                    logging.info(f"[随机上电] 保持上电 {target_on_time}s ({POWER_ON_TIME_MIN}-{POWER_ON_TIME_MAX}s)")

                current_on_time = time.time() - cycle_start_time
                wait_remaining = max(0, target_on_time - current_on_time)
                if wait_remaining > 0:
                    sleep_step = 1.0
                    waited_in_phase = 0
                    while wait_remaining > 0 and self.running:
                        sleep_time = min(sleep_step, wait_remaining)
                        time.sleep(sleep_time)
                        wait_remaining -= sleep_step
                        waited_in_phase += 1
                        # 随机模式下做开机检测
                        if RANDOM_MODE_BOOT_DETECT:
                            serial_detected, serial_detail = self._check_serial_boot()
                            if serial_detected and not self._random_boot_logged:
                                boot_time = time.time() - cycle_start_time
                                logging.info(f"[PASS] 随机上电期间检测到开机 (耗时: {boot_time:.1f}s) {serial_detail}")
                                ping_details += f" | 开机检测通过({boot_time:.1f}s)"
                                self._random_boot_logged = True
                        # 每5秒打印进度
                        if waited_in_phase % 5 == 0 and waited_in_phase > 0:
                            pct = int((target_on_time - wait_remaining) / target_on_time * 100)
                            logging.info(f"[随机上电] 已等待 {int(target_on_time - wait_remaining)}s / {target_on_time}s ({pct}%)")
                    # 随机等待结束后再做一次完整的开机判定
                    if RANDOM_MODE_BOOT_DETECT and not self._random_boot_logged:
                        boot_ok, bt, detail = self.wait_boot_ready(max(0, BOOT_TIMEOUT - (time.time() - cycle_start_time)))
                        if boot_ok:
                            boot_time = bt
                            logging.info(f"[PASS] 随机上电模式开机检测通过 (耗时: {boot_time:.1f}s)")
                            ping_details += f" | {detail}"
                        else:
                            logging.warning(f"[FAIL] 随机上电模式开机检测未通过: {detail}")
                            ping_details += " | 开机检测未通过"
                elif RANDOM_MODE_BOOT_DETECT:
                    # 随机等待时间已过（被POWER_ON_DELAY覆盖），直接做开机检测
                    remaining_timeout = max(1, BOOT_TIMEOUT - (time.time() - cycle_start_time))
                    boot_ok, bt, detail = self.wait_boot_ready(remaining_timeout)
                    if boot_ok:
                        boot_time = bt
                        logging.info(f"[PASS] 随机上电模式开机检测通过 (耗时: {boot_time:.1f}s)")
                        ping_details += f" | {detail}"
                    else:
                        logging.warning(f"[FAIL] 随机上电模式开机检测未通过: {detail}")
                        ping_details += " | 开机检测未通过"
            else:
                # ── 正常模式：等待开机判定 ──
                # 4. 等待设备开机 (网口ping + 可选ADB诊断)
                boot_ok, boot_time, ping_details = self.wait_boot_ready(BOOT_TIMEOUT)

                current_on_time = time.time() - cycle_start_time

                if boot_ok:
                    status = "pass"
                    logging.info(f"[PASS] 第{cycle_num}次开机成功! 启动耗时: {boot_time:.1f}s, 已运行: {current_on_time:.1f}s")
                else:
                    status = "fail"
                    logging.error(f"[FAIL] 第{cycle_num}次开机失败! {ping_details}")

                # 5. 满足最小上电时间
                current_on_time = time.time() - cycle_start_time
                wait_remaining = max(0, MIN_POWER_ON_TIME - current_on_time)
                if wait_remaining > 0:
                    logging.info(f"[上电保持] {wait_remaining:.1f}s (目标: {MIN_POWER_ON_TIME}s)")
                    sleep_step = 1.0
                    while wait_remaining > 0 and self.running:
                        sleep_time = min(sleep_step, wait_remaining)
                        time.sleep(sleep_time)
                        wait_remaining -= sleep_step

            total_duration = time.time() - cycle_start_time

            # 6. 写入报告
            self.write_result(cycle_num, status, total_duration, boot_time, ping_details)

        except Exception as e:
            logging.error(f"循环执行出错: {e}", exc_info=True)
            status = "error"
            total_duration = time.time() - cycle_start_time
            self.write_result(cycle_num, status, total_duration)

        finally:
            # 7. 断电
            self.relay.power_off()
            self.close_serial_ports()

            # 循环摘要行
            status_tag = f"[{status.upper()}]"
            logging.info(f"{'─' * 8} 第 {cycle_num:04d} 次 {status_tag:^8s} 启动: {boot_time:.1f}s  上电: {total_duration:.1f}s {'─' * 8}")

            if self.relay_logger:
                self.relay_logger.log("循环结束", f"第{cycle_num}次 | 状态:{status} | 上电:{total_duration:.1f}s")

            # 8. 断电冷却期 (电容放电)
            if self.running:
                if POWER_OFF_WAIT_RANDOM:
                    actual_wait_time = random.randint(POWER_OFF_WAIT_MIN, POWER_OFF_WAIT_MAX)
                    logging.info(f"[断电冷却] 随机等待 {actual_wait_time}s ({POWER_OFF_WAIT_MIN}-{POWER_OFF_WAIT_MAX}s)")
                else:
                    actual_wait_time = POWER_OFF_WAIT_TIME
                    logging.info(f"[断电冷却] {actual_wait_time}s")

                for i in range(actual_wait_time):
                    if not self.running:
                        break
                    time.sleep(1)
                    if (i + 1) % 5 == 0:
                        pct = int((i + 1) / actual_wait_time * 100)
                        logging.info(f"[断电冷却] {i + 1}s / {actual_wait_time}s ({pct}%)")

    def run(self):
        logging.info("")
        logging.info("╔══════════════════════════════════════════════════════════╗")
        logging.info("║          Reboot Test v2.0                                ║")
        logging.info("╚══════════════════════════════════════════════════════════╝")
        logging.info("")

        # 紧凑配置摘要
        logging.info("配置摘要:")
        if DUT_SERIAL_PORT_COUNT == 0:
            logging.info(f"  串口    | 不使用 (DUT_SERIAL_PORT_COUNT=0)")
        else:
            serial_info = f"  串口1   | {DUT_COM_PORT1} @ {DUT_BAUDRATE}"
            if SERIAL_BOOT_DETECT_PORT1 and SERIAL_BOOT_KEYWORD_PORT1:
                serial_info += (f" | 关键词: '{SERIAL_BOOT_KEYWORD_PORT1}' x{SERIAL_BOOT_SUCCESS_COUNT_PORT1}"
                                f" | 匹配行打印: {'开' if SERIAL_BOOT_LOG_TO_MAIN_PORT1 else '关'}")
            logging.info(serial_info)
            if DUT_SERIAL_PORT_COUNT >= 2:
                serial_info2 = f"  串口2   | {DUT_COM_PORT2} @ {DUT_BAUDRATE}"
                if SERIAL_BOOT_DETECT_PORT2 and SERIAL_BOOT_KEYWORD_PORT2:
                    serial_info2 += (f" | 关键词: '{SERIAL_BOOT_KEYWORD_PORT2}' x{SERIAL_BOOT_SUCCESS_COUNT_PORT2}"
                                     f" | 匹配行打印: {'开' if SERIAL_BOOT_LOG_TO_MAIN_PORT2 else '关'}")
                logging.info(serial_info2)
        if PING_ENABLED:
            ping_src = PING_SOURCE if PING_SOURCE else '自动'
            logging.info(f"  Ping    | {ping_src} -> {PING_TARGET} (包:{PING_PACKET_SIZE}B, 连续{PING_SUCCESS_COUNT}次判定)")
        else:
            logging.info(f"  Ping    | 禁用")
        adb_role = '辅助诊断' if PING_ENABLED else ('开机判定' if ADB_ENABLED else '纯时序模式')
        logging.info(f"  ADB     | {'启用 (' + adb_role + ')' if ADB_ENABLED else '禁用'}")
        if ADB_ENABLED:
            logging.info(f"          | 用户: {ADB_LOGIN_USERNAME or '(跳过)'} | Shell Ping: {'启用' if ADB_PING_ENABLED else '禁用'}")
        logging.info(f"  继电器  | {RELAY_TYPE} (通道{RELAY_CHANNEL}) | {RELAY_COM_PORT}")
        logging.info(f"  超时    | {BOOT_TIMEOUT}s")
        if POWER_ON_TIME_RANDOM:
            boot_detect_str = '含开机检测' if RANDOM_MODE_BOOT_DETECT else '不判定开机'
            logging.info(f"  模式    | 随机上电 {POWER_ON_TIME_MIN}-{POWER_ON_TIME_MAX}s ({boot_detect_str})")
        logging.info(f"  日志目录| {os.path.dirname(LOG_FILE) or _SCRIPT_DIR}")
        logging.info("")

        self.init_report_file()
        self.init_adb_log_file()

        # 初始安全状态：确保先断电
        logging.info("初始化: 执行首次断电以确保安全状态...")
        self.relay.power_off()
        time.sleep(2)

        cycle = 0
        while self.running:
            cycle += 1
            if 0 < MAX_CYCLES < cycle:
                logging.info(f"已达到最大循环次数 {MAX_CYCLES}，自动停止测试")
                break
            self.run_cycle(cycle)

        # 停止后的清理
        logging.info("")
        logging.info("测试循环已终止")
        self.relay.power_off()
        self.relay.close()
        self.close_serial_ports()

        # 关闭继电器日志
        self.relay_logger.close()
        logging.info(f"继电器日志已保存: {RELAY_LOG_FILE}")

        # 关闭ADB日志
        if self.adb_log_handle:
            end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.adb_log_handle.write(f"\n{'=' * 50}\n")
            self.adb_log_handle.write(f"ADB操作日志记录结束: {end_time}\n")
            self.adb_log_handle.write(f"{'=' * 50}\n")
            self.adb_log_handle.close()
            logging.info(f"ADB日志已保存: {ADB_LOG_FILE}")

        # 关闭报告
        if self.report_handle:
            end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.report_handle.write(f"{'=' * 20} 测试会话结束：{end_time} {'=' * 20}\n")
            self.report_handle.close()
            logging.info(f"测试报告已保存: {REPORT_FILE}")


if __name__ == "__main__":
    # 依赖检查
    missing_libs = []
    try:
        import serial
    except ImportError:
        missing_libs.append("pyserial")

    if RELAY_TYPE == 'HID':
        try:
            import hid
        except ImportError:
            missing_libs.append("hidapi")

    if missing_libs:
        print(f"\n[错误] 缺少必要的库: {', '.join(missing_libs)}")
        print(f"请运行以下命令安装: pip install {' '.join(missing_libs)}\n")
        sys.exit(1)

    # 检查ADB是否可用（仅当ADB_ENABLED=True时检查）
    if ADB_ENABLED:
        try:
            result = subprocess.run([ADB_PATH, 'version'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                adb_version = result.stdout.strip().split('\n')[0]
                print(f"[OK] ADB可用: {adb_version}")
            else:
                print(f"[警告] ADB返回异常，请确认ADB已正确安装并加入PATH")
        except FileNotFoundError:
            print(f"[警告] 找不到ADB，ADB诊断功能将不可用")
            print(f"  当前ADB_PATH: {ADB_PATH}")
            print(f"  (如不需要ADB可将 ADB_ENABLED 设为 False)")
        except Exception as e:
            print(f"[警告] ADB检查异常: {e}")
    else:
        print("[信息] ADB已禁用 (ADB_ENABLED=False)")

    # 初始化日志
    setup_logging()

    # 启动测试
    test = PowerCycleTest()
    test.run()
