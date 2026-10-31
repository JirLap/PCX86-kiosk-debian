"""Check installer Wi-Fi commands without modifying the host network."""

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('kiosk_install', Path(__file__).resolve().parents[1] / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
UUID = '12345678-abcd-1234-abcd-123456789abc'


class WifiSetupTests(unittest.TestCase):
    def exercise(self, *, previous='default', uuid=UUID, nmcli=True, fail=None, wireless=True):
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            self.assertIs(kwargs['check'], False)
            self.assertEqual(kwargs['timeout'], 20)
            if fail == 'timeout':
                raise subprocess.TimeoutExpired(command, 20)
            if fail and fail in command:
                return subprocess.CompletedProcess(command, 1, '', 'failure')
            if 'GENERAL.CON-UUID' in command:
                output = uuid
            elif 'show' in command:
                output = previous
            else:
                output = ''
            return subprocess.CompletedProcess(command, 0, output+'\n', '')
        def which(name):
            return {'nmcli':'/usr/bin/nmcli' if nmcli else None, 'iw':'/usr/sbin/iw'}.get(name)
        wireless_path = SimpleNamespace(parent=SimpleNamespace(name='wlan-test'), is_dir=lambda: True)
        net = SimpleNamespace(glob=lambda pattern: [wireless_path] if wireless else [])
        with patch.object(installer.shutil, 'which', side_effect=which), patch.object(installer.subprocess, 'run', side_effect=run), contextlib.redirect_stdout(io.StringIO()) as output:
            installer.configure_wifi_power_saving(net)
        # Network changes must never reactivate a profile or restart NM.
        self.assertFalse(any(item in ('up','down','reapply','systemctl') for cmd in commands for item in cmd))
        return commands, output.getvalue()

    def test_active_profile_is_saved_by_uuid_and_applied_without_reconnect(self):
        commands, output = self.exercise()
        self.assertIn(['sudo','/usr/bin/nmcli','--wait','15','connection','modify','uuid',UUID,'802-11-wireless.powersave','2'], commands)
        self.assertEqual(commands[-1], ['sudo','/usr/sbin/iw','dev','wlan-test','set','power_save','off'])
        self.assertIn('without disconnecting', output)

    def test_repeat_run_skips_saved_setting_but_applies_live(self):
        for value in ('2','disable'):
            with self.subTest(previous=value):
                commands, output = self.exercise(previous=value)
                self.assertFalse(any('modify' in command for command in commands))
                self.assertIn('already', output)
                self.assertIn('power_save', commands[-1])

    def test_disconnected_wifi_does_not_change_any_profile(self):
        commands, output = self.exercise(uuid='--')
        self.assertEqual(len(commands), 1)
        self.assertIn('No active', output)

    def test_ethernet_only_machine_is_skipped(self):
        commands, output = self.exercise(wireless=False)
        self.assertEqual(commands, [])
        self.assertIn('No active', output)

    def test_networkmanager_missing_does_not_modify_network(self):
        commands, output = self.exercise(nmcli=False)
        self.assertEqual(commands, [])
        self.assertIn('unavailable', output)

    def test_profile_write_failure_is_reported_and_no_live_change_is_attempted(self):
        commands, output = self.exercise(fail='modify')
        self.assertNotIn('power_save', commands[-1])
        self.assertIn('WARNING', output)

    def test_live_failure_does_not_claim_success(self):
        commands, output = self.exercise(fail='power_save')
        self.assertIn('modify', commands[-2])
        self.assertIn('driver did not apply', output)
        self.assertNotIn('disabled now', output)

    def test_timeout_warns_without_changing_the_network(self):
        commands, output = self.exercise(fail='timeout')
        self.assertEqual(len(commands), 1)
        self.assertIn('timed out', output)


if __name__ == '__main__':
    unittest.main()
