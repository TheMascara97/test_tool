# -*- coding: utf-8 -*-
"""进程级设备日志/命令共享会话。

一个测试进程只允许打开一个设备日志会话。串口被会话占用后，业务模块必须复用
send_command()/wait_for_pattern()；ADB/SSH 也复用同一条登录后的持续通道。
"""

import logging
import os
import queue
import re
import subprocess
import threading
import time
from datetime import datetime


LOG_CHANNELS = ("adb", "ssh", "serial")
CHANNEL_LABELS = {"adb": "ADB", "ssh": "SSH", "serial": "串口"}


class SessionBusyError(RuntimeError):
    pass


class DeviceSessionError(RuntimeError):
    pass


class BaseSession:
    """提供 FOTA SerialMonitor 兼容的最小监控接口。"""

    def __init__(self, log_path, stop_event=None):
        self.log_path = log_path
        self.stop_event = stop_event or threading.Event()
        self.lines = queue.Queue(maxsize=5000)
        self.log_handle = None
        self.callback = None
        self._lock = threading.Lock()
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
        self.version_rules = []
        self.boot_keyword = ""
        self.boot_success_count = 1
        self._boot_detect_count = 0
        self.flag_rules = []
        self.log_to_main = False
        self._start_time = time.time()

    def open_log(self, headers):
        if self.log_handle:
            return True
        try:
            self.log_handle = open(self.log_path, "a", encoding="utf-8")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log_handle.write(f"\n{'=' * 60}\n")
            self.log_handle.write(f"{headers} 日志记录开始: {stamp}\n")
            self.log_handle.write(f"{'=' * 60}\n")
            self.log_handle.flush()
            return True
        except OSError as exc:
            logging.error("无法创建共享日志文件 %s: %s", self.log_path, exc)
            return False

    def emit(self, line):
        line = (line or "").rstrip("\r\n")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with self._lock:
            if self.log_handle:
                try:
                    self.log_handle.write(f"[{timestamp}] {line}\n")
                    self.log_handle.flush()
                except OSError:
                    self.log_handle = None
        try:
            self.lines.put_nowait((timestamp, line))
        except queue.Full:
            try:
                self.lines.get_nowait()
                self.lines.put_nowait((timestamp, line))
            except (queue.Empty, queue.Full):
                pass
        self._detect_flags(line)
        if self.log_to_main:
            logging.info("[共享日志] %s", line)
        if self.callback:
            try:
                self.callback(timestamp, line)
            except Exception:
                logging.exception("共享日志回调异常")

    def _detect_flags(self, line):
        """标志检测全部由业务引擎注入的规则驱动，不硬编码设备关键字。"""
        with self._lock:
            for rule in self.flag_rules:
                if getattr(self, rule['attr'], False):
                    continue
                pattern = rule.get('pattern') or ''
                if not pattern:
                    continue
                try:
                    hit = (re.search(pattern, line) if rule.get('regex')
                           else pattern in line)
                except re.error:
                    hit = pattern in line
                if hit:
                    setattr(self, rule['attr'], True)
                    if rule.get('time_attr'):
                        setattr(self, rule['time_attr'], time.time())
                    logging.info("[共享日志] %s", rule.get('label') or
                                 f"检测到 {rule['attr']}")
            for pattern, attr in self.version_rules:
                if pattern and pattern in line:
                    setattr(self, attr, True)
            if not self.boot_detected and self.boot_keyword and \
                    self.boot_keyword.lower() in line.lower():
                self._boot_detect_count += 1
                if self._boot_detect_count >= max(1, self.boot_success_count):
                    self.boot_detected = True
                    self.boot_time = time.time() - self._start_time

    def reset_flags(self, start_time=None):
        with self._lock:
            self.boot_detected = False
            self.boot_time = 0.0
            self.boot_version_detected = False
            self.upgrade_version_detected = False
            self.upgrade_started = False
            self.upgrade_completed = False
            self.otfdec_error_detected = False
            self.upgrade_started_time = start_time or time.time()
            self.otfdec_error_time = 0.0
            self.sn_missing_detected = False
            self._boot_detect_count = 0
            self._start_time = start_time or time.time()

    def clear_received_lines(self):
        while True:
            try:
                self.lines.get_nowait()
            except queue.Empty:
                return

    def get_boot_status(self):
        with self._lock:
            return self.boot_detected, self.boot_time

    def get_boot_version_status(self):
        with self._lock:
            return self.boot_version_detected

    def get_upgrade_version_status(self):
        with self._lock:
            return self.upgrade_version_detected

    def get_upgrade_status(self):
        with self._lock:
            return self.upgrade_started, self.upgrade_completed

    def get_otfdec_error(self):
        with self._lock:
            return self.otfdec_error_detected

    def get_otfdec_error_during_upgrade(self):
        with self._lock:
            return (self.upgrade_started and self.otfdec_error_detected and
                    self.otfdec_error_time >= self.upgrade_started_time)

    def get_sn_missing_status(self):
        with self._lock:
            return self.sn_missing_detected

    def get_received_line(self, timeout=0.1):
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def write_bytes(self, data):
        payload = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else str(data)
        result = self.run_command(payload.rstrip("\r\n"), check=False)
        return bool(result and result.get("ok"))

    def send_command(self, cmd):
        result = self.run_command(cmd, check=False)
        return bool(result and result.get("ok"))

    def run_command(self, cmd, timeout=20, check=True):
        raise NotImplementedError

    def wait_for_pattern(self, pattern, timeout=20, use_regex=False, stop_check=None):
        if not pattern:
            return True, "", False
        matcher = re.compile(pattern).search if use_regex else None
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            if self.stop_event.is_set() or (stop_check is not None and not stop_check()):
                return False, "", True
            received = self.get_received_line(timeout=0.1)
            if not received:
                continue
            line = received[1]
            if matcher(line) if matcher else pattern in line:
                return True, line, False
        return False, "", self.stop_event.is_set()

    def close(self):
        pass


