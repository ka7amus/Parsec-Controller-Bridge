"""
Reads a real controller plugged into this PC and streams its state over a
serial connection to a Raspberry Pi Pico running pico_firmware.ino, which
re-emits it as a USB HID gamepad into an OGX-Mini adapter -> Nintendo Switch.

Setup:
    pip install -r requirements.txt

Usage:
    python controller_bridge.py --port COM4
    python controller_bridge.py --port COM4 --debug     # print raw axis/button values
    python controller_bridge.py --port COM4 --identity-test  # calibration mode, no joystick needed
    python controller_bridge.py --list-ports            # show available serial ports
    python controller_bridge.py --list-joysticks         # show connected controllers

IMPORTANT: the button bit positions and hat values below are NOT arbitrary --
they match the exact HID usage-ID order and raw hat encoding that OGX-Mini's
own generic-HID host parser expects (see the big comment in
pico_firmware.ino for why). Bit position = HID button usage ID, read
positionally regardless of what you'd naturally call the button, so
BUTTON_MAP is still the thing to re-check first if a specific physical
button on your controller doesn't do what you expect (SDL button index
assumptions vary by controller) -- run --identity-test to confirm.
"""

import argparse
import struct
import sys
import time

import pygame
import serial
from serial.tools import list_ports

SYNC_BYTE = 0xA5

# Bit positions correspond to HID button usage IDs 1-14, which is what
# OGX-Mini's HIDGeneric driver reads positionally and maps to these Switch
# button roles (see USBHost/HostDriver/HIDGeneric/HIDGeneric.cpp upstream).
BTN_X = 1 << 0
BTN_A = 1 << 1
BTN_B = 1 << 2
BTN_Y = 1 << 3
BTN_LB = 1 << 4
BTN_RB = 1 << 5
BTN_ZL = 1 << 6   # digital left trigger
BTN_ZR = 1 << 7   # digital right trigger
BTN_SELECT = 1 << 8   # back/minus/share
BTN_START = 1 << 9    # plus
BTN_L3 = 1 << 10  # left stick click
BTN_R3 = 1 << 11  # right stick click
BTN_HOME = 1 << 12  # guide/system
BTN_MISC = 1 << 13  # capture/share -- often has no default SDL button, unmapped below

# OGX-Mini casts the raw hat byte directly to its own enum rather than
# rescaling by descriptor logical min/max, so these values must match that
# enum exactly (UP=0 ... UP_LEFT=7, NEUTRAL=8) -- NOT TinyUSB's own
# convention (which is CENTERED=0, UP=1, ..., UP_LEFT=8).
HAT_UP = 0
HAT_UP_RIGHT = 1
HAT_RIGHT = 2
HAT_DOWN_RIGHT = 3
HAT_DOWN = 4
HAT_DOWN_LEFT = 5
HAT_LEFT = 6
HAT_UP_LEFT = 7
HAT_CENTERED = 8

# Default mapping from pygame's SDL "Xbox-style" button indices to our bits.
# Edit this once you've tested against real hardware.
BUTTON_MAP = {
    0: BTN_A,
    1: BTN_B,
    2: BTN_X,
    3: BTN_Y,
    4: BTN_LB,       # left bumper
    5: BTN_RB,       # right bumper
    6: BTN_SELECT,   # back/share
    7: BTN_START,
    8: BTN_L3,     # guide/home > changed to L3  
    9: BTN_R3,       # left stick click > changed to R3
    10: BTN_HOME,      # right stick click > changed to Home
}

# Analog trigger axes (SDL: -1.0 resting, +1.0 fully pressed) mapped to
# digital ZL/ZR buttons, since a real Switch Pro Controller has digital
# ZL/ZR rather than analog triggers.
TRIGGER_AXIS_LEFT = 4
TRIGGER_AXIS_RIGHT = 5
TRIGGER_PRESS_THRESHOLD = 0.5

AXIS_LX = 0
AXIS_LY = 1
AXIS_RX = 2
AXIS_RY = 3

DEADZONE = 0.12

