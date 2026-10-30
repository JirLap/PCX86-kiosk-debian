# Voice Assist commissioning

The Debian kiosk agent transports device status and controls over MQTT. Home Assistant voice uses Wyoming and runs as an independent service.

Before installation, identify the exact microphone and speaker devices on each target laptop. Device identifiers and audio formats can differ between systems and must be checked locally.

1. Confirm the microphone captures speech and the speaker plays a test sound in the logged-in XFCE session.
2. Choose and install a Wyoming-compatible satellite implementation for Debian, pin its version, and run it under a dedicated unprivileged service account where practical.
3. Configure the selected capture/playback devices and the wake-word/Assist pipeline according to the satellite's current documentation.
4. Bind its Wyoming TCP listener to the LAN address, restrict access with firewall rules, and configure its host/port in Home Assistant's built-in Wyoming integration.
5. Test wake word, speech recognition, response audio, mute behavior, and behavior after reboot.

The HAOS integration does not create a fake “voice command” sensor. Home Assistant's Wyoming integration owns the satellite connection and Assist state.
