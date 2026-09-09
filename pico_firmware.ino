// Pico firmware: emulates a generic USB HID gamepad on the Pico's native USB
// port (plug this into the OGX-Mini's controller/host input port), while
// receiving live button/axis state over UART from a PC-connected USB-to-serial
// adapter.
//
// Board:    Raspberry Pi Pico
// Core:     arduino-pico (earlephilhower) - Tools > USB Stack > "Adafruit TinyUSB"
// Library:  Adafruit TinyUSB Library (Library Manager)
//
// Wiring:
//   Pico GPIO0 (UART0 TX) -> adapter RX
//   Pico GPIO1 (UART0 RX) -> adapter TX
//   Pico GND              -> adapter GND
//   Pico native USB        -> OGX-Mini host/input port
//   USB-serial adapter USB  -> PC
//
// IMPORTANT: this uses a CUSTOM HID report descriptor, not TinyUSB's stock
// TUD_HID_REPORT_DESC_GAMEPAD(). That's deliberate -- OGX-Mini's own generic
// HID host parser (Firmware/RP2040/src/USBHost/HostDriver/HIDGeneric in the
// OGX-Mini repo) has two quirks the stock descriptor collides with:
//   1. It bounds-checks button usage IDs against MAX_BUTTONS=32 with a ">="
//      (not ">"), so a report with the stock 32 buttons (usage IDs 1..32)
//      always fails to parse on the last button -- and a parse failure
//      discards the ENTIRE report, not just the button, so the whole
//      device silently does nothing even though it enumerates fine.
//   2. It reads the hat/D-pad value as a raw cast to its own enum
//      (UP=0, UP_RIGHT=1, RIGHT=2, DOWN_RIGHT=3, DOWN=4, DOWN_LEFT=5,
//      LEFT=6, UP_LEFT=7, NEUTRAL=8) rather than rescaling by the
//      descriptor's logical min/max, which doesn't match TinyUSB's own
//      hat convention (CENTERED=0, UP=1, ..., UP_LEFT=8).
// So below: only 14 buttons declared (well under 32), and hat values sent
// match OGX-Mini's scheme directly rather than TinyUSB's.
//
// Onboard LED status (see updateStatusLed()):
//   Slow blink (1s)    - USB not mounted: host (OGX-Mini) hasn't enumerated
//                        this as a device yet. Check which OGX-Mini port
//                        it's plugged into, and the USB cable/port itself.
//   Fast blink (200ms) - USB mounted, but no bytes at all have arrived over
//                        UART recently. Points at the link itself: TX/RX
//                        wiring, missing/loose GND, wrong COM port, or the
//                        PC app not running.
//   Stutter (blink-blink-pause) - USB mounted, bytes ARE arriving over UART,
//                        but none have passed the checksum recently. Points
//                        at signal integrity/framing: baud rate mismatch,
//                        a logic-level mismatch (some USB-UART adapters have
//                        a 3.3V/5V jumper -- the Pico's GPIOs are 3.3V only
//                        and not 5V tolerant), or TX/RX swapped instead of
//                        crossed.
//   Solid on           - Mounted and receiving valid packets. Everything's
//                        working; look elsewhere (e.g. BUTTON_MAP) if the
//                        Switch still isn't responding correctly.

#include <Adafruit_TinyUSB.h>

// Custom HID Gamepad report descriptor:
//   X, Y, Z, Rz : 8-bit signed, -127..127 (left stick X/Y, right stick X/Y)
//   Hat switch  : 8-bit, 0=up 1=up-right 2=right 3=down-right 4=down
//                 5=down-left 6=left 7=up-left 8=neutral (OGX-Mini's own
//                 raw scheme -- see note above)
//   Buttons     : 14 buttons (usage 1-14), + 2 padding bits to byte-align
static uint8_t const desc_hid_report[] = {
  HID_USAGE_PAGE ( HID_USAGE_PAGE_DESKTOP ),
  HID_USAGE      ( HID_USAGE_DESKTOP_GAMEPAD ),
  HID_COLLECTION ( HID_COLLECTION_APPLICATION ),
    HID_USAGE_PAGE  ( HID_USAGE_PAGE_DESKTOP ),
    HID_USAGE       ( HID_USAGE_DESKTOP_X ),
    HID_USAGE       ( HID_USAGE_DESKTOP_Y ),
    HID_USAGE       ( HID_USAGE_DESKTOP_Z ),
    HID_USAGE       ( HID_USAGE_DESKTOP_RZ ),
    HID_LOGICAL_MIN ( 0x81 ),
    HID_LOGICAL_MAX ( 0x7f ),
    HID_REPORT_COUNT( 4 ),
    HID_REPORT_SIZE ( 8 ),
    HID_INPUT       ( HID_DATA | HID_VARIABLE | HID_ABSOLUTE ),

    HID_USAGE_PAGE  ( HID_USAGE_PAGE_DESKTOP ),
    HID_USAGE       ( HID_USAGE_DESKTOP_HAT_SWITCH ),
    HID_LOGICAL_MIN ( 0 ),
    HID_LOGICAL_MAX ( 8 ),
    HID_REPORT_COUNT( 1 ),
    HID_REPORT_SIZE ( 8 ),
    HID_INPUT       ( HID_DATA | HID_VARIABLE | HID_ABSOLUTE ),

    HID_USAGE_PAGE  ( HID_USAGE_PAGE_BUTTON ),
    HID_USAGE_MIN   ( 1 ),
    HID_USAGE_MAX   ( 14 ),
    HID_LOGICAL_MIN ( 0 ),
    HID_LOGICAL_MAX ( 1 ),
    HID_REPORT_COUNT( 14 ),
    HID_REPORT_SIZE ( 1 ),
    HID_INPUT       ( HID_DATA | HID_VARIABLE | HID_ABSOLUTE ),

    HID_REPORT_COUNT( 1 ),
    HID_REPORT_SIZE ( 2 ),
    HID_INPUT       ( HID_CONSTANT ), // 2 padding bits to reach 16 bits
  HID_COLLECTION_END
};

