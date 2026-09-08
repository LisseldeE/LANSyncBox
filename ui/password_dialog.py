"""密码输入对话框
点击“加入房间”的连接后，若房间需要密码，由本对话框负责密码输入与验证。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import threading
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
)
from PySide6.QtCore import Qt, QTimer, QEventLoop

from i18n import I18n
from config import Config
from network.client import SyncClient
from ui.widgets import AnimatedButton, BUTTON_STYLES, UnderlineEdit, fade_widget


class PasswordDialog(QDialog):
    """独立密码输入对话框

    由加入房间对话框在检测到需要密码时弹出。负责：
    - 输入密码
    - 预验证（连接主机 + 认证）
    - 成功后回传验证通过的 Client 实例与密码（供 SyncWindow 复用，避免重复连接）
    """

    def __init__(self, room_code: str, host: str, port: int, parent=None):
        super().__init__(parent)
        self.room_code = room_code
        self.host = host or "127.0.0.1"
        self.port = port or Config.DEFAULT_PORT

        self.password = ""
        self._is_verifying = False
        self._client = None          # 验证成功后保留的 Client 实例
        self._verified_client = None # 转移给调用方的只读引用
        self._fade_animations = {}

        self._init_ui()

    def _init_ui(self):
        """构建精致、符合全局风格的密码输入界面"""
        self.setWindowTitle(I18n.tr('password_dialog_title'))
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # 标题
        title = QLabel(I18n.tr('password_dialog_title'))
        title.setStyleSheet("font-size: 15px; font-weight: bold; color: palette(text);")
        layout.addWidget(title)

        # 房间提示
        hint = QLabel(I18n.tr('password_dialog_hint', code=self.room_code))
        hint.setStyleSheet("font-size: 12px; color: #868e96;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 密码输入框（跟随全局默认输入框样式）
        self.password_edit = UnderlineEdit()
        self.password_edit.setPlaceholderText(I18n.tr('password_hint'))
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setClearButtonEnabled(True)
        self.password_edit.textChanged.connect(self._on_password_changed)
        layout.addWidget(self.password_edit)

        # 校验状态：固定高度占位（始终占位、不参与高度变化），错误时红色提示。
        # 固定高度避免文字出现/消失导致布局挤压、界面高度跳变。
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #ff6b6b; font-size: 11px;")
        self.status_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.status_label.setFixedHeight(20)
        self.status_label.setText("")  # 占位，无错误时保持空白
        layout.addWidget(self.status_label)

        # 按钮
        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        self.confirm_btn = AnimatedButton(I18n.tr('password_confirm'))
        self.confirm_btn.setStyleSheet(BUTTON_STYLES['primary'])
        self.confirm_btn.setFixedWidth(88)
        self.confirm_btn.setDefault(True)
        self.confirm_btn.clicked.connect(self.on_confirm)
        self.confirm_btn.setEnabled(False)  # 密码为空时不可确认
        button_row.addStretch()
        button_row.addWidget(self.confirm_btn)

        self.cancel_btn = AnimatedButton(I18n.tr('cancel'))
        self.cancel_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        self.cancel_btn.setFixedWidth(88)
        self.cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(self.cancel_btn)
        layout.addLayout(button_row)

        self.setMinimumWidth(320)
        self.setMaximumWidth(420)

    # ------------------------------------------------------------------ 交互

    def _on_password_changed(self, text):
        """密码内容变化：无内容时禁用确认，并清除上一次的错误提示"""
        self.confirm_btn.setEnabled(bool(text.strip()))
        # 直接清空文本（label 固定占位高度，不触发布局变化）
        if self.status_label.text():
            self.status_label.clear()

    def on_confirm(self):
        """确认并验证密码"""
        if self._is_verifying:
            return
        pwd = self.password_edit.text().strip()
        if not pwd:
            return
        self.password = pwd
        self._verify()

    def _set_verifying(self, verifying: bool):
        """切换验证中状态：禁用按钮，显示加载中"""
        self._is_verifying = verifying
        self.confirm_btn.setEnabled(not verifying and bool(self.password_edit.text().strip()))
        self.cancel_btn.setEnabled(not verifying)
        if verifying:
            self._show_status(I18n.tr('verifying'), error=False)

    def _show_status(self, text, error=True):
        """显示状态提示（error=True 红色，error=False 蓝色）"""
        self.status_label.setStyleSheet(
            "color: #ff6b6b; font-size: 11px;" if error else
            "color: #339af0; font-size: 11px;"
        )
        self.status_label.setText(text)
        fade_widget(self, self.status_label, True, duration=150)

    # ---------------------------------------------------------------- 验证

    def _verify(self):
        """预验证密码：后台连接主机 + 认证，成功后 accept 并保留 Client"""
        self._set_verifying(True)

        client = SyncClient(self.room_code, self.password)
        self._client = client

        loop = QEventLoop(self)
        timeout_timer = QTimer(self)
        timeout_timer.setSingleShot(True)
        result = {'status': None, 'message': ''}

        def on_connected():
            result['status'] = 'success'
            timeout_timer.stop()
            loop.quit()

        def on_auth_failed(msg):
            result['status'] = 'failed'
            result['message'] = msg or I18n.tr('incorrect_password')
            timeout_timer.stop()
            loop.quit()

        def on_error(msg):
            if result['status'] is None:
                result['status'] = 'error'
                result['message'] = msg or I18n.tr('connection_failed')
                timeout_timer.stop()
                loop.quit()

        def on_timeout():
            if result['status'] is None:
                result['status'] = 'timeout'
                loop.quit()

        client.connected.connect(on_connected)
        client.auth_failed.connect(on_auth_failed)
        client.error_occurred.connect(on_error)
        timeout_timer.timeout.connect(on_timeout)

        def _connect_task():
            # 后台线程：只写共享 result，不操作主线程 Qt 对象（timeout_timer/loop）。
            # 事件循环退出由 on_* 信号或超时兜底完成，避免跨线程 killedTimer。
            try:
                ok = client.connect_to_server(self.host, self.port)
                if not ok and result['status'] is None:
                    result['status'] = 'error'
                    result['message'] = I18n.tr('connection_failed')
            except Exception:
                if result['status'] is None:
                    result['status'] = 'error'
                    result['message'] = I18n.tr('connection_failed')

        threading.Thread(target=_connect_task, daemon=True).start()

        timeout_timer.start(10000)
        loop.exec()

        # 成功：保留 Client 供调用方复用
        if result['status'] == 'success':
            try:
                client.connected.disconnect(on_connected)
                client.auth_failed.disconnect(on_auth_failed)
                client.error_occurred.disconnect(on_error)
            except Exception:
                pass
            self._verified_client = client
            self._set_verifying(False)
            self.accept()
            return

        # 失败/超时/错误：断开 Client，显示错误，保持对话框打开
        self._set_verifying(False)
        self._client = None
        try:
            client.disconnect()
        except Exception:
            pass

        if result['status'] == 'failed':
            self._show_status(result['message'], error=True)
        elif result['status'] == 'timeout':
            self._show_status(I18n.tr('connection_failed'), error=True)
        else:
            self._show_status(result['message'] or I18n.tr('connection_failed'), error=True)

        # 失败后聚焦密码框，便于直接修改重试
        self.password_edit.setFocus()
        self.password_edit.selectAll()

    # ------------------------------------------------------------------ 取值

    def get_verified_client(self):
        """获取验证成功、待复用给同步窗口的 Client 实例"""
        client = self._verified_client
        self._verified_client = None
        return client

    def get_password(self) -> str:
        """获取用户输入的密码"""
        return self.password

    def keyPressEvent(self, event):
        """回车即确认；Esc 取消（若未在验证中）"""
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.on_confirm()
            return
        super().keyPressEvent(event)