"""系统级粘贴全局热键：Windows Ctrl+V / macOS Cmd+V（其余平台降级为无操作）。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import sys

from PySide6.QtCore import QAbstractNativeEventFilter

# ---------- Windows（RegisterHotKey）----------
WM_HOTKEY = 0x0312
MOD_CONTROL = 0x0002
VK_V = 0x56
_HOTKEY_ID = 0x4C53  # 本应用唯一热键 ID（0x4C53 = "LS"）

try:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int,
                                       wintypes.UINT, wintypes.UINT)
    _user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
    _MSG = wintypes.MSG
except Exception:  # 非 Windows / ctypes 不可用：降级为无操作
    _user32 = None
    _MSG = None

# ---------- macOS（Carbon RegisterEventHotKey）----------
# 用 Carbon 注册系统级热键：无需"辅助功能"授权，语义与 Windows 的 RegisterHotKey 一致。
_MAC_MOD_COMMAND = 0x0100         # cmdKey
_MAC_VK_ANSI_V = 0x09             # kVK_ANSI_V
_MAC_CLASS_KEYBOARD = 0x6B657962  # 'keyb'
_MAC_HOTKEY_PRESSED = 5           # kEventHotKeyPressed
_MAC_SIGNATURE = 0x4C53           # 'LS'，与 Windows 同源

_carbon = None
if sys.platform == "darwin":
    try:
        import ctypes

        _carbon = ctypes.CDLL(
            "/System/Library/Frameworks/Carbon.framework/Carbon")

        class _EventHotKeyID(ctypes.Structure):
            _fields_ = [("signature", ctypes.c_uint32),
                        ("id", ctypes.c_uint32)]

        class _EventTypeSpec(ctypes.Structure):
            _fields_ = [("eventClass", ctypes.c_uint32),
                        ("eventKind", ctypes.c_uint32)]

        _EventHandlerProc = ctypes.CFUNCTYPE(
            ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

        _carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        _carbon.InstallEventHandler.restype = ctypes.c_int32
        _carbon.InstallEventHandler.argtypes = (
            ctypes.c_void_p, _EventHandlerProc, ctypes.c_uint32,
            ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p))
        _carbon.RemoveEventHandler.restype = ctypes.c_int32
        _carbon.RemoveEventHandler.argtypes = (ctypes.c_void_p,)
        _carbon.RegisterEventHotKey.restype = ctypes.c_int32
        _carbon.RegisterEventHotKey.argtypes = (
            ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))
        _carbon.UnregisterEventHotKey.restype = ctypes.c_int32
        _carbon.UnregisterEventHotKey.argtypes = (ctypes.c_void_p,)
    except Exception:  # Carbon 不可用：降级为无操作
        _carbon = None


class GlobalPasteHotkey(QAbstractNativeEventFilter):
    """系统级粘贴热键：Windows Ctrl+V / macOS Cmd+V。

    register() 后占用，unregister() 释放；activated 可挂回调，按下（且已注册）时调用。
    """

    def __init__(self):
        super().__init__()
        self._registered = False
        self.activated = None       # 回调，由调用方注入
        self._hotkey_ref = None     # macOS：EventHotKeyRef
        self._handler_ref = None    # macOS：EventHandlerRef
        self._handler_proc = None   # macOS：须持有引用防 GC

    def register(self) -> bool:
        """注册系统级粘贴热键。已注册直接成功；失败（被占用/平台不支持）返回 False。"""
        if self._registered:
            return True
        if _user32 is not None:
            return self._register_windows()
        if _carbon is not None:
            return self._register_macos()
        return False

    def unregister(self):
        if not self._registered:
            return
        try:
            if _user32 is not None:
                _user32.UnregisterHotKey(None, _HOTKEY_ID)
            elif _carbon is not None:
                if self._hotkey_ref is not None:
                    _carbon.UnregisterEventHotKey(self._hotkey_ref)
                if self._handler_ref is not None:
                    _carbon.RemoveEventHandler(self._handler_ref)
        finally:
            self._hotkey_ref = None
            self._handler_ref = None
            self._handler_proc = None
            self._registered = False

    def is_registered(self) -> bool:
        return self._registered

    # ---------- Windows ----------
    def _register_windows(self) -> bool:
        if not _user32.RegisterHotKey(None, _HOTKEY_ID, MOD_CONTROL, VK_V):
            return False
        self._registered = True
        return True

    def nativeEventFilter(self, eventType, message):
        if not self._registered or _user32 is None:
            return False, 0
        if eventType not in (b"windows_generic_MSG", "windows_generic_MSG"):
            return False, 0
        ptr = int(message)
        if not ptr:
            return False, 0
        msg = _MSG.from_address(ptr)
        if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
            if self.activated is not None:
                self.activated()
            return True, 0
        return False, 0

    # ---------- macOS ----------
    def _register_macos(self) -> bool:
        target = _carbon.GetApplicationEventTarget()
        spec = _EventTypeSpec(_MAC_CLASS_KEYBOARD, _MAC_HOTKEY_PRESSED)

        def _on_hotkey(call_ref, event_ref, user_data):
            if self.activated is not None:
                self.activated()
            return 0  # noErr：消费该热键事件

        self._handler_proc = _EventHandlerProc(_on_hotkey)
        handler_ref = ctypes.c_void_p()
        if _carbon.InstallEventHandler(
                target, self._handler_proc, 1, ctypes.byref(spec), None,
                ctypes.byref(handler_ref)) != 0:
            self._handler_proc = None
            return False
        self._handler_ref = handler_ref

        hotkey_ref = ctypes.c_void_p()
        if _carbon.RegisterEventHotKey(
                _MAC_VK_ANSI_V, _MAC_MOD_COMMAND,
                _EventHotKeyID(_MAC_SIGNATURE, 1), target, 0,
                ctypes.byref(hotkey_ref)) != 0:
            _carbon.RemoveEventHandler(self._handler_ref)
            self._handler_ref = None
            self._handler_proc = None
            return False
        self._hotkey_ref = hotkey_ref
        self._registered = True
        return True