# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gui_common as gc


PROJECT = {
    "name": "integration",
    "adb_enabled": True,
    "adb_path": r"C:\tools\adb.exe",
    "adb_serial": "IT-001",
    "adb_auth_enabled": True,
    "adb_username": "tester",
    "adb_password": "secret",
    "adb_login_timeout": 27,
    "serial_enabled": True,
    "serial_ports": [
        {
            "name": "debug",
            "port": "COM101",
            "baudrate": 57600,
            "boot_detect": True,
            "boot_keyword": "BOOT_OK",
            "boot_success_count": 3,
        },
        {
            "name": "second",
            "port": "COM102",
            "baudrate": 57600,
            "boot_detect": False,
            "boot_keyword": "SECOND_OK",
        },
    ],
    "boot_detect_enabled": True,
    "boot_timeout": 222,
    "boot_keyword": "BOOT_OK",
    "msh_enabled": True,
    "msh_pem": r"C:\keys\msh.pem",
    "msh_seed_pattern": r"SEED:([0-9a-f]+)",
    "msh_post_auth_cmd": "auth ok",
    "msh_timeout": 29,
    "msh_success_flag": "MSH_OK",
    "msh_ready_wait": 7,
    "msh_fail_flags": "MSH_FAIL",
}


class ConfigBackupTests(unittest.TestCase):
    def test_invalid_config_is_backed_up_before_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / gc.CFG_FILENAME).write_text("{ broken", encoding="utf-8")
            with mock.patch.object(gc, "app_dir", return_value=str(root)):
                with self.assertLogs(level="ERROR"):
                    self.assertEqual(gc.load_gui_config(), {})
                backups = list(root.glob("gui_config.corrupt_*.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(encoding="utf-8"), "{ broken")

    def test_missing_config_has_no_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(gc, "app_dir", return_value=str(root)):
                self.assertEqual(gc.load_gui_config(), {})
                self.assertEqual(list(root.glob("gui_config.corrupt_*.json")), [])

    def test_non_object_config_is_backed_up(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = json.dumps([1, 2])
            (root / gc.CFG_FILENAME).write_text(payload, encoding="utf-8")
            with mock.patch.object(gc, "app_dir", return_value=str(root)):
                with self.assertLogs(level="ERROR"):
                    self.assertEqual(gc.load_gui_config(), {})
                self.assertEqual(len(list(root.glob("gui_config.corrupt_*.json"))), 1)


class PageProjectRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_reboot_page_follows_active_project(self):
        from gui_reboot import RebootPage
        page = RebootPage()
        try:
            with mock.patch.object(gc, "get_active_project", return_value=PROJECT):
                page.refresh_project()
            self.assertEqual(page.combo_port_count.currentIndex(), 2)
            self.assertEqual(page.combo_com1.currentText(), "COM101")
            self.assertEqual(page.combo_com2.currentText(), "COM102")
            self.assertEqual(page.spin_baudrate.value(), 57600)
            self.assertTrue(page.chk_detect_p1.isChecked())
            self.assertEqual(page.edit_kw_p1.text(), "BOOT_OK")
            self.assertEqual(page.spin_count_p1.value(), 3)
            self.assertEqual(page.spin_boot_timeout.value(), 222)
            self.assertTrue(page.chk_adb.isChecked())
            self.assertEqual(page.edit_adb_path.text(), PROJECT["adb_path"])
            self.assertEqual(page.edit_adb_serial.text(), PROJECT["adb_serial"])
            self.assertEqual(page.edit_adb_user.text(), PROJECT["adb_username"])
            self.assertEqual(page.edit_adb_pass.text(), PROJECT["adb_password"])
            self.assertEqual(page.spin_adb_timeout.value(), 27)
        finally:
            page.deleteLater()

    def test_fota_page_follows_active_project(self):
        from gui_fota import FotaPage
        page = FotaPage()
        try:
            with mock.patch.object(gc, "get_active_project", return_value=PROJECT):
                page.refresh_project()
            self.assertEqual(page.combo_dut_port.currentText(), "COM101")
            self.assertEqual(page.spin_baud.value(), 57600)
            self.assertEqual(page.edit_boot_flag.text(), "BOOT_OK")
            self.assertEqual(page.spin_boot_timeout.value(), 222)
            self.assertTrue(page.chk_msh_enabled.isChecked())
            self.assertEqual(page.edit_pem.text(), PROJECT["msh_pem"])
            self.assertEqual(page.edit_seed_pattern.text(), PROJECT["msh_seed_pattern"])
            self.assertEqual(page.edit_msh_ok_flag.text(), PROJECT["msh_success_flag"])
            self.assertEqual(page.edit_check_adb_path.text(), PROJECT["adb_path"])
            self.assertEqual(page.edit_check_adb_serial.text(), PROJECT["adb_serial"])
        finally:
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
