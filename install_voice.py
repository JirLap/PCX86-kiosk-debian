#!/usr/bin/python3
"""Optional, pinned Linux Voice Assistant installation for an XFCE kiosk."""

from __future__ import annotations

import argparse
import getpass
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile

from install import ask, install_file_with_sudo, unique_backup_path

LVA_VERSION = "v1.1.18"
LVA_COMMIT = "b55d36af522c263eeed4e023d6ebf30da47e4710"
LVA_REPOSITORY = "https://github.com/OHF-Voice/linux-voice-assistant.git"
SERVICE = "pcx86-voice.service"
MODELS = ("hey_jarvis", "okay_nabu", "hey_home_assistant")
PACKAGES = ("python3-venv", "python3-dev", "build-essential", "libmpv2",
            "libasound2-plugins", "avahi-utils", "git", "pkg-config", "nftables")


def resolve_ha_addresses(value: str) -> list[str]:
    """Allow only explicit HA host addresses, never arbitrary network prefixes."""
    if not value or any(char in value for char in "\n\r\x00/%"):
        raise ValueError("Enter the Home Assistant host name or IP address, without a URL or subnet.")
    try:
        addresses = {str(ip_address(value))}
    except ValueError:
        addresses = {str(ip_address(item[4][0])) for item in socket.getaddrinfo(value, None, type=socket.SOCK_STREAM)}
    if any(ip_address(address).is_unspecified or ip_address(address).is_multicast for address in addresses):
        raise ValueError("The Home Assistant address must identify a host.")
    return sorted(addresses)


def systemd_quote(value: str) -> str:
    if any(char in value for char in "\n\r\x00"):
        raise ValueError("Newlines/control characters are not allowed in service values")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def validate_audio(config: dict) -> None:
    """Check the desktop audio session before changing any services."""
    if not shutil.which("pactl"):
        raise ValueError("pactl is missing. Install the kiosk companion and verify desktop audio first.")
    def names(kind):
        result = subprocess.check_output(["pactl", "list", "short", kind], text=True)
        return {line.split()[1] for line in result.splitlines() if len(line.split()) >= 2}
    sources, sinks = names("sources"), names("sinks")
    if config["input_device"] not in sources or config["input_device"].endswith(".monitor"):
        raise ValueError("Choose an available microphone ID from pactl list short sources, excluding .monitor sources.")
    output = config["output_device"]
    if output != "pulse" and (not output.startswith("pulse/") or output[6:] not in sinks):
        raise ValueError("Choose pulse or pulse/<available sink ID> from pactl list short sinks.")


def render_firewall(addresses: list[str], port: int) -> str:
    if not 1024 <= port <= 65535:
        raise ValueError("Choose a voice port from 1024 to 65535")
    parsed = [ip_address(address) for address in addresses]
    if not parsed:
        raise ValueError("At least one HA host address is required")
    rules = [f'    iifname "lo" tcp dport {port} accept']
    for family in (4, 6):
        values = [str(address) for address in parsed if address.version == family]
        if values:
            rules.append(f"    tcp dport {port} {'ip' if family == 4 else 'ip6'} saddr {{ {', '.join(values)} }} accept")
    rules.append(f"    tcp dport {port} drop")
    return "table inet pcx86_voice {\n  chain input {\n    type filter hook input priority -10; policy accept;\n" + '\n'.join(rules) + "\n  }\n}\n"


NETWORK_HELPER = '''#!/bin/sh
set -eu
if /usr/sbin/nft list table inet pcx86_voice >/dev/null 2>&1; then
  { printf 'delete table inet pcx86_voice\\n'; cat /etc/pcx86-kiosk/voice.nft; } | /usr/sbin/nft -f -
else
  /usr/sbin/nft -f /etc/pcx86-kiosk/voice.nft
fi
'''

NETWORK_SERVICE = '''[Unit]
Description=PCX86 voice API access from Home Assistant only
Before=pcx86-voice.service
[Service]
Type=oneshot
ExecStart=/usr/local/lib/pcx86-kiosk/voice-network
RemainAfterExit=yes
'''


