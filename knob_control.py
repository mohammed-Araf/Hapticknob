# knob_control.py
#
# Closed-Loop Haptic Rubber-Band Controller for Flycat 2804 + TMC6300 + AS5600
# Raspberry Pi Pico 2 W (RP2350)
#
# Behavior:
# - Follows target_deg from UI
# - Acts as a virtual rubber band / torsional spring:
#   If physically forced away from target, it resists with continuous restoring torque.
#   As soon as released, it snaps right back to the target position!

import sys
import time
import math
import select
from machine import Pin, I2C, PWM

# ============================================================
# PINS (Matches MacroPad Controller Board.kicad_pcb)
# ============================================================
UH, UL = 20, 21
VH, VL = 18, 19
WH, WL = 16, 17

ENC_SDA = 26
ENC_SCL = 27

POLE_PAIRS = 7
PWM_HZ = 20000
DEAD_NS = 300

# ============================================================
# RUBBER-BAND / HAPTIC SPRING PARAMETERS
# ============================================================
SPRING_K = 2.2         # Spring stiffness (lead angle per degree of error)
DAMPING_D = 0.04       # Damping to eliminate overshoot / ringing
MAX_LEAD_DEG = 12.5    # Max mechanical lead angle (approx 87 deg electrical = max torque)
MIN_HOLD_AMP = 0.25    # Holding amplitude at rest
MAX_SPRING_AMP = 0.65  # Max restoring amplitude when stretched

# ============================================================
# AS5600 DRIVER
# ============================================================
AS5600_ADDR = 0x36
AS5600_RAW_ANGLE = 0x0C

class AS5600:
    def __init__(self, i2c):
        self.i2c = i2c
        self.total_counts = 0
        self.last_raw = self.read_raw()
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

    def get_continuous_deg(self):
        return (self.total_counts / 4096.0) * 360.0

# ============================================================
# MOTOR DRIVER
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

PHASE_120 = 2.0 * math.pi / 3.0

def drive(motor, electrical_angle, amplitude):
    a = math.sin(electrical_angle)
    b = math.sin(electrical_angle - PHASE_120)
    c = math.sin(electrical_angle - 2.0 * PHASE_120)

    motor.set_phase(0, 0.5 + 0.5 * amplitude * a)
    motor.set_phase(1, 0.5 + 0.5 * amplitude * b)
    motor.set_phase(2, 0.5 + 0.5 * amplitude * c)

# ============================================================
# MAIN HAPTIC LOOP
# ============================================================
def main():
    i2c = I2C(1, sda=Pin(ENC_SDA), scl=Pin(ENC_SCL), freq=400000)
    enc = AS5600(i2c)
    motor = Motor()

    # Initial electrical alignment
    drive(motor, 0.0, 0.45)
    time.sleep_ms(600)

    for _ in range(10):
        enc.update()
        time.sleep_ms(10)

    initial_deg = enc.get_continuous_deg()

    # Note: MOTOR_DIR is 1.0 (Normal).
    # Encoder count direction is negated so clockwise physical rotation gives positive actual_deg!
    motor_dir = 1.0
    enc_dir = -1.0 

    target_deg = 0.0
    prev_actual_deg = 0.0

    # Local copies of tuning parameters
    spring_k = SPRING_K
    damping_d = DAMPING_D
    max_lead = MAX_LEAD_DEG
    min_hold_amp = MIN_HOLD_AMP
    max_spring_amp = MAX_SPRING_AMP

    poll_obj = select.poll()
    poll_obj.register(sys.stdin, select.POLLIN)

    rx_buf = ""
    last_time = time.ticks_ms()
    last_telemetry_time = last_time
    last_speed_time = last_time
    last_speed_angle = 0.0
    filtered_rpm = 0.0

    print("STATUS:HAPTIC_READY")

    try:
        while True:
            now = time.ticks_ms()
            dt = time.ticks_diff(now, last_time) / 1000.0
            last_time = now

            if dt <= 0:
                dt = 0.001
            if dt > 0.02:
                dt = 0.02

            # 1. Non-blocking serial command processing
            if poll_obj.poll(0):
                ch = sys.stdin.read(1)
                if ch:
                    if ch == '\n' or ch == '\r':
                        line = rx_buf.strip()
                        rx_buf = ""
                        if line.startswith("SET:"):
                            try:
                                target_deg = float(line[4:])
                            except ValueError:
                                pass
                        elif line.startswith("K:"):
                            try:
                                spring_k = float(line[2:])
                            except ValueError:
                                pass
                        elif line == "ZERO":
                            initial_deg = enc.get_continuous_deg()
                            target_deg = 0.0
                            prev_actual_deg = 0.0
                    else:
                        rx_buf += ch

            # 2. Read encoder
            enc.update()
            actual_deg = (enc.get_continuous_deg() - initial_deg) * enc_dir

            # 3. Position error & Velocity
            pos_error = target_deg - actual_deg
            vel_dps = (actual_deg - prev_actual_deg) / dt
            prev_actual_deg = actual_deg

            # 4. Rubber-band virtual spring equation:
            torque_cmd = (spring_k * pos_error) - (damping_d * vel_dps)
            lead_angle_mech = max(-max_lead, min(max_lead, torque_cmd))

            # Amplitude scales with stretch distance:
            stretch_ratio = min(1.0, abs(pos_error) / 30.0)
            amplitude = min_hold_amp + (max_spring_amp - min_hold_amp) * stretch_ratio

            # 5. Commutate motor relative to current rotor angle
            elec_angle = (actual_deg + lead_angle_mech) * POLE_PAIRS * (math.pi / 180.0) * motor_dir
            drive(motor, elec_angle, amplitude)

            # 6. Filtered RPM calculation for telemetry
            if time.ticks_diff(now, last_speed_time) >= 80:
                s_dt = time.ticks_diff(now, last_speed_time) / 1000.0
                d_ang = actual_deg - last_speed_angle
                raw_rpm = (d_ang / 360.0 / s_dt) * 60.0
                filtered_rpm = filtered_rpm * 0.70 + raw_rpm * 0.30
                last_speed_angle = actual_deg
                last_speed_time = now

            # 7. Telemetry transmission at 25 Hz
            if time.ticks_diff(now, last_telemetry_time) >= 40:
                print("DATA:%.1f,%.1f,%.1f" % (target_deg, actual_deg, filtered_rpm))
                last_telemetry_time = now

            time.sleep_us(1000)

    except KeyboardInterrupt:
        pass
    finally:
        motor.off()
        print("STATUS:STOPPED")

if __name__ == "__main__":
    main()
