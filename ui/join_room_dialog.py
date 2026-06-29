"""
加入房间对话框
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFrame, QMessageBox, QWidget,
    QGraphicsOpacityEffect, QApplication
)
from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QByteArray, QEventLoop
from PySide6.QtGui import QFont, QValidator, QKeyEvent

from i18n import I18n
from config import Config
from network.discovery import RoomDiscovery
from network.client import SyncClient
from ui.widgets import AnimatedButton, BUTTON_STYLES, fade_widget


class DigitValidator(QValidator):
    """数字验证器 - 只允许输入单个数字"""
    
    def validate(self, text, pos):
        if text == '' or text.isdigit():
            return QValidator.Acceptable, text, pos
        return QValidator.Invalid, text, pos


class DigitLineEdit(QLineEdit):
    """数字输入框 - 支持退格键自动向前删除"""
    
    backspace_pressed = Signal()  # 退格键按下信号
    paste_requested = Signal(str)  # 粘贴请求信号
    
    def keyPressEvent(self, event: QKeyEvent):
        """键盘事件处理"""
        # 检测粘贴操作 (Ctrl+V)
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_V:
            # 发射粘贴信号，让父组件处理
            clipboard = QApplication.clipboard()
            text = clipboard.text()
            self.paste_requested.emit(text)
            return
        
        if event.key() == Qt.Key_Backspace:
            # 如果当前格子为空，发送信号让父组件处理
            if not self.text():
                self.backspace_pressed.emit()
                return
            # 如果当前格子有内容，正常删除
            super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)


class RoomCodeInput(QWidget):
    """房间号输入组件 - 6个格子输入6个数字"""
    
    code_completed = Signal()  # 输入完成信号
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.digit_edits = []
        self._last_complete_state = False  # 记录上一次的完成状态
        self._init_ui()
    
    def _init_ui(self):
        """初始化界面"""
        layout = QHBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # 创建6个数字输入格子
        for i in range(6):
            edit = DigitLineEdit()
            edit.setAlignment(Qt.AlignCenter)
            edit.setMaxLength(1)
            edit.setMinimumSize(40, 50)
            edit.setMaximumSize(50, 60)
            
            # 使用系统颜色适配深色/浅色模式
            edit.setStyleSheet("""
                QLineEdit {
                    font-size: 28px;
                    font-weight: bold;
                    background-color: palette(base);
                    border: 2px solid palette(mid);
                    border-radius: 6px;
                    color: palette(text);
                }
                QLineEdit:focus {
                    border: 2px solid #339af0;
                }
            """)
            
            # 只允许输入数字
            edit.setValidator(DigitValidator())
            
            # 输入后自动跳转到下一个
            edit.textChanged.connect(lambda text, idx=i: self._on_text_changed(text, idx))
            
            # 处理退格键
            edit.backspace_pressed.connect(lambda idx=i: self._on_backspace_pressed(idx))
            
            # 第一个输入框支持粘贴全部6位房间号
            if i == 0:
                edit.paste_requested.connect(self._on_paste_requested)
            
            self.digit_edits.append(edit)
            layout.addWidget(edit)
        
        # 设置字体
        font = QFont()
        font.setPointSize(20)
        font.setBold(True)
        for edit in self.digit_edits:
            edit.setFont(font)
    
    def _on_backspace_pressed(self, index: int):
        """处理退格键按下"""
        if index > 0:
            # 移动到前一个格子并清空
            prev_edit = self.digit_edits[index - 1]
            prev_edit.clear()
            prev_edit.setFocus()
    
    def _on_paste_requested(self, text: str):
        """处理粘贴请求"""
        # 检查粘贴的内容是否是6位数字
        text = text.strip()
        if len(text) == 6 and text.isdigit():
            # 填充到所有输入框
            for i, digit in enumerate(text):
                self.digit_edits[i].setText(digit)
            
            # 移动焦点到最后一个输入框
            self.digit_edits[5].setFocus()
    
    def _on_text_changed(self, text: str, index: int):
        """文本改变时自动跳转"""
        if text and index < 5:
            # 输入了数字，跳转到下一个
            self.digit_edits[index + 1].setFocus()
        
        # 检查是否输入完成（只在从未完成变为完成时发送信号）
        is_complete = self.is_complete()
        if is_complete and not self._last_complete_state:
            self.code_completed.emit()
        self._last_complete_state = is_complete
    
    def set_room_code(self, code: str):
        """设置房间号"""
        code = code.zfill(6)
        for i, digit in enumerate(code[:6]):
            self.digit_edits[i].setText(digit)
        # 更新完成状态
        self._last_complete_state = self.is_complete()
    
    def get_room_code(self) -> str:
        """获取房间号"""
        return "".join(edit.text() for edit in self.digit_edits)
    
    def is_complete(self) -> bool:
        """检查是否已输入完整的6位数字"""
        return all(edit.text().isdigit() for edit in self.digit_edits)
    
    def clear(self):
        """清空输入"""
        for edit in self.digit_edits:
            edit.clear()
        self.digit_edits[0].setFocus()
        self._last_complete_state = False


class JoinRoomDialog(QDialog):
    """加入房间对话框"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.room_code = ""
        self.password = ""
        self.host_address = ""
        self.discovered_host = ""  # 发现的主机地址
        self.host_port = Config.DEFAULT_PORT  # 发现的主机端口（默认9527）
        self._fade_animations = {}  # 动画字典
        self._room_requires_password = False  # 房间是否需要密码
        self._room_checked = False  # 房间是否已检测
        self._is_checking = False  # 是否正在检测中
        self._is_verifying = False  # 是否正在验证密码
        self._verified_client = None  # 预验证成功的 Client 实例（传递给 SyncWindow 复用）
        self.init_ui()
    
    def init_ui(self):
        """初始化界面"""
        self.setWindowTitle(I18n.tr('join_room_title'))
        self.setModal(True)
        self.setFixedWidth(400)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # 房间号输入
        room_code_layout = QVBoxLayout()
        room_code_label = QLabel(I18n.tr('room_code'))
        room_code_layout.addWidget(room_code_label)
        
        # 房间号输入组件（6个格子）
        self.room_code_input = RoomCodeInput()
        # 连接输入完成信号，自动检测房间
        self.room_code_input.code_completed.connect(self._on_code_completed)
        room_code_layout.addWidget(self.room_code_input)
        
        # 状态标签（显示扫描状态）
        self.status_label = QLabel(I18n.tr('ready_waiting'))
        self.status_label.setStyleSheet("color: #868e96; font-size: 12px;")
        self.status_label.setWordWrap(True)
        room_code_layout.addWidget(self.status_label)
        
        layout.addLayout(room_code_layout)
        
        # 密码输入（默认隐藏）
        self.password_widget = QWidget()
        password_layout = QVBoxLayout(self.password_widget)
        password_layout.setContentsMargins(0, 0, 0, 0)
        
        password_label = QLabel(I18n.tr('password'))
        password_layout.addWidget(password_label)
        
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText(I18n.tr('password_hint'))
        self.password_edit.setEchoMode(QLineEdit.Password)
        password_layout.addWidget(self.password_edit)
        
        self.password_widget.setVisible(False)  # 默认隐藏
        layout.addWidget(self.password_widget)
        
        # 主机地址输入（可选）
        host_layout = QVBoxLayout()
        host_label = QLabel(I18n.tr('host_address_optional'))
        host_layout.addWidget(host_label)
        
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText(I18n.tr('host_address_hint'))
        host_layout.addWidget(self.host_edit)
        
        layout.addLayout(host_layout)
        
        # 弹性空间
        layout.addStretch()
        
        # 按钮
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        
        self.connect_btn = AnimatedButton(I18n.tr('connect'))
        self.connect_btn.setFixedWidth(100)
        self.connect_btn.clicked.connect(self.on_connect)
        self.connect_btn.setDefault(True)
        self.connect_btn.setStyleSheet(BUTTON_STYLES['primary'])
        
        self.cancel_btn = AnimatedButton(I18n.tr('cancel'))
        self.cancel_btn.setFixedWidth(100)
        self.cancel_btn.clicked.connect(self.reject)
        self.cancel_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        
        button_layout.addStretch()
        button_layout.addWidget(self.connect_btn)
        button_layout.addWidget(self.cancel_btn)
        
        layout.addLayout(button_layout)
    
    def _on_code_completed(self):
        """输入完成时自动检测房间"""
        # 如果正在检测中，则不触发
        if self._is_checking:
            return
        
        # 重新启用连接按钮（可能因版本不一致被禁用）
        self.connect_btn.setEnabled(True)
        
        # 添加一个小延迟，让用户看到输入完成
        QTimer.singleShot(300, self._check_room_exists)
    
    def _check_room_exists(self):
        """检测房间是否存在"""
        self._is_checking = True
        room_code = self.room_code_input.get_room_code()
        
        # 验证房间号
        if not self.room_code_input.is_complete():
            self._is_checking = False
            QMessageBox.warning(self, I18n.tr('join_room_title'), I18n.tr('invalid_room_code'))
            return
        
        # 如果用户指定了主机地址，直接使用
        host_address = self.host_edit.text().strip()
        if host_address:
            # 显示状态：已找到房间
            self._show_status(I18n.tr('room_found_manual'), color='#51cf66')
            self._room_checked = True
            self._is_checking = False
            # 显示密码输入框
            self._show_password_input()
            return
        
        # 没有指定主机地址，进行房间发现
        # 显示状态：正在搜索房间
        self._show_status(I18n.tr('searching_room'), color='#339af0')
        
        # 创建房间发现服务
        self.discovery = RoomDiscovery(self)
        self.discovery.room_found.connect(self.on_room_found)
        self.discovery.discovery_finished.connect(self.on_discovery_finished)
        self.discovery.error_occurred.connect(self.on_discovery_error)
        
        # 保存房间号
        self._pending_room_code = room_code
        
        # 开始发现
        self.discovery.discover_room(room_code, timeout=3)
    
    def _show_status(self, text: str, color: str = '#868e96'):
        """显示状态标签"""
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")
    
    def on_room_found(self, host_ip: str, room_code: str, port: int, version: str = ""):
        """发现房间
        Args:
            host_ip: 主机IP
            room_code: 房间号
            port: 端口
            version: 主机版本号
        """
        # 找到房间，停止发现
        self.discovery.stop_discovery()
        
        self.room_code = room_code
        self.host_address = host_ip
        self.host_port = port
        self.discovered_host = host_ip
        
        # 版本号核对
        local_version = Config.APP_VERSION
        if version and version != local_version:
            # 版本不一致：红字显示，禁用连接按钮
            self._show_status(
                I18n.tr('version_mismatch', local=local_version, remote=version),
                color='#ff6b6b'
            )
            self.connect_btn.setEnabled(False)
            self._room_checked = False
            self._is_checking = False
            return
        
        # 版本一致：显示已找到房间
        self._show_status(I18n.tr('room_found', ip=host_ip), color='#51cf66')
        self._room_checked = True
        self._is_checking = False
        
        # 显示密码输入框
        self._show_password_input()
    
    def on_discovery_finished(self, rooms: list):
        """发现完成"""
        self._is_checking = False
        if not rooms:
            # 没有找到房间
            self._show_status(I18n.tr('room_not_found'), color='#ff6b6b')
            self._room_checked = False
    
    def on_discovery_error(self, error: str):
        """发现错误"""
        self._show_status(error, color='#ff6b6b')
        self._room_checked = False
        self._is_checking = False
    
    def _show_password_input(self):
        """显示密码输入框（淡入动画）"""
        self._room_requires_password = True
        fade_widget(self, self.password_widget, True, duration=200)
    
    def _hide_password_input(self):
        """隐藏密码输入框（淡出动画）"""
        self._room_requires_password = False
        fade_widget(self, self.password_widget, False, duration=150)
    
    def on_connect(self):
        """连接房间（先预验证密码，成功后 accept；失败则在对话框内显示错误）"""
        # 防止重复点击
        if self._is_verifying:
            return

        room_code = self.room_code_input.get_room_code()

        # 验证房间号
        if not self.room_code_input.is_complete():
            QMessageBox.warning(self, I18n.tr('join_room_title'), I18n.tr('invalid_room_code'))
            return

        # 如果没有检测过房间，先检测
        if not self._room_checked and not self.host_edit.text().strip():
            self._check_room_exists()
            return

        # 设置房间信息
        self.room_code = room_code
        self.password = self.password_edit.text()

        # 如果用户指定了主机地址，使用它
        host_address = self.host_edit.text().strip()
        if host_address:
            self.host_address = host_address
            self.discovered_host = host_address

        # 预验证：连接主机并验证密码
        self._verify_password()

    def _verify_password(self):
        """预验证密码：创建临时 Client 连接 + 验证，成功后保留 client 并 accept"""
        self._is_verifying = True
        self.connect_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self._show_status(I18n.tr('verifying'), color='#339af0')

        host = self.host_address or "127.0.0.1"
        port = self.host_port or Config.DEFAULT_PORT

        # 创建临时 Client 进行验证
        client = SyncClient(self.room_code, self.password)
        self._verified_client = client

        # 用事件循环等待验证结果
        loop = QEventLoop(self)
        timeout_timer = QTimer(self)
        timeout_timer.setSingleShot(True)

        result = {'status': None, 'message': ''}  # None / 'success' / 'failed' / 'timeout' / 'error'

        def on_connected():
            result['status'] = 'success'
            timeout_timer.stop()
            loop.quit()

        def on_auth_failed(msg):
            result['status'] = 'failed'
            result['message'] = msg
            timeout_timer.stop()
            loop.quit()

        def on_error(msg):
            if result['status'] is None:
                result['status'] = 'error'
                result['message'] = msg
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

        # 尝试连接
        if not client.connect_to_server(host, port):
            # 连接建立失败（同步返回 False）
            self._is_verifying = False
            self.connect_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)
            self._verified_client = None
            try:
                client.disconnect()
            except Exception:
                pass
            self._show_status(I18n.tr('connection_failed'), color='#ff6b6b')
            return

        # 等待验证结果（10 秒超时）
        timeout_timer.start(10000)
        loop.exec()

        # 处理结果
        if result['status'] == 'success':
            # 验证成功：断开临时信号连接（避免 SyncWindow 复用时闭包被意外调用），保留 client 实例
            try:
                client.connected.disconnect(on_connected)
                client.auth_failed.disconnect(on_auth_failed)
                client.error_occurred.disconnect(on_error)
            except Exception:
                pass
            self._is_verifying = False
            self._show_status(I18n.tr('room_found', ip=host), color='#51cf66')
            self.accept()
            return

        # 验证失败 / 超时 / 错误：断开 client，显示错误，保持对话框打开
        self._is_verifying = False
        self.connect_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self._verified_client = None

        try:
            client.disconnect()
        except Exception:
            pass

        # 移除房间号输入框的焦点（QEventLoop 退出后 QDialog 会自动聚焦第一个可聚焦控件）
        for edit in self.room_code_input.digit_edits:
            edit.clearFocus()
        # 将焦点设置到密码输入框，方便用户直接修改密码重试
        if self._room_requires_password:
            self.password_edit.setFocus()
            self.password_edit.selectAll()

        if result['status'] == 'failed':
            # 验证失败：直接显示服务器返回的错误信息（如 "密码错误"、"房间号错误"）
            self._show_status(result['message'] or I18n.tr('auth_failed'), color='#ff6b6b')
        elif result['status'] == 'timeout':
            self._show_status(I18n.tr('connection_failed'), color='#ff6b6b')
        elif result['status'] == 'error':
            self._show_status(result['message'] or I18n.tr('connection_failed'), color='#ff6b6b')

    def get_verified_client(self):
        """获取预验证成功的 Client 实例（供 SyncWindow 复用，避免重复连接）"""
        client = self._verified_client
        self._verified_client = None  # 转移所有权
        return client

    def get_room_code(self) -> str:
        """获取房间号"""
        return self.room_code
    
    def get_password(self) -> str:
        """获取密码"""
        return self.password
    
    def get_host_address(self) -> str:
        """获取主机地址"""
        return self.host_address
    
    def get_host_port(self) -> int:
        """获取主机端口"""
        return self.host_port
    
    def get_discovered_host(self) -> str:
        """获取发现的主机地址"""
        return self.discovered_host