Adafruit_USBD_HID usb_hid;

typedef struct __attribute__((packed)) {
  int8_t   x;        // left stick X
  int8_t   y;        // left stick Y
  int8_t   z;        // right stick X
  int8_t   rz;       // right stick Y
  uint8_t  hat;       // 0=up..7=up-left, 8=neutral (OGX-Mini raw scheme)
  uint16_t buttons;   // bit0=usage1 (X) .. bit13=usage14 (MISC), see PC app
} ogxmini_report_t;

static const uint8_t HAT_NEUTRAL = 8;

// --- Wire protocol -----------------------------------------------------
// Byte stream from PC, one frame per tick:
//   [0] sync byte 0xA5
//   [1] x   (int8)   left stick X
//   [2] y   (int8)   left stick Y
//   [3] z   (int8)   right stick X
//   [4] rz  (int8)   right stick Y
//   [5] hat (uint8)  0=up 1=up-right 2=right 3=down-right 4=down
//                    5=down-left 6=left 7=up-left 8=neutral
//   [6..7] buttons (uint16, little-endian), bit0=usage1 .. bit13=usage14
//   [8] checksum = XOR of bytes [1..7]
static const uint8_t SYNC_BYTE = 0xA5;
static const size_t PAYLOAD_LEN = 8; // everything after the sync byte

static uint8_t rxBuf[PAYLOAD_LEN];
static size_t rxIdx = 0;
static bool syncing = true;

static ogxmini_report_t report;
static uint32_t lastPacketMs = 0;   // last time a valid (checksum-passing) packet arrived
static uint32_t lastByteMs = 0;     // last time ANY byte arrived over UART, valid or not
static uint32_t lastSendMs = 0;
static const uint32_t FAILSAFE_TIMEOUT_MS = 300; // center/release if PC goes quiet
static const uint32_t LINK_STALE_MS = 1000;      // how long "recent" means for LED purposes

static uint8_t xorChecksum(const uint8_t *data, size_t len) {
  uint8_t c = 0;
  for (size_t i = 0; i < len; i++) c ^= data[i];
  return c;
}

static void resetReport() {
  memset(&report, 0, sizeof(report));
  report.hat = HAT_NEUTRAL;
}

static void applyPacket(const uint8_t *p) {
  report.x  = (int8_t)p[0];
  report.y  = (int8_t)p[1];
  report.z  = (int8_t)p[2];
  report.rz = (int8_t)p[3];
  report.hat = p[4];
  memcpy(&report.buttons, &p[5], 2);
}

// See the LED status comment near the top of this file for what each
// pattern means.
static void updateStatusLed() {
  uint32_t now = millis();
  bool mounted = TinyUSBDevice.mounted();
  bool recentPacket = (now - lastPacketMs) < FAILSAFE_TIMEOUT_MS;
  bool recentByte = (now - lastByteMs) < LINK_STALE_MS;

  if (mounted && recentPacket) {
    digitalWrite(LED_BUILTIN, HIGH); // solid: all good
    return;
  }

  if (!mounted) {
    // slow blink: not enumerated by the host
    uint32_t period = 1000;
    digitalWrite(LED_BUILTIN, (now % period) < (period / 4) ? HIGH : LOW);
    return;
  }

  if (mounted && recentByte) {
    // stutter: bytes arriving but failing checksum -- link exists, data is bad
    uint32_t phase = now % 600;
    bool on = (phase < 80) || (phase >= 160 && phase < 240);
    digitalWrite(LED_BUILTIN, on ? HIGH : LOW);
    return;
  }

  // fast blink: mounted, but nothing arriving over UART at all
  uint32_t period = 200;
  digitalWrite(LED_BUILTIN, (now % period) < (period / 4) ? HIGH : LOW);
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  Serial1.setTX(0);
  Serial1.setRX(1);
  Serial1.begin(115200);

  usb_hid.setPollInterval(2);
  usb_hid.setReportDescriptor(desc_hid_report, sizeof(desc_hid_report));
  usb_hid.begin();

  resetReport();

  // Deliberately not waiting for TinyUSBDevice.mounted() here: if the host
  // never enumerates the device, we still want to reach loop() so the
  // status LED can report that instead of hanging silently forever.
}

void loop() {
  while (Serial1.available()) {
    uint8_t b = (uint8_t)Serial1.read();
    lastByteMs = millis();

    if (syncing) {
      if (b == SYNC_BYTE) {
        syncing = false;
        rxIdx = 0;
      }
      continue;
    }

    rxBuf[rxIdx++] = b;
    if (rxIdx == PAYLOAD_LEN) {
      uint8_t calc = xorChecksum(rxBuf, PAYLOAD_LEN - 1);
      if (calc == rxBuf[PAYLOAD_LEN - 1]) {
        applyPacket(rxBuf);
        lastPacketMs = millis();
      }
      syncing = true;
      rxIdx = 0;
    }
  }

  // Failsafe: if the PC stops sending, release everything rather than
  // latching whatever the last input happened to be.
  if (millis() - lastPacketMs > FAILSAFE_TIMEOUT_MS) {
    resetReport();
  }

  // Send at a steady ~250Hz regardless of packet arrival, so held inputs
  // keep being reported.
  if (millis() - lastSendMs >= 4) {
    if (usb_hid.ready()) {
      usb_hid.sendReport(0, &report, sizeof(report));
      lastSendMs = millis();
    }
  }

  updateStatusLed();
}
