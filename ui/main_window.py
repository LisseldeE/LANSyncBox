"""
主窗口
"""
import os
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QSpacerItem, QSizePolicy, QMessageBox
)
from PySide6.QtCore import Qt, QSize, QUrl, QFileSystemWatcher, QTimer
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
        self._version_label = None  # 版本和缓存信息标签
        self._cache_watcher = None  # 文件系统监控器
        self._cache_refresh_timer = None  # 缓存刷新延迟定时器
        
        self.init_ui()
        self._setup_cache_watcher()

    def _setup_cache_watcher(self):
        """设置缓存文件夹监控器"""
        self._cache_watcher = QFileSystemWatcher()
        self._cache_refresh_timer = QTimer()
        self._cache_refresh_timer.setSingleShot(True)  # 单次触发
        self._cache_refresh_timer.timeout.connect(self._refresh_cache_size)

        # 获取缓存文件夹路径（确保存在）
        try:
            cache_folder = Config.get_sync_folder()
            cache_folder_str = str(cache_folder)
        except Exception as e:
            # 路径获取失败,不继续初始化监控器
            return

        # 监听缓存文件夹及其所有子文件夹
        paths_to_watch = [cache_folder_str]

        # 递归添加子文件夹（如果有）
        if cache_folder.exists():
            try:
                for root, dirs, files in os.walk(cache_folder_str):
                    for dir_name in dirs:
                        dir_path = os.path.join(root, dir_name)
                        paths_to_watch.append(dir_path)
            except Exception as e:
                # 遍历失败,只监控根目录
                paths_to_watch = [cache_folder_str]

        # 添加所有路径到监控器
        for path in paths_to_watch:
            if os.path.exists(path):
                try:
                    self._cache_watcher.addPath(path)
                except Exception as e:
                    # 添加失败,跳过此路径
                    pass

        # 当文件夹内容变化时，延迟刷新缓存大小（避免频繁触发）
        self._cache_watcher.directoryChanged.connect(self._delayed_refresh_cache)

    def _delayed_refresh_cache(self):
        """延迟刷新缓存大小（合并短时间内多次触发）"""
        # 如果定时器已经在运行，重启它（合并多次触发）
        if self._cache_refresh_timer.isActive():
            self._cache_refresh_timer.stop()
        # 延迟500ms后刷新，避免频繁计算
        self._cache_refresh_timer.start(500)

    def _format_size(self, size_bytes: int) -> str:
        """格式化文件大小（字节转换为人类可读格式）"""
        if size_bytes == 0:
            return "0 B"

        units = ['B', 'KB', 'MB', 'GB', 'TB']
        unit_index = 0
        size = float(size_bytes)

        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1

        # 小于1MB显示整数，大于等于1MB显示两位小数
        if unit_index < 2:  # B 或 KB
            return f"{int(size)} {units[unit_index]}"
        else:
            return f"{size:.2f} {units[unit_index]}"
    
    def init_ui(self):
        """初始化界面"""
        # 窗口设置
        self.setWindowTitle(I18n.tr('app_name'))
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

        # 版本和缓存信息
        cache_size = Config.get_cache_size()
        cache_size_str = self._format_size(cache_size)
        version_text = f"{I18n.tr('about_version', version=Config.DISPLAY_VERSION)}  |  {I18n.tr('cache_size', size=cache_size_str)}"
        self._version_label = QLabel(version_text)
        self._version_label.setAlignment(Qt.AlignCenter)
        self._version_label.setStyleSheet("color: #999; font-size: 11px;")
        main_layout.addWidget(self._version_label)
    
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
            # 获取预验证成功的 Client 实例（避免 SyncWindow 重复连接）
            verified_client = dialog.get_verified_client()

            # 打开同步窗口
            self.open_sync_window(is_host=False, room_code=room_code, password=password, host_address=host_address, host_port=host_port, existing_client=verified_client)
    
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
        self.setWindowTitle(I18n.tr('app_name'))
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

                # 版本和缓存信息
                if self._version_label:
                    cache_size = Config.get_cache_size()
                    cache_size_str = self._format_size(cache_size)
                    version_text = f"{I18n.tr('about_version', version=Config.DISPLAY_VERSION)}  |  {I18n.tr('cache_size', size=cache_size_str)}"
                    self._version_label.setText(version_text)
    
    def on_about(self):
        """关于"""
        dialog = AboutDialog(self)
        dialog.exec()
    
    def open_sync_window(self, is_host: bool, room_code: str, password: str = "", host_address: str = "", host_port: int = None, existing_client=None):
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
            host_port=host_port,
            existing_client=existing_client,
        )
        self._sync_window.setWindowTitle(I18n.tr('app_name'))
        self._sync_window.setMinimumSize(Config.WINDOW_MIN_WIDTH, Config.WINDOW_MIN_HEIGHT)
        self._sync_window.resize(1000, 700)

        # 窗口关闭时刷新缓存大小并显示主窗口
        self._sync_window.closed.connect(self._show_and_refresh_cache)

        self._sync_window.show()

    def _refresh_cache_size(self):
        """刷新缓存大小显示"""
        if self._version_label:
            cache_size = Config.get_cache_size()
            cache_size_str = self._format_size(cache_size)
            version_text = f"{I18n.tr('about_version', version=Config.DISPLAY_VERSION)}  |  {I18n.tr('cache_size', size=cache_size_str)}"
            self._version_label.setText(version_text)

    def _show_and_refresh_cache(self):
        """刷新缓存大小后显示主窗口"""
        # 刷新缓存大小显示
        self._refresh_cache_size()

        # 显示主窗口
        self.show()