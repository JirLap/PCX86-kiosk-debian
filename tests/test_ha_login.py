"""Check sign-in isolation, credential handling and browser-profile activation."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import install
import setup_ha_login as login


class Wait:
    def __init__(self, driver, timeout=45):
        self.driver = driver

    def until(self, predicate):
        result = predicate(self.driver)
        if not result:
            raise TimeoutError('not ready')
        return result


class HaLoginTests(unittest.TestCase):
    def setUp(self):
        self.keys = ModuleType('selenium.webdriver.common.keys')
        self.keys.Keys = type('Keys', (), {'ENTER': '\ue007'})
        self.ui = ModuleType('selenium.webdriver.support.ui')
        self.ui.WebDriverWait = Wait
        self.modules = patch.dict(sys.modules, {
            'selenium.webdriver.common.keys': self.keys,
            'selenium.webdriver.support.ui': self.ui,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def driver(self, states=('signed_in',), remembered=True):
        driver = Mock(current_url='http://ha.example:8123/auth/authorize')
        driver.username = Mock()
        driver.password = Mock()
        driver.code = Mock()
        states = iter(states)

        def execute(script, *args):
            if script == login.FIND_INPUT:
                return getattr(driver, args[0])
            if script == login.LOGIN_STATE:
                return next(states)
            if 'checkbox' in script:
                return remembered
            if 'handler' in script:
                return True
            if 'hass.user.id' in script:
                return 'user-id'
            raise AssertionError('Unexpected script')

        driver.execute_script.side_effect = execute
        return driver

    def test_standard_login_and_remember_me(self):
        driver = self.driver()
        self.assertEqual(login.sign_in(driver, 'http://ha.example:8123/dashboard', 'panel', 'secret', Wait(driver)), 'user-id')
        driver.username.send_keys.assert_called_once_with('panel')
        self.assertEqual(driver.password.send_keys.call_args_list[0].args, ('secret',))

    def test_two_factor_code_is_entered_in_terminal(self):
        driver = self.driver(states=('mfa', 'signed_in'))
        with patch.object(login.getpass, 'getpass', return_value='123456'):
            login.sign_in(driver, 'http://ha.example:8123/', 'panel', 'secret', Wait(driver))
        self.assertEqual(driver.code.send_keys.call_args_list[0].args, ('123456',))

    def test_failed_login_or_mfa_selection_is_not_retried(self):
        for state in ('failed', 'select_mfa'):
            driver = self.driver(states=(state,))
            with self.subTest(state=state), self.assertRaises(login.LoginError):
                login.sign_in(driver, 'http://ha.example:8123/', 'panel', 'secret', Wait(driver))
            self.assertEqual(driver.password.send_keys.call_count, 2)

    def test_redirect_does_not_receive_credentials(self):
        driver = self.driver()
        driver.current_url = 'https://other.example/auth'
        with self.assertRaises(login.LoginError):
            login.sign_in(driver, 'http://ha.example:8123/', 'panel', 'secret', Wait(driver))
        driver.password.send_keys.assert_not_called()
        driver.username.send_keys.assert_not_called()

    def test_missing_remember_me_does_not_submit(self):
        driver = self.driver(remembered=False)
        with self.assertRaises(login.LoginError):
            login.sign_in(driver, 'http://ha.example:8123/', 'panel', 'secret', Wait(driver))
        driver.password.send_keys.assert_called_once_with('secret')

    def test_invalid_urls_rejected(self):
        for url in ('file:///tmp/test', 'http://user:secret@ha/', 'http:///missing', 'javascript:alert(1)'):
            with self.subTest(url=url), self.assertRaises(login.LoginError):
                login.validate_url(url)

    def test_activation_preserves_config_and_creates_backup_without_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            original = {'kiosk_url': 'http://ha/', 'mqtt_password': 'mqtt-only', 'camera': {'stream_url': 'rtsp://camera/'}, 'custom': 42}
            path.write_text(json.dumps(original))
            login.activate_profile(path, original, Path(directory) / 'profile')
            updated = json.loads(path.read_text())
            self.assertEqual(updated.pop('browser_profile'), str(Path(directory) / 'profile'))
            self.assertEqual(updated, original)
            self.assertEqual(json.loads(next(Path(directory).glob('*.bak')).read_text()), original)

    def test_concurrent_config_change_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text('{"custom": 2}')
            with self.assertRaises(login.LoginError):
                login.activate_profile(path, {'custom': 1}, Path(directory) / 'profile')
            self.assertEqual(json.loads(path.read_text()), {'custom': 2})

    def test_profile_activated_only_after_reopening_as_same_user(self):
        for persistent in (True, False):
            with self.subTest(persistent=persistent), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                path = home / 'config.json'
                path.write_text('{"kiosk_url": "http://ha.example:8123/"}')
                driver = self.driver(states=('signed_in' if persistent else 'waiting',))
                manager = Mock()
                manager.__enter__ = Mock(return_value=driver)
                manager.__exit__ = Mock(return_value=False)
                with patch.object(login.Path, 'home', return_value=home), patch.object(
                    login, 'browser', return_value=manager
                ) as launch, patch.object(login, 'sign_in', return_value='user-id'), patch.object(
                    login, 'ask', return_value='panel'
                ), patch.object(login.getpass, 'getpass', return_value='secret'), contextlib.redirect_stdout(io.StringIO()):
                    if persistent:
                        login.setup(path)
                    else:
                        with self.assertRaises(TimeoutError):
                            login.setup(path)
                saved = json.loads(path.read_text())
                self.assertEqual('browser_profile' in saved, persistent)
                self.assertEqual(launch.call_count, 2)
                self.assertEqual(len(list((home / '.local/share/pcx86-kiosk/browser-profiles').iterdir())), int(persistent))
                self.assertNotIn('secret', path.read_text())

    def test_optional_failure_does_not_abort_or_print_exception_contents(self):
        with patch.object(install.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['secret'])), contextlib.redirect_stdout(io.StringIO()) as output:
            install.install_browser_login(Path('config.json'))
        self.assertIn('Kiosk installation is prepared', output.getvalue())
        self.assertNotIn('secret', output.getvalue())


if __name__ == '__main__':
    unittest.main()
