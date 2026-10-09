# knob_controller.py -- Pico 2 W Firmware (No-Haptic Mode)
#
# Encoder-only controller: AS5600 angle -> serial output + NeoPixel LEDs.
# Motor/TMC6300 is NOT used. All motor PWM pins left idle.
#
# Button behavior:
#   SW4 held + turn = Volume (0-100%)     -> sends VOL:<n>
#   SW5 held + turn = Brightness (0-100%) -> sends BRI:<n>
#   SW3 held + turn = Scroll              -> sends SCROLL:+1 / SCROLL:-1
#   SW6 press       = Play/Pause toggle   -> sends BTN:SW6
#
# USB Serial protocol (one line per event):
#   VOL:<0-100>   BRI:<0-100>   SCROLL:+1 / SCROLL:-1   BTN:<name>   STATUS:<info>

import sys, time, select
from machine import Pin, I2C
import neopixel

# ============================================================
# PINS
# ============================================================
ENC_SDA, ENC_SCL = 26, 27     # AS5600 on I2C1: GP26, GP27
LED_PIN, N_LEDS  = 15, 20     # NeoPixel ring: GP15

BUTTON_PINS = {
    "SW4":   9,       # Volume     (Net /S4  -> GP9)
    "SW5":   1,       # Brightness (Net /S5  -> GP1)
    "SW3":  10,       # Scroll     (Net /S3  -> GP10)
    "SW6":   0,       # Play/Pause (Net /S6  -> GP0)
    "SW1":  12,       # Alias for SW4
    "SW2":  11,       # Alias for SW5
    "BTN_1": 13,      # Alias for SW4
    "BTN_2": 14,      # Alias for SW5
}

# ============================================================
# TUNING
# ============================================================
DEG_PER_PERCENT = 3.0    # 300 deg full travel = 100%
MIN_DEG         = 0.0
MAX_DEG         = 300.0
SCROLL_STEP_DEG = 18.0   # encoder degrees per scroll click
LED_LEVEL       = 40     # NeoPixel brightness 0-255
ENC_DIR         = -1.0   # flip to 1.0 if CW should be positive

# ============================================================
# AS5600 CONTINUOUS MULTI-TURN ENCODER
# ============================================================
AS5600_ADDR      = 0x36
AS5600_RAW_ANGLE = 0x0C

class AS5600:
    def __init__(self, i2c):
        self.i2c          = i2c
        self.total_counts = 0
        self.last_raw     = self._read_raw()

    def _read_raw(self):
        try:
            d = self.i2c.readfrom_mem(AS5600_ADDR, AS5600_RAW_ANGLE, 2)
            return ((d[0] & 0x0F) << 8) | d[1]
        except OSError:
            return self.last_raw

    def update(self):
        raw   = self._read_raw()
        delta = raw - self.last_raw
        if delta >  2048: delta -= 4096
        if delta < -2048: delta += 4096
        self.total_counts += delta
        self.last_raw      = raw

    def degrees(self):
        return (self.total_counts / 4096.0) * 360.0

# ============================================================
# NEOPIXEL HELPERS
# ============================================================
np = neopixel.NeoPixel(Pin(LED_PIN), N_LEDS)

GREEN = (0,         LED_LEVEL, 0)
RED   = (LED_LEVEL, 0,         0)
CYAN  = (0,         LED_LEVEL, LED_LEVEL)
WHITE = (LED_LEVEL, LED_LEVEL, LED_LEVEL)

def leds_off():
    np.fill((0, 0, 0)); np.write()

def leds_bar(pct, lo=GREEN, hi=RED):
    t   = max(0.0, min(1.0, pct / 100.0))
    col = tuple(int(lo[k] + (hi[k] - lo[k]) * t) for k in range(3))
    lit = round(t * N_LEDS)
    for i in range(N_LEDS):
        np[i] = col if i < lit else (0, 0, 0)
    np.write()

def leds_chase(deg, color=CYAN):
    idx = int((deg % 360.0) / 360.0 * N_LEDS) % N_LEDS
    np.fill((0, 0, 0))
    np[idx] = color
    np.write()

def leds_solid(color):
    np.fill(color); np.write()

