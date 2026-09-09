# Parsec-Controller-Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

 Allows for Parsec users or Moonlight users to control a remote console through Their own controller adapter. 


YT Vid on the Project HERE---> https://youtu.be/zYuDzN80DpQ
 
 
 Controller adapters for consoles need a controller plugged into them to work so I wanted to have a way to send Parsec inputs to a console from a PC 
 
 We can't have the PC plug straight into the console adapter since we'd end up with a USB to USB cable and that doesn't gel well with the 'host/device' setup that USB needs to work. So what is made here is essentially a 'device' that can sit between the two 'hosts' (PC and controller adapter) 
 
 That is what the Pico and USB UART adapter are for, essentially just to be a man in the middle. 
 
 The python exe that is included just takes inputs from a controller that is detected on your computer (like the Xbox 360 pad that parsec players show up as) and sends them out to the COM port on your computer which Windows will then route to the UART adapter. 

The whole chain using a Switch and OGX adapter as an example (what you see in the vid):

```
┌────────────┐  USB   ┌────────────────┐ 3 wires ┌────────────────┐  USB   ┌────────────┐  USB   ┌────────────┐
│     PC     ├───────►│  USB-to-UART   │◄───────►│  Raspberry Pi  ├───────►│  OGX-Mini  ├───────►│  Nintendo  │
│ (this app) │        │    adapter     │ TX/RX/  │      Pico      │ native │  (host     │        │   Switch   │
└────────────┘        └────────────────┘ GND     └────────────────┘  USB   │   port)    │        └────────────┘
                                                                           └────────────┘
```

Wiring that you need to do below:

```
 USB-to-UART adapter                Raspberry Pi Pico
 ┌───────────────────┐              ┌───────────────────┐
 │                 RX │◄────────────┤ GPIO0  (UART0 TX) │
 │                 TX ├────────────►│ GPIO1  (UART0 RX) │
 │                GND ├─────────────┤ GND               │
 └───────────────────┘              └───────────────────┘
         │
         │ USB
         ▼
    your PC
```

In order to Flash the PICO: 

1. Install [Arduino IDE](https://www.arduino.cc/en/software) and the
   [arduino-pico core](https://github.com/earlephilhower/arduino-pico)
   (Boards Manager: "Raspberry Pi Pico/RP2040").
2. Install the **Adafruit TinyUSB Library** via Library Manager.
3. Board settings: `Raspberry Pi Pico`, and under **Tools > USB Stack** choose
   **Adafruit TinyUSB**.
4. Open `pico_firmware/pico_firmware.ino`, hold BOOTSEL and plug in the Pico,
   then upload.
5. After upload, unplug from your dev PC and plug the Pico's USB port into
   your controller adapter's port instead.
   
   
The LED:

The firmware has the LED setup to let you know what is going on when it has power. It will be blinking until you have the app open and running.

| Pattern | Meaning |
|---|---|
| Slow blink (~1s) | USB not mounted - the controller adapter has not seen the pico yet. Check you're in the host/controller-in port of your controller adapter and check the cable. |
| Fast blink (~200ms) | Mounted, but **no bytes at all** have arrived over UART recently. Points at the link itself: TX/RX wiring, a missing/loose GND connection, the wrong COM port, or `controller_bridge.py` simply not running. |
| Stutter (blink-blink-pause) | Mounted, and bytes **are** arriving over UART, but none have passed the checksum recently. The link exists but the data on it is bad — check for a baud rate mismatch, TX/RX swapped instead of crossed, or a logic-level mismatch (see below). |
| Solid on | Mounted and receiving valid packets — everything is working, if nothing is happening on the console you probably need to check the button mapping in the pico firmware |

A common cause of the stutter pattern: some USB-UART adapters have a 3.3V/5V
logic-level jumper or switch. The Pico's GPIOs are 3.3V only and **not** 5V
tolerant, so make sure the adapter is set to 3.3V — besides corrupting the
data, sustained 5V into a Pico GPIO risks damaging it.

Running the Python app:

There is an exe that opens a small GUI, you should have a COM# selected or just hit refresh once you have the adapter plugged in.
Then you can choose the controller you want to use which should be a 360 pad (from parsec and moonlight)
You can leave the send rate at 125 since thats the rate that parsec and moonlight use for their controller
Press 'Start' and it should now be controlling your console you have it plugged into


Im not going to rewrite the whole remapping process, if you need to change anything in the pico firmware for mapping use 'rawbuttons.py' to get the numbers to remap but other than that these are the steps from the C Machine itself 

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