def render_service(config: dict, home: Path, username: str, uid: int, source: Path) -> str:
    config_dir = home / ".config/pcx86-voice"
    state_dir = home / ".local/state/pcx86-voice"
    args = [str(source/".venv/bin/python"), "-m", "linux_voice_assistant",
            "--name", config["name"], "--port", str(config["port"]),
            "--wake-model", config["wake_word"],
            "--audio-input-device", config["input_device"],
            "--audio-output-device", config["output_device"],
            "--preferences-file", str(config_dir/"preferences.json"),
            "--download-dir", str(state_dir/"downloads"),
            "--mic-noise-suppression", "2", "--disable-peripheral-api"]
    return f'''[Unit]
Description=PCX86 Linux Voice Assistant
After=network-online.target user@{uid}.service pcx86-voice-network.service
Wants=network-online.target
Requires=pcx86-voice-network.service
StartLimitIntervalSec=0
[Service]
Type=simple
User={username}
WorkingDirectory={systemd_quote(str(source))[1:-1]}
Environment={systemd_quote('PULSE_SERVER=unix:/run/user/'+str(uid)+'/pulse/native')}
Environment={systemd_quote('XDG_RUNTIME_DIR=/run/user/'+str(uid))}
Environment={systemd_quote('XDG_CACHE_HOME='+str(state_dir/'cache'))}
Environment=OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 PYTHONUNBUFFERED=1
ExecStart={" ".join(systemd_quote(value) for value in args)}
Restart=on-failure
RestartSec=15
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={systemd_quote(str(config_dir))} {systemd_quote(str(state_dir))}
[Install]
WantedBy=multi-user.target
'''


