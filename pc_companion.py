# pc_companion.py -- runs on your laptop (Mac or Windows), not on the Pico.
#
# Install first:   pip install pyserial pynput screen_brightness_control pycaw comtypes
#
# Communicates with knob_controller.py over USB serial:
#   VOL:<0-100>   BRI:<0-100>   SCROLL:+1 / SCROLL:-1   BTN:<name>

import platform
import subprocess
import sys
import time
import threading
import serial
from pynput.mouse import Controller as MouseController
from pynput.keyboard import Controller as KeyboardController, Key

PORT = "COM3"          # Check Device Manager on Windows (e.g. COM3)
BAUD = 115200

OS_NAME = platform.system()   # "Windows", "Darwin" (Mac), "Linux"
mouse = MouseController()
keyboard = KeyboardController()

# ---------------------------------------------------------------- volume
_last_known_vol = 50
_audio_warning_shown = False

def get_volume_windows():
    try:
        from pycaw.pycaw import AudioUtilities
        speakers = AudioUtilities.GetSpeakers()
        if hasattr(speakers, 'EndpointVolume'):
            return round(speakers.EndpointVolume.GetMasterVolumeLevelScalar() * 100.0)
    except Exception:
        pass
    return 50

def set_volume_windows(pct):
    global _last_known_vol, _audio_warning_shown
    try:
        from pycaw.pycaw import AudioUtilities
        speakers = AudioUtilities.GetSpeakers()
        if hasattr(speakers, 'EndpointVolume'):
            speakers.EndpointVolume.SetMasterVolumeLevelScalar(pct / 100.0, None)
        else:
            from ctypes import POINTER, cast
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import IAudioEndpointVolume
            interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            volume.SetMasterVolumeLevelScalar(pct / 100.0, None)
        _last_known_vol = pct
        _audio_warning_shown = False
    except Exception as e:
        # If no active playback device is detected in Windows, fall back to media keys
        if not _audio_warning_shown:
            print("[Volume note] No active speaker/headphone found in Windows (disabled or unplugged). Falling back to media keys.")
            _audio_warning_shown = True
        delta = pct - _last_known_vol
        steps = max(1, int(round(abs(delta) / 2.0)))
        key = Key.media_volume_up if delta > 0 else Key.media_volume_down
        for _ in range(steps):
            keyboard.press(key)
            keyboard.release(key)
        _last_known_vol = pct

def set_volume_mac(pct):
    subprocess.run(["osascript", "-e", "set volume output volume %d" % pct], check=False)

def set_volume(pct):
    try:
        if OS_NAME == "Windows":
            set_volume_windows(pct)
        elif OS_NAME == "Darwin":
            set_volume_mac(pct)
        print(f"[Volume] {pct}%")
    except Exception as e:
        print("[Volume error]:", e)

# ---------------------------------------------------------------- brightness (Non-Blocking)
# DDC/CI monitor communication can take 1-3 seconds per command.
# We process brightness in a dedicated background worker, always applying the latest target
# to ensure the serial loop NEVER blocks or accumulates lag!
_pending_brightness = [None]
_brightness_lock = threading.Lock()

def _brightness_worker():
    import screen_brightness_control as sbc
    while True:
        target = None
        with _brightness_lock:
            if _pending_brightness[0] is not None:
                target = _pending_brightness[0]
                _pending_brightness[0] = None
        if target is not None:
            try:
                sbc.set_brightness(target)
                print(f"[Brightness Applied] {target}%")
            except Exception as e:
                print(f"[Brightness note]: {e}")
        time.sleep(0.04)

threading.Thread(target=_brightness_worker, daemon=True).start()

def set_brightness(pct):
    with _brightness_lock:
        _pending_brightness[0] = pct
    print(f"[Brightness Target] {pct}%")

# ---------------------------------------------------------------- scroll
def scroll(direction):
    mouse.scroll(0, direction * 2)   # +1/-1 from Pico

# ---------------------------------------------------------------- macro buttons
def handle_macro_button(btn_name):
    try:
        if btn_name in ("SW6", "S6"):
            print("[Button SW6] Media Play / Pause")
            keyboard.press(Key.media_play_pause)
            keyboard.release(Key.media_play_pause)
    except Exception as e:
        print(f"[Macro error]: {e}")

# ---------------------------------------------------------------- main loop
def main():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except Exception as e:
        print("Couldn't open", PORT, "-", e)
        print("Make sure Thonny is closed and PORT matches your Pico.")
        sys.exit(1)

    print(f"Connected to {PORT} - MacroPad Knob Controller is active!\n")
    print("Controls:")
    print("  - SW4 (or SW1) + Turn: Volume (0-100% with haptic endstops)")
    print("  - SW5 (or SW2) + Turn: Brightness (0-100% with haptic endstops)")
    print("  - SW3 + Turn: Free-spin mouse scroll")
    print("  - SW6: Play / Pause Media\n")

    # Sync initial Windows volume to Pico
    if OS_NAME == "Windows":
        initial_vol = get_volume_windows()
        time.sleep(0.5)
        ser.write(f"INIT_VOL:{initial_vol}\n".encode())
        print(f"Synced initial system volume: {initial_vol}%")

    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        try:
            if line.startswith("VOL:"):
                set_volume(int(line[4:]))
            elif line.startswith("BRI:"):
                set_brightness(int(line[4:]))
            elif line.startswith("SCROLL:"):
                direction = 1 if line[7:] == "+1" else -1
                scroll(direction)
            elif line.startswith("BTN:"):
                handle_macro_button(line[4:])
            elif line.startswith("STATUS:"):
                print(f"[Pico Status] {line[7:]}")
            else:
                print(f"[Pico] {line}")
        except Exception as e:
            print("Error handling line %r: %s" % (line, e))

if __name__ == "__main__":
    main()
