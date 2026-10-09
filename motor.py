# motor.py
#
# Flycat 2804 / 140KV BLDC
# Pico 2 W + Sparkfun TMC6300 + AS5600 Encoder
#
# Smooth open-loop sinusoidal drive with rotor alignment lock-in.

import time
import math
from machine import Pin, I2C, PWM

# ============================================================
# PIN CONFIGURATION (Matches MacroPad Controller Board.kicad_pcb)
# ============================================================
UH, UL = 20, 21  # Phase U (TP19)
VH, VL = 18, 19  # Phase V (TP20)
WH, WL = 16, 17  # Phase W (TP21)

ENC_SDA = 26     # AS5600 SDA (TP23 / Net /B)
ENC_SCL = 27     # AS5600 SCL (TP22 / Net /A)

# ============================================================
# MOTOR / DRIVE PARAMETERS
# ============================================================
POLE_PAIRS = 7
PWM_HZ = 20000
DEAD_NS = 300

# Amplitudes (0.0 to 1.0):
# 0.50 amplitude on 5V supply = 2.5V pk-pk -> ~100mA on 24 ohm motor.
# Solid holding torque and smooth rotation without excessive heating.
ALIGN_AMP = 0.50
RUN_AMP = 0.55

# Motion Timing:
ALIGN_S = 1.0      # Lock rotor at electrical angle 0 before spinning
RAMP_UP_S = 10.0   # Smooth ramp up from 0 to FMAX
HOLD_S = 5.0       # Hold at top speed
DECEL_S = 6.0      # Smooth deceleration back to 0
FMAX_HZ = 35.0     # 35 Hz electrical = (35 / 7) * 60 = 300 RPM mechanical

# ============================================================
# AS5600 ENCODER
# ============================================================
AS5600_ADDR = 0x36
AS5600_RAW_ANGLE = 0x0C

class AS5600:
    def __init__(self, i2c):
        self.i2c = i2c
        self.last_raw = self.read_raw()
        self.total_counts = 0
        self.drops = 0

    def read_raw(self):
        try:
            data = self.i2c.readfrom_mem(AS5600_ADDR, AS5600_RAW_ANGLE, 2)
            return ((data[0] & 0x0F) << 8) | data[1]
        except OSError:
            self.drops += 1
            return self.last_raw if hasattr(self, 'last_raw') else 0

    def update(self):
        raw = self.read_raw()
        delta = raw - self.last_raw
        if delta > 2048:
            delta -= 4096
        elif delta < -2048:
            delta += 4096
        self.total_counts += delta
        self.last_raw = raw
        return self.total_counts

    def angle_degrees(self):
        raw = self.read_raw()
        return raw * 360.0 / 4096.0

def find_encoder():
    try:
        i2c = I2C(1, sda=Pin(ENC_SDA), scl=Pin(ENC_SCL), freq=400000)
        devices = i2c.scan()
        print("I2C devices found:", [hex(x) for x in devices])
        if AS5600_ADDR in devices:
            return i2c
    except Exception as e:
        print("Hardware I2C error:", e)
    return None

