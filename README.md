> ⚠️ **IN DEVELOPMENT — DO NOT INSTALL YET.** The installer and kiosk agent are still being developed and have not been validated as a complete release. These instructions are for development review only; features and configuration may change.

# PCX86-kiosk-debian

Debian-side companion for the [PCX86-kiosk Home Assistant integration](https://github.com/JirLap/PCX86-kiosk). It installs a per-user MQTT agent for a Debian XFCE/Chromium kiosk. It does not install or modify the separate `HAOS-kiosk` Frigate display add-on.

## Requirements

- A 64-bit Debian laptop with an XFCE desktop session.
- A desktop user with `sudo` access.
- An MQTT broker that Home Assistant can also reach.
- A Home Assistant installation with its built-in MQTT integration configured and the [PCX86-kiosk integration](https://github.com/JirLap/PCX86-kiosk) installed through HACS.

## Install Debian and the kiosk agent

For the Debian base system, use the official [Debian installation guide for amd64](https://www.debian.org/releases/stable/amd64/) and [installer downloads](https://www.debian.org/releases/stable/debian-installer/). Select XFCE during installation, connect the laptop to the network, and make sure the desktop user can run `sudo`.

From the XFCE desktop, open **Terminal** and download and run the shell installer as the normal desktop user:

```sh
wget -O ~/pcx86-kiosk-install.sh https://raw.githubusercontent.com/JirLap/PCX86-kiosk-debian/main/install.sh
sh ~/pcx86-kiosk-install.sh
```

Do not run the installer as `root` or prepend `sudo`. It requests `sudo` only for the limited system-level installation steps. The shell launcher downloads the source archive to a temporary directory and starts the interactive Python installer. The temporary download is removed when the installer exits. To review the launcher first, open `~/pcx86-kiosk-install.sh` in a text editor. Do not pipe it directly into `sh`: the installer needs terminal input for its setup questions.

The first run asks for a unique device ID, MQTT broker host and port, MQTT username and password, and the initial kiosk URL. The password is entered without echoing to the terminal. Optional camera snapshot and RTSP endpoint URLs can also be entered. The installer installs required Debian packages, the agent and a restricted power helper, then creates an XFCE autostart entry. Log out and back in, or reboot, to start the agent.

The agent configuration is stored in `~/.config/pcx86-kiosk/config.json` with private file permissions. On later runs, existing settings are offered as defaults; press Enter to keep them. The saved password is never displayed, and Enter at its hidden prompt preserves it. Enter `!clear` to clear an optional value. A timestamped backup is created before the configuration is replaced. Re-running the installer is intended to update the agent while preserving existing settings.

To update the installer, run the same `wget` command again, then run `sh ~/pcx86-kiosk-install.sh`.

## Wi-Fi power saving

During installation and repeat runs, the installer detects each active NetworkManager Wi-Fi profile and disables its power saving using `802-11-wireless.powersave=2`. It identifies profiles by UUID, so it works with arbitrary network names. This matches NetworkManager's [documented disable value](https://networkmanager.dev/docs/api/latest/settings-802-11-wireless.html).

The profile setting persists across restarts. The installer also applies `iw dev <interface> set power_save off` immediately. It does not disconnect/reconnect the network, restart NetworkManager, change the Wi-Fi band or access point, or modify credentials. Repeat runs keep the profile setting and reapply the live adapter setting. A laptop using only Ethernet, an inactive Wi-Fi profile, or a network managed without NetworkManager is skipped; connect the intended Wi-Fi network and rerun the installer to configure it. Other saved, inactive profiles are left unchanged.

If the driver rejects the live change or a command fails, the installer prints a warning and continues with the kiosk installation. Disabling Wi-Fi power saving can increase power consumption; it is intended to improve remote kiosk availability and is not a guarantee against every network failure. It is separate from whole-system suspend and Wi-Fi Wake-on-LAN.

To inspect the result locally, substitute the actual interface and profile UUID:

```sh
nmcli --get-values GENERAL.CON-UUID device show <interface>
nmcli --get-values 802-11-wireless.powersave connection show uuid <profile-uuid>
/usr/sbin/iw dev <interface> get power_save
```

Expected results are `disable` (or `2`, depending on nmcli version/output format) for the profile and `Power save: off` for the adapter. To restore NetworkManager defaults instead, set `sudo nmcli connection modify uuid <profile-uuid> 802-11-wireless.powersave 0`. This changes the next profile activation; it does not restore the live driver setting immediately. Use the previous value printed by the installer if the profile originally had an explicit policy instead of the default.

## Home Assistant setup and automatic discovery

Install the PCX86-kiosk integration from HACS and restart Home Assistant. Configure the panel with the same MQTT broker that Home Assistant uses. When the agent connects, it publishes a retained discovery announcement; Home Assistant then offers the panel for confirmation. The dialog asks only for a device name and an optional area. Its device ID, MAC address, IP address, and MQTT topics are read from the announcement.

This integration uses Home Assistant's custom-integration MQTT discovery hook. It does not use the MQTT integration's `homeassistant` discovery prefix, and that prefix does not need to be changed. The broker itself is not installed by this project.

## MQTT topics and information

For device ID `kiosk`, the agent publishes a retained announcement on `pcx86_kiosk/discovery/kiosk`, retained status on `pcx86_kiosk/kiosk/status`, and listens for commands on `pcx86_kiosk/kiosk/command`. It republishes the announcement after connecting to the broker. Do not use the same device ID for multiple panels.

Status includes available CPU, memory, disk, uptime, update count, temperature, battery, display, Chromium, camera, audio and microphone information. Network and device information includes hostname, IP address, MAC address, network interface, broadcast address, Wi-Fi network and signal, operating system, kernel, architecture and hardware manufacturer/model when Linux provides them. Unavailable hardware details are omitted.

The agent accepts only fixed, allow-listed commands, including status refresh, kiosk URL change and page refresh, browser start/stop/restart, display on/off, brightness and volume changes, mute controls, camera service start/stop, suspend, reboot and shutdown. It ignores retained command messages. MQTT credentials stay in the panel's private configuration; restrict broker ACLs to each device's discovery, status and command topics.

## Screen off, suspend and wake

`sleep` and `wake` only turn the display off and on. `suspend` requests whole-system suspend and is separate. The agent refuses suspend if it cannot arm Wi-Fi Wake-on-LAN magic-packet support. Successful wake also depends on the laptop adapter, driver, firmware, access point and router. While the laptop is suspended it is disconnected from MQTT; Home Assistant must send a Wake-on-LAN packet to the configured Wi-Fi MAC address and broadcast network. Verify this path before relying on suspend.

## Camera and Voice Assist

Camera use is optional and hardware-specific. The agent can report configured snapshot and RTSP URLs and control the `pcx86-camera.service`; it does not infer the camera device, audio capture source, streaming server, codecs or authentication. Configure and verify an HTTP JPEG snapshot and an RTSP stream separately before enabling the camera in Home Assistant. Keep camera endpoints on a trusted network or protect them with authentication.

Voice Assist is separate from MQTT. Configure a Wyoming satellite service for the laptop's microphone and speaker, then add it through Home Assistant's Wyoming integration. See [`docs/voice-assist.md`](docs/voice-assist.md) for the integration boundary and setup checklist.

## Remove

Remove the PCX86-kiosk entry from Home Assistant. On the Debian panel, remove the `pcx86-kiosk-agent.desktop` autostart entry, `~/.local/lib/pcx86-kiosk/`, and `~/.config/pcx86-kiosk/`. If this project installed them, remove `/etc/sudoers.d/pcx86-kiosk` and `/usr/local/sbin/pcx86-power`. Remove the optional LightDM setting only if it was enabled by this installer.
