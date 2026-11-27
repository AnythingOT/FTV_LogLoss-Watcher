"""
LogLoss Watcher - for FTView SE DataLog Models

  python logloss_watcher.py              normal start
  python logloss_watcher.py --minimized  start hidden in the tray (used by "Start with Windows")
  python logloss_watcher.py --simulate   no real PLC: tag values are set by hand in the PLCs tab

Needs: Python 3.8 or newer (Tkinter included) and pylogix (pip install --user pylogix).
Keep logloss_watcher.py, llw_core.py and llw_tray.py in the same folder.
PLC access is READ-ONLY. Default setup PIN: 1234 (change it in Settings > Security).
"""
import csv, os, queue, re, sys, threading, time
from datetime import datetime, timedelta, date
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

import llw_core as core
import llw_tray

APP = "LogLoss Watcher"
SUBTITLE = "for FTView SE DataLog Models"
VERSION = "2.0.0"
FROZEN = bool(getattr(sys, "frozen", False))           # True when running as the built .exe
# Config and history live next to the .exe (or the .py). Never next to __file__ in a frozen build:
# in a one-file .exe that is a temporary folder that Windows deletes when the program closes.
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CFG_PATH = APP_DIR / "logloss_watcher_config.json"
OLD_CFG = APP_DIR / "dl_watchdog_config.json"
DATA_DIR = APP_DIR / "history"
HEARTBEAT = DATA_DIR / "heartbeat.txt"
MAX_HISTORY = 5000

BRAND = "AnythingOT"
# AnythingOT look (same palette as FTV_Diagnostics-Filter and FTV_DLP-Reporting-Tool):
# navy header, orange accent, light grey background. Status colours below are NOT branding:
# they mean OK / critical / warning and stay as they are.
FONT = "Segoe UI"
NAVY, NAVY_2, NAVY_3 = "#0F1B2D", "#1C2B42", "#2A3B55"
ACCENT, ACCENT_DARK = "#F28C28", "#D9771A"
BG, CARD, BORDER = "#F3F5F8", "#FFFFFF", "#DDE2E9"
TEXT, MUTED, HEAD_MUTED = "#1F2937", "#6B7280", "#9FB0C8"
SELECT, BUTTON, BUTTON_HOVER, FOOTER = "#FDE7D1", "#EEF1F5", "#E2E7EE", "#E9EDF2"
C_OK, C_BAD, C_WARN, C_GREY, C_INFO = "#2e7d32", "#c62828", "#ef6c00", "#757575", "#1565c0"
STATE_BG = {"Started": "#e3f4e3", "Stopped": "#ffffff", "Critical": "#ffd6d6", "Warning": "#ffe7c2",
            "Unknown": "#e4e4e4",
            "Paused": "#f2f2f2", "Disabled": "#f7f7f7", "waiting": "#ffffff"}
LEVEL_BG = {"ALARM": "#ffd6d6", "WARN": "#ffe7c2", "OK": "#e3f4e3", "INFO": "#ffffff"}
LEVEL_NAME = {"ALARM": "Critical", "WARN": "Warning", "OK": "OK", "INFO": "Info"}
TYPE_HELP = {
    core.T_MACRO: "Started by the startup macro and always running while the HMI runs. Issue if the data "
                  "set stops growing. Paused while the SE client is not running (Settings).",
    core.T_EXPR: "Started/stopped by FactoryTalk events. Paste the event's ON expression; it is evaluated "
                 "with live PLC values (Trigger column) and compared with the growth of the files.",
}


# =====================================================================================
# helpers
# =====================================================================================
class ToolTip:
    def __init__(self, widget, text):
        self.w, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _e=None):
        if self.tip:
            return
        self.tip = tk.Toplevel(self.w)
        self.tip.overrideredirect(True)
        self.tip.geometry("+{}+{}".format(self.w.winfo_rootx() + 12,
                                          self.w.winfo_rooty() + self.w.winfo_height() + 4))
        tk.Label(self.tip, text=self.text, bg=NAVY, fg="white", bd=0, justify="left",
                 font=(FONT, 9), wraplength=380, padx=10, pady=6).pack()

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def _sort_key(v):
    """Numbers sort as numbers ('12 KB', '3 min ago', '45.2'), everything else as text."""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*(KB|MB|s ago|min ago|h ago)?\s*$", str(v))
    if m:
        n = float(m.group(1))
        n *= {"MB": 1024, "min ago": 60, "h ago": 3600}.get(m.group(2), 1)
        return (0, n, "")
    return (1, 0, str(v).lower())


def sort_tree(t, col=None, toggle=True):
    """Sort a Treeview by a column (click the header; click again to reverse)."""
    if col is None:
        if not getattr(t, "_sort", None):
            return
        col, desc = t._sort
    else:
        old = getattr(t, "_sort", None)
        desc = (not old[1]) if (toggle and old and old[0] == col) else False
    t._sort = (col, desc)
    items = [(t.set(i, col), i) for i in t.get_children("")]
    items.sort(key=lambda x: _sort_key(x[0]), reverse=desc)
    for pos, (_, i) in enumerate(items):
        t.move(i, "", pos)
    for c in t["columns"]:
        name = c + ((" ▼" if desc else " ▲") if c == col else "")
        t.heading(c, text=name)


def make_tree(parent, cols, widths, height=12, stretch_col=None, select="browse"):
    f = ttk.Frame(parent)
    t = ttk.Treeview(f, columns=cols, show="headings", height=height, selectmode=select)
    t._sort = None
    for c, w in zip(cols, widths):
        t.heading(c, text=c, anchor="w", command=lambda c=c: sort_tree(t, c))
        t.column(c, width=w, minwidth=40, anchor="w", stretch=(c == stretch_col))
    sb = ttk.Scrollbar(f, orient="vertical", command=t.yview)
    t.configure(yscrollcommand=sb.set)
    t.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return f, t


def toolbar(parent, buttons, side="left"):
    f = ttk.Frame(parent)
    for txt, cmd, tip in buttons:
        b = ttk.Button(f, text=txt, command=cmd)
        b.pack(side=side, padx=(0, 4))
        if tip:
            ToolTip(b, tip)
    return f


def hms(sec):
    sec = int(sec)
    return "{}:{:02d}".format(sec // 3600, (sec % 3600) // 60)


class FormDialog(simpledialog.Dialog):
    def __init__(self, parent, title, fields, note=""):
        self.fields, self.note, self.result = fields, note, None
        super().__init__(parent, title)

    def body(self, master):
        self.entries = []
        for i, (lab, val) in enumerate(self.fields):
            ttk.Label(master, text=lab).grid(row=i, column=0, sticky="w", padx=6, pady=4)
            e = ttk.Entry(master, width=38)
            e.insert(0, str(val))
            e.grid(row=i, column=1, padx=6, pady=4)
            self.entries.append(e)
        if self.note:
            ttk.Label(master, text=self.note, foreground=C_GREY, wraplength=360, justify="left"
                      ).grid(row=len(self.fields), column=0, columnspan=2, sticky="w", padx=6, pady=(4, 0))
        return self.entries[0]

    def apply(self):
        self.result = [e.get().strip() for e in self.entries]


class PickDialog(simpledialog.Dialog):
    def __init__(self, parent, title, items):
        self.items, self.result = items, None
        super().__init__(parent, title)

    def body(self, master):
        ttk.Label(master, text="Filter:").grid(row=0, column=0, sticky="w", padx=4)
        self.fv = tk.StringVar()
        self.fv.trace_add("write", lambda *a: self._fill())
        e = ttk.Entry(master, textvariable=self.fv, width=56)
        e.grid(row=0, column=1, sticky="we", pady=4)
        self.lb = tk.Listbox(master, selectmode="extended", width=76, height=22, activestyle="none",
                             selectbackground=SELECT, selectforeground=TEXT, highlightcolor=ACCENT,
                             highlightbackground=BORDER, relief="flat", bd=0)
        sb = ttk.Scrollbar(master, command=self.lb.yview)
        self.lb.config(yscrollcommand=sb.set)
        self.lb.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=4)
        sb.grid(row=1, column=2, sticky="ns")
        self.cnt = ttk.Label(master, text="", foreground=C_GREY)
        self.cnt.grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=4)
        self._fill()
        return e

    def _fill(self):
        f = self.fv.get().lower()
        self.shown = [i for i in self.items if f in i.lower()][:5000]
        self.lb.delete(0, "end")
        for i in self.shown:
            self.lb.insert("end", i)
        self.cnt.config(text="{} of {} tags. Ctrl/Shift+click to select several. Members of UDTs may "
                             "have to be added by hand (Add tag...).".format(len(self.shown), len(self.items)))

    def apply(self):
        self.result = [self.shown[i] for i in self.lb.curselection()]


# =====================================================================================
# Popup banner
# =====================================================================================
class Banner:
    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=C_BAD, highlightthickness=2, highlightbackground="#7f0000")
        f = tk.Frame(self.win, bg=C_BAD)
        f.pack(fill="both", expand=True)
        self.head = tk.Label(f, text="", fg="white", bg=C_BAD, anchor="w",
                             font=("Segoe UI", 12, "bold"), padx=12, pady=6)
        self.head.grid(row=0, column=0, sticky="w")
        self.label = tk.Label(f, text="", fg="white", bg=C_BAD, justify="left", anchor="w",
                              font=("Segoe UI", 10, "bold"), padx=14)
        self.label.grid(row=1, column=0, sticky="w")
        self.foot = tk.Label(f, text="Check the datalog path and call support", fg="#ffdede", bg=C_BAD,
                             anchor="w", font=("Segoe UI", 9), padx=14, pady=4)
        self.foot.grid(row=2, column=0, sticky="w")
        for w in (f, self.head, self.label, self.foot):         # drag anywhere on the popup to move it
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.configure(cursor="fleur")
        self.dragged = False
        bf = tk.Frame(f, bg=C_BAD, cursor="arrow")
        bf.grid(row=0, column=1, rowspan=3, padx=10, pady=8, sticky="ns")
        self.snooze_btn = tk.Button(bf, text="Snooze", takefocus=0, width=12, command=self.snooze, cursor="hand2")
        self.snooze_btn.pack(pady=2)
        tk.Button(bf, text="Open", takefocus=0, width=12, command=app.show_window, cursor="hand2").pack(pady=2)
        self.win.withdraw()
        self.visible = False
        self.snooze_until = 0
        self.test_until = 0
        self.lines = []
        self.items = []
        self.last_text = None
        self.last_geo = None
        self.preview_pos = None
        self.win.update_idletasks()
        self._no_activate()

    def _no_activate(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id())
            st = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, st | 0x08000000 | 0x00000080)
        except Exception:
            pass

    def _work_area(self):
        """Work area (screen minus taskbar) of the monitor that shows the watcher window."""
        try:
            import ctypes
            from ctypes import wintypes as W
            u32 = ctypes.windll.user32

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]
            u32.GetParent.restype = W.HWND
            u32.GetParent.argtypes = [W.HWND]
            u32.MonitorFromWindow.restype = W.HANDLE
            u32.MonitorFromWindow.argtypes = [W.HWND, W.DWORD]
            u32.GetMonitorInfoW.argtypes = [W.HANDLE, ctypes.POINTER(MONITORINFO)]
            hwnd = u32.GetParent(self.app.root.winfo_id()) or self.app.root.winfo_id()
            mon = u32.MonitorFromWindow(hwnd, 2)                  # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if mon and u32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                r = mi.rcWork
                return r.left, r.top, r.right, r.bottom
            r = W.RECT()
            u32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # primary work area
            return r.left, r.top, r.right, r.bottom
        except Exception:
            return 0, 0, self.win.winfo_screenwidth(), self.win.winfo_screenheight() - 48

    def position(self):
        """Position to use: the value selected in Settings (even before 'Apply') while a test runs,
        otherwise the saved setting."""
        if time.time() < self.test_until and self.preview_pos:
            return self.preview_pos
        return self.app.cfg["settings"].get("banner_pos", "bottom-left")

    def _drag_start(self, e):
        self._dx = e.x_root - self.win.winfo_x()
        self._dy = e.y_root - self.win.winfo_y()

    def _drag_move(self, e):
        """Temporary position: kept while this popup is open, back to the Settings position next time."""
        self.dragged = True
        geo = "+{}+{}".format(e.x_root - self._dx, e.y_root - self._dy)
        self.win.geometry(geo)
        self.last_geo = geo

    def _place(self):
        if self.dragged and self.visible:
            return                                    # user moved it: leave it there while it is open
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        L, T, R, B = self._work_area()
        pos = self.position()
        x = L + 6 if "left" in pos else R - w - 6
        y = B - h - 6 if "bottom" in pos else T + 6
        geo = "+{}+{}".format(int(x), int(y))
        if geo != self.last_geo:
            self.win.geometry(geo)
            self.last_geo = geo

    def update(self, items):
        """items: list of (text, data log name). Called every poll. Keeps a running test visible."""
        now = time.time()
        if now < self.test_until:
            return
        self.items = items
        if not items or now < self.snooze_until:
            self.hide()
            return
        self._show([t for t, _ in items], "⚠  DATALOG CRITICAL")

    def test(self, pos=None):
        self.preview_pos = pos
        self.test_until = time.time() + 15
        self.dragged = False
        self._show(["Example_Datalog_1 has not started (TEST)",
                    "Example_Datalog_2 is not growing in size (TEST)"], "⚠  DATALOG CRITICAL - TEST")

    def _show(self, lines, head):
        self.lines = lines
        text = "\n".join("•  " + l for l in lines)
        self.snooze_btn.config(text="Snooze {:g} min".format(self.app.cfg["settings"]["snooze_min"]))
        if text + head != self.last_text:
            self.head.config(text=head)
            self.label.config(text=text)
            self.last_text = text + head
        self._place()                                   # every time: follows setting / size changes
        if not self.visible:
            self.win.deiconify()
            self.visible = True
            self.last_geo = None
            self._place()                               # Windows: re-apply after the window is mapped
        self.win.attributes("-topmost", True)
        self.win.lift()

    def hide(self):
        if self.visible:
            self.win.withdraw()
            self.visible = False
        self.dragged = False

    def snooze(self):
        if time.time() < self.test_until:
            self.test_until = 0
            self.hide()
            return
        m = self.app.cfg["settings"]["snooze_min"]
        self.snooze_until = time.time() + m * 60
        until = datetime.fromtimestamp(self.snooze_until).strftime("%H:%M")
        for text, dl in (self.items or [(l, "-") for l in self.lines]):
            self.app.add_event("INFO", core.CAT_SNOOZE, dl, "Popup snoozed for {:g} min (until {}) by {}".format(
                m, until, core.windows_user()), "issue: " + text)
        self.hide()


