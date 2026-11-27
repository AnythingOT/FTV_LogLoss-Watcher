"""
llw_core.py - engine of LogLoss Watcher (for FTView SE DataLog Models). No GUI in here.

  * Expression : FTView-style expressions, evaluated with 3-state logic (True/False/Unknown)
  * PLC        : read-only access through pylogix (or a simulator for testing)
  * Files      : growth of the newest data set of each datalog folder (names + os.stat only)
  * Monitor    : compares what SHOULD happen (trigger) with what the files DO,
                 produces history events, issues, retention checks and coverage figures

PLC access is READ-ONLY. This module never calls Write(). Python 3.8+ compatible.
"""
import ctypes, csv, hashlib, json, os, re, subprocess, threading, time
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

# ----------------------------------------------------------------------------------
# Types and defaults
# ----------------------------------------------------------------------------------
T_MACRO, T_EXPR = 1, 2
INFERRED_NOTE = ("Start/stop is inferred from file growth. Exact datalog start/stop times are in the "
                 "DAT files.")
EVENT_HINT = ("Trigger is 1 but the DL is not growing - the Event Detector will not restart it until the "
              "trigger goes 0 -> 1. Restart the events (EventOff / EventOn).")
TYPE_NAMES = {T_MACRO: "Macro - started by the startup macro (always on)",
              T_EXPR: "EXPR - started / stopped by an event expression"}
TYPE_SHORT = {T_MACRO: "Macro", T_EXPR: "EXPR"}

# event categories
CAT_STARTSTOP, CAT_ISSUE, CAT_PLC, CAT_SNOOZE = "Start/Stop", "Issue", "PLC", "Snooze"
CAT_WATCHER, CAT_RETENTION, CAT_HANG, CAT_SETUP = "Watcher", "Retention", "Engine hang", "Setup"
CAT_SERVICE = "Service"
CATEGORIES = [CAT_STARTSTOP, CAT_ISSUE, CAT_HANG, CAT_SERVICE, CAT_PLC, CAT_RETENTION, CAT_SNOOZE,
              CAT_WATCHER, CAT_SETUP]

DEFAULT_SETTINGS = {
    "log_root": "",
    "check_sec": 10,
    "runtime_grace_sec": 60,         # gate: checks start this long after the SE client (runtime) starts
    "client_exe": "DisplayClient.exe",
    "engine_exe": "DatalogServ.exe",
    "banner_pos": "bottom-left",
    "snooze_min": 30,
    "progress_min": 60,
    "popup_on_not_stopped": False,
    "log_only": False,
    "plc_timeout": 3.0,
    "plc_retry_sec": 60,
    "keep_days": 90,                 # FTView "delete data sets older than" (0 = do not check)
    "history_days": 365,             # watcher's own history / coverage files
    "lock_enabled": True,
    "lock_min": 10,
    "pin_hash": hashlib.sha256(b"1234").hexdigest(),
}
DEFAULT_DL = {"name": "", "type": T_MACRO, "enabled": True, "expr": "",
              "stale_min": 3, "start_grace_min": 2, "stop_grace_min": 3}


def new_config():
    return {"version": 3, "settings": dict(DEFAULT_SETTINGS), "plcs": [], "datalogs": []}


def load_config(path):
    """Loads v1 or v2 config. Returns (cfg, notes) - notes describe automatic conversions."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg, notes = new_config(), []
    cfg["settings"].update({k: v for k, v in data.get("settings", {}).items() if k in DEFAULT_SETTINGS})
    if data.get("constants"):
        notes.append("HMI tag values are no longer used - HMI tags in expressions are treated as empty "
                      "(a comparison with text is FALSE, a tag on its own is FALSE)")
    for p in data.get("plcs", []):
        cfg["plcs"].append({"topic": p.get("topic", ""), "ip": p.get("ip", ""),
                            "slot": int(p.get("slot", 0)), "conn_size": int(p.get("conn_size", 0) or 0),
                            "tags": list(p.get("tags", []))})
    for d in data.get("datalogs", []):
        n = dict(DEFAULT_DL)
        n.update({k: v for k, v in d.items() if k in DEFAULT_DL})
        t = d.get("type", T_MACRO)
        if t == 3:                                            # old "single PLC tag" type
            n["type"], n["expr"] = T_EXPR, "{" + d.get("tag", "") + "}" if d.get("tag") else ""
            notes.append(f"{n['name']}: tag type converted to EXPR {n['expr']}")
        elif t == 4:                                          # old "log on change" type
            n["type"], n["enabled"] = T_MACRO, False
            notes.append(f"{n['name']}: 'log on change' type removed - set to Macro and DISABLED")
        elif t not in (T_MACRO, T_EXPR):
            n["type"] = T_MACRO
        cfg["datalogs"].append(n)
    return cfg, notes


def save_config(cfg, path):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def pin_hash(pin):
    return hashlib.sha256(pin.encode()).hexdigest()


def ref_key(topic, tag):
    """Canonical key of a PLC tag reference: '[plc1]mytag.member'."""
    return "[{}]{}".format(topic.strip(), tag.strip()).lower()


def split_ref(ref):
    """'[PLC1]MyTag.X' -> ('PLC1', 'MyTag.X') ; anything else -> (None, ref)."""
    m = re.match(r"^\s*\[([^\]]+)\]\s*(.+?)\s*$", ref)
    return (m.group(1), m.group(2)) if m else (None, ref.strip())


def now_stamps(ts=None):
    """-> (local 'YYYY-MM-DD HH:MM:SS', utc 'YYYY-MM-DD HH:MM:SS')"""
    ts = time.time() if ts is None else ts
    return (datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


# ----------------------------------------------------------------------------------
# Expression (FTView subset)  IF/THEN/ELSE, AND/OR/NOT, && || !, = == <> != < > <= >=,
#                             EQ NE LT GT LE GE, + - * /, ( ), numbers, "strings", {tags}
# ----------------------------------------------------------------------------------
class ExprError(Exception):
    pass


_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+) | (?P<tag>\{[^}]*\}) | (?P<str>"[^"]*") |
    (?P<num>\d+\.\d*|\.\d+|\d+) |
    (?P<op>==|<>|!=|<=|>=|&&|\|\||[=<>!()+\-*/]) |
    (?P<id>[A-Za-z_][A-Za-z_0-9]*) | (?P<bad>.)
""", re.X)
_KW_OPS = {"EQ": "==", "NE": "<>", "LT": "<", "GT": ">", "LE": "<=", "GE": ">="}


