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
from network.discovery import RoomDiscovery, TcpRoomProbe
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
    """房间行控件：左侧细竖指示条 + 房间号·在线X（+ 可选右侧删除『×』按钮）

    按房间号去重分组：一行 = 一个房间，「在线X」为该房间当前发现的兼容存活成员数。
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

    def __init__(self, room_code: str, count: int = 0, indicator_color: str = "",
                 show_delete: bool = True, ip: str = "", parent=None):
        super().__init__(parent)
        self.room_code = room_code
        self.count = count
        self.ip = ip or ""
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

        # 文字：房间号 · 在线X（使用调色板默认文本色，自适应明暗）
        self._label = QLabel(self._label_text())
        self._label.setStyleSheet("color: palette(text);")
        layout.addWidget(self._label, 1, Qt.AlignVCenter)

        # 上次手动指定的主机 IP（小字灰显，仅手动输入过 IP 的历史房间有）
        self._ip_label = QLabel(self.ip)
        self._ip_label.setStyleSheet("color: #868e96; font-size: 11px;")
        self._ip_label.setToolTip(self.ip)
        self._ip_label.setVisible(bool(self.ip))
        layout.addWidget(self._ip_label, 0, Qt.AlignVCenter)

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

    def _label_text(self) -> str:
        """行文案：房间号 · 在线X"""
        return f"{self.room_code} · {I18n.tr('online_label', count=self.count)}"

    def set_count(self, count: int):
        """更新该房间在线成员数（在线X）"""
        self.count = count
        self._label.setText(self._label_text())

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
        self._verified_client = None  # 预验证成功的 Client 实例（传递给 SyncWindow 复用）
        self._is_scanning = False  # 是否正在扫描所有房间
        self._scan_discovery = None  # 扫描发现服务
        # 改版：按房间号去重分组的统一房间模型，替换旧的『每应答IP一行』列表与历史行
        # {'room_code': str, 'members': [{'ip','port','version','sync_version'}],  # 本次扫描兼容成员
        #  'count': int, 'is_history': bool, 'widget': RoomRowWidget, 'item': QListWidgetItem}
        self._room_groups = {}
        self._manual_ip = ""
        # IP 来源标记：'manual'=用户手输（连接成功才落历史线索），'auto'=列表填充/程序写入
        self._ip_source = ""
        self._setting_host = False  # 程序写入 host 栏时置位，避免被误判成手输
        self._probe = None  # TCP 探活服务（列表扫描：历史房间带线索 IP 时用）
        self._entry_probe = None  # TCP 探活服务（房间号输入路径的定向探活）
        self._first_show = True  # 是否首次显示
        self._loader = None  # 加载动画组件
        self._regress_as_host = False  # 本端即该房间主机，连接按钮已切换为"回归"
        # 手输本机 IP 的自连自检：先 TCP 探活取宿主 id 判定，随后自动续走连接流程
        self._manual_self_checked = False
        self._pending_autoconnect_seq = None  # 待自动续接连接的探测代号（None=无）
        self.init_ui()
        # 开启对话框级鼠标追踪：用于图例浮层在空白区域的移出隐藏
        self.setMouseTracking(True)

    def _cleanup_background(self):
        """对话框关闭时清理后台活动：停止所有探测/扫描服务"""
        # 停止房间发现服务（手动/扫描共用 self.discovery 会在各自结束点清理，此处兜底）
        for probe in (getattr(self, 'discovery', None), getattr(self, '_scan_discovery', None)):
            if probe is not None:
                try:
                    probe.stop_discovery()
                except Exception:
                    pass
        # 停止 TCP 探活：置停后回调不再发射，在途探测靠 1s 超时自然收敛
        for probe in (getattr(self, '_probe', None), getattr(self, '_entry_probe', None)):
            if probe is not None:
                try:
                    probe.stop()
                except Exception:
                    pass
        self._probe = None
        self._entry_probe = None

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
        ep = getattr(self, '_entry_probe', None)
        if ep is not None:
            try:
                ep.stop()
            except Exception:
                pass
            self._entry_probe = None
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
        """手动修改主机 IP（方案 1：不再自动探测，有 IP 即启用连接按钮）

        旧逻辑每敲一个字符 → 定向探测 + 同步 join 清理，而 Windows 上 socket.close()
        无法即时唤醒另一线程阻塞中的 recvfrom，导致 GUI 线程每字符被 join 拖住
        最多 ~200ms，快速输入时叠加成整体卡死。故手动 IP 不再探测，改为：地址形如
        IP 且房间号已填满时，把该 IP 作为连接锚点并置蓝连接按钮，由用户手动点击尝试
        （连不上由可取消的验证流程超时兜底）。
        """
        self._cancel_verifying()
        self._cancel_checking()
        self._reset_regress_state()
        self._room_checked = False
        self._manual_self_checked = False  # IP 变更：自连自检结论作废，需按新 IP 重判
        host = text.strip()
        # 标记 IP 来源：程序写入（列表填充/回退清空）记 'auto'，用户手输记 'manual'
        self._ip_source = "" if not host else ("auto" if self._setting_host else "manual")
        if self.room_code_input.is_complete() and ('.' in host):
            # 手动指定了主机 IP：作为连接锚点，连接按钮立即可点（不做逐键探测）
            self.host_address = host
            self.host_port = Config.DEFAULT_PORT
            self.discovered_host = host
            self.connect_btn.setEnabled(True)
            self._show_status(I18n.tr('manual_ip_ready'), color='#51cf66')
        else:
            # 无完整主机 IP：禁连，交由发现/点击列表填充后再启用，避免错误地址直连
            self.connect_btn.setEnabled(False)

    def _schedule_host_probe(self):
        """中断旧探测并针对『当前房间号 + 当前 IP』立即重新定向探测

        用于点击列表项填充（发现/历史）后显式触发——host 文本可能因切换到
        同 IP 的另一房间号而不变化，此时 textChanged 不会激活探测，必须显式调用。
        """
        self._cancel_checking()    # 中断在途的旧扫描/探测（回收线程与定时器）
        self._room_checked = False
        self.connect_btn.setEnabled(False)  # 探测确认存在前保持禁用
        # 立即定向探测（无防抖延迟）；房间号不完整时 _check_room_exists 会静默复位
        self._check_room_exists()

    def _on_room_code_input_changed(self):
        """房间号输入变化时更新连接按钮状态，并清除"已找到"等历史状态"""
        # 用户正在改房间号：若此刻有进行中的连接验证，取消它解堵 UI
        self._cancel_verifying()
        self._manual_self_checked = False  # 房间号变更：自连自检结论作废
        if not self.room_code_input.is_complete():
            self._reset_regress_state()
            self.connect_btn.setEnabled(False)
            # 房间号不完整：立即取消正在进行的探测（stop + 使旧回调失效），
            # 后台扫描逻辑到此停止，等待用户输满新房间号
            self._cancel_checking()
            self._room_checked = False
            # 界面回到"等待输入"默认状态
            self._show_status(I18n.tr('ready_waiting'), color='#868e96')

    def _check_room_exists(self):
        """检测房间是否存在"""
        # 新一轮探测：先复位上一轮的"回归为主机"结论，避免旧房间号的结论残留
        self._reset_regress_state()
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
        
        # 用户指定了主机地址：先用 TCP 定向探活（跨网段/防火墙/对端发现应答器未启动时
        # UDP 全摸不到，只有 TCP 可靠），命中即判房间在线并跳过 UDP；未命中再回退
        # UDP 定向 → 广播，兼容「房间换了设备/IP 线索过期」的情况
        host_address = self.host_edit.text().strip()
        if host_address:
            # 显示状态：正在搜索房间
            self._show_status(I18n.tr('searching_room'), color='#339af0')

            # 保存房间号与目标 IP；定向探测标识 + 广播回退只做一次（防无限叠加）
            self._pending_room_code = room_code
            self._manual_ip = host_address
            self._direct_probe = True
            self._fallback_broadcast_done = False

            # TCP 定向探活（独立服务，与列表扫描的 self._probe 互不影响）
            self._entry_probe = TcpRoomProbe(self)
            self._entry_probe.probed.connect(
                lambda rc, ip, online, hz, seq=seq: self._on_entry_probe_result(rc, ip, online, seq, hz))
            self._entry_probe.probe(host_address, room_code, Config.DEFAULT_PORT)
            return

        self._direct_probe = False
        
        # 没有指定主机地址，进行房间发现
        # 显示状态：正在搜索房间
        self._show_status(I18n.tr('searching_room'), color='#339af0')
        
        # 创建房间发现服务
        self.discovery = RoomDiscovery(self)
        self.discovery.room_found.connect(lambda ip, code, port, ver="", sv="", seq=seq: self.on_room_found(ip, code, port, ver, seq, sv))
        self.discovery.discovery_finished.connect(lambda rooms, seq=seq: self.on_discovery_finished(rooms, seq))
        self.discovery.error_occurred.connect(lambda err, seq=seq: self.on_discovery_error(err, seq))
        
        # 保存房间号
        self._pending_room_code = room_code
        
        # 开始发现（1.5秒超时）
        self.discovery.discover_room(room_code, timeout=1.5)
    
    def _on_entry_probe_result(self, room_code: str, ip: str, online: bool, seq: int, host_id: str = ""):
        """房间号输入路径的 TCP 定向探活结果：命中即判已找到并跳过 UDP 回退"""
        if seq is not None and seq != self._check_seq:
            return  # 已被取消/替换的旧一代探测回调，丢弃
        if self._pending_room_code is not None and room_code != self._pending_room_code:
            return  # 残留的旧房间号结果，丢弃
        if online:
            self.room_code = room_code
            self.host_address = ip
            self.host_port = Config.DEFAULT_PORT
            self.discovered_host = ip
            self._room_checked = True
            self._is_checking = False
            # 宿主即本端（该房间由本机创建）→ 切换为"回归为主机"，不再以连接端身份连接
            if self._offer_regress_if_host(host_id, ip):
                self._pending_autoconnect_seq = None
                return
            self._show_status(I18n.tr('room_found', ip=ip), color='#51cf66')
            self.connect_btn.setEnabled(True)
            # 手输本机 IP 的自检流程：探明宿主非本端，续走原连接流程（不再重复自检）
            if self._pending_autoconnect_seq is not None:
                if self._pending_autoconnect_seq == seq:
                    self._pending_autoconnect_seq = None
                    QTimer.singleShot(0, self.on_connect)
            return
        # 手输本机 IP 的自检流程：探活未答（可能为旧端）不阻断，放行原直连兜底
        if self._pending_autoconnect_seq is not None:
            if self._pending_autoconnect_seq == seq:
                self._pending_autoconnect_seq = None
                QTimer.singleShot(0, self.on_connect)
            return
        # TCP 未命中：房间可能换了设备或线索 IP 已过期，回退 UDP 定向（含广播兜底）
        self._start_direct_udp_probe(ip, room_code, seq)

    def _start_direct_udp_probe(self, host_address: str, room_code: str, seq: int):
        """UDP 定向探测指定 IP 上是否存在该房间号（TCP 未命中时的回退路径）"""
        self.discovery = RoomDiscovery(self)
        self.discovery.room_found.connect(lambda ip, code, port, ver="", sv="", seq=seq: self.on_room_found(ip, code, port, ver, seq, sv))
        self.discovery.discovery_finished.connect(lambda rooms, seq=seq: self.on_discovery_finished(rooms, seq))
        self.discovery.error_occurred.connect(lambda err, seq=seq: self.on_discovery_error(err, seq))
        self._direct_probe = True
        self._fallback_broadcast_done = False
        self.discovery.discover_room_at(host_address, room_code, timeout=1.5)

    def _show_status(self, text: str, color: str = '#868e96'):
        """显示状态标签"""
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")

    def _discovered_has_password(self, room_code: str):
        """本轮发现中该房间是否设密码：True/False；无从判断（无应答或旧端）返回 None。"""
        disc = getattr(self, 'discovery', None)
        if disc is None:
            return None
        try:
            return disc.get_has_password(room_code)
        except Exception:
            return None

    def _reset_regress_state(self):
        """复位"回归为主机"标记与按钮文案（房间号/IP 变更或重新探测时调用）"""
        if not self._regress_as_host:
            return
        self._regress_as_host = False
        self.connect_btn.setText(I18n.tr('connect'))

    def _offer_regress_if_host(self, host_id: str, host_ip: str = "") -> bool:
        """本端即该房间主机 → 连接按钮切换为"回归"，点击以主机身份重新入房。

        复用创建房间界面的回归判据（宿主 end_id == 本机持久 end_id）。命中后不再
        以连接端身份入房，从根本上杜绝主机端被对端"复用管理监听"/陈旧 host_id 钉成
        主机候选而产生的热循环与其他端"验证失败"。
        host_ip 为该宿主线索地址：本进程无房间密码记录时，回归须落回常规连接端
        验证去连它核对密码，故此处一并记为连接锚点。
        """
        if not host_id or host_id != UserConfig.get_end_id():
            return False
        self._regress_as_host = True
        self._room_checked = True
        self._is_checking = False
        if host_ip:
            self.host_address = host_ip
            self.discovered_host = host_ip
            self.host_port = self.host_port or Config.DEFAULT_PORT
        self.connect_btn.setText(I18n.tr('return_as_host'))
        self.connect_btn.setEnabled(True)
        self._show_status(I18n.tr('room_own_regress'), color='#51cf66')
        return True

    def _start_regress(self):
        """以主机身份回归（仅在本进程已知该房间密码时调用）：本地比对内存摘要取明文。

        无密码房（摘要空串）无需弹框；有密码房弹本地校验框（不连主机）验证后取明文。
        密码属房间属性、非主机可控，故此校验不可跳过。
        """
        room_code = self.room_code_input.get_room_code() or self.room_code
        expected = UserConfig.get_room_password_hash(room_code)
        if expected:
            from ui.password_dialog import PasswordDialog
            dlg = PasswordDialog(room_code, "", 0, self, expected_hash=expected)
            if not dlg.exec():
                return  # 取消回归：保持加入界面，可改输密码后重试
            self.password = dlg.get_password()
        else:
            self.password = ""
        self.room_code = room_code
        self.accept()
    
    def on_room_found(self, host_ip: str, room_code: str, port: int, version: str = "", seq: int = None, sync_version: str = ""):
        """发现房间
        Args:
            host_ip: 主机IP
            room_code: 房间号
            port: 端口
            version: 主机应用版本号（仅展示，不参与校验）
            seq: 触发本回调的探测代号（None 表示未代际校验的旧调用点，仍放行）
            sync_version: 主机同步逻辑版本号（加入房间只校验此号一致）
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

        # 占用者即本端（该房间由本机创建）→ 切换为"回归为主机"，不再以连接端身份入房
        discovery = getattr(self, 'discovery', None)
        if discovery is not None and self._offer_regress_if_host(
                discovery.get_host_id(room_code), host_ip):
            return
        
        # 同步逻辑版本号核对（一致性校验只比此号；对端未上报 sync_version
        # 视为不可验证——旧端同步逻辑未知，同样拒绝，避免混跑分叉）
        local_sync = Config.SYNC_LOGIC_VERSION
        if sync_version != local_sync:
            # 版本不一致：红字显示，禁用连接按钮
            self._show_status(
                I18n.tr('version_mismatch', local=local_sync, remote=sync_version),
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
        # 聚合本轮全部应答：宿主 end_id 即本端 → 该房间由本机创建，切换为"回归为主机"
        # （按当前房间号过滤：本机若正主持其它房间，其应答不得误判到本次房间号上）
        my_id = UserConfig.get_end_id()
        cur_code = self._pending_room_code or self.room_code_input.get_room_code()
        mine = next(
            (r for r in rooms or []
             if isinstance(r, dict) and (r.get('host_id') or '') == my_id
             and r.get('room_code') == cur_code),
            None)
        if mine is not None:
            if self._offer_regress_if_host(my_id, (mine.get('ip') or '').strip()):
                return
        if not rooms:
            # 定向探测未命中且未做过广播回退：历史/发现者 IP 可能已过期（房间换了
            # IP）或定向一次性 UDP 丢包。清掉手动 IP 回退广播重扫一次，按房间号重定位
            # 新 IP——否则会误报"未找到房间"并把连接按钮禁用，用户只能退出重进才连上。
            if getattr(self, '_direct_probe', False) \
                    and not getattr(self, '_fallback_broadcast_done', False):
                self._fallback_broadcast_done = True
                self._direct_probe = False
                # 隐藏式清空地址框（不触发 textChanged 重扫，避免搞出探测叠加）
                self.host_edit.blockSignals(True)
                self.host_edit.clear()
                self.host_edit.blockSignals(False)
                self.host_address = ""
                self.discovered_host = ""
                self._ip_source = ""  # 地址已清空，来源标记同步复位
                self._check_room_exists()  # 空 host → 进入广播 discover_room
                return
            # 广播（或已回退）仍未找到：判未找到并禁用按钮
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

        # 回归态：本端即该房间主机，以主机身份重新入房，不走"被钉成候选"的连接端路径。
        # 已知该房间密码（含无密码房）→ 本地校验后直接开主机窗；
        # 未知（重启/同机双开失忆）→ 不静默放行，落回下面常规连接端验证连存活主机
        # 服务核对密码，通过后 _regress_as_host 仍为真，main_window 自会以主机身份开窗。
        if self._regress_as_host:
            if UserConfig.has_room_password_record(room_code):
                self._start_regress()
                return
            # 本进程无密码记录（重启/同机双开失忆）：由发现广播判定房间有无密码。
            # 明确无密码房 → 直接以主机身份回归，不再弹密码框（否则空输入禁用确认的死锁）
            if self._discovered_has_password(room_code) is False:
                self.room_code = room_code
                self.password = ""
                UserConfig.set_room_password(room_code, "")  # 记回属性，供下次回归免探测
                self.accept()
                return
            self._manual_self_checked = True  # 已判定宿主即本端，无需再做本机 IP 自检

        # 锚未建立时需先探测确认房间存在再连，避免用旧锚点连错 IP 卡在验证中。
        # 例外：用户已手动填写完整 IP（方案 1：直连入口）——视为有效锚点，直接尝试连接，
        # 连不上由 _attempt_connect 的验证超时兜底，不再强行走定向探测
        # （跨网段/防火墙下 UDP 探测本就摸不到，反而会禁用按钮导致连不上）。
        manual_host = self.host_edit.text().strip()
        is_manual = self.room_code_input.is_complete() and ('.' in manual_host)
        # 手输地址即本机地址：先 TCP 探活取宿主 id 判定（自连会令主机退化成连接端），
        # 探明宿主非本端或探活未答则自动续走直连；远程 IP 不受影响，仍走原直连快路径
        if is_manual and not self._manual_self_checked and RoomDiscovery.is_local_ip(manual_host):
            self._manual_self_checked = True
            self._check_room_exists()
            self._pending_autoconnect_seq = self._check_seq  # 本次探测代号（回调凭此续接）
            return
        if not is_manual and not self._room_checked:
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
            # 记录房间密码属性（内存摘要，空串=已知的无密码房，供本机回归据此校验）
            UserConfig.set_room_password(self.room_code, "")
            self.accept()
        elif status == 'regress':
            # 对端回带主机即本端且空密码被接受 ⇒ 本端即该房间主机、房间无密码
            # → 直接以主机身份回归，避免被当成"需要密码"而卡在密码框
            self._regress_as_host = True
            self.password = ""
            UserConfig.set_room_password(self.room_code, "")
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
            # "对端回带主机即本端"是回归信号而非密码失败：区分出来，交由 on_connect
            # 以主机身份回归（消息已在密码校验之后产生，说明空密码已被对端接受）
            if SyncClient.REGRESS_AUTH_MSG in (msg or ""):
                result['status'] = 'regress'
            else:
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
                # 记录历史：房间号始终记；仅"用户手输 IP"才落该 IP 作定向探活线索
                UserConfig.add_room_history(self.room_code, self._manual_entry_ip())
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
            if result['status'] == 'regress':
                return 'regress', ''
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
            # 记录房间密码属性（SHA256 摘要，非明文；加入端同样持房间属性）
            UserConfig.set_room_password(self.room_code, self.password)
            # 密码房此前从不写历史，导致手输 IP 的密码房永远进不了历史（线索丢失）
            UserConfig.add_room_history(self.room_code, self._manual_entry_ip())
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

    def is_regress_as_host(self) -> bool:
        """是否以"回归为主机"方式进入（该房间由本机创建）"""
        return self._regress_as_host

    # ========== 发现房间板块相关方法 ==========

    def _start_scan_all_rooms(self):
        """开始扫描局域网内所有房间"""
        if self._is_scanning:
            return

        self._is_scanning = True
        self.scan_btn.setEnabled(False)
        # 统一分组模型：扫描启动重置各房间本次成员
        #  - 历史行：清空成员、在线归 0、指示条回黄（本次未响应则保持黄）
        #  - 纯扫描行（非历史，易失）：从列表移除，本次重新发现后再建
        for code, group in list(self._room_groups.items()):
            group['members'] = []
            group['count'] = 0
            group['probe_online'] = False  # 本轮 TCP 探活是否命中
            group['probe_ip'] = ""         # 命中时采用的 IP
            group['host_id'] = ""          # 本轮宿主 end_id（重新聚合）
            w = group.get('widget')
            if w is None:
                continue
            w.set_count(0)
            if group.get('is_history'):
                w.set_status(False)
            else:
                item = group.get('item')
                if item is not None and self.rooms_list_widget.row(item) >= 0:
                    self.rooms_list_widget.takeItem(self.rooms_list_widget.row(item))
                del self._room_groups[code]
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

        # 扫描窗口（秒）：广播发现聚合所有房间（历史行是否在线亦由它兜底）
        self._scan_timeout = 2
        self._scan_discovery.discover_all_rooms(timeout=self._scan_timeout)

        # 带线索 IP 的历史房间：并行做 TCP 定向探活（跨网段/防火墙/对端发现应答器未启动
        # 时 UDP 全摸不到，只有 TCP 可靠）。广播为全房间单次扫描、无法按房间排除，故照常
        # 执行；未命中的房间自然由广播结果兜底。
        if self._probe is not None:  # 重扫：先停上一轮探活，避免残留回调
            try:
                self._probe.stop()
            except Exception:
                pass
        self._probe = TcpRoomProbe(self)
        self._probe.probed.connect(self._on_probe_result)
        for code, group in list(self._room_groups.items()):
            ip = group.get('ip') or ""
            if group.get('is_history') and ip:
                self._probe.probe(ip, code, Config.DEFAULT_PORT)

    # ---- 房间分组模型 ----

    def _new_room_group(self, room_code: str, is_history: bool, ip: str = "") -> dict:
        """新建一个房间分组并把对应行插入列表。历史行放顶部，扫描行放底部。"""
        group = {
            'room_code': room_code,
            'members': [], 'count': 0, 'is_history': is_history,
            'ip': ip or '',                # 历史线索 IP（仅手动输入过 IP 的房间有）
            'probe_online': False, 'probe_ip': "",
            'host_id': '',                 # 扫描聚合到的宿主 end_id（用于"回归为主机"判定）
            'widget': None, 'item': None,
        }
        if is_history:
            widget = RoomRowWidget(room_code, 0, show_delete=True, ip=ip)  # 动态指示条
        else:
            widget = RoomRowWidget(room_code, 0, indicator_color=RoomRowWidget.COLOR_SCANNED, show_delete=False)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, room_code)
        item.setSizeHint(QSize(0, RoomRowWidget.ROW_HEIGHT))
        if is_history:
            self.rooms_list_widget.insertItem(0, item)  # 历史置顶
        else:
            self.rooms_list_widget.addItem(item)
        self.rooms_list_widget.setItemWidget(item, widget)
        widget.clicked.connect(lambda g=group: self._fill_from_room(g))
        if is_history:
            widget.remove_requested.connect(lambda g=group: self._remove_history_group(g))
        group['widget'] = widget
        group['item'] = item
        self._room_groups[room_code] = group
        return group

    def _live_room_count(self) -> int:
        """当前有在线成员的房间数（用于『发现 N 个房间』状态）"""
        return sum(1 for g in self._room_groups.values() if g['count'] > 0)

    def _apply_group_online(self, group: dict):
        """按成员数刷新该行『在线X』并在有成员时点亮绿；扫描行固定蓝不受影响"""
        group['count'] = len(group['members'])
        w = group.get('widget')
        if w is None:
            return
        w.set_count(group['count'])
        if group.get('is_history') and group['count'] > 0:
            w.set_status(True)
        self.scan_status_label.setText(I18n.tr('rooms_found_count', count=self._live_room_count()))
        self.scan_status_label.setStyleSheet("color: #51cf66; font-size: 12px;")

    def _on_probe_result(self, room_code: str, ip: str, online: bool, host_id: str = ""):
        """TCP 探活回调：命中即按 IP 并入成员并点亮；未命中交由广播发现兜底"""
        group = self._room_groups.get(room_code)
        if group is None or not group.get('is_history') or not online:
            return
        group['probe_online'] = True
        group['probe_ip'] = ip
        # 记录宿主 end_id：点击该行填充时据此判定"回归为主机"（防主机以连接端身份加入）
        if host_id:
            group['host_id'] = host_id
        # 按 IP 去重并入成员：广播若也命中同 IP 不重复计数
        if all(m.get('ip') != ip for m in group['members']):
            group['members'].append({
                'ip': ip, 'port': Config.DEFAULT_PORT,
                'version': '', 'sync_version': Config.SYNC_LOGIC_VERSION,
            })
        self._apply_group_online(group)

    def _on_scan_room_found(self, host_ip: str, room_code: str, port: int, version: str = "", sync_version: str = ""):
        """扫描发现单个成员：按房间号聚合进分组，更新『在线X』。

        只有 sync_version 一致的兼容成员计入在线数且可作为加入目标（与
        RoomDiscovery.get_room_aggregates 一致）；不兼容成员不记账、不参与加入。
        """
        # 过滤 127.0.0.1 地址（只保留真实 IP）
        if host_ip == '127.0.0.1':
            return
        if not room_code:
            return
        # 兼容性过滤：局端同步逻辑版本一致者才计入在线N
        if sync_version != Config.SYNC_LOGIC_VERSION:
            return

        group = self._room_groups.get(room_code)
        if group is None:
            group = self._new_room_group(room_code, is_history=False)

        # 成员按 (ip, port) 去重（同端多应答不重复计数）
        for m in group['members']:
            if m['ip'] == host_ip and m['port'] == port:
                break
        else:
            group['members'].append({
                'ip': host_ip, 'port': port,
                'version': version, 'sync_version': sync_version,
            })

        self._apply_group_online(group)

    def _on_scan_finished(self, rooms: list):
        """扫描完成"""
        self._is_scanning = False
        self.scan_btn.setEnabled(True)

        # 把本轮各房宿主的 end_id 落进分组：列表点击行时据此判定"回归为主机"
        # （点击行走 TCP 定向探活，只回在线状态、拿不到 host_id，故须在扫描收尾先存下）
        for r in rooms or []:
            if not isinstance(r, dict):
                continue
            group = self._room_groups.get(r.get('room_code'))
            if group is None:
                continue
            hid = (r.get('host_id') or '').strip()
            if hid:
                group['host_id'] = hid

        # 隐藏加载动画
        if self._loader:
            self._loader.hide()

        if self._live_room_count() == 0:
            self.scan_status_label.setText(I18n.tr('no_rooms_found'))
            self.scan_status_label.setStyleSheet("color: #868e96; font-size: 12px;")

        # 清理扫描服务
        if self._scan_discovery:
            self._scan_discovery.stop_discovery()
            self._scan_discovery = None

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
        """列表项点击：所有行均为 RoomRowWidget，填充已由其 clicked 信号处理，此处无需动作"""
        pass

    def _set_host_text(self, text: str):
        """程序写入 host 栏：置位来源标记，避免被 _on_host_edit_text_changed 当成手输"""
        self._setting_host = True
        self.host_edit.setText(text)
        self._setting_host = False

    def _manual_entry_ip(self) -> str:
        """用户手输模式下当前连接的主机 IP；非手输返回空串（不落历史线索）"""
        if self._ip_source != 'manual':
            return ""
        host = (self.host_address or "").strip()
        return host if '.' in host else ""

    def _fill_from_room(self, group: dict):
        """根据房间分组填充输入框；有兼容成员则取首个成员为默认入口并定向探测。

        无在线成员（离线历史房间）：仅填房间号，随后触发广播搜索；用户也可在
        host 栏手动输入 IP 强制定向加入（跨网段/UDP 摸不到的兜底入口）。
        """
        room_code = group.get('room_code')
        if not room_code:
            return
        if room_code == self.room_code_input.get_room_code():
            return  # 匹配项不可点击，直接返回

        # 填充房间号到输入框（不触发检测，避免重复刷新）
        self.room_code_input.set_room_code(room_code, trigger_check=False)

        # 入口优先用 TCP 探活命中的 IP（跨网段下的可靠锚点），其次广播成员
        entry_ip = group.get('probe_ip') if group.get('probe_online') else ""
        entry_port = 0
        if not entry_ip:
            for m in group.get('members') or []:
                if m.get('ip'):
                    entry_ip, entry_port = m['ip'], (m.get('port') or Config.DEFAULT_PORT)
                    break
        if not entry_ip and group.get('is_history'):
            # 线索 IP：本轮 TCP 探活尚未回结果时也先试，交由定向探活确认/回退
            entry_ip = (group.get('ip') or "").strip()
            entry_port = Config.DEFAULT_PORT
        if entry_ip:
            self.discovered_host = entry_ip
            self.host_port = entry_port or Config.DEFAULT_PORT
            self.host_address = entry_ip  # 同时设置 host_address，确保连接时使用正确地址
            # 填入地址框使后续探测走定向探测（程序写入，不记作手输线索）
            self._set_host_text(entry_ip)
        else:
            # 无在线成员：清空主机栏，交由广播/手动 IP 兜底
            self.discovered_host = ""
            self.host_address = ""
            self.host_port = Config.DEFAULT_PORT
            self._set_host_text("")

        # 该房间由本机创建 → 直接切换为"回归为主机"，跳过探测与连接端路径
        # （地址已先行落定：本进程无密码记录时，回归须落回常规连接端验证去连它核对密码）
        if self._offer_regress_if_host((group.get('host_id') or '').strip(), entry_ip):
            return

        # 显式中断旧扫描并按『当前房间号 + 地址框(可能为空)』重新探测
        # ——同房间号切换时 host 文本不变，textChanged 不会触发，必须显式重扫
        self._schedule_host_probe()

    def _update_matching_room_style(self):
        """更新列表项样式：匹配当前输入的房间号时灰色不可点击"""
        current_code = self.room_code_input.get_room_code()
        for i in range(self.rooms_list_widget.count()):
            widget = self.rooms_list_widget.itemWidget(self.rooms_list_widget.item(i))
            if widget is not None:
                widget.set_matched(widget.room_code == current_code)

    # ========== 最近连接历史（房间号 + 可选的手输 IP 线索） ==========

    def _load_history(self):
        """加载历史房间到列表顶部（默认黄；广播或 TCP 探活命中后点亮绿）"""
        self._clear_history_rows()
        for entry in UserConfig.get_room_history_entries():
            code = entry.get('room_code')
            if not code:
                continue
            self._new_room_group(code, is_history=True, ip=entry.get('ip') or "")

    def _clear_history_rows(self):
        """移除全部历史分组行（含列表项）与缓存"""
        for code, group in list(self._room_groups.items()):
            if not group.get('is_history'):
                continue
            item = group.get('item')
            if item is not None and self.rooms_list_widget.row(item) >= 0:
                self.rooms_list_widget.takeItem(self.rooms_list_widget.row(item))
            del self._room_groups[code]

    def _remove_history_group(self, group: dict):
        """删除一条历史：从持久化 + 界面分组一并移除"""
        code = group.get('room_code')
        if code:
            UserConfig.remove_room_history(code)
        item = group.get('item')
        if item is not None and self.rooms_list_widget.row(item) >= 0:
            self.rooms_list_widget.takeItem(self.rooms_list_widget.row(item))
        if group.get('room_code') in self._room_groups:
            del self._room_groups[group['room_code']]
