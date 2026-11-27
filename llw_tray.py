"""
llw_tray.py - system tray icon for LogLoss Watcher, using only the Windows API (ctypes).
No extra packages. One background thread that waits for clicks (0 % CPU when idle).

Tray(on_open, on_menu) ; set_state("ok"|"issue"|"warn"|"off", tooltip) ; stop()
If anything fails (not Windows, blocked API) .ok is False and the app works without a tray.
"""
import ctypes, os, struct, tempfile, threading

COLORS = {"ok": (46, 125, 50), "issue": (198, 40, 40), "warn": (239, 108, 0), "off": (117, 117, 117)}


def make_ico_bytes(rgb, size=16):
    """16x16 32-bit ICO with a filled circle - generated in memory, no image library needed."""
    r0, g0, b0 = rgb
    px = bytearray()
    c = (size - 1) / 2.0
    for y in range(size - 1, -1, -1):                 # BMP rows bottom-up
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if d <= c - 1.0:
                hl = 1.25 if (x < c and y < c) else 1.0    # simple highlight
                px += bytes((min(255, int(b0 * hl)), min(255, int(g0 * hl)), min(255, int(r0 * hl)), 255))
            elif d <= c + 0.2:
                px += bytes((int(b0 * .55), int(g0 * .55), int(r0 * .55), 255))   # darker rim
            else:
                px += bytes((0, 0, 0, 0))
    mask = bytes(((size + 31) // 32) * 4 * size)        # AND mask (all 0, alpha is used)
    bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(px) + len(mask), 0, 0, 0, 0)
    img = bih + bytes(px) + mask
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(img), 22) + img


def write_icons(folder):
    """Writes llw_<state>.ico files; returns {state: path}."""
    paths = {}
    for k, rgb in COLORS.items():
        p = os.path.join(folder, "llw_{}.ico".format(k))
        try:
            data = make_ico_bytes(rgb)
            if not os.path.exists(p) or open(p, "rb").read() != data:
                with open(p, "wb") as f:
                    f.write(data)
        except OSError:
            p = os.path.join(tempfile.gettempdir(), "llw_{}.ico".format(k))
            with open(p, "wb") as f:
                f.write(make_ico_bytes(rgb))
        paths[k] = p
    return paths