class Expression:
    def __init__(self, text):
        self.text = text
        self.toks = self._tokenize(text)
        self.i = 0
        self.refs = {}
        if not self.toks:
            raise ExprError("expression is empty")
        self.ast = self._expr()
        if self.i < len(self.toks):
            raise ExprError("unexpected '{}'".format(self.toks[self.i][1]))

    def _tokenize(self, s):
        out = []
        for m in _TOKEN_RE.finditer(s):
            k, v = m.lastgroup, m.group()
            if k == "ws":
                continue
            if k == "bad":
                raise ExprError("invalid character '{}'".format(v))
            if k == "id":
                u = v.upper()
                if u in _KW_OPS:
                    k, v = "op", _KW_OPS[u]
                elif u in ("IF", "THEN", "ELSE", "AND", "OR", "NOT", "TRUE", "FALSE"):
                    k, v = "kw", u
                else:
                    raise ExprError("unknown word '{}' (tags must be inside {{ }})".format(v))
            out.append((k, v))
        return out

    def _peek(self, *vals):
        if self.i < len(self.toks) and self.toks[self.i][0] in ("op", "kw") and self.toks[self.i][1] in vals:
            return self.toks[self.i][1]
        return None

    def _take(self, *vals):
        v = self._peek(*vals)
        if v:
            self.i += 1
        return v

    def _need(self, val):
        if not self._take(val):
            got = self.toks[self.i][1] if self.i < len(self.toks) else "end of expression"
            raise ExprError("expected {} but found '{}'".format(val, got))

    def _expr(self):
        if self._take("IF"):
            c = self._expr(); self._need("THEN")
            t = self._expr(); self._need("ELSE")
            return ("if", c, t, self._expr())
        return self._or()

    def _or(self):
        a = self._and()
        while self._take("OR", "||"):
            a = ("or", a, self._and())
        return a

    def _and(self):
        a = self._not()
        while self._take("AND", "&&"):
            a = ("and", a, self._not())
        return a

    def _not(self):
        if self._take("NOT", "!"):
            return ("not", self._not())
        return self._cmp()

    def _cmp(self):
        a = self._add()
        op = self._take("==", "=", "<>", "!=", "<", ">", "<=", ">=")
        return ("cmp", op, a, self._add()) if op else a

    def _add(self):
        a = self._mul()
        while True:
            op = self._take("+", "-")
            if not op:
                return a
            a = ("ar", op, a, self._mul())

    def _mul(self):
        a = self._unary()
        while True:
            op = self._take("*", "/")
            if not op:
                return a
            a = ("ar", op, a, self._unary())

    def _unary(self):
        if self._take("-"):
            return ("neg", self._unary())
        return self._primary()

    def _primary(self):
        if self.i >= len(self.toks):
            raise ExprError("expression ends too early")
        k, v = self.toks[self.i]
        if k == "num":
            self.i += 1
            return ("val", float(v))
        if k == "str":
            self.i += 1
            return ("val", v[1:-1])
        if k == "kw" and v in ("TRUE", "FALSE"):
            self.i += 1
            return ("val", 1.0 if v == "TRUE" else 0.0)
        if k == "tag":
            self.i += 1
            inner = v[1:-1].strip()
            if not inner:
                raise ExprError("empty tag { }")
            key = ref_key(*split_ref(inner)) if inner.startswith("[") else inner.lower()
            self.refs[key] = inner
            return ("ref", key)
        if self._take("("):
            a = self._expr()
            self._need(")")
            return a
        if self._peek("IF"):
            return self._expr()
        raise ExprError("unexpected '{}'".format(v))

    def plc_refs(self):
        return {k: v for k, v in self.refs.items() if v.startswith("[")}

    def const_refs(self):
        return {k: v for k, v in self.refs.items() if not v.startswith("[")}

    def evaluate(self, values):
        """values: {ref_key: value or None}. Returns True / False / None (unknown)."""
        return truth(_ev(self.ast, values))


def truth(v):
    if v is None:
        return None
    if isinstance(v, str):
        return len(v) > 0
    return v != 0


def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    return v


