# switch-input-bridge

Lets a controller plugged into your PC control a Nintendo Switch, by relaying
its input to your OGX-Mini adapter through a Raspberry Pi Pico acting as a
translator.

## Why this shape

OGX-Mini (the adapter you already have) expects a *real* controller plugged
into its own host port — it has no documented way to accept raw input over
USB/serial directly from a PC. So the Pico's job is to sit in between: it
presents itself to OGX-Mini as a generic HID gamepad (one of OGX-Mini's
supported input types) over its native USB port, while separately receiving
the actual button/stick state from your PC over a UART link.

A Pico's native USB can only do one job at a time, so it can't talk to both
the PC and OGX-Mini over the same cable — hence the extra UART hop.

```
[Controller] -> [PC: controller_bridge.py] -> [USB-UART adapter] -> [Pico UART]
                                                                        |
                                                                  (native USB,
                                                                 HID gamepad)
                                                                        v
                                                              [OGX-Mini host port]
                                                                        |
                                                                (OGX-Mini device
                                                                 port, unchanged)
                                                                        v
                                                                  [Nintendo Switch]
```

## What you need

- The Raspberry Pi Pico you already have
- A cheap USB-to-UART (TTL serial) adapter, e.g. an FTDI or CP2102 board (~$3-5)
- 3 jumper wires (TX, RX, GND)
- Your existing OGX-Mini adapter, unmodified

## Wiring

Overall chain, PC to Switch:

```
┌────────────┐  USB   ┌────────────────┐ 3 wires ┌────────────────┐  USB   ┌────────────┐  USB   ┌────────────┐
│     PC     ├───────►│  USB-to-UART   │◄───────►│  Raspberry Pi  ├───────►│  OGX-Mini  ├───────►│  Nintendo  │
│ (this app) │        │    adapter     │ TX/RX/  │      Pico      │ native │  (host     │        │   Switch   │
└────────────┘        └────────────────┘ GND     └────────────────┘  USB  │   port)    │        └────────────┘
                                                                            └────────────┘
```

Only the middle hop (adapter <-> Pico) is wires you connect yourself; the
rest are ordinary USB cables. Zoomed in on that hop:

```
 USB-to-UART adapter                Raspberry Pi Pico
 ┌───────────────────┐              ┌───────────────────┐
 │                 RX │◄────────────┤ GPIO0  (UART0 TX)  │
 │                 TX ├────────────►│ GPIO1  (UART0 RX)  │
 │                GND ├─────────────┤ GND                │
 └───────────────────┘              └───────────────────┘
         │
         │ USB
         ▼
    your PC
```

| Pico          | USB-UART adapter |
|---------------|-------------------|
| GPIO0 (UART0 TX) | RX             |
| GPIO1 (UART0 RX) | TX             |
| GND           | GND               |