class Tray:
    WM_APP_TRAY = 0x8001
    MENU = [(1, "Open LogLoss Watcher"), (0, None), (2, "Test popup"), (0, None), (3, "Exit...")]

    def __init__(self, icon_paths, on_open, on_menu, tooltip="LogLoss Watcher"):
        self.ok = False
        self.icon_paths, self.on_open, self.on_menu = icon_paths, on_open, on_menu
        self.tip, self.state = tooltip, "off"
        self.hwnd = None
        self.icons = {}
        self._ready = threading.Event()
        if os.name != "nt":
            return
        threading.Thread(target=self._run, daemon=True, name="tray").start()
        self._ready.wait(5)

    # ---- Win32 definitions -----------------------------------------------------------
    def _setup_api(self):
        from ctypes import wintypes as W
        self.W = W
        u32, s32, k32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", W.UINT), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                        ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR),
                        ("lpszClassName", W.LPCWSTR)]

        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                        ("d", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT),
                        ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", ctypes.c_wchar * 128),
                        ("dwState", W.DWORD), ("dwStateMask", W.DWORD), ("szInfo", ctypes.c_wchar * 256),
                        ("uVersion", W.UINT), ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", W.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", W.HICON)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        self.WNDCLASSW, self.NID, self.POINT = WNDCLASSW, NOTIFYICONDATAW, POINT
        u32.DefWindowProcW.restype = LRESULT
        u32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u32.RegisterClassW.restype = W.ATOM
        u32.CreateWindowExW.restype = W.HWND
        u32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
        u32.LoadImageW.restype = W.HANDLE
        u32.LoadImageW.argtypes = [W.HINSTANCE, W.LPCWSTR, W.UINT, ctypes.c_int, ctypes.c_int, W.UINT]
        u32.CreatePopupMenu.restype = W.HMENU
        u32.AppendMenuW.argtypes = [W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR]
        u32.TrackPopupMenu.argtypes = [W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       W.HWND, W.LPVOID]
        u32.TrackPopupMenu.restype = ctypes.c_int
        u32.DestroyMenu.argtypes = [W.HMENU]
        u32.SetForegroundWindow.argtypes = [W.HWND]
        u32.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        u32.GetMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
        u32.TranslateMessage.argtypes = [ctypes.POINTER(W.MSG)]
        u32.DispatchMessageW.argtypes = [ctypes.POINTER(W.MSG)]
        u32.DestroyWindow.argtypes = [W.HWND]
        u32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        u32.RegisterWindowMessageW.argtypes = [W.LPCWSTR]
        u32.RegisterWindowMessageW.restype = W.UINT
        s32.Shell_NotifyIconW.argtypes = [W.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        k32.GetModuleHandleW.restype = W.HMODULE
        k32.GetModuleHandleW.argtypes = [W.LPCWSTR]
        self.u32, self.s32, self.k32 = u32, s32, k32

    # ---- thread ----------------------------------------------------------------------
    def _run(self):
        try:
            self._setup_api()
            W, u32 = self.W, self.u32
            hinst = self.k32.GetModuleHandleW(None)
            self._proc = self.WNDPROC(self._wndproc)                 # keep a reference!
            wc = self.WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = "LogLossWatcherTray"
            u32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = u32.CreateWindowExW(0, "LogLossWatcherTray", "LogLossWatcherTray", 0,
                                            0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd:
                raise OSError("CreateWindowExW failed")
            self._taskbar_created = u32.RegisterWindowMessageW("TaskbarCreated")
            for k, p in self.icon_paths.items():
                self.icons[k] = u32.LoadImageW(None, p, 1, 16, 16, 0x10)    # IMAGE_ICON, LR_LOADFROMFILE
            self._notify(0)                                              # NIM_ADD
            self.ok = True
        except Exception:
            self.ok = False
            self._ready.set()
            return
        self._ready.set()
        msg = W.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def _nid(self):
        n = self.NID()
        n.cbSize = ctypes.sizeof(self.NID)
        n.hWnd = self.hwnd
        n.uID = 1
        n.uFlags = 0x1 | 0x2 | 0x4                                       # MESSAGE | ICON | TIP
        n.uCallbackMessage = self.WM_APP_TRAY
        n.hIcon = self.icons.get(self.state) or self.icons.get("off")
        n.szTip = self.tip[:127]
        return n

    def _notify(self, action):
        n = self._nid()
        self.s32.Shell_NotifyIconW(action, ctypes.byref(n))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == self.WM_APP_TRAY:
                ev = lparam & 0xFFFF
                if ev in (0x203, 0x202):                                 # LBUTTONDBLCLK / LBUTTONUP
                    self.on_open()
                elif ev == 0x205:                                        # RBUTTONUP
                    self._menu()
                return 0
            if msg == self._taskbar_created:                             # Explorer restarted
                self._notify(0)
                return 0
            if msg == 0x0010:                                            # WM_CLOSE
                self._notify(2)                                          # NIM_DELETE
                self.u32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0002:                                            # WM_DESTROY
                self.u32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return self.u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self):
        u32 = self.u32
        m = u32.CreatePopupMenu()
        for cid, text in self.MENU:
            if cid == 0:
                u32.AppendMenuW(m, 0x800, 0, None)                       # separator
            else:
                u32.AppendMenuW(m, 0, cid, text)
        pt = self.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(self.hwnd)
        cmd = u32.TrackPopupMenu(m, 0x0100 | 0x0080 | 0x0002, pt.x, pt.y, 0, self.hwnd, None)
        u32.PostMessageW(self.hwnd, 0, 0, 0)
        u32.DestroyMenu(m)
        if cmd:
            self.on_menu(cmd)

    # ---- public (any thread) ---------------------------------------------------------
    def set_state(self, state, tooltip):
        if not self.ok:
            return
        tooltip = tooltip[:127]
        if state == self.state and tooltip == self.tip:
            return
        self.state, self.tip = state, tooltip
        try:
            self._notify(1)                                              # NIM_MODIFY
        except Exception:
            pass

    def stop(self):
        if self.ok and self.hwnd:
            try:
                self.u32.PostMessageW(self.hwnd, 0x0010, 0, 0)
            except Exception:
                pass
