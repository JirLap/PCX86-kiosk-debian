"""Verify per-user locker overrides, backups and repeat setup."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import configure_desktop as desktop


class DesktopSetupTests(unittest.TestCase):
    def test_locking_disabled_for_kiosk_user_with_backups_and_repeat_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            original = home / '.config/autostart/light-locker.desktop'
            original.parent.mkdir(parents=True)
            original.write_text('[Desktop Entry]\nExec=light-locker\n')
            xfconf = {(channel, prop): 'true' for channel, prop, _ in desktop.XFCONF_SETTINGS}
            settings = {'lock-after-screensaver': 'uint32 5', 'lock-on-suspend': 'true', 'lock-on-lid': 'false'}
            commands = []

            def run(args):
                commands.append(args)
                value = ''
                if args[0] == 'xfconf-query':
                    key = (args[2], args[4])
                    if '-s' in args:
                        xfconf[key] = args[args.index('-s') + 1]
                    else:
                        value = xfconf[key]
                elif args[0] == 'gsettings':
                    if args[1] == 'list-schemas':
                        value = 'apps.light-locker\n'
                    elif args[1] == 'get':
                        value = settings[args[3]]
                    else:
                        settings[args[3]] = args[4]
                return subprocess.CompletedProcess(args, 0, value, '')

            with patch.object(desktop.shutil, 'which', side_effect=lambda name: name), patch.object(
                desktop, 'command', side_effect=run
            ), patch.object(desktop.os, 'getuid', return_value=1000, create=True), contextlib.redirect_stdout(io.StringIO()):
                desktop.disable_locking(home)
                snapshot = home / '.config/pcx86-kiosk/desktop-lock-settings.before.json'
                before = snapshot.read_text()
                commands.clear()
                desktop.disable_locking(home)
            self.assertIn('Hidden=true', original.read_text())
            backups = list(original.parent.glob('light-locker.desktop.*.bak'))
            self.assertEqual(len(backups), 1)
            self.assertIn('Exec=light-locker', backups[0].read_text())
            self.assertEqual(snapshot.read_text(), before)
            self.assertEqual(json.loads(before)['apps.light-locker:lock-after-screensaver'], 'uint32 5')
            self.assertFalse(any('-s' in command or 'set' in command for command in commands))
            self.assertTrue(all(value == 'false' for value in xfconf.values()))
            for args in commands:
                if args[0] == 'pkill':
                    self.assertEqual(args[1:3], ['-u', '1000'])
            self.assertFalse(any('sudo' in args for args in commands))

    def test_missing_desktop_tools_still_masks_future_locker_autostart(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(desktop.shutil, 'which', return_value=None), contextlib.redirect_stdout(io.StringIO()):
            home = Path(directory)
            desktop.disable_locking(home)
            for name in desktop.LOCKERS:
                self.assertIn('Hidden=true', (home / '.config/autostart' / (name + '.desktop')).read_text())

    def test_missing_xfconf_properties_are_created_as_booleans(self):
        commands = []
        def run(args):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0 if '-s' in args else 1, '', '')
        with tempfile.TemporaryDirectory() as directory, patch.object(desktop.shutil, 'which', side_effect=lambda name: name if name == 'xfconf-query' else None), patch.object(desktop, 'command', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            desktop.disable_locking(Path(directory))
        writes = [args for args in commands if '-s' in args]
        self.assertEqual(len(writes), len(desktop.XFCONF_SETTINGS))
        self.assertTrue(all(args[-3:] == ['--create', '--type', 'bool'] for args in writes))
