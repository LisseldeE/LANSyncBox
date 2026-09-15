"""
权限管理面板
主机端专用：查看并切换各连接端的同步权限（只读 / 读写）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QWidget, QApplication
)
from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import QPalette, QColor, QPainter

from i18n import I18n
from ui.sync_window import PermSegmentedControl
from ui.widgets import AnimatedButton, BUTTON_STYLES


class PermManageDialog(QDialog):
    """权限管理面板（主机端专用）

    每行一个已认证连接端：IP + 只读/读写权限胶囊。
    - 切换中：该行胶囊置灰（复用模式切换占位，宽度不变避免布局跳动）
    - 收 ACK：胶囊落定 + 底部状态提示「已切换为只读/读写」
    - 重发超限：胶囊回滚 + 底部状态提示「切换失败（对端无响应）」
    - 断开：移除该行
    """

    def __init__(self, server, parent=None):
        super().__init__(parent)
        self.server = server
        self.setWindowTitle(I18n.tr('perm_dialog_title'))
        # 非模态面板：带系统关闭按钮，不阻塞主窗口
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setMinimumWidth(340)
        self.setMaximumWidth(420)

        self._rows = {}          # client_id -> {label, seg}
        self._state_label = None
        self._state_timer = None
        self._empty_label = None

        self._init_ui()

        # 连接服务端权限信号（跨线程信号由 Qt 自动排队回 UI 线程）
        self.server.perm_switching.connect(self._on_perm_switching)
        self.server.perm_ack_received.connect(self._on_perm_ack)
        self.server.perm_switch_failed.connect(self._on_perm_failed)
        self.server.client_connected.connect(lambda cid: self._refresh())
        self.server.client_disconnected.connect(self._on_client_disconnected)

        self._refresh()

    # ---------- UI ----------

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 12, 16, 12)

        # 标题
        title_label = QLabel(I18n.tr('perm_dialog_title'))
        title_label.setStyleSheet("""
            QLabel { font-size: 15px; font-weight: bold; color: #339af0; }
        """)
        layout.addWidget(title_label)

        # 连接端列表滚动区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        self._list_layout.addStretch()
        scroll.setWidget(self._list_container)
        layout.addWidget(scroll, stretch=1)

        # 底部状态提示（轻量反馈，短暂显示后自动清空）
        self._state_label = QLabel('')
        self._state_label.setStyleSheet("font-size: 11px; color: #868e96;")
        self._state_label.setWordWrap(True)
        layout.addWidget(self._state_label)

        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(2000)
        self._state_timer.timeout.connect(lambda: self._state_label.setText(''))

        # 关闭按钮：使用全局取消样式（同进度/创建等对话框），深浅色下均不突兀
        close_btn = AnimatedButton(I18n.tr('close'))
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setFixedSize(100, 30)
        close_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        close_btn.clicked.connect(self.close)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _is_dark(self) -> bool:
        win = self.window()
        pal = win.palette() if win is not None and win is not self else QApplication.palette()
        bg = pal.color(QPalette.Window)
        return (bg.red() * 0.299 + bg.green() * 0.587 + bg.blue() * 0.114) < 128

    # ---------- 行管理 ----------

    def _refresh(self):
        """按当前已认证连接端重建行列表（连接/刷新时调用，行状态保留）"""
        with self.server._lock:
            entries = [(cid, info) for cid, info in list(self.server.clients.items())
                       if info.get('authenticated')]

        # 移除已不存在的行
        for cid in list(self._rows.keys()):
            if cid not in {e[0] for e in entries}:
                self._remove_row(cid)

        for cid, info in entries:
            if cid in self._rows:
                continue
            self._add_row(cid, info)

        # 空状态提示
        empty_visible = not entries
        if self._empty_label is None:
            self._empty_label = QLabel(I18n.tr('perm_dialog_empty'))
            self._empty_label.setStyleSheet("font-size: 12px; color: #adb5bd;")
            self._empty_label.setAlignment(Qt.AlignCenter)
            self._list_layout.insertWidget(0, self._empty_label)
        self._empty_label.setVisible(empty_visible)

    def _add_row(self, client_id: str, info: dict):
        """添加一个连接端行：IP + 权限胶囊"""
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(4, 2, 4, 2)
        row_layout.setSpacing(8)

        ip = info.get('ip') or client_id.split(':')[0]
        ip_label = QLabel(ip)
        dark = self._is_dark()
        ip_label.setStyleSheet(
            f"font-size: 12px; color: {'#a0a0a0' if dark else '#495057'};"
        )
        row_layout.addWidget(ip_label)

        row_layout.addStretch()

        seg = PermSegmentedControl()
        seg.setFixedWidth(100)
        seg.set_perm(info.get('perm', "rw"), animate=False)
        seg.perm_switch_requested.connect(
            lambda perm, cid=client_id: self._on_perm_requested(cid, perm)
        )
        row_layout.addWidget(seg)

        self._rows[client_id] = {'widget': row, 'label': ip_label, 'seg': seg}
        self._list_layout.insertWidget(self._list_layout.count() - 1, row)

    def _remove_row(self, client_id: str):
        """移除一个连接端行"""
        row = self._rows.pop(client_id, None)
        if row:
            row['widget'].deleteLater()

    def _on_client_disconnected(self, client_id: str):
        """连接端断开：移除行"""
        self._remove_row(client_id)
        # 同步刷新空状态
        with self.server._lock:
            remaining = [
                cid for cid, info in self.server.clients.items()
                if info.get('authenticated')
            ]
        if self._empty_label is not None:
            self._empty_label.setVisible(not remaining)

    # ---------- 切换交互 ----------

    def _on_perm_requested(self, client_id: str, target_perm: str):
        """行内胶囊点击：向服务端发起权限切换"""
        row = self._rows.get(client_id)
        if not row:
            return
        if not self.server.set_perm(client_id, target_perm):
            # 服务端拒绝（权限未变化/切换进行中）：恢复胶囊原档位（无动画，避免闪烁）
            with self.server._lock:
                info = self.server.clients.get(client_id)
            row['seg'].set_perm((info or {}).get('perm', "rw"), animate=False)

    def _on_perm_switching(self, client_id: str, new_perm: str):
        """权限切换发起：该行胶囊置灰，等待对端 ACK"""
        row = self._rows.get(client_id)
        if row:
            row['seg'].set_switching(True)

    def _on_perm_ack(self, client_id: str, perm: str):
        """权限应用成功回执：胶囊落定 + 状态提示"""
        row = self._rows.get(client_id)
        if row:
            row['seg'].set_switching(False)
            row['seg'].set_perm(perm, animate=False)
            if perm == "ro":
                self._show_state(I18n.tr('perm_to_readonly'))
            else:
                self._show_state(I18n.tr('perm_to_readwrite'))

    def _on_perm_failed(self, client_id: str, old_perm: str, new_perm: str):
        """权限切换失败（对端无响应）：胶囊回滚 + 状态提示"""
        row = self._rows.get(client_id)
        if row:
            row['seg'].set_switching(False)
            row['seg'].set_perm(old_perm, animate=True)
            self._show_state(I18n.tr('perm_switch_failed'))

    def _show_state(self, message: str):
        """底部状态提示（2 秒后自动清空）"""
        self._state_label.setText(message)
        self._state_timer.start()
