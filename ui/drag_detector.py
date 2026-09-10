"""
系统级拖拽会话检测器 - 通过全局低级鼠标钩子（WH_MOUSE_LL）感知「桌面有拖拽正在进行」。

用途：驱动屏幕顶部的快捷放置条出现。它不做文件接收——接收文件由 DropZone 作为
OLE drop-target 处理。二者分离：此检测器只负责“是否在拖 + 光标在哪”。

实现约束：
- 低级钩子回调运行在系统的同步输入派发上下文中，钩子内禁止执行 Qt/COM/阻塞操作
  （否则会抛 RPC_E_CANTCALLOUT_ININPUTSYNCCALL）。因此回调只更新线程安全的共享变量，
  Qt 侧用定时器轮询读取，不在此回调内调用任何 Qt API。
- 判定“拖拽中”是启发式：左键按下后移动超过阈值即视为一次拖拽会话，直到左键抬起。
  无法区分拖文件 vs 拖文本选区/滚动条，故是否真正添加文件一律以 DropZone 收到的
  真实 OLE drop mime 为准。
"""
import ctypes
import ctypes.wintypes
from PySide6.QtCore import QPoint

# ---- Win32 常量 ----
WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
VK_LBUTTON = 0x01

_DRAG_THRESHOLD = 4  # 判定拖拽生效的位移阈值（px）

# 共享状态（主线程轮询读 / 钩子回调写）
_state = {
    "installed": False,
    "dragging": False,
    "down_pos": (0, 0),
    "down_time": 0.0,
    "pos": (0, 0),
}


class _LowLevelMouseProc:
    """ctypes 回调包装，保持引用防止被 GC，避免钩子被卸载。"""

    def __init__(self):
        self._proc = None  # HOOKPROC 可调用对象

    def __call__(self, n_code, w_param, l_param):
        if n_code == 0:  # HC_ACTION
            msg = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            x, y = msg.pt.x, msg.pt.y
            _state["pos"] = (x, y)
            if w_param == WM_LBUTTONDOWN:
                _state["down_pos"] = (x, y)
                _state["dragging"] = False
            elif w_param == WM_MOUSEMOVE:
                if _state["dragging"] is False and ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                    dx = x - _state["down_pos"][0]
                    dy = y - _state["down_pos"][1]
                    if dx * dx + dy * dy >= _DRAG_THRESHOLD * _DRAG_THRESHOLD:
                        _state["dragging"] = True
            elif w_param == WM_LBUTTONUP:
                _state["dragging"] = False
        next_ptr = ctypes.cast(self._proc, ctypes.c_void_p).value
        return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", ctypes.wintypes.POINT),
        ("mouseData", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),  # ULONG_PTR
    ]


class DragDetector:
    """全局拖拽会话检测器。

    使用：房间就绪后 start()，关闭时 finish()。Qt 定时器(~30ms)轮询 is_dragging()/cursor_pos()。
    """

    def __init__(self):
        ctypes.windll.user32.GetAsyncKeyState.restype = ctypes.c_short
        self._hook = ctypes.c_void_p()
        self._proc_holder = _LowLevelMouseProc()

    def start(self):
        """安装全局低级鼠标钩子。已安装则直接返回（幂等）。"""
        if _state["installed"]:
            return True

        # hmod 必须为 0（NULL）：WH_MOUSE_LL 为低级钩子，回调在本进程内由系统直接映射。
        # 若传 exe 模块句柄（GetModuleHandleW(None)），系统会在该模块里查找“导出符号形式的
        # 钩子过程”，而 ctypes 回调并无此导出符号，导致 SetWindowsHookExW 失败并返回
        # ERROR_MOD_NOT_FOUND(126)，钩子装不上、功能静默失效。传 0 则回调按进程内地址解析。
        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p
        )
        self._proc_holder._proc = HOOKPROC(self._proc_holder)
        proc_ptr = ctypes.cast(self._proc_holder._proc, ctypes.c_void_p).value
        if not proc_ptr:
            return False
        hook = ctypes.windll.user32.SetWindowsHookExW(WH_MOUSE_LL, proc_ptr, 0, 0)
        if not hook:
            return False
        self._hook = ctypes.c_void_p(hook)
        _state["installed"] = True
        _state["dragging"] = False
        return True

    def finish(self):
        """卸载钩子（幂等）。必须在 Qt 主线程调用。"""
        if not _state["installed"]:
            return
        if self._hook.value:
            ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
        self._hook = ctypes.c_void_p()
        _state["installed"] = False
        _state["dragging"] = False

    def is_dragging(self) -> bool:
        return bool(_state["dragging"])

    def cursor_pos(self) -> QPoint:
        return QPoint(_state["pos"][0], _state["pos"][1])