def _ev(n, vals):
    t = n[0]
    if t == "val":
        return n[1]
    if t == "ref":
        return _num(vals.get(n[1]))
    if t == "not":
        b = truth(_ev(n[1], vals))
        return None if b is None else (0.0 if b else 1.0)
    if t in ("and", "or"):
        a, b = truth(_ev(n[1], vals)), truth(_ev(n[2], vals))
        if t == "and":
            if a is False or b is False:
                return 0.0
            return None if (a is None or b is None) else 1.0
        if a is True or b is True:
            return 1.0
        return None if (a is None or b is None) else 0.0
    if t == "if":
        c = truth(_ev(n[1], vals))
        tv, fv = _ev(n[2], vals), _ev(n[3], vals)
        if c is None:
            return tv if tv == fv else None
        return tv if c else fv
    if t == "neg":
        a = _ev(n[1], vals)
        return None if a is None or isinstance(a, str) else -a
    a, b = _ev(n[2], vals), _ev(n[3], vals)
    if a is None or b is None:
        return None
    if t == "cmp":
        try:
            r = {"==": a == b, "=": a == b, "<>": a != b, "!=": a != b,
                 "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[n[1]]
        except TypeError:
            r = False
        return 1.0 if r else 0.0
    if isinstance(a, str) or isinstance(b, str):
        return a + b if (n[1] == "+" and isinstance(a, str) and isinstance(b, str)) else None
    if n[1] == "+":
        return a + b
    if n[1] == "-":
        return a - b
    if n[1] == "*":
        return a * b
    return None if b == 0 else a / b


HMI_TAG_VALUE = ""          # HMI (non-PLC) tags in expressions: empty -> "x == 'text'" is FALSE, tag alone FALSE


def hmi_values(expr):
    """Values for the HMI tags of an expression (always empty)."""
    return {k: HMI_TAG_VALUE for k in expr.const_refs()}


def label_event(dl, event):
    """Put the data log name in front of the message text (also in exports / popups)."""
    if dl and dl != "-" and not event.startswith(dl + ":"):
        return "{}: {}".format(dl, event)
    return event


def parse_const(text):
    s = str(text).strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return s[1:-1]
    try:
        return float(s)
    except ValueError:
        return s


# ----------------------------------------------------------------------------------
# PLC access (read only)
# ----------------------------------------------------------------------------------
try:
    from pylogix import PLC as _PylogixPLC          # noqa
    HAVE_PYLOGIX = True
except Exception:
    _PylogixPLC = None
    HAVE_PYLOGIX = False


class SimPLC:
    """Fake PLC for testing without hardware."""
    store = {}                                        # (topic_lower, tag_lower) -> value
    offline = set()                                   # topics (lower) that do not answer

    def __init__(self, topic, ip, slot, timeout=3.0, conn_size=0):
        self.topic = topic.lower()

    def read(self, tags):
        if self.topic in SimPLC.offline:
            return {t: None for t in tags}, "No connection (simulated)"
        return {t: SimPLC.store.get((self.topic, t.lower()), False) for t in tags}, "OK (simulated)"

    def test(self):
        if self.topic in SimPLC.offline:
            return False, "No connection (simulated)"
        return True, "OK (simulated)"

    def tag_list(self):
        return sorted({t for (tp, t) in SimPLC.store if tp == self.topic})

    def close(self):
        pass


class LogixPLC:
    """Read-only wrapper around pylogix. Only Read / GetTagList / GetPLCTime / GetModuleProperties."""

    def __init__(self, topic, ip, slot, timeout=3.0, conn_size=0):
        if not HAVE_PYLOGIX:
            raise RuntimeError("pylogix is not installed (pip install --user pylogix)")
        self.ip, self.slot, self.timeout = ip, int(slot), float(timeout)
        self.conn_size = int(conn_size or 0)          # 0 = pylogix default; e.g. 504 for Logix Emulate
        self.comm = None

    def _conn(self):
        if self.comm is None:
            c = _PylogixPLC()
            c.IPAddress = self.ip
            c.ProcessorSlot = self.slot
            if self.conn_size:
                c.ConnectionSize = self.conn_size
            try:
                c.SocketTimeout = self.timeout
            except Exception:
                pass
            self.comm = c
        return self.comm

    def read(self, tags):
        if not tags:
            return {}, "OK"
        out = {}
        try:
            res = self._conn().Read(list(tags))
            if not isinstance(res, list):
                res = [res]
            bad = 0
            for t, r in zip(tags, res):
                ok = getattr(r, "Status", "") == "Success"
                out[t] = r.Value if ok else None
                bad += 0 if ok else 1
            status = "OK" if bad == 0 else "{} tag(s) failed: {}".format(bad, getattr(res[0], "Status", ""))
            if bad == len(tags):
                self.close()
            return out, status
        except Exception as e:
            self.close()
            return {t: None for t in tags}, "No connection ({})".format(e)

    def test(self):
        t0 = time.time()
        try:
            r = self._conn().GetPLCTime()
            ms = (time.time() - t0) * 1000
            if getattr(r, "Status", "") != "Success":
                self.close()
                return False, "Failed: {}".format(getattr(r, "Status", "?"))
            name = ""
            try:
                m = self._conn().GetModuleProperties(self.slot)
                if getattr(m, "Status", "") == "Success":
                    name = "  {} v{}".format(m.Value.ProductName, m.Value.Revision)
            except Exception:
                pass
            return True, "OK {:.0f} ms{}".format(ms, name)
        except Exception as e:
            self.close()
            return False, "No connection ({})".format(e)

    def tag_list(self):
        r = self._conn().GetTagList(True)
        if getattr(r, "Status", "") != "Success":
            raise RuntimeError(getattr(r, "Status", "tag list failed"))
        names = []
        udts = getattr(self.comm, "UDTByName", {}) or {}
        for t in r.Value:
            n = getattr(t, "TagName", "")
            if not n or n.startswith("__"):
                continue
            names.append(n)
            u = udts.get(getattr(t, "DataType", ""))
            for f in getattr(u, "Fields", []) or []:
                fn = getattr(f, "TagName", "")
                if fn and not fn.startswith("ZZZZ"):
                    names.append(n + "." + fn)
        return sorted(set(names), key=str.lower)

    def close(self):
        try:
            if self.comm:
                self.comm.Close()
        except Exception:
            pass
        self.comm = None


def make_plc(p, settings, simulate):
    cls = SimPLC if simulate else LogixPLC
    return cls(p["topic"], p["ip"], p.get("slot", 0), settings.get("plc_timeout", 3.0), p.get("conn_size", 0))


# ----------------------------------------------------------------------------------
# Windows helpers (no admin, no extra processes)
# ----------------------------------------------------------------------------------
def _toolhelp_entries():
    """[(lower-case exe name, pid)] of all processes."""
    k32 = ctypes.windll.kernel32

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                    ("th32ProcessID", ctypes.c_uint32), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
                    ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260)]
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k32.CreateToolhelp32Snapshot(2, 0)                 # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise OSError("snapshot failed")
    out = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((e.szExeFile.lower(), int(e.th32ProcessID)))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def _toolhelp_processes():
    return {n for n, _ in _toolhelp_entries()}


def _process_start(pid):
    """Start time (epoch) of a process, or None if Windows does not allow us to ask
    (usual for service processes when the user is not an administrator)."""
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = None
        for access in (0x1000, 0x0400):          # QUERY_LIMITED_INFORMATION (Vista+), QUERY_INFORMATION
            h = k32.OpenProcess(access, 0, pid)
            if h:
                break
        if not h:
            return None
        try:
            ft = [ctypes.c_uint64() for _ in range(4)]
            if not k32.GetProcessTimes(ctypes.c_void_p(h), *[ctypes.byref(f) for f in ft]):
                return None
            return (ft[0].value - 116444736000000000) / 1e7
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def process_info(exe):
    """{pid: start epoch or None} of all processes named exe, or None if unknown.
    Read-only: Toolhelp snapshot (tasklist as fallback) plus GetProcessTimes where allowed."""
    exe = exe.lower()
    try:
        pids = [pid for n, pid in _toolhelp_entries() if n == exe]
    except Exception:
        try:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq " + exe],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                                 timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            pids = [int(r[1]) for r in csv.reader(out.splitlines()) if len(r) > 1 and r[0].lower() == exe]
        except Exception:
            return None
    return {pid: _process_start(pid) for pid in pids}


def running_processes():
    """Lower-case process names, or None if unknown."""
    try:
        return _toolhelp_processes()
    except Exception:
        pass
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return {row[0].lower() for row in csv.reader(out.splitlines()) if row}
    except Exception:
        return None


def uptime_min():
    try:
        f = ctypes.windll.kernel32.GetTickCount64
        f.restype = ctypes.c_uint64
        return f() / 60000.0
    except Exception:
        return 1e9


def set_low_priority():
    """Run below normal priority so FactoryTalk always comes first."""
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)    # BELOW_NORMAL_PRIORITY_CLASS
        return True
    except Exception:
        return False


def windows_user():
    return os.environ.get("USERNAME", "?")


# ----------------------------------------------------------------------------------
# Data set files
# ----------------------------------------------------------------------------------
_WIN_STAT = None


def _win_stat_api():
    """CreateFileW + GetFileInformationByHandle, set up once."""
    global _WIN_STAT
    if _WIN_STAT is None:
        try:
            from ctypes import wintypes as W
            k32 = ctypes.windll.kernel32

            class BHFI(ctypes.Structure):
                _fields_ = [("attr", W.DWORD), ("ctime", W.FILETIME), ("atime", W.FILETIME),
                            ("mtime", W.FILETIME), ("vol", W.DWORD), ("size_hi", W.DWORD), ("size_lo", W.DWORD),
                            ("links", W.DWORD), ("idx_hi", W.DWORD), ("idx_lo", W.DWORD)]
            k32.CreateFileW.restype = W.HANDLE
            k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
            k32.GetFileInformationByHandle.argtypes = [W.HANDLE, ctypes.POINTER(BHFI)]
            k32.CloseHandle.argtypes = [W.HANDLE]
            _WIN_STAT = (k32, BHFI)
        except Exception:
            _WIN_STAT = False
    return _WIN_STAT


