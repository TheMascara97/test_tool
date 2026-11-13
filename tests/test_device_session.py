# -*- coding: utf-8 -*-
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import device_session
import gui_common as gc


PROJECT = {
    "adb_enabled": True,
    "adb_path": "adb",
    "serial_enabled": True,
    "serial_ports": [{"port": "COM101", "baudrate": 115200}],
    "ssh_enabled": True,
    "ssh_host": "192.168.1.10",
    "ssh_username": "root",
}


class DeviceSessionConfigTests(unittest.TestCase):
    def test_default_and_invalid_log_channel(self):
        project = gc.normalize_project({"name": "p"})
        self.assertEqual(project["log_channel"], "serial")
        project["log_channel"] = "TELNET"
        self.assertEqual(gc.normalize_project(project)["log_channel"], "serial")

    def test_validation_requires_selected_channel(self):
        manager = device_session.DeviceSessionManager()
        project = dict(PROJECT, log_channel="adb", log_command="logcat")
        manager.validate(project)
        with self.assertRaises(device_session.DeviceSessionError):
            manager.validate(dict(project, adb_enabled=False))
        no_host = dict(project, log_channel="ssh")
        no_host.pop("ssh_host")
        with self.assertRaises(device_session.DeviceSessionError):
            manager.validate(no_host)


class SharedSessionTests(unittest.TestCase):
    def test_manager_blocks_second_session(self):
        manager = device_session.DeviceSessionManager()
        manager._session = type("Fake", (), {"channel": "adb"})()
        with self.assertRaises(device_session.SessionBusyError):
            manager.start(PROJECT, "unused.log")
        manager._session.close = lambda: None
        manager.stop()
        self.assertFalse(manager.is_active())

    def test_marker_shell_captures_command_output(self):
        class FakeShell(device_session.MarkerShellSession):
            def _write_raw(self, text):
                if "_START__" in text:
                    time.sleep(0.02)
                    self._handle_text("__TOOLBOX_CMD_1_START__\nrollback ok\n")
                    self._handle_text("__TOOLBOX_CMD_1_END__:0\n")
                return True

        with tempfile.TemporaryDirectory() as temp:
            session = FakeShell(PROJECT, str(Path(temp) / "device.log"))
            result = session.run_command("rollback", timeout=2, check=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "rollback ok")
        self.assertEqual(result["code"], 0)

    def test_shared_adapter_reports_boot_keyword(self):
        with tempfile.TemporaryDirectory() as temp:
            session = device_session.SerialSession(
                PROJECT, str(Path(temp) / "device.log"))
            adapter = device_session.SharedSerialLoggerAdapter(
                session, boot_keyword="BOOT_OK", boot_success_count=2)
            session.emit("first BOOT_OK")
            self.assertFalse(adapter.is_boot_detected())
            session.emit("second BOOT_OK")
            self.assertTrue(adapter.is_boot_detected())

    def test_adapter_open_does_not_reopen_serial(self):
        with tempfile.TemporaryDirectory() as temp:
            session = device_session.SerialSession(
                PROJECT, str(Path(temp) / "device.log"))
            session.ser = type("FakeSerial", (), {"is_open": True})()
            adapter = device_session.SharedSerialLoggerAdapter(session)
            self.assertTrue(adapter.open())
            session.ser = None
            self.assertFalse(adapter.open())

    def test_log_command_runs_in_background(self):
        written = []

        class FakeShell(device_session.MarkerShellSession):
            def _write_raw(self, text):
                written.append(text)
                return True

        with tempfile.TemporaryDirectory() as temp:
            session = FakeShell(
                dict(PROJECT, log_command="logcat -v time"),
                str(Path(temp) / "device.log"))
            self.assertTrue(session._start_log_command())
        self.assertEqual(written, ["{ logcat -v time; } &\n"])

    def test_command_echo_is_not_parsed_as_marker(self):
        class FakeShell(device_session.MarkerShellSession):
            def _write_raw(self, text):
                # 模拟 shell 回显整条命令行，然后才输出真实标记。
                self._handle_text(text)
                self._handle_text("__TOOLBOX_CMD_1_START__\nhello\n")
                self._handle_text("__TOOLBOX_CMD_1_END__:0\n")
                return True

        with tempfile.TemporaryDirectory() as temp:
            session = FakeShell(PROJECT, str(Path(temp) / "device.log"))
            result = session.run_command("echo hi", timeout=2, check=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "hello")

    def test_shared_adb_shell_reuses_session(self):
        session = mock.Mock()
        session.run_command.return_value = {"ok": True, "output": "1.2.3"}
        shell = device_session.SharedAdbShellSession(session)
        ok, msg, cancelled = shell.start()
        self.assertTrue(ok)
        self.assertFalse(cancelled)
        ok, out = shell.run_command("getprop ro.build.version", timeout=5)
        self.assertTrue(ok)
        self.assertEqual(out, "1.2.3")
        session.run_command.assert_called_once_with(
            "getprop ro.build.version", timeout=5, check=True)
        shell.close()

    def test_msh_requires_serial_channel(self):
        manager = device_session.DeviceSessionManager()
        project = dict(PROJECT, log_channel="adb", log_command="logcat",
                       msh_enabled=True)
        with self.assertRaises(device_session.DeviceSessionError):
            manager.validate(project)
        project["log_channel"] = "serial"
        manager.validate(project)

    def test_adb_start_fails_when_device_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            session = device_session.AdbSession(
                dict(PROJECT, log_channel="adb", log_command="logcat"),
                str(Path(temp) / "device.log"))
            with mock.patch.object(session, "_device_online", return_value=False):
                self.assertFalse(session.start())
            self.assertIsNone(session.log_handle)

    def test_dead_connection_command_fails_fast(self):
        class FakeShell(device_session.MarkerShellSession):
            def _write_raw(self, text):
                return False

        with tempfile.TemporaryDirectory() as temp:
            session = FakeShell(PROJECT, str(Path(temp) / "device.log"))
            result = session.run_command("reboot", timeout=2, check=False)
        self.assertFalse(result["ok"])
        self.assertIn("重连", result["error"])

    def test_reconnect_loop_restores_connection(self):
        attempts = []

        class FakeShell(device_session.MarkerShellSession):
            def _restart_connection(self):
                attempts.append(1)
                if len(attempts) < 2:
                    raise device_session.DeviceSessionError("设备尚未上线")

        with tempfile.TemporaryDirectory() as temp:
            session = FakeShell(PROJECT, str(Path(temp) / "device.log"))
            session.running = True
            with mock.patch.object(device_session.time, "sleep"):
                session._reconnect_loop()
        self.assertEqual(len(attempts), 2)

    def test_reconnect_loop_stops_when_session_closed(self):
        class FakeShell(device_session.MarkerShellSession):
            def _restart_connection(self):
                raise AssertionError("不应在停止后重连")

        with tempfile.TemporaryDirectory() as temp:
            session = FakeShell(PROJECT, str(Path(temp) / "device.log"))
            session.running = True
            session.stop_event.set()
            with mock.patch.object(device_session.time, "sleep"):
                session._reconnect_loop()


if __name__ == "__main__":
    unittest.main()
