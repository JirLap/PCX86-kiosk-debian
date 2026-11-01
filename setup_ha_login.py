#!/usr/bin/python3
"""One-time HA browser sign-in using credentials entered only in the terminal."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from install import ask, read_existing_config, unique_backup_path


# HA renders login inputs inside web components. Return only visible native
# inputs; no password, page HTML, screenshots or tokens are logged/exported.
FIND_INPUT = """
function find(root) {
  for (const el of root.querySelectorAll('*')) {
    if (el.matches('input') && el.name === argumentsName &&
        el.getClientRects().length && !el.disabled) return el;
    if (el.shadowRoot) { const match = find(el.shadowRoot); if (match) return match; }
  }
  return null;
}
const argumentsName = arguments[0];
return find(document);
"""

LOGIN_STATE = """
const app = document.querySelector('home-assistant');
if (app && app.hass && app.hass.user && app.hass.connection &&
    app.hass.connection.connected) return 'signed_in';
const flow = document.querySelector('ha-auth-flow');
if (flow && flow.step) {
  if (flow.step.type === 'abort' ||
      (flow.step.errors && Object.keys(flow.step.errors).length)) return 'failed';
  if (flow.step.step_id === 'mfa') return 'mfa';
  if (flow.step.step_id === 'select_mfa_module') return 'select_mfa';
}
return 'waiting';
"""


class LoginError(Exception):
    """A safe, credential-free message for the installer."""


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise LoginError('Use an http(s) HA URL without credentials in the URL.')


def require_origin(driver, url: str) -> None:
    def origin(value):
        parsed = urlsplit(value)
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)
    if origin(driver.current_url) != origin(url):
        raise LoginError('HA redirected to a different origin. No credentials were sent; use the final direct HA URL.')


def browser(profile: Path, binary: str):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    options = webdriver.ChromeOptions()
    options.binary_location = binary
    for argument in ('--headless=new', '--password-store=basic', '--no-first-run',
                     '--no-default-browser-check', '--window-size=1280,900',
                     '--user-data-dir=' + str(profile)):
        options.add_argument(argument)
    options.add_experimental_option('prefs', {
        'credentials_enable_service': False, 'profile.password_manager_enabled': False,
    })
    # Use Debian's matching driver, never a downloaded Selenium Manager driver.
    driver = webdriver.Chrome(service=Service('/usr/bin/chromedriver', log_output=subprocess.DEVNULL), options=options)
    driver.set_page_load_timeout(45)
    return driver


def sign_in(driver, url: str, username: str, password: str, wait):
    from selenium.webdriver.common.keys import Keys

    driver.get(url)
    user_field = wait.until(lambda current: current.execute_script(FIND_INPUT, 'username'))
    password_field = wait.until(lambda current: current.execute_script(FIND_INPUT, 'password'))
    require_origin(driver, url)
    if not driver.execute_script("const flow = document.querySelector('ha-auth-flow'); return flow?.step?.handler?.[0] === 'homeassistant';"):
        raise LoginError('Only the standard Home Assistant username/password provider is supported.')
    user_field.send_keys(username)
    password_field.send_keys(password)
    # Select HA's own Remember me checkbox rather than injecting browser tokens.
    remembered = driver.execute_script("""
      const checkbox = document.querySelector('ha-auth-flow ha-checkbox');
      if (!checkbox) return false;
      if (!checkbox.checked) {
        const input = checkbox.shadowRoot?.querySelector('input');
        if (input) input.click(); else checkbox.click();
      }
      return checkbox.checked;
    """)
    if not remembered:
        raise LoginError('HA did not offer Remember me; existing kiosk login was left unchanged.')
    password_field.send_keys(Keys.ENTER)
    state = wait.until(lambda current: (value if (value := current.execute_script(LOGIN_STATE)) != 'waiting' else False))
    if state == 'mfa':
        code = getpass.getpass('Home Assistant two-factor code (hidden): ')
        field = wait.until(lambda current: current.execute_script(FIND_INPUT, 'code'))
        require_origin(driver, url)
        field.send_keys(code)
        field.send_keys(Keys.ENTER)
        # The old MFA step can remain briefly while its request is pending.
        state = wait.until(lambda current: (value if (value := current.execute_script(LOGIN_STATE)) in ('signed_in', 'failed') else False))
    if state != 'signed_in':
        raise LoginError('HA rejected the login or requires an unsupported sign-in step. Existing kiosk login was left unchanged.')
    return driver.execute_script("return document.querySelector('home-assistant').hass.user.id;")


def activate_profile(config_path: Path, original: dict, profile: Path) -> None:
    """Change only browser profile after persistent sign-in has been verified."""
    if read_existing_config(config_path) != original:
        raise LoginError('Kiosk settings changed during sign-in. Retry setup; no profile was activated.')
    backup = unique_backup_path(config_path)
    shutil.copy2(config_path, backup)
    os.chmod(backup, 0o600)
    updated = dict(original, browser_profile=str(profile))
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=config_path.parent,
                                     prefix='.login-', delete=False) as file:
        json.dump(updated, file, indent=2)
        file.write('\n')
        temporary = Path(file.name)
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(config_path)
    finally:
        temporary.unlink(missing_ok=True)


def setup(config_path: Path) -> None:
    from selenium.webdriver.support.ui import WebDriverWait

    config = read_existing_config(config_path)
    url = config['kiosk_url']
    validate_url(url)
    print('Signing in to the configured kiosk URL using a separate browser profile.')
    print('For http:// URLs, credentials travel without TLS on your local network.')
    username = ask('Home Assistant username (not the MQTT username)')
    password = getpass.getpass('Home Assistant password (hidden; not saved by this installer): ')
    if not username or not password:
        raise LoginError('Empty credentials: browser sign-in skipped.')
    base = Path.home() / '.local/share/pcx86-kiosk/browser-profiles'
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    profile = Path(tempfile.mkdtemp(prefix='ha-', dir=base))
    activated = False
    try:
        with browser(profile, config.get('browser', '/usr/bin/chromium')) as driver:
            user_id = sign_in(driver, url, username, password, WebDriverWait(driver, 45))
        password = ''
        # A successful login alone does not prove it survives browser shutdown.
        with browser(profile, config.get('browser', '/usr/bin/chromium')) as driver:
            driver.get(url)
            WebDriverWait(driver, 45).until(lambda current: current.execute_script(LOGIN_STATE) == 'signed_in')
            if driver.execute_script("return document.querySelector('home-assistant').hass.user.id;") != user_id:
                raise LoginError('The saved browser session belongs to a different HA user.')
        activate_profile(config_path, config, profile)
        activated = True
    finally:
        if not activated:
            # Only remove the fresh directory created by this call, never an
            # existing browser profile or a path read from user configuration.
            shutil.rmtree(profile)
    print('HA browser sign-in saved and verified after reopening Chromium. User permissions remain managed by HA.')
    print('Log out and back in, or reboot, to activate this profile in the kiosk.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/pcx86-kiosk/config.json')
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise SystemExit('Run as the kiosk desktop user, without sudo.')
    try:
        setup(args.config)
    except LoginError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        # Selenium exceptions can contain request data. Do not print tracebacks,
        # credentials, token-bearing URLs or browser page contents.
        raise SystemExit('HA browser sign-in could not be completed (connection, browser, form or persistence check). Existing kiosk login was left unchanged.') from None


if __name__ == '__main__':
    main()
