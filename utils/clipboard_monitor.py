"""剪切板监听与分发组件（局域网剪切板 - 文本部分）

决策：**文本走系统剪贴板广播**；**图片一律作为文件处理**（不写入远程剪切板，
与复制的文件共用 P2P 链路，属于阶段二，当前阶段不投递）。

职责：
- 监听系统剪贴板变化（QClipboard.dataChanged），识别内容类型（文本/图片/文件）。
- 仅对文本内容产生 clipboard_committed 信号，交由上层上报主机分发。
- 提供 set_written_hash / apply_to_clipboard，用于"写入系统剪贴板"时记录内容摘要，
  抑制由网络接收写入再次触发 dataChanged 造成的回环重发（A→host→B→… 死循环）。

线程归属约定：本组件对 QClipboard 的所有访问必须发生在 GUI 线程；
网络层接收到的内容经信号回投主线程后，再调用 apply_to_clipboard 写入。
"""
import hashlib
import os
import time

from PySide6.QtCore import QObject, QElapsedTimer, Signal
from PySide6.QtWidgets import QApplication


class ClipboardMonitor(QObject):
    # 本端系统剪贴板新增了可投递文本
    clipboard_committed = Signal(str, bytes)  # (mime_type, data)  mime_type 目前恒为 "text"
    # 本端系统剪贴板复制了文件/图片（图片已存为临时 PNG）：交 UI 建会话并上报
    file_copy = Signal(list)  # [{'name': 条目名, 'path': 本机绝对路径}, ...]

    # 文本字节上限（超过则忽略，防止广播风暴与内存占用）
    TEXT_MAX_BYTES = 1 * 1024 * 1024

    # 最小分发间隔（毫秒），防 Ctrl+C 连按或异常循环造成风暴
    THROTTLE_MS = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self._enabled = False
        self._last_content_hash = None  # 最近一次"写入系统剪贴板"的内容摘要（用于防回环）
        self._throttle = QElapsedTimer()
        self._throttle.invalidate()
        QApplication.clipboard().dataChanged.connect(self._on_data_changed)

    def set_enabled(self, enabled: bool):
        """使能/禁用分发。房间连接成功后才应开启分发，避免房间外的复制被投递。"""
        self._enabled = enabled

    def set_written_hash(self, mime_type: str, data: bytes):
        """记录一次"写入系统剪贴板"的内容摘要。

        在调用 apply_to_clipboard 前调用，使随后的 dataChanged 能被识别为本端写回的，
        从而抑制把这份内容再次上报主机形成回环。
        """
        self._last_content_hash = self._content_hash(mime_type, data)

    def _content_hash(self, mime_type: str, data: bytes) -> str:
        return hashlib.sha256(mime_type.encode('utf-8') + b'\0' + bytes(data)).hexdigest()

    def _on_data_changed(self):
        """系统剪贴板变化回调（GUI 线程）"""
        if not self._enabled:
            return

        # 节流：两次分发间隔过近则忽略，避免连按造成重复投递
        if self._throttle.isValid() and self._throttle.elapsed() < self.THROTTLE_MS:
            return

        mime_data = QApplication.clipboard().mimeData()
        if mime_data is None:
            return

        # 文件（URL 本地文件）：作为文件 P2P 会话投递
        if mime_data.hasUrls():
            paths = [url.toLocalFile() for url in mime_data.urls()
                     if url.isLocalFile() and url.toLocalFile()]
            if paths:
                entries = [{'name': os.path.basename(p), 'path': p} for p in paths]
                self.file_copy.emit(entries)
                self._throttle.restart()
            return

        # 图片：一律作为文件处理，先把剪贴板图存为临时 PNG 再进文件会话
        if mime_data.hasImage():
            temp_path = self._save_clipboard_image()
            if temp_path:
                self.file_copy.emit([{'name': os.path.basename(temp_path), 'path': temp_path}])
                self._throttle.restart()
            return

        # 文本：走剪切板广播
        if mime_data.hasText():
            data = mime_data.text().encode('utf-8')
            if len(data) <= self.TEXT_MAX_BYTES and self._commit("text", data):
                self._throttle.restart()

    def _save_clipboard_image(self) -> str:
        """把系统剪贴板中的图片存为临时 PNG，返回路径；失败返回 None。"""
        import tempfile
        img = QApplication.clipboard().image()
        if img.isNull():
            return None
        try:
            folder = os.path.join(tempfile.gettempdir(), 'LANSyncBox', 'ClipboardImages')
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"IMG_{int(time.time() * 1000)}.png")
            if img.save(path, "PNG"):
                return path
        except Exception:
            pass
        return None

    def _commit(self, mime_type: str, data: bytes) -> bool:
        """防回环后发射分发信号。返回是否真正发射。"""
        # 与最近一次"写入系统剪贴板"的内容相同 → 视为本端/他端写回的，不重复投递
        if self._last_content_hash is not None and self._content_hash(mime_type, data) == self._last_content_hash:
            return False
        self.clipboard_committed.emit(mime_type, data)
        return True

    @staticmethod
    def apply_to_clipboard(mime_type: str, data: bytes) -> str:
        """将接收到的剪切板内容写入本端系统剪贴板。

        当前仅支持文本；非文本返回空字符串（图片已改为按文件处理，不走剪贴板）。

        Args:
            mime_type: "text"
            data: 文本 utf-8 字节
        """
        if mime_type == "text":
            QApplication.clipboard().setText(data.decode('utf-8', errors='replace'))
            return "text"
        return ""