# Named inputs for --identity-test, in the same bit/value space as BUTTON_MAP
# and the hat table above. Each is asserted alone so you can watch the
# console and see what reacts.
NAMED_BUTTONS = [
    ("X", BTN_X),
    ("A", BTN_A),
    ("B", BTN_B),
    ("Y", BTN_Y),
    ("LB", BTN_LB),
    ("RB", BTN_RB),
    ("ZL (digital left trigger)", BTN_ZL),
    ("ZR (digital right trigger)", BTN_ZR),
    ("SELECT (back/minus/share)", BTN_SELECT),
    ("START (plus)", BTN_START),
    ("L3 (left stick click)", BTN_L3),
    ("R3 (right stick click)", BTN_R3),
    ("HOME (guide/system)", BTN_HOME),
    ("MISC (capture/share)", BTN_MISC),
]

NAMED_HATS = [
    ("HAT up", HAT_UP),
    ("HAT up-right", HAT_UP_RIGHT),
    ("HAT right", HAT_RIGHT),
    ("HAT down-right", HAT_DOWN_RIGHT),
    ("HAT down", HAT_DOWN),
    ("HAT down-left", HAT_DOWN_LEFT),
    ("HAT left", HAT_LEFT),
    ("HAT up-left", HAT_UP_LEFT),
]

# dict kwargs match build_packet's lx/ly/rx/ry parameters (left stick, right stick)
NAMED_AXES = [
    ("left stick X -> min (-127)", dict(lx=-127)),
    ("left stick X -> max (+127)", dict(lx=127)),
    ("left stick Y -> min (-127)", dict(ly=-127)),
    ("left stick Y -> max (+127)", dict(ly=127)),
    ("right stick X -> min (-127)", dict(rx=-127)),
    ("right stick X -> max (+127)", dict(rx=127)),
    ("right stick Y -> min (-127)", dict(ry=-127)),
    ("right stick Y -> max (+127)", dict(ry=127)),
]


def clamp_axis(value: float) -> int:
    if abs(value) < DEADZONE:
        value = 0.0
    value = max(-1.0, min(1.0, value))
    return int(round(value * 127))


def build_packet(buttons: int, hat: int, lx: int, ly: int, rx: int, ry: int) -> bytes:
    payload = struct.pack(
        "<4b B H",
        lx, ly, rx, ry,  # x, y, z, rz
        hat,
        buttons,
    )
    checksum = 0
    for b in payload:
        checksum ^= b
    return bytes([SYNC_BYTE]) + payload + bytes([checksum])


def read_state(joystick: "pygame.joystick.Joystick"):
    pygame.event.pump()

    buttons = 0
    for idx, bit in BUTTON_MAP.items():
        if idx < joystick.get_numbuttons() and joystick.get_button(idx):
            buttons |= bit

    num_axes = joystick.get_numaxes()
    if TRIGGER_AXIS_LEFT < num_axes and joystick.get_axis(TRIGGER_AXIS_LEFT) > TRIGGER_PRESS_THRESHOLD:
        buttons |= BTN_ZL
    if TRIGGER_AXIS_RIGHT < num_axes and joystick.get_axis(TRIGGER_AXIS_RIGHT) > TRIGGER_PRESS_THRESHOLD:
        buttons |= BTN_ZR

    hat = HAT_CENTERED
    if joystick.get_numhats() > 0:
        hx, hy = joystick.get_hat(0)
        hat = {
            (0, 0): HAT_CENTERED,
            (0, 1): HAT_UP,
            (1, 1): HAT_UP_RIGHT,
            (1, 0): HAT_RIGHT,
            (1, -1): HAT_DOWN_RIGHT,
            (0, -1): HAT_DOWN,
            (-1, -1): HAT_DOWN_LEFT,
            (-1, 0): HAT_LEFT,
            (-1, 1): HAT_UP_LEFT,
        }.get((hx, hy), HAT_CENTERED)

    lx = clamp_axis(joystick.get_axis(AXIS_LX)) if num_axes > AXIS_LX else 0
    ly = clamp_axis(joystick.get_axis(AXIS_LY)) if num_axes > AXIS_LY else 0
    rx = clamp_axis(joystick.get_axis(AXIS_RX)) if num_axes > AXIS_RX else 0
    ry = clamp_axis(joystick.get_axis(AXIS_RY)) if num_axes > AXIS_RY else 0

    return buttons, hat, lx, ly, rx, ry