def file_stat(path):
    """(size, mtime) of a file that may be OPEN AND BEING WRITTEN by FactoryTalk.

    The folder listing (Explorer, and os.stat on Windows 11 with Python 3.12+) can show an old
    size/date for an open file. Asking the file itself through a handle gives the current values.
    The handle only has FILE_READ_ATTRIBUTES access and shares read/write/delete, so it never
    blocks FactoryTalk from writing, renaming or deleting the file. Closed again immediately."""
    api = _win_stat_api()
    if api:
        k32, BHFI = api
        h = k32.CreateFileW(str(path), 0x80, 0x7, None, 3, 0, None)   # READ_ATTRIBUTES, share all, OPEN_EXISTING
        if h and h != ctypes.c_void_p(-1).value:
            try:
                info = BHFI()
                if k32.GetFileInformationByHandle(h, ctypes.byref(info)):
                    size = (info.size_hi << 32) | info.size_lo
                    ft = (info.mtime.dwHighDateTime << 32) | info.mtime.dwLowDateTime
                    return size, (ft - 116444736000000000) / 1e7
            finally:
                k32.CloseHandle(h)
    st = os.stat(str(path))
    return st.st_size, st.st_mtime

_SET_RE = re.compile(r"^(\d{4}) (\d{2}) (\d{2}) (\d{4}) ")


def scan_folder(folder):
    """Reads the folder listing (names only) and stats ONLY the files of the newest data set.
    -> dict(newest=prefix, size=bytes, file=name of most recently written file, ftime=mtime,
            oldest=prefix, sets=count)  or None if no data set files."""
    sets = {}
    try:
        with os.scandir(str(folder)) as it:
            for e in it:
                m = _SET_RE.match(e.name)
                if m and e.name[-4:].upper() == ".DAT":
                    sets.setdefault(e.name[:15], []).append(e.name)
    except OSError:
        return None
    if not sets:
        return None
    newest = max(sets)
    size, best, bt = 0, None, 0.0
    for n in sets[newest]:
        try:
            fsize, fmtime = file_stat(os.path.join(str(folder), n))   # current values, even while written
        except OSError:
            continue
        size += fsize
        if fmtime >= bt:
            best, bt = n, fmtime
    return {"newest": newest, "size": size, "file": best, "ftime": bt,
            "oldest": min(sets), "sets": len(sets)}


def set_date(prefix):
    m = _SET_RE.match(prefix + " ")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def fmt_size(b):
    if b is None:
        return "-"
    return "{:.1f} MB".format(b / 1048576.0) if b >= 1048576 else "{:.0f} KB".format(b / 1024.0)


def fmt_age(sec):
    if sec is None:
        return "-"
    if sec < 90:
        return "{:.0f} s ago".format(sec)
    if sec < 5400:
        return "{:.0f} min ago".format(sec / 60)
    return "{:.1f} h ago".format(sec / 3600)


# ----------------------------------------------------------------------------------
# Monitor
# ----------------------------------------------------------------------------------
class DLState:
    def __init__(self):
        self.scan = None
        self.size = None
        self.set = None
        self.last_growth = None
        self.set_changed_at = None
        self.expected = "init"
        self.exp_since = None
        self.running = False
        self.alarm = None
        self.alarm_level = None
        self.last_progress = 0
        self.stop_since = None
        self.stop_warned = False
        self.stopped_logged = True
        self.retention_day = None
        self.folder_problem = None


