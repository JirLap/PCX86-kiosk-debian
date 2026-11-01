#!/usr/bin/python3
"""Disable automatic desktop locking for this dedicated kiosk user only."""

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess


LOCKERS = ('light-locker', 'xfce4-screensaver')
XFCONF_SETTINGS = (
    ('xfce4-power-manager', '/xfce4-power-manager/lock-screen-suspend-hibernate', 'false'),
    ('xfce4-session', '/shutdown/LockScreen', 'false'),
    ('xfce4-screensaver', '/lock/enabled', 'false'),
    ('xfce4-screensaver', '/saver/enabled', 'false'),
)
LIGHT_LOCKER_SETTINGS = (
    ('lock-after-screensaver', '0'), ('lock-on-suspend', 'false'), ('lock-on-lid', 'false'),
)


def command(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        print('WARNING: desktop setting command unavailable or timed out:', args[0])
        return None


def disable_locking(home: Path) -> None:
    autostart = home / '.config/autostart'
    autostart.mkdir(parents=True, exist_ok=True)
    for name in LOCKERS:
        target = autostart / (name + '.desktop')
        content = '[Desktop Entry]\nType=Application\nName=Disabled kiosk screen locker\nHidden=true\n'
        if target.exists() and target.read_text() == content:
            continue
        if target.exists():
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            backup = target.with_name(target.name + '.' + stamp + '.bak')
            suffix = 1
            while backup.exists():
                backup = target.with_name(target.name + '.' + stamp + f'-{suffix}.bak')
                suffix += 1
            shutil.copy2(target, backup)
        target.write_text(content, encoding='utf-8')

    changes = []
    previous = {}
    xfconf = shutil.which('xfconf-query')
    if xfconf:
        for channel, prop, value in XFCONF_SETTINGS:
            result = command([xfconf, '-c', channel, '-p', prop])
            if result is None:
                continue
            old = result.stdout.strip() if result.returncode == 0 else None
            if old == value:
                continue
            previous[channel + ':' + prop] = old
            args = [xfconf, '-c', channel, '-p', prop, '-s', value]
            if old is None:
                args += ['--create', '--type', 'bool']
            changes.append(args)
    gsettings = shutil.which('gsettings')
    schemas = command([gsettings, 'list-schemas']) if gsettings else None
    if schemas is not None and schemas.returncode == 0 and 'apps.light-locker' in schemas.stdout.splitlines():
        for key, value in LIGHT_LOCKER_SETTINGS:
            result = command([gsettings, 'get', 'apps.light-locker', key])
            if result is None or result.returncode != 0:
                continue
            old = result.stdout.strip()
            if old.removeprefix('uint32 ') != value:
                previous['apps.light-locker:' + key] = old
                changes.append([gsettings, 'set', 'apps.light-locker', key, value])
    if previous:
        backup_dir = home / '.config/pcx86-kiosk'
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Preserve the first pre-kiosk values rather than overwriting on login.
        snapshot = backup_dir / 'desktop-lock-settings.before.json'
        saved = json.loads(snapshot.read_text()) if snapshot.exists() else {}
        for key, value in previous.items():
            saved.setdefault(key, value)
        snapshot.write_text(json.dumps(saved, indent=2) + '\n')
        os.chmod(snapshot, 0o600)
    for args in changes:
        result = command(args)
        if result is None or result.returncode != 0:
            print('WARNING: could not apply kiosk desktop setting:', args[0:5])
    # Saved XFCE sessions can restore a locker despite Hidden=true. Stop only
    # these exact programs owned by this user; leave the desktop session intact.
    pkill = shutil.which('pkill')
    if pkill:
        for name in LOCKERS:
            result = command([pkill, '-u', str(os.getuid()), '-x', '-f', '(/[^ ]*/)?' + name + '( .*)?'])
            if result is not None and result.returncode not in (0, 1):
                print('WARNING: could not stop kiosk locker:', name)
    print('Kiosk desktop locking configured for this user. Screen blanking remains controlled by the kiosk agent.')


def main():
    if os.geteuid() == 0:
        raise SystemExit('Run as the kiosk desktop user, without sudo.')
    bus = Path('/run/user') / str(os.getuid()) / 'bus'
    if not os.environ.get('DBUS_SESSION_BUS_ADDRESS') and bus.exists():
        os.environ['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + str(bus)
    disable_locking(Path.home())


if __name__ == '__main__':
    main()
