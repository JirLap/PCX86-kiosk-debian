# Optional Home Assistant voice satellite

> **Development preview.** Validate wake-word detection, recognition, response audio and recovery on each laptop before relying on voice control.

The optional installer runs [Linux Voice Assistant (LVA)](https://github.com/OHF-Voice/linux-voice-assistant) using the native ESPHome protocol. MQTT continues to carry PCX86 system telemetry and kiosk commands; Home Assistant's built-in ESPHome integration owns the Assist satellite and its voice settings. No ESPHome firmware build, ESPHome Device Builder app or Wyoming satellite is required.

The installer pins LVA to **v1.1.18**, commit `b55d36af522c263eeed4e023d6ebf30da47e4710`, in a separate Python environment. It uses the existing desktop PulseAudio server. It does not replace the audio server or the Sendspin service. Speech recognition, conversation and speech synthesis are selected in Home Assistant; the laptop detects its wake word locally with microWakeWord.

## Requirements

- Debian 13 amd64, Python 3.13 and a working XFCE/PulseAudio desktop session with automatic login if unattended operation is required.
- The desktop user must have sudo access. Run the installer as this user, without sudo.
- A microphone and speaker verified locally, and access to the internet for installation/model downloads.
- Home Assistant reachable from the panel, with a configured Assist pipeline: speech-to-text, conversation agent and text-to-speech. Choose Czech for a Czech assistant and expose only the entities you want it to control.
- A stable Home Assistant host address. The broker address can be different; enter the actual HA host address in the voice installer.

## Install or update voice only

List microphone and speaker IDs in the desktop user's terminal:

```sh
pactl list short sources
pactl list short sinks
```

Choose a real microphone source such as `alsa_input.…`, not an output's `.monitor` source. The output setting is `pulse` for the current default speaker or `pulse/<sink-id>` for an explicit speaker. Avoid numeric indexes, which can change across reboots. Connect USB audio devices before installation and use the same devices on repeat runs.

Download the shell launcher and start its separate voice setup:

```sh
wget -O ~/pcx86-kiosk-install.sh https://raw.githubusercontent.com/JirLap/PCX86-kiosk-debian/main/install.sh
sh ~/pcx86-kiosk-install.sh --voice
```

The questions cover the voice device name, HA host/IP, microphone ID, speaker and wake word. The default wake word is **Hey Jarvis** (`hey_jarvis`); `okay_nabu` and `hey_home_assistant` are also supported. The normal full kiosk installer offers voice as an optional final step, disabled by default on a fresh installation.

Repeat the same command to update/reconfigure voice. Existing answers are offered as defaults, the voice configuration is backed up, and HA-managed preferences are preserved. The pinned version is reused; a repeat run does not silently upgrade upstream LVA. Updating voice restarts only the voice service. The `--voice` path does not rewrite the MQTT/kiosk configuration. Do not run installers concurrently.

Configuration lives in `~/.config/pcx86-voice/config.json`; HA-managed preferences live in the adjacent `preferences.json`. Runtime data is in `~/.local/state/pcx86-voice/`. Source, its Python environment and the recorded dependency versions are under `~/.local/share/pcx86-voice/`.

## Add to Home Assistant

1. Open **Settings → Devices & services** and configure the discovered voice device. If no discovery appears (for example, across routed subnets), choose **Add integration → ESPHome** and enter the panel's IP address with port **6053**.
2. Open the new voice device, select its Assist pipeline/assistant and assign an area. Choose the intended Czech pipeline rather than leaving an unintended default.
3. Check the wake-word selection, microphone mute, noise suppression and response volume exposed by LVA. Its microphone volume setting is a software level; check the physical input gain separately in PulseAudio. The installer preserves that physical gain and mute state.
4. Say the configured wake word, then a simple request such as the time. Verify the recognized text, answer and speaker output before testing an exposed light or other device.

This uses HA's built-in ESPHome integration, so the installed PCX86 custom component does not need a code update or an HA restart just to add voice. Voice has a separate integration entry. LVA exposes a response/media player, while **Music Assistant / Sendspin remains the music player**. Choose the Sendspin player for music automations. This installer does not configure automatic Sendspin ducking or acoustic echo cancellation; test listening with music and camera audio before using those concurrently.

## Network and microphone behavior

This LVA version exposes an unencrypted ESPHome API without an authentication key. The installer therefore installs a dedicated `inet pcx86_voice` nftables table that allows TCP 6053 only from the resolved HA host addresses and local processes; other sources are dropped. Its optional peripheral API is disabled. The rules are loaded before the voice service starts. Only this project's table is replaced, and existing firewall rules and SSH are left untouched. Existing firewall policies must also allow the HA connection. Do not forward this port to the internet.

Host names are resolved at installation time. If HA's address changes, rerun voice setup to refresh the allow-list. Use a DHCP reservation or static address for HA and the panel. mDNS discovery does not bypass the access rules and may be unavailable across subnets; manual ESPHome setup works when routing/firewalls allow it.

While the service runs, the microphone is captured for local wake-word detection. The selected Assist pipeline processes a triggered request and may use cloud services depending on your HA configuration. LVA's mute switch pauses its voice processing; the PCX86 MQTT microphone mute controls the physical PulseAudio source, which can also affect camera recording. Stopping the voice service releases its capture stream. This installer does not save microphone recordings.

Display sleep keeps Linux and the audio services running, so voice can remain available while the screen is off. Voice activation does not automatically turn the display on. To do that, add a Home Assistant automation based on the Assist satellite's listening state and invoke PCX86's Wake panel button.

## Diagnose and control

```sh
sudo systemctl status pcx86-voice --no-pager
sudo journalctl -u pcx86-voice -n 60 --no-pager
pactl list short source-outputs
sudo nft list table inet pcx86_voice
```

To pause listening or restart voice:

```sh
sudo systemctl stop pcx86-voice
sudo systemctl start pcx86-voice
```

If there is no microphone input, check source selection, physical mute/gain, the logged-in desktop session and `/run/user/<uid>/pulse/native`. A voice service cannot use the session's audio server before it is available; it retries after startup failures. If HA cannot connect, check the configured HA source address, routing and TCP 6053 before changing wake words. If a request is heard but not answered, inspect the selected Assist pipeline and its STT/TTS providers in HA.

After setup, also test physical microphone mute, display sleep/wake, a voice-service restart and a laptop reboot. Reboot is a commissioning check, not something the installer performs automatically. Reliable wake detection and echo handling depend on the laptop's microphone placement and speakers.

### Microphone levels and missing wake-word responses

With voice installed, the shell launcher can measure input levels without restarting anything or changing settings:

```sh
sh ~/pcx86-kiosk-install.sh --voice --check-microphone
```

It displays the currently active wake word from HA's saved preferences, physical mute/volume and five seconds of signal levels. Press Enter and speak near the microphone. Audio stays in memory and is not saved. A clear increase while speaking confirms capture; near-silent values need an input/mute check. A large clipped percentage indicates excessive gain or microphone boost, which can prevent recognition even though the microphone is working. Lower physical capture gain in the XFCE audio settings and repeat. A value above 100% can worsen clipping; increasing gain is not a general fix. Software microphone volume in HA cannot restore an already distorted input. The installer preserves physical gain rather than guessing one value for every laptop.

HA can change the active wake word during setup. Check the diagnostic's **Active wake words** or the device's voice configuration: saying Hey Jarvis will not activate a satellite configured for Okay Nabu. If capture is clear and the correct word still fails, tune sensitivity in small steps as described in [LVA's configuration guide](https://github.com/OHF-Voice/linux-voice-assistant/blob/main/docs/configuration.md).

### Adjust microphone gain if the panel does not respond

Adding the satellite through ESPHome does not calibrate the physical microphone gain. The Debian installer preserves the existing setting. Before increasing it, confirm that the satellite is connected to HA, both microphone mute controls are off, and you are saying the active wake word shown in HA.

1. Run the microphone check above and speak at the distance where you intend to use the panel. If the input is already clipping, reduce gain instead of increasing it.
2. In HA, open the panel's **PCX86-kiosk** device and adjust **Microphone volume** (**Hlasitost mikrofonu** in Czech). This controls the default PulseAudio input. Make sure that default input is also the microphone selected for voice; with multiple microphones, use the Debian audio settings or the explicit source commands below instead.
3. If speech levels are too low, increase the current value in small steps, for example from 15% to 20%, then 25%, testing the wake word after each change. These are examples, not universal settings: the required level depends on the microphone and distance. Avoid jumping directly to 100% or above.
4. Repeat the microphone check. Aim for a clear increase while speaking with little or no clipping. Excessive gain or microphone boost can distort speech and make recognition worse.
5. Once the wake word triggers, wait for the listening sound and say a short request. Verify the spoken response as well as the wake sound. No HA restart is needed for gain changes.

On the Debian desktop, the equivalent control is in the sound settings under **Input Devices** (for example, `pavucontrol` if installed). Select the intended microphone, ensure it is unmuted and adjust its input volume. To use a terminal, list source IDs and replace `SOURCE_ID` below with the microphone ID configured for voice:

```sh
pactl list short sources
pactl get-source-volume SOURCE_ID
pactl set-source-volume SOURCE_ID 25%
```

The last command sets an example test level; choose a level appropriate to the measurements. Note the previous value so it can be restored. The ESPHome voice device's separate **Mic Volume** setting scales audio inside LVA; it does not replace physical input calibration or repair clipping that has already occurred.

## Disable or remove voice

Disable automatic startup and stop microphone capture:

```sh
sudo systemctl disable --now pcx86-voice.service
```

For removal, delete the voice ESPHome entry in HA. After stopping voice, remove `/etc/systemd/system/pcx86-voice.service`, `/etc/systemd/system/pcx86-voice-network.service`, `/usr/local/lib/pcx86-kiosk/voice-network` and `/etc/pcx86-kiosk/voice.nft`, then run `sudo systemctl daemon-reload`. Delete only the dedicated firewall table with `sudo nft delete table inet pcx86_voice` if it exists. Remove the three per-user `pcx86-voice` directories listed above if you no longer need their settings. Keep shared audio packages and Sendspin in place.
