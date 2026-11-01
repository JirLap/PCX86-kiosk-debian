#!/usr/bin/python3
"""MQTT telemetry and allow-listed controls for a Debian XFCE kiosk."""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import psutil
import paho.mqtt.client as mqtt

LOGGER = logging.getLogger("pcx86_kiosk")
TOPIC_ROOT = "pcx86_kiosk"
DISCOVERY_ROOT = f"{TOPIC_ROOT}/discovery"
COMMANDS = {
    "wake", "sleep", "screen_on", "screen_off", "status",
    "chromium_start", "chromium_stop", "chromium_restart",
    "kiosk_start", "kiosk_stop", "kiosk_restart", "kiosk_refresh",
    "camera_start", "camera_stop",
    "mic_mute", "mic_unmute", "audio_mute", "audio_unmute",
    "suspend", "reboot", "shutdown",
    "screensaver_on", "screensaver_off",
}


def run(command: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str]:
    """Run a fixed argument vector without a shell."""
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def _read_first_line(path: Path) -> str | None:
    """Read one optional hardware identifier without failing on missing DMI data."""
    try:
        value = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return value or None


class PanelAgent:
    """Per-user agent that runs inside the active XFCE session."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.device_id = self.config["device_id"]
        self.root = f"{TOPIC_ROOT}/{self.device_id}"
        self.status_topic = f"{self.root}/status"
        self.command_topic = f"{self.root}/command"
        self.discovery_topic = f"{DISCOVERY_ROOT}/{self.device_id}"
        if hasattr(mqtt, "CallbackAPIVersion"):
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1,
                client_id=f"pcx86-{self.device_id}",
            )
        else:  # Compatibility with Debian packages using Paho MQTT 1.x.
            self.client = mqtt.Client(client_id=f"pcx86-{self.device_id}")
        self.client.username_pw_set(self.config["mqtt_username"], self.config["mqtt_password"])
        self.client.will_set(
            self.status_topic,
            json.dumps({"availability": "offline", "device_id": self.device_id}),
            qos=1,
            retain=True,
        )
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self._stop = threading.Event()
        self._last_command = "startup"
        self._result = "ok"
        self._last_update_check = 0.0
        self._updates_available: int | None = None
        self._update_check_running = False
        self._update_check_lock = threading.Lock()
        self._display_sleep = self.config.get("display_sleep")
        self._last_monitor = None
        self._last_idle_ms = None
        self._desktop_display_configured = False
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            LOGGER.warning("MQTT connection failed: %s", reason_code)
            return
        client.subscribe(self.command_topic, qos=1)
        self.publish_status_safely()
        try:
            self.publish_discovery()
        except Exception:
            LOGGER.exception("Cannot publish discovery; MQTT connection remains active")

    def on_disconnect(self, client, userdata, *args):
        if not self._stop.is_set():
            LOGGER.warning("MQTT disconnected: %s", args[-1] if args else "unknown reason")

    def publish_status(self):
        status = self.collect_status()
        status.update(
            availability="online",
            device_id=self.device_id,
            last_command=self._last_command,
            result=self._result,
            updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.client.publish(self.status_topic, json.dumps(status), qos=1, retain=True)

    def publish_status_safely(self):
        """A failed sensor read must not terminate the MQTT worker or main loop."""
        try:
            self.publish_status()
        except Exception:
            LOGGER.exception("Status collection failed; will retry")

    def network_loop(self):
        """Let Paho reconnect normally, and recover if an unexpected callback fails."""
        while not self._stop.is_set():
            try:
                self.client.loop_forever(retry_first_connection=True)
            except Exception:
                LOGGER.exception("MQTT worker failed; reconnecting in 5 seconds")
            if self._stop.wait(5):
                break
            self.client.connect_async(
                self.config["mqtt_host"], int(self.config.get("mqtt_port", 1883)), keepalive=45
            )

    def publish_discovery(self):
        """Publish retained metadata for Home Assistant's MQTT config-flow discovery."""
        metadata = self.collect_device_metadata()
        announcement = {
            "device_id": self.device_id,
            "name": metadata.get("hostname", f"PCX86 kiosk {self.device_id}"),
            "status_topic": self.status_topic,
            "command_topic": self.command_topic,
            **metadata,
        }
        self.client.publish(
            self.discovery_topic,
            json.dumps(announcement),
            qos=1,
            retain=True,
        )

    def collect_status(self) -> dict:
        memory = psutil.virtual_memory()
        disk = shutil.disk_usage(Path.home())
        status: dict = {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": memory.percent,
            "memory_used_bytes": memory.used,
            "disk_free_bytes": disk.free,
            "uptime_seconds": int(time.time() - psutil.boot_time()),
            "kiosk_url": self.config["kiosk_url"],
            "chromium": "running" if self.chromium_running() else "stopped",
            "power_mode": "display_sleep" if self._display_sleep else "awake",
            "screensaver_enabled": bool(self.config.get("screensaver_enabled", False)),
            "screensaver_timeout": int(self.config.get("screensaver_timeout", 10)),
        }
        cpu_temp = self.cpu_temperature()
        if cpu_temp is not None:
            status["cpu_temperature_c"] = cpu_temp
        ip_address, interface = self.network_route()
        if ip_address:
            status["ip_address"] = ip_address
        status.update(self.collect_device_metadata(ip_address, interface))
        wifi = self.wifi_signal(interface) if interface else None
        if wifi is not None:
            status["wifi_signal_dbm"] = wifi
        monitor = "off" if self._display_sleep else self.monitor_state()
        if monitor is not None:
            status["monitor"] = monitor
        status["screensaver_active"] = bool(self._display_sleep and self._display_sleep.get("reason") == "screensaver") or (not self._display_sleep and monitor == "off")
        volume = self.audio_volume()
        if volume is not None:
            status["system_volume"] = volume
        muted = self.audio_muted()
        if muted is not None:
            status["system_muted"] = muted
        mic_muted = self.microphone_muted()
        if mic_muted is not None:
            status["microphone_muted"] = mic_muted
        mic_volume = self.microphone_volume()
        if mic_volume is not None:
            status["microphone_volume"] = mic_volume
        brightness = self.brightness()
        if brightness is not None:
            status["screen_brightness"] = brightness
        status.update(self.battery_state())
        self.refresh_updates_if_due()
        if self._updates_available is not None:
            status["available_updates"] = self._updates_available
        camera = self.config.get("camera", {})
        if camera.get("snapshot_url"):
            status["camera_snapshot_url"] = camera["snapshot_url"]
        if camera.get("stream_url"):
            status["camera_stream_url"] = camera["stream_url"]
        status["camera"] = self.camera_state()
        return status

    def collect_device_metadata(
        self, ip_address: str | None = None, interface: str | None = None
    ) -> dict:
        """Return hardware and operating-system identifiers available on Linux."""
        if ip_address is None or interface is None:
            ip_address, interface = self.network_route()

        try:
            os_release = platform.freedesktop_os_release()
        except (AttributeError, OSError):
            os_release = {}

        metadata = {
            "hostname": socket.gethostname(),
            "kernel_version": platform.release(),
            "architecture": platform.machine(),
            "os_name": os_release.get("NAME", "Linux"),
            "os_version": os_release.get("PRETTY_NAME", ""),
        }
        manufacturer = _read_first_line(Path("/sys/class/dmi/id/sys_vendor"))
        model = _read_first_line(Path("/sys/class/dmi/id/product_name"))
        if manufacturer:
            metadata["manufacturer"] = manufacturer
        if model:
            metadata["model"] = model
        if ip_address:
            metadata["ip_address"] = ip_address
        if interface:
            metadata["network_interface"] = interface
            for address in psutil.net_if_addrs().get(interface, []):
                if address.family == getattr(socket, "AF_PACKET", object()):
                    mac_address = address.address.lower()
                    if mac_address and mac_address != "00:00:00:00:00:00":
                        metadata["mac_address"] = mac_address
                if address.family == socket.AF_INET and address.address == ip_address:
                    if address.broadcast:
                        metadata["broadcast_address"] = address.broadcast
            ssid = self.wifi_ssid(interface)
            if ssid:
                metadata["wifi_ssid"] = ssid
        return metadata

    @staticmethod
    def wifi_ssid(interface: str) -> str | None:
        """Return the SSID for a connected Wi-Fi interface when available."""
        result = run(["/usr/sbin/iw", "dev", interface, "link"])
        match = re.search(r"^\s*SSID:\s*(.+)$", result.stdout, re.MULTILINE)
        return match.group(1).strip() if result.returncode == 0 and match else None

    def network_route(self) -> tuple[str | None, str | None]:
        try:
            broker = self.config["mqtt_host"]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((broker, int(self.config.get("mqtt_port", 1883))))
                address = sock.getsockname()[0]
            for item in psutil.net_if_addrs().items():
                if any(address == addr.address for addr in item[1]):
                    return address, item[0]
        except (OSError, ValueError):
            pass
        return None, None

    @staticmethod
    def wifi_signal(interface: str) -> int | None:
        result = run(["/usr/sbin/iw", "dev", interface, "link"])
        match = re.search(r"Signal:\s*(-?\d+)\s*dBm", result.stdout)
        return int(match.group(1)) if result.returncode == 0 and match else None

    @staticmethod
    def chromium_pids() -> list[int]:
        pids = []
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                info = process.info
                if "chromium" in (info["name"] or "").lower() and "--kiosk" in (info["cmdline"] or []):
                    pids.append(info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return pids

    def chromium_running(self) -> bool:
        return bool(self.chromium_pids())

    @staticmethod
    def monitor_state() -> str | None:
        if not os.environ.get("DISPLAY"):
            return None
        result = run(["/usr/bin/xset", "-q"])
        match = re.search(r"Monitor is (On|Off|Standby|Suspend)", result.stdout)
        if match:
            return "on" if match.group(1) == "On" else "off"
        if "DPMS is Disabled" in result.stdout:
            return "on"  # Disabling DPMS powers the display on; core blanking is disabled.
        return None

    @staticmethod
    def audio_volume() -> int | None:
        result = run(["/usr/bin/pactl", "get-sink-volume", "@DEFAULT_SINK@"])
        match = re.search(r"/(\s*\d+)%", result.stdout)
        return int(match.group(1)) if result.returncode == 0 and match else None

    @staticmethod
    def audio_muted() -> bool | None:
        result = run(["/usr/bin/pactl", "get-sink-mute", "@DEFAULT_SINK@"])
        if result.returncode:
            return None
        return "yes" in result.stdout.lower()

    @staticmethod
    def microphone_muted() -> bool | None:
        result = run(["/usr/bin/pactl", "get-source-mute", "@DEFAULT_SOURCE@"])
        if result.returncode:
            return None
        return "yes" in result.stdout.lower()

    @staticmethod
    def microphone_volume() -> int | None:
        result = run(["/usr/bin/pactl", "get-source-volume", "@DEFAULT_SOURCE@"])
        match = re.search(r"/(\s*\d+)%", result.stdout)
        return int(match.group(1)) if result.returncode == 0 and match else None

    @staticmethod
    def cpu_temperature() -> float | None:
        try:
            groups = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        for name, readings in groups.items():
            if name.lower() not in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
                continue
            for reading in readings:
                if reading.current is not None:
                    return round(float(reading.current), 1)
        return None

    @staticmethod
    def brightness() -> int | None:
        for path in Path("/sys/class/backlight").glob("*/brightness"):
            try:
                value = int(path.read_text().strip())
                maximum = int(path.parent.joinpath("max_brightness").read_text().strip())
                return round(100 * value / maximum) if maximum else None
            except (OSError, ValueError, ZeroDivisionError):
                continue
        return None

    @staticmethod
    def battery_state() -> dict:
        for battery in Path("/sys/class/power_supply").glob("BAT*"):
            result = {}
            try:
                result["battery_percent"] = int((battery / "capacity").read_text().strip())
            except (OSError, ValueError):
                pass
            try:
                result["charging_status"] = (battery / "status").read_text().strip()
            except OSError:
                pass
            try:
                voltage = int((battery / "voltage_now").read_text().strip())
                result["battery_voltage_v"] = round(voltage / 1_000_000, 2)
            except (OSError, ValueError):
                pass
            return result
        return {}

    @staticmethod
    def camera_state() -> str:
        result = run(["/usr/bin/systemctl", "--user", "is-active", "pcx86-camera.service"])
        return "on" if result.stdout.strip() == "active" else "off"

    def refresh_updates_if_due(self):
        with self._update_check_lock:
            if self._update_check_running or time.monotonic() - self._last_update_check < int(
                self.config.get("update_check_interval", 3600)
            ):
                return
            self._last_update_check = time.monotonic()
            self._update_check_running = True
        threading.Thread(target=self._check_updates, name="apt-update-check", daemon=True).start()

    def _check_updates(self):
        try:
            result = run(
                ["/usr/bin/apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"],
                timeout=45,
            )
            if result.returncode == 0:
                self._updates_available = sum(
                    1 for line in result.stdout.splitlines() if line.startswith("Inst ")
                )
        except subprocess.TimeoutExpired:
            LOGGER.info("APT update check timed out; keeping the previous value")
        finally:
            with self._update_check_lock:
                self._update_check_running = False

    def on_message(self, client, userdata, message):
        if message.retain:
            LOGGER.warning("Ignoring retained MQTT command")
            return
        command = message.payload.decode("utf-8", errors="replace").strip()
        self._last_command = command.split(":", 1)[0]
        try:
            self._dispatch(command)
            self._result = "ok"
        except Exception as err:
            self._result = f"error: {err}"
            LOGGER.exception("Command failed: %s", self._last_command)
        finally:
            self.publish_status_safely()

    def _dispatch(self, command: str):
        if command.startswith("set_url:"):
            self.set_kiosk_url(command.partition(":")[2])
            self.restart_kiosk()
            return
        if command.startswith("volume_set:"):
            value = int(command.partition(":")[2])
            if not 0 <= value <= 150:
                raise ValueError("volume must be between 0 and 150")
            self.checked(["/usr/bin/pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"])
            return
        if command.startswith("mic_volume_set:"):
            value = int(command.partition(":")[2])
            if not 0 <= value <= 150:
                raise ValueError("microphone volume must be between 0 and 150")
            self.checked(["/usr/bin/pactl", "set-source-volume", "@DEFAULT_SOURCE@", f"{value}%"])
            return
        if command.startswith("brightness_set:"):
            self.set_brightness(int(command.partition(":")[2]))
            return
        if command.startswith("screensaver_timeout_set:"):
            value = int(command.partition(":")[2])
            if not 1 <= value <= 240:
                raise ValueError("screensaver timeout must be 1 to 240 minutes")
            self.config["screensaver_timeout"] = value
            self.write_config()
            self.apply_screensaver()
            if not self._display_sleep:
                self.checked(["/usr/bin/xset", "s", "reset"])
            return
        if command not in COMMANDS:
            raise ValueError("unsupported command")

        if command in ("wake", "screen_on"):
            self.wake_display()
        elif command in ("sleep", "screen_off", "suspend"):
            # Legacy 'suspend' commands also use the remotely recoverable mode.
            self.sleep_display()
        elif command in ("screensaver_on", "screensaver_off"):
            if command == "screensaver_on" and not shutil.which("xprintidle"):
                raise RuntimeError("Install xprintidle to enable the screensaver")
            self.config["screensaver_enabled"] = command == "screensaver_on"
            self.write_config()
            if command == "screensaver_off" and self._display_sleep and self._display_sleep.get("reason") == "screensaver":
                self.wake_display()
            self.apply_screensaver()
            if not self._display_sleep:
                self.checked(["/usr/bin/xset", "s", "reset"])
                if command == "screensaver_off":
                    self.checked(["/usr/bin/xset", "dpms", "force", "on"])
        elif command in ("chromium_start", "kiosk_start"):
            self.start_kiosk()
        elif command in ("chromium_stop", "kiosk_stop"):
            self.stop_kiosk()
        elif command in ("chromium_restart", "kiosk_restart"):
            self.restart_kiosk()
        elif command == "kiosk_refresh":
            if shutil.which("xdotool"):
                result = run([shutil.which("xdotool"), "key", "--clearmodifiers", "ctrl+r"])
                if result.returncode:
                    self.restart_kiosk()
            else:
                self.restart_kiosk()
        elif command == "mic_mute":
            self.checked(["/usr/bin/pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1"])
        elif command == "mic_unmute":
            self.checked(["/usr/bin/pactl", "set-source-mute", "@DEFAULT_SOURCE@", "0"])
        elif command == "audio_mute":
            self.checked(["/usr/bin/pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"])
        elif command == "audio_unmute":
            self.checked(["/usr/bin/pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])
        elif command == "camera_start":
            self.checked(["/usr/bin/systemctl", "--user", "start", "pcx86-camera.service"])
        elif command == "camera_stop":
            self.checked(["/usr/bin/systemctl", "--user", "stop", "pcx86-camera.service"])
        elif command == "reboot":
            self.start_power_action("reboot")
        elif command == "shutdown":
            self.start_power_action("shutdown")
        elif command == "status":
            return

    @staticmethod
    def checked(command: list[str], timeout: int = 8):
        result = run(command, timeout=timeout)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or f"command exited {result.returncode}")

    @staticmethod
    def start_power_action(action: str):
        """Start only the root helper's fixed power actions without waiting for resume."""
        if action not in ("reboot", "shutdown"):
            raise ValueError("unsupported power action")
        process = subprocess.Popen(
            ["/usr/bin/sudo", "-n", "/usr/local/sbin/pcx86-power", action],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.15)
        if process.poll() not in (None, 0):
            detail = process.stderr.read().strip() if process.stderr else ""
            raise RuntimeError(detail or f"power helper exited {process.returncode}")

    def apply_screensaver(self):
        """Own idle handling; native timers can be inhibited by kiosk video."""
        self.configure_desktop_display()
        self.checked(["/usr/bin/xset", "s", "off"])
        self.checked(["/usr/bin/xset", "+dpms"])
        self.checked(["/usr/bin/xset", "dpms", "0", "0", "0"])

    def configure_desktop_display(self):
        """Keep XFCE from independently blanking or disabling manual DPMS."""
        if getattr(self, "_desktop_display_configured", False):
            return
        xfconf = shutil.which("xfconf-query")
        if xfconf:
            for key, kind, value in (
                ("dpms-enabled", "bool", "true"),
                ("dpms-on-ac-sleep", "uint", "0"),
                ("dpms-on-ac-off", "uint", "0"),
                ("dpms-on-battery-sleep", "uint", "0"),
                ("dpms-on-battery-off", "uint", "0"),
                ("blank-on-ac", "int", "0"),
                ("blank-on-battery", "int", "0"),
            ):
                prop = "/xfce4-power-manager/" + key
                current = run([xfconf, "-c", "xfce4-power-manager", "-p", prop])
                if current.returncode == 0 and current.stdout.strip() == value:
                    continue
                args = [xfconf, "-c", "xfce4-power-manager", "-p", prop, "-s", value]
                if current.returncode != 0:
                    args += ["--create", "--type", kind]
                self.checked(args)
        self._desktop_display_configured = True

    def check_screensaver(self):
        """Track actual X11 input idle time; local input cancels automatic sleep."""
        automatic_sleep = self._display_sleep and self._display_sleep.get("reason") == "screensaver"
        if (self._display_sleep and not automatic_sleep) or (not self.config.get("screensaver_enabled", False) and not automatic_sleep):
            return
        tool = shutil.which("xprintidle")
        if not tool:
            return
        result = run([tool])
        if result.returncode != 0 or not result.stdout.strip().isdigit():
            return
        idle_ms = int(result.stdout.strip())
        previous = self._last_idle_ms
        self._last_idle_ms = idle_ms
        if self._display_sleep:
            if self._display_sleep.get("reason") == "screensaver" and previous is not None and idle_ms < previous:
                self.wake_display()
            return
        minutes = max(1, min(240, int(self.config.get("screensaver_timeout", 10))))
        if self.config.get("screensaver_enabled", False) and idle_ms >= minutes * 60_000:
            self.sleep_display(reason="screensaver")

    def sleep_display(self, reason: str = "manual"):
        """Stop rendering and turn off the display while retaining SSH/MQTT/audio."""
        if not self._display_sleep:
            self._display_sleep = {
                "brightness": self.brightness(),
                "restore_kiosk": self.chromium_running(),
                "reason": reason,
            }
            self.config["display_sleep"] = self._display_sleep
            self.write_config()
        self.stop_kiosk()
        self.apply_screensaver()
        self.checked(["/usr/bin/xset", "dpms", "force", "off"])
        if self._display_sleep.get("brightness") is not None:
            self.set_brightness(0)

    def wake_display(self):
        """Restore the saved brightness and only the browser that was running."""
        saved = self._display_sleep or {}
        self.apply_screensaver()
        self.checked(["/usr/bin/xset", "s", "reset"])
        self.checked(["/usr/bin/xset", "dpms", "force", "on"])
        if saved.get("brightness") is not None:
            self.set_brightness(saved["brightness"])
        if saved.get("restore_kiosk"):
            self.start_kiosk()
        self._display_sleep = None
        self.config.pop("display_sleep", None)
        self.write_config()

    def set_kiosk_url(self, value: str):
        if len(value) > 2048:
            raise ValueError("URL must be 2048 characters or fewer")
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError("URL must be an http(s) address without embedded credentials")
        self.config["kiosk_url"] = value
        self.write_config()

    def write_config(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.config_path.parent,
            delete=False, prefix=".agent-", suffix=".json",
        ) as temporary:
            json.dump(self.config, temporary, indent=2)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(self.config_path)

    def start_kiosk(self):
        if self.chromium_running():
            return
        browser = self.config.get("browser", "/usr/bin/chromium")
        store = self.config.get("browser_password_store")
        if store is None:
            # Preserve an existing kiosk launcher's explicit storage choice.
            try:
                legacy = (Path.home() / ".config/autostart/chromium.desktop").read_text()
            except OSError:
                legacy = ""
            store = "basic" if "--password-store=basic" in legacy else "auto"
        if store not in ("auto", "basic"):
            raise ValueError("browser_password_store must be auto or basic")
        password_store_args = ["--password-store=basic"] if store == "basic" else []
        profile = self.config.get("browser_profile")
        profile_args = ["--user-data-dir=" + str(profile)] if profile else []
        subprocess.Popen(
            [
                browser,
                *password_store_args,
                *profile_args,
                "--kiosk",
                "--app=" + self.config["kiosk_url"],
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=9222",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop_kiosk(self):
        pids = self.chromium_pids()
        processes = []
        for pid in pids:
            try:
                processes.append(psutil.Process(pid))
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
        _, alive = psutil.wait_procs(processes, timeout=3)
        if alive:
            raise RuntimeError("Kiosk browser did not stop within three seconds")

    def restart_kiosk(self):
        self.stop_kiosk()
        time.sleep(1)
        self.start_kiosk()

    @staticmethod
    def set_brightness(value: int):
        if not 0 <= value <= 100:
            raise ValueError("brightness must be between 0 and 100")
        PanelAgent.checked([
            "/usr/bin/sudo", "-n", "/usr/local/sbin/pcx86-power", "brightness", str(value)
        ])

    def run(self):
        if self._display_sleep:
            self.sleep_display()
        else:
            self.apply_screensaver()
            self.start_kiosk()
        self.client.connect_async(
            self.config["mqtt_host"], int(self.config.get("mqtt_port", 1883)), keepalive=45
        )
        worker = threading.Thread(target=self.network_loop, name="pcx86-mqtt", daemon=True)
        worker.start()
        next_status = 0.0
        try:
            while not self._stop.wait(2):
                now = time.monotonic()
                try:
                    self.check_screensaver()
                except Exception:
                    LOGGER.exception("Cannot update screensaver; will retry")
                try:
                    monitor = self.monitor_state()
                except (OSError, subprocess.TimeoutExpired):
                    monitor = self._last_monitor
                changed = monitor != self._last_monitor
                self._last_monitor = monitor
                if self.client.is_connected() and (changed or now >= next_status):
                    self.publish_status_safely()
                    next_status = now + max(2, int(self.config.get("status_interval", 30)))
        finally:
            self._stop.set()
            if self.client.is_connected():
                self.client.publish(
                    self.status_topic,
                    json.dumps({"availability": "offline", "device_id": self.device_id}),
                    qos=1,
                    retain=True,
                )
            self.client.disconnect()
            worker.join(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    agent = PanelAgent(args.config)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: agent._stop.set())
    agent.run()


if __name__ == "__main__":
    main()
