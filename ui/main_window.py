"""
主窗口
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import os
import re
import threading
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QSpacerItem, QSizePolicy, QApplication
)
from PySide6.QtCore import Qt, QSize, QUrl, Signal, QFileSystemWatcher, QTimer, QRectF
from PySide6.QtGui import QFont, QDesktopServices, QPainter, QPixmap, QColor, QPalette
from PySide6.QtSvg import QSvgRenderer

from i18n import I18n
from config import Config, UserConfig
from ui.create_room_dialog import CreateRoomDialog
from ui.join_room_dialog import JoinRoomDialog
from ui.about_dialog import AboutDialog, fetch_latest_version
from ui.settings_dialog import SettingsDialog
from ui.capsule_notification import CapsuleNotification
from ui.announcement import fetch_announcement, is_newer
from ui.announcement_dialog import AnnouncementDialog
from ui.widgets import AnimatedButton, SnapOutlineButton, BUTTON_STYLES, ClickableLabel


def _render_megaphone_icon(color: str) -> QPixmap:
    """渲染公告喇叭线条图标（2 倍尺寸保证高分屏清晰）"""
    svg = ('<svg viewBox="0 0 24 24" fill="none" stroke="%C%" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round">'
           '<path d="M3 11v2a1 1 0 0 0 1 1h2l7 4V6L6 10H4a1 1 0 0 0-1 1z"/>'
           '<path d="M14 9.5a3 3 0 0 1 0 5"/></svg>')
    renderer = QSvgRenderer(bytearray(svg.replace('%C%', color).encode('utf-8')))
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return pix


class AnnouncementEntry(QFrame):
    """公告持久入口（细条样式）：喇叭图标 + 单行省略公告预览。

    默认隐藏不占布局空间；有公告时由主窗口 set_announcement 显示并常驻，
    直到收到更新的公告才刷新。点击（clicked 信号）由主窗口打开详情窗口。
    浅灰底细条，悬浮略深；深浅主题自适应。
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(32)
        self._hovered = False
        self._dark = None  # 上次应用的深浅标记，paintEvent 时按需刷新

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(8)
        self._icon = QLabel(self)
        self._icon.setFixedSize(16, 16)
        self._icon.setScaledContents(True)
        lay.addWidget(self._icon)
        self._text = QLabel(self)
        lay.addWidget(self._text, 1)

        self._full_text = ""
        self.setVisible(False)

    # ------------------------------------------------------------------ 对外接口

    def set_announcement(self, text: str):
        """设置公告预览文本并显示（持久入口，直到新公告刷新）"""
        self._full_text = text
        self._update_text()
        self.setVisible(True)

    def clear_announcement(self):
        """清除公告并隐藏入口"""
        self._full_text = ""
        self._text.clear()
        self.setVisible(False)

    # ------------------------------------------------------------------ 内部实现

    def _update_text(self):
        fm = self._text.fontMetrics()
        avail = self.width() - 20 - 16 - 8  # 左右内边距 + 图标 + 间距
        self._text.setText(fm.elidedText(self._full_text, Qt.ElideRight, max(avail, 20)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._full_text:
            self._update_text()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        self._sync_theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.NoPen)
        p.setBrush(self._bg_hover if self._hovered else self._bg)
        p.drawRoundedRect(rect, 6.0, 6.0)
        p.end()
        super().paintEvent(event)

    def _sync_theme(self):
        """按当前深浅主题刷新底色/文字/图标色（仅在深浅切换时重新应用）"""
        win = self.window()
        pal = win.palette() if win is not None else QApplication.palette()
        bg = pal.color(QPalette.Window)
        dark = (bg.red() * 0.299 + bg.green() * 0.587 + bg.blue() * 0.114) < 128
        if dark == self._dark:
            return
        self._dark = dark
        if dark:
            self._bg, self._bg_hover = QColor("#262626"), QColor("#2f2f2f")
            fg, icon = "#a0a0a0", "#a0a0a0"
        else:
            self._bg, self._bg_hover = QColor("#f1f3f5"), QColor("#e7eaee")
            fg, icon = "#495057", "#868e96"
        self._text.setStyleSheet(
            f"color: {fg}; font-size: 12px; background: transparent; border: none;")
        self._icon.setPixmap(_render_megaphone_icon(icon))


class MainWindow(QMainWindow):
    """主窗口"""

    _update_fetched = Signal(str)  # 自动检查更新结果（新版本号；空串=无需提醒）
    _announcement_fetched = Signal(str, str)  # 公告拉取结果（版本号, 正文；失败为空串）

    def __init__(self):
        super().__init__()
        self._sync_window = None  # 保持同步窗口引用
        self._settings_dialog = None  # 设置对话框引用（非模态需持有防 GC）
        self._version_label = None  # 版本和缓存信息标签
        self._update_label = None  # 发现新版本小字提醒标签
        self._update_version = ""  # 已提醒的新版本号（用于语言切换后刷新文案）
        self._cache_watcher = None  # 文件系统监控器
        self._cache_refresh_timer = None  # 缓存刷新延迟定时器
        self._announce_capsule = CapsuleNotification()  # 公告提示胶囊
        self._announcement = None  # 当前公告 (版本, 正文)，主界面入口持久显示直到新公告
        
        self.init_ui()
        self._setup_cache_watcher()
        self._update_fetched.connect(self._on_update_fetched)
        self._announcement_fetched.connect(self._on_announcement_fetched)
        self._start_auto_update_check()
        self._start_announcement_check()

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

    def _get_cache_color(self, size_bytes: int) -> str:
        """
        根据缓存大小返回对应的颜色

        Args:
            size_bytes: 缓存大小（字节）

        Returns:
            颜色字符串（十六进制）
        """
        # 转换为MB
        size_mb = size_bytes / (1024 * 1024)

        if size_mb < 200:
            return "#999999"  # 灰色
        elif size_mb < 500:
            return "#ff922b"  # 橙黄色
        else:
            return "#f03e3e"  # 红色

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

        # 公告持久入口（细条）：有公告时显示，点击查看完整公告；默认隐藏不占布局
        self._announce_entry = AnnouncementEntry()
        self._announce_entry.clicked.connect(self._on_announcement_clicked)
        main_layout.addWidget(self._announce_entry)

        # 弹性空间
        main_layout.addSpacerItem(QSpacerItem(20, 40, QSizePolicy.Minimum, QSizePolicy.Expanding))
        
        # 底部按钮区域（三个按钮均匀分布）
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(10)
        
        # 弹性空间 - 首部
        bottom_layout.addStretch()
        
        # 语言切换按钮
        self.lang_btn = AnimatedButton(self._get_language_text())
        self.lang_btn.setFixedSize(100, 34)
        self.lang_btn.clicked.connect(self.on_toggle_language)
        self.lang_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        bottom_layout.addWidget(self.lang_btn)
        
        # 弹性空间 - 中间
        bottom_layout.addStretch()
        
        # 设置按钮（设备像素对齐边框，避免非整数缩放下边框被裁切）
        self.manage_cache_btn = SnapOutlineButton(I18n.tr('settings'))
        self.manage_cache_btn.setFixedSize(100, 34)
        self.manage_cache_btn.clicked.connect(self.on_open_settings)
        bottom_layout.addWidget(self.manage_cache_btn)
        
        # 弹性空间 - 中间
        bottom_layout.addStretch()
        
        # 关于按钮（设备像素对齐边框）
        self.about_btn = SnapOutlineButton(I18n.tr('about'))
        self.about_btn.setFixedSize(100, 34)
        self.about_btn.clicked.connect(self.on_about)
        bottom_layout.addWidget(self.about_btn)
        
        # 弹性空间 - 尾部
        bottom_layout.addStretch()

        # 发现新版本小字提醒（默认隐藏，发现新版本后显示，可点击打开下载页）。
        # 置于三个按钮上方，显示时不挤压下方内容布局
        self._update_label = ClickableLabel(
            "",
            normal_color=None,
            hover_color="#228be6",
            underline_on_hover=True,
        )
        self._update_label.setAlignment(Qt.AlignCenter)
        self._update_label.setStyleSheet("font-size: 11px;")
        self._update_label.setVisible(False)
        self._update_label.set_click_callback(self._on_update_clicked)
        main_layout.addWidget(self._update_label)

        main_layout.addLayout(bottom_layout)

        # 版本和缓存信息
        cache_size = Config.get_cache_size()
        cache_size_str = self._format_size(cache_size)
        cache_color = self._get_cache_color(cache_size)
        version_text = f"{I18n.tr('about_version', version=Config.APP_VERSION)}  |  <span style='color: {cache_color};'>{I18n.tr('cache_size', size=cache_size_str)}</span>"
        self._version_label = QLabel(version_text)
        self._version_label.setAlignment(Qt.AlignCenter)
        self._version_label.setStyleSheet("color: #999; font-size: 11px;")
        self._version_label.setTextFormat(Qt.RichText)  # 支持HTML格式
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
    
    def on_open_settings(self):
        """打开设置对话框（非模态；缓存被清空时同步刷新主界面缓存占用）"""
        self._settings_dialog = SettingsDialog(self)
        self._settings_dialog.cache_changed.connect(self._refresh_cache_size)
        self._settings_dialog.announcements_toggled.connect(self._on_announcements_toggled)
        self._settings_dialog.show()
    
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
                
                # 底部按钮（直接用创建时保存的引用，不依赖布局索引）
                if self.manage_cache_btn:
                    self.manage_cache_btn.setText(I18n.tr('settings'))
                if self.about_btn:
                    self.about_btn.setText(I18n.tr('about'))

                # 公告入口（语言切换后按当前语言刷新预览，持久显示）
                self._refresh_announcement_entry()

                # 版本和缓存信息
                if self._version_label:
                    cache_size = Config.get_cache_size()
                    cache_size_str = self._format_size(cache_size)
                    cache_color = self._get_cache_color(cache_size)
                    version_text = f"{I18n.tr('about_version', version=Config.APP_VERSION)}  |  <span style='color: {cache_color};'>{I18n.tr('cache_size', size=cache_size_str)}</span>"
                    self._version_label.setText(version_text)

                # 发现新版本提醒（语言切换后按当前语言重设文案，可见性保持不变）
                if self._update_label and self._update_version:
                    self._update_label.setText(
                        I18n.tr('settings_update_available', version=self._update_version)
                    )

    # ---------- 自动检查更新 ----------

    def _start_auto_update_check(self):
        """自动检查更新：开启后程序启动时后台静默检查一次，有新版本以主界面小字提醒"""
        if not Config.ENABLE_CHECK_UPDATE or not UserConfig.get_auto_check_update():
            return

        def _fetch():
            latest, _ = fetch_latest_version()
            self._update_fetched.emit(latest or "")

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_update_fetched(self, version: str):
        """后台检查结果：仅当确实存在新版本时显示主界面小字提醒（静默失败）"""
        if not version:
            return
        if not self._is_newer(version, Config.APP_VERSION):
            return
        self._update_version = version
        self._update_label.setText(I18n.tr('settings_update_available', version=version))
        self._update_label.setVisible(True)

    @staticmethod
    def _is_newer(latest: str, current: str) -> bool:
        """四段式版本号比较（R 前缀），latest > current 返回 True"""
        latest_match = re.search(r'R(\d+)\.(\d+)\.(\d+)\.(\d+)', latest)
        current_match = re.search(r'R(\d+)\.(\d+)\.(\d+)\.(\d+)', current)
        if not latest_match or not current_match:
            return False
        return tuple(map(int, latest_match.groups())) > tuple(map(int, current_match.groups()))

    # ---------- 公告 ----------

    def _start_announcement_check(self):
        """公告拉取：开启接收时先恢复已接收公告的持久入口，再后台静默拉取一次新公告"""
        if not UserConfig.get_receive_announcements():
            return
        self._restore_announcement_entry()

        def _fetch():
            version, text = fetch_announcement()
            self._announcement_fetched.emit(version or "", text or "")

        threading.Thread(target=_fetch, daemon=True).start()

    def _restore_announcement_entry(self):
        """从 config.json 恢复已接收公告（入口持久显示，不重播胶囊）。

        若只有版本号而无正文（旧版本逻辑或异常中断的残留），视为未接收：
        清空版本号，让本次拉取把服务器公告当新公告处理并补齐正文。
        """
        version = UserConfig.get_last_announcement()
        text = UserConfig.get_last_announcement_text()
        if version and not text:
            UserConfig.set_last_announcement("")
            return
        if version and text:
            self._announcement = (version, text)
            self._refresh_announcement_entry()

    def _on_announcements_toggled(self, enabled: bool):
        """「接收推送公告」开关：关闭即隐藏主界面公告入口；重新开启时恢复已接收公告"""
        if enabled:
            self._restore_announcement_entry()
        else:
            self._announcement = None
            self._announce_entry.clear_announcement()

    def _on_announcement_fetched(self, version: str, text: str):
        """公告拉取结果：仅当版本新于已记录值时播放胶囊并显示主界面持久入口（静默失败）"""
        if not version or not text:
            return
        if not is_newer(version, UserConfig.get_last_announcement()):
            return
        self._announcement = (version, text)
        # 「新公告通过胶囊栏显示」关闭时跳过胶囊播放，公告仅在主界面入口显示
        if UserConfig.get_capsule_announcements():
            self._announce_capsule.show_announcement(text)
        UserConfig.set_last_announcement(version)
        UserConfig.set_last_announcement_text(text)
        self._refresh_announcement_entry()

    def _refresh_announcement_entry(self):
        """按当前语言刷新公告入口预览（持久显示，直到收到新公告才更新）"""
        if not self._announcement:
            return
        version, text = self._announcement
        prefix = f"{I18n.tr('announcement')}：" if I18n.get_language() == "zh_CN" \
            else f"{I18n.tr('announcement')}: "
        self._announce_entry.set_announcement(prefix + text)

    def _on_announcement_clicked(self):
        """点击公告入口：弹出完整公告详情窗口"""
        if not self._announcement:
            return
        version, text = self._announcement
        dialog = AnnouncementDialog(text, self)
        dialog.exec()

    def _on_update_clicked(self, event):
        """点击提醒标签：打开下载向导页（按语言选中/英文版）"""
        QDesktopServices.openUrl(QUrl(Config.get_download_wizard_url(I18n.get_language())))
    
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
            cache_color = self._get_cache_color(cache_size)
            version_text = f"{I18n.tr('about_version', version=Config.APP_VERSION)}  |  <span style='color: {cache_color};'>{I18n.tr('cache_size', size=cache_size_str)}</span>"
            self._version_label.setText(version_text)

    def _show_and_refresh_cache(self):
        """刷新缓存大小后显示主窗口"""
        # 刷新缓存大小显示
        self._refresh_cache_size()

        # 显示主窗口
        self.show()

    def activate_visible_window(self):
        """将当前真正可见的活动窗口唤起（同步界面优先，否则主窗口）。

        单实例唤出用：同步界面打开时主窗口已隐藏，若只唤起主窗口会在
        同步界面之上再次呼出空白主窗口，故须优先唤起同步窗口。
        """
        sync = self._sync_window
        if sync is not None:
            try:
                if sync.isVisible():
                    sync.showNormal()  # 若最小化则还原
                    sync.raise_()
                    sync.activateWindow()
                    return
            except RuntimeError:
                pass  # 同步窗口的 C++ 对象已销毁
        self.showNormal()  # 若最小化则还原
        self.raise_()
        self.activateWindow()