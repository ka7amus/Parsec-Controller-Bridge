"""
GUI wrapper around controller_bridge.py so day-to-day use doesn't require a
terminal: pick a COM port and controller from dropdowns, hit Start.

Run from source:
    python bridge_gui.py

Or build a standalone .exe with PyInstaller -- see the README's
"Building a standalone .exe" section.

This is a thin wrapper: all the actual protocol/HID logic (BUTTON_MAP,
build_packet, read_state, etc.) still lives in controller_bridge.py and is
imported from here. For calibration (--identity-test) or verbose per-packet
debugging (--debug), use controller_bridge.py directly from the command
line -- this GUI only logs lifecycle events (started/stopped/errors), not
every packet, to keep the log readable.
"""

import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import pygame
import serial
from serial.tools import list_ports

import controller_bridge as cb


def config_path() -> str:
    base = os.getenv("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "SwitchInputBridge")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")


def load_config() -> dict:
    try:
        with open(config_path(), "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> None:
    try:
        with open(config_path(), "w") as f:
            json.dump(data, f)
    except OSError:
        pass


class StreamWorker(threading.Thread):
    """Runs the same read-controller/send-packet loop as controller_bridge.py's
    CLI streaming mode, in a background thread so the GUI stays responsive."""

    def __init__(self, port: str, baud: int, joystick_index: int, rate_hz: float, log_queue: "queue.Queue"):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.joystick_index = joystick_index
        self.rate_hz = rate_hz
        self.log_queue = log_queue
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            joystick = pygame.joystick.Joystick(self.joystick_index)
            joystick.init()
        except Exception as e:
            self.log_queue.put(("error", f"Failed to open joystick: {e}"))
            return

        try:
            ser = serial.Serial(self.port, self.baud, timeout=0)
        except Exception as e:
            self.log_queue.put(("error", f"Failed to open {self.port}: {e}"))
            return

        self.log_queue.put(("status", f"Streaming: {joystick.get_name()} -> {self.port}"))
        period = 1.0 / self.rate_hz
        try:
            while not self.stop_event.is_set():
                start = time.time()
                buttons, hat, lx, ly, rx, ry = cb.read_state(joystick)
                packet = cb.build_packet(buttons, hat, lx, ly, rx, ry)
                ser.write(packet)
                elapsed = time.time() - start
                time.sleep(max(0.0, period - elapsed))
        except Exception as e:
            self.log_queue.put(("error", f"Streaming stopped unexpectedly: {e}"))
        finally:
            ser.close()
            self.log_queue.put(("status", "Stopped"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Switch Input Bridge")
        self.resizable(False, False)
        self.report_callback_exception = self._on_tk_exception

        pygame.init()
        pygame.joystick.init()

        self.worker: StreamWorker | None = None
        self.log_queue: "queue.Queue" = queue.Queue()
        self.config_data = load_config()

        self._build_ui()
        self._refresh_ports()
        self._refresh_joysticks()
        self._restore_selection()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_log_queue)

    # -- UI construction --------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        frame = ttk.Frame(self)
        frame.grid(row=0, column=0, sticky="nsew", **pad)

        ttk.Label(frame, text="Serial port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(frame, textvariable=self.port_var, state="readonly", width=30)
        self.port_combo.grid(row=0, column=1, **pad)
        ttk.Button(frame, text="Refresh", command=self._refresh_ports).grid(row=0, column=2, **pad)

        ttk.Label(frame, text="Controller:").grid(row=1, column=0, sticky="w")
        self.joystick_var = tk.StringVar()
        self.joystick_combo = ttk.Combobox(frame, textvariable=self.joystick_var, state="readonly", width=30)
        self.joystick_combo.grid(row=1, column=1, **pad)
        ttk.Button(frame, text="Refresh", command=self._refresh_joysticks).grid(row=1, column=2, **pad)

        ttk.Label(frame, text="Send rate (Hz):").grid(row=2, column=0, sticky="w")
        self.rate_var = tk.StringVar(value=str(self.config_data.get("rate", 125)))
        ttk.Entry(frame, textvariable=self.rate_var, width=10).grid(row=2, column=1, sticky="w", **pad)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=3, column=0, columnspan=3, pady=(8, 4))
        self.start_button = ttk.Button(button_frame, text="Start", command=self._on_start)
        self.start_button.grid(row=0, column=0, padx=4)
        self.stop_button = ttk.Button(button_frame, text="Stop", command=self._on_stop, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=4)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(frame, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).grid(
            row=4, column=0, columnspan=3, sticky="w", **pad
        )

        self.log_text = tk.Text(frame, width=56, height=10, state="disabled", wrap="word")
        self.log_text.grid(row=5, column=0, columnspan=3, **pad)

    # -- Port/joystick discovery -------------------------------------------

    def _refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def _refresh_joysticks(self):
        pygame.joystick.quit()
        pygame.joystick.init()
        names = []
        for i in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(i)
            j.init()
            names.append(f"{i}: {j.get_name()}")
        self.joystick_combo["values"] = names
        if names and self.joystick_var.get() not in names:
            self.joystick_var.set(names[0])

    def _restore_selection(self):
        last_port = self.config_data.get("port")
        if last_port and last_port in self.port_combo["values"]:
            self.port_var.set(last_port)

        last_joystick_name = self.config_data.get("joystick_name")
        if last_joystick_name:
            for value in self.joystick_combo["values"]:
                if value.endswith(last_joystick_name):
                    self.joystick_var.set(value)
                    break

    def _selected_joystick_index(self) -> int | None:
        value = self.joystick_var.get()
        if not value:
            return None
        return int(value.split(":", 1)[0])

    # -- Start/Stop ---------------------------------------------------------

    def _on_start(self):
        port = self.port_var.get()
        joystick_index = self._selected_joystick_index()
        if not port or joystick_index is None:
            messagebox.showerror("Switch Input Bridge", "Select a serial port and a controller first.")
            return
        try:
            rate = float(self.rate_var.get())
        except ValueError:
            messagebox.showerror("Switch Input Bridge", "Send rate must be a number.")
            return

        joystick_name = self.joystick_var.get().split(":", 1)[1].strip() if ":" in self.joystick_var.get() else ""
        save_config({"port": port, "joystick_name": joystick_name, "rate": rate})

        self.worker = StreamWorker(port, 115200, joystick_index, rate, self.log_queue)
        self.worker.start()

        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.port_combo.config(state="disabled")
        self.joystick_combo.config(state="disabled")
        self.status_var.set("Starting...")

    def _on_stop(self):
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=2.0)
            self.worker = None
        self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")
        self.port_combo.config(state="readonly")
        self.joystick_combo.config(state="readonly")
        self.status_var.set("Idle")

    # -- Logging --------------------------------------------------------

    def _log(self, message: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                kind, message = self.log_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(message)
                    self._log(message)
                elif kind == "error":
                    self.status_var.set("Error")
                    self._log(f"ERROR: {message}")
                    messagebox.showerror("Switch Input Bridge", message)
                    self._on_stop()
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _on_tk_exception(self, exc, val, tb):
        messagebox.showerror("Switch Input Bridge - unexpected error", str(val))

    def _on_close(self):
        self._on_stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
