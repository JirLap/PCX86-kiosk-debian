"""Report microphone signal levels without saving audio or changing settings.

Run through install.sh --voice --check-microphone, which selects the voice venv.
"""

import json
from pathlib import Path
import subprocess

import numpy as np
import soundcard as sc


def main():
    directory = Path.home()/".config/pcx86-voice"
    config = json.loads((directory/"config.json").read_text())
    preferences_path = directory/"preferences.json"
    preferences = json.loads(preferences_path.read_text()) if preferences_path.exists() else {}
    active_words = preferences.get("active_wake_words", [config["wake_word"]])
    print("Active wake words:", ", ".join(word for word in active_words if word) or "none")
    print("Microphone:", config["input_device"])
    subprocess.run(["pactl","get-source-mute",config["input_device"]],check=True)
    subprocess.run(["pactl","get-source-volume",config["input_device"]],check=True)
    input("Press Enter, then speak near the microphone for five seconds. Audio stays in memory: ")
    mic = sc.get_microphone(config["input_device"])
    with mic.recorder(samplerate=16000,channels=1) as recording:
        for second in range(5):
            samples = recording.record(numframes=16000)
            rms = float(np.sqrt(np.mean(samples*samples)))
            peak = float(np.max(np.abs(samples)))
            clipped = float(np.mean(np.abs(samples)>=0.99)*100)
            print(f"{second+1}s: RMS {20*np.log10(max(rms,1e-12)):.1f} dBFS; "
                  f"peak {20*np.log10(max(peak,1e-12)):.1f} dBFS; clipped {clipped:.2f}%",flush=True)
            if clipped > 1:
                print("WARNING: input is clipping. Reduce physical microphone gain/boost and repeat.")
    print("Check for a clear level increase while speaking, without clipping. "
          "If levels stay near silence, verify input source, physical mute and microphone hardware.")
    print("This checks capture, not wake-word recognition. Use the active wake word shown above in HA.")


if __name__ == "__main__":
    main()