def list_ports_cmd():
    for p in list_ports.comports():
        print(f"{p.device}  {p.description}")


def list_joysticks_cmd():
    pygame.init()
    pygame.joystick.init()
    count = pygame.joystick.get_count()
    if count == 0:
        print("No joysticks detected.")
        return
    for i in range(count):
        j = pygame.joystick.Joystick(i)
        j.init()
        print(f"[{i}] {j.get_name()}  axes={j.get_numaxes()} buttons={j.get_numbuttons()} hats={j.get_numhats()}")


def send_repeated(ser, buttons=0, hat=HAT_CENTERED, lx=0, ly=0, rx=0, ry=0, duration_s=1.0, interval_s=0.05):
    """Write the same packet repeatedly for duration_s, so the Pico's
    300ms failsafe doesn't clear the state mid-hold."""
    packet = build_packet(buttons, hat, lx, ly, rx, ry)
    end = time.time() + duration_s
    while time.time() < end:
        ser.write(packet)
        time.sleep(interval_s)


def identity_test(ser, hold_s: float, gap_s: float):
    print("Identity test: each input below will be asserted alone in turn.")
    print("Watch the console and note what reacts, then update BUTTON_MAP /")
    print("AXIS_* / the hat table in this file to match.\n")

    def rest():
        send_repeated(ser, duration_s=gap_s)

    rest()
    for name, bit in NAMED_BUTTONS:
        print(f">> {name}")
        send_repeated(ser, buttons=bit, duration_s=hold_s)
        rest()

    for name, value in NAMED_HATS:
        print(f">> {name}")
        send_repeated(ser, hat=value, duration_s=hold_s)
        rest()

    for name, axes in NAMED_AXES:
        print(f">> {name}")
        send_repeated(ser, duration_s=hold_s, **axes)
        rest()

    print("\nIdentity test complete.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="Serial port the USB-UART adapter enumerates as (e.g. COM4)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--joystick", type=int, default=0, help="Index of the joystick to read (see --list-joysticks)")
    parser.add_argument("--rate", type=float, default=125.0, help="Send rate in Hz")
    parser.add_argument("--debug", action="store_true", help="Print each outgoing state instead of just running quietly")
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--list-joysticks", action="store_true")
    parser.add_argument("--identity-test", action="store_true", help="Calibration mode: assert each button/hat/axis alone in turn, no joystick required")
    parser.add_argument("--identity-hold", type=float, default=2.0, help="Seconds to hold each input during --identity-test")
    parser.add_argument("--identity-gap", type=float, default=0.6, help="Seconds to rest (all released) between inputs during --identity-test")
    args = parser.parse_args()

    if args.list_ports:
        list_ports_cmd()
        return
    if args.list_joysticks:
        list_joysticks_cmd()
        return

    if not args.port:
        print("error: --port is required (use --list-ports to see options)", file=sys.stderr)
        sys.exit(1)

    if args.identity_test:
        ser = serial.Serial(args.port, args.baud, timeout=0)
        print(f"Connected to {args.port} @ {args.baud} baud.")
        try:
            identity_test(ser, args.identity_hold, args.identity_gap)
        except KeyboardInterrupt:
            pass
        finally:
            ser.close()
        return

    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() <= args.joystick:
        print("error: no joystick at that index (use --list-joysticks)", file=sys.stderr)
        sys.exit(1)
    joystick = pygame.joystick.Joystick(args.joystick)
    joystick.init()
    print(f"Reading from: {joystick.get_name()}")

    ser = serial.Serial(args.port, args.baud, timeout=0)
    print(f"Streaming to {args.port} @ {args.baud} baud. Ctrl+C to stop.")

    period = 1.0 / args.rate
    try:
        while True:
            start = time.time()
            buttons, hat, lx, ly, rx, ry = read_state(joystick)
            packet = build_packet(buttons, hat, lx, ly, rx, ry)
            ser.write(packet)

            if args.debug:
                print(f"buttons={buttons:014b} hat={hat} lx={lx} ly={ly} rx={rx} ry={ry}")

            elapsed = time.time() - start
            time.sleep(max(0.0, period - elapsed))
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


if __name__ == "__main__":
    main()
