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
from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QByteArray, QEventLoop, QSize, QPoint
from PySide6.QtGui import QFont, QValidator, QKeyEvent, QShowEvent, QColor, QPalette, QPainter, QPen

from i18n import I18n
from config import Config, UserConfig
from network.discovery import RoomDiscovery
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


class LegendHelpIcon(QWidget):
    """圆形问号图标；悬浮时弹出图例说明浮层。

    代替原先直接排布在按钮行内的整块图例——整块图例会撑高按钮行，
    导致 150% 缩放下取消按钮顶部 hover 事件丢失。
    """

    hover_in = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, event):
        self.hover_in.emit()
        super().enterEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        accent = QColor("#339af0")
        p.setPen(QPen(accent, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(self.rect().adjusted(2, 2, -2, -2))
        f = self.font()
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(accent, 1.0))
        p.drawText(self.rect(), Qt.AlignCenter, "?")
        p.end()


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
        self._check_seq = 0  # 探测代号（generation）：新探测自增，旧探测回调凭此失效
        self._is_verifying = False  # 是否正在验证密码
        self._verify_loop = None  # 验证中的事件循环（用于输入变化时中途取消验证）
        self._verify_timer = None  # 验证中的超时定时器（取消验证时一并停止）
        # IP 输入防抖定时器：输入停顿一段时间后才定向探测，避免逐字触发探测导致实例/线程堆积
        self._ip_probe_timer = QTimer(self)
        self._ip_probe_timer.setSingleShot(True)
        self._ip_probe_timer.setInterval(900)
        self._ip_probe_timer.timeout.connect(self._check_room_exists)
        self._verified_client = None  # 预验证成功的 Client 实例（传递给 SyncWindow 复用）
        self._is_scanning = False  # 是否正在扫描所有房间
        self._scan_discovery = None  # 扫描发现服务
        self._history_probes = []  # 扫描期间对历史行的定向探测服务（每条历史一个 RoomDiscovery）
        self._discovered_rooms_list = []  # 扫描发现的房间列表
        self._first_show = True  # 是否首次显示
        self._loader = None  # 加载动画组件
        self._history_items = []  # 历史行控件列表
        self._history_status = {}  # 历史行在线状态 {(ip, room_code): online}，扫描结果即时写入
        self._manual_ip = ""  # 手动指定的主机地址（定向探测目标）
        self.init_ui()
        # 开启对话框级鼠标追踪：用于图例浮层在空白区域的移出隐藏
        self.setMouseTracking(True)

    def _set_history_online(self, ip: str, room_code: str, online: bool):
        """记录历史行在线状态并更新对应指示条（由广播扫描结果即时点亮，无常驻探测）"""
        self._history_status[(ip, room_code)] = online
        for row in self._history_items:
            if row.get('room_code') == room_code and row.get('ip') == ip:
                row['widget'].set_status(online)
                break

    def _cleanup_background(self):
        """对话框关闭时清理后台活动：停止所有探测/扫描服务"""
        # 停止房间号/IP 输入的防抖探测定时器
        if hasattr(self, '_ip_probe_timer'):
            self._ip_probe_timer.stop()
        # 停止房间发现服务（手动/扫描共用 self.discovery 会在各自结束点清理，此处兜底）
        for probe in (getattr(self, 'discovery', None), getattr(self, '_scan_discovery', None)):
            if probe is not None:
                try:
                    probe.stop_discovery()
                except Exception:
                    pass
        self._stop_history_probes()

    def accept(self):
        """连接成功：关闭前清理后台活动（扫描/房间探测）"""
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
        # 手动修改 IP：使此前“已探测/已锚定”结论失效，需按新 IP 重新确认才可连接
        self.host_edit.textChanged.connect(self._on_host_edit_text_changed)
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

        # 左下角图例入口：圆形问号图标，悬浮弹出图例说明浮层。
        # （整块图例会撑高按钮行，导致 150% 缩放下取消按钮顶部 hover 丢失，故改为图标+浮层）
        self.legend_help_btn = LegendHelpIcon()
        self.legend_help_btn.hover_in.connect(self._show_legend_popup)
        self.legend_popup = self._build_legend_popup()
        button_layout.addWidget(self.legend_help_btn)

        button_layout.addStretch()  # 弹性空间，让按钮靠右
        button_layout.addWidget(self.connect_btn)
        button_layout.addWidget(self.cancel_btn)

        layout.addLayout(button_layout)

    def _build_legend_popup(self) -> "QFrame":
        """构建图例说明浮层（悬浮问号图标弹出），明暗主题自适应配色。"""
        popup = QFrame(self)
        popup.setObjectName("LegendPopup")
        popup.setAttribute(Qt.WA_TransparentForMouseEvents, True)  # 不拦截鼠标，避免浮层抢占悬浮
        base = QApplication.palette().color(QPalette.Window)
        luminance = base.red() * 0.299 + base.green() * 0.587 + base.blue() * 0.114
        if luminance < 128:
            bg, border = "rgba(50, 50, 56, 235)", "rgba(255, 255, 255, 60)"
        else:
            bg, border = "rgba(255, 255, 255, 240)", "rgba(0, 0, 0, 60)"
        popup.setStyleSheet(
            f"QFrame#LegendPopup {{ background-color: {bg}; border: 1px solid {border}; border-radius: 8px; }}"
        )
        lay = QVBoxLayout(popup)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(5)
        for _color, _text in (
            (RoomRowWidget.COLOR_SCANNED, I18n.tr('legend_scanned')),
            (RoomRowWidget.COLOR_ONLINE, I18n.tr('legend_history_online')),
            (RoomRowWidget.COLOR_OFFLINE, I18n.tr('legend_history_offline')),
        ):
            row = QHBoxLayout()
            row.setSpacing(6)
            _dot = QFrame()
            _dot.setFixedSize(8, 8)
            _dot.setStyleSheet(f"background: {_color}; border-radius: 2px;")
            row.addWidget(_dot)
            _lbl = QLabel(_text)
            _lbl.setStyleSheet("color: palette(text); font-size: 11px;")
            row.addWidget(_lbl)
            row.addStretch()
            lay.addLayout(row)
        popup.adjustSize()
        popup.hide()
        return popup

    def _show_legend_popup(self):
        """在问号图标上方弹出图例浮层（与图标轻微交叠，保证悬浮转移连续）。"""
        popup = self.legend_popup
        icon = self.legend_help_btn
        popup.adjustSize()
        plt = icon.mapTo(self, QPoint(0, 0))
        x = plt.x()
        y = plt.y() - popup.height() + 4  # 与图标顶部交叠 4px，避免移动时悬停跳变
        # 保持浮层在对话框内
        x = max(3, min(x, self.width() - popup.width() - 3))
        y = max(3, y)
        popup.move(x, y)
        popup.show()
        popup.raise_()

    def mouseMoveEvent(self, event):
        # 光标离开图标与浮层区域时隐藏浮层
        if getattr(self, 'legend_popup', None) is not None and self.legend_popup.isVisible():
            gp = event.globalPosition().toPoint()
            _ir = self.legend_help_btn.rect()
            icon_rect = _ir.translated(self.legend_help_btn.mapToGlobal(_ir.topLeft()))
            _pr = self.legend_popup.rect()
            pop_rect = _pr.translated(self.legend_popup.mapToGlobal(_pr.topLeft()))
            if not icon_rect.contains(gp) and not pop_rect.contains(gp):
                self.legend_popup.hide()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if getattr(self, 'legend_popup', None) is not None:
            self.legend_popup.hide()
        super().leaveEvent(event)

    def _on_code_completed(self):
        """输入完成时自动检测房间（探测确认存在后才可点连接）"""
        # 用户换了个新房间号：取消仍在进行中的连接验证，让新探测接管
        self._cancel_verifying()
        # 若旧探测还未结束，先取消旧的再重新调度，否则新房间号会被防重入吞掉
        if self._is_checking:
            self._cancel_checking()

        # 探测期间保持连接按钮禁用，可否点击交由探测结果判定（存在才可点）
        self.connect_btn.setEnabled(False)

        # 添加一个小延迟，让用户看到输入完成
        QTimer.singleShot(300, self._check_room_exists)

    def _cancel_checking(self):
        """取消正在进行的房间探测，避免旧探测结果污染新输入"""
        self._check_seq += 1  # 使本代旧探测的回调全部失效
        d = getattr(self, 'discovery', None)
        if d is not None:
            try:
                d.stop_discovery()
            except Exception:
                pass
            self.discovery = None
        self._is_checking = False
        self._pending_room_code = None
    
    def _cancel_verifying(self):
        """取消正在进行的连接验证（用户在中途修改 IP/房间号时调用，解堵『验证中』状态）

        通过停止超时定时器并退出阻塞事件循环，让 _attempt_connect 即刻返回；
        _is_verifying 复位与按钮恢复由 _attempt_connect 的退出清理完成。
        """
        if not self._is_verifying:
            return
        timer = self._verify_timer
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        loop = self._verify_loop
        if loop is not None:
            loop.quit()

    def _on_host_edit_text_changed(self, text: str = ""):
        """手动修改主机 IP：使此前针对旧 IP 的『已探测/已锚定』结论失效

        取消在途的验证与探测，并（房间号已填满时）针对新 IP 自动重新探测确认，
        存在才启用连接按钮——避免用旧锚点去连错误 IP 而长时间卡在验证中。
        """
        self._cancel_verifying()
        self._cancel_checking()
        self._room_checked = False
        self.connect_btn.setEnabled(False)
        # 房间号已填满且 IP 形如地址（含小数点）时：启动/重置防抖定时器，输入停顿约 900ms 后才定向探测
        if self.room_code_input.is_complete() and '.' in text:
            self._ip_probe_timer.start()
        else:
            # 房间号不完整或 IP 尚不像地址：停止防抖探测，避免中途触发
            self._ip_probe_timer.stop()

    def _on_room_code_input_changed(self):
        """房间号输入变化时更新连接按钮状态，并清除"已找到"等历史状态"""
        # 用户正在改房间号：若此刻有进行中的连接验证，取消它解堵 UI
        self._cancel_verifying()
        if not self.room_code_input.is_complete():
            self.connect_btn.setEnabled(False)
            # 房间号不完整：立即取消正在进行的探测（stop + 使旧回调失效），
            # 后台扫描逻辑到此停止，等待用户输满新房间号
            self._cancel_checking()
            self._ip_probe_timer.stop()
            self._room_checked = False
            # 界面回到"等待输入"默认状态
            self._show_status(I18n.tr('ready_waiting'), color='#868e96')

    def _check_room_exists(self):
        """检测房间是否存在"""
        # 若已有探测在途（如 300ms 调度叠加/重复触发），先取消旧的，保证只保留最新一次探测
        if self._is_checking:
            self._cancel_checking()
        # 本代探测开始：自增代号，旧探测（如已取消/被替换的）回调凭此失效
        self._check_seq += 1
        seq = self._check_seq
        self._is_checking = True
        room_code = self.room_code_input.get_room_code()
        
        # 验证房间号：调度在途期间用户删位到不完整时静默复位（不弹窗，弹窗由 on_connect 负责）
        if not self.room_code_input.is_complete():
            self._is_checking = False
            self._show_status(I18n.tr('ready_waiting'), color='#868e96')
            return
        
        # 用户指定了主机地址：定向探测该 IP 上是否存在该房间号（真实验证，不盲信）
        host_address = self.host_edit.text().strip()
        if host_address:
            # 显示状态：正在搜索房间
            self._show_status(I18n.tr('searching_room'), color='#339af0')

            # 创建房间发现服务，定向探测
            self.discovery = RoomDiscovery(self)
            self.discovery.room_found.connect(lambda ip, code, port, ver="", seq=seq: self.on_room_found(ip, code, port, ver, seq))
            self.discovery.discovery_finished.connect(lambda rooms, seq=seq: self.on_discovery_finished(rooms, seq))
            self.discovery.error_occurred.connect(lambda err, seq=seq: self.on_discovery_error(err, seq))

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
        self.discovery.room_found.connect(lambda ip, code, port, ver="", seq=seq: self.on_room_found(ip, code, port, ver, seq))
        self.discovery.discovery_finished.connect(lambda rooms, seq=seq: self.on_discovery_finished(rooms, seq))
        self.discovery.error_occurred.connect(lambda err, seq=seq: self.on_discovery_error(err, seq))
        
        # 保存房间号
        self._pending_room_code = room_code
        
        # 开始发现（1.5秒超时）
        self.discovery.discover_room(room_code, timeout=1.5)
    
    def _show_status(self, text: str, color: str = '#868e96'):
        """显示状态标签"""
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")
    
    def on_room_found(self, host_ip: str, room_code: str, port: int, version: str = "", seq: int = None):
        """发现房间
        Args:
            host_ip: 主机IP
            room_code: 房间号
            port: 端口
            version: 主机版本号
            seq: 触发本回调的探测代号（None 表示未代际校验的旧调用点，仍放行）
        """
        # 代际守卫：回调来自已被取消/替换的旧探测时，直接丢弃
        if seq is not None and seq != self._check_seq:
            return
        # 房间号守卫：回调房间号与当前输入不一致（旧房间号的残留结果）时丢弃
        if self._pending_room_code is not None and room_code != self._pending_room_code:
            return
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
    
    def on_discovery_finished(self, rooms: list, seq: int = None):
        """发现完成"""
        # 代际守卫：丢弃来自已取消/替换旧探测的结束回调
        if seq is not None and seq != self._check_seq:
            return
        self._is_checking = False
        if not rooms:
            # 没有找到房间
            self._show_status(I18n.tr('room_not_found'), color='#ff6b6b')
            self._room_checked = False
            self.connect_btn.setEnabled(False)
    
    def on_discovery_error(self, error: str, seq: int = None):
        """发现错误"""
        # 代际守卫：丢弃来自已取消/替换旧探测的错误回调
        if seq is not None and seq != self._check_seq:
            return
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

        # 锚未建立（含修改 IP 后已失效）：先探测确认房间存在于当前输入再连接，
        # 避免用旧锚点直接连错 IP 而卡在验证中
        if not self._room_checked:
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
        elif status == 'cancel':
            # 用户中途修改输入取消了验证：界面已复位，等待新输入，无需提示
            pass
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
        # 记录到实例，供输入变化时 _cancel_verifying 中途取消本验证
        self._verify_loop = loop
        self._verify_timer = timeout_timer

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

        try:
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
                # 记录历史：host_edit 有 IP（手动指定 / 扫描 / 历史填充都会填充）时写入；
                # 无 IP 的纯广播连接不入历史
                if self.host_edit.text().strip():
                    UserConfig.add_room_history(self.room_code, host)
                return 'success', ''

            # 失败 / 超时 / 错误 / 取消：断开 client，恢复按钮，交还原对话框判断后续走向
            self._is_verifying = False
            self.connect_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)
            self._verified_client = None

            try:
                client.disconnect()
            except Exception:
                pass

            if result['status'] is None:
                # 用户中途修改输入主动取消了验证（非真实失败），界面已复位，不在此提示
                return 'cancel', ''
            if result['status'] == 'failed':
                return 'failed', (result['message'] or I18n.tr('auth_failed'))
            if result['status'] == 'timeout':
                return 'timeout', I18n.tr('connection_failed')
            return 'error', (result['message'] or I18n.tr('connection_failed'))
        finally:
            # 无论结果如何清空引用，避免下轮验证残留旧 loop/timer
            self._verify_loop = None
            self._verify_timer = None

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
        # 每次扫描为新评估：先把历史行全部重置为黄，本次未响应的保持黄，响应的由 _on_scan_room_found 点亮绿
        for row in self._history_items:
            self._set_history_online(row['ip'], row['room_code'], False)
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

        # 扫描窗口（秒）
        self._scan_timeout = 2

        # 开始广播扫描所有房间
        self._scan_discovery.discover_all_rooms(timeout=self._scan_timeout)
        # 同时对每条历史行按其 IP 定向探测：广播扫不到跨网段主机，定向单播可直达，
        # 响应即点亮绿，超时无响应保持黄（黄=该 IP 定向探测未见响应，更准确）
        self._start_history_directed_probes(self._scan_timeout)

    def _on_scan_room_found(self, host_ip: str, room_code: str, port: int, version: str = ""):
        """扫描发现单个房间"""
        # 过滤 127.0.0.1 地址（只保留真实 IP）
        if host_ip == '127.0.0.1':
            return

        # 检查是否已存在（避免重复）
        for room in self._discovered_rooms_list:
            if room['ip'] == host_ip:
                return

        # 去重：该房间已存在于历史（同房间号+IP）时，隐藏扫描项、保留历史条目
        # （历史条目带在线绿/离线黄状态与删除按钮，信息更完整）
        # 同时用扫描结果即时点亮对应历史行（无常驻探测，扫描到即视为在线）
        for row in self._history_items:
            if row.get('room_code') == room_code and row.get('ip') == host_ip:
                self._set_history_online(host_ip, room_code, True)
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
        self._stop_history_probes()

    def _start_history_directed_probes(self, timeout: int):
        """扫描期间按历史行 IP 定向探测（每条历史起一个定向发现，响应到达即点亮绿）"""
        self._stop_history_probes()
        for row in self._history_items:
            ip = row.get('ip')
            code = row.get('room_code')
            if not ip or not code:
                continue
            disc = RoomDiscovery(self)
            disc.room_found.connect(
                lambda r_ip, r_code, port, ver, c=code, i=ip: self._set_history_online(i, c, True)
            )
            if disc.discover_room_at(ip, code, timeout=timeout):
                self._history_probes.append(disc)

    def _stop_history_probes(self):
        """停止并释放扫描期间的历史行定向探测服务"""
        for disc in self._history_probes:
            try:
                disc.stop_discovery()
            except Exception:
                pass
        self._history_probes.clear()

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
        self._stop_history_probes()

    def _on_room_item_clicked(self, item: QListWidgetItem):
        """点击发现的房间项，自动填充房间号（自定义渲染行由 widget.clicked 处理）"""
        # 自定义渲染行（RoomRowWidget）已通过 clicked 信号填充，避免双重触发
        if self.rooms_list_widget.itemWidget(item) is not None:
            return
        room_info = item.data(Qt.UserRole)
        if room_info:
            self._fill_from_room(room_info)

    def _fill_from_room(self, room_info: dict):
        """根据扫描到的房间信息填充输入框，随后定向探测确认存在才启用连接按钮"""
        current_code = self.room_code_input.get_room_code()
        if room_info['room_code'] == current_code:
            return  # 匹配项不可点击，直接返回

        # 填充房间号到输入框（不触发检测，避免重复刷新）
        self.room_code_input.set_room_code(room_info['room_code'], trigger_check=False)
        # 记录主机信息（连接时使用）
        self.discovered_host = room_info['ip']
        self.host_port = room_info['port']
        self.host_address = room_info['ip']  # 同时设置 host_address，确保连接时使用正确地址
        # 将扫描到的 IP 填入地址框，使后续探测走定向探测（确认该 IP 上房间仍在线）
        self.host_edit.setText(room_info['ip'])
        # 探测确认存在前保持连接按钮禁用（setText 触发 textChanged，由 _on_host_edit_text_changed 调度定向探测）
        self.connect_btn.setEnabled(False)
        self._room_checked = False

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
        """加载历史房间记录到列表顶部（默认黄，扫描到后由 _set_history_online 点亮绿）"""
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

    def _clear_history_rows(self):
        """移除现有历史行控件并清空缓存"""
        # 逐个移除（从列表反向，避免索引偏移）
        for row in list(self._history_items):
            item = row.get('item')
            if item is not None and self.rooms_list_widget.row(item) >= 0:
                self.rooms_list_widget.takeItem(self.rooms_list_widget.row(item))
        self._history_items.clear()
        self._history_status.clear()

    def _add_history_row(self, room_code: str, ip: str):
        """向列表顶部插入一条历史行，返回记录字典或 None"""
        widget = RoomRowWidget(room_code, ip)
        widget.clicked.connect(lambda c=room_code, d=ip: self._on_history_clicked(c, d))
        widget.remove_requested.connect(lambda c=room_code, d=ip: self._on_history_remove(c, d))
        # 复用已记录状态：重建该行时按字典恢复绿/黄
        if self._history_status.get((ip, room_code)):
            widget.set_status(True)

        item = QListWidgetItem()
        # 显式固定高度：保证单元格/悬浮/点击范围与行视觉一致（不依赖文字 sizeHint）
        item.setSizeHint(QSize(0, RoomRowWidget.ROW_HEIGHT))
        # 插入到顶部（历史上方为历史，下方为扫描结果）
        self.rooms_list_widget.insertItem(0, item)
        self.rooms_list_widget.setItemWidget(item, widget)
        return {'item': item, 'widget': widget, 'room_code': room_code, 'ip': ip}

    def _on_history_clicked(self, room_code: str, ip: str):
        """点击历史行：填充房间号+IP，触发原有房间号自动检测，并从列表移除该行（不从 config 移除）"""
        # 填充房间号（不触发检测，避免与下方检测重复）
        self.room_code_input.set_room_code(room_code, trigger_check=False)
        # 填充主机地址
        self.host_edit.setText(ip)
        self.host_address = ip
        self.discovered_host = ip
        self.host_port = Config.DEFAULT_PORT
        # 探测确认存在前保持连接按钮禁用（setText 触发 textChanged，由 _on_host_edit_text_changed 调度定向探测）
        self.connect_btn.setEnabled(False)
        self._room_checked = False
        # 从历史列表移除该行（仅显示，保留 config 历史记录）
        for row in list(self._history_items):
            if row.get('room_code') == room_code and row.get('ip') == ip:
                item = row.get('item')
                if item is not None and self.rooms_list_widget.row(item) >= 0:
                    self.rooms_list_widget.takeItem(self.rooms_list_widget.row(item))
                if row in self._history_items:
                    self._history_items.remove(row)
                break

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
