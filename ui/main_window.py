# -*- coding: utf-8 -*-
"""
LANSyncBox 主界面 - 纵向布局（紧凑版）
"""

import sys
import os
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QMessageBox, QApplication, QFileDialog, QDialog, QSystemTrayIcon, QMenu, QAction
)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QIcon

from config import STYLESHEET, APP_NAME, APP_VERSION, APP_ID, COLORS
from ui.widgets import AnimatedButton
from i18n import I18n

# 延迟导入：这些模块较重，在需要时才导入
# from ui.create_room_dialog import CreateRoomDialog
# from ui.join_room_dialog import JoinRoomDialog
# from ui.sync_window import SyncWindow


class MainWindow(QMainWindow):
    """主界面 - 纵向布局（紧凑）"""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        # 纵向窗口：窄而高（紧凑尺寸）
        self.setMinimumSize(240, 300)
        self.resize(280, 360)
        
        # 应用样式
        self.setStyleSheet(STYLESHEET)
        
        # 同步窗口列表
        self.sync_windows = []
        
        # 系统托盘
        self._init_tray_icon()
        
        self._init_ui()
    
    def _init_tray_icon(self):
        """初始化系统托盘图标"""
        # 获取图标路径
        icon_path = os.path.join(os.path.dirname(__file__), '..', 'icon.ico')
        
        # 创建托盘图标
        self.tray_icon = QSystemTrayIcon(self)
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            # 使用默认图标
            self.tray_icon.setIcon(self.style().standardIcon(self.style().SP_ComputerIcon))
        
        # 创建托盘菜单
        self.tray_menu = QMenu()
        
        # 显示主窗口
        show_main_action = QAction(I18n.t('main_create'), self)
        show_main_action.triggered.connect(self._show_window)
        self.tray_menu.addAction(show_main_action)
        
        # 分隔线
        self.tray_menu.addSeparator()
        
        # 退出
        quit_action = QAction(I18n.t('main_exit'), self)
        quit_action.triggered.connect(self._quit_app)
        self.tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()
    
    def _update_tray_menu(self):
        """更新托盘菜单，添加同步窗口选项"""
        # 清除旧菜单
        self.tray_menu.clear()
        
        # 如果有同步窗口，只显示同步窗口选项，不显示主窗口
        if self.sync_windows:
            for sync_win in self.sync_windows:
                if sync_win.room_code:
                    action = QAction(f"{I18n.t('sync_room')}{sync_win.room_code}", self)
                    action.triggered.connect(lambda checked, w=sync_win: self._show_sync_window(w))
                    self.tray_menu.addAction(action)
        else:
            # 没有同步窗口时，显示主窗口选项
            show_main_action = QAction(I18n.t('main_create'), self)
            show_main_action.triggered.connect(self._show_window)
            self.tray_menu.addAction(show_main_action)
        
        # 分隔线
        self.tray_menu.addSeparator()
        
        # 退出
        quit_action = QAction(I18n.t('main_exit'), self)
        quit_action.triggered.connect(self._quit_app)
        self.tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(self.tray_menu)
    
    def _show_sync_window(self, sync_win):
        """显示指定的同步窗口"""
        # 清除最小化状态，然后显示窗口
        sync_win.setWindowState(Qt.WindowNoState)
        sync_win.show()
        sync_win.activateWindow()
        sync_win.raise_()
    
    def _on_tray_activated(self, reason):
        """托盘图标激活事件"""
        if reason == QSystemTrayIcon.DoubleClick:
            # 如果有同步窗口，显示第一个同步窗口
            if self.sync_windows:
                self._show_sync_window(self.sync_windows[0])
            else:
                self._show_window()
    
    def _show_window(self):
        """显示窗口"""
        self.show()
        self.activateWindow()
        self.raise_()
    
    def _quit_app(self):
        """退出应用"""
        # 关闭所有同步窗口
        for sync_win in self.sync_windows:
            sync_win.close()
        self.sync_windows.clear()
        
        self.tray_icon.hide()
        self.close()
    
    def changeEvent(self, event):
        """窗口状态改变事件"""
        if event.type() == event.WindowStateChange:
            if self.windowState() & Qt.WindowMinimized:
                # 最小化时隐藏到托盘
                event.ignore()
                self.hide()
                return
        super().changeEvent(event)
    
    def closeEvent(self, event):
        """关闭窗口事件"""
        # 关闭所有同步窗口
        for sync_win in self.sync_windows:
            sync_win.close()
        self.sync_windows.clear()
        
        if hasattr(self, 'tray_icon') and self.tray_icon:
            self.tray_icon.hide()
        event.accept()
    
    def _init_ui(self):
        """初始化UI"""
        # 中央widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 主布局 - 纵向
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(20, 20, 20, 20)
        
        # 标题区域
        title_label = QLabel(APP_NAME)
        title_label.setStyleSheet(f"""
            QLabel {{
                font-size: 18px;
                font-weight: bold;
                color: {COLORS['primary']};
            }}
        """)
        title_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title_label)
        
        # 版本信息
        version_label = QLabel(APP_VERSION)
        version_label.setStyleSheet(f"""
            QLabel {{
                font-size: 10px;
                color: {COLORS['text_secondary']};
            }}
        """)
        version_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(version_label)
        
        main_layout.addSpacing(15)
        
        # 按钮区域 - 纵向排列
        # 创建连接按钮（主机端）
        self.create_btn = AnimatedButton(I18n.t('main_create'))
        self.create_btn.setObjectName("primaryBtn")
        self.create_btn.setMinimumHeight(38)
        self.create_btn.clicked.connect(self._on_create_clicked)
        main_layout.addWidget(self.create_btn)
        
        main_layout.addSpacing(8)
        
        # 加入连接按钮（连接端）
        self.join_btn = AnimatedButton(I18n.t('main_join'))
        self.join_btn.setObjectName("successBtn")
        self.join_btn.setMinimumHeight(38)
        self.join_btn.clicked.connect(self._on_join_clicked)
        main_layout.addWidget(self.join_btn)
        
        main_layout.addStretch()
        
        # 底部按钮区域
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(8)
        
        # 语言切换按钮
        self.lang_btn = QPushButton("EN" if I18n.get_lang() == 'zh' else "中文")
        self.lang_btn.setMinimumWidth(55)
        self.lang_btn.setFixedHeight(32)
        self.lang_btn.setStyleSheet("""
            QPushButton {
                background-color: #e9ecef;
                color: #495057;
                border: 1px solid #ced4da;
                border-radius: 6px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #dee2e6;
            }
        """)
        self.lang_btn.clicked.connect(self._toggle_lang)
        bottom_layout.addWidget(self.lang_btn)
        
        bottom_layout.addStretch()
        
        # 关于按钮
        self.about_btn = QPushButton(I18n.t('main_about'))
        self.about_btn.setMinimumWidth(55)
        self.about_btn.setFixedHeight(32)
        self.about_btn.setStyleSheet("""
            QPushButton {
                background-color: #e9ecef;
                color: #495057;
                border: 1px solid #ced4da;
                border-radius: 6px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #dee2e6;
            }
        """)
        self.about_btn.clicked.connect(self._show_about)
        bottom_layout.addWidget(self.about_btn)
        
        main_layout.addLayout(bottom_layout)
    
    def _toggle_lang(self):
        """切换语言"""
        current = I18n.get_lang()
        new_lang = 'en' if current == 'zh' else 'zh'
        I18n.set_lang(new_lang)
        
        # 更新按钮文本
        self.lang_btn.setText("EN" if new_lang == 'zh' else "中文")
        self.about_btn.setText(I18n.t('main_about'))
        
        # 更新界面文本
        self.create_btn.setText(I18n.t('main_create'))
        self.join_btn.setText(I18n.t('main_join'))
        
        # 更新托盘菜单
        self._update_tray_menu()
    
    def _on_create_clicked(self):
        """创建连接按钮点击"""
        # 延迟导入重量级模块
        from ui.create_room_dialog import CreateRoomDialog
        from ui.sync_window import SyncWindow
        
        dialog = CreateRoomDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            # 获取创建的房间信息
            room_code = dialog.room_code
            password = dialog.password
            sync_folder = dialog.sync_folder
            allow_peer_sync = dialog.allow_peer_sync
            
            # 打开同步状态界面（主机模式）
            sync_window = SyncWindow(
                mode='host',
                room_code=room_code,
                password=password,
                sync_folder=sync_folder,
                allow_peer_sync=allow_peer_sync,
                parent=self
            )
            self.sync_windows.append(sync_window)
            self._update_tray_menu()
            sync_window.show()
            self.hide()
    
    def _on_join_clicked(self):
        """加入连接按钮点击"""
        # 延迟导入重量级模块
        from ui.join_room_dialog import JoinRoomDialog
        from ui.sync_window import SyncWindow
        
        dialog = JoinRoomDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            # 获取加入的房间信息
            room_code = dialog.room_code
            password = dialog.password
            sync_folder = dialog.sync_folder
            host_address = dialog.host_address
            
            # 打开同步状态界面（连接端模式）
            sync_window = SyncWindow(
                mode='client',
                room_code=room_code,
                password=password,
                sync_folder=sync_folder,
                host_address=host_address,
                parent=self
            )
            self.sync_windows.append(sync_window)
            self._update_tray_menu()
            sync_window.show()
            self.hide()
    
    def _show_about(self):
        """显示关于对话框"""
        from ui.about_dialog import AboutDialog
        dialog = AboutDialog(self)
        dialog.exec_()