# ============================================================
# HARDWARE INIT
# ============================================================
i2c = I2C(1, sda=Pin(ENC_SDA), scl=Pin(ENC_SCL), freq=400000)
enc = AS5600(i2c)

btn = {}
for name, pin_num in BUTTON_PINS.items():
    try:
        btn[name] = Pin(pin_num, Pin.IN, Pin.PULL_UP)
    except Exception:
        pass

# Warm-up: let encoder settle
for _ in range(5):
    enc.update()
    time.sleep_ms(10)

origin_deg = enc.degrees()   # zero = wherever knob rests at boot

def get_deg():
    return (enc.degrees() - origin_deg) * ENC_DIR

def get_pct(deg):
    return max(0.0, min(100.0, deg / DEG_PER_PERCENT))

# ============================================================
# STATE
# ============================================================
saved_vol_pct = 50.0
saved_bri_pct = 50.0
last_sent_vol = None
last_sent_bri = None

scroll_accum  = 0.0
prev_deg      = get_deg()
active        = None

poll   = select.poll()
poll.register(sys.stdin, select.POLLIN)
rx_buf = ""

print("STATUS:READY")

last_t = time.ticks_ms()

# ============================================================
# MAIN LOOP  (~100 Hz)
# ============================================================
while True:
    now = time.ticks_ms()
    dt  = time.ticks_diff(now, last_t) / 1000.0
    last_t = now
    if dt <= 0: dt = 0.01

    # --- Non-blocking serial read (INIT_VOL / INIT_BRI from PC) ---
    if poll.poll(0):
        ch = sys.stdin.read(1)
        if ch:
            if ch in ('\n', '\r'):
                line = rx_buf.strip()
                rx_buf = ""
                if line.startswith("INIT_VOL:"):
                    try: saved_vol_pct = float(line[9:])
                    except ValueError: pass
                elif line.startswith("INIT_BRI:"):
                    try: saved_bri_pct = float(line[9:])
                    except ValueError: pass
            else:
                rx_buf += ch

    # --- Encoder ---
    enc.update()
    actual_deg = get_deg()
    delta_deg  = actual_deg - prev_deg
    prev_deg   = actual_deg

    # --- Button scan ---
    def get_sw():
        for name in ("SW4", "SW5", "SW3", "SW6",
                     "SW1", "SW2", "BTN_1", "BTN_2"):
            if name in btn and btn[name].value() == 0:
                if name in ("SW4", "SW1", "BTN_1"): return "SW4"
                if name in ("SW5", "SW2", "BTN_2"): return "SW5"
                if name == "SW3":                   return "SW3"
                return name   # SW6 etc.
        return None

    sw = get_sw()

    # --- Switch transitions ---
    if sw != active:
        active = sw
        if active is None:
            leds_off()
            print("STATUS:RELEASED")
        else:
            print("STATUS:%s_ENGAGED" % active)
            if   active == "SW4": leds_bar(saved_vol_pct)
            elif active == "SW5": leds_bar(saved_bri_pct)
            elif active == "SW3": leds_off()
            elif active == "SW6":
                leds_solid(WHITE)
                print("BTN:SW6")

    # --- Mode actions ---
    if active == "SW4":
        clamped = max(MIN_DEG, min(MAX_DEG, actual_deg))
        saved_vol_pct = get_pct(clamped)
        leds_bar(saved_vol_pct)
        v = round(saved_vol_pct)
        if v != last_sent_vol:
            print("VOL:%d" % v)
            last_sent_vol = v

    elif active == "SW5":
        clamped = max(MIN_DEG, min(MAX_DEG, actual_deg))
        saved_bri_pct = get_pct(clamped)
        leds_bar(saved_bri_pct)
        b = round(saved_bri_pct)
        if b != last_sent_bri:
            print("BRI:%d" % b)
            last_sent_bri = b

    elif active == "SW3":
        scroll_accum += delta_deg
        while scroll_accum >=  SCROLL_STEP_DEG:
            print("SCROLL:+1")
            scroll_accum -= SCROLL_STEP_DEG
        while scroll_accum <= -SCROLL_STEP_DEG:
            print("SCROLL:-1")
            scroll_accum += SCROLL_STEP_DEG
        leds_chase(actual_deg, CYAN)

    # SW6 already fired BTN:SW6 on the transition edge above

    time.sleep_ms(10)   # 100 Hz