# ============================================================
# MOTOR DRIVER (TMC6300 6-PWM)
# ============================================================
class Motor:
    def __init__(self):
        self.high = []
        self.low = []
        self.dead = int(65535 * DEAD_NS * PWM_HZ / 1_000_000_000)

        for hp, lp in ((UH, UL), (VH, VL), (WH, WL)):
            h = PWM(Pin(hp), freq=PWM_HZ, duty_u16=0)
            l = PWM(Pin(lp), freq=PWM_HZ, duty_u16=65535, invert=True)
            self.high.append(h)
            self.low.append(l)

    def set_phase(self, index, duty):
        duty = max(0.0, min(1.0, duty))
        d = int(duty * 65535)
        high_duty = max(0, d - self.dead // 2)
        low_duty = min(65535, d + self.dead // 2)
        self.high[index].duty_u16(high_duty)
        self.low[index].duty_u16(low_duty)

    def off(self):
        for p in self.high + self.low:
            try:
                p.deinit()
            except Exception:
                pass
        for pin in (UH, UL, VH, VL, WH, WL):
            Pin(pin, Pin.OUT, value=0)

# ============================================================
# THREE PHASE SINUSOIDAL DRIVE
# ============================================================
PHASE_120 = 2.0 * math.pi / 3.0

def drive(motor, electrical_angle, amplitude):
    a = math.sin(electrical_angle)
    b = math.sin(electrical_angle - PHASE_120)
    c = math.sin(electrical_angle - 2.0 * PHASE_120)

    motor.set_phase(0, 0.5 + 0.5 * amplitude * a)
    motor.set_phase(1, 0.5 + 0.5 * amplitude * b)
    motor.set_phase(2, 0.5 + 0.5 * amplitude * c)

# ============================================================
# MOTION PROFILE (Alignment -> Smooth Ramp -> Hold -> Decel)
# ============================================================
def get_target(t):
    # Phase 1: Rotor alignment lock-in (0 Hz DC field at theta=0)
    if t < ALIGN_S:
        return 0.0, ALIGN_AMP, "ALIGN"

    # Phase 2: Smooth acceleration ramp from 0 Hz
    t_ramp = t - ALIGN_S
    if t_ramp < RAMP_UP_S:
        x = t_ramp / RAMP_UP_S
        smooth = x * x * (3.0 - 2.0 * x)
        hz = FMAX_HZ * smooth
        return hz, RUN_AMP, "RAMP "

    # Phase 3: Hold at max speed
    t_hold = t_ramp - RAMP_UP_S
    if t_hold < HOLD_S:
        return FMAX_HZ, RUN_AMP, "HOLD "

    # Phase 4: Smooth deceleration to 0 Hz
    t_decel = t_hold - HOLD_S
    if t_decel < DECEL_S:
        x = 1.0 - (t_decel / DECEL_S)
        smooth = x * x * (3.0 - 2.0 * x)
        hz = FMAX_HZ * smooth
        return hz, RUN_AMP, "DECEL"

    return 0.0, 0.0, "DONE "

# ============================================================
# MAIN TEST EXECUTION
# ============================================================
def run_motor_test():
    total_time = ALIGN_S + RAMP_UP_S + HOLD_S + DECEL_S
    target_max_rpm = (FMAX_HZ / POLE_PAIRS) * 60.0

    print("========================================")
    print("FLYCAT 2804 / TMC6300 CONTROL TEST")
    print("========================================")
    print(f"Alignment Time:  {ALIGN_S:.1f} s (Amp: {ALIGN_AMP*100:.0f}%)")
    print(f"Ramp Up Time:    {RAMP_UP_S:.1f} s")
    print(f"Hold Time:       {HOLD_S:.1f} s")
    print(f"Decel Time:      {DECEL_S:.1f} s")
    print(f"Max Target Freq: {FMAX_HZ:.1f} Hz ({target_max_rpm:.1f} RPM)")
    print(f"Run Amplitude:   {RUN_AMP*100:.0f}%")
    print("========================================\n")

    i2c = find_encoder()
    enc = AS5600(i2c) if i2c else None
    if enc:
        print("AS5600: Connected and calibrated")
    else:
        print("AS5600: Warning - not detected, proceeding in pure open-loop")

    motor = Motor()
    electrical_angle = 0.0
    start_time = time.ticks_ms()
    last_time = start_time
    last_encoder_time = start_time
    last_encoder_counts = enc.total_counts if enc else 0
    filtered_rpm = 0.0

    try:
        while True:
            now = time.ticks_ms()
            elapsed = time.ticks_diff(now, start_time) / 1000.0

            if elapsed >= total_time:
                break

            dt = time.ticks_diff(now, last_time) / 1000.0
            last_time = now
            if dt <= 0:
                continue
            if dt > 0.05:
                dt = 0.05

            target_hz, amplitude, stage = get_target(elapsed)

            # Advance angle only when target_hz > 0
            electrical_angle += 2.0 * math.pi * target_hz * dt
            if electrical_angle > 100000.0:
                electrical_angle -= 100000.0

            drive(motor, electrical_angle, amplitude)

            if enc:
                enc.update()
                if time.ticks_diff(now, last_encoder_time) >= 150:
                    enc_dt = time.ticks_diff(now, last_encoder_time) / 1000.0
                    delta = enc.total_counts - last_encoder_counts
                    raw_rpm = (delta / 4096.0 / enc_dt) * 60.0
                    filtered_rpm = filtered_rpm * 0.70 + raw_rpm * 0.30

                    cmd_rpm = (target_hz / POLE_PAIRS) * 60.0
                    print(f"[{stage}] t={elapsed:5.1f}s | Commanded: {cmd_rpm:5.1f} RPM ({target_hz:4.1f} Hz) | Measured: {filtered_rpm:6.1f} RPM")

                    last_encoder_counts = enc.total_counts
                    last_encoder_time = now

            time.sleep_us(500)

    except KeyboardInterrupt:
        print("\nInterrupted by user!")
    finally:
        motor.off()
        print("\nMotor safely turned OFF.")

    if enc:
        print(f"AS5600 I2C drops: {enc.drops}")
    print("Test Complete.")

# Run
run_motor_test()