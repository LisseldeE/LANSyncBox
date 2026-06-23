"""
主窗口
"""
import os
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QSpacerItem, QSizePolicy, QMessageBox
)
from PySide6.QtCore import Qt, QSize, QUrl
from PySide6.QtGui import QFont, QDesktopServices

from i18n import I18n
from config import Config, UserConfig
from ui.create_room_dialog import CreateRoomDialog
from ui.join_room_dialog import JoinRoomDialog
from ui.about_dialog import AboutDialog
from ui.widgets import AnimatedButton, BUTTON_STYLES


class MainWindow(QMainWindow):
    """主窗口"""
    
    def __init__(self):
        super().__init__()
        self._sync_window = None  # 保持同步窗口引用
        self.init_ui()
    
    def init_ui(self):
        """初始化界面"""
        # 窗口设置
        self.setWindowTitle(f"{I18n.tr('app_name')} - {I18n.tr('app_title')}")
        self.setMinimumSize(400, 500)
        self.resize(400, 500)
        
        # 创建中心部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 主布局
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(20)
        main_layout.setContentsMargins(30, 30, 30, 30)
        
        # 标题
        title_label = QLabel(I18n.tr('app_name'))
        title_font = QFont()
        title_font.setPointSize(24)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title_label)
        
        # 副标题
        subtitle_label = QLabel(I18n.tr('app_title'))
        subtitle_font = QFont()
        subtitle_font.setPointSize(12)
        subtitle_label.setFont(subtitle_font)
        subtitle_label.setAlignment(Qt.AlignCenter)
        subtitle_label.setStyleSheet("color: #666;")
        main_layout.addWidget(subtitle_label)
        
        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        main_layout.addWidget(line)
        
        # 按钮区域
        button_layout = QVBoxLayout()
        button_layout.setSpacing(15)
        
        # 创建房间按钮
        self.create_room_btn = AnimatedButton(I18n.tr('create_room'))
        self.create_room_btn.setMinimumHeight(50)
        self.create_room_btn.clicked.connect(self.on_create_room)
        self.create_room_btn.setStyleSheet(BUTTON_STYLES['primary'])
        button_layout.addWidget(self.create_room_btn)
        
        # 加入房间按钮
        self.join_room_btn = AnimatedButton(I18n.tr('join_room'))
        self.join_room_btn.setMinimumHeight(50)
        self.join_room_btn.clicked.connect(self.on_join_room)
        self.join_room_btn.setStyleSheet(BUTTON_STYLES['primary'])
        button_layout.addWidget(self.join_room_btn)
        
        main_layout.addLayout(button_layout)
        
        # 弹性空间
        main_layout.addSpacerItem(QSpacerItem(20, 40, QSizePolicy.Minimum, QSizePolicy.Expanding))
        
        # 底部按钮区域（三个按钮均匀分布）
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(10)
        
        # 弹性空间 - 首部
        bottom_layout.addStretch()
        
        # 语言切换按钮
        self.lang_btn = AnimatedButton(self._get_language_text())
        self.lang_btn.setFixedWidth(100)
        self.lang_btn.clicked.connect(self.on_toggle_language)
        self.lang_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        bottom_layout.addWidget(self.lang_btn)
        
        # 弹性空间 - 中间
        bottom_layout.addStretch()
        
        # 管理缓存按钮
        self.manage_cache_btn = AnimatedButton(I18n.tr('manage_cache'))
        self.manage_cache_btn.setFixedWidth(100)
        self.manage_cache_btn.clicked.connect(self.on_manage_cache)
        self.manage_cache_btn.setStyleSheet(BUTTON_STYLES['outline'])
        bottom_layout.addWidget(self.manage_cache_btn)
        
        # 弹性空间 - 中间
        bottom_layout.addStretch()
        
        # 关于按钮
        about_btn = AnimatedButton(I18n.tr('about'))
        about_btn.setFixedWidth(100)
        about_btn.clicked.connect(self.on_about)
        about_btn.setStyleSheet(BUTTON_STYLES['outline'])
        bottom_layout.addWidget(about_btn)
        
        # 弹性空间 - 尾部
        bottom_layout.addStretch()
        
        main_layout.addLayout(bottom_layout)
        
        # 版本信息
        version_label = QLabel(I18n.tr('about_version', version=Config.APP_VERSION))
        version_label.setAlignment(Qt.AlignCenter)
        version_label.setStyleSheet("color: #999; font-size: 11px;")
        main_layout.addWidget(version_label)
    
    def on_create_room(self):
        """创建房间"""
        dialog = CreateRoomDialog(self)
        if dialog.exec():
            # 获取创建的房间信息
            room_code = dialog.get_room_code()
            password = dialog.get_password()
            
            # 打开同步窗口
            self.open_sync_window(is_host=True, room_code=room_code, password=password)
    
    def on_join_room(self):
        """加入房间"""
        dialog = JoinRoomDialog(self)
        if dialog.exec():
            # 获取房间信息
            room_code = dialog.get_room_code()
            password = dialog.get_password()
            host_address = dialog.get_host_address()
            host_port = dialog.get_host_port()
            
            # 打开同步窗口
            self.open_sync_window(is_host=False, room_code=room_code, password=password, host_address=host_address, host_port=host_port)
    
    def on_toggle_language(self):
        """切换语言"""
        current_lang = I18n.get_language()
        if current_lang == "zh_CN":
            I18n.set_language("en_US")
        else:
            I18n.set_language("zh_CN")
        
        # 持久化语言设置到 config.json
        UserConfig.set_language(I18n.get_language())
        
        # 刷新界面
        self._refresh_ui()
    
    def on_manage_cache(self):
        """打开 SyncFolder 缓存文件夹"""
        sync_folder = Config.get_sync_folder()
        if not sync_folder.exists():
            QMessageBox.warning(self, I18n.tr('manage_cache'), I18n.tr('manage_cache_not_found'))
            return
        url = QUrl.fromLocalFile(str(sync_folder))
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, I18n.tr('manage_cache'), I18n.tr('manage_cache_error'))
    
    def _get_language_text(self) -> str:
        """获取语言按钮显示文本"""
        current_lang = I18n.get_language()
        if current_lang == "zh_CN":
            return I18n.tr('english')
        else:
            return I18n.tr('chinese')
    
    def _refresh_ui(self):
        """刷新界面文本"""
        self.setWindowTitle(f"{I18n.tr('app_name')} - {I18n.tr('app_title')}")
        self.lang_btn.setText(self._get_language_text())
        
        # 刷新子控件
        central_widget = self.centralWidget()
        if central_widget:
            layout = central_widget.layout()
            if layout:
                # 标题
                title_label = layout.itemAt(0).widget()
                if title_label:
                    title_label.setText(I18n.tr('app_name'))
                
                # 副标题
                subtitle_label = layout.itemAt(1).widget()
                if subtitle_label:
                    subtitle_label.setText(I18n.tr('app_title'))
                
                # 按钮
                button_layout = layout.itemAt(3)
                if button_layout:
                    create_btn = button_layout.itemAt(0).widget()
                    join_btn = button_layout.itemAt(1).widget()
                    if create_btn:
                        create_btn.setText(I18n.tr('create_room'))
                    if join_btn:
                        join_btn.setText(I18n.tr('join_room'))
                
                # 底部按钮（布局：stretch, lang_btn, stretch, manage_cache_btn, stretch, about_btn, stretch）
                bottom_layout = layout.itemAt(5)
                if bottom_layout:
                    # manage_cache_btn 在索引 3
                    manage_cache_btn = bottom_layout.itemAt(3).widget()
                    if manage_cache_btn:
                        manage_cache_btn.setText(I18n.tr('manage_cache'))
                    # about_btn 在索引 5
                    about_btn = bottom_layout.itemAt(5).widget()
                    if about_btn:
                        about_btn.setText(I18n.tr('about'))
                
                # 版本信息
                version_label = layout.itemAt(6).widget()
                if version_label:
                    version_label.setText(I18n.tr('about_version', version=Config.APP_VERSION))
    
    def on_about(self):
        """关于"""
        dialog = AboutDialog(self)
        dialog.exec()
    
    def open_sync_window(self, is_host: bool, room_code: str, password: str = "", host_address: str = "", host_port: int = None):
        """打开同步窗口"""
        from ui.sync_window import SyncWindow
        
        # 隐藏主窗口
        self.hide()
        
        # 创建同步窗口并保持引用
        self._sync_window = SyncWindow(
            is_host=is_host,
            room_code=room_code,
            password=password,
            host_address=host_address,
            host_port=host_port
        )
        self._sync_window.setWindowTitle(f"{I18n.tr('app_name')} - {I18n.tr('room_info', code=room_code)}")
        self._sync_window.setMinimumSize(Config.WINDOW_MIN_WIDTH, Config.WINDOW_MIN_HEIGHT)
        self._sync_window.resize(1000, 700)
        
        # 窗口关闭时显示主窗口
        self._sync_window.closed.connect(self.show)
        
        self._sync_window.show()