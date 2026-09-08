"""加入房间对话框
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import threading
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFrame, QMessageBox, QWidget,
    QGraphicsOpacityEffect, QApplication, QListWidgetItem,
    QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QByteArray, QEventLoop, QSize
from PySide6.QtGui import QFont, QValidator, QKeyEvent, QShowEvent, QColor, QPalette

from i18n import I18n
from config import Config, UserConfig
from network.discovery import RoomDiscovery, RoomProbe
from network.client import SyncClient
from ui.widgets import AnimatedButton, SnapOutlineButton, BUTTON_STYLES, UnderlineEdit
from ui.loading_animation import PageLoader, LoaderState
from ui.smooth_scroll import SmoothScrollList


def _hover_gray():
    """返回适配当前明暗模式的浅灰悬浮色：深色用亮灰叠层，浅色用浅灰叠层"""
    base = QApplication.palette().color(QPalette.Window)
    luminance = base.red() * 0.299 + base.green() * 0.587 + base.blue() * 0.114
    return "rgba(255, 255, 255, 0.10)" if luminance < 128 else "rgba(0, 0, 0, 0.08)"


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
    code_changed = Signal()  # 输入变化信号（用于实时匹配检测）

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

        # 发射输入变化信号（实时匹配检测）
        self.code_changed.emit()

        # 检查是否输入完成（只在从未完成变为完成时发送信号）
        is_complete = self.is_complete()
        if is_complete and not self._last_complete_state:
            self.code_completed.emit()
        self._last_complete_state = is_complete
    
    def set_room_code(self, code: str, trigger_check: bool = True):
        """设置房间号
        
        Args:
            code: 房间号（6位数字）
            trigger_check: 是否触发检测（通过列表点击时为False，手动输入时为True）
        """
        code = code.zfill(6)
        # 阻塞信号，避免触发6次 code_changed
        self.blockSignals(True)
        for i, digit in enumerate(code[:6]):
            self.digit_edits[i].setText(digit)
        self.blockSignals(False)
        # 更新完成状态
        self._last_complete_state = self.is_complete()
        # 手动触发一次 code_changed（更新列表项样式）
        self.code_changed.emit()
        # 如果输入完整且需要检测，触发 code_completed
        if self._last_complete_state and trigger_check:
            self.code_completed.emit()
    
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


class RoomRowWidget(QWidget):
    """房间行控件：左侧细竖指示条 + 房间号/IP（+ 可选右侧删除『×』按钮）

    历史行：指示条绿/黄动态，带删除按钮；
    扫描行：指示条固定主题蓝，无删除按钮。
    点击行主体（排除删除按钮）触发 clicked 信号用于填充输入框。
    """
    clicked = Signal()          # 点击行主体（填充输入框）
    remove_requested = Signal()  # 点击『×』删除该历史

    INDICATOR_WIDTH = 3  # 指示条宽度（px）
    ROW_HEIGHT = 34      # 行高（固定，保证指示条顶满整行）

    # 状态色常量：扫描行/历史行的指示条与左下角图例共用同一来源，避免颜色漂移
    COLOR_SCANNED = "#339af0"   # 扫描发现的房间（固定主题蓝）
    COLOR_ONLINE = "#69db7c"    # 历史房间在线
    COLOR_OFFLINE = "#ffd43b"   # 历史房间离线

    def __init__(self, room_code: str, ip: str, indicator_color: str = "",
                 show_delete: bool = True, parent=None):
        super().__init__(parent)
        self.room_code = room_code
        self.ip = ip
        self._fixed_color = indicator_color  # 固定色指示条（扫描行）；空则动态（历史行）
        self._init_ui(show_delete)

    def _init_ui(self, show_delete: bool):
        self.setCursor(Qt.PointingHandCursor)
        # 固定行高：让 widget 与 item 高度一致，指示条才能顶满，杜绝下方空隙
        self.setFixedHeight(self.ROW_HEIGHT)
        layout = QHBoxLayout(self)
        # 右边缘留与上下垂直居中相同的空闲（右侧删除叉号不再贴边）
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(6)

        # 左侧细竖指示条：历史默认黄色（探测后变绿），扫描固定主题蓝
        # 垂直拉伸顶满整行高度（状态边条式），不依赖外部行高
        default_color = self._fixed_color or self.COLOR_OFFLINE
        self._bar_background = QFrame()
        self._bar_background.setFixedWidth(self.INDICATOR_WIDTH)
        self._bar_background.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self._bar_background.setStyleSheet(
            f"background: {default_color};"
        )
        layout.addWidget(self._bar_background, 0)

        # 文字：房间号 (IP)（使用调色板默认文本色，自适应明暗）
        self._label = QLabel(f"{self.room_code} ({self.ip})")
        self._label.setStyleSheet("color: palette(text);")
        layout.addWidget(self._label, 1, Qt.AlignVCenter)

        # 删除按钮『×』（仅历史行显示）
        self._del_btn = QPushButton("×")
        self._del_btn.setFixedSize(18, 18)
        self._del_btn.setCursor(Qt.PointingHandCursor)
        self._del_btn.setToolTip("删除该历史")
        self._del_btn.setStyleSheet("""
            QPushButton {
                border: none;
                background: transparent;
                color: #868e96;
                font-size: 15px;
                font-weight: bold;
            }
            QPushButton:hover {
                color: #ff6b6b;
            }
        """)
        self._del_btn.clicked.connect(self.remove_requested.emit)
        layout.addWidget(self._del_btn, 0, Qt.AlignVCenter)
        self._del_btn.setVisible(show_delete)

    def set_status(self, reachable: bool):
        """更新指示条状态：绿=找到，黄=未找到（仅动态指示条的历史行生效）"""
        if self._fixed_color:
            return  # 固定色行不受探测影响
        color = self.COLOR_ONLINE if reachable else self.COLOR_OFFLINE
        self._bar_background.setStyleSheet(
            f"background: {color};"
        )

    def set_matched(self, matched: bool):
        """匹配当前输入的房间号时置灰并禁用交互；否则恢复"""
        if matched:
            self._label.setStyleSheet("color: #868e96; background: transparent;")
            self.setEnabled(False)
        else:
            self._label.setStyleSheet("color: palette(text); background: transparent;")
            self.setEnabled(True)

    def mousePressEvent(self, event):
        # 点击行主体（非删除按钮区域）触发填充
        if event.button() == Qt.LeftButton:
            # 若事件落在删除按钮上则由按钮自行处理，此处忽略
            if self._del_btn.rect().contains(self._del_btn.mapFromGlobal(event.globalPosition().toPoint())):
                return
            self.clicked.emit()
        super().mousePressEvent(event)


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
        self._room_checked = False  # 房间是否已检测
        self._is_checking = False  # 是否正在检测中
        self._is_verifying = False  # 是否正在验证密码
        self._verified_client = None  # 预验证成功的 Client 实例（传递给 SyncWindow 复用）
        self._is_scanning = False  # 是否正在扫描所有房间
        self._scan_discovery = None  # 扫描发现服务
        self._discovered_rooms_list = []  # 扫描发现的房间列表
        self._first_show = True  # 是否首次显示
        self._loader = None  # 加载动画组件
        self._history_items = []  # 历史行控件列表 [RoomRowWidget, ...]
        self._room_probe = None  # 历史可达性探测服务
        self._manual_ip = ""  # 手动指定的主机地址（定向探测目标）
        self.init_ui()
        # 周期刷新：每 1s 重新探测历史行与手动目标的可达性，按各自绿/黄状态更新指示条
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._on_periodic_refresh)

    def _on_periodic_refresh(self):
        """周期刷新历史行指示条：按各自最新绿/黄状态刷新列表展示"""
        self._probe_history()

    def _cleanup_background(self):
        """对话框关闭时清理后台活动：停止周期定时器与所有探测/扫描服务"""
        if self._refresh_timer is not None and self._refresh_timer.isActive():
            self._refresh_timer.stop()
        # 停止历史可达性探测
        if self._room_probe is not None:
            try:
                self._room_probe.stop_probing()
            except Exception:
                pass
            self._room_probe = None
        # 停止房间发现服务（手动/扫描共用 self.discovery 会在各自结束点清理，此处兜底）
        for probe in (getattr(self, 'discovery', None), getattr(self, '_scan_discovery', None)):
            if probe is not None:
                try:
                    probe.stop_discovery()
                except Exception:
                    pass

    def accept(self):
        """连接成功：关闭前清理后台活动（定时器/探测/扫描）"""
        self._cleanup_background()
        super().accept()

    def reject(self):
        """关闭（取消/ESC/右上角）：清理后台活动"""
        self._cleanup_background()
        super().reject()

    def closeEvent(self, event):
        """窗口关闭：清理后台活动"""
        self._cleanup_background()
        super().closeEvent(event)

    def showEvent(self, event: QShowEvent):
        """对话框显示事件 - 首次显示时加载历史并自动扫描房间"""
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            # 加载历史房间记录到列表顶部
            self._load_history()
            # 启动周期刷新定时器（不重复开启）
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
            # 延迟启动扫描（等待对话框完全显示）
            QTimer.singleShot(100, self._start_scan_all_rooms)
    
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
        # 连接输入变化信号，实时匹配列表项
        self.room_code_input.code_changed.connect(self._update_matching_room_style)
        self.room_code_input.code_changed.connect(self._on_room_code_input_changed)
        room_code_layout.addWidget(self.room_code_input)
        
        # 状态标签（显示扫描状态）
        self.status_label = QLabel(I18n.tr('ready_waiting'))
        self.status_label.setStyleSheet("color: #868e96; font-size: 12px;")
        self.status_label.setWordWrap(True)
        room_code_layout.addWidget(self.status_label)
        
        layout.addLayout(room_code_layout)
        
        # 主机地址输入（可选）
        host_layout = QVBoxLayout()
        host_label = QLabel(I18n.tr('host_address_optional'))
        host_layout.addWidget(host_label)

        self.host_edit = UnderlineEdit()
        self.host_edit.setPlaceholderText(I18n.tr('host_address_hint'))
        host_layout.addWidget(self.host_edit)

        layout.addLayout(host_layout)

        # 发现房间板块
        discover_layout = QVBoxLayout()

        # 标题和刷新按钮
        discover_header = QHBoxLayout()
        discover_label = QLabel(I18n.tr('discover_rooms'))
        discover_label.setStyleSheet("font-weight: bold;")
        discover_header.addWidget(discover_label)

        self.scan_btn = SnapOutlineButton(I18n.tr('refresh_scan'))
        self.scan_btn.setFixedWidth(80)
        self.scan_btn.clicked.connect(self._start_scan_all_rooms)
        discover_header.addStretch()
        discover_header.addWidget(self.scan_btn)
        discover_layout.addLayout(discover_header)

        # 扫描状态标签
        self.scan_status_label = QLabel(I18n.tr('discover_rooms_hint'))
        self.scan_status_label.setStyleSheet("color: #868e96; font-size: 12px;")

        # 扫描提示文字 + 加载动画同一行（动画靠右）
        scan_status_row = QHBoxLayout()
        scan_status_row.setSpacing(6)
        scan_status_row.addWidget(self.scan_status_label)

        # 加载动画容器（固定宽度避免水平跳动，高度贴合内容避免上下空白）
        loader_container = QWidget()
        loader_container.setFixedWidth(90)
        loader_layout = QHBoxLayout(loader_container)
        loader_layout.setContentsMargins(0, 0, 0, 0)

        # 加载动画（状态二：中间状态）
        self._loader = PageLoader()
        self._loader.set_state(LoaderState.INTERMEDIATE)
        loader_layout.addWidget(self._loader)
        self._loader.hide()  # 初始隐藏

        scan_status_row.addStretch()
        scan_status_row.addWidget(loader_container, 0, Qt.AlignRight | Qt.AlignVCenter)
        discover_layout.addLayout(scan_status_row)

        # 发现的房间列表（平滑滚动控件：滚轮带缓动动画与惯性手感）
        self.rooms_list_widget = SmoothScrollList()
        self.rooms_list_widget.setMaximumHeight(150)
        _hover = _hover_gray()
        self.rooms_list_widget.setStyleSheet(f"""
            QListWidget {{
                border: 1px solid palette(mid);
                border-radius: 4px;
                background-color: palette(base);
                outline: none;
            }}
            QListWidget::item {{
                padding: 0px;
            }}
            QListWidget::item:selected {{
                background-color: {_hover};
                color: palette(text);
                border: none;
            }}
            QListWidget::item:hover:!disabled {{
                background-color: {_hover};
                color: palette(text);
                border: none;
            }}
        """)
        self.rooms_list_widget.itemClicked.connect(self._on_room_item_clicked)
        discover_layout.addWidget(self.rooms_list_widget)

        layout.addLayout(discover_layout)

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
        self.connect_btn.setEnabled(False)  # 初始禁用，等待房间号输入完成

        self.cancel_btn = AnimatedButton(I18n.tr('cancel'))
        self.cancel_btn.setFixedWidth(100)
        self.cancel_btn.clicked.connect(self.reject)
        self.cancel_btn.setStyleSheet(BUTTON_STYLES['secondary'])

        # 左下角状态图例：竖排三项，每行色块+文字并排，整体缩小紧凑
        legend_layout = QVBoxLayout()
        legend_layout.setSpacing(1)
        for _color, _text in (
            (RoomRowWidget.COLOR_SCANNED, I18n.tr('legend_scanned')),
            (RoomRowWidget.COLOR_ONLINE, I18n.tr('legend_history_online')),
            (RoomRowWidget.COLOR_OFFLINE, I18n.tr('legend_history_offline')),
        ):
            _item_row = QHBoxLayout()
            _item_row.setSpacing(5)
            _dot = QFrame()
            _dot.setFixedSize(8, 8)
            _dot.setStyleSheet(f"background: {_color}; border-radius: 2px;")
            _item_row.addWidget(_dot)
            _lbl = QLabel(_text)
            _lbl.setStyleSheet("color: #868e96; font-size: 10px;")
            _item_row.addWidget(_lbl)
            _item_row.addStretch()
            legend_layout.addLayout(_item_row)
        button_layout.addLayout(legend_layout)

        button_layout.addStretch()  # 弹性空间，让按钮靠右
        button_layout.addWidget(self.connect_btn)
        button_layout.addWidget(self.cancel_btn)

        layout.addLayout(button_layout)
    
    def _on_code_completed(self):
        """输入完成时自动检测房间（探测确认存在后才可点连接）"""
        # 如果正在检测中，则不触发
        if self._is_checking:
            return

        # 探测期间保持连接按钮禁用，可否点击交由探测结果判定（存在才可点）
        self.connect_btn.setEnabled(False)

        # 添加一个小延迟，让用户看到输入完成
        QTimer.singleShot(300, self._check_room_exists)
    
    def _on_room_code_input_changed(self):
        """房间号输入变化时更新连接按钮状态，并清除"已找到"等历史状态"""
        if not self.room_code_input.is_complete():
            self.connect_btn.setEnabled(False)
            # 房间号不完整时清除"已找到房间"等先前结果
            self._room_checked = False
            if not self._is_checking:
                self._show_status('', color='#868e96')
    
    def _check_room_exists(self):
        """检测房间是否存在"""
        self._is_checking = True
        room_code = self.room_code_input.get_room_code()
        
        # 验证房间号
        if not self.room_code_input.is_complete():
            self._is_checking = False
            QMessageBox.warning(self, I18n.tr('join_room_title'), I18n.tr('invalid_room_code'))
            return
        
        # 用户指定了主机地址：定向探测该 IP 上是否存在该房间号（真实验证，不盲信）
        host_address = self.host_edit.text().strip()
        if host_address:
            # 显示状态：正在搜索房间
            self._show_status(I18n.tr('searching_room'), color='#339af0')

            # 创建房间发现服务，定向探测
            self.discovery = RoomDiscovery(self)
            self.discovery.room_found.connect(self.on_room_found)
            self.discovery.discovery_finished.connect(self.on_discovery_finished)
            self.discovery.error_occurred.connect(self.on_discovery_error)

            # 保存房间号与目标 IP
            self._pending_room_code = room_code
            self._manual_ip = host_address

            # 定向探测该 IP 上是否存在该房间号（1.5秒超时）
            self.discovery.discover_room_at(host_address, room_code, timeout=1.5)
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
        
        # 开始发现（1.5秒超时）
        self.discovery.discover_room(room_code, timeout=1.5)
    
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
        
        # 版本一致：显示已找到房间，探测确认存在 → 连接按钮才可点击
        self._show_status(I18n.tr('room_found', ip=host_ip), color='#51cf66')
        self._room_checked = True
        self._is_checking = False
        self.connect_btn.setEnabled(True)
    
    def on_discovery_finished(self, rooms: list):
        """发现完成"""
        self._is_checking = False
        if not rooms:
            # 没有找到房间
            self._show_status(I18n.tr('room_not_found'), color='#ff6b6b')
            self._room_checked = False
            self.connect_btn.setEnabled(False)
    
    def on_discovery_error(self, error: str):
        """发现错误"""
        self._show_status(error, color='#ff6b6b')
        self._room_checked = False
        self._is_checking = False
    
    def on_connect(self):
        """连接房间：先以空密码尝试，无密码房间直接进入；被拒（需要密码）则弹出密码对话框"""
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

        # 如果用户指定了主机地址，使用它
        host_address = self.host_edit.text().strip()
        if host_address:
            self.host_address = host_address
            self.discovered_host = host_address

        # 先以空密码预验证：无密码的房间直接进入；有密码的房间会被拒而进入密码对话框
        status, message = self._attempt_connect("")

        if status == 'success':
            # 无密码房间：无需密码对话框，直接进入同步界面
            self.password = ""
            self.accept()
        elif status == 'failed':
            # 空密码被拒：该房间需要密码，弹出密码对话框由它负责输入与验证
            self._open_password_dialog()
        else:
            # 超时 / 连接错误：已在 _attempt_connect 中显示错误，保持对话框打开
            self._show_status(message or I18n.tr('connection_failed'), color='#ff6b6b')

    def _attempt_connect(self, password: str):
        """以指定密码预验证连接，返回 (status, message)；不负责 accept"""
        self._is_verifying = True
        self.connect_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self._show_status(I18n.tr('verifying'), color='#339af0')

        host = self.host_address or "127.0.0.1"
        port = self.host_port or Config.DEFAULT_PORT

        # 创建临时 Client 进行验证
        client = SyncClient(self.room_code, password)
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

        # 尝试连接（放到后台线程，避免 socket.connect 同步阻塞冻结界面）
        # 成功/失败均通过上述信号驱动 QEventLoop 退出，不在此同步等待返回值
        def _connect_task():
            # 本函数运行在后台线程：只写共享 result，绝不操作主线程 Qt 对象
            # （timeout_timer / loop）。事件循环退出由 on_* 信号或超时兜底完成。
            try:
                ok = client.connect_to_server(host, port)
                if not ok and result['status'] is None:
                    # 同步建立连接失败：connect_to_server 内部已 emit error_occurred，
                    # 由 on_error 在主线程退出循环；此处仅记录结果
                    result['status'] = 'error'
                    result['message'] = I18n.tr('connection_failed')
            except Exception:
                if result['status'] is None:
                    result['status'] = 'error'
                    result['message'] = I18n.tr('connection_failed')

        threading.Thread(target=_connect_task, daemon=True).start()

        # 等待验证结果（10 秒超时）
        timeout_timer.start(10000)
        loop.exec()

        # 成功：保留 client 实例，断开临时信号连接供 SyncWindow 复用
        if result['status'] == 'success':
            try:
                client.connected.disconnect(on_connected)
                client.auth_failed.disconnect(on_auth_failed)
                client.error_occurred.disconnect(on_error)
            except Exception:
                pass
            self._is_verifying = False
            self._show_status(I18n.tr('room_found', ip=host), color='#51cf66')
            # 记录历史：仅当用户手动指定了 IP（host_edit 有文本）时写入；扫描发现的房间不入历史
            if self.host_edit.text().strip():
                UserConfig.add_room_history(self.room_code, host)
            return 'success', ''

        # 失败 / 超时 / 错误：断开 client，恢复按钮，交还原对话框判断后续走向
        self._is_verifying = False
        self.connect_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self._verified_client = None

        try:
            client.disconnect()
        except Exception:
            pass

        if result['status'] == 'failed':
            return 'failed', (result['message'] or I18n.tr('auth_failed'))
        if result['status'] == 'timeout':
            return 'timeout', I18n.tr('connection_failed')
        return 'error', (result['message'] or I18n.tr('connection_failed'))

    def _open_password_dialog(self):
        """弹出独立密码对话框；验证通过后回填 password/client 并进入同步界面"""
        from ui.password_dialog import PasswordDialog

        host = self.host_address or "127.0.0.1"
        port = self.host_port or Config.DEFAULT_PORT
        dlg = PasswordDialog(self.room_code, host, port, self)
        if dlg.exec():
            self.password = dlg.get_password()
            self._verified_client = dlg.get_verified_client()
            self.accept()

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

    # ========== 发现房间板块相关方法 ==========

    def _start_scan_all_rooms(self):
        """开始扫描局域网内所有房间"""
        if self._is_scanning:
            return

        self._is_scanning = True
        self.scan_btn.setEnabled(False)
        self._clear_scan_rows()
        self._discovered_rooms_list.clear()
        self.scan_status_label.setText(I18n.tr('scanning_rooms'))
        self.scan_status_label.setStyleSheet("color: #339af0; font-size: 12px;")

        # 显示加载动画（重置进度，让光束从左侧重新开始）
        if self._loader:
            self._loader.reset_animation()
            self._loader.show()

        # 创建扫描发现服务
        self._scan_discovery = RoomDiscovery(self)
        self._scan_discovery.room_found.connect(self._on_scan_room_found)
        self._scan_discovery.discovery_finished.connect(self._on_scan_finished)
        self._scan_discovery.error_occurred.connect(self._on_scan_error)

        # 开始扫描所有房间（超时2秒）
        self._scan_discovery.discover_all_rooms(timeout=2)

    def _on_scan_room_found(self, host_ip: str, room_code: str, port: int, version: str = ""):
        """扫描发现单个房间"""
        # 过滤 127.0.0.1 地址（只保留真实 IP）
        if host_ip == '127.0.0.1':
            return

        # 检查是否已存在（避免重复）
        for room in self._discovered_rooms_list:
            if room['ip'] == host_ip:
                return

        # 添加到列表
        room_info = {
            'ip': host_ip,
            'room_code': room_code,
            'port': port,
            'version': version
        }
        self._discovered_rooms_list.append(room_info)

        # 添加到列表控件：使用统一 RoomRowWidget（固定主题蓝指示条、无删除按钮）
        widget = RoomRowWidget(room_code, host_ip, indicator_color=RoomRowWidget.COLOR_SCANNED, show_delete=False)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, room_info)
        # 显式固定高度：不用 sizeHint（它只是文字撑出的窄高度），保证单元格/悬浮/点击范围与行视觉一致
        item.setSizeHint(QSize(0, RoomRowWidget.ROW_HEIGHT))
        self.rooms_list_widget.addItem(item)
        self.rooms_list_widget.setItemWidget(item, widget)
        # 点击该扫描行填充输入框（与历史行一致，保证 setItemWidget 下点击可靠）
        widget.clicked.connect(lambda ri=room_info: self._fill_from_room(ri))

        # 更新状态
        count = len(self._discovered_rooms_list)
        self.scan_status_label.setText(I18n.tr('rooms_found_count', count=count))
        self.scan_status_label.setStyleSheet("color: #51cf66; font-size: 12px;")

    def _on_scan_finished(self, rooms: list):
        """扫描完成"""
        self._is_scanning = False
        self.scan_btn.setEnabled(True)

        # 隐藏加载动画
        if self._loader:
            self._loader.hide()

        if not self._discovered_rooms_list:
            self.scan_status_label.setText(I18n.tr('no_rooms_found'))
            self.scan_status_label.setStyleSheet("color: #868e96; font-size: 12px;")

        # 清理扫描服务
        if self._scan_discovery:
            self._scan_discovery.stop_discovery()
            self._scan_discovery = None

        # 扫描完成后重新探测历史行可达性（刷新绿/黄指示条）
        self._probe_history()

    def _on_scan_error(self, error: str):
        """扫描错误"""
        self._is_scanning = False
        self.scan_btn.setEnabled(True)

        # 隐藏加载动画
        if self._loader:
            self._loader.hide()

        self.scan_status_label.setText(error)
        self.scan_status_label.setStyleSheet("color: #ff6b6b; font-size: 12px;")

        # 清理扫描服务
        if self._scan_discovery:
            self._scan_discovery.stop_discovery()
            self._scan_discovery = None

    def _on_room_item_clicked(self, item: QListWidgetItem):
        """点击发现的房间项，自动填充房间号（自定义渲染行由 widget.clicked 处理）"""
        # 自定义渲染行（RoomRowWidget）已通过 clicked 信号填充，避免双重触发
        if self.rooms_list_widget.itemWidget(item) is not None:
            return
        room_info = item.data(Qt.UserRole)
        if room_info:
            self._fill_from_room(room_info)

    def _fill_from_room(self, room_info: dict):
        """根据扫描到的房间信息填充输入框并启用连接按钮"""
        current_code = self.room_code_input.get_room_code()
        if room_info['room_code'] == current_code:
            return  # 匹配项不可点击，直接返回

        # 填充房间号到输入框（不触发检测，避免重复刷新）
        self.room_code_input.set_room_code(room_info['room_code'], trigger_check=False)
        # 启用连接按钮
        self.connect_btn.setEnabled(True)
        # 记录主机信息（连接时使用）
        self.discovered_host = room_info['ip']
        self.host_port = room_info['port']
        self.host_address = room_info['ip']  # 同时设置 host_address，确保连接时使用正确地址
        # 清空手动输入的主机地址（使用扫描发现的）
        self.host_edit.clear()
        # 更新状态
        self._show_status(I18n.tr('room_found', ip=room_info['ip']), color='#51cf66')
        self._room_checked = True

    def _update_matching_room_style(self):
        """更新列表项样式：匹配当前输入的房间号时灰色不可点击"""
        current_code = self.room_code_input.get_room_code()

        # 遍历所有列表项（历史行无 data(room_info)，跳过）
        for i in range(self.rooms_list_widget.count()):
            item = self.rooms_list_widget.item(i)
            room_info = item.data(Qt.UserRole)
            widget = self.rooms_list_widget.itemWidget(item)

            if room_info and widget is not None:
                # 扫描行：使用 RoomRowWidget 置灰/恢复
                widget.set_matched(room_info['room_code'] == current_code)
            elif room_info:
                # 默认渲染的扫描行（兼容旧路径）
                if room_info['room_code'] == current_code:
                    item.setForeground(QColor('#868e96'))
                    flags = item.flags()
                    flags &= ~Qt.ItemIsSelectable
                    flags &= ~Qt.ItemIsEnabled
                    item.setFlags(flags)
                else:
                    palette = QApplication.palette()
                    item.setForeground(palette.color(QPalette.Text))
                    flags = item.flags()
                    flags |= Qt.ItemIsSelectable
                    flags |= Qt.ItemIsEnabled
                    item.setFlags(flags)

    # ========== 最近连接历史 ==========

    def _clear_scan_rows(self):
        """清除所有扫描发现的房间行，保留历史行（历史行无 data(room_info)）"""
        for i in range(self.rooms_list_widget.count() - 1, -1, -1):
            item = self.rooms_list_widget.item(i)
            if item.data(Qt.UserRole) is not None:
                self.rooms_list_widget.takeItem(i)

    def _load_history(self):
        """加载历史房间记录到列表顶部，并异步探测可达性（默认黄，可达变绿）"""
        self._clear_history_rows()
        history = UserConfig.get_room_history()
        if not history:
            return

        for entry in history:
            code = entry.get("room_code", "")
            ip = entry.get("ip", "")
            if not code or not ip:
                continue
            row = self._add_history_row(code, ip)
            if row is None:
                continue
            self._history_items.append(row)

        # 启动可达性探测
        self._probe_history()

    def _probe_history(self):
        """对全部历史行重新探测可达性：默认黄，定向探测到可达变绿（周期刷新复用）"""
        # 若已有探测在运行，跳过本次，避免 1s 定时器叠加并发探测
        if self._room_probe is not None:
            return
        # 确保有历史行
        if not self._history_items:
            return
        # 构建探测目标（仅针对仍存在于列表中的历史行）
        probes = [
            {"ip": row.get('ip'), "room_code": row.get('room_code'), "port": self.host_port}
            for row in self._history_items
            if row.get('ip') and row.get('room_code')
        ]
        if not probes:
            return
        # 启动新的可达性探测（定向探测，若不在运行才创建）
        self._room_probe = RoomProbe(self)
        self._room_probe.probed.connect(self._on_history_probed)
        self._room_probe.finished.connect(self._on_probe_finished)
        self._room_probe.start_probing(probes)

    def _on_probe_finished(self):
        """历史探测全部完成：释放探测服务，允许下一轮周期刷新"""
        self._room_probe = None

    def _clear_history_rows(self):
        """移除现有历史行控件并清空缓存"""
        # 逐个移除（从列表反向，避免索引偏移）
        for row in list(self._history_items):
            item = row.get('item')
            if item is not None and self.rooms_list_widget.row(item) >= 0:
                self.rooms_list_widget.takeItem(self.rooms_list_widget.row(item))
        self._history_items.clear()
        # 若存在探测服务，停止并释放
        if self._room_probe:
            self._room_probe.stop_probing()
            self._room_probe = None

    def _add_history_row(self, room_code: str, ip: str):
        """向列表顶部插入一条历史行，返回记录字典或 None"""
        widget = RoomRowWidget(room_code, ip)
        widget.clicked.connect(lambda c=room_code, d=ip: self._on_history_clicked(c, d))
        widget.remove_requested.connect(lambda c=room_code, d=ip: self._on_history_remove(c, d))

        item = QListWidgetItem()
        # 显式固定高度：保证单元格/悬浮/点击范围与行视觉一致（不依赖文字 sizeHint）
        item.setSizeHint(QSize(0, RoomRowWidget.ROW_HEIGHT))
        # 插入到顶部（历史上方为历史，下方为扫描结果）
        self.rooms_list_widget.insertItem(0, item)
        self.rooms_list_widget.setItemWidget(item, widget)
        return {'item': item, 'widget': widget, 'room_code': room_code, 'ip': ip}

    def _on_history_probed(self, ip: str, room_code: str, reachable: bool):
        """历史行探测完成：更新对应指示条绿/黄"""
        for row in self._history_items:
            if row.get('room_code') == room_code and row.get('ip') == ip:
                row['widget'].set_status(reachable)
                break

    def _on_history_clicked(self, room_code: str, ip: str):
        """点击历史行：填充房间号+IP，触发原有房间号自动检测，并从列表移除该行（不从 config 移除）"""
        # 填充房间号（不触发检测，避免与下方检测重复）
        self.room_code_input.set_room_code(room_code, trigger_check=False)
        # 填充主机地址
        self.host_edit.setText(ip)
        self.host_address = ip
        self.discovered_host = ip
        self.host_port = Config.DEFAULT_PORT
        # 历史房间号完整，直接启用连接按钮
        self.connect_btn.setEnabled(True)
        # 从历史列表移除该行（仅显示，保留 config 历史记录）
        for row in list(self._history_items):
            if row.get('room_code') == room_code and row.get('ip') == ip:
                item = row.get('item')
                if item is not None and self.rooms_list_widget.row(item) >= 0:
                    self.rooms_list_widget.takeItem(self.rooms_list_widget.row(item))
                if row in self._history_items:
                    self._history_items.remove(row)
                break
        # 触发原有房间号逻辑自动检测一次（双重保险）
        QTimer.singleShot(0, self._check_room_exists)

    def _on_history_remove(self, room_code: str, ip: str):
        """删除一条历史记录"""
        UserConfig.remove_room_history(room_code, ip)
        # 同步移除界面行
        for row in list(self._history_items):
            if row.get('room_code') == room_code and row.get('ip') == ip:
                item = row.get('item')
                if item is not None and self.rooms_list_widget.row(item) >= 0:
                    self.rooms_list_widget.takeItem(self.rooms_list_widget.row(item))
                if row in self._history_items:
                    self._history_items.remove(row)
                break