class Monitor:
    """Call step() periodically from ONE worker thread. snapshot() is thread-safe."""

    def __init__(self, cfg, simulate=False, emit=None, clock=time.time,
                 procs_fn=running_processes, uptime_fn=uptime_min, data_dir=None, pinfo_fn=process_info):
        self.lock = threading.RLock()
        self.simulate = simulate
        self.emit = emit or (lambda ev: None)
        self.clock, self.procs_fn, self.uptime_fn = clock, procs_fn, uptime_fn
        self.pinfo_fn = pinfo_fn
        self.data_dir = Path(data_dir) if data_dir else None
        self.svc = self._load_svc()        # last known PC boot time + datalog service process ids
        self.states, self.plc_objs, self.plc_status, self.plc_next = {}, {}, {}, {}
        self.values, self.rows, self.alarms = {}, [], []
        self.engine_ok = self.client_ok = None
        self.engine_alarm = False
        self.runtime_since = None        # time the SE client was first seen running
        self.gate_open = None
        self.gate_text = "waiting for runtime"
        self.recent_issues = []
        self.last_hang = 0
        self.last_step = None
        self.cov = {}                      # day -> {dl: {"trig":s, "log":s, "issues":n}}
        self.cov_saved = 0
        self._load_coverage()
        self.reload(cfg)

    # -- configuration ----------------------------------------------------------------
    def reload(self, cfg):
        with self.lock:
            self.cfg = json.loads(json.dumps(cfg))
            self.s = self.cfg["settings"]
            for o in self.plc_objs.values():
                o.close()
            self.plc_objs, self.plc_next = {}, {}
            self.exprs, self.expr_err = {}, {}
            for d in self.cfg["datalogs"]:
                if d["type"] == T_EXPR:
                    try:
                        self.exprs[d["name"]] = Expression(d["expr"])
                    except ExprError as e:
                        self.expr_err[d["name"]] = str(e)
            names = {d["name"] for d in self.cfg["datalogs"]}
            self.states = {n: st for n, st in self.states.items() if n in names}
            for n in names:
                self.states.setdefault(n, DLState())
            self.hmi = {}
            for e in self.exprs.values():
                self.hmi.update(hmi_values(e))

    def _ev(self, level, cat, dl, event, detail=""):
        loc, utc = now_stamps(self.clock())
        event = label_event(dl, event)
        self.emit({"time": loc, "utc": utc, "level": level, "cat": cat, "dl": dl,
                   "event": event, "detail": detail})

    # -- PLC --------------------------------------------------------------------------
    def _needed_tags(self):
        need = {}
        for d in self.cfg["datalogs"]:
            if d.get("enabled", True) and d["type"] == T_EXPR and d["name"] in self.exprs:
                for r in self.exprs[d["name"]].plc_refs().values():
                    topic, tag = split_ref(r)
                    if topic:
                        need.setdefault(topic.lower(), set()).add(tag)
        return need

    def _read_plcs(self, now):
        values = {}
        need = self._needed_tags()
        topics = {p["topic"].lower(): p for p in self.cfg["plcs"]}
        for tl, tags in need.items():
            tags = sorted(tags)
            p = topics.get(tl)
            res = {}
            if p is None:
                pass
            elif now < self.plc_next.get(tl, 0):
                pass                                     # unreachable: wait for the retry time
            else:
                obj = self.plc_objs.get(tl)
                try:
                    if obj is None:
                        obj = self.plc_objs[tl] = make_plc(p, self.s, self.simulate)
                    res, status = obj.read(tags)
                except Exception as e:
                    res, status = {}, str(e)
                ok = any(v is not None for v in res.values())
                self._plc_state(p["topic"], ok, status)
                if not ok:
                    self.plc_next[tl] = now + float(self.s.get("plc_retry_sec", 60))
            for t in tags:
                values[ref_key(tl, t)] = res.get(t)
        return values

    def _plc_state(self, topic, ok, status):
        prev = self.plc_status.get(topic.lower())
        self.plc_status[topic.lower()] = (ok, status)
        if prev is None or prev[0] != ok:
            if ok and prev is not None:
                self._ev("OK", CAT_PLC, "PLC " + topic, "PLC responding again", status)
            elif not ok:
                self._ev("WARN", CAT_PLC, "PLC " + topic, "PLC not reachable - its EXPR data logs "
                         "cannot be judged (retry every {:g} s)".format(float(self.s.get("plc_retry_sec", 60))),
                         status)

    # -- main step --------------------------------------------------------------------
    def step(self):
        with self.lock:
            now = self.clock()
            s = self.s
            prev_step = self.last_step
            dt = 0.0 if prev_step is None else min(now - prev_step, 3 * float(s["check_sec"]))
            self.last_step = now
            procs = self.procs_fn()
            self.client_ok = None if procs is None else s["client_exe"].lower() in procs
            self.engine_ok = None if procs is None else s["engine_exe"].lower() in procs

            self._service_check(now, prev_step)
            gate = self._gate(now)
            alarms = []
            if gate and self.engine_ok is False:
                if not self.engine_alarm:
                    self._ev("ALARM", CAT_ISSUE, "-", "{} is not running; {}".format(
                        s["engine_exe"], self._popup_word()))
                    self.engine_alarm = True
                alarms.append(("ALARM", "Datalog service {} is not running".format(s["engine_exe"]), "-"))
            elif self.engine_alarm:
                if gate and self.engine_ok:
                    self._ev("OK", CAT_ISSUE, "-", "{} running again".format(s["engine_exe"]))
                self.engine_alarm = False

            self.values = self._read_plcs(now) if gate else {}
            vals = dict(self.hmi)
            vals.update(self.values)

            rows = []
            today = datetime.fromtimestamp(now).date()
            for d in self.cfg["datalogs"]:
                st = self.states[d["name"]]
                rows.append(self._step_dl(d, st, now, dt, today, gate, vals))
                if st.alarm:
                    alarms.append((st.alarm_level, st.alarm, d["name"]))
            self._hang_check(now)
            self.rows, self.alarms = rows, alarms
            if self.data_dir and now - self.cov_saved >= 300:
                self.save_coverage()
            return rows, alarms

    # -- PC / datalog service restart tracking ------------------------------------------
    def _svc_file(self):
        return self.data_dir / "service_state.json" if self.data_dir else None

    def _load_svc(self):
        f = self._svc_file()
        try:
            d = json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else {}
        except Exception:
            d = {}
        d["pids"] = {str(k): v for k, v in (d.get("pids") or {}).items()}
        return d

    def _save_svc(self):
        f = self._svc_file()
        if f:
            try:
                f.write_text(json.dumps(self.svc), encoding="utf-8")
            except OSError:
                pass

    @staticmethod
    def _t(epoch):
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")

    def _service_check(self, now, prev_step=None):
        """History entries when the PC restarted and when the datalog service process
        stopped / started. Works without admin rights: a restart is seen as a new process id;
        the exact start time is added only where Windows allows reading it."""
        sv, exe, changed = self.svc, self.s["engine_exe"], False
        up = self.uptime_fn()
        boot = now - up * 60 if up < 1e8 else None
        reboot = False
        if boot is not None:
            # A reboot can only happen while this program is not running, so it is checked once,
            # at the first check: the PC started later than the start we saved -> it restarted.
            # (300 s margin for clock corrections; a clock changed by more while the watcher was
            # closed can give a false entry, hence "PC restarted" is always shown with both times.)
            if prev_step is None and sv.get("boot") is not None and boot > sv["boot"] + 300:
                self._ev("WARN", CAT_SERVICE, "-", "PC restarted at {}".format(self._t(boot)),
                         "previous start {}".format(self._t(sv["boot"])))
                reboot = True
            if sv.get("boot") is None or reboot or abs(boot - sv["boot"]) > 60:
                sv["boot"], changed = boot, True             # keep the saved value current
        info = self.pinfo_fn(exe)
        if info is not None:
            old = sv["pids"]
            new = {str(pid): st for pid, st in info.items()}
            if reboot:
                old = {}                                      # every process is new after a reboot
            if not old and new and sv.get("seen") is None:
                starts = [v for v in new.values() if v]
                self._ev("INFO", CAT_SERVICE, "-", "Datalog service {} running{}".format(
                    exe, " since " + self._t(min(starts)) if starts else ""),
                    "process id " + ", ".join(sorted(new)))
            else:
                for pid in sorted(set(old) - set(new)):
                    self._ev("WARN", CAT_SERVICE, "-", "Datalog service {} stopped (process {} ended)".format(
                        exe, pid), "noticed {}".format(self._t(now)))
                for pid in sorted(set(new) - set(old)):
                    st = new[pid]
                    if st:
                        when = "at " + self._t(st)
                    elif prev_step:
                        when = "between {} and {}".format(self._t(prev_step), self._t(now))
                    else:
                        when = "while the watcher was not running (before {})".format(self._t(now))
                    ref = st if st else (prev_step or now)
                    after_boot = reboot or (sv.get("boot") is not None and ref - sv["boot"] < (600 if st else 900))
                    why = ("after PC restart" if after_boot
                           else "PC was NOT restarted - HMI server / service restarted")
                    self._ev("WARN", CAT_SERVICE, "-", "Datalog service {} started {}".format(exe, when),
                             "process id {}, {}".format(pid, why))
            if new != sv["pids"] or sv.get("seen") is None:
                sv["pids"], sv["seen"], changed = new, True, True
        if changed:
            self._save_svc()

    def _gate(self, now):
        """Gate check: data logs are judged only while the SE client (runtime) has been running for
        at least runtime_grace_sec. Runtime closed -> everything paused, open issues cleared."""
        grace = float(self.s.get("runtime_grace_sec", 60))
        if self.client_ok is None:                    # process list not readable: do not block checking
            running = True
        else:
            running = self.client_ok
        if running:
            if self.runtime_since is None:
                self.runtime_since = now
                if self.gate_open is not None or grace > 0:
                    self._ev("INFO", CAT_WATCHER, "-", "Runtime ({}) started - checks begin in {:g} s".format(
                        self.s["client_exe"], grace))
            left = grace - (now - self.runtime_since)
            gate = left <= 0
            self.gate_text = "runtime started - checks begin in {:.0f} s".format(max(left, 0))
        else:
            if self.runtime_since is not None or self.gate_open is None:
                self._ev("INFO", CAT_WATCHER, "-", "Runtime ({}) not running - checks paused".format(
                    self.s["client_exe"]))
            self.runtime_since = None
            gate = False
            self.gate_text = "waiting for runtime ({})".format(self.s["client_exe"])
        if gate and self.gate_open is not True:
            n = sum(1 for d in self.cfg["datalogs"] if d.get("enabled", True))
            self._ev("INFO", CAT_WATCHER, "-", "Runtime running {:g} s - checking {} data log(s)".format(grace, n))
        self.gate_open = gate
        return gate

    def _popup_word(self):
        return "logged only (log-only mode)" if self.s["log_only"] else "popup generated"

    def _row(self, d, st, now, state, trigger, detail=""):
        sc = st.scan or {}
        return {"name": d["name"], "type": TYPE_SHORT[d["type"]], "state": state, "trigger": trigger,
                "size": fmt_size(sc.get("size")),
                "age": fmt_age(None if st.last_growth is None else now - st.last_growth),
                "file": sc.get("file") or "-",
                "ftime": datetime.fromtimestamp(sc["ftime"]).strftime("%Y-%m-%d %H:%M:%S")
                if sc.get("ftime") else "-",
                "detail": detail}

    def _step_dl(self, d, st, now, dt, today, gate, vals):
        name, typ = d["name"], d["type"]
        root = self.s["log_root"]
        folder = Path(root) / name if root else None
        sc = scan_folder(folder) if folder else None
        st.folder_problem = None
        if folder and sc is None:
            st.folder_problem = ("folder not found: {}".format(folder) if not folder.is_dir()
                                 else "no data set files (YYYY MM DD 0000 ... .DAT) in {}".format(folder))
        grew, delta = False, 0
        if sc and st.scan:
            if sc["newest"] != st.scan["newest"]:
                grew, delta = True, sc["size"]
                st.set_changed_at = now
            elif sc["size"] > st.scan["size"]:
                grew, delta = True, sc["size"] - st.scan["size"]
        st.scan = sc
        gap = (now - st.last_growth) if (grew and st.last_growth) else 0.0
        if grew:
            st.last_growth = now
        if sc:
            self._retention(d, st, sc, now, today)

        if not d.get("enabled", True):
            self._clear(st, name, silent=True)
            return self._row(d, st, now, "Disabled", "-")
        if not root:
            return self._row(d, st, now, "Unknown", "-", "datalog folder not set (Settings)")
        if not gate:
            if st.expected != "paused":
                self._clear(st, name, silent=True)
                st.expected, st.running = "paused", False
            return self._row(d, st, now, "Paused", "-", self.gate_text)

        if typ == T_MACRO:
            exp, what = True, "monitoring started"
        else:
            if name in self.expr_err:
                return self._row(d, st, now, "Unknown", "?", "expression error: " + self.expr_err[name])
            exp, what = self.exprs[name].evaluate(vals), "trigger expression satisfied"

        if exp != st.expected:
            prev = st.expected
            if exp is True:
                st.exp_since, st.running = now, False
                st.stop_since, st.stop_warned = None, False
                if typ == T_MACRO:
                    self._ev("INFO", CAT_STARTSTOP, name, "Monitoring started - waiting for DL to grow")
                else:
                    self._ev("INFO", CAT_STARTSTOP, name, "Trigger expression satisfied - waiting for DL to start")
            elif exp is False:
                if prev is True:
                    self._ev("INFO", CAT_STARTSTOP, name, "Trigger expression no longer satisfied - DL should stop")
                st.stop_since, st.stop_warned = now, False
                if st.alarm and st.alarm_level == "ALARM":
                    self._clear(st, name, "Critical cleared - DL no longer required")
            else:
                self._ev("WARN", CAT_PLC, name, "Trigger unknown (PLC value not readable) - check suspended")
                self._clear(st, name, silent=True)
            st.expected = exp

        if exp is None:
            return self._row(d, st, now, "Unknown", "?", "PLC not reachable / value unknown")

        trigger = "Always" if typ == T_MACRO else ("Triggered" if exp else "Waiting")
        stale = float(d["stale_min"]) * 60
        if exp is True:
            if grew:
                if not st.running:
                    st.running, st.stopped_logged, st.last_progress = True, False, now
                    self._ev("OK", CAT_STARTSTOP, name, "DL started - inferred from file growth",
                             "+{}, {} after trigger".format(fmt_size(delta), fmt_age(now - (st.exp_since or now))
                                                             .replace(" ago", "")))
                if st.alarm:
                    self._clear(st, name, "DL is growing in size again - critical cleared")
                elif now - st.last_progress >= float(self.s["progress_min"]) * 60:
                    self._ev("OK", CAT_STARTSTOP, name, "DL is growing in size", fmt_size(sc["size"] if sc else None))
                    st.last_progress = now
            elif not st.alarm:
                if st.running:
                    ref = st.last_growth or st.exp_since
                    if now - ref >= stale:
                        self._raise(st, name, now, "{} is not growing in size".format(name),
                                    "DL has not grown for {:g} min".format(float(d["stale_min"])),
                                    hint=EVENT_HINT if typ == T_EXPR else "")
                else:
                    limit = float(d["stale_min"] if typ == T_MACRO else d["start_grace_min"])
                    if now - st.exp_since >= limit * 60:
                        self._raise(st, name, now, "{} has not started".format(name),
                                    "DL has not started {:g} min after {}".format(limit, what),
                                    hint=EVENT_HINT if typ == T_EXPR else "")
            logging_now = st.running and st.last_growth is not None and now - st.last_growth < stale
            if grew:     # credit the time since the previous growth (writes can be buffered)
                credit = min(gap, now - (st.exp_since or now)) if 0 < gap <= stale else dt
            else:
                credit = 0.0
            self._coverage(today, name, dt, True, credit)
            if st.alarm:
                return self._row(d, st, now, "Critical", trigger, st.alarm + "  -  " + (
                    st.folder_problem or (EVENT_HINT if typ == T_EXPR else "check the datalog path")))
            if logging_now:
                return self._row(d, st, now, "Started", trigger, "inferred from file growth")
            limit = float(d["stale_min"] if typ == T_MACRO else d["start_grace_min"]) * 60
            waited = now - (st.exp_since or now)
            return self._row(d, st, now, "Stopped", trigger, "inferred from file not growing - waiting for start "
                             "{}:{:02d} of {}:{:02d}{}".format(
                int(waited // 60), int(waited % 60), int(limit // 60), int(limit % 60),
                "  -  " + st.folder_problem if st.folder_problem else ""))

        # expected False
        self._coverage(today, name, dt, False, 0.0)
        if grew:
            st.stopped_logged = False
            if st.stop_since and not st.stop_warned and now - st.stop_since >= float(d["stop_grace_min"]) * 60:
                st.stop_warned = True
                mins = (now - st.stop_since) / 60
                if self.s["popup_on_not_stopped"]:
                    self._raise(st, name, now, "{} did not stop".format(name),
                                "DL still growing {:.0f} min after trigger off".format(mins), level="WARN")
                else:
                    self._ev("WARN", CAT_STARTSTOP, name, "DL still growing {:.0f} min after trigger off".format(mins))
        elif not st.stopped_logged and st.last_growth and now - st.last_growth >= 60:
            self._ev("INFO", CAT_STARTSTOP, name, "DL stopped - inferred from file not growing")
            st.stopped_logged, st.running = True, False
            if st.alarm and st.alarm_level == "WARN":
                self._clear(st, name, "DL stopped - warning cleared")
        if st.alarm:
            return self._row(d, st, now, "Critical" if st.alarm_level == "ALARM" else "Warning", trigger, st.alarm)
        if st.last_growth and now - st.last_growth < 60:
            return self._row(d, st, now, "Started", trigger, "inferred from file growth - still logging after "
                                                              "trigger off")
        return self._row(d, st, now, "Stopped", trigger, "inferred from file not growing")

    # -- issues -----------------------------------------------------------------------
    def _raise(self, st, name, now, banner_text, history_text, level="ALARM", hint=""):
        st.alarm, st.alarm_level = banner_text, level
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        near = (st.set_changed_at and now - st.set_changed_at <= 600) or (now - midnight <= 600)
        parts = []
        if st.folder_problem:
            parts.append(st.folder_problem)
        elif hint:
            parts.append(hint)
        if near:
            parts.append("near data set rollover / deletion (see Rockwell Jira 1388792)")
        detail = " | ".join(parts)
        self._ev(level, CAT_ISSUE, name, "{}; {}".format(history_text, self._popup_word()), detail)
        if level == "ALARM":
            self.recent_issues.append((now, name))
            day = datetime.fromtimestamp(now).date().isoformat()
            c = self.cov.setdefault(day, {}).setdefault(name, {"trig": 0.0, "log": 0.0, "issues": 0})
            c["issues"] += 1

    def _clear(self, st, name, text="", silent=False):
        if st.alarm and not silent:
            self._ev("OK", CAT_ISSUE, name, text)
        st.alarm = st.alarm_level = None

    def _hang_check(self, now):
        self.recent_issues = [(t, n) for t, n in self.recent_issues if now - t <= 60]
        names = sorted({n for _, n in self.recent_issues})
        if len(names) >= 2 and now - self.last_hang > 600:
            self.last_hang = now
            self._ev("ALARM", CAT_HANG, "-", "Datalog engine hang suspected - {} data logs stopped within "
                     "60 s".format(len(names)), ", ".join(names))

    # -- retention --------------------------------------------------------------------
    def _retention(self, d, st, sc, now, today):
        keep = int(float(self.s.get("keep_days", 0)))
        if keep <= 0 or st.retention_day == today.isoformat():
            return
        newest = set_date(sc["newest"])
        if newest != today:
            return                    # FTView deletes only when it creates a new set
        if st.set_changed_at and now - st.set_changed_at < 120:
            return                    # give FTView time to delete
        st.retention_day = today.isoformat()
        oldest = set_date(sc["oldest"])
        if oldest is None:
            return
        age = (today - oldest).days
        if age > keep + 1:
            self._ev("WARN", CAT_RETENTION, d["name"], "Old data sets not deleted",
                     "oldest {} ({} days), keep {} days, {} data sets in folder".format(
                         oldest.isoformat(), age, keep, sc["sets"]))
        else:
            self._ev("INFO", CAT_RETENTION, d["name"], "Deletion OK",
                     "oldest {} ({} days), keep {} days".format(oldest.isoformat(), age, keep))

    # -- coverage ---------------------------------------------------------------------
    def _coverage(self, today, name, dt, triggered, credit):
        c = self.cov.setdefault(today.isoformat(), {}).setdefault(name, {"trig": 0.0, "log": 0.0, "issues": 0})
        if triggered:
            c["trig"] += max(dt, 0.0)
            c["log"] = min(c["trig"], c["log"] + max(credit, 0.0))

    def _cov_file(self, day):
        return self.data_dir / "coverage_{}.json".format(day)

    def _load_coverage(self):
        if not self.data_dir:
            return
        f = self._cov_file(date.today().isoformat())
        try:
            self.cov[date.today().isoformat()] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass

    def save_coverage(self):
        if not self.data_dir:
            return
        with self.lock:
            self.cov_saved = self.clock()
            for day, data in list(self.cov.items()):
                try:
                    self._cov_file(day).write_text(json.dumps(data, indent=1), encoding="utf-8")
                except OSError:
                    pass
            today = date.today().isoformat()
            for day in [k for k in self.cov if k < today]:
                del self.cov[day]                         # already saved, keep memory small

    def snapshot(self):
        with self.lock:
            return {"rows": list(self.rows), "alarms": list(self.alarms),
                    "engine": self.engine_ok, "client": self.client_ok, "gate": self.gate_open,
                    "gate_text": self.gate_text,
                    "plc": dict(self.plc_status), "values": dict(self.values)}


def read_coverage(data_dir, day_from, day_to):
    """-> list of (day, dl, trig_sec, log_sec, issues)"""
    out = []
    d = day_from
    while d <= day_to:
        f = Path(data_dir) / "coverage_{}.json".format(d.isoformat())
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for dl, c in sorted(data.items()):
                out.append((d.isoformat(), dl, c.get("trig", 0.0), c.get("log", 0.0), c.get("issues", 0)))
        except Exception:
            pass
        d += timedelta(days=1)
    return out


def list_sets(folder, day_from, day_to):
    """Data sets in folder dated day_from..day_to -> [(prefix, total size, last write epoch, files)].
    Stats only the files of those days (read attributes only, never opens them for reading)."""
    sets = {}
    try:
        with os.scandir(str(folder)) as it:
            for e in it:
                m = _SET_RE.match(e.name)
                if m and e.name[-4:].upper() == ".DAT":
                    d = set_date(e.name[:15])
                    if d and day_from <= d <= day_to:
                        sets.setdefault(e.name[:15], []).append(e.name)
    except OSError:
        return None
    out = []
    for prefix in sorted(sets):
        size, last = 0, 0.0
        for n in sets[prefix]:
            try:
                fs, fm = file_stat(os.path.join(str(folder), n))
            except OSError:
                continue
            size += fs
            last = max(last, fm)
        out.append((prefix, size, last, len(sets[prefix])))
    return out


def utc_offset_text(when=None):
    when = when or datetime.now()
    off = when.astimezone().utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    return "UTC{}{:02d}:{:02d}".format("+" if mins >= 0 else "-", abs(mins) // 60, abs(mins) % 60)


def write_incident_report(fh, cfg, events, t_from, t_to, incident, header_lines, coverage, status_rows,
                          include_expr=False):
    """Plain-text incident report. events: history entries (dicts) of the window, oldest first."""
    W = 118
    def line(ch="-"):
        fh.write(ch * W + "\n")
    def title(t):
        fh.write("\n" + t + "\n")
        line()
    for h in header_lines:
        fh.write(h + "\n")
    fh.write("Incident time : {:%Y-%m-%d %H:%M} local\n".format(incident))
    fh.write("Window        : {:%Y-%m-%d %H:%M} to {:%Y-%m-%d %H:%M} local ({})\n".format(
        t_from, t_to, utc_offset_text(incident)))
    fh.write("              : {:%Y-%m-%d %H:%M} to {:%Y-%m-%d %H:%M} UTC  <- use these for FactoryTalk "
             "Diagnostics exports\n".format(t_from.astimezone(timezone.utc), t_to.astimezone(timezone.utc)))
    fh.write("NOTE: " + INFERRED_NOTE + "\n")
    line("=")

    title("1. SUMMARY")
    by = {}
    for e in events:
        by[e["level"]] = by.get(e["level"], 0) + 1
    fh.write("History entries in window: {}  (Critical {}, Warning {}, OK {}, Info {})\n".format(
        len(events), by.get("ALARM", 0), by.get("WARN", 0), by.get("OK", 0), by.get("INFO", 0)))
    crit = [e for e in events if e["level"] == "ALARM"]
    if crit:
        fh.write("First Critical : {}  {}\n".format(crit[0]["time"], crit[0]["event"]))
        dls = sorted({e["dl"] for e in crit if e["dl"] != "-"})
        if dls:
            fh.write("Data logs with Critical: {}\n".format(", ".join(dls)))
    svc = [e for e in events if e.get("cat") == CAT_SERVICE]
    fh.write("PC / datalog service events: {}\n".format(len(svc) if svc else "none"))
    for e in svc:
        fh.write("    {}  {}{}\n".format(e["time"], e["event"], "  [" + e["detail"] + "]" if e["detail"] else ""))
    gaps = [e for e in events if e.get("cat") == CAT_WATCHER and "was not running" in e["event"]]
    fh.write("Watcher downtime in window: {}\n".format("none" if not gaps else ""))
    for e in gaps:
        fh.write("    {}  {}  [{}]\n".format(e["time"], e["event"], e["detail"]))

    title("2. TIMELINE (all history entries in the window, oldest first)")
    wdl = max([len(e["dl"]) for e in events] + [8]) + 2
    fh.write("{:<19}  {:<19}  {:<8} {:<12} {:<{w}}{}\n".format("Local time", "UTC", "Level", "Category",
                                                             "Data log", "Event", w=wdl))
    for e in events:
        fh.write("{:<19}  {:<19}  {:<8} {:<12} {:<{w}}{}{}\n".format(
            e["time"], e.get("utc", ""), {"ALARM": "Critical", "WARN": "Warning"}.get(e["level"], e["level"].title()),
            e.get("cat", ""), e["dl"], e["event"], "  [" + e["detail"] + "]" if e["detail"] else "", w=wdl))
    if not events:
        fh.write("(no history entries in this window)\n")

    title("3. DATA SET FILES (read from the datalog folders now)")
    fh.write("Last write = time the files of that data set were last written. A data set whose last write is "
             "before the\nincident while its data log should have been logging shows when logging really "
             "stopped.\n\n")
    root = cfg["settings"].get("log_root", "")
    d_from, d_to = t_from.date() - timedelta(days=1), t_to.date()
    for d in cfg["datalogs"]:
        if not d.get("enabled", True):
            continue
        folder = Path(root) / d["name"] if root else None
        sets = list_sets(folder, d_from, d_to) if folder else None
        fh.write("{}  ({})\n".format(d["name"], folder if folder else "datalog folder not set"))
        if sets is None:
            fh.write("    folder not readable\n")
        elif not sets:
            fh.write("    no data set dated {} to {}\n".format(d_from, d_to))
        for prefix, size, last, n in sets or []:
            lt = datetime.fromtimestamp(last) if last else None
            rel = ""
            if lt:
                mins = (lt - incident).total_seconds() / 60
                rel = "  ({:+.0f} min from incident)".format(mins)
            fh.write("    {}   {:>9}   last write {}{}   {} file(s)\n".format(
                prefix, fmt_size(size), lt.strftime("%Y-%m-%d %H:%M:%S") if lt else "-", rel, n))

    title("4. COVERAGE (time logged / time it should have logged, per day)")
    if coverage:
        for day, dl, trig, log, iss in coverage:
            fh.write("{}  {:<{w}} should {:>6.1f} h  logged {:>6.1f} h  {:>7}  critical {}\n".format(
                day, dl, trig / 3600, log / 3600, "{:.1f} %".format(100 * log / trig) if trig else "-", iss, w=wdl))
    else:
        fh.write("(no coverage data for these days)\n")

    title("5. WATCHER CONFIGURATION")
    st = cfg["settings"]
    fh.write("check every {} s, runtime grace {} s, client {}, datalog service {}\n".format(
        st.get("check_sec"), st.get("runtime_grace_sec"), st.get("client_exe"), st.get("engine_exe")))
    for d in cfg["datalogs"]:
        fh.write("  {:<{w}} {:<6} {:<8} stale {} min, start grace {} min, stop grace {} min{}\n".format(
            d["name"], TYPE_SHORT.get(d["type"], "?"), "enabled" if d.get("enabled", True) else "DISABLED",
            d.get("stale_min"), d.get("start_grace_min"), d.get("stop_grace_min"),
            ("\n" + " " * (wdl + 4) + "trigger: " + d.get("expr", "")) if include_expr and d["type"] == T_EXPR else "",
            w=wdl))

    title("6. STATUS AT REPORT TIME (now - not at the incident)")
    for r in status_rows:
        fh.write("  {:<{w}} {:<9} trigger {:<5} size {:>9}  last growth {:<12} {}\n".format(
            r["name"], r["state"], str(r["trigger"]), r["size"], r["age"], r["detail"], w=wdl))
    if not status_rows:
        fh.write("(monitoring not running)\n")
    line("=")
    fh.write("End of report\n")
    return len(events)


def purge_old_files(data_dir, days):
    """Delete the watcher's own history/coverage files older than `days` days."""
    if days <= 0:
        return 0
    cutoff = (date.today() - timedelta(days=int(days))).isoformat()
    n = 0
    for f in Path(data_dir).glob("*_????-??-??.*"):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        if m and m.group(1) < cutoff:
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def check_folders(cfg):
    """-> list of (name, enabled, folder, status text, ok)"""
    root = cfg["settings"].get("log_root", "")
    out = []
    for d in cfg["datalogs"]:
        if not root:
            out.append((d["name"], d.get("enabled", True), "-", "datalog folder not set (Settings)", False))
            continue
        f = Path(root) / d["name"]
        if not f.is_dir():
            out.append((d["name"], d.get("enabled", True), str(f), "folder not found", False))
            continue
        sc = scan_folder(f)
        if sc is None:
            out.append((d["name"], d.get("enabled", True), str(f),
                        "no data set files (expected names like 'YYYY MM DD 0000 <model> (Float).DAT')", False))
        else:
            out.append((d["name"], d.get("enabled", True), str(f), "OK - {} data sets, newest {}, oldest {}, "
                        "today {}".format(sc["sets"], sc["newest"][:10], sc["oldest"][:10], fmt_size(sc["size"])), True))
    return out