class SerialSession(BaseSession):
    def __init__(self, project, log_path, stop_event=None):
        super().__init__(log_path, stop_event)
        import serial
        self._serial_module = serial
        ports = project.get("serial_ports") or []
        if not ports or not str(ports[0].get("port") or "").strip():
            raise DeviceSessionError("串口日志通道未配置端口")
        self.port = str(ports[0].get("port"))
        self.baudrate = int(ports[0].get("baudrate") or 115200)
        self.ser = None
        self.thread = None
        self.running = False
        self._write_lock = threading.Lock()

    def start(self):
        if not self.open_log(f"串口 {self.port} @ {self.baudrate}"):
            return False
        try:
            self.ser = self._serial_module.Serial(self.port, self.baudrate, timeout=0.5)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception as exc:
            logging.error("共享串口打开失败 %s: %s", self.port, exc)
            self.ser = None
            self._close_log_handle()
            return False
        self.running = True
        self.reset_flags()
        self.thread = threading.Thread(target=self._reader, name="SharedSerialReader", daemon=True)
        self.thread.start()
        logging.info("共享串口日志已启动: %s @ %s", self.port, self.baudrate)
        return True

    def _reader(self):
        while self.running and not self.stop_event.is_set():
            try:
                if self.ser and self.ser.in_waiting:
                    raw = self.ser.readline()
                    if raw:
                        self.emit(raw.decode("utf-8", errors="ignore"))
                else:
                    time.sleep(0.02)
            except Exception as exc:
                if self.running:
                    logging.warning("共享串口读取异常: %s", exc)
                    time.sleep(0.2)

    def run_command(self, cmd, timeout=20, check=True):
        try:
            with self._write_lock:
                if not self.ser or not self.ser.is_open:
                    return {"ok": False, "output": "", "error": "串口未打开"}
                self.ser.write((str(cmd).rstrip("\r\n") + "\r\n").encode("utf-8"))
                self.ser.flush()
            return {"ok": True, "output": ""}
        except Exception as exc:
            return {"ok": False, "output": "", "error": str(exc)}

    def close(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self._close_log_handle()
        logging.info("共享串口日志已停止")


class MarkerShellSession(BaseSession):
    """ADB/SSH 持续 shell；日志命令和普通命令共用同一登录通道。"""

    marker_prefix = "__TOOLBOX_CMD_"

    def __init__(self, project, log_path, stop_event=None):
        super().__init__(log_path, stop_event)
        self.channel = "adb"
        self.log_command = str(project.get("log_command") or "logcat -v time").strip()
        self.project = project
        self.ready = False
        self.thread = None
        self.running = False
        self._cmd_lock = threading.Lock()
        self._counter = 0
        self._line_buffer = ""
        self._pending = {}

    def _read_stream(self):
        raise NotImplementedError

    def _write_raw(self, text):
        raise NotImplementedError

    def _start_reader(self, label):
        self.running = True
        self.thread = threading.Thread(
            target=self._read_stream, name=f"Shared{label}Reader", daemon=True)
        self.thread.start()

    def _start_log_command(self):
        if not self.log_command:
            self.ready = True
            return True
        # 后台运行日志命令，保持 shell 可交互；否则 logcat 等前台进程会阻塞
        # 后续版本检查/降级命令。
        self._write_raw("{ " + self.log_command + "; } &\n")
        self.ready = True
        return True

    def _handle_text(self, text):
        self._line_buffer += text
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            marker = self._consume_marker(line)
            if marker is None:
                self.emit(line)

    def _consume_marker(self, line):
        # shell 回显的命令行也含标记文本，先过滤，避免误判命令开始/结束。
        if ("echo " + self.marker_prefix) in line:
            return "echo"
        for command_id, state in list(self._pending.items()):
            end = f"{self.marker_prefix}{command_id}_END__:"
            if state.get("started") and not line.startswith(end):
                state.setdefault("output", []).append(line)
            start = f"{self.marker_prefix}{command_id}_START__"
            if state.get("started") is False and line.strip() == start:
                state["started"] = True
                return command_id
            if state.get("started") and line.startswith(end):
                try:
                    state["code"] = int(line[len(end):].strip() or "0")
                except ValueError:
                    state["code"] = 1
                state["output_text"] = "\n".join(state.get("output", []))
                state["event"].set()
                return command_id
        return None

    def run_command(self, cmd, timeout=20, check=True):
        with self._cmd_lock:
            command_id = self._counter + 1
            self._counter = command_id
            event = threading.Event()
            self._pending[command_id] = {"started": False, "event": event, "code": None}
            start = f"{self.marker_prefix}{command_id}_START__"
            end = f"{self.marker_prefix}{command_id}_END__:$?"
            try:
                if not self._write_raw(f"echo {start}; {cmd}; echo {end}\n"):
                    self._pending.pop(command_id, None)
                    return {"ok": False, "output": "",
                            "error": "设备连接已断开，正在自动重连"}
                if not event.wait(max(1, timeout)):
                    self._pending.pop(command_id, None)
                    return {"ok": False, "output": "", "error": "命令执行超时", "timeout": True}
                finished = self._pending.pop(command_id, {"code": 1})
                if finished.get("error"):
                    return {"ok": False, "output": "",
                            "error": finished["error"]}
                code = finished.get("code")
                code = 0 if code is None else code
                return {
                    "ok": code == 0 or not check, "code": code,
                    "output": (finished.get("output_text") or "").strip(),
                }
            except Exception as exc:
                self._pending.pop(command_id, None)
                return {"ok": False, "output": "", "error": str(exc)}

    def _fail_pending(self, reason):
        """连接断开时让等待中的命令立即返回，由重连线程恢复会话。"""
        for command_id, state in list(self._pending.items()):
            state["error"] = reason
            state["event"].set()
            self._pending.pop(command_id, None)

    def _restart_connection(self):
        raise NotImplementedError

    def _reconnect_loop(self):
        while self.running and not self.stop_event.is_set():
            time.sleep(2)
            if not self.running or self.stop_event.is_set():
                return
            try:
                logging.info("[共享日志] 连接断开，尝试自动重连...")
                self._restart_connection()
                logging.info("[共享日志] 重连成功，日志与命令通道已恢复")
                return
            except Exception as exc:
                logging.warning("[共享日志] 重连失败: %s", exc)

    def close_common(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self._close_log_handle()

    def _close_log_handle(self):
        if self.log_handle:
            try:
                self.log_handle.close()
            except Exception:
                pass
            self.log_handle = None


class AdbSession(MarkerShellSession):
    def __init__(self, project, log_path, stop_event=None):
        super().__init__(project, log_path, stop_event)
        self.channel = "adb"
        self.adb = str(project.get("adb_path") or "adb").strip() or "adb"
        self.serial = str(project.get("adb_serial") or "").strip()
        self.username = str(project.get("adb_username") or "")
        self.password = str(project.get("adb_password") or "")
        self.auth_enabled = bool(project.get("adb_auth_enabled") and self.username)
        self.proc = None

    def start(self):
        args = [self.adb]
        if self.serial:
            args += ["-s", self.serial]
        args += ["shell"]
        if not self.open_log(f"ADB {self.serial or '默认设备'}: {self.log_command}"):
            return False
        if not self._device_online():
            logging.error("ADB 设备未连接，无法启动共享日志会话")
            self._close_log_handle()
            return False
        try:
            self.proc = self._spawn_shell()
        except Exception as exc:
            logging.error("共享 ADB shell 启动失败: %s", exc)
            self._close_log_handle()
            return False
        self._login()
        self._start_reader("Adb")
        ok = self._start_log_command()
        if ok:
            logging.info("共享 ADB 日志已启动: %s", self.log_command)
        return ok

    def _shell_args(self):
        args = [self.adb]
        if self.serial:
            args += ["-s", self.serial]
        return args + ["shell"]

    def _device_online(self):
        args = [self.adb]
        if self.serial:
            args += ["-s", self.serial]
        args += ["get-state"]
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return result.returncode == 0 and "device" in (result.stdout or "")
        except Exception:
            return False

    def _spawn_shell(self):
        return subprocess.Popen(
            self._shell_args(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _login(self):
        if self.auth_enabled:
            self._write_raw(self.username + "\n")
            time.sleep(0.5)
            self._write_raw(self.password + "\n")
            time.sleep(0.5)

    def _read_stream(self):
        while self.running and self.proc and self.proc.poll() is None:
            try:
                text = self.proc.stdout.readline()
                if not text:
                    time.sleep(0.05)
                else:
                    self._handle_text(text)
            except Exception as exc:
                if self.running:
                    logging.warning("共享 ADB 读取异常: %s", exc)
                    time.sleep(0.2)
        if self.running and not self.stop_event.is_set():
            logging.warning("[共享日志] ADB 连接断开（设备可能正在重启）")
            self._fail_pending("ADB 连接断开，正在自动重连")
            self._reconnect_loop()

    def _restart_connection(self):
        if not self._device_online():
            raise DeviceSessionError("ADB 设备尚未上线")
        self._line_buffer = ""
        self.proc = self._spawn_shell()
        self._login()
        self._start_log_command()
        self._start_reader("Adb")

    def _write_raw(self, text):
        try:
            if not self.proc or self.proc.poll() is not None:
                return False
            self.proc.stdin.write(text)
            self.proc.stdin.flush()
            return True
        except Exception:
            return False

    def close(self):
        self.close_common()
        self._terminate_proc()
        logging.info("共享 ADB 日志已停止")

    def _terminate_proc(self):
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None


class SshSession(MarkerShellSession):
    def __init__(self, project, log_path, stop_event=None):
        super().__init__(project, log_path, stop_event)
        self.channel = "ssh"
        self.host = str(project.get("ssh_host") or "").strip()
        self.port = int(project.get("ssh_port") or 22)
        self.username = str(project.get("ssh_username") or "").strip()
        self.password = str(project.get("ssh_password") or "")
        self.client = None
        self.shell = None

    def start(self):
        if not self.host or not self.username:
            logging.error("共享 SSH 日志缺少主机或用户名")
            return False
        if not self.open_log(f"SSH {self.host}:{self.port}: {self.log_command}"):
            return False
        try:
            self._connect()
        except Exception as exc:
            logging.error("共享 SSH 连接失败 %s:%s - %s", self.host, self.port, exc)
            self._close_log_handle()
            return False
        self._start_reader("Ssh")
        ok = self._start_log_command()
        if ok:
            logging.info("共享 SSH 日志已启动: %s@%s", self.username, self.host)
        return ok

    def _connect(self):
        import paramiko
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            self.host, port=self.port, username=self.username,
            password=self.password, timeout=10, banner_timeout=10,
            auth_timeout=10, allow_agent=False, look_for_keys=False)
        self.shell = self.client.invoke_shell(width=240, height=5000)

    def _read_stream(self):
        while self.running and self.shell:
            try:
                if self.shell.recv_ready():
                    text = self.shell.recv(8192).decode("utf-8", errors="ignore")
                    if not text:
                        break
                    self._handle_text(text)
                else:
                    time.sleep(0.05)
            except Exception as exc:
                if self.running:
                    logging.warning("共享 SSH 读取异常: %s", exc)
                    break
        if self.running and not self.stop_event.is_set():
            logging.warning("[共享日志] SSH 连接断开（设备可能正在重启）")
            self._fail_pending("SSH 连接断开，正在自动重连")
            self._reconnect_loop()

    def _restart_connection(self):
        self._close_transport()
        self._line_buffer = ""
        self._connect()
        self._start_log_command()
        self._start_reader("Ssh")

    def _write_raw(self, text):
        try:
            if not self.shell:
                return False
            self.shell.send(text)
            return True
        except Exception:
            return False

    def close(self):
        self.close_common()
        self._close_transport()
        logging.info("共享 SSH 日志已停止")

    def _close_transport(self):
        for resource in (self.shell, self.client):
            if resource:
                try:
                    resource.close()
                except Exception:
                    pass
        self.shell = None
        self.client = None


class DeviceSessionManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._session = None
        self.project = None

    def describe(self):
        with self._lock:
            if not self._session:
                return None
            return {
                "channel": self._session.channel,
                "project": self.project,
                "session": self._session,
            }

    def is_active(self):
        return self.describe() is not None

    def validate(self, project):
        channel = str(project.get("log_channel") or "serial").lower()
        if channel not in LOG_CHANNELS:
            raise DeviceSessionError("日志通道无效")
        if channel == "adb":
            if not project.get("adb_enabled"):
                raise DeviceSessionError("项目未启用 ADB")
        elif channel == "ssh":
            if not project.get("ssh_enabled") or not str(project.get("ssh_host") or "").strip():
                raise DeviceSessionError("SSH 日志通道未启用或未配置主机")
            try:
                import paramiko  # noqa: F401
            except ImportError:
                raise DeviceSessionError("SSH 日志通道需要依赖 paramiko，请先安装")
        else:
            if not project.get("serial_enabled") or not (project.get("serial_ports") or []):
                raise DeviceSessionError("串口日志通道未启用或未配置端口")
        if project.get("msh_enabled") and channel != "serial":
            raise DeviceSessionError(
                "MSH 认证需要串口，请将日志通道设为串口或关闭 MSH 认证")

    def start(self, project, log_path, stop_event=None):
        with self._lock:
            if self._session:
                raise SessionBusyError("设备日志会话已启动")
            self.validate(project)
            channel = str(project.get("log_channel") or "serial").lower()
            cls = {"adb": AdbSession, "ssh": SshSession, "serial": SerialSession}[channel]
            session = cls(project, os.path.abspath(log_path), stop_event)
            if not session.start():
                session.close()
                raise DeviceSessionError(f"{CHANNEL_LABELS[channel]}日志启动失败")
            self._session = session
            self.project = project
            return session

    def stop(self):
        with self._lock:
            session, self._session, self.project = self._session, None, None
        if session:
            session.close()


manager = DeviceSessionManager()


class SharedSerialLoggerAdapter:
    """兼容开关机测试 SerialLogger 的共享会话适配器。"""

    def __init__(self, session, label="串口1", boot_keyword="",
                 boot_success_count=1, boot_log_to_main=True):
        self.session = session
        self.label = label
        self.boot_keyword = boot_keyword
        self.boot_success_count = boot_success_count
        self.boot_log_to_main = boot_log_to_main
        self.read_bytes = 0
        session.boot_keyword = boot_keyword
        session.boot_success_count = max(1, int(boot_success_count or 1))
        session.log_to_main = bool(boot_log_to_main)

    def open(self):
        # 串口已由共享会话打开，这里只确认状态，绝不二次打开。
        return bool(self.session.ser and self.session.ser.is_open)

    def start_reading(self):
        self.read_bytes = 1

    def reset_boot_detect(self):
        self.session.reset_flags()
        self.read_bytes = 1

    def is_boot_detected(self):
        return bool(self.boot_keyword and self.session.boot_detected)

    def get_boot_detect_info(self):
        return {
            "enabled": bool(self.boot_keyword),
            "detected": self.session.boot_detected,
            "count": self.session._boot_detect_count,
            "required": max(1, int(self.boot_success_count or 1)),
            "keyword": self.boot_keyword,
        }

    def stop(self):
        return None


class SharedAdbShellSession:
    """复用共享 ADB 会话执行命令，避免开关机测试另开 adb shell。"""

    def __init__(self, session):
        self.session = session

    def start(self, timeout=30):
        return True, "复用共享 ADB 会话", False

    def run_command(self, cmd, timeout=20):
        result = self.session.run_command(cmd, timeout=timeout, check=True)
        return bool(result.get('ok')), (result.get('output') or
                                        result.get('error') or '')

    def close(self):
        return None


def start_logging(project, log_path, stop_event=None):
    return manager.start(project, log_path, stop_event)


def stop_logging():
    manager.stop()


def active_session():
    return manager.describe()


def is_active():
    return manager.is_active()
