"""Exercise fresh/repeat setup without running Debian system commands."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install


class KioskInstallTests(unittest.TestCase):
    def test_setup_automatically_uses_basic_and_preserves_other_settings(self):
        for previous in (None, 'auto', 'basic'):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                config_path = home / '.config/pcx86-kiosk/config.json'
                original = {
                    'device_id': 'panel-test', 'mqtt_host': '192.0.2.10',
                    'mqtt_port': 1883, 'mqtt_username': 'panel',
                    'mqtt_password': 'test-only', 'kiosk_url': 'http://192.0.2.10:8123',
                    'camera': {'snapshot_url': None, 'stream_url': None},
                    'screensaver_enabled': True, 'screensaver_timeout': 17,
                    'custom_setting': 'preserved',
                }
                if previous is not None:
                    original['browser_password_store'] = previous
                    config_path.parent.mkdir(parents=True)
                    config_path.write_text(json.dumps(original))

                # A separate old launcher must also be disabled on migration.
                legacy = home / '.config/autostart/chromium.desktop'
                legacy.parent.mkdir(parents=True, exist_ok=True)
                legacy.write_text('Exec=chromium --kiosk http://192.0.2.10:8123\n')
                prompts = []

                def ask(label, default=''):
                    prompts.append(label)
                    if label.startswith(('Enable automatic', 'Install/update')):
                        return 'n'
                    return str(original.get({
                        'MQTT device ID': 'device_id',
                        'MQTT broker address': 'mqtt_host',
                        'MQTT username': 'mqtt_username',
                        'Initial kiosk URL': 'kiosk_url',
                    }.get(label, ''), default))

                real_exists = Path.exists
                real_mkstemp = tempfile.mkstemp

                def mkstemp(*args, **kwargs):
                    # The installer uses only the path. Close its unused descriptor
                    # here so Windows can unlink the file as Debian would.
                    descriptor, name = real_mkstemp(*args, **kwargs)
                    os.close(descriptor)
                    return descriptor, name

                def exists(path):
                    if path == Path('/etc/debian_version'):
                        return True
                    return real_exists(path)

                with patch.object(install.os, 'geteuid', return_value=1000, create=True), patch.object(
                    install.Path, 'home', return_value=home
                ), patch.object(install.Path, 'exists', exists), patch.object(
                    install.getpass, 'getuser', return_value='panel'
                ), patch.object(install.getpass, 'getpass', return_value='test-only' if previous is None else ''), patch.object(
                    install.shutil, 'which', return_value='/usr/bin/sudo'
                ), patch.object(install, 'ask', side_effect=ask), patch.object(
                    install.subprocess, 'run'
                ), patch.object(install, 'configure_wifi_power_saving'), patch.object(
                    install, 'install_file_with_sudo'
                ), patch.object(install.tempfile, 'mkstemp', side_effect=mkstemp
                ), contextlib.redirect_stdout(io.StringIO()):
                    install.main()

                saved = json.loads(config_path.read_text())
                self.assertEqual(saved['browser_password_store'], 'basic')
                self.assertFalse(any('password store' in label for label in prompts))
                self.assertFalse(legacy.exists())
                launcher = (home / '.local/lib/pcx86-kiosk/start-agent.sh').read_text()
                self.assertLess(launcher.index('configure_desktop.py'), launcher.index('exec /usr/bin/python3'))
                self.assertTrue((home / '.local/lib/pcx86-kiosk/configure_desktop.py').exists())
                self.assertEqual(len(list(legacy.parent.glob('chromium.desktop.*.bak'))), 1)
                if previous is not None:
                    for key, value in original.items():
                        if key != 'browser_password_store':
                            self.assertEqual(saved[key], value)
                    backups = list(config_path.parent.glob('config.json.*.bak'))
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(json.loads(backups[0].read_text()), original)


if __name__ == '__main__':
    unittest.main()
