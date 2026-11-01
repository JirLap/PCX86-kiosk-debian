#!/usr/bin/python3
"""Interactive installer for the PCX86 Debian kiosk agent."""

from __future__ import annotations

from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parent
AGENT_SOURCE = REPO_ROOT / "src" / "panel_agent.py"
POWER_HELPER = REPO_ROOT / "src" / "pcx86-power"


def ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    if value == "!clear":
        return ""
    return value or default


def unique_backup_path(path: Path) -> Path:
    """Return a timestamped backup name that never overwrites an older backup."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.{timestamp}.bak")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{timestamp}-{suffix}.bak")
        suffix += 1
    return candidate


def read_existing_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise SystemExit(f"Cannot read existing config {path}: {err}. It was left unchanged.")
    if not isinstance(config, dict):
        raise SystemExit(f"Existing config {path} is not a JSON object. It was left unchanged.")
    if not isinstance(config.get("camera", {}), dict):
        raise SystemExit(f"The camera section in {path} is invalid. It was left unchanged.")
    return config


def install_file_with_sudo(source: Path, target: str, mode: str):
    subprocess.run(
        ["sudo", "install", "-D", "-o", "root", "-g", "root", "-m", mode, str(source), target],
        check=True,
    )


def configure_wifi_power_saving(net_dir: Path = Path("/sys/class/net")) -> None:
    """Disable power saving on active NM Wi-Fi profiles without reconnecting."""
    nmcli = shutil.which("nmcli")
    iw = shutil.which("iw") or (
        "/usr/sbin/iw" if Path("/usr/sbin/iw").is_file() else None
    )
    if not nmcli:
        print("NetworkManager/nmcli is unavailable; Wi-Fi power saving was not changed.")
        return

    def run(command):
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=20, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            print("WARNING: Wi-Fi configuration command failed or timed out; check network settings.")
            return None
        if result.returncode:
            print(f"WARNING: Wi-Fi configuration command failed (exit {result.returncode}); check network settings.")
            return None
        return result

    active = False
    for wireless in sorted(net_dir.glob("*/wireless")):
        if not wireless.is_dir():
            continue
        interface = wireless.parent.name
        connection = run([nmcli, "--get-values", "GENERAL.CON-UUID", "device", "show", interface])
        if connection is None:
            continue
        uuid = connection.stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid):
            continue  # Disconnected or not managed by NetworkManager.
        active = True
        previous = run([nmcli, "--get-values", "802-11-wireless.powersave", "connection", "show", "uuid", uuid])
        if previous is None:
            continue
        if previous.stdout.strip() not in ("2", "disable"):
            changed = run(["sudo", nmcli, "--wait", "15", "connection", "modify", "uuid", uuid,
                           "802-11-wireless.powersave", "2"])
            if changed is None:
                continue
            print(f"Wi-Fi {interface}: saved powersave=2 for profile UUID {uuid} (previously {previous.stdout.strip()}).")
        else:
            print(f"Wi-Fi {interface}: profile already has power saving disabled.")
        if not iw:
            print(f"WARNING: iw is unavailable; {interface} has persistent settings only, not applied live.")
        elif run(["sudo", iw, "dev", interface, "set", "power_save", "off"]) is not None:
            print(f"Wi-Fi {interface}: power saving disabled now, without disconnecting.")
        else:
            print(f"WARNING: {interface} profile was saved, but the driver did not apply power saving off live.")
    if not active:
        print("No active NetworkManager Wi-Fi profile found; Wi-Fi power saving was not changed.")


def install_optional_voice() -> None:
    """Keep a failed optional voice setup from aborting the prepared kiosk."""
    try:
        subprocess.run([sys.executable, str(REPO_ROOT / "install_voice.py")], check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Optional voice installation did not complete: {error}. Kiosk installation is prepared; retry voice with --voice.")


def install_browser_login(config_path: Path) -> None:
    """Run optional interactive HA login outside the MQTT configuration process."""
    try:
        subprocess.run(['sudo', 'apt-get', 'install', '-y', '--no-install-recommends',
                        'python3-selenium', 'chromium-driver'], check=True)
        subprocess.run(['/usr/bin/python3', str(REPO_ROOT / 'setup_ha_login.py'),
                        '--config', str(config_path)], check=True)
    except (OSError, subprocess.CalledProcessError):
        print('Optional HA browser sign-in did not complete. Kiosk installation is prepared; you can retry the installer or sign in on the panel.')


def main():
    if os.geteuid() == 0:
        raise SystemExit("Run this installer as the kiosk desktop user, without sudo.")
    if not Path("/etc/debian_version").exists():
        raise SystemExit("This installer targets Debian Linux.")
    if not shutil.which("sudo"):
        raise SystemExit("Install and configure sudo for this user, then run the installer again.")

    username = getpass.getuser()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", username):
        raise SystemExit("The current Linux username contains unsupported characters.")

    config_dir = Path.home() / ".config/pcx86-kiosk"
    config_path = config_dir / "config.json"
    existing = read_existing_config(config_path)
    camera_existing = existing.get("camera", {})
    if existing:
        print(f"Found existing panel configuration: {config_path}")
        print("Press Enter to keep each shown value. Enter !clear to remove an optional value.")
    else:
        print("No PCX86 panel configuration found; this is a first-time setup.")
    print("PCX86-kiosk Debian setup. MQTT passwords are entered locally and never displayed.")
    device_id = ask("MQTT device ID", str(existing.get("device_id", "kiosk"))).lower()
    if not re.fullmatch(r"[a-z0-9_-]+", device_id):
        raise SystemExit("Use lowercase letters, digits, hyphens, or underscores for the device ID.")
    mqtt_host = ask("MQTT broker address", str(existing.get("mqtt_host", "")))
    if not mqtt_host:
        raise SystemExit("Enter the host name or IP address of your existing MQTT broker.")
    try:
        mqtt_port = int(ask("MQTT broker port", str(existing.get("mqtt_port", 1883))))
    except ValueError:
        raise SystemExit("The MQTT broker port must be a number from 1 to 65535.")
    if not 1 <= mqtt_port <= 65535:
        raise SystemExit("The MQTT broker port must be a number from 1 to 65535.")
    mqtt_username = ask("MQTT username", str(existing.get("mqtt_username", "")))
    old_password = str(existing.get("mqtt_password", ""))
    if old_password:
        entered_password = getpass.getpass(
            "MQTT password (hidden; Enter keeps saved password, !clear removes it): "
        )
        if not entered_password:
            mqtt_password = old_password
        elif entered_password == "!clear":
            mqtt_password = ""
        else:
            mqtt_password = entered_password
    else:
        mqtt_password = getpass.getpass("MQTT password (hidden; leave blank for anonymous access): ")
    kiosk_url = ask("Initial kiosk URL", str(existing.get("kiosk_url", "")))
    if not kiosk_url.startswith(("http://", "https://")):
        raise SystemExit("The kiosk URL must begin with http:// or https://.")
    snapshot_url = ask(
        "Optional laptop-camera snapshot URL (enter !clear to remove)",
        str(camera_existing.get("snapshot_url") or ""),
    )
    stream_url = ask(
        "Optional laptop-camera RTSP URL (enter !clear to remove)",
        str(camera_existing.get("stream_url") or ""),
    )
    legacy_browser = Path.home() / ".config/autostart/chromium.desktop"
    # Automatic desktop login cannot unlock the user's login keyring. Apply this
    # on repeat installs too, including configurations previously saved as auto.
    password_store = "basic"
    print("Chromium kiosk password store: basic (automatic setup without a keyring unlock prompt; browser secrets are not encrypted by the desktop keyring).")

    print("Installing Debian packages needed for the agent, kiosk, and hardware inspection.")
    subprocess.run(["sudo", "apt-get", "update"], check=True)
    subprocess.run(
        [
            "sudo", "apt-get", "install", "-y",
            "chromium", "python3-paho-mqtt", "python3-psutil", "procps",
            "x11-xserver-utils", "xdotool", "pulseaudio-utils", "iw",
            "ffmpeg", "v4l-utils", "xprintidle",
        ],
        check=True,
    )

    print("Disabling Wi-Fi power saving on active NetworkManager Wi-Fi profiles.")
    configure_wifi_power_saving()

    install_dir = Path.home() / ".local/lib/pcx86-kiosk"
    autostart_dir = Path.home() / ".config/autostart"
    install_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    autostart_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)
    shutil.copy2(AGENT_SOURCE, install_dir / "panel_agent.py")
    os.chmod(install_dir / "panel_agent.py", 0o755)

    if config_path.exists():
        backup = unique_backup_path(config_path)
        shutil.copy2(config_path, backup)
        os.chmod(backup, 0o600)
        print(f"Existing configuration backed up to {backup}.")
    config = dict(existing)
    config.update({
        "device_id": device_id,
        "mqtt_host": mqtt_host,
        "mqtt_port": mqtt_port,
        "mqtt_username": mqtt_username,
        "mqtt_password": mqtt_password,
        "kiosk_url": kiosk_url,
        "browser": existing.get("browser", "/usr/bin/chromium"),
        "browser_password_store": password_store,
        "screensaver_enabled": existing.get("screensaver_enabled", False),
        "screensaver_timeout": existing.get("screensaver_timeout", 10),
        "status_interval": existing.get("status_interval", 30),
        "update_check_interval": existing.get("update_check_interval", 3600),
    })
    camera = dict(camera_existing)
    camera.update({
        "snapshot_url": snapshot_url or None,
        "stream_url": stream_url or None,
    })
    config["camera"] = camera
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=config_dir, delete=False, prefix=".config-"
    ) as file:
        json.dump(config, file, indent=2)
        file.write("\n")
        temporary = Path(file.name)
    os.chmod(temporary, 0o600)
    temporary.replace(config_path)

    agent_entry = autostart_dir / "pcx86-kiosk-agent.desktop"
    log_dir = Path.home() / ".local/state/pcx86-kiosk"
    log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(log_dir, 0o700)
    launcher = install_dir / "start-agent.sh"
    launcher.write_text(
        "#!/bin/sh\numask 077\nexec /usr/bin/python3 "
        + shlex.quote(str(install_dir / "panel_agent.py"))
        + " --config " + shlex.quote(str(config_path))
        + " >> " + shlex.quote(str(log_dir / "agent.log")) + " 2>&1\n",
        encoding="utf-8",
    )
    os.chmod(launcher, 0o755)
    legacy_entry = autostart_dir / "panel-mqtt-agent.desktop"
    if legacy_entry.exists():
        legacy_backup = unique_backup_path(legacy_entry)
        shutil.copy2(legacy_entry, legacy_backup)
        legacy_entry.unlink()
        print(f"Disabled the previous MQTT autostart entry; kept its backup at {legacy_backup}.")
    agent_entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=PCX86 kiosk agent\n"
        "Comment=MQTT telemetry and safe panel controls\n"
        f'Exec="{launcher}"\n'
        "Terminal=false\nX-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    os.chmod(agent_entry, 0o644)
    if legacy_browser.exists() and "--kiosk" in legacy_browser.read_text():
        legacy_backup = unique_backup_path(legacy_browser)
        shutil.copy2(legacy_browser, legacy_backup)
        legacy_browser.unlink()
        print(f"Disabled the separate Chromium kiosk launcher; kept its backup at {legacy_backup}.")

    print("Installing the restricted root helper for brightness, reboot, and shutdown.")
    install_file_with_sudo(POWER_HELPER, "/usr/local/sbin/pcx86-power", "0755")
    sudoers = Path(tempfile.mkstemp(prefix="pcx86-sudoers-")[1])
    sudoers.write_text(
        f"{username} ALL=(root) NOPASSWD: /usr/local/sbin/pcx86-power reboot, "
        "/usr/local/sbin/pcx86-power shutdown, "
        "/usr/local/sbin/pcx86-power brightness *\n",
        encoding="utf-8",
    )
    subprocess.run(["sudo", "visudo", "-cf", str(sudoers)], check=True)
    install_file_with_sudo(sudoers, "/etc/sudoers.d/pcx86-kiosk", "0440")
    sudoers.unlink(missing_ok=True)

    if ask("Enable automatic XFCE login for this user?", "y").lower() in ("y", "yes", "a"):
        lightdm_source = Path(tempfile.mkstemp(prefix="pcx86-lightdm-")[1])
        lightdm_source.write_text(
            "[Seat:*]\n"
            f"autologin-user={username}\n"
            "autologin-user-timeout=0\n",
            encoding="utf-8",
        )
        install_file_with_sudo(
            lightdm_source,
            "/etc/lightdm/lightdm.conf.d/50-pcx86-kiosk.conf",
            "0644",
        )
        lightdm_source.unlink(missing_ok=True)

    print('Optional HA browser sign-in: enter the chosen HA user in this terminal (also works over SSH). No HA password is saved in kiosk configuration.')
    if ask('Sign in to Home Assistant for the kiosk now?', 'n' if existing.get('browser_profile') else 'y').lower() in ('y', 'yes', 'a'):
        install_browser_login(config_path)

    voice_installed = (Path.home() / ".config/pcx86-voice/config.json").exists()
    if ask("Install/update the optional Home Assistant voice satellite?", "y" if voice_installed else "n").lower() in ("y","yes","a"):
        install_optional_voice()
    print("Installation prepared. Log out and back in, or reboot, to start the kiosk agent.")
    print("Camera streaming needs its optional service. Voice can also be added with the shell launcher's --voice option.")
    print("MQTT credentials are stored in ~/.config/pcx86-kiosk/config.json with mode 600.")


if __name__ == "__main__":
    main()
