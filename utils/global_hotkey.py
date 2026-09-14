"""系统级 Ctrl+V 全局热键（仅 Windows 生效）。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
from PySide6.QtCore import QAbstractNativeEventFilter

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


class GlobalPasteHotkey(QAbstractNativeEventFilter):
    """全局 Ctrl+V 热键：register() 后占用，unregister() 释放。

    activated 属性可挂回调：系统级 Ctrl+V 被按下且本热键已注册时调用。
    """

    def __init__(self):
        super().__init__()
        self._registered = False
        self.activated = None  # 回调，由调用方注入

    def register(self) -> bool:
        """注册全局 Ctrl+V。已注册直接成功；失败（被占用/平台不支持）返回 False。"""
        if self._registered:
            return True
        if _user32 is None:
            return False
        if not _user32.RegisterHotKey(None, _HOTKEY_ID, MOD_CONTROL, VK_V):
            return False
        self._registered = True
        return True

    def unregister(self):
        if not self._registered:
            return
        try:
            _user32.UnregisterHotKey(None, _HOTKEY_ID)
        finally:
            self._registered = False

    def is_registered(self) -> bool:
        return self._registered

    def nativeEventFilter(self, eventType, message):
        if not self._registered:
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
