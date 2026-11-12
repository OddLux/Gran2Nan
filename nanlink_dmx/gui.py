"""Tkinter desktop UI for Gran2Nan, the Art-Net -> Nanlink bridge."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import queue
import socket
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import APP_NAME, __version__, keyimport, mesh
from .artnet import universe_label
from .config import Config, FixtureConfig, load_config, save_config
from .engine import Bridge
from .fixtures import PERSONALITIES, describe_value
from .link import scan_for_boxes

STATE_COLORS = {"connected": "#2e9d48", "connecting": "#d9a400", "scanning": "#d9a400",
                "disconnected": "#c8392e", "not configured": "#888888"}


class AsyncRunner:
    """Runs the asyncio side (BLE + Art-Net) in a background thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="asyncio")
        self.thread.start()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn, *args):
        self.loop.call_soon_threadsafe(fn, *args)


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(("log", self.format(record)))


def local_ipv4_addresses() -> list[str]:
    ips = {"0.0.0.0"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.add("127.0.0.1")
    return sorted(ips, key=lambda ip: (ip != "0.0.0.0", ip.startswith("127."), ip))


# --------------------------------------------------------------------------
# Fixture editor
# --------------------------------------------------------------------------

class FixtureDialog(tk.Toplevel):
    def __init__(self, parent, fixture: FixtureConfig, title: str):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.result: FixtureConfig | None = None
        self._labels = {p.label + ("  (experimental)" if p.experimental else ""): p.key
                        for p in PERSONALITIES.values()}
        key_to_label = {v: k for k, v in self._labels.items()}

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        self.name = tk.StringVar(value=fixture.name)
        self.channel = tk.IntVar(value=fixture.channel)
        self.address = tk.IntVar(value=fixture.dmx_address)
        self.ptype = tk.StringVar(value=key_to_label.get(fixture.personality, next(iter(self._labels))))
        self.enabled = tk.BooleanVar(value=fixture.enabled)
        opts = fixture.options or {}
        self.cct_min = tk.IntVar(value=int(opts.get("cct_min", 2700)))
        self.cct_max = tk.IntVar(value=int(opts.get("cct_max", 6500)))

        rows = [
            ("Name", ttk.Entry(f, textvariable=self.name, width=28)),
            ("Nanlink channel", ttk.Spinbox(f, from_=1, to=512, textvariable=self.channel, width=8)),
            ("DMX start address", ttk.Spinbox(f, from_=1, to=512, textvariable=self.address, width=8)),
            ("Type", ttk.Combobox(f, textvariable=self.ptype, values=list(self._labels), state="readonly",
                                  width=40)),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            widget.grid(row=r, column=1, sticky="w", pady=3)
        self.cct_frame = ttk.Frame(f)
        ttk.Label(self.cct_frame, text="CCT range (K)").pack(side="left", padx=(0, 10))
        ttk.Spinbox(self.cct_frame, from_=1500, to=20000, increment=100, textvariable=self.cct_min,
                    width=7).pack(side="left")
        ttk.Label(self.cct_frame, text=" – ").pack(side="left")
        ttk.Spinbox(self.cct_frame, from_=1500, to=20000, increment=100, textvariable=self.cct_max,
                    width=7).pack(side="left")
        self.cct_frame.grid(row=4, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(f, text="Enabled", variable=self.enabled).grid(row=5, column=1, sticky="w", pady=3)
        self.footprint = ttk.Label(f, foreground="#555")
        self.footprint.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

        b = ttk.Frame(f)
        b.grid(row=7, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(b, text="OK", command=self._ok, default="active").pack(side="left", padx=4)
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.ptype.trace_add("write", lambda *a: self._refresh())
        self.address.trace_add("write", lambda *a: self._refresh())
        self._refresh()
        self.grab_set()
        self.wait_visibility()
        self.focus_set()

    def _refresh(self):
        p = PERSONALITIES[self._labels[self.ptype.get()]]
        if p.key == "cct":
            self.cct_frame.grid()
        else:
            self.cct_frame.grid_remove()
        try:
            start = int(self.address.get())
        except (tk.TclError, ValueError):
            start = 1
        names = ", ".join(f"{start + i}: {n}" for i, n in enumerate(p.channels))
        self.footprint.config(text=f"DMX channels: {names}")

    def _ok(self):
        try:
            channel, address = int(self.channel.get()), int(self.address.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Fixture", "Channel and address must be numbers.", parent=self)
            return
        p = PERSONALITIES[self._labels[self.ptype.get()]]
        if not 1 <= channel <= 512 or not 1 <= address <= 513 - p.footprint:
            messagebox.showerror("Fixture", "Channel must be 1-512 and the footprint must fit in 512.",
                                 parent=self)
            return
        options = {}
        if p.key == "cct":
            options = {"cct_min": int(self.cct_min.get()), "cct_max": int(self.cct_max.get())}
        self.result = FixtureConfig(self.name.get().strip() or f"Light {channel}", channel, address,
                                    p.key, bool(self.enabled.get()), options)
        self.destroy()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        ui = max(1.0, self.winfo_fpixels("1i") / 96)  # Windows display scaling (1.0 = 100 %)
        self.geometry(f"{int(1000 * ui)}x{int(660 * ui)}")
        self.minsize(int(860 * ui), int(520 * ui))
        ttk.Style(self).configure("Treeview", rowheight=int(22 * ui))
        self.events: queue.Queue = queue.Queue()
        self.cfg = load_config()
        self.runner = AsyncRunner()
        self.bridge = Bridge(self.cfg.copy(), emit=lambda kind, data: self.events.put((kind, data)))
        self.status: dict = {}
        self._fader_rows: list[list[dict]] = []
        self._dragging: set[tuple[int, int]] = set()
        self._setting_faders = False

        handler = QueueLogHandler(self.events)
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        logging.getLogger("nanlink").addHandler(handler)
        logging.getLogger("nanlink").setLevel(logging.INFO)

        self._build_header()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._build_control_tab()
        self._build_settings_tab()
        self._build_monitor_tab()
        self._build_log_tab()
        self._build_about_tab()
        self._rebuild_fixture_views()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._poll_events)
        if not (self.cfg.mesh.net_key and self.cfg.mesh.app_key):
            self.nb.select(self.settings_tab)
            self._log("Welcome! Import the mesh keys from your phone in Settings to get started.")
        if self.cfg.output.autostart:
            self._start()

    # ---- header ------------------------------------------------------------

    def _build_header(self):
        h = ttk.Frame(self, padding=(8, 8))
        h.pack(fill="x")
        self.run_btn = ttk.Button(h, text="Start", width=10, command=self._toggle_run)
        self.run_btn.pack(side="left")
        self.ble_dot = tk.Canvas(h, width=14, height=14, highlightthickness=0)
        self.ble_dot.pack(side="left", padx=(16, 4))
        self.ble_lbl = ttk.Label(h, text="Bluetooth: stopped", width=26)
        self.ble_lbl.pack(side="left")
        self.art_dot = tk.Canvas(h, width=14, height=14, highlightthickness=0)
        self.art_dot.pack(side="left", padx=(8, 4))
        self.art_lbl = ttk.Label(h, text="Art-Net: -", width=34)
        self.art_lbl.pack(side="left")
        self.rate_lbl = ttk.Label(h, text="", width=18)
        self.rate_lbl.pack(side="right")
        self._dot(self.ble_dot, "#888")
        self._dot(self.art_dot, "#888")

    @staticmethod
    def _dot(canvas: tk.Canvas, color: str):
        canvas.delete("all")
        canvas.create_oval(2, 2, 12, 12, fill=color, outline="")

    # ---- control tab ---------------------------------------------------------

    def _build_control_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Fixtures & Control")
        tab.columnconfigure(0, weight=3)
        tab.columnconfigure(1, weight=4)
        tab.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(tab, text="Patch", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        cols = ("name", "ch", "dmx", "type", "out")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse", height=12)
        for c, text, w in (("name", "Name", 110), ("ch", "Nanlink ch", 70), ("dmx", "DMX", 70),
                           ("type", "Type", 90), ("out", "Output", 150)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor="w", stretch=c in ("name", "out"))
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self._edit_fixture())
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(6, 0))
        for text, cmd in (("Add…", self._add_fixture), ("Edit…", self._edit_fixture),
                          ("Remove", self._remove_fixture), ("Identify", self._identify)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=(0, 4))

        right = ttk.LabelFrame(tab, text="Manual control", padding=6)
        right.grid(row=0, column=1, sticky="nsew")
        top = ttk.Frame(right)
        top.pack(fill="x")
        self.manual_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Manual only (ignore console)", variable=self.manual_only,
                        command=lambda: self.runner.call(self.bridge.set_manual_only,
                                                         self.manual_only.get())).pack(side="left")
        ttk.Button(top, text="All off", command=lambda: self._all_dimmers(0)).pack(side="right")
        ttk.Button(top, text="All full", command=lambda: self._all_dimmers(255)).pack(side="right", padx=4)
        self.control_lbl = ttk.Label(right, text="", foreground="#555", wraplength=420)
        self.control_lbl.pack(fill="x", pady=(4, 6))

        canvas = tk.Canvas(right, highlightthickness=0)
        scroll = ttk.Scrollbar(right, orient="vertical", command=canvas.yview)
        self.faders = ttk.Frame(canvas)
        self.faders.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=self.faders, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _rebuild_fixture_views(self):
        self.tree.delete(*self.tree.get_children())
        for i, f in enumerate(self.cfg.fixtures):
            p = PERSONALITIES.get(f.personality)
            end = f.dmx_address + (p.footprint if p else 1) - 1
            dmx = str(f.dmx_address) if end == f.dmx_address else f"{f.dmx_address}-{end}"
            self.tree.insert("", "end", iid=str(i), values=(
                f.name + ("" if f.enabled else " (off)"), f.channel, dmx,
                p.key.upper() if p else f.personality, ""))

        for child in self.faders.winfo_children():
            child.destroy()
        self._fader_rows = []
        for i, f in enumerate(self.cfg.fixtures):
            p = PERSONALITIES.get(f.personality)
            box = ttk.LabelFrame(self.faders, text=f"{f.name}  ·  Nanlink ch {f.channel}", padding=6)
            box.pack(fill="x", pady=(0, 6), padx=(0, 4))
            box.columnconfigure(1, weight=1)
            row = []
            for s, chname in enumerate(p.channels if p else ()):
                var = tk.DoubleVar(value=0)
                ttk.Label(box, text=chname, width=10).grid(row=s, column=0, sticky="w")
                scale = ttk.Scale(box, from_=0, to=255, variable=var,
                                  command=lambda v, i=i, s=s: self._fader_moved(i, s, v))
                scale.grid(row=s, column=1, sticky="ew", padx=6)
                scale.bind("<ButtonPress-1>", lambda e, k=(i, s): self._dragging.add(k))
                scale.bind("<ButtonRelease-1>", lambda e, k=(i, s): self._dragging.discard(k))
                lbl = ttk.Label(box, text="0", width=14)
                lbl.grid(row=s, column=2, sticky="e")
                row.append({"var": var, "scale": scale, "label": lbl})
            self._fader_rows.append(row)
        if not self.cfg.fixtures:
            ttk.Label(self.faders, text="No fixtures yet - click 'Add…' to patch a light.").pack(pady=20)

    def _fader_moved(self, index: int, slot: int, value: str):
        if self._setting_faders:
            return
        self.runner.call(self.bridge.set_manual, index, slot, int(round(float(value))))

    def _all_dimmers(self, value: int):
        if self.status.get("control") != "manual":
            messagebox.showinfo("Manual control", "The console is in control. Tick 'Manual only' "
                                "or stop Art-Net to use manual control.")
            return
        for i, row in enumerate(self._fader_rows):
            if row:
                self.runner.call(self.bridge.set_manual, i, 0, value)

    def _selected(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _add_fixture(self):
        used = {f.channel for f in self.cfg.fixtures}
        channel = next((c for c in range(1, 513) if c not in used), 1)
        address = 1
        for f in self.cfg.fixtures:
            p = PERSONALITIES.get(f.personality)
            address = max(address, f.dmx_address + (p.footprint if p else 1))
        d = FixtureDialog(self, FixtureConfig(f"Light {channel}", channel, min(address, 512)), "Add fixture")
        self.wait_window(d)
        if d.result:
            self.cfg.fixtures.append(d.result)
            self._fixtures_changed()

    def _edit_fixture(self):
        i = self._selected()
        if i is None:
            return
        d = FixtureDialog(self, self.cfg.fixtures[i], "Edit fixture")
        self.wait_window(d)
        if d.result:
            self.cfg.fixtures[i] = d.result
            self._fixtures_changed()

    def _remove_fixture(self):
        i = self._selected()
        if i is not None and messagebox.askyesno("Remove", f"Remove {self.cfg.fixtures[i].name}?"):
            del self.cfg.fixtures[i]
            self._fixtures_changed()

    def _identify(self):
        i = self._selected()
        if i is not None:
            self.runner.call(self.bridge.identify, i)

    def _fixtures_changed(self):
        self._check_overlaps()
        save_config(self.cfg)
        self.runner.submit(self.bridge.apply_config(self.cfg.copy()))
        self._rebuild_fixture_views()

    def _check_overlaps(self):
        used: dict[int, str] = {}
        for f in self.cfg.fixtures:
            p = PERSONALITIES.get(f.personality)
            for a in range(f.dmx_address, f.dmx_address + (p.footprint if p else 1)):
                if a in used and used[a] != f.name:
                    self._log(f"Note: DMX {a} is shared by {used[a]} and {f.name}")
                used[a] = f.name

    # ---- settings tab ------------------------------------------------------

    def _build_settings_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.settings_tab = tab
        self.nb.add(tab, text="Settings")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        c = self.cfg

        # Bluetooth + mesh
        bt = ttk.LabelFrame(tab, text="WS-TB-1 (Bluetooth Mesh)", padding=8)
        bt.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        bt.columnconfigure(1, weight=1)
        self.v_addr = tk.StringVar(value=c.ble.device_address)
        self.v_net = tk.StringVar(value=c.mesh.net_key)
        self.v_app = tk.StringVar(value=c.mesh.app_key)
        self.v_box = tk.StringVar(value=f"0x{c.mesh.box_address:04X}")
        self.v_src = tk.StringVar(value=f"0x{c.mesh.src_address:04X}" if c.mesh.src_address else "auto")
        self.v_iv = tk.IntVar(value=c.mesh.iv_index)
        r = 0
        ttk.Label(bt, text="Box Bluetooth address").grid(row=r, column=0, sticky="w", pady=2)
        af = ttk.Frame(bt)
        af.grid(row=r, column=1, sticky="ew", pady=2)
        self.addr_combo = ttk.Combobox(af, textvariable=self.v_addr, width=22)
        self.addr_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(af, text="Scan", command=self._scan).pack(side="left", padx=(4, 0))
        for label, var in (("NetKey", self.v_net), ("AppKey", self.v_app),
                           ("Box mesh address", self.v_box), ("Our mesh address", self.v_src)):
            r += 1
            ttk.Label(bt, text=label).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(bt, textvariable=var, width=40,
                      show="•" if "Key" in label else "").grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        ttk.Label(bt, text="IV index").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Spinbox(bt, from_=0, to=2 ** 32 - 1, textvariable=self.v_iv, width=12).grid(
            row=r, column=1, sticky="w", pady=2)
        r += 1
        imp = ttk.Frame(bt)
        imp.grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(imp, text="Pair box…", command=self._pair_box).pack(side="left", padx=(0, 4))
        ttk.Button(imp, text="Import keys from phone (adb)", command=self._import_adb).pack(side="left")
        ttk.Button(imp, text="Import from log file…", command=self._import_file).pack(side="left", padx=4)
        r += 1
        self.key_info = ttk.Label(bt, foreground="#555", wraplength=420, justify="left")
        self.key_info.grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(bt, foreground="#555", wraplength=420, justify="left", text=(
            "Pair box: factory-reset the WS-TB-1, then click it - the bridge creates its own keys "
            "(no phone needed; the Nanlink app can no longer control the box). Import from phone: "
            "keeps the Nanlink app's pairing.")).grid(row=r + 1, column=0, columnspan=2,
                                                                  sticky="w", pady=(4, 0))

        # Art-Net
        an = ttk.LabelFrame(tab, text="Art-Net input", padding=8)
        an.grid(row=0, column=1, sticky="nsew", pady=(0, 6))
        self.v_bind = tk.StringVar(value=c.artnet.bind_ip)
        u = c.artnet.universe
        self.v_net_n = tk.IntVar(value=u >> 8)
        self.v_sub = tk.IntVar(value=(u >> 4) & 0xF)
        self.v_uni = tk.IntVar(value=u & 0xF)
        self.v_name = tk.StringVar(value=c.artnet.short_name)
        ttk.Label(an, text="Listen on").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Combobox(an, textvariable=self.v_bind, values=local_ipv4_addresses(), width=18).grid(
            row=0, column=1, sticky="w", pady=2)
        ttk.Label(an, text="Net : Sub : Universe").grid(row=1, column=0, sticky="w", pady=2)
        uf = ttk.Frame(an)
        uf.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Spinbox(uf, from_=0, to=127, textvariable=self.v_net_n, width=4).pack(side="left")
        ttk.Label(uf, text=":").pack(side="left")
        ttk.Spinbox(uf, from_=0, to=15, textvariable=self.v_sub, width=3).pack(side="left")
        ttk.Label(uf, text=":").pack(side="left")
        ttk.Spinbox(uf, from_=0, to=15, textvariable=self.v_uni, width=3).pack(side="left")
        ttk.Label(an, text="Node name").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(an, textvariable=self.v_name, width=20).grid(row=2, column=1, sticky="w", pady=2)
        ttk.Label(an, foreground="#555", wraplength=330, justify="left", text=(
            "Most consoles call Art-Net 0:0:0 \"Universe 1\". 'Listen on 0.0.0.0' accepts all "
            "network adapters.")).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Output
        out = ttk.LabelFrame(tab, text="Output", padding=8)
        out.grid(row=1, column=1, sticky="nsew")
        self.v_rate = tk.DoubleVar(value=c.output.max_rate)
        self.v_burst = tk.IntVar(value=c.output.burst)
        self.v_settle = tk.BooleanVar(value=c.output.settle_repeat)
        self.v_refresh = tk.DoubleVar(value=c.output.refresh_interval)
        self.v_auto = tk.BooleanVar(value=c.output.autostart)
        ttk.Label(out, text="Max messages / second").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Spinbox(out, from_=1, to=100, textvariable=self.v_rate, width=6).grid(row=0, column=1, sticky="w")
        ttk.Label(out, text="Refresh all every (s, 0 = off)").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Spinbox(out, from_=0, to=600, textvariable=self.v_refresh, width=6).grid(
            row=1, column=1, sticky="w")
        ttk.Label(out, text="Burst (messages at once)").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Spinbox(out, from_=1, to=32, textvariable=self.v_burst, width=6).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(out, text="Repeat each final value once (more reliable)",
                        variable=self.v_settle).grid(row=3, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(out, text="Start bridge when the app opens",
                        variable=self.v_auto).grid(row=4, column=0, columnspan=2, sticky="w", pady=2)

        bottom = ttk.Frame(tab)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(bottom, text="Export settings…", command=self._export_settings).pack(side="left")
        ttk.Button(bottom, text="Import settings…", command=self._import_settings).pack(side="left", padx=4)
        ttk.Button(bottom, text="Save && apply", command=self._save_settings).pack(side="right")
        self._update_key_info()

    def _update_key_info(self):
        try:
            k = mesh.MeshKeys(mesh.parse_key(self.v_net.get()), mesh.parse_key(self.v_app.get()))
            self.key_info.config(text=f"Keys OK · network ID {k.network_id.hex()} · AID {k.aid}")
        except Exception:
            self.key_info.config(text="Keys not set.")

    def _settings_to_cfg(self) -> Config | None:
        cfg = self.cfg.copy()
        try:
            if self.v_net.get().strip() or self.v_app.get().strip():
                cfg.mesh.net_key = mesh.parse_key(self.v_net.get()).hex()
                cfg.mesh.app_key = mesh.parse_key(self.v_app.get()).hex()
            cfg.mesh.box_address = int(self.v_box.get(), 0)
            src = self.v_src.get().strip().lower()
            cfg.mesh.src_address = 0 if src in ("", "auto", "0") else int(src, 0)
            cfg.mesh.iv_index = int(self.v_iv.get())
            if not 0 <= cfg.mesh.src_address <= 0x7FFF or not 1 <= cfg.mesh.box_address <= 0x7FFF:
                raise ValueError("mesh addresses must be between 0x0001 and 0x7FFF")
            if cfg.mesh.src_address == cfg.mesh.box_address:
                raise ValueError("our mesh address must differ from the box address")
            cfg.ble.device_address = self.v_addr.get().split(" ")[0].strip()
            cfg.artnet.bind_ip = self.v_bind.get().strip() or "0.0.0.0"
            socket.inet_aton(cfg.artnet.bind_ip)
            cfg.artnet.universe = (int(self.v_net_n.get()) & 0x7F) << 8 | (int(self.v_sub.get()) & 0xF) << 4 \
                | (int(self.v_uni.get()) & 0xF)
            cfg.artnet.short_name = self.v_name.get().strip() or "Gran2Nan"
            cfg.output.max_rate = max(1.0, min(100.0, float(self.v_rate.get())))
            cfg.output.burst = max(1, min(32, int(self.v_burst.get())))
            cfg.output.refresh_interval = max(0.0, float(self.v_refresh.get()))
            cfg.output.settle_repeat = bool(self.v_settle.get())
            cfg.output.autostart = bool(self.v_auto.get())
        except (ValueError, tk.TclError, OSError) as e:
            messagebox.showerror("Settings", f"Invalid setting: {e}")
            return None
        return cfg

    def _save_settings(self):
        cfg = self._settings_to_cfg()
        if not cfg:
            return
        self.cfg = cfg
        save_config(cfg)
        self._update_key_info()
        self.runner.submit(self.bridge.apply_config(cfg.copy()))
        self._log(f"Settings saved. Art-Net universe {universe_label(cfg.artnet.universe)}.")

    def _export_settings(self):
        path = filedialog.asksaveasfilename(title="Export settings", defaultextension=".json",
                                            initialfile="nanlink-bridge-settings.json",
                                            filetypes=[("Settings", "*.json")])
        if path:
            save_config(self.cfg, path)
            self._log(f"Settings exported to {path} (contains the mesh keys - keep it private)")

    def _import_settings(self):
        path = filedialog.askopenfilename(title="Import settings", filetypes=[("Settings", "*.json")])
        if not path:
            return
        try:
            cfg = load_config(path)
        except (OSError, ValueError) as e:
            messagebox.showerror("Import settings", f"Could not read {path}: {e}")
            return
        self.cfg = cfg
        save_config(cfg)
        self.runner.submit(self.bridge.apply_config(cfg.copy()))
        # Rebuild the settings form with the imported values
        current = self.nb.index(self.nb.select())
        self.nb.forget(self.settings_tab)
        self._build_settings_tab()
        self.nb.insert(1, self.settings_tab, text="Settings")
        self.nb.select(current)
        self._rebuild_fixture_views()
        self._layout_monitor()
        self._log(f"Settings imported from {path}")

    def _scan(self):
        self.addr_combo.config(values=["scanning…"])
        fut = self.runner.submit(scan_for_boxes())

        def done():
            if not fut.done():
                self.after(200, done)
                return
            try:
                found = fut.result()
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Scan", f"Bluetooth scan failed: {e}")
                return
            items = [f"{a}  {n}  ({r} dBm)" for a, n, r in found]
            self.addr_combo.config(values=items)
            if found and not self.v_addr.get().strip():
                self.v_addr.set(found[0][0])
            self._log(f"Scan found {len(found)} mesh device(s)" + (": " + ", ".join(items) if items else ""))

        self.after(200, done)

    def _pair_box(self):
        if not messagebox.askokcancel("Pair box", (
                "1. Factory-reset the WS-TB-1 (see its manual) and keep it near this PC.\n"
                "2. Close the Nanlink app on your phone.\n\n"
                "The bridge will pair the box with new keys of its own. Afterwards the Nanlink app "
                "can't control the box until you reset it and add it there again.\n\nContinue?")):
            return
        from .provision import pair_box
        log = lambda text: self.events.put(("log", text))  # noqa: E731

        async def run():
            await self.bridge.release_link()
            return await pair_box(self.bridge.seq_store(), log)

        self.config(cursor="watch")
        fut = self.runner.submit(run())

        def done():
            if not fut.done():
                self.after(200, done)
                return
            self.config(cursor="")
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                self._log(f"Pairing failed: {e}")
                messagebox.showerror("Pair box", f"Pairing failed:\n{e}")
                self.runner.submit(self.bridge.apply_config(self.cfg.copy()))  # reconnect as before
                return
            self.v_net.set(res.net_key.hex())
            self.v_app.set(res.app_key.hex())
            self.v_box.set(f"0x{res.unicast:04X}")
            self.v_iv.set(res.iv_index)
            self.v_addr.set(res.address)
            cfg = self._settings_to_cfg()
            if cfg:
                cfg.mesh.device_key = res.device_key.hex()
                self.cfg = cfg
                save_config(cfg)
                self._update_key_info()
                self.runner.submit(self.bridge.apply_config(cfg.copy()))
            self._log("Box paired. Settings saved - use 'Export settings' to copy them to other PCs.")
            messagebox.showinfo("Pair box", "The box is paired with the bridge and settings are saved.")

        done()

    def _import_adb(self):
        adb = keyimport.find_adb()
        if not adb:
            messagebox.showerror("Import", "adb was not found. Install Android platform-tools "
                                 "(e.g. 'winget install Google.PlatformTools') and try again.")
            return
        self.config(cursor="watch")
        result: dict = {}

        def work():
            try:
                result["text"] = keyimport.read_phone_log(adb)
            except Exception as e:  # noqa: BLE001
                result["error"] = str(e)

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def wait():
            if t.is_alive():
                self.after(200, wait)
                return
            self.config(cursor="")
            if "error" in result:
                messagebox.showerror("Import", result["error"])
            else:
                self._apply_import(keyimport.parse_log(result["text"]))

        wait()

    def _import_file(self):
        path = filedialog.askopenfilename(title="Logcat or bugreport",
                                          filetypes=[("Logs", "*.txt *.log *.zip"), ("All files", "*.*")])
        if path:
            self._apply_import(keyimport.parse_log(keyimport.read_file(path)))

    def _apply_import(self, res: keyimport.ImportResult):
        if not res.networks:
            messagebox.showwarning("Import", "No Nanlink mesh network found in the log.\n\nOpen the Nanlink "
                                   "app (close it fully first, then reopen) and connect to the WS-TB-1, "
                                   "then import again.")
            return
        mac = self.v_addr.get().split(" ")[0].strip()
        found = res.find_box(mac) if mac else None
        if found:
            net, node = found
        else:
            net = next(reversed(res.networks.values()))
            boxes = [n for n in net.nodes.values() if n.name != "FeasyMesh Provisioner"]
            node = boxes[0] if len(boxes) == 1 else None
        self.v_net.set(net.net_key.hex())
        self.v_iv.set(net.iv_index)
        if res.app_keys:
            self.v_app.set(res.app_keys[-1].hex())
        msg = [f"Network {net.uuid}", f"network ID {net.network_id.hex()}"]
        if node:
            self.v_box.set(f"0x{node.address:04X}")
            msg.append(f"box '{node.name}' at mesh address 0x{node.address:04X}")
            if not mac:
                mac_guess = node.uuid.replace("-", "")[:12].upper()
                self.v_addr.set(":".join(mac_guess[i:i + 2] for i in range(0, 12, 2)))
        else:
            msg.append("box address not identified - check 'Box mesh address'")
        if not res.app_keys:
            msg.append("AppKey not found in log (use the app to change a light, then import again)")
        self._update_key_info()
        self._log("Imported: " + ", ".join(msg))
        messagebox.showinfo("Import", "\n".join(msg) + "\n\nClick 'Save & apply' to use these settings.")

    # ---- monitor tab -------------------------------------------------------

    def _build_monitor_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="DMX monitor")
        self.mon_info = ttk.Label(tab, foreground="#555")
        self.mon_info.pack(anchor="w")
        self.mon = tk.Canvas(tab, background="#1e1e1e", highlightthickness=0)
        self.mon.pack(fill="both", expand=True, pady=(6, 0))
        self._mon_cells: list[tuple[int, int]] = []
        self.mon.bind("<Configure>", lambda e: self._layout_monitor())

    def _layout_monitor(self):
        self.mon.delete("all")
        w, h = max(self.mon.winfo_width(), 100), max(self.mon.winfo_height(), 100)
        cols, rows = 32, 16
        cw, ch = w / cols, h / rows
        patched = self._patched_slots()
        self._mon_cells = []
        for i in range(512):
            x, y = (i % cols) * cw, (i // cols) * ch
            rect = self.mon.create_rectangle(x + 1, y + 1, x + cw - 1, y + ch - 1, outline="#3b82f6"
                                             if i + 1 in patched else "#333", fill="#1e1e1e")
            txt = self.mon.create_text(x + cw / 2, y + ch / 2, text="", fill="#ddd",
                                       font=("Segoe UI", max(6, int(min(cw / 3.5, ch / 2.2)))))
            self._mon_cells.append((rect, txt))

    def _patched_slots(self) -> set[int]:
        slots = set()
        for f in self.cfg.fixtures:
            p = PERSONALITIES.get(f.personality)
            slots.update(range(f.dmx_address, f.dmx_address + (p.footprint if p else 1)))
        return slots

    def _update_monitor(self, data: bytes):
        if not self._mon_cells or self.nb.index(self.nb.select()) != 2:
            return
        for i, (rect, txt) in enumerate(self._mon_cells):
            v = data[i]
            g = 30 + v * 150 // 255
            self.mon.itemconfigure(rect, fill=f"#{g:02x}{g:02x}{g:02x}" if v else "#1e1e1e")
            self.mon.itemconfigure(txt, text=str(v) if v else "")

    # ---- log tab -----------------------------------------------------------

    def _build_log_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Log")
        self.log_text = ScrolledText(tab, height=10, state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    def _log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---- about tab ---------------------------------------------------------

    ABOUT_OVERVIEW = (
        f"{APP_NAME} lets a lighting console control Nanlite lights. It receives Art-Net from the "
        "network and sends the light commands over Bluetooth to a NANLINK WS-TB-1 transmitter box, "
        "which relays them over 2.4 GHz to every light set to a Nanlink channel.",
        "Patch each light with its Nanlink channel and a DMX start address, and the console runs it "
        "like any other fixture. When no Art-Net arrives, the faders on the Fixtures & Control tab "
        "take over. Settings holds the box pairing, the Art-Net universe and the output rate.",
    )
    CREDITS_LINKS = (
        ("Email", "info@oddlux.lighting", "mailto:info@oddlux.lighting"),
        ("GitHub", "OddLux", "https://github.com/OddLux"),
    )

    def _build_about_tab(self):
        tab = ttk.Frame(self.nb, padding=16)
        self.nb.add(tab, text="About")
        wrapped = []

        ttk.Label(tab, text=APP_NAME, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(tab, text=f"Version {__version__} · Art-Net to Nanlite over Bluetooth",
                  foreground="#555").pack(anchor="w", pady=(0, 12))
        for para in self.ABOUT_OVERVIEW:
            lbl = ttk.Label(tab, text=para, justify="left")
            lbl.pack(anchor="w", fill="x", pady=(0, 8))
            wrapped.append(lbl)
        ttk.Label(tab, text="Console ──Art-Net──▶ " + APP_NAME + " ──Bluetooth──▶ WS-TB-1 ──2.4 GHz──▶ lights",
                  font=("Consolas", 10)).pack(anchor="w", pady=(4, 16))

        credits = ttk.LabelFrame(tab, text="Credits", padding=10)
        credits.pack(anchor="w", fill="x")
        r = 0
        for r, (label, text, url) in enumerate(self.CREDITS_LINKS):
            ttk.Label(credits, text=label).grid(row=r, column=0, sticky="w", padx=(0, 12), pady=1)
            link = ttk.Label(credits, text=text, foreground="#1a5fb4", cursor="hand2",
                             font=("Segoe UI", 9, "underline"))
            link.grid(row=r, column=1, sticky="w", pady=1)
            link.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
        ttk.Label(credits, text="Developed by OddLux", font=("Segoe UI", 11, "bold")).grid(
            row=r + 1, column=0, columnspan=2, sticky="w", pady=(10, 0))

        tab.bind("<Configure>", lambda e: [w.config(wraplength=max(200, e.width - 40)) for w in wrapped])

    # ---- run control & events ----------------------------------------------

    def _toggle_run(self):
        if self.status.get("running"):
            self.runner.submit(self.bridge.stop())
        else:
            self._start()

    def _start(self):
        self.runner.submit(self.bridge.start())

    def _poll_events(self):
        latest_status = None
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "status":
                    latest_status = data
                elif kind == "log":
                    self._log(data)
        except queue.Empty:
            pass
        if latest_status:
            self._apply_status(latest_status)
        self.after(50, self._poll_events)

    def _apply_status(self, s: dict):
        self.status = s
        self.run_btn.config(text="Stop" if s["running"] else "Start")
        link = s["link"] if s["running"] else "stopped"
        self._dot(self.ble_dot, STATE_COLORS.get(link, "#888"))
        self.ble_lbl.config(text=f"Bluetooth: {link}")
        if not s["running"]:
            self._dot(self.art_dot, "#888")
            self.art_lbl.config(text="Art-Net: stopped")
        elif s["artnet_live"]:
            self._dot(self.art_dot, "#2e9d48")
            self.art_lbl.config(text=f"Art-Net: receiving from {s['artnet_source']}")
        else:
            self._dot(self.art_dot, "#d9a400" if s["artnet_other"] else "#888")
            other = s["artnet_other"]
            self.art_lbl.config(text="Art-Net: no data" + (
                f" (seen universe {', '.join(universe_label(u) for u in other[:2])})" if other else ""))
        self.rate_lbl.config(text=f"{s['rate']} msg/s · {s['pending']} queued")

        manual = s["control"] == "manual"
        if s["manual_only"]:
            self.control_lbl.config(text="Manual only: the console is ignored. Faders control the lights.")
        elif manual:
            self.control_lbl.config(text="No Art-Net from the console - the faders control the lights. "
                                    "Art-Net takes over again as soon as it arrives.")
        else:
            self.control_lbl.config(text="The console is in control (faders follow it). Tick 'Manual only' "
                                    "to take over.")

        self._setting_faders = True
        try:
            for i, (dmx, sent) in enumerate(s["fixtures"]):
                if i >= len(self._fader_rows):
                    break
                p = PERSONALITIES.get(self.cfg.fixtures[i].personality)
                targets = p.map(dmx, {**dict(p.default_options), **(self.cfg.fixtures[i].options or {})}) \
                    if p else {}
                params = list(targets)
                for slot, w in enumerate(self._fader_rows[i]):
                    w["scale"].state(["!disabled"] if manual else ["disabled"])
                    if (i, slot) not in self._dragging and slot < len(dmx) and int(w["var"].get()) != dmx[slot]:
                        w["var"].set(dmx[slot])
                    if slot < len(params):
                        param = params[slot]
                        w["label"].config(text=f"{dmx[slot]:3d} → {describe_value(param, targets[param][1])}")
                out = ", ".join(f"{k} {describe_value(k, v)}" for k, v in sent.items())
                if self.tree.exists(str(i)):
                    self.tree.set(str(i), "out", out or "-")
        finally:
            self._setting_faders = False
        self._update_monitor(s["universe_data"])
        info = (f"Universe {universe_label(self.cfg.artnet.universe)} · {s['artnet_frames']} frames received"
                + (f" from {s['artnet_source']}" if s["artnet_source"] else "")
                + " · blue outline = patched")
        self.mon_info.config(text=info)

    def _on_close(self):
        try:
            self.runner.submit(self.bridge.stop()).result(timeout=4)
        except Exception:
            pass
        self.destroy()


def main():
    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens instead of bitmap-stretched
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    App().mainloop()
