# -*- coding: utf-8 -*-
import unittest
from unittest import mock

import NASH_FOTA


class DowngradeModeTests(unittest.TestCase):
    def make_executor(self):
        executor = NASH_FOTA.DowngradeExecutor("unused.log")
        executor._log = mock.Mock()
        executor._write_status = mock.Mock()
        return executor

    def test_dispatch_keeps_legacy_jflash_mode(self):
        executor = self.make_executor()
        with mock.patch.object(NASH_FOTA, "DOWNGRADE_MODE", "jflash"), \
                mock.patch.object(
                    executor, "execute_jflash_downgrade",
                    return_value=(True, "jflash", False)) as execute:
            self.assertEqual(executor.execute_downgrade(), (True, "jflash", False))
        execute.assert_called_once_with()

    def test_power_cycle_mode_requires_relay(self):
        executor = self.make_executor()
        with mock.patch.object(NASH_FOTA, "DOWNGRADE_MODE", "power_cycle"):
            ok, detail, cancelled = executor.execute_downgrade()
        self.assertFalse(ok)
        self.assertIn("继电器", detail)
        self.assertFalse(cancelled)

    def test_power_cycle_downgrade_succeeds(self):
        relay = mock.Mock()
        relay.power_cycle.return_value = (True, False)
        executor = self.make_executor()
        executor.relay = relay
        with mock.patch.object(NASH_FOTA, "DOWNGRADE_MODE", "power_cycle"), \
                mock.patch.object(NASH_FOTA, "DOWNGRADE_MAX_ROUNDS", 1), \
                mock.patch.object(NASH_FOTA, "DOWNGRADE_TRIGGER_BOOT_WAIT", 0):
            ok, detail, cancelled = executor.execute_downgrade()
        self.assertTrue(ok)
        self.assertFalse(cancelled)
        relay.power_cycle.assert_called_once_with()

    def test_adb_command_downgrade_succeeds(self):
        executor = self.make_executor()
        executor.owner = mock.Mock()
        with mock.patch.object(NASH_FOTA, "DOWNGRADE_MODE", "command"), \
                mock.patch.object(NASH_FOTA, "DOWNGRADE_TRIGGER_CHANNEL", "adb"), \
                mock.patch.object(NASH_FOTA, "DOWNGRADE_TRIGGER_CMD", "reboot"), \
                mock.patch.object(NASH_FOTA, "DOWNGRADE_TRIGGER_BOOT_WAIT", 0), \
                mock.patch.object(
                    executor, "_run_adb_downgrade_command",
                    return_value=(True, "rebooting", False)) as run_cmd, \
                mock.patch.object(NASH_FOTA, "sleep_interruptible",
                                  return_value=False):
            ok, detail, cancelled = executor.execute_downgrade()
        self.assertTrue(ok)
        self.assertEqual(detail, "rebooting")
        run_cmd.assert_called_once_with("reboot", check_expect=False)

    def test_adb_commands_execute_each_line_in_order(self):
        executor = self.make_executor()
        executor.owner = mock.Mock()
        results = [(True, "one", False), (True, "two", False)]
        with mock.patch.object(NASH_FOTA, "DOWNGRADE_MODE", "command"), \
                mock.patch.object(NASH_FOTA, "DOWNGRADE_TRIGGER_CHANNEL", "adb"), \
                mock.patch.object(NASH_FOTA, "DOWNGRADE_TRIGGER_CMD",
                                  "cmd-one\ncmd-two"), \
                mock.patch.object(NASH_FOTA, "DOWNGRADE_TRIGGER_BOOT_WAIT", 0), \
                mock.patch.object(executor, "_run_adb_downgrade_command",
                                  side_effect=results) as run_cmd:
            ok, detail, cancelled = executor.execute_downgrade()
        self.assertTrue(ok)
        self.assertEqual(detail, "one\ntwo")
        self.assertEqual(run_cmd.call_args_list, [
            mock.call("cmd-one", check_expect=False),
            mock.call("cmd-two", check_expect=False),
        ])

    def test_serial_command_validates_expected_output(self):
        monitor = mock.Mock()
        monitor.send_command.return_value = True
        monitor.wait_for_pattern.return_value = (True, "DOWNGRADE_OK", False)
        owner = mock.Mock(serial_monitor=monitor)
        executor = self.make_executor()
        executor.owner = owner
        with mock.patch.object(NASH_FOTA, "DOWNGRADE_TRIGGER_CHANNEL", "serial"), \
                mock.patch.object(NASH_FOTA, "DOWNGRADE_TRIGGER_EXPECT",
                                  "DOWNGRADE_OK"):
            ok, detail, cancelled = executor._run_downgrade_command("rollback")
        self.assertTrue(ok)
        self.assertEqual(detail, "DOWNGRADE_OK")
        monitor.send_command.assert_called_once_with("rollback")
        monitor.wait_for_pattern.assert_called_once()


class DowngradeGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_mode_and_channel_config_round_trip(self):
        from gui_fota import FotaPage
        page = FotaPage()
        try:
            page.combo_downgrade_mode.setCurrentIndex(
                page.combo_downgrade_mode.findData("power_cycle"))
            page._update_all_deps()
            self.assertTrue(page.grp_downgrade_jflash.isHidden())
            self.assertTrue(page.grp_downgrade_command.isHidden())

            page.combo_downgrade_mode.setCurrentIndex(
                page.combo_downgrade_mode.findData("command"))
            page.combo_downgrade_channel.setCurrentIndex(
                page.combo_downgrade_channel.findData("adb"))
            page._update_all_deps()
            for widget in [
                    page.combo_downgrade_channel,
                    page.edit_downgrade_cmd,
                    page.edit_downgrade_expect,
                    page.spin_downgrade_trigger_timeout,
                    page.edit_downgrade_adb_path,
                    page.edit_downgrade_adb_serial]:
                self.assertFalse(widget.isHidden())
            self.assertTrue(page.edit_downgrade_ssh_host.isHidden())
            self.assertTrue(page.grp_downgrade_command.layout().labelForField(
                page.edit_downgrade_ssh_host).isHidden())

            page.combo_downgrade_channel.setCurrentIndex(
                page.combo_downgrade_channel.findData("ssh"))
            page.edit_downgrade_cmd.setPlainText("rollback now")
            page._update_all_deps()
            self.assertFalse(page.grp_downgrade_command.isHidden())
            self.assertTrue(page.edit_downgrade_adb_path.isHidden())
            self.assertFalse(page.edit_downgrade_ssh_host.isHidden())

            cfg = page.collect()
            self.assertEqual(cfg["DOWNGRADE_MODE"], "command")
            self.assertEqual(cfg["DOWNGRADE_TRIGGER_CHANNEL"], "ssh")
            self.assertEqual(cfg["DOWNGRADE_TRIGGER_CMD"], "rollback now")

            page.combo_downgrade_mode.setCurrentIndex(
                page.combo_downgrade_mode.findData("jflash"))
            page.combo_downgrade_channel.setCurrentIndex(
                page.combo_downgrade_channel.findData("adb"))
            page.apply_config(cfg)
            self.assertEqual(page.combo_downgrade_mode.currentData(), "command")
            self.assertEqual(page.combo_downgrade_channel.currentData(), "ssh")
            self.assertEqual(page.edit_downgrade_cmd.toPlainText(), "rollback now")
        finally:
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