# =====================================================================================
# Export dialog
# =====================================================================================
class ExportDialog(simpledialog.Dialog):
    PERIODS = ["Today", "Last 24 hours", "Last 7 days", "Last 30 days", "Custom"]

    def __init__(self, app):
        self.app, self.result = app, None
        super().__init__(app.root, "Export")

    def body(self, m):
        r = 0
        ttk.Label(m, text="Content", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=3)
        self.v_content = tk.StringVar(value="history")
        cf = ttk.Frame(m)
        cf.grid(row=r, column=1, columnspan=3, sticky="w")
        ttk.Radiobutton(cf, text="History", value="history", variable=self.v_content).pack(side="left")
        ttk.Radiobutton(cf, text="Coverage report (per data log per day)", value="coverage",
                        variable=self.v_content).pack(side="left", padx=12)
        r += 1
        ttk.Label(m, text="Period", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=3)
        self.v_period = tk.StringVar(value="Last 7 days")
        cb = ttk.Combobox(m, textvariable=self.v_period, values=self.PERIODS, state="readonly", width=16)
        cb.grid(row=r, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda e: self._period())
        r += 1
        ttk.Label(m, text="From / To").grid(row=r, column=0, sticky="w")
        self.v_from, self.v_to = tk.StringVar(), tk.StringVar()
        self.e_from = ttk.Entry(m, textvariable=self.v_from, width=17)
        self.e_from.grid(row=r, column=1, sticky="w")
        self.e_to = ttk.Entry(m, textvariable=self.v_to, width=17)
        self.e_to.grid(row=r, column=2, sticky="w", padx=6)
        ttk.Label(m, text="YYYY-MM-DD HH:MM (local time)", foreground=C_GREY).grid(row=r, column=3, sticky="w")
        r += 1
        ttk.Label(m, text="Severity", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=(8, 3))
        sf = ttk.Frame(m)
        sf.grid(row=r, column=1, columnspan=3, sticky="w", pady=(8, 3))
        self.v_lv = {}
        for k in ("ALARM", "WARN", "OK", "INFO"):
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(sf, text=LEVEL_NAME[k], variable=v).pack(side="left", padx=(0, 10))
            self.v_lv[k] = v
        r += 1
        ttk.Label(m, text="Category", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="nw", pady=3)
        cfm = ttk.Frame(m)
        cfm.grid(row=r, column=1, columnspan=3, sticky="w")
        self.v_cat = {}
        for i, c in enumerate(core.CATEGORIES):
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(cfm, text=c, variable=v).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 10))
            self.v_cat[c] = v
        r += 1
        ttk.Label(m, text="Data logs", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="nw", pady=3)
        self.lb = tk.Listbox(m, selectmode="extended", height=8, width=40, exportselection=False)
        self.dls = ["- (general / watcher)"] + [d["name"] for d in self.app.cfg["datalogs"]] + \
                   ["PLC " + p["topic"] for p in self.app.cfg["plcs"]]
        for n in self.dls:
            self.lb.insert("end", n)
        self.lb.selection_set(0, "end")
        self.lb.grid(row=r, column=1, columnspan=2, sticky="w", pady=3)
        bf = ttk.Frame(m)
        bf.grid(row=r, column=3, sticky="nw")
        ttk.Button(bf, text="All", command=lambda: self.lb.selection_set(0, "end")).pack(fill="x")
        ttk.Button(bf, text="None", command=lambda: self.lb.selection_clear(0, "end")).pack(fill="x", pady=2)
        r += 1
        ttk.Label(m, text="Text search").grid(row=r, column=0, sticky="w", pady=3)
        self.v_search = tk.StringVar()
        ttk.Entry(m, textvariable=self.v_search, width=30).grid(row=r, column=1, columnspan=2, sticky="w")
        r += 1
        ttk.Label(m, text="Format", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=(8, 3))
        ff = ttk.Frame(m)
        ff.grid(row=r, column=1, columnspan=3, sticky="w", pady=(8, 3))
        self.v_fmt = tk.StringVar(value="txt")
        ttk.Radiobutton(ff, text="TXT (readable)", value="txt", variable=self.v_fmt).pack(side="left")
        ttk.Radiobutton(ff, text="CSV (Excel)", value="csv", variable=self.v_fmt).pack(side="left", padx=12)
        ttk.Label(m, text="Severity, category and search apply to History. Coverage uses period and "
                          "data logs.", foreground=C_GREY).grid(row=r + 1, column=0, columnspan=4, sticky="w",
                                                                 pady=(6, 0))
        self._period()
        return cb

    def _period(self):
        now = datetime.now()
        p = self.v_period.get()
        start = {"Today": now.replace(hour=0, minute=0, second=0, microsecond=0),
                 "Last 24 hours": now - timedelta(hours=24), "Last 7 days": now - timedelta(days=7),
                 "Last 30 days": now - timedelta(days=30)}.get(p)
        st = "normal" if p == "Custom" else "disabled"
        self.e_from.config(state="normal")
        self.e_to.config(state="normal")
        if start is not None:
            self.v_from.set(start.strftime("%Y-%m-%d %H:%M"))
            self.v_to.set(now.strftime("%Y-%m-%d %H:%M"))
        self.e_from.config(state=st)
        self.e_to.config(state=st)

    def validate(self):
        try:
            self.t_from = datetime.strptime(self.v_from.get().strip(), "%Y-%m-%d %H:%M")
            self.t_to = datetime.strptime(self.v_to.get().strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            messagebox.showerror(APP, "Dates must look like 2026-11-26 14:30", parent=self)
            return False
        if self.t_to < self.t_from:
            messagebox.showerror(APP, "'To' is before 'From'.", parent=self)
            return False
        if not self.lb.curselection():
            messagebox.showerror(APP, "Select at least one data log.", parent=self)
            return False
        return True

    def apply(self):
        sel = [self.dls[i] for i in self.lb.curselection()]
        self.result = {"content": self.v_content.get(), "from": self.t_from, "to": self.t_to,
                       "levels": {k for k, v in self.v_lv.items() if v.get()},
                       "cats": {k for k, v in self.v_cat.items() if v.get()},
                       "dls": {("-" if s.startswith("- ") else s) for s in sel},
                       "search": self.v_search.get().strip().lower(), "fmt": self.v_fmt.get(),
                       "period": self.v_period.get()}


class IncidentDialog(simpledialog.Dialog):
    """Incident report: everything the watcher knows about a time window, in one TXT."""

    def __init__(self, app, when):
        self.app, self.when, self.result = app, when, None
        super().__init__(app.root, "Incident report")

    def body(self, m):
        ttk.Label(m, text="One TXT file with the timeline, data set files, PC / service restarts and coverage "
                          "around an incident.\nSelect a Critical entry in History first to use its time.",
                  foreground=C_GREY, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(m, text="Incident time").grid(row=1, column=0, sticky="w")
        self.v_when = tk.StringVar(value=self.when.strftime("%Y-%m-%d %H:%M"))
        e = ttk.Entry(m, textvariable=self.v_when, width=17)
        e.grid(row=1, column=1, sticky="w")
        ttk.Label(m, text="YYYY-MM-DD HH:MM (local time)", foreground=C_GREY).grid(row=1, column=2, sticky="w",
                                                                                   padx=6)
        ttk.Label(m, text="Minutes before").grid(row=2, column=0, sticky="w", pady=3)
        self.v_before = tk.StringVar(value="30")
        ttk.Entry(m, textvariable=self.v_before, width=7).grid(row=2, column=1, sticky="w")
        ttk.Label(m, text="Minutes after").grid(row=3, column=0, sticky="w")
        self.v_after = tk.StringVar(value="30")
        ttk.Entry(m, textvariable=self.v_after, width=7).grid(row=3, column=1, sticky="w")
        self.v_expr = tk.BooleanVar(value=False)
        ttk.Checkbutton(m, text="Include trigger expressions (contain PLC tag names)",
                        variable=self.v_expr).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        return e

    def validate(self):
        try:
            self.t_when = datetime.strptime(self.v_when.get().strip(), "%Y-%m-%d %H:%M")
            self.before = float(self.v_before.get())
            self.after = float(self.v_after.get())
            if not (0 <= self.before <= 10080 and 0 <= self.after <= 10080):
                raise ValueError
        except ValueError:
            messagebox.showerror(APP, "Time must look like 2026-11-26 14:30 and minutes must be 0 to 10080 "
                                      "(7 days).", parent=self)
            return False
        return True

    def apply(self):
        self.result = {"when": self.t_when, "from": self.t_when - timedelta(minutes=self.before),
                       "to": self.t_when + timedelta(minutes=self.after), "expr": self.v_expr.get()}


# =====================================================================================
# Main application
# =====================================================================================
class App:
    def __init__(self, simulate=False, minimized=False):
        self.simulate = simulate
        core.set_low_priority()
        self.root = tk.Tk()
        self.root.title("{} {}".format(APP, VERSION) + ("  -  SIMULATION" if simulate else ""))
        self.root.geometry("1280x740")
        self.root.minsize(1000, 600)
        DATA_DIR.mkdir(exist_ok=True)
        self.icons = llw_tray.write_icons(str(DATA_DIR))
        try:
            self.root.iconbitmap(default=self.icons["ok"])
        except Exception:
            pass
        self._style()
        self.ui_q, self.ev_q = queue.Queue(), queue.Queue()
        self.history = []
        self.hidden = False
        self.closing = False
        self.purge_day = None
        self.last_beat = 0

        startup_notes = []
        self.cfg = None
        src = CFG_PATH if CFG_PATH.exists() else (OLD_CFG if OLD_CFG.exists() else None)
        if src:
            try:
                self.cfg, notes = core.load_config(src)
                startup_notes += notes
                if src == OLD_CFG:
                    startup_notes.append("Configuration taken over from DL Watchdog ({})".format(OLD_CFG.name))
            except Exception as e:
                messagebox.showerror(APP, "Configuration file is damaged:\n{}\n\nStarting empty.".format(e))
        if self.cfg is None:
            self.cfg = core.new_config()
        core.save_config(self.cfg, CFG_PATH)

        self.monitor = core.Monitor(self.cfg, simulate=simulate, emit=self.ev_q.put, data_dir=DATA_DIR)
        self.running = threading.Event()
        self.quit_flag = threading.Event()
        self.wake = threading.Event()
        self.locked = True
        self.last_activity = time.time()
        self.cur_dl = None
        self.last_check = None

        self._build_menu()
        self._build_header()
        self._build_statusbar()
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        self._build_status()
        self._build_datalogs()
        self._build_plcs()
        self._build_settings()
        self._build_history()
        self.banner = Banner(self)
        self._load_history()
        for seq in ("<Any-KeyPress>", "<Any-ButtonPress>", "<MouseWheel>", "<Motion>"):
            self.root.bind_all(seq, self._activity, add="+")
        self.root.bind("<Control-s>", lambda e: self._ctrl_s())
        self.root.bind("<Unmap>", self._on_unmap)
        self.set_lock(True, quiet=True)
        self.refresh_all()

        self.tray = llw_tray.Tray(self.icons, on_open=lambda: self.ui_q.put(self.show_window),
                                  on_menu=lambda c: self.ui_q.put(lambda: self._tray_cmd(c)),
                                  tooltip=APP)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        threading.Thread(target=self._worker, daemon=True).start()

        self.add_event("INFO", core.CAT_WATCHER, "-", "{} {} started by {}".format(
            APP, VERSION, core.windows_user()) + (" (simulation)" if simulate else ""))
        self._check_downtime()
        for n in startup_notes:
            self.add_event("WARN", core.CAT_SETUP, "-", n)
        if not core.HAVE_PYLOGIX and not simulate:
            self.add_event("WARN", core.CAT_WATCHER, "-", "pylogix is not installed - PLC tags cannot be read",
                           "Install with:  pip install --user pylogix")
        if not self.tray.ok:
            self.add_event("INFO", core.CAT_WATCHER, "-", "Tray icon not available - window minimizes instead")
        if not self.cfg["settings"]["log_root"]:
            self.add_event("WARN", core.CAT_SETUP, "-", "Datalog folder not set", "Unlock setup, then Settings")
        self.start_monitoring()                # always ON; the runtime gate decides when checks begin
        self.root.after(300, self._poll)
        if minimized:
            self.root.after(400, self.hide_window)

    # ---------------------------------------------------------------- look & feel
    def _style(self):
        """AnythingOT look on Tk's 'clam' theme (no extra package; same as the other AnythingOT tools)."""
        r = self.root
        r.configure(bg=BG)
        r.option_add("*Font", (FONT, 9))
        # dialogs (PIN, export, incident, pickers) are classic Tk frames: same grey as the window.
        # Widgets that set their own colour (alarm banner, header) are not affected.
        r.option_add("*Toplevel.background", BG)
        r.option_add("*Frame.background", BG)
        st = ttk.Style(r)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=TEXT, font=(FONT, 9), bordercolor=BORDER,
                     lightcolor=BORDER, darkcolor=BORDER, focuscolor=ACCENT, troughcolor=BG,
                     selectbackground=SELECT, selectforeground=TEXT)
        # labels and frames
        st.configure("Hint.TLabel", foreground=MUTED)
        st.configure("Big.TLabel", font=(FONT, 11, "bold"))
        st.configure("Footer.TLabel", background=FOOTER, foreground=TEXT)
        st.configure("TLabelframe", background=BG, bordercolor=BORDER, lightcolor=BG, darkcolor=BG)
        st.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=(FONT, 9, "bold"))
        st.configure("Section.TLabelframe", background=BG, bordercolor=BORDER)
        st.configure("Section.TLabelframe.Label", background=BG, foreground=MUTED, font=(FONT, 9, "bold"))
        st.configure("TSeparator", background=BORDER)
        # buttons
        st.configure("TButton", padding=(10, 4), background=BUTTON, relief="flat", anchor="center")
        st.map("TButton", background=[("disabled", "#F3F4F6"), ("pressed", "#D5DBE3"), ("active", BUTTON_HOVER)],
               foreground=[("disabled", "#A0A7B4")])
        st.configure("Accent.TButton", background=ACCENT, foreground="white", font=(FONT, 9, "bold"),
                     bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
        st.map("Accent.TButton",
               background=[("disabled", "#E5C3A1"), ("pressed", ACCENT_DARK), ("active", ACCENT_DARK)],
               foreground=[("disabled", "white")],
               bordercolor=[("disabled", "#E5C3A1"), ("active", ACCENT_DARK)],
               lightcolor=[("disabled", "#E5C3A1"), ("active", ACCENT_DARK)],
               darkcolor=[("disabled", "#E5C3A1"), ("active", ACCENT_DARK)])
        st.configure("Footer.TButton", background=FOOTER, foreground=ACCENT_DARK, padding=(8, 2),
                     bordercolor=FOOTER, lightcolor=FOOTER, darkcolor=FOOTER)
        st.map("Footer.TButton", background=[("active", "#DDE3EA")], foreground=[("disabled", "#A0A7B4")])
        # check boxes and radio buttons
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, background=BG, indicatorbackground="white", indicatorforeground="white",
                         upperbordercolor="#B8C0CC", lowerbordercolor="#B8C0CC")
            st.map(w, background=[("active", BG)],
                   indicatorbackground=[("selected", ACCENT), ("pressed", SELECT)],
                   upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])
        # entry fields
        for w in ("TEntry", "TSpinbox", "TCombobox"):
            st.configure(w, fieldbackground="white", padding=3, arrowcolor=MUTED, insertcolor=TEXT)
            st.map(w, bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
                   fieldbackground=[("readonly", "white"), ("disabled", "#F3F4F6")])
        st.map("TCombobox", selectbackground=[("readonly", "white")], selectforeground=[("readonly", TEXT)],
               bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
               fieldbackground=[("readonly", "white"), ("disabled", "#F3F4F6")])
        r.option_add("*TCombobox*Listbox.selectBackground", SELECT)
        r.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        # tables: row height from the font, so rows do not clip on high-DPI screens
        try:
            import tkinter.font as tkfont
            row_h = max(24, tkfont.nametofont("TkDefaultFont").metrics("linespace") + 8)
        except Exception:
            row_h = 24
        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=row_h,
                     bordercolor=BORDER)
        st.configure("Treeview.Heading", background=BUTTON, foreground=TEXT, font=(FONT, 9, "bold"),
                     relief="flat", padding=(6, 4))
        st.map("Treeview.Heading", background=[("active", BUTTON_HOVER)])
        st.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", TEXT)])
        for opt in ("foreground", "background"):     # Tk 8.6.9: row colours (status) need this
            try:
                st.map("Treeview", **{opt: [e for e in st.map("Treeview", query_opt=opt)
                                            if e[:2] != ("!disabled", "!selected")]})
            except Exception:
                pass
        # tabs
        st.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=BUTTON, foreground=MUTED, padding=(14, 5),
                     bordercolor=BORDER, lightcolor=BUTTON)
        st.map("TNotebook.Tab", background=[("selected", CARD), ("active", BUTTON_HOVER)],
               foreground=[("selected", TEXT)], lightcolor=[("selected", ACCENT)])
        # scroll bars, split bars, progress
        for o in ("Vertical", "Horizontal"):
            st.configure(o + ".TScrollbar", background="#D5DBE3", troughcolor=CARD, bordercolor=CARD,
                         lightcolor="#D5DBE3", darkcolor="#D5DBE3", arrowcolor=MUTED, gripcount=0)
            st.map(o + ".TScrollbar", background=[("active", "#B8C0CC")])
        st.configure("TPanedwindow", background=BG)
        st.configure("Sash", sashthickness=8, gripcount=0, background=BG, lightcolor=BG, bordercolor=BG)
        st.configure("TProgressbar", background=ACCENT, troughcolor="#E5E7EB", bordercolor="#E5E7EB",
                     lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)

    def _build_header(self):
        h = tk.Frame(self.root, bg=NAVY, height=64)
        h.pack(fill="x")
        h.pack_propagate(False)
        mark = tk.Frame(h, bg=NAVY)
        mark.pack(side="left", padx=(18, 0))
        tk.Label(mark, text="Anything", bg=NAVY, fg="white", font=(FONT, 17, "bold")).pack(side="left")
        tk.Label(mark, text="OT", bg=NAVY, fg=ACCENT, font=(FONT, 17, "bold")).pack(side="left")
        tk.Frame(h, bg=NAVY_2, width=1).pack(side="left", fill="y", padx=16, pady=14)
        title = tk.Frame(h, bg=NAVY)
        title.pack(side="left")
        tk.Label(title, text=APP, bg=NAVY, fg="white", font=(FONT, 13, "bold")).pack(anchor="w")
        sub = tk.Frame(title, bg=NAVY)
        sub.pack(anchor="w")
        tk.Label(sub, text=SUBTITLE, bg=NAVY, fg=HEAD_MUTED, font=(FONT, 9)).pack(side="left")
        if self.simulate:
            tk.Label(sub, text="SIMULATION", bg=ACCENT, fg="white", font=(FONT, 8, "bold"),
                     padx=6).pack(side="left", padx=(8, 0))
        self.badges = {}
        for key, title_text in (("lock", "Setup"), ("alarms", "Critical"), ("client", "SE client"),
                                ("engine", "Datalog service"), ("mon", "Monitoring")):
            f = tk.Frame(h, bg=NAVY)
            f.pack(side="right", padx=(0, 18) if key == "lock" else 6)
            tk.Label(f, text=title_text, bg=NAVY, fg=HEAD_MUTED, font=(FONT, 8)).pack(anchor="w")
            # badge colours are status (OK / critical / warning), not branding
            b = tk.Label(f, text="-", bg=C_GREY, fg="white", font=(FONT, 9, "bold"), padx=10, pady=2, width=11)
            b.pack()
            self.badges[key] = b
        self.badges["lock"].config(cursor="hand2")
        self.badges["lock"].bind("<Button-1>", lambda e: self.toggle_lock())

    def _badge(self, key, text, color):
        b = self.badges[key]
        if b.cget("text") != text or b.cget("bg") != color:
            b.config(text=text, bg=color)

    def _build_statusbar(self):
        sb = tk.Frame(self.root, bg=FOOTER, height=30)
        sb.pack(side="bottom", fill="x")
        sb.pack_propagate(False)
        self.sb_left = ttk.Label(sb, text="", padding=(12, 2), style="Footer.TLabel")
        self.sb_left.pack(side="left")
        self.busy_bar = ttk.Progressbar(sb, mode="indeterminate", length=120)
        self.busy_lbl = ttk.Label(sb, text="", foreground=C_INFO, padding=(6, 2), style="Footer.TLabel")
        self.jobs = {}
        tk.Label(sb, text="\u00a9 {}  \u00b7  {} v{}".format(BRAND, APP, VERSION), bg=FOOTER, fg=MUTED,
                 font=(FONT, 9)).pack(side="right", padx=12)
        self.sb_lockbtn = ttk.Button(sb, text="Unlock setup...", command=self.toggle_lock, style="Footer.TButton")
        self.sb_lockbtn.pack(side="right", padx=4, pady=2)
        self.sb_right = ttk.Label(sb, text="", padding=(8, 2), style="Footer.TLabel")
        self.sb_right.pack(side="right")

    def job_start(self, text):
        """Show the busy indicator while a PLC read / test / browse runs. Returns a job id."""
        jid = object()
        self.jobs[jid] = text
        self._jobs_show()
        return jid

    def job_end(self, jid):
        self.jobs.pop(jid, None)
        self._jobs_show()

    def _jobs_show(self):
        if self.jobs:
            self.busy_lbl.config(text=list(self.jobs.values())[-1] + ("  (+{})".format(len(self.jobs) - 1)
                                                                       if len(self.jobs) > 1 else ""))
            if not self.busy_bar.winfo_ismapped():
                self.busy_bar.pack(side="left", padx=(12, 0), pady=2)
                self.busy_lbl.pack(side="left")
                self.busy_bar.start(12)
            self.root.config(cursor="watch")
        else:
            self.busy_bar.stop()
            self.busy_bar.pack_forget()
            self.busy_lbl.pack_forget()
            self.root.config(cursor="")

    # ---------------------------------------------------------------- menu, lock, window
    def _build_menu(self):
        m = tk.Menu(self.root)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="Save configuration", command=self.save_cfg, accelerator="Ctrl+S")
        fm.add_command(label="Import configuration...", command=self.import_cfg)
        fm.add_command(label="Export configuration...", command=self.export_cfg)
        fm.add_separator()
        fm.add_command(label="Export history / coverage...", command=self.export_dialog)
        fm.add_command(label="Incident report...", command=self.incident_report)
        fm.add_command(label="Open program folder", command=lambda: self._open(APP_DIR))
        fm.add_separator()
        fm.add_command(label="Hide to tray", command=self.hide_window)
        fm.add_command(label="Exit...", command=self.exit_app)
        m.add_cascade(label="File", menu=fm)
        mm = tk.Menu(m, tearoff=0)
        mm.add_command(label="Start monitoring", command=self.start_monitoring)
        mm.add_command(label="Stop monitoring...", command=self.stop_monitoring)
        mm.add_separator()
        mm.add_command(label="Test popup", command=self.test_banner)
        m.add_cascade(label="Monitoring", menu=mm)
        sm = tk.Menu(m, tearoff=0)
        sm.add_command(label="Unlock setup...", command=self.unlock)
        sm.add_command(label="Lock setup", command=lambda: self.set_lock(True))
        sm.add_command(label="Change PIN...", command=self.change_pin)
        m.add_cascade(label="Setup", menu=sm)
        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label="About", command=lambda: messagebox.showinfo(
            APP, "{} {}\n{}\n\nPLC access is read-only (pylogix).\nRuns at below-normal priority.\n\n"
                 "Configuration:\n{}\n\nHistory:\n{}".format(APP, VERSION, SUBTITLE, CFG_PATH, DATA_DIR)))
        m.add_cascade(label="Help", menu=hm)
        self.root.config(menu=m)

    def _activity(self, _e=None):
        self.last_activity = time.time()

    def toggle_lock(self):
        if self.locked:
            self.unlock()
        else:
            self.set_lock(True)

    def set_lock(self, locked, quiet=False, reason=""):
        was = self.locked
        self.locked = locked
        for i in (1, 2, 3):
            self.nb.tab(i, state="disabled" if locked else "normal")
        if locked and self.nb.index("current") in (1, 2, 3):
            self.nb.select(0)
        self.sb_lockbtn.config(text="Unlock setup..." if locked else "Lock setup")
        if locked and not was and not quiet:
            if self.dl_modified():
                self.dl_form_load(self.cfg["datalogs"][self.cur_dl])
                reason += " - unsaved data log changes discarded"
            self.add_event("INFO", core.CAT_SETUP, "-", "Setup locked" + (
                " ({})".format(reason.strip(" -")) if reason else ""))
        if not locked:
            self.last_activity = time.time()

    def ask_pin(self, why="Enter the setup PIN:"):
        pin = simpledialog.askstring(APP, why, show="*", parent=self.root)
        if pin is None:
            return False
        if core.pin_hash(pin) == self.cfg["settings"]["pin_hash"]:
            return True
        self.add_event("WARN", core.CAT_SETUP, "-", "Wrong setup PIN entered by " + core.windows_user())
        messagebox.showerror(APP, "Wrong PIN.")
        return False

    def unlock(self):
        if self.ask_pin():
            self.set_lock(False)
            self.add_event("INFO", core.CAT_SETUP, "-", "Setup unlocked by " + core.windows_user())

    def change_pin(self):
        if self.locked:
            messagebox.showinfo(APP, "Unlock setup first.")
            return
        a = simpledialog.askstring(APP, "New PIN (at least 4 characters):", show="*", parent=self.root)
        if not a:
            return
        if len(a) < 4:
            messagebox.showerror(APP, "PIN must have at least 4 characters.")
            return
        if a != simpledialog.askstring(APP, "Repeat the new PIN:", show="*", parent=self.root):
            messagebox.showerror(APP, "PINs do not match.")
            return
        self.cfg["settings"]["pin_hash"] = core.pin_hash(a)
        self.save_cfg(quiet=True)
        self.add_event("INFO", core.CAT_SETUP, "-", "Setup PIN changed")
        messagebox.showinfo(APP, "PIN changed.")

    def _protected(self, what):
        """Stop/Exit need the PIN when setup is locked (so operators cannot switch the watcher off)."""
        if not self.locked:
            return messagebox.askyesno(APP, what + "\n\nData log problems will NOT be detected.")
        return self.ask_pin(what + "\nEnter the setup PIN:")

    def _ctrl_s(self):
        if self.locked:
            return
        i = self.nb.index("current")
        if i == 1:
            self.dl_apply()
        elif i == 3:
            self.settings_apply()

    def hide_window(self):
        if self.tray.ok:
            self.root.withdraw()
            self.hidden = True
        else:
            self.root.iconify()

    def _on_unmap(self, e):
        if e.widget is self.root and self.tray.ok and self.root.state() == "iconic":
            self.root.after(50, self.hide_window)

    def show_window(self):
        self.hidden = False
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        self._update_status(self.monitor.snapshot(), force=True)

    def _tray_cmd(self, cmd):
        if cmd == 1:
            self.show_window()
        elif cmd == 2:
            self.test_banner()
        elif cmd == 3:
            self.show_window()
            self.exit_app()

    # ---------------------------------------------------------------- Status tab
    def _build_status(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="Status")
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(0, 6))
        toolbar(top, [("▶  Start monitoring", self.start_monitoring, "Start checking the data logs"),
                      ("■  Stop monitoring", self.stop_monitoring, "Needs the PIN when setup is locked")]
                ).pack(side="left")
        b = ttk.Button(top, text="⟳  Check now", command=self.check_now)
        b.pack(side="right")
        ToolTip(b, "Run a check immediately instead of waiting")
        self.v_problems = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show only critical / warning / unknown", variable=self.v_problems,
                        command=lambda: self._update_status(self.monitor.snapshot(), force=True)
                        ).pack(side="left", padx=16)
        self.st_summary = ttk.Label(top, text="", style="Big.TLabel")
        self.st_summary.pack(side="right", padx=(0, 16))
        cols = ("State", "Data log", "Type", "Trigger", "Size", "Last growth", "Current file", "File time", "Detail")
        fr, self.st_tree = make_tree(f, cols, (80, 180, 55, 75, 75, 85, 300, 130, 260), 18, stretch_col="Detail")
        fr.pack(fill="both", expand=True)
        for k, c in STATE_BG.items():
            self.st_tree.tag_configure(k, background=c)
        self.st_tree.bind("<Double-1>", self.status_dbl)
        ttk.Label(f, style="Hint.TLabel",
                  text="State: Started = data set growing   Stopped = not logging   Critical = should log but does not   "
                       "Unknown = PLC not reachable   Paused = runtime not running / starting.     Trigger: Triggered = "
                       "should log   Waiting = should be stopped   Always = Macro.     Double-click a row for its history."
                  ).pack(anchor="w", pady=(6, 0))
        ttk.Label(f, text="ℹ  " + core.INFERRED_NOTE, foreground=C_INFO).pack(anchor="w", pady=(2, 0))

    def check_now(self):
        if not self.running.is_set():
            messagebox.showinfo(APP, "Monitoring is stopped - press Start monitoring first.")
            return
        self.wake.set()

    def status_dbl(self, _e=None):
        sel = self.st_tree.selection()
        if sel:
            self.v_hdl.set(sel[0])
            self.hist_refill()
            self.nb.select(4)

    # ---------------------------------------------------------------- Data Logs tab
    def _build_datalogs(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="Data Logs")
        pw = ttk.PanedWindow(f, orient="horizontal")
        pw.pack(fill="both", expand=True)

        left = ttk.LabelFrame(pw, text="Data logs", style="Section.TLabelframe", padding=6)
        pw.add(left, weight=1)
        fr, self.dl_tree = make_tree(left, ("Name", "Type", "On"), (190, 60, 34), 20, stretch_col="Name")
        fr.pack(fill="both", expand=True)
        self.dl_tree.bind("<<TreeviewSelect>>", lambda e: self.dl_select())
        toolbar(left, [("Add", self.dl_add, "New data log"),
                       ("Duplicate...", self.dl_dup, "Copy the selected data log and replace text "
                                                     "(e.g. a PLC topic) in its name and expression"),
                       ("Remove", self.dl_remove, "Stop monitoring this data log (files are not touched)")]
                ).pack(fill="x", pady=(6, 0))
        toolbar(left, [("▲ Up", lambda: self.dl_move(-1), None),
                       ("▼ Down", lambda: self.dl_move(1), None)]).pack(fill="x", pady=(4, 0))

        right = ttk.Frame(pw, padding=(8, 0, 0, 0))
        pw.add(right, weight=4)
        g = ttk.LabelFrame(right, text="General", style="Section.TLabelframe", padding=8)
        g.pack(fill="x")
        ttk.Label(g, text="Name (log folder)").grid(row=0, column=0, sticky="w", pady=3)
        self.v_name = tk.StringVar()
        self.cb_name = ttk.Combobox(g, textvariable=self.v_name, width=42)
        self.cb_name.grid(row=0, column=1, sticky="w", padx=6)
        b = ttk.Button(g, text="⟳", width=3, command=self.refresh_folders)
        b.grid(row=0, column=2, sticky="w")
        ToolTip(b, "Re-read the folder names in the datalog folder")
        ttk.Label(g, text="Type").grid(row=1, column=0, sticky="w", pady=3)
        self.v_type = tk.StringVar()
        cb = ttk.Combobox(g, textvariable=self.v_type, values=list(core.TYPE_NAMES.values()),
                          state="readonly", width=46)
        cb.grid(row=1, column=1, columnspan=2, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.dl_show_type())
        self.v_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(g, text="Enabled (monitor this data log)", variable=self.v_enabled
                        ).grid(row=2, column=1, sticky="w", padx=6, pady=3)
        self.lbl_typehelp = ttk.Label(g, text="", style="Hint.TLabel", wraplength=700, justify="left")
        self.lbl_typehelp.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        self.fr_expr = ttk.LabelFrame(right, text="Trigger expression", style="Section.TLabelframe", padding=8)
        ttk.Label(self.fr_expr, style="Hint.TLabel", wraplength=720, justify="left",
                  text="Paste the ON expression of the FactoryTalk event. PLC tags are written {[TOPIC]TagName}. "
                       "HMI tags (no [TOPIC]) cannot be read and are treated as EMPTY: a comparison with text is "
                       "FALSE, the tag on its own is FALSE. Supports AND OR NOT  IF THEN ELSE  = <> < > <= >=  "
                       "+ - * /  and brackets.").pack(anchor="w")
        self.tx_expr = tk.Text(self.fr_expr, height=6, width=90, font=("Consolas", 10), wrap="word",
                               relief="solid", bd=1, undo=True)
        self.tx_expr.pack(fill="x", pady=6)
        eb = ttk.Frame(self.fr_expr)
        eb.pack(fill="x")
        ttk.Button(eb, text="✓ Validate", command=self.expr_validate).pack(side="left")
        ttk.Button(eb, text="Evaluate now", command=self.expr_evaluate).pack(side="left", padx=4)
        ttk.Label(eb, text="Insert tag:").pack(side="left", padx=(16, 4))
        self.cb_ins = ttk.Combobox(eb, width=40, state="readonly")
        self.cb_ins.pack(side="left")
        ttk.Button(eb, text="Insert", command=self.expr_insert).pack(side="left", padx=4)
        self.lbl_expr = ttk.Label(self.fr_expr, text="", wraplength=720, justify="left")
        self.lbl_expr.pack(fill="x", pady=(6, 0))

        self.fr_time = ttk.LabelFrame(right, text="Issue timing", style="Section.TLabelframe", padding=8)
        self.fr_time.pack(fill="x", pady=(8, 0))
        self.tv, self.time_rows = {}, {}
        for k, lab in (("start_grace_min", "Issue if the data log has NOT STARTED this long after the trigger"),
                       ("stale_min", "Issue if the data log has NOT GROWN this long while it should log"),
                       ("stop_grace_min", "Warning if the data log is STILL GROWING this long after the trigger "
                                          "is off (history only)")):
            v = tk.StringVar()
            self.tv[k] = v
            self.time_rows[k] = (ttk.Spinbox(self.fr_time, from_=0, to=100000, increment=1, textvariable=v, width=7),
                                 ttk.Label(self.fr_time, text="min", width=4), ttk.Label(self.fr_time, text=lab))
        ttk.Label(self.fr_time, style="Hint.TLabel", wraplength=720, justify="left",
                  text="Tip: set 'NOT GROWN' to at least 3 x the logging interval of the FTView model."
                  ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(4, 0))

        foot = ttk.Frame(right)
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(foot, text="Apply and save", style="Accent.TButton", command=self.dl_apply).pack(side="right")
        ttk.Button(foot, text="Revert", command=self.dl_revert).pack(side="right", padx=6)
        self.lbl_dl = ttk.Label(foot, text="")
        self.lbl_dl.pack(side="left")

    def all_refs(self):
        out = []
        for p in self.cfg["plcs"]:
            out += ["[{}]{}".format(p["topic"], t) for t in p["tags"]]
        return out

    def refresh_folders(self):
        r = self.cfg["settings"]["log_root"]
        try:
            names = sorted(p.name for p in Path(r).iterdir() if p.is_dir()) if r else []
        except OSError:
            names = []
        self.cb_name["values"] = names

    def dl_refresh_list(self, select=None):
        self.dl_tree.delete(*self.dl_tree.get_children())
        for i, d in enumerate(self.cfg["datalogs"]):
            self.dl_tree.insert("", "end", iid=str(i), values=(
                d["name"], core.TYPE_SHORT[d["type"]], "✓" if d.get("enabled", True) else "–"))
        self.cb_ins["values"] = self.all_refs()
        if select is not None and 0 <= select < len(self.cfg["datalogs"]):
            self._dl_internal = True
            self.dl_tree.selection_set(str(select))
            self.dl_tree.see(str(select))
            self._dl_internal = False
            self.cur_dl = select
            self.dl_form_load(self.cfg["datalogs"][select])
        elif not self.cfg["datalogs"]:
            self.cur_dl = None
            self.dl_form_load(None)

    def dl_select(self):
        if getattr(self, "_dl_internal", False):
            return
        sel = self.dl_tree.selection()
        if not sel:
            return
        i = int(sel[0])
        if i == self.cur_dl:
            return
        if self.dl_modified():
            ans = messagebox.askyesnocancel(APP, "Save your changes to '{}'?".format(
                self.cfg["datalogs"][self.cur_dl]["name"]))
            if ans is None or (ans and not self.dl_apply(reselect=False)):
                self._dl_internal = True
                self.dl_tree.selection_set(str(self.cur_dl))
                self._dl_internal = False
                return
            if ans:
                self.dl_refresh_list(select=i)
                return
        self.cur_dl = i
        self.dl_form_load(self.cfg["datalogs"][i])

    def dl_modified(self):
        return (self.cur_dl is not None and self.cur_dl < len(self.cfg["datalogs"])
                and self.dl_form_get() != self.cfg["datalogs"][self.cur_dl])

    def dl_revert(self):
        if self.cur_dl is not None and self.cur_dl < len(self.cfg["datalogs"]):
            self.dl_form_load(self.cfg["datalogs"][self.cur_dl])

    def dl_form_load(self, d):
        d = d or dict(core.DEFAULT_DL)
        self.v_name.set(d["name"])
        self.v_type.set(core.TYPE_NAMES[d["type"]])
        self.v_enabled.set(d.get("enabled", True))
        self.tx_expr.delete("1.0", "end")
        self.tx_expr.insert("1.0", d.get("expr", ""))
        for k in self.tv:
            self.tv[k].set("{:g}".format(float(d.get(k, core.DEFAULT_DL[k]))))
        self.lbl_expr.config(text="")
        self.dl_show_type()

    def dl_type(self):
        for k, v in core.TYPE_NAMES.items():
            if v == self.v_type.get():
                return k
        return core.T_MACRO

    def dl_show_type(self):
        t = self.dl_type()
        self.lbl_typehelp.config(text=TYPE_HELP[t])
        self.fr_expr.pack_forget()
        if t == core.T_EXPR:
            self.fr_expr.pack(fill="x", pady=(8, 0), before=self.fr_time)
        show = ["stale_min"] if t == core.T_MACRO else ["start_grace_min", "stale_min", "stop_grace_min"]
        row = 0
        for k, (sp, un, lb) in self.time_rows.items():
            for w in (sp, un, lb):
                w.grid_forget()
            if k in show:
                sp.grid(row=row, column=0, padx=(0, 4), pady=3, sticky="w")
                un.grid(row=row, column=1, sticky="w")
                lb.grid(row=row, column=2, padx=6, pady=3, sticky="w")
                row += 1

    def dl_form_get(self):
        ok = self.cur_dl is not None and self.cur_dl < len(self.cfg["datalogs"])
        base = dict(self.cfg["datalogs"][self.cur_dl]) if ok else dict(core.DEFAULT_DL)
        d = dict(base)
        d["name"] = self.v_name.get().strip()
        d["type"] = self.dl_type()
        d["enabled"] = bool(self.v_enabled.get())
        d["expr"] = self.tx_expr.get("1.0", "end").strip()
        for k, v in self.tv.items():
            try:
                d[k] = float(v.get())
            except ValueError:
                d[k] = base.get(k, core.DEFAULT_DL[k])
        return d

    def dl_apply(self, reselect=True):
        i = self.cur_dl
        if i is None or i >= len(self.cfg["datalogs"]):
            return False
        d = self.dl_form_get()
        err = None
        if not d["name"]:
            err = "The name is empty."
        elif any(x["name"].lower() == d["name"].lower() for j, x in enumerate(self.cfg["datalogs"]) if j != i):
            err = "A data log called '{}' already exists.".format(d["name"])
        elif d["type"] == core.T_EXPR:
            try:
                core.Expression(d["expr"])
            except core.ExprError as e:
                err = "Expression error: {}".format(e)
        if err:
            messagebox.showerror(APP, err)
            return False
        root = self.cfg["settings"]["log_root"]
        if root and not (Path(root) / d["name"]).is_dir():
            if not messagebox.askyesno(APP, "Folder\n{}\ndoes not exist (yet).\n\nSave anyway?".format(
                    Path(root) / d["name"])):
                return False
        if d["type"] == core.T_EXPR:
            self._add_missing_tags(core.Expression(d["expr"]))
        self.cfg["datalogs"][i] = d
        self.save_cfg(quiet=True)
        self.add_event("INFO", core.CAT_SETUP, d["name"], "Data log settings saved")
        self.lbl_dl.config(text="✓ Saved {:%H:%M:%S}".format(datetime.now()), foreground=C_OK)
        self.dl_refresh_list(select=i if reselect else None)
        return True

    def _add_missing_tags(self, expr):
        topics = {p["topic"].lower(): p for p in self.cfg["plcs"]}
        added, unknown = [], set()
        for orig in expr.plc_refs().values():
            topic, tag = core.split_ref(orig)
            p = topics.get(topic.lower())
            if p is None:
                unknown.add(topic)
            elif tag.lower() not in [t.lower() for t in p["tags"]]:
                p["tags"].append(tag)
                added.append("[{}]{}".format(p["topic"], tag))
        if added:
            self.add_event("INFO", core.CAT_SETUP, "-", "Tags added to the PLC tag list automatically",
                           ", ".join(added))
        if unknown:
            messagebox.showwarning(APP, "These PLC topics are used but not defined in the PLCs tab:\n\n   " +
                                   ", ".join(sorted(unknown)) +
                                   "\n\nAdd them in the PLCs tab, otherwise the trigger stays unknown.")

    def dl_add(self):
        if self.dl_modified() and not messagebox.askyesno(APP, "Discard unsaved changes of the current data log?"):
            return
        names = {d["name"].lower() for d in self.cfg["datalogs"]}
        n = 1
        while "new_datalog_{}".format(n) in names:
            n += 1
        self.cfg["datalogs"].append(dict(core.DEFAULT_DL, name="New_Datalog_{}".format(n), enabled=False))
        self.refresh_folders()
        self.dl_refresh_list(select=len(self.cfg["datalogs"]) - 1)
        self.lbl_dl.config(text="New data log (disabled) - choose folder and type, tick Enabled, then Apply "
                                "and save", foreground=C_INFO)

    def dl_dup(self):
        if self.cur_dl is None:
            return
        src = self.cfg["datalogs"][self.cur_dl]
        r = FormDialog(self.root, "Duplicate data log", [("Find text:", ""), ("Replace with:", "")],
                       note="The text is replaced in the name and expression of the copy "
                            "(not case-sensitive). Leave empty for an exact copy.").result
        if r is None:
            return
        a, b = r

        def rep(s):
            return re.sub(re.escape(a), b.replace("\\", "\\\\"), s, flags=re.I) if a else s
        d = dict(src)
        d.update(name=rep(src["name"]), expr=rep(src["expr"]))
        names = {x["name"].lower() for x in self.cfg["datalogs"]}
        while d["name"].lower() in names:
            d["name"] += "_copy"
        self.cfg["datalogs"].insert(self.cur_dl + 1, d)
        if d["type"] == core.T_EXPR:
            try:
                self._add_missing_tags(core.Expression(d["expr"]))
            except core.ExprError:
                pass
        self.save_cfg(quiet=True)
        self.dl_refresh_list(select=self.cfg["datalogs"].index(d))
        self.lbl_dl.config(text="✓ Copy created: " + d["name"], foreground=C_OK)

    def dl_remove(self):
        if self.cur_dl is None:
            return
        n = self.cfg["datalogs"][self.cur_dl]["name"]
        if messagebox.askyesno(APP, "Remove '{}' from monitoring?\n\nThe log files are not touched.".format(n)):
            del self.cfg["datalogs"][self.cur_dl]
            self.cur_dl = None
            self.save_cfg(quiet=True)
            self.add_event("INFO", core.CAT_SETUP, n, "Data log removed from monitoring")
            self.dl_refresh_list(select=0)

    def dl_move(self, step):
        i = self.cur_dl
        if i is None or not 0 <= i + step < len(self.cfg["datalogs"]):
            return
        if self.dl_modified():
            messagebox.showinfo(APP, "Apply or revert your changes first.")
            return
        L = self.cfg["datalogs"]
        L[i], L[i + step] = L[i + step], L[i]
        self.save_cfg(quiet=True)
        self.dl_refresh_list(select=i + step)

    def expr_insert(self):
        r = self.cb_ins.get()
        if r:
            self.tx_expr.insert("insert", "{" + r + "}")

    def expr_validate(self):
        try:
            e = core.Expression(self.tx_expr.get("1.0", "end").strip())
        except core.ExprError as x:
            self.lbl_expr.config(text="✗  {}".format(x), foreground=C_BAD)
            return None
        plc = sorted(e.plc_refs().values())
        cst = sorted(e.const_refs().values())
        miss = []
        topics = {p["topic"].lower() for p in self.cfg["plcs"]}
        bad = sorted({core.split_ref(r)[0] for r in plc if core.split_ref(r)[0].lower() not in topics})
        msg = "✓  Expression OK.   PLC tags: {}".format(len(plc))
        if cst:
            msg += "\nℹ  HMI tag(s) treated as empty: {}".format(", ".join(cst))
        if bad:
            msg += "\n⚠  PLC topic(s) not defined in the PLCs tab: {}".format(", ".join(bad))
        self.lbl_expr.config(text=msg, foreground=C_WARN if (miss or bad) else C_OK)
        return e

    def expr_evaluate(self):
        e = self.expr_validate()
        if not e:
            return
        refs = list(e.plc_refs().values())

        def done(vals):
            allv = core.hmi_values(e)
            allv.update(vals)
            res = e.evaluate(allv)
            shown = "   ".join("{} = {}".format(r, vals.get(core.ref_key(*core.split_ref(r)))) for r in refs)
            txt = {True: "TRIGGERED  -  the data log should be logging",
                   False: "WAITING  -  the data log should be stopped",
                   None: "UNKNOWN  -  a value could not be read"}[res]
            self.lbl_expr.config(text="Result: {}\n{}".format(txt, shown),
                                 foreground={True: C_OK, False: C_INFO, None: C_WARN}[res])
        self.lbl_expr.config(text="Reading PLC values...", foreground=C_INFO)
        self.read_refs(refs, done)

    def read_refs(self, refs, callback, text="Reading PLC values..."):
        plcs = {p["topic"].lower(): p for p in self.cfg["plcs"]}
        settings = dict(self.cfg["settings"])
        jid = self.job_start(text)

        def work():
            out, by = {}, {}
            for r in refs:
                t, tag = core.split_ref(r)
                if t:
                    by.setdefault(t.lower(), []).append(tag)
            for t, tags in by.items():
                p, res = plcs.get(t), {}
                if p:
                    try:
                        o = core.make_plc(p, settings, self.simulate)
                        res, _ = o.read(tags)
                        o.close()
                    except Exception:
                        res = {}
                for tag in tags:
                    out[core.ref_key(t, tag)] = res.get(tag)

            def done():
                self.job_end(jid)
                callback(out)
            self.ui_q.put(done)
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- PLCs tab
    def _build_plcs(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="PLCs")
        pw = ttk.PanedWindow(f, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.LabelFrame(pw, text="Controllers", style="Section.TLabelframe", padding=6)
        pw.add(left, weight=1)
        ttk.Label(left, style="Hint.TLabel", wraplength=470, justify="left",
                  text="Topic = the same shortcut name as in FactoryTalk View (the part in [ ] of a tag)."
                  ).pack(anchor="w", pady=(0, 4))
        fr, self.plc_tree = make_tree(left, ("Topic", "IP address", "Slot", "Conn. size", "Status"),
                                      (80, 120, 40, 70, 220), 18, stretch_col="Status")
        fr.pack(fill="both", expand=True)
        self.plc_tree.tag_configure("ok", background=STATE_BG["Started"])
        self.plc_tree.tag_configure("fail", background=STATE_BG["Critical"])
        self.plc_tree.bind("<<TreeviewSelect>>", lambda e: self.tags_refresh())
        self.plc_tree.bind("<Double-1>", lambda e: self.plc_edit())
        toolbar(left, [("Add", self.plc_add, "Add a controller"),
                       ("Edit", self.plc_edit, "Change topic, IP or slot (double-click works too)"),
                       ("Remove", self.plc_remove, "Remove the controller and its tag list"),
                       ("Test", self.plc_test, "Read the PLC clock to test the connection"),
                       ("Test all", lambda: self.plc_test(all_=True), None)]).pack(fill="x", pady=(6, 0))
        right = ttk.LabelFrame(pw, text="Tags", style="Section.TLabelframe", padding=6)
        pw.add(right, weight=1)
        self.lbl_tags = ttk.Label(right, text="Select a controller on the left.", style="Hint.TLabel")
        self.lbl_tags.pack(anchor="w", pady=(0, 4))
        fr, self.tag_tree = make_tree(right, ("Tag", "Value", "Used by"), (240, 80, 220), 18, stretch_col="Tag",
                                      select="extended")
        fr.pack(fill="both", expand=True)
        self.tag_tree.bind("<Double-1>", lambda e: self.sim_set())
        toolbar(right, [("Add tag...", self.tag_add, "Type a tag name, e.g. a UDT member"),
                        ("Browse PLC...", self.tag_browse, "Pick tags from the controller's tag list"),
                        ("Remove", self.tag_remove, "Remove the selected tags (only if not used)"),
                        ("Read now", self.tag_read, "Read the current values from the PLC")]).pack(fill="x", pady=(6, 0))
        if self.simulate:
            ttk.Label(right, text="SIMULATION: double-click a tag to set its value.", foreground=C_BAD
                      ).pack(anchor="w", pady=(4, 0))

    def plc_refresh(self, select_topic=None):
        keep = select_topic or (self.plc_tree.selection()[0] if self.plc_tree.selection() else None)
        self.plc_tree.delete(*self.plc_tree.get_children())
        st = self.monitor.snapshot()["plc"]
        for p in self.cfg["plcs"]:
            s = st.get(p["topic"].lower())
            self.plc_tree.insert("", "end", iid=p["topic"], values=(p["topic"], p["ip"], p["slot"],
                                                                    p.get("conn_size") or "auto",
                                                                    "" if s is None else s[1]),
                                 tags=(() if s is None else ("ok" if s[0] else "fail")))
        if keep and self.plc_tree.exists(keep):
            self.plc_tree.selection_set(keep)
        self.tags_refresh()

    def sel_plc(self):
        s = self.plc_tree.selection()
        if s:
            for p in self.cfg["plcs"]:
                if p["topic"] == s[0]:
                    return p
        return None

    def used_by(self, topic, tag=None):
        users = []
        for d in self.cfg["datalogs"]:
            if d["type"] != core.T_EXPR:
                continue
            try:
                refs = list(core.Expression(d["expr"]).plc_refs().values())
            except core.ExprError:
                continue
            for r in refs:
                t, g = core.split_ref(r)
                if t and t.lower() == topic.lower() and (tag is None or g.lower() == tag.lower()):
                    users.append(d["name"])
                    break
        return users

    def plc_dialog(self, p=None):
        p = p or {"topic": "", "ip": "", "slot": 0, "conn_size": 0}
        r = FormDialog(self.root, "Controller", [("Topic (FTView shortcut)", p["topic"]), ("IP address", p["ip"]),
                                                 ("Processor slot", p["slot"]),
                                                 ("Connection size (0 = auto)", p.get("conn_size", 0))],
                       note="Slot: chassis slot of the processor (CompactLogix = 0, Emulate = slot of the "
                            "emulated controller).\nConnection size: leave 0. For Studio 5000 Logix Emulate use "
                            "504; FactoryTalk Linx must have 'Listen on EtherNet/IP encapsulation ports' enabled "
                            "(pylogix issue #221).").result
        if not r:
            return None
        topic, ip, slot, csize = r
        try:
            csize = int(csize or 0)
            if csize and not 100 <= csize <= 4002:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP, "Connection size must be 0 (auto) or a number between 100 and 4002.")
            return None
        if not topic or not ip:
            messagebox.showerror(APP, "Topic and IP address are required.")
            return None
        if not re.match(r"^[A-Za-z0-9_\-. ]+$", topic):
            messagebox.showerror(APP, "Topic may contain letters, digits, _ - . and spaces only.")
            return None
        try:
            slot = int(slot)
        except ValueError:
            messagebox.showerror(APP, "Slot must be a number.")
            return None
        return topic, ip, slot, csize

    def plc_add(self):
        r = self.plc_dialog()
        if not r:
            return
        if any(p["topic"].lower() == r[0].lower() for p in self.cfg["plcs"]):
            messagebox.showerror(APP, "Topic {} already exists.".format(r[0]))
            return
        self.cfg["plcs"].append({"topic": r[0], "ip": r[1], "slot": r[2], "conn_size": r[3], "tags": []})
        self.save_cfg(quiet=True)
        self.plc_refresh(r[0])

    def plc_edit(self):
        p = self.sel_plc()
        if not p:
            return
        r = self.plc_dialog(p)
        if not r:
            return
        old = p["topic"]
        if r[0].lower() != old.lower() and any(x["topic"].lower() == r[0].lower() for x in self.cfg["plcs"]):
            messagebox.showerror(APP, "Topic {} already exists.".format(r[0]))
            return
        if r[0] != old:
            users = self.used_by(old)
            if users and messagebox.askyesno(APP, "{} data log(s) use [{}].\nChange them to [{}] too?".format(
                    len(users), old, r[0])):
                pat = re.compile(r"\[" + re.escape(old) + r"\]", re.I)
                for d in self.cfg["datalogs"]:
                    d["expr"] = pat.sub("[" + r[0] + "]", d["expr"])
        p.update(topic=r[0], ip=r[1], slot=r[2], conn_size=r[3])
        self.save_cfg(quiet=True)
        self.plc_refresh(r[0])
        self.dl_refresh_list(select=self.cur_dl)

    def plc_remove(self):
        p = self.sel_plc()
        if not p:
            return
        users = self.used_by(p["topic"])
        if users:
            messagebox.showerror(APP, "{} is used by:\n\n   {}\n\nChange those data logs first.".format(
                p["topic"], "\n   ".join(users)))
            return
        if messagebox.askyesno(APP, "Remove controller {} and its tag list?".format(p["topic"])):
            self.cfg["plcs"].remove(p)
            self.save_cfg(quiet=True)
            self.plc_refresh()

    def plc_test(self, all_=False):
        targets = list(self.cfg["plcs"]) if all_ else [p for p in [self.sel_plc()] if p]
        settings = dict(self.cfg["settings"])
        for p in targets:
            if self.plc_tree.exists(p["topic"]):
                self.plc_tree.set(p["topic"], "Status", "testing...")
            jid = self.job_start("Testing connection to {}...".format(p["topic"]))

            def work(p=p, jid=jid):
                try:
                    o = core.make_plc(p, settings, self.simulate)
                    ok, msg = o.test()
                    o.close()
                except Exception as e:
                    ok, msg = False, str(e)

                def show():
                    self.job_end(jid)
                    if self.plc_tree.exists(p["topic"]):
                        self.plc_tree.set(p["topic"], "Status", msg)
                        self.plc_tree.item(p["topic"], tags=("ok" if ok else "fail",))
                    self.add_event("OK" if ok else "WARN", core.CAT_PLC, "PLC " + p["topic"], "Connection test", msg)
                self.ui_q.put(show)
            threading.Thread(target=work, daemon=True).start()

    def tags_refresh(self, values=None):
        self.tag_tree.delete(*self.tag_tree.get_children())
        p = self.sel_plc()
        if not p:
            self.lbl_tags.config(text="Select a controller on the left.")
            return
        self.lbl_tags.config(text="Tags read from {}  ({})".format(p["topic"], len(p["tags"])))
        live = self.monitor.snapshot()["values"]
        for t in p["tags"]:
            k = core.ref_key(p["topic"], t)
            v = (values or {}).get(k, live.get(k, ""))
            if self.simulate:
                v = core.SimPLC.store.get((p["topic"].lower(), t.lower()), False)
            self.tag_tree.insert("", "end", iid=t, values=(t, v, ", ".join(self.used_by(p["topic"], t)) or "(not used)"))

    def tag_add(self):
        p = self.sel_plc()
        if not p:
            messagebox.showinfo(APP, "Select a controller first.")
            return
        t = simpledialog.askstring(APP, "Tag name in {}\n\nExamples:  MyTag   MyUDT.Member   "
                                        "Program:MainProgram.MyTag".format(p["topic"]), parent=self.root)
        if t and t.strip():
            t = t.strip().strip("{}")
            if t.lower() not in [x.lower() for x in p["tags"]]:
                p["tags"].append(t)
                self.save_cfg(quiet=True)
            self.tags_refresh()
            self.dl_refresh_list(select=self.cur_dl)

    def tag_browse(self):
        p = self.sel_plc()
        if not p:
            messagebox.showinfo(APP, "Select a controller first.")
            return
        settings = dict(self.cfg["settings"])
        jid = self.job_start("Reading tag list from {}...".format(p["topic"]))
        self.lbl_tags.config(text="Reading tag list from {}...".format(p["topic"]))

        def work():
            try:
                o = core.make_plc(p, settings, self.simulate)
                names, err = o.tag_list(), None
                o.close()
            except Exception as e:
                names, err = [], str(e)

            def show():
                self.job_end(jid)
                self.tags_refresh()
                if err:
                    messagebox.showerror(APP, "Could not read the tag list of {}:\n{}".format(p["topic"], err))
                    return
                picked = PickDialog(self.root, "Tags in " + p["topic"], names).result
                if picked:
                    for n in picked:
                        if n.lower() not in [x.lower() for x in p["tags"]]:
                            p["tags"].append(n)
                    self.save_cfg(quiet=True)
                    self.tags_refresh()
                    self.dl_refresh_list(select=self.cur_dl)
            self.ui_q.put(show)
        threading.Thread(target=work, daemon=True).start()

    def tag_remove(self):
        p = self.sel_plc()
        sel = self.tag_tree.selection()
        if not p or not sel:
            return
        for t in sel:
            users = self.used_by(p["topic"], t)
            if users:
                messagebox.showerror(APP, "[{}]{} is used by:\n\n   {}".format(p["topic"], t, "\n   ".join(users)))
                return
        p["tags"] = [x for x in p["tags"] if x not in sel]
        self.save_cfg(quiet=True)
        self.tags_refresh()
        self.dl_refresh_list(select=self.cur_dl)

    def tag_read(self):
        p = self.sel_plc()
        if p:
            self.lbl_tags.config(text="Reading {} tag(s) from {}...".format(len(p["tags"]), p["topic"]))
            for t in p["tags"]:
                if self.tag_tree.exists(t):
                    self.tag_tree.set(t, "Value", "reading...")
            self.read_refs(["[{}]{}".format(p["topic"], t) for t in p["tags"]], lambda v: self.tags_refresh(v),
                           text="Reading {} tag(s) from {}...".format(len(p["tags"]), p["topic"]))

    def sim_set(self):
        if not self.simulate:
            return
        p = self.sel_plc()
        sel = self.tag_tree.selection()
        if not p or not sel:
            return
        key = (p["topic"].lower(), sel[0].lower())
        cur = core.SimPLC.store.get(key, False)
        v = simpledialog.askstring(APP, "Simulated value for [{}]{}\n(1 / 0, number or text):".format(
            p["topic"], sel[0]), initialvalue=str(int(cur) if isinstance(cur, bool) else cur), parent=self.root)
        if v is None:
            return
        v = v.strip()
        core.SimPLC.store[key] = True if v == "1" else False if v == "0" else core.parse_const(v)
        self.tags_refresh()

    # ---------------------------------------------------------------- Settings tab
    def _build_settings(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="Settings")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Apply and save", style="Accent.TButton", command=self.settings_apply).pack(side="left")
        ttk.Button(bar, text="Revert", command=self.settings_load).pack(side="left", padx=6)
        self.lbl_set = ttk.Label(bar, text="")
        self.lbl_set.pack(side="left", padx=10)
        cols = ttk.Frame(f)
        cols.pack(fill="both", expand=True)
        lc = ttk.Frame(cols)
        lc.pack(side="left", fill="y", anchor="n")
        mc = ttk.Frame(cols)
        mc.pack(side="left", fill="y", anchor="n", padx=(10, 0))
        rc = ttk.Frame(cols)
        rc.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.sv = {}

        def section(parent, title):
            s = ttk.LabelFrame(parent, text=title, style="Section.TLabelframe", padding=8)
            s.pack(fill="x", pady=(0, 8))
            return s

        def field(sec, row, key, label, kind, tip=None, width=10):
            ttk.Label(sec, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            if kind == "bool":
                v = tk.BooleanVar()
                w = ttk.Checkbutton(sec, variable=v)
            elif kind == "pos":
                v = tk.StringVar()
                w = ttk.Combobox(sec, textvariable=v, state="readonly", width=14,
                                 values=["bottom-left", "bottom-right", "top-left", "top-right"])
                w.bind("<<ComboboxSelected>>", lambda e: self.test_banner())
            else:
                v = tk.StringVar()
                w = ttk.Entry(sec, textvariable=v, width=width)
            w.grid(row=row, column=1, sticky="w")
            if tip:
                ToolTip(w, tip)
            self.sv[key] = (v, kind)

        s = section(lc, "Data log files")
        ttk.Label(s, text="Datalog folder").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.sv["log_root"] = (tk.StringVar(), "entry")
        ttk.Entry(s, textvariable=self.sv["log_root"][0], width=36).grid(row=0, column=1, sticky="w")
        ttk.Button(s, text="Browse...", command=self.browse_root).grid(row=0, column=2, padx=6)
        field(s, 1, "keep_days", "FTView deletes data sets older than (days)", "num",
              "Same value as in the FTView data log models. 0 = do not check deletion.")

        s = section(lc, "Monitoring")
        field(s, 0, "check_sec", "Check every (seconds)", "num")
        field(s, 1, "runtime_grace_sec", "Start checks after runtime is running (seconds)", "num",
              "Gate: nothing is judged while the SE client (runtime) is closed, and for this long after it starts")
        field(s, 2, "plc_timeout", "PLC timeout (seconds)", "num")
        field(s, 3, "plc_retry_sec", "Retry an unreachable PLC every (seconds)", "num")

        s = section(lc, "Processes")
        field(s, 0, "client_exe", "SE client process", "entry", "Name as shown in Task Manager > Details", 26)
        field(s, 1, "engine_exe", "Datalog service process", "entry", "Name as shown in Task Manager > Details", 26)
        ttk.Button(s, text="Check now", command=self.check_processes).grid(row=0, column=2, rowspan=2, padx=6)

        s = section(mc, "Popup")
        field(s, 0, "banner_pos", "Position", "pos")
        field(s, 1, "snooze_min", "Snooze time (minutes)", "num")
        field(s, 2, "popup_on_not_stopped", "Also popup when a data log does not stop", "bool")
        field(s, 3, "log_only", "Log-only mode (no popups, history only)", "bool",
              "Use this for the first week to check for false issues")
        ttk.Button(s, text="Test popup", command=self.test_banner).grid(row=0, column=2, padx=6)

        s = section(mc, "Security, history and start-up")
        field(s, 0, "lock_enabled", "Auto-lock setup", "bool",
              "When off, setup stays unlocked until 'Lock setup' is pressed")
        field(s, 1, "lock_min", "Auto-lock after inactivity (minutes)", "num",
              "Setup locks when nobody used keyboard or mouse in this window for this time")
        ttk.Button(s, text="Change PIN...", command=self.change_pin).grid(row=0, column=2, padx=6)
        field(s, 2, "history_days", "Keep watcher history (days)", "num")
        field(s, 3, "progress_min", "History 'DL is growing' every (minutes)", "num")
        self.v_autostart = tk.BooleanVar(value=self.startup_file().exists())
        ttk.Label(s, text="Start with Windows, hidden in tray (this user)").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Checkbutton(s, variable=self.v_autostart).grid(row=4, column=1, sticky="w")


        c = ttk.LabelFrame(rc, text="Data log folders", style="Section.TLabelframe", padding=8)
        c.pack(fill="both", expand=True)
        ttk.Label(c, style="Hint.TLabel", wraplength=420, justify="left",
                  text="Checks every data log folder in the datalog folder entered on the left (also before "
                       "'Apply'). Expected file names: 'YYYY MM DD 0000 <model> (Float).DAT'."
                  ).pack(anchor="w", pady=(0, 6))
        fr, self.fold_tree = make_tree(c, ("Data log", "On", "Result"), (150, 30, 300), 16, stretch_col="Result")
        fr.pack(fill="both", expand=True)
        self.fold_tree.tag_configure("ok", background=STATE_BG["Started"])
        self.fold_tree.tag_configure("bad", background=STATE_BG["Critical"])
        self.fold_tree.tag_configure("off", background=STATE_BG["Disabled"])
        toolbar(c, [("Check folders", self.folder_check, "List what the watcher finds in each folder"),
                    ("Open datalog folder", lambda: self._open(self.sv["log_root"][0].get() or APP_DIR), None)]
                ).pack(fill="x", pady=(6, 0))

    def settings_load(self):
        s = self.cfg["settings"]
        for k, (v, kind) in self.sv.items():
            val = s.get(k, core.DEFAULT_SETTINGS.get(k))
            v.set(val if kind in ("bool", "entry", "pos") else "{:g}".format(float(val)))
        self.v_autostart.set(self.startup_file().exists())
        self.folder_check()

    def settings_apply(self, quiet=False):
        s = self.cfg["settings"]
        new = {}
        for k, (v, kind) in self.sv.items():
            if kind == "num":
                try:
                    new[k] = float(v.get())
                    if new[k] < 0:
                        raise ValueError
                except ValueError:
                    messagebox.showerror(APP, "'{}' is not a valid number.".format(v.get()))
                    return False
            elif kind == "bool":
                new[k] = bool(v.get())
            else:
                new[k] = v.get().strip()
        new["check_sec"] = max(2.0, new["check_sec"])
        new["lock_min"] = max(1.0, new["lock_min"])
        new["plc_retry_sec"] = max(10.0, new["plc_retry_sec"])
        if new["log_root"] and not Path(new["log_root"]).is_dir():
            if not messagebox.askyesno(APP, "Folder {} was not found.\nSave anyway?".format(new["log_root"])):
                return False
        changed = [k for k in new if new[k] != s.get(k)]
        if "log_only" in changed:
            self.add_event("INFO", core.CAT_SETUP, "-", "Log-only mode " + ("ON (no popups)" if new["log_only"] else "OFF"))
        if "lock_enabled" in changed:
            self.add_event("INFO", core.CAT_SETUP, "-", "Auto-lock " + ("ON" if new["lock_enabled"] else "OFF"))
        s.update(new)
        self.set_autostart(self.v_autostart.get())
        self.save_cfg(quiet=True)
        if changed:
            self.add_event("INFO", core.CAT_SETUP, "-", "Settings saved", ", ".join(changed))
        self.refresh_folders()
        self.settings_load()
        if not quiet:
            messagebox.showinfo(APP, "Settings saved.\n\nDatalog folder: {}".format(s["log_root"] or "(not set)"))
        return True

    def check_processes(self):
        procs = core.running_processes()
        if procs is None:
            messagebox.showwarning(APP, "Could not read the process list.")
            return
        out = []
        for k in ("client_exe", "engine_exe"):
            n = self.sv[k][0].get().strip()
            out.append("{}:  {}".format(n, "RUNNING" if n.lower() in procs else "not running"))
        messagebox.showinfo(APP, "\n".join(out))

    def folder_check(self):
        cfg = {"settings": {"log_root": self.sv["log_root"][0].get().strip()}, "datalogs": self.cfg["datalogs"]}
        self.fold_tree.delete(*self.fold_tree.get_children())
        for name, on, folder, txt, ok in core.check_folders(cfg):
            self.fold_tree.insert("", "end", values=(name, "✓" if on else "–", txt),
                                  tags=("off",) if not on else (("ok",) if ok else ("bad",)))

    def settings_form(self):
        """Settings as typed in the form (None if a number is invalid)."""
        new = {}
        for k, (v, kind) in self.sv.items():
            if kind == "num":
                try:
                    new[k] = float(v.get())
                except ValueError:
                    return None
            elif kind == "bool":
                new[k] = bool(v.get())
            else:
                new[k] = v.get().strip()
        return new

    def settings_pending(self):
        """True if the Settings tab has values that are not applied/saved yet."""
        new = self.settings_form()
        if new is None:
            return True
        s = self.cfg["settings"]
        return any((float(s.get(k, 0)) != v) if isinstance(v, float) else (s.get(k) != v) for k, v in new.items())

    def _apply_pending_settings(self):
        if self.settings_pending():
            if messagebox.askyesno(APP, "The Settings tab has changes that are not applied yet "
                                        "(e.g. the datalog folder).\n\nApply them now?"):
                return self.settings_apply(quiet=True)
        return True

    def browse_root(self):
        d = filedialog.askdirectory(initialdir=self.sv["log_root"][0].get() or None)
        if d:
            self.sv["log_root"][0].set(str(Path(d)))

    def startup_file(self):
        return Path(os.environ.get("APPDATA", str(APP_DIR))) / \
            r"Microsoft\Windows\Start Menu\Programs\Startup" / "LogLoss_Watcher.vbs"

    def set_autostart(self, on):
        f = self.startup_file()
        try:
            old = f.with_name("DL_Watchdog.vbs")
            if old.exists():
                old.unlink()
            if on:
                if FROZEN:                                    # the .exe starts itself
                    cmd = '"""{}"" --minimized"'.format(Path(sys.executable).resolve())
                else:
                    exe = Path(sys.executable)
                    pyw = exe.with_name("pythonw.exe")
                    exe = pyw if pyw.exists() else exe
                    cmd = '"""{}"" ""{}"" --minimized"'.format(exe, Path(__file__).resolve())
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text('Set sh = CreateObject("WScript.Shell")\r\n'
                             'sh.CurrentDirectory = "{}"\r\n'
                             'sh.Run {}, 0, False\r\n'.format(APP_DIR, cmd), encoding="utf-8")
            elif f.exists():
                f.unlink()
        except OSError as e:
            messagebox.showerror(APP, "Could not change Windows start-up:\n{}".format(e))

    # ---------------------------------------------------------------- History tab
    def _build_history(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="History")
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Show").pack(side="left")
        self.v_hlevel = tk.StringVar(value="All")
        cb = ttk.Combobox(top, textvariable=self.v_hlevel, state="readonly", width=18,
                          values=["All", "OK", "Critical", "Warning", "Critical + Warning"])
        cb.pack(side="left", padx=(4, 10))
        cb.bind("<<ComboboxSelected>>", lambda e: self.hist_refill())
        ttk.Label(top, text="Category").pack(side="left")
        self.v_hcat = tk.StringVar(value="All")
        cb = ttk.Combobox(top, textvariable=self.v_hcat, state="readonly", width=13,
                          values=["All"] + core.CATEGORIES)
        cb.pack(side="left", padx=(4, 10))
        cb.bind("<<ComboboxSelected>>", lambda e: self.hist_refill())
        ttk.Label(top, text="Data log").pack(side="left")
        self.v_hdl = tk.StringVar(value="All")
        self.cb_hdl = ttk.Combobox(top, textvariable=self.v_hdl, state="readonly", width=24)
        self.cb_hdl.pack(side="left", padx=(4, 10))
        self.cb_hdl.bind("<<ComboboxSelected>>", lambda e: self.hist_refill())
        ttk.Label(top, text="Days").pack(side="left")
        self.v_hfrom = tk.StringVar(value=(date.today() - timedelta(days=1)).isoformat())
        self.v_hto = tk.StringVar(value=date.today().isoformat())
        ttk.Entry(top, textvariable=self.v_hfrom, width=11).pack(side="left", padx=(4, 0))
        ttk.Label(top, text="to").pack(side="left", padx=3)
        ttk.Entry(top, textvariable=self.v_hto, width=11).pack(side="left")
        b = ttk.Button(top, text="Load", command=self.hist_load_range)
        b.pack(side="left", padx=(4, 10))
        ToolTip(b, "Load the history of these days (YYYY-MM-DD) from the daily files. New entries keep "
                   "appearing live when the range includes today.")
        ttk.Label(top, text="Search").pack(side="left")
        self.v_hsearch = tk.StringVar()
        self.v_hsearch.trace_add("write", lambda *a: self.hist_refill())
        ttk.Entry(top, textvariable=self.v_hsearch, width=18).pack(side="left", padx=4)
        toolbar(top, [("Incident report...", self.incident_report,
                       "Everything around one time (select a Critical entry first) - for support / Rockwell"),
                      ("Export...", self.export_dialog, "History or coverage report as TXT / CSV"),
                      ("Open folder", lambda: self._open(DATA_DIR), "Daily history files"),
                      ("Clear view", self.hist_clear_view, "Clears the list (files are kept)")],
                side="right").pack(side="right")
        fr, self.hist_tree = make_tree(f, ("Time (local)", "UTC", "Level", "Category", "Data log", "Event", "Detail"),
                                       (130, 130, 60, 85, 160, 380, 300), 22, stretch_col="Event")
        fr.pack(fill="both", expand=True)
        for k, c in LEVEL_BG.items():
            self.hist_tree.tag_configure(k, background=c)
        self.hist_count = ttk.Label(f, text="", style="Hint.TLabel")
        self.hist_count.pack(anchor="w", pady=(6, 0))
        self.hist_cleared_at = ""

    def _hist_match(self, e):
        if e["time"] <= self.hist_cleared_at:
            return False
        lv = self.v_hlevel.get()
        want = {"OK": ("OK",), "Critical": ("ALARM",), "Warning": ("WARN",),
                "Critical + Warning": ("ALARM", "WARN")}.get(lv)
        if want and e["level"] not in want:
            return False
        if self.v_hcat.get() != "All" and e.get("cat") != self.v_hcat.get():
            return False
        if self.v_hdl.get() != "All" and e["dl"] != self.v_hdl.get():
            return False
        q = self.v_hsearch.get().strip().lower()
        return not q or q in (e["dl"] + " " + e["event"] + " " + e["detail"]).lower()

    def hist_refill(self):
        self.hist_tree.delete(*self.hist_tree.get_children())
        for e in self.history:
            if self._hist_match(e):
                self._hist_insert(e)
        if getattr(self.hist_tree, "_sort", None):
            sort_tree(self.hist_tree, toggle=False)
        self._hist_count()

    def hist_load_range(self):
        try:
            a = datetime.strptime(self.v_hfrom.get().strip(), "%Y-%m-%d")
            b = datetime.strptime(self.v_hto.get().strip(), "%Y-%m-%d")
        except ValueError:
            messagebox.showerror(APP, "Days must look like 2026-11-26")
            return
        if b < a:
            a, b = b, a
        self._drain_events()
        self.history = self.read_history_range(a, b.replace(hour=23, minute=59))[-MAX_HISTORY:]
        self.hist_cleared_at = ""
        self.hist_range_to = b.date()
        self.hist_refill()

    def hist_clear_view(self):
        self.hist_cleared_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.hist_refill()

    def _hist_count(self):
        self.hist_count.config(text="{} entries shown.   History files: {}".format(
            len(self.hist_tree.get_children()), DATA_DIR))

    def _hist_insert(self, e):
        self.hist_tree.insert("", 0, values=(e["time"], e.get("utc", ""), LEVEL_NAME.get(e["level"], e["level"]),
                                             e.get("cat", ""), e["dl"], e["event"], e["detail"]), tags=(e["level"],))
        kids = self.hist_tree.get_children()
        if len(kids) > MAX_HISTORY:
            self.hist_tree.delete(*kids[MAX_HISTORY:])

    def add_event(self, level, cat, dl, event, detail=""):
        loc, utc = core.now_stamps()
        event = core.label_event(dl, event)
        self.ev_q.put({"time": loc, "utc": utc, "level": level, "cat": cat, "dl": dl, "event": event,
                       "detail": detail})

    def _store_event(self, e):
        if getattr(self, "hist_range_to", date.today()) >= date.today():
            self.history.append(e)                    # live entries only when the loaded range includes today
            if len(self.history) > MAX_HISTORY:
                del self.history[:len(self.history) - MAX_HISTORY]
            if self._hist_match(e):
                self._hist_insert(e)
                self._hist_count()
        try:
            with (DATA_DIR / "history_{}.txt".format(e["time"][:10])).open("a", encoding="utf-8") as fh:
                fh.write("\t".join((e["time"], e.get("utc", ""), e["level"], e.get("cat", ""), e["dl"],
                                    e["event"], e["detail"].replace("\t", " ").replace("\n", " "))) + "\n")
        except OSError:
            pass

    @staticmethod
    def _parse_line(line):
        p = line.rstrip("\n").split("\t")
        if len(p) >= 7:
            return {"time": p[0], "utc": p[1], "level": p[2], "cat": p[3], "dl": p[4], "event": p[5], "detail": p[6]}
        return None

    def _load_history(self):
        for day in (date.today() - timedelta(days=1), date.today()):
            f = DATA_DIR / "history_{}.txt".format(day.isoformat())
            if f.exists():
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_HISTORY:]:
                    e = self._parse_line(line)
                    if e:
                        self.history.append(e)
        self.history = self.history[-MAX_HISTORY:]
        self.hist_refill()

    def read_history_range(self, t_from, t_to):
        a, b = t_from.strftime("%Y-%m-%d %H:%M:00"), t_to.strftime("%Y-%m-%d %H:%M:59")
        out, d = [], t_from.date()
        while d <= t_to.date():
            f = DATA_DIR / "history_{}.txt".format(d.isoformat())
            if f.exists():
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    e = self._parse_line(line)
                    if e and a <= e["time"] <= b:
                        out.append(e)
            d += timedelta(days=1)
        return out

    # ---------------------------------------------------------------- export
    def export_dialog(self):
        opt = ExportDialog(self).result
        if not opt:
            return
        self._drain_events()
        ext = opt["fmt"]
        fn = filedialog.asksaveasfilename(
            defaultextension="." + ext, filetypes=[("CSV", "*.csv")] if ext == "csv" else [("Text", "*.txt")],
            initialfile="LogLoss_{}_{:%Y%m%d_%H%M}.{}".format(opt["content"], datetime.now(), ext))
        if not fn:
            return
        try:
            n = self._export_history(fn, opt) if opt["content"] == "history" else self._export_coverage(fn, opt)
        except OSError as e:
            messagebox.showerror(APP, "Could not write the file:\n{}".format(e))
            return
        self.add_event("INFO", core.CAT_WATCHER, "-", "Exported {} ({} rows)".format(opt["content"], n), fn)
        messagebox.showinfo(APP, "Exported {} rows to\n{}".format(n, fn))

    def incident_report(self):
        self._drain_events()
        when = None
        sel = self.hist_tree.selection() if hasattr(self, "hist_tree") else ()
        if sel:
            try:
                when = datetime.strptime(self.hist_tree.item(sel[0], "values")[0][:16], "%Y-%m-%d %H:%M")
            except (ValueError, IndexError):
                when = None
        if when is None:
            crit = [e for e in self.history if e["level"] == "ALARM"]
            try:
                when = datetime.strptime(crit[-1]["time"][:16], "%Y-%m-%d %H:%M") if crit else None
            except ValueError:
                when = None
        opt = IncidentDialog(self, when or datetime.now()).result
        if not opt:
            return
        fn = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")],
                                          initialfile="LogLoss_incident_{:%Y%m%d_%H%M}.txt".format(opt["when"]))
        if not fn:
            return
        jid = self.job_start("Writing incident report...")
        try:
            self.monitor.save_coverage()
            events = self.read_history_range(opt["from"], opt["to"])
            cov = core.read_coverage(DATA_DIR, opt["from"].date(), opt["to"].date())
            hdr = ["{} {} - {} - INCIDENT REPORT".format(APP, VERSION, SUBTITLE),
                   "Computer {}  user {}  created {:%Y-%m-%d %H:%M:%S}".format(
                       os.environ.get("COMPUTERNAME", ""), core.windows_user(), datetime.now())]
            rows = self.monitor.snapshot()["rows"] if self.running.is_set() else []
            with open(fn, "w", encoding="utf-8") as fh:
                n = core.write_incident_report(fh, self.cfg, events, opt["from"], opt["to"], opt["when"], hdr,
                                               cov, rows, include_expr=opt["expr"])
        except OSError as e:
            messagebox.showerror(APP, "Could not write the file:\n{}".format(e))
            return
        finally:
            self.job_end(jid)
        self.add_event("INFO", core.CAT_WATCHER, "-", "Incident report written ({} entries, {:%Y-%m-%d %H:%M})".format(
            n, opt["when"]), fn)
        if messagebox.askyesno(APP, "Incident report written ({} history entries):\n{}\n\nOpen it now?".format(n, fn)):
            self._open(fn)

    def _header(self, opt):
        return "{} {} - {} - {} export - computer {} - {:%Y-%m-%d %H:%M:%S}\nPeriod: {:%Y-%m-%d %H:%M} to " \
               "{:%Y-%m-%d %H:%M} (local time)\nNOTE: {}".format(APP, VERSION, SUBTITLE, opt["content"],
                                                                 os.environ.get("COMPUTERNAME", ""), datetime.now(),
                                                                 opt["from"], opt["to"], core.INFERRED_NOTE)

    def _export_history(self, fn, opt):
        rows = []
        for e in self.read_history_range(opt["from"], opt["to"]):
            if e["level"] not in opt["levels"] or e.get("cat") not in opt["cats"]:
                continue
            if e["dl"] not in opt["dls"]:
                continue
            if opt["search"] and opt["search"] not in (e["dl"] + " " + e["event"] + " " + e["detail"]).lower():
                continue
            rows.append(e)
        if opt["fmt"] == "csv":
            with open(fn, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["# " + core.INFERRED_NOTE])
                w.writerow(["Time (local)", "UTC", "Level", "Category", "Data log", "Event", "Detail"])
                for e in rows:
                    w.writerow([e["time"], e["utc"], LEVEL_NAME.get(e["level"], e["level"]), e["cat"], e["dl"],
                                e["event"], e["detail"]])
        else:
            wdl = max([len(e["dl"]) for e in rows] + [8]) + 2
            with open(fn, "w", encoding="utf-8") as fh:
                fh.write(self._header(opt) + "\n")
                fh.write("Severity: {}   Categories: {}   Search: '{}'   Entries: {}\n".format(
                    ", ".join(LEVEL_NAME[k] for k in ("ALARM", "WARN", "OK", "INFO") if k in opt["levels"]),
                    ", ".join(c for c in core.CATEGORIES if c in opt["cats"]), opt["search"], len(rows)))
                fh.write("-" * 120 + "\n")
                fh.write("{:<19}  {:<19}  {:<7}  {:<11}  {:<{w}}{}\n".format(
                    "Local time", "UTC", "Level", "Category", "Data log", "Event", w=wdl))
                for e in rows:
                    fh.write("{:<19}  {:<19}  {:<7}  {:<11}  {:<{w}}{}{}\n".format(
                        e["time"], e["utc"], LEVEL_NAME.get(e["level"], e["level"]), e["cat"], e["dl"], e["event"],
                        "  [" + e["detail"] + "]" if e["detail"] else "", w=wdl))
        return len(rows)

    def _export_coverage(self, fn, opt):
        self.monitor.save_coverage()
        cov = [c for c in core.read_coverage(DATA_DIR, opt["from"].date(), opt["to"].date()) if c[1] in opt["dls"]]
        snoozes = {}
        for e in self.read_history_range(datetime.combine(opt["from"].date(), datetime.min.time()),
                                         datetime.combine(opt["to"].date(), datetime.max.time().replace(microsecond=0))):
            if e.get("cat") == core.CAT_SNOOZE:
                snoozes[e["time"][:10]] = snoozes.get(e["time"][:10], 0) + 1
        if opt["fmt"] == "csv":
            with open(fn, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["# " + core.INFERRED_NOTE])
                w.writerow(["Date", "Data log", "Should log (h:mm)", "Logged (h:mm)", "Coverage %", "Critical",
                            "Snoozes that day (all)"])
                for d, dl, trig, log, iss in cov:
                    w.writerow([d, dl, hms(trig), hms(log), "{:.1f}".format(100 * log / trig) if trig else "",
                                iss, snoozes.get(d, 0)])
        else:
            wdl = max([len(c[1]) for c in cov] + [8]) + 2
            with open(fn, "w", encoding="utf-8") as fh:
                fh.write(self._header(opt) + "\n")
                fh.write("Coverage = time the data set was growing / time it should have been logging.\n")
                fh.write("-" * 90 + "\n")
                fh.write("{:<11} {:<{w}} {:>10} {:>10} {:>9} {:>7}\n".format(
                    "Date", "Data log", "Should", "Logged", "Coverage", "Critical", w=wdl))
                last = None
                for d, dl, trig, log, iss in cov:
                    if last and d != last:
                        fh.write("{:<11} snoozes that day: {}\n\n".format("", snoozes.get(last, 0)))
                    last = d
                    fh.write("{:<11} {:<{w}} {:>10} {:>10} {:>9} {:>7}\n".format(
                        d, dl, hms(trig), hms(log), "{:.1f} %".format(100 * log / trig) if trig else "-", iss, w=wdl))
                if last:
                    fh.write("{:<11} snoozes that day: {}\n".format("", snoozes.get(last, 0)))
        return len(cov)

    # ---------------------------------------------------------------- configuration file
    def save_cfg(self, quiet=False):
        if not quiet and not self.locked and not self._apply_pending_settings():
            return
        try:
            core.save_config(self.cfg, CFG_PATH)
        except OSError as e:
            messagebox.showerror(APP, "Could not save the configuration:\n{}".format(e))
            return
        self.monitor.reload(self.cfg)
        self.refresh_hist_dl()
        if not quiet:
            messagebox.showinfo(APP, "Configuration saved to\n{}".format(CFG_PATH))

    def import_cfg(self):
        if self.locked:
            messagebox.showinfo(APP, "Unlock setup first.")
            return
        fn = filedialog.askopenfilename(filetypes=[("Watcher configuration", "*.json")])
        if not fn:
            return
        try:
            new, notes = core.load_config(fn)
        except Exception as e:
            messagebox.showerror(APP, "Not a valid configuration file:\n{}".format(e))
            return
        if not messagebox.askyesno(APP, "Replace the current configuration with\n{}\n\nDatalog folder: {}\n{} data "
                                        "logs, {} controllers, {} tags.\n\nThe current PIN is kept.".format(
                                            fn, new["settings"]["log_root"] or "(not set)", len(new["datalogs"]),
                                            len(new["plcs"]), sum(len(p["tags"]) for p in new["plcs"]))):
            return
        new["settings"]["pin_hash"] = self.cfg["settings"]["pin_hash"]
        self.cfg = new
        self.cur_dl = None
        self.save_cfg(quiet=True)
        self.refresh_all()
        self.add_event("INFO", core.CAT_SETUP, "-", "Configuration imported", fn)
        for n in notes:
            self.add_event("WARN", core.CAT_SETUP, "-", n)

    def export_cfg(self):
        fn = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Watcher configuration", "*.json")],
                                          initialfile="LogLoss_config_{}_{:%Y%m%d}.json".format(
                                              os.environ.get("COMPUTERNAME", "PC"), datetime.now()))
        if fn:
            if not self.locked and not self._apply_pending_settings():
                return
            core.save_config(self.cfg, fn)
            self.add_event("INFO", core.CAT_SETUP, "-", "Configuration exported", fn)
            messagebox.showinfo(APP, "Exported to\n{}\n\nContains: all settings (datalog folder: {}), {} controllers "
                                     "with their tags and {} data logs. Handle it as confidential.".format(
                                         fn, self.cfg["settings"]["log_root"] or "not set", len(self.cfg["plcs"]),
                                         len(self.cfg["datalogs"])))

    def refresh_hist_dl(self):
        self.cb_hdl["values"] = ["All", "-"] + [d["name"] for d in self.cfg["datalogs"]] + \
                                ["PLC " + p["topic"] for p in self.cfg["plcs"]]

    def refresh_all(self):
        self.settings_load()
        self.plc_refresh()
        self.refresh_folders()
        self.cur_dl = None
        self.dl_refresh_list(select=0 if self.cfg["datalogs"] else None)
        self.refresh_hist_dl()

    # ---------------------------------------------------------------- watcher downtime / housekeeping
    def _check_downtime(self):
        try:
            txt = HEARTBEAT.read_text(encoding="utf-8").strip().split("|")
            last = float(txt[0])
            clean = len(txt) > 1 and txt[1] == "exit"
        except Exception:
            return
        gap = time.time() - last
        if gap > 180:
            a = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M")
            b = datetime.now().strftime("%Y-%m-%d %H:%M")
            how = "closed by user" if clean else "not closed properly - crash, power loss or Windows shutdown"
            self.add_event("WARN", core.CAT_WATCHER, "-", "{} was not running from {} to {}".format(APP, a, b),
                           "{:.1f} h, {}".format(gap / 3600, how))

    def _beat(self, final=False):
        try:
            HEARTBEAT.write_text("{:.0f}{}".format(time.time(), "|exit" if final else ""), encoding="utf-8")
        except OSError:
            pass

    def _housekeeping(self):
        today = date.today().isoformat()
        if self.purge_day != today:
            self.purge_day = today
            n = core.purge_old_files(DATA_DIR, self.cfg["settings"].get("history_days", 365))
            if n:
                self.add_event("INFO", core.CAT_WATCHER, "-", "Old history files deleted", "{} files older than "
                               "{:g} days".format(n, float(self.cfg["settings"]["history_days"])))
        if time.time() - self.last_beat >= 60:
            self.last_beat = time.time()
            self._beat()

    # ---------------------------------------------------------------- monitoring loop
    def start_monitoring(self):
        if not self.running.is_set():
            self.running.set()
            on = [d["name"] for d in self.cfg["datalogs"] if d.get("enabled", True)]
            self.add_event("INFO", core.CAT_WATCHER, "-", "Monitoring started - {} enabled data log(s)".format(len(on)),
                           ", ".join(on) if on else "no data log enabled")

    def stop_monitoring(self):
        if self.running.is_set() and self._protected("Stop monitoring?"):
            self.running.clear()
            self.banner.hide()
            self.add_event("WARN", core.CAT_WATCHER, "-", "Monitoring stopped by " + core.windows_user())

    def _worker(self):
        while not self.quit_flag.is_set():
            if self.running.is_set():
                try:
                    self.monitor.step()
                    self.last_check = datetime.now()
                except Exception as e:
                    self.add_event("WARN", core.CAT_WATCHER, "-", "Watcher internal error", repr(e))
            self.wake.wait(max(2.0, float(self.cfg["settings"]["check_sec"])))
            self.wake.clear()

    def _drain_events(self):
        try:
            while True:
                self._store_event(self.ev_q.get_nowait())
        except queue.Empty:
            pass

    def _poll(self):
        """Runs every 0.5 s (1 s when hidden). Must never stop: errors are logged, and the
        next run is always scheduled unless the application is closing."""
        try:
            while not self.closing:
                try:
                    cb = self.ui_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    cb()
                except Exception as e:
                    self._poll_error("callback", e)
            if self.closing:
                return                      # window destroyed (e.g. Exit from the tray menu)
            self._drain_events()
            snap = self.monitor.snapshot()
            if not self.hidden:
                self._update_status(snap)
                self._update_chrome(snap)
            else:
                self._autolock()
            alarms = snap["alarms"] if self.running.is_set() else []
            self.banner.update([] if self.cfg["settings"]["log_only"] else [(a[1], a[2]) for a in alarms])
            self._update_tray(snap, alarms)
            self._housekeeping()
        except Exception as e:
            if self.closing:
                return
            self._poll_error("update", e)
        if not self.closing:
            try:
                self.root.after(1000 if self.hidden else 500, self._poll)
            except tk.TclError:
                pass

    def _poll_error(self, where, e):
        """Log a GUI error once per minute at most, keep running."""
        now = time.time()
        if now - getattr(self, "_last_poll_err", 0) > 60:
            self._last_poll_err = now
            self.add_event("WARN", core.CAT_WATCHER, "-", "Watcher display error ({}) - monitoring continues"
                           .format(where), repr(e))

    def _update_tray(self, snap, alarms):
        rows = snap["rows"]
        n_iss = sum(1 for a in alarms if a[0] == "ALARM")
        n_unk = sum(1 for r in rows if r["state"] == "Unknown")
        n_start = sum(1 for r in rows if r["state"] == "Started")
        n_stop = sum(1 for r in rows if r["state"] == "Stopped")
        if not self.running.is_set():
            state, tip = "off", "{} - monitoring STOPPED".format(APP)
        else:
            state = "issue" if n_iss else ("warn" if (alarms or n_unk) else "ok")
            tip = "{}\n{} started, {} stopped, {} critical{}".format(
                APP, n_start, n_stop, n_iss, ", {} unknown".format(n_unk) if n_unk else "")
        self.tray.set_state(state, tip)

    def _autolock(self):
        s = self.cfg["settings"]
        if not self.locked and s.get("lock_enabled", True):
            if s.get("lock_min", 10) * 60 - (time.time() - self.last_activity) <= 0:
                self.set_lock(True, reason="auto-lock after {:g} min without activity".format(float(s["lock_min"])))

    def _update_chrome(self, snap):
        s = self.cfg["settings"]
        on = self.running.is_set()
        self._badge("mon", "ON" if on else "STOPPED", C_OK if on else C_BAD)
        val = snap["engine"]
        self._badge("engine", "?" if val is None else ("RUNNING" if val else "NOT RUNNING"),
                    C_GREY if val is None else (C_OK if val else C_BAD))
        val = snap["client"]
        if val and not snap.get("gate"):
            self._badge("client", "STARTING", C_WARN)
        else:
            self._badge("client", "?" if val is None else ("RUNNING" if val else "NOT RUNNING"),
                        C_GREY if val is None else (C_OK if val else C_GREY))
        n = sum(1 for a in snap["alarms"] if a[0] == "ALARM")
        w = len(snap["alarms"]) - n
        self._badge("alarms", "{} critical".format(n) if n else ("{} warning".format(w) if w else "none"),
                    C_BAD if n else (C_WARN if w else C_OK))
        self._autolock()
        if self.locked:
            self.sb_right.config(text="Setup locked")
            self._badge("lock", "LOCKED", C_GREY)
        else:
            if s.get("lock_enabled", True):
                left = max(0, s.get("lock_min", 10) * 60 - (time.time() - self.last_activity))
                self.sb_right.config(text="Setup unlocked  -  auto-lock in {}:{:02d}".format(int(left // 60), int(left % 60)))
            else:
                self.sb_right.config(text="Setup unlocked  -  auto-lock OFF")
            self._badge("lock", "UNLOCKED", C_WARN)
        chk = self.last_check.strftime("%H:%M:%S") if self.last_check else "-"
        mode = "LOG-ONLY (no popups)" if s["log_only"] else "popups on"
        self.sb_left.config(text="Last check {}    |    {} data logs    |    {} controllers    |    mode: {}".format(
            chk, len(self.cfg["datalogs"]), len(self.cfg["plcs"]), mode))
        if self.nb.index("current") == 3 and not self.locked:
            pend = self.settings_pending()
            if pend:
                self.lbl_set.config(text="●  Changes not applied yet", foreground=C_WARN)
            elif str(self.lbl_set.cget("text")).startswith("●"):
                self.lbl_set.config(text="")
        if self.nb.index("current") == 1 and not self.locked:
            if self.dl_modified():
                self.lbl_dl.config(text="●  Unsaved changes", foreground=C_WARN)
            elif str(self.lbl_dl.cget("text")).startswith("●"):
                self.lbl_dl.config(text="")

    def _update_status(self, snap, force=False):
        rows = {r["name"]: r for r in snap["rows"]}
        counts, want = {}, []
        hidden = 0
        for d in self.cfg["datalogs"]:
            if not d.get("enabled", True):
                hidden += 1
                continue
            r = rows.get(d["name"])
            state = r["state"] if r else "waiting"
            counts[state] = counts.get(state, 0) + 1
            if self.v_problems.get() and state not in ("Critical", "Warning", "Unknown"):
                continue
            want.append(d["name"])
        if force or set(self.st_tree.get_children()) != set(want):
            self.st_tree.delete(*self.st_tree.get_children())
            for n in want:
                self.st_tree.insert("", "end", iid=n, values=("", n))
        for n in want:
            r = rows.get(n)
            if r is None:
                self.st_tree.item(n, values=("…", n, "", "", "", "", "", "", "waiting for first check"), tags=())
                continue
            self.st_tree.item(n, values=(r["state"], n, r["type"], r["trigger"], r["size"], r["age"], r["file"],
                                         r["ftime"], r["detail"]), tags=(r["state"],))
        parts = ["{} {}".format(counts[k], k.lower()) for k in ("Started", "Stopped", "Critical", "Warning",
                                                                "Unknown", "Paused")
                 if counts.get(k)]
        if getattr(self.st_tree, "_sort", None):
            sort_tree(self.st_tree, toggle=False)
        txt = "   ".join(parts) if parts else "no enabled data logs"
        if hidden:
            txt += "      ({} disabled, not shown)".format(hidden)
        self.st_summary.config(text=txt)

    # ---------------------------------------------------------------- misc
    def test_banner(self):
        pos = None
        if not self.locked and hasattr(self, "sv"):
            pos = self.sv["banner_pos"][0].get() or None     # value in Settings, even if not applied yet
        self.banner.test(pos)
        self.add_event("INFO", core.CAT_WATCHER, "-", "Test popup shown ({})".format(
            pos or self.cfg["settings"].get("banner_pos", "bottom-left")))

    def _open(self, path):
        try:
            os.startfile(str(path))
        except Exception as e:
            messagebox.showerror(APP, str(e))

    def exit_app(self):
        if not self._protected("Exit {}?".format(APP)):
            return
        self.add_event("WARN", core.CAT_WATCHER, "-", "{} closed by {}".format(APP, core.windows_user()))
        self._drain_events()
        self.monitor.save_coverage()
        self._beat(final=True)
        self.closing = True
        self.quit_flag.set()
        self.wake.set()
        self.tray.stop()
        self.banner.hide()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App(simulate="--simulate" in sys.argv, minimized="--minimized" in sys.argv).run()