Cross TX/RX (Pico TX goes to the adapter's RX, and vice versa). Then:

- Pico's native USB port -> OGX-Mini's host/controller input port
- USB-UART adapter -> your PC (regular USB)

## Flashing the Pico

1. Install [Arduino IDE](https://www.arduino.cc/en/software) and the
   [arduino-pico core](https://github.com/earlephilhower/arduino-pico)
   (Boards Manager: "Raspberry Pi Pico/RP2040").
2. Install the **Adafruit TinyUSB Library** via Library Manager.
3. Board settings: `Raspberry Pi Pico`, and under **Tools > USB Stack** choose
   **Adafruit TinyUSB**.
4. Open `pico_firmware/pico_firmware.ino`, hold BOOTSEL and plug in the Pico,
   then upload.
5. After upload, unplug from your dev PC and plug the Pico's USB port into
   OGX-Mini's host port instead.

## Reading the onboard LED

The firmware drives the Pico's onboard LED as a status indicator, since its
only USB port is busy being the gamepad (no spare serial console to print
debug output to once it's plugged into OGX-Mini):

| Pattern | Meaning |
|---|---|
| Slow blink (~1s) | USB not mounted — OGX-Mini (or whatever it's plugged into) hasn't enumerated it as a device. Check you're in the host/controller-input port, not the console-out port, and check the cable. |
| Fast blink (~200ms) | Mounted, but **no bytes at all** have arrived over UART recently. Points at the link itself: TX/RX wiring, a missing/loose GND connection, the wrong COM port, or `controller_bridge.py` simply not running. |
| Stutter (blink-blink-pause) | Mounted, and bytes **are** arriving over UART, but none have passed the checksum recently. The link exists but the data on it is bad — check for a baud rate mismatch, TX/RX swapped instead of crossed, or a logic-level mismatch (see below). |
| Solid on | Mounted and receiving valid packets — everything in the bridge is working. If the Switch still doesn't respond correctly at this point, the issue is downstream (e.g. `BUTTON_MAP` needs calibration, see below). |

A common cause of the stutter pattern: some USB-UART adapters have a 3.3V/5V
logic-level jumper or switch. The Pico's GPIOs are 3.3V only and **not** 5V
tolerant, so make sure the adapter is set to 3.3V — besides corrupting the
data, sustained 5V into a Pico GPIO risks damaging it.

## Running the PC app

Two ways to run it: a command-line script (`controller_bridge.py`, best for
calibration/debugging) or a GUI (`bridge_gui.py`, best for everyday use once
things are calibrated — no terminal needed).

```bash
cd pc_app
pip install -r requirements.txt
python controller_bridge.py --list-ports        # find your USB-UART adapter's COM port
python controller_bridge.py --list-joysticks     # find your controller's index
python controller_bridge.py --port COM5 --debug  # run it, printing live state
```

Drop `--debug` once you've confirmed it's working.

## GUI app

```bash
cd pc_app
python bridge_gui.py
```

Pick the COM port and controller from the dropdowns (Refresh re-scans both —
useful if you plug something in after opening the app) and hit Start. It
remembers your last-used port/controller/rate (stored in
`%APPDATA%\SwitchInputBridge\config.json`) and preselects them next time.

The GUI is a thin wrapper — it imports `build_packet`/`read_state`/etc.
directly from `controller_bridge.py`, so `BUTTON_MAP` and everything else
you calibrated still applies unchanged. It only logs lifecycle events
(started/stopped/errors), not every packet, to keep the log panel readable —
for per-packet detail or `--identity-test`, use `controller_bridge.py`
directly.

### Building a standalone .exe

```bash
pip install -r requirements.txt -r requirements-build.txt
pyinstaller --onefile --windowed --name SwitchInputBridge bridge_gui.py
```

The result is `dist/SwitchInputBridge.exe` — double-clickable, no console
window, no Python installation required on the machine that runs it. Re-run
the command after any code changes; `build/`, `dist/`, and the generated
`.spec` file are build output and don't need to be committed.

## Calibration — expect to do this

`BUTTON_MAP` and the axis assignments in `pc_app/controller_bridge.py` are a
best-effort default for a standard Xbox-style SDL layout. Whether OGX-Mini's
generic-HID parser assigns incoming HID buttons/axes to Switch inputs in the
same order is untested. Two ways to calibrate:

**Identity test (no controller needed):**

```bash
python controller_bridge.py --port COM5 --identity-test
```

This asserts each button, D-pad direction, and stick extreme alone in turn
(2s hold, 0.6s rest by default — tune with `--identity-hold`/`--identity-gap`),
printing which one it's currently sending. Watch the Switch and note what
reacts for each line.

**Or, with a real controller plugged in:**

1. Run with `--debug`.
2. Press one physical button/direction at a time and note what it does on
   the Switch.

Either way, once you know the real mapping, edit `BUTTON_MAP` (and
`AXIS_LX`/`AXIS_LY`/`AXIS_RX`/`AXIS_RY`, and the hat lookup table in
`read_state`, if the sticks/D-pad are swapped or inverted) in
`controller_bridge.py` to match.

## Protocol reference

9-byte frames, PC -> Pico, over UART at 115200 baud:

| Offset | Field       | Type          |
|--------|-------------|---------------|
| 0      | sync        | `0xA5`        |
| 1      | x           | int8 (left stick X) |
| 2      | y           | int8 (left stick Y) |
| 3      | z           | int8 (right stick X) |
| 4      | rz          | int8 (right stick Y) |
| 5      | hat         | uint8, **0=up, 1=up-right, 2=right, 3=down-right, 4=down, 5=down-left, 6=left, 7=up-left, 8=neutral** |
| 6-7    | buttons     | uint16 little-endian bitmask, bit0=HID button usage 1 ... bit13=usage 14 |
| 8      | checksum    | XOR of bytes 1-7 |

The Pico firmware has a 300ms failsafe: if no valid frame arrives in that
window, it centers the sticks and releases all buttons, so a crashed or
disconnected PC app doesn't leave inputs stuck held.

Analog triggers (LT/RT) are sent as digital `ZL`/`ZR` button bits (usage 7
and 8) rather than as separate analog axes, since a real Switch Pro
Controller has digital ZL/ZR rather than analog triggers.

**Why the hat values look unusual:** this isn't the standard HID hat-switch
convention (which would be 0=centered, 1=up, ...) — it's deliberately matched
to how OGX-Mini's own generic-HID parser interprets the raw byte. See the
big comment at the top of `pico_firmware.ino` for the full explanation.

## What was actually wrong (for context)

The first version of this firmware used TinyUSB's stock 32-button gamepad
HID descriptor. Reading OGX-Mini's own source
(`Firmware/RP2040/src/USBHost/HostDriver/HIDGeneric` and `HIDParser` in the
[OGX-Mini repo](https://github.com/wiredopposite/OGX-Mini)) turned up two
concrete mismatches that explained "Pico enumerates fine, PC link is solid,
but the Switch does nothing":

1. OGX-Mini's parser bounds-checks HID button usage IDs with `>= 32`
   (not `> 32`), and usage IDs are `usage_min + index`. TinyUSB's stock
   32-button descriptor declares usage IDs 1-32, so the *last* button
   always hits that bound and the parser discards the **entire** report —
   not just that button, everything, on every single packet. The fix:
   a custom descriptor declaring only 14 buttons (well under the limit),
   which is all OGX-Mini's generic driver reads anyway.
2. OGX-Mini reads the hat/D-pad byte as a raw cast to its own enum
   (`UP=0 ... UP_LEFT=7, NEUTRAL=8`), not TinyUSB's convention
   (`CENTERED=0, UP=1, ..., UP_LEFT=8`) and not rescaled by the descriptor's
   logical min/max. The fix: send hat values in OGX-Mini's own scheme
   directly (see the protocol table above).

Both are now fixed in `pico_firmware.ino` and `controller_bridge.py`. If you
already had a prior version flashed/running, re-flash the Pico and pull the
latest `controller_bridge.py` before testing further.

## Caveats

This has been written and cross-checked against OGX-Mini's own source, but
still not run end-to-end on real hardware by me — verify the wiring,
upload, and button calibration steps yourself, and expect to iterate on
`BUTTON_MAP` once you can see live behavior on console.