def install_text(text: str, target: str, mode: str = "0644"):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", delete=False) as file:
        file.write(text)
        path = Path(file.name)
    try:
        install_file_with_sudo(path, target, mode)
    finally:
        path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name")
    parser.add_argument("--ha-address")
    parser.add_argument("--input-device")
    parser.add_argument("--output-device")
    parser.add_argument("--wake-word", choices=MODELS)
    parser.add_argument("--yes", action="store_true", help="Use supplied arguments/existing config without prompting")
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise SystemExit("Run as the kiosk desktop user, without sudo.")
    if not Path("/etc/debian_version").exists():
        raise SystemExit("This installer targets Debian Linux.")
    username = getpass.getuser()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", username):
        raise SystemExit("Unsupported Linux username")
    home = Path.home()
    config_dir = home/".config/pcx86-voice"
    config_path = config_dir/"config.json"
    existing = json.loads(config_path.read_text()) if config_path.exists() else {}
    if not isinstance(existing,dict):
        raise SystemExit("Existing voice config must be an object; it was left unchanged.")
    def setting(key, argument, label, default=""):
        value = argument if argument is not None else existing.get(key,default)
        return str(value) if args.yes else ask(label,str(value))
    config = {
        "name":setting("name",args.name,"Voice device name",socket.gethostname()+" Voice"),
        "ha_address":setting("ha_address",args.ha_address,"Home Assistant host/IP allowed to connect"),
        "input_device":setting("input_device",args.input_device,"PulseAudio microphone ID (use pactl list short sources)"),
        "output_device":setting("output_device",args.output_device,"mpv output device (pulse or pulse/<sink-id>)","pulse"),
        "wake_word":setting("wake_word",args.wake_word,"Wake word (hey_jarvis/okay_nabu/hey_home_assistant)","hey_jarvis"),
        "port":6053,
    }
    if not config["input_device"] or not config["name"] or config["wake_word"] not in MODELS:
        raise SystemExit("A name, explicit microphone ID and a supported wake word are required.")
    try:
        addresses = resolve_ha_addresses(config["ha_address"])
        validate_audio(config)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Voice settings could not be verified: {error}. Existing services were left unchanged.") from error
    if any(char in value for value in config.values() if isinstance(value,str) for char in "\r\n\x00"):
        raise SystemExit("Control characters are not allowed in voice settings")
    config["ha_allowed_ips"] = addresses
    config["upstream_version"] = LVA_VERSION
    config["upstream_commit"] = LVA_COMMIT
    print("Installing pinned LVA",LVA_VERSION,"using the existing PulseAudio server.")
    subprocess.run(["sudo","apt-get","update"],check=True)
    subprocess.run(["sudo","apt-get","install","-y","--no-install-recommends",*PACKAGES],check=True)
    base = home/".local/share/pcx86-voice"
    base.mkdir(parents=True,exist_ok=True)
    source = base/('lva-'+LVA_VERSION)
    if not (source/".git").exists():
        subprocess.run(["git","clone","--depth","1","--branch",LVA_VERSION,LVA_REPOSITORY,str(source)],check=True)
    commit = subprocess.check_output(["git","-C",str(source),"rev-parse","HEAD"],text=True).strip()
    if commit != LVA_COMMIT:
        raise SystemExit("LVA source does not match the pinned release commit; refusing installation")
    python = source/".venv/bin/python"
    marker = base/('installed-'+LVA_VERSION+'.txt')
    if not python.exists():
        subprocess.run(["python3","-m","venv",str(source/".venv")],check=True)
        marker.unlink(missing_ok=True)
    if not marker.exists():
        env = dict(os.environ,CXXFLAGS="-O1 -g0",MAKEFLAGS="-j1")
        subprocess.run([str(python),"-m","pip","install","--upgrade","pip","setuptools","wheel"],check=True)
        subprocess.run([str(python),"-m","pip","install","--editable",str(source)],check=True,env=env)
        frozen = subprocess.check_output([str(python),"-m","pip","freeze"],text=True)
        (base/('dependencies-'+LVA_VERSION+'.txt')).write_text(frozen)
        subprocess.run([str(python),"-m","linux_voice_assistant","--help"],cwd=source,check=True,stdout=subprocess.DEVNULL)
        marker.write_text(LVA_COMMIT+'\n')
    (source/"version.txt").write_text(LVA_VERSION+'\n')
    for directory in (config_dir, home/".local/state/pcx86-voice"):
        directory.mkdir(parents=True,exist_ok=True)
        os.chmod(directory,0o700)
    if config_path.exists():
        backup = unique_backup_path(config_path)
        shutil.copy2(config_path,backup)
        os.chmod(backup,0o600)
    # Stop only voice while changing its own unit and private configuration.
    subprocess.run(["sudo","systemctl","stop",SERVICE],check=False)
    config_path.write_text(json.dumps(config,indent=2)+'\n')
    os.chmod(config_path,0o600)
    preferences = config_dir/"preferences.json"
    if not preferences.exists():
        preferences.write_text(json.dumps({"volume":0.35})+'\n')
        os.chmod(preferences,0o600)
    install_text(render_firewall(addresses,config["port"]),"/etc/pcx86-kiosk/voice.nft")
    install_text(NETWORK_HELPER,"/usr/local/lib/pcx86-kiosk/voice-network","0755")
    install_text(NETWORK_SERVICE,"/etc/systemd/system/pcx86-voice-network.service")
    install_text(render_service(config,home,username,os.getuid(),source),"/etc/systemd/system/"+SERVICE)
    subprocess.run(["sudo","systemctl","daemon-reload"],check=True)
    subprocess.run(["sudo","systemd-analyze","verify","/etc/systemd/system/"+SERVICE],check=True)
    subprocess.run(["sudo","systemctl","restart","pcx86-voice-network.service"],check=True)
    subprocess.run(["sudo","systemctl","enable","--now",SERVICE],check=True)
    print("Voice service enabled. Check: sudo journalctl -u pcx86-voice -n 50")
    print("In HA add ESPHome using this panel's IP and port 6053, then choose an Assist pipeline.")
    print("API access is restricted to the configured HA host and localhost. Music remains on Sendspin.")


if __name__ == "__main__":
    main()
