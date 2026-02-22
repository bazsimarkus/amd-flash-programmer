#!/usr/bin/env python3
"""
AM29F010 / AM29F040B Flash Programmer GUI
Compatible with FLASH_PROGRAMMER.ino (Arduino Nano)
Requires: pip install pyserial
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import serial
import serial.tools.list_ports
import threading
import time
import os
import re

# ─────────────────────────────────────────────────────────────────────────────
# CHIP DATABASE  (indices must match Arduino sketch)
# ─────────────────────────────────────────────────────────────────────────────
CHIP_DB = {
    "AM29F040B (512 KB)": {"index": 1, "size": 524_288,  "mfr_id": "01", "dev_id": "A4"},
    "AM29F010  (128 KB)": {"index": 0, "size": 131_072,  "mfr_id": "01", "dev_id": "20"},
}
DEFAULT_CHIP = "AM29F040B (512 KB)"

MFR_NAMES = {
    "01": "AMD / Spansion", "04": "Fujitsu",    "1F": "Atmel",
    "20": "ST Micro",       "37": "AMIC",        "89": "Intel",
    "AD": "Hynix",          "BF": "SST",         "C2": "Macronix",
    "DA": "Winbond",
}
DEV_NAMES = {
    "20": "AM29F010 (128 KB)",
    "A4": "AM29F040B (512 KB)",
    "5B": "AM29F040  (512 KB)",
}

BAUD_RATES = ["115200", "57600", "38400", "19200", "9600"]

# ─────────────────────────────────────────────────────────────────────────────
# SERIAL WORKER
# ─────────────────────────────────────────────────────────────────────────────
class SerialWorker:
    def __init__(self, port: serial.Serial):
        self.port = port

    def flush(self):
        self.port.reset_input_buffer()
        self.port.reset_output_buffer()

    def send_cmd(self, cmd: str):
        self.port.write((cmd + "\n").encode())

    def read_line(self, timeout=5.0) -> str:
        self.port.timeout = timeout
        return self.port.readline().decode(errors="replace").strip()

    def read_exact(self, n: int, progress_cb=None, timeout=120.0) -> bytes:
        data = bytearray()
        deadline = time.time() + timeout
        while len(data) < n:
            if time.time() > deadline:
                raise TimeoutError(f"Timed out — got {len(data)}/{n} bytes")
            avail = self.port.in_waiting
            if avail:
                chunk = self.port.read(min(avail, n - len(data)))
                data.extend(chunk)
                if progress_cb:
                    progress_cb(len(data), n)
            else:
                time.sleep(0.005)
        return bytes(data)


# ─────────────────────────────────────────────────────────────────────────────
# LIGHT PALETTE
# ─────────────────────────────────────────────────────────────────────────────
C = {
    "bg":        "#F0F0F0",
    "panel":     "#FFFFFF",
    "border":    "#C8C8C8",
    "hdr_bg":    "#2C3E50",
    "hdr_fg":    "#FFFFFF",
    "accent":    "#1A6DB5",
    "ok":        "#1B6E2E",
    "warn":      "#7A5C00",
    "err":       "#A01818",
    "text":      "#1A1A1A",
    "text_dim":  "#606060",
    "hex_bg":    "#FAFAFA",
    "hex_addr":  "#1A6DB5",
    "hex_data":  "#1A1A1A",
    "hex_ff":    "#C0C0C0",
    "hex_ascii": "#1B6E2E",
    "log_ts":    "#909090",
}

FU  = ("Segoe UI", 9)
FUB = ("Segoe UI", 9,  "bold")
FH  = ("Segoe UI", 10, "bold")
FM  = ("Consolas", 9)
FMS = ("Consolas", 8)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────────────────────────────────────
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AM29F010 / AM29F040B Flash Programmer")
        self.root.geometry("1400x800")
        self.root.minsize(1400, 800)
        self.root.configure(bg=C["bg"])

        self.serial_port: serial.Serial | None = None
        self.worker:      SerialWorker  | None = None
        self.is_connected = False
        self.is_busy      = False
        self.current_data: bytes | None = None

        self.var_port     = tk.StringVar()
        self.var_baud     = tk.StringVar(value="115200")
        self.var_chip     = tk.StringVar(value=DEFAULT_CHIP)
        self.var_mfr_id   = tk.StringVar(value="—")
        self.var_mfr_name = tk.StringVar(value="—")
        self.var_dev_id   = tk.StringVar(value="—")
        self.var_dev_name = tk.StringVar(value="—")
        self.var_start    = tk.StringVar(value="000000")
        self.var_end      = tk.StringVar(value="07FFFF")
        self.var_verify   = tk.BooleanVar(value=True)

        self._setup_ttk()
        self._build_ui()
        self._refresh_ports()

    # ── STYLES ──────────────────────────────────────────────────────────────
    def _setup_ttk(self):
        s = ttk.Style()
        s.theme_use("clam")

        s.configure(".",              background=C["bg"],    foreground=C["text"],  font=FU)
        s.configure("TFrame",         background=C["bg"])
        s.configure("White.TFrame",   background=C["panel"])
        s.configure("TLabel",         background=C["bg"],    foreground=C["text"])
        s.configure("W.TLabel",       background=C["panel"], foreground=C["text"])
        s.configure("Dim.TLabel",     background=C["panel"], foreground=C["text_dim"])

        s.configure("TLabelframe",        background=C["panel"], bordercolor=C["border"])
        s.configure("TLabelframe.Label",  background=C["panel"], foreground=C["accent"], font=FUB)

        s.configure("TButton",        background=C["panel"], foreground=C["text"],
                    bordercolor=C["border"], font=FU, padding=(6,3), relief="flat")
        s.map("TButton",
              background=[("active", C["border"]), ("disabled", C["bg"])],
              foreground=[("disabled", C["text_dim"])])

        s.configure("Accent.TButton", background=C["accent"], foreground="white",
                    font=FUB, padding=(8,4))
        s.map("Accent.TButton",
              background=[("active", "#145591"), ("disabled", C["border"])])

        s.configure("Danger.TButton", background=C["err"], foreground="white",
                    font=FUB, padding=(8,4))
        s.map("Danger.TButton",
              background=[("active", "#7a1212"), ("disabled", C["border"])])

        s.configure("OK.TButton",     background=C["ok"], foreground="white",
                    font=FUB, padding=(8,4))
        s.map("OK.TButton",
              background=[("active", "#145522"), ("disabled", C["border"])])

        s.configure("TCombobox", fieldbackground=C["panel"], background=C["panel"],
                    foreground=C["text"])
        s.map("TCombobox", fieldbackground=[("readonly", C["panel"])])

        s.configure("TEntry",    fieldbackground=C["panel"], foreground=C["text"])

        s.configure("TProgressbar", background=C["accent"], troughcolor=C["border"],
                    bordercolor=C["border"])

        s.configure("TCheckbutton", background=C["panel"], foreground=C["text"])
        s.map("TCheckbutton",       background=[("active", C["panel"])])

        s.configure("TNotebook",       background=C["bg"], bordercolor=C["border"])
        s.configure("TNotebook.Tab",   background=C["border"], foreground=C["text"],
                    padding=[10,4], font=FU)
        s.map("TNotebook.Tab",
              background=[("selected", C["panel"])],
              foreground=[("selected", C["accent"])])

        s.configure("TSeparator", background=C["border"])

    # ── UI ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Header
        hdr = tk.Frame(self.root, bg=C["hdr_bg"], pady=7)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Flash Programmer  —  AM29F010 / AM29F040B",
                 font=FH, bg=C["hdr_bg"], fg=C["hdr_fg"]).pack(side="left", padx=12)
        self.lbl_conn_hdr = tk.Label(hdr, text="● Not connected",
                                      font=FU, bg=C["hdr_bg"], fg="#FF9999")
        self.lbl_conn_hdr.pack(side="right", padx=12)

        # ── Toolbar panel (white background, contains all control rows) ──────
        toolbar = tk.Frame(self.root, bg=C["panel"],
                           relief="flat", bd=0)
        toolbar.pack(fill="x")

        # Separator below header
        tk.Frame(toolbar, bg=C["border"], height=1).pack(fill="x")

        # Row 1: Connection + Chip + Operations in a single horizontal band
        row1 = tk.Frame(toolbar, bg=C["panel"], pady=6)
        row1.pack(fill="x", padx=8)

        # -- Connection group
        cg = ttk.LabelFrame(row1, text="Connection", padding=(6,4))
        cg.pack(side="left", padx=(0,8))

        ttk.Label(cg, text="Port:", style="W.TLabel").grid(row=0, column=0, sticky="w", padx=(0,3))
        self.cb_port = ttk.Combobox(cg, textvariable=self.var_port,
                                     width=9, font=FU,
                                     validate="key",
                                     validatecommand=(self.root.register(lambda: False), '%d'))
        self.cb_port.grid(row=0, column=1, padx=(0,2))
        ttk.Button(cg, text="↺", command=self._refresh_ports, width=2).grid(row=0, column=2, padx=(0,6))

        ttk.Label(cg, text="Baud:", style="W.TLabel").grid(row=0, column=3, sticky="w", padx=(0,3))
        self.cb_baud = ttk.Combobox(cg, textvariable=self.var_baud,
                                     values=BAUD_RATES, width=8, font=FU,
                                     validate="key",
                                     validatecommand=(self.root.register(lambda: False), '%d'))
        self.cb_baud.grid(row=0, column=4, padx=(0,8))

        self.btn_connect = ttk.Button(cg, text="Connect",
                                       command=self._toggle_connection,
                                       style="Accent.TButton")
        self.btn_connect.grid(row=0, column=5)

        # -- Help button (right side of header row)
        ttk.Button(row1, text="❓ Help", command=self._show_help).pack(side="right", padx=(0,4))

        # -- Chip group
        chg = ttk.LabelFrame(row1, text="Chip", padding=(6,4))
        chg.pack(side="left", padx=(0,8))

        self.cb_chip = ttk.Combobox(chg, textvariable=self.var_chip,
                                     values=list(CHIP_DB.keys()),
                                     width=19, font=FU,
                                     validate="key",
                                     validatecommand=(self.root.register(lambda: False), '%d'))
        self.cb_chip.grid(row=0, column=0, padx=(0,4))
        self.cb_chip.bind("<<ComboboxSelected>>", self._on_chip_change)

        self.btn_apply = ttk.Button(chg, text="Apply", command=self._apply_chip)
        self.btn_apply.grid(row=0, column=1, padx=(0,4))
        self.btn_apply.state(["disabled"])

        self.btn_read_id = ttk.Button(chg, text="Read Chip ID",
                                       command=lambda: self._start_op(self._do_read_id))
        self.btn_read_id.grid(row=0, column=2)
        self.btn_read_id.state(["disabled"])

        # -- Operations group
        opg = ttk.LabelFrame(row1, text="Operations", padding=(6,4))
        opg.pack(side="left", padx=(0,8))

        self.btn_read_mem = ttk.Button(opg, text="Read Memory",
                                        command=self._ask_read, style="Accent.TButton")
        self.btn_erase    = ttk.Button(opg, text="Erase Chip",
                                        command=self._ask_erase, style="Danger.TButton")
        self.btn_write    = ttk.Button(opg, text="Write File…",
                                        command=self._ask_write, style="OK.TButton")
        self.btn_verify   = ttk.Button(opg, text="Verify vs File…",
                                        command=self._ask_verify)
        self.btn_save_bin = ttk.Button(opg, text="💾 Save Binary",
                                        command=self._save_binary)

        for i, b in enumerate([self.btn_read_mem, self.btn_erase,
                                self.btn_write, self.btn_verify, self.btn_save_bin]):
            b.grid(row=0, column=i, padx=3)

        self.op_buttons = [self.btn_read_mem, self.btn_erase,
                           self.btn_write, self.btn_verify]
        for b in self.op_buttons:
            b.state(["disabled"])
        self.btn_save_bin.state(["disabled"])

        # Row 2: Chip ID info bar
        tk.Frame(toolbar, bg=C["border"], height=1).pack(fill="x")
        id_row = tk.Frame(toolbar, bg=C["panel"], pady=4)
        id_row.pack(fill="x", padx=8)

        def id_field(parent, lbl_txt, var, col, bold=False):
            tk.Label(parent, text=lbl_txt, font=FU,
                     bg=C["panel"], fg=C["text_dim"]).grid(
                row=0, column=col*2, sticky="w", padx=(16 if col else 4, 2))
            tk.Label(parent, textvariable=var,
                     font=FUB if bold else FU,
                     bg=C["panel"], fg=C["text"]).grid(
                row=0, column=col*2+1, sticky="w", padx=(0,4))

        id_field(id_row, "Mfr ID:",    self.var_mfr_id,   0, bold=True)
        id_field(id_row, "Manufacturer:", self.var_mfr_name, 1)
        id_field(id_row, "Device ID:", self.var_dev_id,   2, bold=True)
        id_field(id_row, "Device:",    self.var_dev_name, 3)

        # Row 3: Progress bar
        tk.Frame(toolbar, bg=C["border"], height=1).pack(fill="x")
        prog_row = tk.Frame(toolbar, bg=C["panel"], pady=5)
        prog_row.pack(fill="x", padx=8)

        self.lbl_prog = tk.Label(prog_row, text="Idle", font=FUB,
                                  bg=C["panel"], fg=C["text_dim"], width=14, anchor="w")
        self.lbl_prog.pack(side="left", padx=4)

        self.progress = ttk.Progressbar(prog_row, orient="horizontal",
                                         mode="determinate", length=340)
        self.progress.pack(side="left", padx=4)

        self.lbl_pct = tk.Label(prog_row, text="", font=FU,
                                 bg=C["panel"], fg=C["text"], width=5)
        self.lbl_pct.pack(side="left")

        self.lbl_prog_detail = tk.Label(prog_row, text="", font=FU,
                                         bg=C["panel"], fg=C["text_dim"])
        self.lbl_prog_detail.pack(side="left", padx=6)

        # Row 4: Read range + options
        tk.Frame(toolbar, bg=C["border"], height=1).pack(fill="x")
        opt_row = tk.Frame(toolbar, bg=C["panel"], pady=5)
        opt_row.pack(fill="x", padx=8)

        tk.Label(opt_row, text="Read range — Start (hex):", font=FU,
                 bg=C["panel"], fg=C["text"]).pack(side="left", padx=(4,2))
        self.ent_start = ttk.Entry(opt_row, textvariable=self.var_start,
                                    width=8, font=FM)
        self.ent_start.pack(side="left", padx=(0,8))

        tk.Label(opt_row, text="End (hex):", font=FU,
                 bg=C["panel"], fg=C["text"]).pack(side="left", padx=(0,2))
        self.ent_end = ttk.Entry(opt_row, textvariable=self.var_end,
                                  width=8, font=FM)
        self.ent_end.pack(side="left", padx=(0,8))

        ttk.Button(opt_row, text="Full Chip",
                   command=self._set_full_range).pack(side="left", padx=(0,16))

        ttk.Separator(opt_row, orient="vertical").pack(side="left", fill="y", padx=6)

        ttk.Checkbutton(opt_row, text="Verify after write",
                        variable=self.var_verify).pack(side="left", padx=8)

        # Bottom separator
        tk.Frame(toolbar, bg=C["border"], height=1).pack(fill="x")

        # ── Split pane: Event Log (left) | Hex View (right) ──────────────────
        paned = tk.PanedWindow(self.root, orient="horizontal",
                               bg=C["border"], sashwidth=5, sashpad=0,
                               bd=0, relief="flat")
        paned.pack(fill="both", expand=True)

        log_f = ttk.Frame(paned)
        paned.add(log_f, minsize=200)
        self._build_log_view(log_f)

        hex_f = ttk.Frame(paned)
        paned.add(hex_f, minsize=300)
        self._build_hex_view(hex_f)

        # Set initial sash position after window is rendered
        self.root.after(100, lambda: paned.sash_place(0, 420, 0))

        # ── Status bar ────────────────────────────────────────────────────────
        sbar = tk.Frame(self.root, bg=C["border"], pady=3)
        sbar.pack(fill="x", side="bottom")
        self.lbl_status = tk.Label(sbar, text="Ready", font=FU,
                                    bg=C["border"], fg=C["text_dim"],
                                    anchor="w", padx=6)
        self.lbl_status.pack(side="left")

    def _build_hex_view(self, parent):
        tb = tk.Frame(parent, bg=C["panel"], pady=3)
        tb.pack(fill="x")
        tk.Label(tb, text="Hex View", font=FUB,
                 bg=C["panel"], fg=C["accent"]).pack(side="left", padx=6)
        self.lbl_hex_info = tk.Label(tb, text="No data loaded", font=FU,
                                      bg=C["panel"], fg=C["text_dim"])
        self.lbl_hex_info.pack(side="left", padx=6)
        ttk.Button(tb, text="Clear", command=self._clear_hex).pack(side="right", padx=6)
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x")

        self.hex_text = tk.Text(parent, font=FMS,
                                 bg=C["hex_bg"], fg=C["hex_data"],
                                 relief="flat", padx=8, pady=6,
                                 selectbackground=C["accent"],
                                 selectforeground="white",
                                 state="disabled")
        sb = ttk.Scrollbar(parent, command=self.hex_text.yview)
        self.hex_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.hex_text.pack(fill="both", expand=True)

        self.hex_text.tag_configure("addr",  foreground=C["hex_addr"], font=(*FMS[:1], FMS[1], "bold"))
        self.hex_text.tag_configure("sep",   foreground=C["border"])
        self.hex_text.tag_configure("ff",    foreground=C["hex_ff"])
        self.hex_text.tag_configure("data",  foreground=C["hex_data"])
        self.hex_text.tag_configure("ascii", foreground=C["hex_ascii"])

    def _build_log_view(self, parent):
        tb = tk.Frame(parent, bg=C["panel"], pady=3)
        tb.pack(fill="x")
        tk.Label(tb, text="Event Log", font=FUB,
                 bg=C["panel"], fg=C["accent"]).pack(side="left", padx=6)
        ttk.Button(tb, text="Save Log", command=self._save_log).pack(side="right", padx=(0,4))
        ttk.Button(tb, text="Clear", command=self._clear_log).pack(side="right", padx=(0,2))
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x")

        self.log_text = tk.Text(parent, font=FMS,
                                 bg=C["hex_bg"], fg=C["text"],
                                 relief="flat", padx=8, pady=6,
                                 state="disabled")
        sb = ttk.Scrollbar(parent, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

        self.log_text.tag_configure("ts",   foreground=C["log_ts"])
        self.log_text.tag_configure("info", foreground=C["text"])
        self.log_text.tag_configure("ok",   foreground=C["ok"])
        self.log_text.tag_configure("warn", foreground=C["warn"])
        self.log_text.tag_configure("err",  foreground=C["err"])
        self.log_text.tag_configure("data", foreground=C["accent"])

    # ── LOGGING ─────────────────────────────────────────────────────────────
    def _log(self, msg: str, level="info"):
        def _do():
            ts = time.strftime("[%H:%M:%S] ")
            self.log_text.config(state="normal")
            self.log_text.insert("end", ts, "ts")
            self.log_text.insert("end", msg + "\n", level)
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.root.after(0, _do)

    def _status(self, msg):
        self.root.after(0, lambda: self.lbl_status.config(text=msg))

    def _set_progress(self, val, mx, label="", detail=""):
        def _do():
            self.progress.config(mode="determinate", maximum=max(mx, 1), value=val)
            pct = int(val / mx * 100) if mx else 0
            self.lbl_pct.config(text=f"{pct}%")
            if label:  self.lbl_prog.config(text=label)
            if detail: self.lbl_prog_detail.config(text=detail)
        self.root.after(0, _do)

    def _set_progress_busy(self, label=""):
        def _do():
            self.progress.config(mode="indeterminate")
            self.progress.start(12)
            self.lbl_pct.config(text="")
            if label: self.lbl_prog.config(text=label)
            self.lbl_prog_detail.config(text="")
        self.root.after(0, _do)

    def _stop_progress(self):
        def _do():
            self.progress.stop()
            self.progress.config(mode="determinate", value=0)
            self.lbl_prog.config(text="Idle")
            self.lbl_pct.config(text="")
            self.lbl_prog_detail.config(text="")
        self.root.after(0, _do)

    # ── PORTS ────────────────────────────────────────────────────────────────
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.cb_port["values"] = ports
        if ports and self.var_port.get() not in ports:
            self.cb_port.current(0)
        self._log(f"Found {len(ports)} port(s): {', '.join(ports) or 'none'}")

    # ── CONNECTION ───────────────────────────────────────────────────────────
    def _toggle_connection(self):
        if self.is_connected:
            self._disconnect()
        else:
            threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self):
        port = self.var_port.get()
        baud = int(self.var_baud.get())
        if not port:
            self.root.after(0, lambda: messagebox.showerror("Error", "Select a serial port first."))
            return

        self.root.after(0, lambda: self.btn_connect.config(state="disabled"))
        self._log(f"Connecting to {port} @ {baud}…")

        try:
            sp = serial.Serial()
            sp.port     = port
            sp.baudrate = baud
            sp.timeout  = 1.0
            sp.dtr      = False   # Don't auto-reset on open
            sp.open()

            # Pulse DTR to trigger Arduino reset (same effect as opening normally)
            sp.dtr = True
            time.sleep(0.1)
            sp.dtr = False

            self._log("Waiting for Arduino to boot (3 s)…")
            time.sleep(3.0)   # Generous wait — covers slow bootloaders
            sp.reset_input_buffer()

            # Drain any boot messages (READY, version lines, etc.)
            sp.timeout = 0.5
            for _ in range(8):
                line = sp.readline().decode(errors="replace").strip()
                if line:
                    self._log(f"  Boot: {line}", "data")
                    if "READY" in line:
                        break

            # Send a status ping to confirm the sketch is alive
            sp.reset_input_buffer()
            sp.write(b"?\n")
            sp.timeout = 3.0
            ping = sp.readline().decode(errors="replace").strip()
            self._log(f"  Ping: '{ping}'", "data")

            if not ping:
                # One more try after extra wait
                time.sleep(1.5)
                sp.reset_input_buffer()
                sp.write(b"?\n")
                sp.timeout = 4.0
                ping = sp.readline().decode(errors="replace").strip()
                self._log(f"  Ping (retry): '{ping}'", "data")

            if not ping:
                sp.close()
                raise RuntimeError(
                    "Arduino not responding.\n"
                    "• Check COM port and baud rate (should be 115200)\n"
                    "• Make sure FLASH_PROGRAMMER.ino is uploaded\n"
                    "• Try pressing the Arduino Reset button then Connect")

            self.serial_port = sp
            self.worker = SerialWorker(sp)
            self.is_connected = True

            def _ui_connected():
                self.btn_connect.config(text="Disconnect", state="normal")
                self.lbl_conn_hdr.config(text=f"● {port}", fg="#99FF99")
                self._set_op_state("normal")
                self.btn_apply.state(["!disabled"])
                self.btn_read_id.state(["!disabled"])

            self.root.after(0, _ui_connected)
            self._status(f"Connected: {port} @ {baud}")
            self._log(f"Connected to {port}.", "ok")

            # Auto-apply default chip (no busy lock — happens transparently)
            self._do_apply_chip()

        except Exception as e:
            err_msg = str(e)
            self._log(f"Connection failed: {err_msg}", "err")
            self.root.after(0, lambda: self.btn_connect.config(state="normal"))
            self.root.after(0, lambda _m=err_msg: messagebox.showerror("Connection Error", _m))

    def _disconnect(self):
        try:
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.close()
        except Exception:
            pass
        self.serial_port = None
        self.worker = None
        self.is_connected = False
        self.btn_connect.config(text="Connect")
        self.lbl_conn_hdr.config(text="● Not connected", fg="#FF9999")
        self._set_op_state("disabled")
        self.btn_apply.state(["disabled"])
        self.btn_read_id.state(["disabled"])
        self._status("Disconnected")
        self._log("Disconnected.", "warn")

    def _set_op_state(self, state):
        flag = "!disabled" if state == "normal" else "disabled"
        for b in self.op_buttons:
            b.state([flag])

    # ── CHIP ─────────────────────────────────────────────────────────────────
    def _on_chip_change(self, *_):
        chip = CHIP_DB[self.var_chip.get()]
        self.var_start.set("000000")
        self.var_end.set(f"{chip['size']-1:06X}")

    def _set_full_range(self):
        self._on_chip_change()

    def _apply_chip(self):
        self._start_op(self._do_apply_chip)

    def _do_apply_chip(self):
        chip_name = self.var_chip.get()
        chip = CHIP_DB[chip_name]
        try:
            self.worker.flush()
            self.worker.send_cmd(f"C{chip['index']}")
            resp = self.worker.read_line(4)
            if "CHIP_OK" in resp:
                self._log(f"Chip set: {chip_name}", "ok")
                self._status(f"Chip: {chip_name}")
                self.root.after(0, self._on_chip_change)
            else:
                self._log(f"Chip select response: '{resp}'", "warn")
        except Exception as e:
            self._log(f"Chip select error: {e}", "err")
        finally:
            self._end_op()

    # ── THREADING ────────────────────────────────────────────────────────────
    def _start_op(self, fn, *args):
        if not self.is_connected:
            messagebox.showerror("Error", "Not connected.")
            return
        if self.is_busy:
            messagebox.showwarning("Busy", "Another operation is running.")
            return
        self.is_busy = True
        self._set_op_state("disabled")
        self.root.after(0, lambda: self.btn_connect.config(state="disabled"))
        self.btn_apply.state(["disabled"])
        self.btn_read_id.state(["disabled"])
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _end_op(self):
        self.is_busy = False
        def _do():
            self._set_op_state("normal")
            self.btn_connect.config(state="normal")
            self.btn_apply.state(["!disabled"])
            self.btn_read_id.state(["!disabled"])
            self._stop_progress()
        self.root.after(0, _do)

    # ── READ CHIP ID ─────────────────────────────────────────────────────────
    def _do_read_id(self):
        try:
            self._set_progress_busy("Reading ID…")
            self.worker.flush()
            self.worker.send_cmd("I")
            resp = self.worker.read_line(8)
            self._log(f"Raw ID: {resp}", "data")
            m = re.search(r"MFR:([0-9A-Fa-f]+)\s+DEV:([0-9A-Fa-f]+)", resp)
            if m:
                mfr = m.group(1).upper()
                dev = m.group(2).upper()
                def _upd():
                    self.var_mfr_id.set(f"0x{mfr}")
                    self.var_mfr_name.set(MFR_NAMES.get(mfr, "Unknown"))
                    self.var_dev_id.set(f"0x{dev}")
                    self.var_dev_name.set(DEV_NAMES.get(dev, "Unknown"))
                self.root.after(0, _upd)
                self._log(f"Manufacturer: {MFR_NAMES.get(mfr,'?')} (0x{mfr})", "ok")
                self._log(f"Device:       {DEV_NAMES.get(dev,'?')} (0x{dev})", "ok")
                exp = CHIP_DB[self.var_chip.get()]
                if mfr != exp["mfr_id"].upper() or dev != exp["dev_id"].upper():
                    self._log("⚠ IDs don't match selected chip!", "warn")
            else:
                self._log(f"Could not parse ID response: '{resp}'", "err")
        except Exception as e:
            self._log(f"Read ID error: {e}", "err")
        finally:
            self._end_op()

    # ── READ MEMORY ──────────────────────────────────────────────────────────
    def _ask_read(self):
        self._start_op(self._do_read)

    def _do_read(self):
        try:
            start_addr = int(self.var_start.get().strip(), 16)
            end_addr   = int(self.var_end.get().strip(),   16)
            if start_addr > end_addr:
                self.root.after(0, lambda: messagebox.showerror(
                    "Error", "Start address must be ≤ end address."))
                return
            size = end_addr - start_addr + 1
            self._log(f"Reading 0x{start_addr:06X}–0x{end_addr:06X} ({size:,} bytes)…")
            self._set_progress(0, size, "Reading…")

            self.worker.flush()
            self.worker.send_cmd(f"R {start_addr:X} {end_addr:X}")

            header = self.worker.read_line(10)
            self._log(f"Header: {header}", "data")
            if "READ_ERR" in header:
                raise RuntimeError(f"Arduino error: {header}")
            if not header.startswith("RSTART"):
                raise RuntimeError(f"Unexpected header: '{header}'")

            parts = header.split()
            byte_count = int(parts[1]) if len(parts) > 1 else size

            t0 = time.time()
            def pcb(got, total):
                el  = time.time() - t0
                spd = got / el if el > 0.1 else 0
                eta = (total - got) / spd if spd > 10 else 0
                self._set_progress(got, total, "Reading…",
                    f"{got:,}/{total:,} bytes  {spd/1024:.1f} KB/s  ETA {eta:.0f}s")

            data = self.worker.read_exact(byte_count, pcb, timeout=180)
            footer = self.worker.read_line(5)
            if "REND" not in footer:
                self._log(f"Warning: expected REND, got '{footer}'", "warn")

            elapsed = time.time() - t0
            self._log(f"Read complete: {len(data):,} bytes in {elapsed:.1f}s "
                      f"({len(data)/elapsed/1024:.1f} KB/s)", "ok")
            self.current_data = data
            self.root.after(0, lambda: self._display_hex(data, start_addr))
            self.root.after(0, lambda: self.btn_save_bin.state(["!disabled"]))
            self._status(f"Read: {len(data):,} bytes from 0x{start_addr:06X}")

            # Ask to save on main thread
            save_event = threading.Event()
            do_save = [False]
            def ask():
                do_save[0] = messagebox.askyesno(
                    "Save", f"Read {len(data):,} bytes. Save to file?")
                save_event.set()
            self.root.after(0, ask)
            save_event.wait()
            if do_save[0]:
                self.root.after(0, self._save_binary)

        except Exception as e:
            err_msg = str(e)
            self._log(f"Read error: {err_msg}", "err")
            self.root.after(0, lambda _m=err_msg: messagebox.showerror("Read Error", _m))
        finally:
            self._end_op()

    # ── ERASE ────────────────────────────────────────────────────────────────
    def _ask_erase(self):
        if not messagebox.askyesno("Confirm Erase",
                f"Erase entire {self.var_chip.get()}?\n\nAll data will be permanently lost.",
                icon="warning"):
            return
        self._start_op(self._do_erase)

    def _do_erase(self):
        try:
            self._log("Starting chip erase…", "warn")
            self._set_progress_busy("Erasing…")
            self._status("Erasing — may take up to 70 seconds…")
            self.worker.flush()
            self.worker.send_cmd("E")

            resp = self.worker.read_line(5)
            if "ERASE_START" not in resp:
                raise RuntimeError(f"Unexpected erase response: '{resp}'")
            self._log("Erase in progress (DQ7 polling active)…")

            result = self.worker.read_line(75)
            self._log(f"Erase result: {result}", "data")
            if "ERASE_OK" in result:
                self._log("Chip erased successfully.", "ok")
                self._status("Erase complete")
                self.root.after(0, lambda: messagebox.showinfo(
                    "Erase Complete", "Chip erased successfully."))
            else:
                raise RuntimeError(f"Erase failed or timed out: '{result}'")
        except Exception as e:
            err_msg = str(e)
            self._log(f"Erase error: {err_msg}", "err")
            self.root.after(0, lambda _m=err_msg: messagebox.showerror("Erase Failed", _m))
        finally:
            self._end_op()

    # ── WRITE ────────────────────────────────────────────────────────────────
    def _ask_write(self):
        fp = filedialog.askopenfilename(
            title="Select ROM / BIN File",
            filetypes=[("Binary / ROM", "*.bin *.rom *.img"), ("All Files", "*.*")])
        if not fp:
            return
        chip = CHIP_DB[self.var_chip.get()]
        fsize = os.path.getsize(fp)
        if fsize > chip["size"]:
            messagebox.showerror("File Too Large",
                f"File is {fsize:,} bytes but chip holds only {chip['size']:,} bytes.")
            return
        if not messagebox.askyesno("Confirm Write",
                f"Write '{os.path.basename(fp)}' ({fsize:,} bytes)?\n\n"
                "Make sure the chip is erased first."):
            return
        self._start_op(self._do_write, fp)

    def _do_write(self, filepath):
        f = None
        try:
            fsize = os.path.getsize(filepath)
            self._log(f"Writing {os.path.basename(filepath)} ({fsize:,} bytes)…")
            f = open(filepath, "rb")
            data = f.read()

            self.worker.flush()
            self.worker.send_cmd(f"W {fsize}")

            resp = self.worker.read_line(5)
            if "WREADY" not in resp:
                raise RuntimeError(f"Arduino not ready: '{resp}'")
            self._log("Arduino ready — byte-by-byte handshake upload…")

            # --- Byte-by-byte handshake write protocol ---
            # Arduino programs one byte, sends 'K' ACK, we send next byte.
            # This avoids overflowing the Arduino's 64-byte UART receive buffer.
            # Throughput is ~5 KB/s — slow but 100% reliable with any file content.
            #
            # Serial framing:
            #   We send 1 byte, Arduino replies 'K' (1 byte, no newline).
            #   On error: Arduino sends "WERR:<addr>\n"
            #   On success: Arduino sends "WDONE:<n>\n"
            sent = 0
            t0   = time.time()

            # Set a tight per-byte timeout: Arduino should ACK within 10 ms
            # (byte-program time is ≤200 µs + serial latency).
            self.serial_port.timeout = 3.0   # generous: 3 s per byte

            UPDATE_EVERY = 128   # update progress bar every N bytes

            for byte_val in data:
                # Send one byte
                self.serial_port.write(bytes([byte_val]))

                # Wait for ACK ('K') or an error line
                ack = self.serial_port.read(1)
                if not ack:
                    raise TimeoutError(
                        f"No ACK from Arduino after byte {sent} "
                        f"(value 0x{byte_val:02X}). Check wiring.")

                if ack == b'K':
                    sent += 1
                else:
                    # Something unexpected — could be start of "WERR..." line
                    # Read rest of the line to get full error message
                    rest = b""
                    self.serial_port.timeout = 1.0
                    while True:
                        ch = self.serial_port.read(1)
                        if not ch or ch == b'\n':
                            break
                        rest += ch
                    full_msg = (ack + rest).decode(errors="replace").strip()
                    raise RuntimeError(f"Arduino error at byte {sent}: '{full_msg}'")

                # Update progress every N bytes to avoid flooding the UI
                if sent % UPDATE_EVERY == 0 or sent == fsize:
                    el  = time.time() - t0
                    spd = sent / el if el > 0.1 else 0
                    eta = (fsize - sent) / spd if spd > 10 else 0
                    self._set_progress(sent, fsize, "Writing…",
                        f"{sent:,}/{fsize:,} bytes  {spd/1024:.1f} KB/s  ETA {eta:.0f}s")

            # All bytes sent — read the final WDONE/WERR response
            self.serial_port.timeout = 10.0
            resp = self.worker.read_line(10)
            self._log(f"  {resp}", "data")

            if resp.startswith("WDONE"):
                n       = resp.split(":")[1] if ":" in resp else str(sent)
                elapsed = time.time() - t0
                self._log(
                    f"Write complete: {n} bytes in {elapsed:.1f}s "
                    f"({int(sent/elapsed/1024*10)/10} KB/s).", "ok")
                self._status(f"Write complete: {n} bytes")
                # Capture n in default arg to avoid lambda closure bug
                self.root.after(0, lambda _n=n:
                    messagebox.showinfo("Write Complete",
                                        f"Programmed {_n} bytes successfully."))
            elif "WERR" in resp:
                raise RuntimeError(f"Programming error: {resp}")
            else:
                self._log(f"Unexpected final response: '{resp}'", "warn")

            if self.var_verify.get():
                self._log("Running post-write verification…")
                self._do_verify_data(data, 0)

        except Exception as e:
            # Capture e in default arg — avoids "cannot access free variable 'e'"
            # NameError that occurs when the lambda fires after the except block exits.
            err_msg = str(e)
            self._log(f"Write error: {err_msg}", "err")
            self.root.after(0, lambda _m=err_msg:
                messagebox.showerror("Write Error", _m))
        finally:
            if f: f.close()
            self._end_op()

    # ── VERIFY ───────────────────────────────────────────────────────────────
    def _ask_verify(self):
        fp = filedialog.askopenfilename(
            title="Reference File",
            filetypes=[("Binary", "*.bin *.rom *.img"), ("All Files", "*.*")])
        if fp:
            self._start_op(self._do_verify_file, fp)

    def _do_verify_file(self, filepath):
        try:
            with open(filepath, "rb") as f:
                ref = f.read()
            self._log(f"Verifying against {os.path.basename(filepath)} ({len(ref):,} bytes)…")
            self._do_verify_data(ref, 0)
        except Exception as e:
            self._log(f"Verify error: {e}", "err")
        finally:
            self._end_op()

    def _do_verify_data(self, ref: bytes, start_addr: int):
        size     = len(ref)
        end_addr = start_addr + size - 1
        self.worker.flush()
        self.worker.send_cmd(f"R {start_addr:X} {end_addr:X}")

        header = self.worker.read_line(10)
        if not header.startswith("RSTART"):
            self._log(f"Verify read failed: {header}", "err")
            return
        byte_count = int(header.split()[1]) if len(header.split()) > 1 else size

        chip_data = self.worker.read_exact(
            byte_count,
            lambda g, t: self._set_progress(g, t, "Verifying…", f"{g:,}/{t:,} bytes"),
            timeout=180)
        self.worker.read_line(5)

        errors = [(start_addr + i, ref[i], chip_data[i])
                  for i in range(min(len(ref), len(chip_data)))
                  if ref[i] != chip_data[i]]

        if not errors:
            self._log(f"Verify PASSED — {size:,} bytes match.", "ok")
            self.root.after(0, lambda: messagebox.showinfo(
                "Verify Passed", f"All {size:,} bytes verified OK."))
        else:
            self._log(f"Verify FAILED — {len(errors)} mismatch(es).", "err")
            detail = "\n".join(f"  0x{a:06X}: expected 0x{e:02X}, got 0x{g:02X}"
                               for a, e, g in errors[:10])
            msg = (f"{len(errors)} mismatch(es):\n{detail}"
                   + ("\n  (first 10 shown)" if len(errors) > 10 else ""))
            self.root.after(0, lambda: messagebox.showerror("Verify Failed", msg))

    # ── HEX VIEW ─────────────────────────────────────────────────────────────
    def _display_hex(self, data: bytes, base: int = 0):
        self.hex_text.config(state="normal")
        self.hex_text.delete("1.0", "end")
        COLS = 16
        for i in range(0, len(data), COLS):
            chunk = data[i:i+COLS]
            addr  = base + i
            self.hex_text.insert("end", f"{addr:06X}  ", "addr")
            self.hex_text.insert("end", "│ ", "sep")
            for j, b in enumerate(chunk):
                tag = "ff" if b == 0xFF else "data"
                self.hex_text.insert("end", f"{b:02X} ", tag)
                if j == 7:
                    self.hex_text.insert("end", " ")
            if len(chunk) < COLS:
                pad = (COLS - len(chunk)) * 3
                if len(chunk) <= 8: pad += 1
                self.hex_text.insert("end", " " * pad)
            self.hex_text.insert("end", "│ ", "sep")
            asc = "".join(chr(b) if 32 <= b <= 126 else "·" for b in chunk)
            self.hex_text.insert("end", asc + "\n", "ascii")
        self.hex_text.config(state="disabled")
        self.lbl_hex_info.config(
            text=f"{len(data):,} bytes  │  0x{base:06X} – 0x{base+len(data)-1:06X}")

    def _clear_hex(self):
        self.hex_text.config(state="normal")
        self.hex_text.delete("1.0", "end")
        self.hex_text.config(state="disabled")
        self.lbl_hex_info.config(text="No data loaded")
        self.current_data = None
        self.btn_save_bin.state(["disabled"])

    # ── SAVE ─────────────────────────────────────────────────────────────────
    def _save_binary(self):
        if not self.current_data:
            return
        fp = filedialog.asksaveasfilename(
            defaultextension=".bin",
            filetypes=[("Binary", "*.bin"), ("ROM", "*.rom"), ("All", "*.*")])
        if fp:
            with open(fp, "wb") as f:
                f.write(self.current_data)
            self._log(f"Saved {len(self.current_data):,} bytes → {fp}", "ok")

    # ── LOG ACTIONS ──────────────────────────────────────────────────────────
    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _save_log(self):
        fp = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All", "*.*")])
        if fp:
            content = self.log_text.get("1.0", "end")
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content)
            self._log(f"Log saved → {fp}", "ok")

    # ── HELP ─────────────────────────────────────────────────────────────────
    def _show_help(self):
        win = tk.Toplevel(self.root)
        win.title("Help — AM29F010 / AM29F040B Flash Programmer")
        win.geometry("680x560")
        win.resizable(True, True)
        win.configure(bg=C["bg"])
        win.grab_set()

        # Header
        tk.Frame(win, bg=C["hdr_bg"], pady=7).pack(fill="x")
        tk.Label(win.winfo_children()[-1],
                 text="Flash Programmer — Help",
                 font=FH, bg=C["hdr_bg"], fg=C["hdr_fg"]).pack(side="left", padx=12)

        # Scrollable text area
        frame = tk.Frame(win, bg=C["bg"])
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        txt = tk.Text(frame, font=FU, bg=C["panel"], fg=C["text"],
                      relief="flat", padx=12, pady=10, wrap="word",
                      state="normal")
        sb = ttk.Scrollbar(frame, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)

        txt.tag_configure("h1",  font=("Segoe UI", 11, "bold"), foreground=C["accent"],
                          spacing1=10, spacing3=4)
        txt.tag_configure("h2",  font=("Segoe UI", 9,  "bold"), foreground=C["text"],
                          spacing1=8, spacing3=2)
        txt.tag_configure("body", font=FU, foreground=C["text"], spacing3=3)
        txt.tag_configure("code", font=FM, foreground=C["accent"],
                          background="#EEF4FF")
        txt.tag_configure("warn", font=FU, foreground=C["err"])

        help_content = [
            ("h1",  "Overview"),
            ("body","This tool programs AM29F010 (128 KB) and AM29F040B (512 KB) parallel flash "
                    "chips via an Arduino Nano acting as a USB-to-parallel bridge. The Arduino "
                    "must be running the FLASH_PROGRAMMER.ino sketch."),

            ("h1",  "Connection"),
            ("h2",  "Port"),
            ("body","Select the COM port that corresponds to your Arduino Nano. Click ↺ to "
                    "refresh the port list if the Arduino was connected after the app started."),
            ("h2",  "Baud Rate"),
            ("body","Must match the baud rate set in the Arduino sketch. Default: 115200."),
            ("h2",  "Connect / Disconnect"),
            ("body","Click Connect to open the serial port and initialize the Arduino. The app "
                    "will pulse DTR to reset the board and wait up to 3 seconds for it to boot. "
                    "If the Arduino does not respond, check the port, baud rate, and that the "
                    "correct sketch is uploaded."),

            ("h1",  "Chip Selection"),
            ("body","Choose your flash chip from the dropdown. Click Apply to send the chip "
                    "index to the Arduino. This configures the correct address range and timing."),
            ("h2",  "Read Chip ID"),
            ("body","Sends the autoselect command sequence to the chip and reads back the "
                    "manufacturer and device ID bytes. Use this to confirm you have the right "
                    "chip inserted and that the wiring is correct."),

            ("h1",  "Operations"),
            ("h2",  "Read Memory"),
            ("body","Reads the flash contents over the address range specified in the "
                    "Start / End fields and displays the result in the Hex View panel. "
                    "The data is also held in memory so you can save it as a binary file."),
            ("h2",  "Erase Chip"),
            ("body","Issues a full-chip erase command (AMD CFI sector erase sequence). "
                    "All bytes will be set to 0xFF. This typically takes 1–5 seconds. "
                    "⚠ All existing data will be permanently erased."),
            ("h2",  "Write File…"),
            ("body","Opens a file picker, then writes the selected binary to the flash "
                    "chip starting at address 0. Programming uses a byte-by-byte ACK "
                    "handshake at ~5 KB/s for reliability. If Verify after write is "
                    "checked, a full readback comparison is run automatically."),
            ("h2",  "Verify vs File…"),
            ("body","Reads the chip and compares it byte-for-byte against a reference "
                    "binary file you select. Reports pass/fail and lists the first 10 "
                    "mismatches if the verification fails."),
            ("h2",  "Save Binary"),
            ("body","Saves the last data read from the chip as a .bin / .rom file "
                    "to disk. Enabled only after a successful read."),

            ("h1",  "Read Range"),
            ("body","Enter hex addresses (without 0x prefix) for Start and End. "
                    "Click Full Chip to auto-fill the range for the selected chip. "
                    "Example: Start "),
            ("code","000000"),
            ("body"," End "),
            ("code","07FFFF"),
            ("body"," reads all 512 KB."),

            ("h1",  "Hex View  /  Event Log"),
            ("body","The window is split into two panels. The left panel shows the Event Log "
                    "with timestamped messages for every operation. The right panel shows the "
                    "Hex View of the last read data. You can drag the divider between them to "
                    "resize the panels."),
            ("body","Use the Clear button in each panel to reset its contents. Use Save Log "
                    "to export the event log to a .log text file."),

            ("h1",  "Troubleshooting"),
            ("h2",  "Arduino not responding"),
            ("body","• Verify the correct COM port and baud rate (115200).\n"
                    "• Press the Arduino Reset button, then click Connect.\n"
                    "• Re-upload FLASH_PROGRAMMER.ino and try again."),
            ("h2",  "Verify failures after write"),
            ("body","• Check VCC and GND connections to the flash chip.\n"
                    "• Ensure the chip was fully erased before writing.\n"
                    "• Try reducing baud rate to 57600 for noisy setups."),
            ("h2",  "Chip ID reads as FF/FF"),
            ("body","• Check address and data bus wiring.\n"
                    "• Confirm the chip is powered and /CE, /OE, /WE are connected correctly."),
        ]

        for i, (tag, text) in enumerate(help_content):
            # Add a blank line before h1/h2 headings (except the very first item)
            if tag in ("h1", "h2") and i > 0:
                txt.insert("end", "\n")
            txt.insert("end", text, tag)
            if tag in ("h1", "h2"):
                txt.insert("end", "\n")

        txt.config(state="disabled")

        ttk.Button(win, text="Close", command=win.destroy,
                   style="Accent.TButton").pack(pady=(0,10))


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
