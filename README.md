> ⚠️ **UPOZORNĚNÍ – PROJEKT JE VE VÝVOJI.** Nestahujte ani neinstalujte pro běžné použití. Skript je určen pouze k řízenému testu správce na Panel-patro; jeho chování na cílovém notebooku ještě není ověřené. Před spuštěním zajistěte zálohu konfigurace a místní přístup k notebooku.

# PCX86-kiosk-debian

Debian-side companion for the [PCX86-kiosk Home Assistant integration](https://github.com/JirLap/PCX86-kiosk). It installs a per-user MQTT agent for an XFCE/Chromium kiosk. It does not modify or install the separate `HAOS-kiosk` project.

## Current target and scope

- Debian 13, XFCE, Chromium, systemd user session.
- MQTT telemetry and a fixed allow-list of controls.
- CPU, RAM, disk, uptime, APT upgrade count, available temperature, battery, IP/Wi-Fi signal, display, browser and audio state when Linux exposes them.
- Kiosk URL, refresh, browser start/stop/restart, speaker volume/mute, microphone mute, display brightness, reboot and shutdown.
- Screen blank/restore and whole-system suspend are separate actions.

Camera streaming and Wyoming voice satellite are optional follow-up services. Their capture device names and formats are hardware-specific and must be identified on each laptop before enabling. The installer does not claim a working camera or voice service until those devices and streams have been configured and verified.

## Install

### 1. Install the Debian base system

For a standard 64-bit Intel/AMD laptop, use Debian's official [installation guide for amd64](https://www.debian.org/releases/stable/amd64/) and the official [Debian installer downloads](https://www.debian.org/releases/stable/debian-installer/). Select the XFCE desktop environment during installation. After the first boot, connect the laptop to the network and make sure the desktop user can run `sudo`.

### 2. Run the installer on the panel

Log in to the XFCE desktop on the panel and open **Terminal** from the Applications menu. Run the following commands there as the normal desktop user (not as `root`):

```sh
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/JirLap/PCX86-kiosk-debian.git
cd PCX86-kiosk-debian
python3 install.py
```

If you already cloned the repository, update it and run the installer from its directory instead:

```sh
cd ~/PCX86-kiosk-debian
git pull
python3 install.py
```

Do **not** prepend `sudo` to `python3 install.py`; the installer asks for `sudo` only for the limited system-level steps. On the first run on Panel-patro, enter the broker address, port, device ID (`patro`), MQTT username/password, and the panel's initial kiosk URL. The password prompt is hidden. At the end, answer whether to enable automatic XFCE login. The installer reports where it saved the configuration and any backup. Log out and back in, or reboot, to start the new agent.

The MQTT configuration is stored with mode `0600` in `~/.config/pcx86-kiosk/config.json`. On later runs, press Enter to keep displayed values; the saved password is not shown and Enter at its hidden prompt preserves it. The script performs `apt` package installation and installs the restricted power helper/sudoers rule, so it will ask for the user's `sudo` password during those steps.

### Important when testing on an existing, partially configured panel

The installer can be run on an existing Debian/XFCE panel, but it is still a development build and has not been verified on Panel-patro. It installs packages, replaces the PCX86 agent/configuration and autostart entry, and when it finds the older `panel-mqtt-agent.desktop`, disables it while keeping a timestamped backup. It also installs a restricted sudoers rule and optionally enables LightDM automatic login. The first run does not import settings from the older panel agent or its MQTT client configuration; enter the correct broker, account, password and kiosk URL manually (`patro` is the device ID for Panel-patro).

When `~/.config/pcx86-kiosk/config.json` already exists, its device ID, broker, port, username, kiosk URL and optional camera URLs are shown as defaults. Press Enter to keep a value; the saved password is never displayed, and pressing Enter at its hidden prompt keeps it. Enter `!clear` at a prompt to clear an optional value (or the saved password). Before writing, the installer makes a timestamped config backup without overwriting earlier backups. Re-running with the same answers is intended to retain the configuration and replace the agent, but this behavior still needs a real-panel verification. Log out/in or reboot after installation to start the agent.

When the Debian companion is ready, in Home Assistant configure the built-in MQTT integration to use this broker, add the `PCX86-kiosk` repository through HACS, then add **Panel MQTT** with the same device ID. This project does not install an MQTT broker.

The agent preserves the `panel/<device_id>/command` and `panel/<device_id>/status` topic shape. Do not use the same device ID on two running laptops.

## MQTT protocol

The panel subscribes to `panel/<device_id>/command`. It publishes retained JSON to `panel/<device_id>/status` every 30 seconds and when a command arrives. MQTT Last Will publishes `{"availability":"offline"}` after an unexpected disconnect.

Commands are plain text. Fixed actions include:

`status`, `wake`, `sleep`, `screen_on`, `screen_off`, `chromium_start`, `chromium_stop`, `chromium_restart`, `kiosk_start`, `kiosk_stop`, `kiosk_restart`, `kiosk_refresh`, `suspend`, `reboot`, `shutdown`, `camera_start`, `camera_stop`, `mic_mute`, `mic_unmute`, `audio_mute`, `audio_unmute`, `volume_set:0..150`, `mic_volume_set:0..150`, `brightness_set:0..100`, and `set_url:http(s)://...`.

`wake` and `sleep` only turn the display on/off. `suspend` requests whole-system suspend and first attempts `iw ... wowlan enable magic-packet`. If Wi-Fi WoWLAN is not supported, suspend is intentionally refused instead of silently making remote wake impossible. The Wi-Fi adapter, kernel driver, firmware and access point all need to support magic-packet wake.

After suspend, MQTT is disconnected. Home Assistant's **Wake notebook** button sends a standard UDP/9 Wake-on-LAN packet using the configured Wi-Fi MAC and broadcast address. If HA is on another subnet, the router must permit directed broadcasts to the panel's LAN; many routers block these. For example, a laptop on `192.168.0.0/24` may require `192.168.0.255`, but only if the router forwards it. Configure the MAC and broadcast address in the HA integration. Test wake reliability before using suspend unattended.

## Camera and microphone streaming

The agent reports optional `camera_snapshot_url` and `camera_stream_url` endpoints from its config, but the camera backend service still needs to be installed/configured for the actual laptop. The installer currently gathers optional endpoint URLs; it does not infer `/dev/video*`, ALSA device identifiers, codecs, RTSP server address or audio track configuration. Once the HTTP snapshot endpoint works, Home Assistant's standard `camera.snapshot` action can save an image; there is no separate MQTT snapshot command.

Before enabling camera actions, identify:

1. The camera device using `v4l2-ctl --list-devices` and supported modes.
2. The intended microphone capture source using `arecord -L` or `pactl list sources short`.
3. A supported streaming backend (for example, FFmpeg feeding a local RTSP server) with an HTTP JPEG snapshot URL and RTSP URL reachable by Home Assistant.
4. A per-user `pcx86-camera.service` and snapshot service. The current agent starts/stops these service names; do not add camera entities with endpoint URLs until the service responds and publishes a valid snapshot and stream with audio as required.

Do not expose an unauthenticated camera stream to the public internet. Restrict HTTP/RTSP ports to the local network or use authenticated/reverse-proxied access.

## Voice Assist

Voice Assist is not MQTT. Install/configure a Wyoming satellite service against the specific laptop microphone and speaker devices, then add it to Home Assistant using the built-in Wyoming integration. Device identifiers and audio formats must be checked locally. See `docs/voice-assist.md` for the integration boundary and commissioning checklist.

## Power and permissions

The installer installs `/usr/local/sbin/pcx86-power` and one sudoers rule limited to that helper with only `suspend`, `reboot`, `shutdown`, and validated `brightness 0..100` actions. The helper is root-owned; the MQTT agent does not execute shell commands from received messages. MQTT credentials remain in the user's private configuration file. Restrict broker ACLs to each device's own topic pair.

## Upgrade and remove

Re-running `python3 install.py` replaces the agent and preserves the current JSON settings by offering them as defaults; it also writes a timestamped backup before saving. For removal, delete the integration entry from Home Assistant and remove the user autostart file, `~/.local/lib/pcx86-kiosk`, `~/.config/pcx86-kiosk`, `/etc/sudoers.d/pcx86-kiosk`, and `/usr/local/sbin/pcx86-power`. Remove the optional LightDM config only if it was installed by this project.

## Diagnostics

- MQTT status: `panel/<device_id>/status`.
- User config: `~/.config/pcx86-kiosk/config.json` (contains credentials; do not post it publicly).
- Run agent output: log out/in, or launch its `Exec` command from `~/.config/autostart/pcx86-kiosk-agent.desktop` in a terminal for diagnostics.
- Validate WoWLAN locally with `iw dev <wifi-interface> wowlan show` and test after arming magic-packet wake. Hardware support is not guaranteed.
- APT update count reflects the local package index. Update it using normal Debian maintenance.
