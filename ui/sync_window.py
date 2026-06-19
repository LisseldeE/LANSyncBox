# -*- coding: utf-8 -*-
"""
LANSyncBox 同步状态界面 - 横向布局（紧凑版）
"""

import os
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QGroupBox, QCheckBox,
    QHeaderView, QProgressBar, QDialog, QSplitter, QFrame, QApplication
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QCursor

from config import STYLESHEET, COLORS, DEFAULT_PORT
from ui.widgets import AnimatedButton
from network.server import SyncServer
from network.client import SyncClient
from sync.file_watcher import FileWatcher
from room.room_manager import RoomManager
from i18n import I18n


class SyncWindow(QMainWindow):
    """同步状态界面 - 横向布局（紧凑）"""
    
    def __init__(self, mode='host', room_code='', password='', sync_folder='',
                 host_address='', allow_peer_sync=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"LANSyncBox - {room_code}")
        # 横向窗口：宽而矮（紧凑尺寸）
        self.setMinimumSize(500, 280)
        self.resize(600, 320)
        self.setStyleSheet(STYLESHEET)
        
        self.mode = mode
        self.room_code = room_code
        self.password = password
        self.sync_folder = sync_folder
        self.host_address = host_address
        self.allow_peer_sync = allow_peer_sync
        
        # 网络组件
        self.server: SyncServer = None
        self.client: SyncClient = None
        
        # 文件监控
        self.file_watcher: FileWatcher = None
        
        # 隐藏状态
        self.hide_from_others = False
        
        # 同步文件记录
        self.sync_records = []
        
        self._init_ui()
        self._start_sync()
    
    def changeEvent(self, event):
        """窗口状态改变事件"""
        if event.type() == event.WindowStateChange:
            if self.windowState() & Qt.WindowMinimized:
                # 最小化时隐藏窗口（不显示左下角浮窗）
                event.ignore()
                self.hide()
                return
        super().changeEvent(event)
    
    def _init_ui(self):
        """初始化UI - 横向布局"""
        # 中央widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 主布局 - 横向
        main_layout = QHBoxLayout(central_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # 左侧面板 - 状态和选项
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setSpacing(6)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        # 状态信息
        mode_text = I18n.t('sync_role_host') if self.mode == 'host' else I18n.t('sync_role_client')
        self.mode_label = QLabel(f"{I18n.t('common_mode')}：{mode_text}")
        self.mode_label.setStyleSheet(f"font-weight: bold; color: {COLORS['primary']}; font-size: 12px;")
        left_layout.addWidget(self.mode_label)
        
        self.room_label = QLabel(f"{I18n.t('sync_room')}{self.room_code}")
        self.room_label.setStyleSheet("font-size: 11px;")
        self.room_label.setCursor(QCursor(Qt.PointingHandCursor))
        self.room_label.setToolTip(I18n.t('sync_copy_room'))
        self.room_label.mousePressEvent = self._on_room_label_clicked
        left_layout.addWidget(self.room_label)
        
        self.status_label = QLabel(f"{I18n.t('common_status')}：{I18n.t('common_connecting')}")
        self.status_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 11px;")
        self.status_label.setWordWrap(True)
        left_layout.addWidget(self.status_label)
        
        # 在线连接端（仅主机端显示）
        if self.mode == 'host':
            self.clients_count_label = QLabel(f"{I18n.t('common_online')}：0")
            self.clients_count_label.setStyleSheet("font-size: 11px;")
            left_layout.addWidget(self.clients_count_label)
        
        left_layout.addSpacing(8)
        
        # 选项区域
        options_frame = QFrame()
        options_frame.setStyleSheet(f"QFrame {{ border: 1px solid {COLORS['border']}; border-radius: 4px; padding: 5px; }}")
        options_layout = QVBoxLayout(options_frame)
        options_layout.setSpacing(5)
        
        if self.mode == 'client':
            # 隐藏按钮 - 默认隐藏，验证成功后根据主机设置决定是否显示
            self.hide_checkbox = QCheckBox(I18n.t('sync_hide_files'))
            self.hide_checkbox.stateChanged.connect(self._on_hide_changed)
            self.hide_checkbox.hide()  # 默认隐藏
            options_layout.addWidget(self.hide_checkbox)
            
            # 全量同步按钮
            self.sync_btn = AnimatedButton(I18n.t('sync_full_sync'))
            self.sync_btn.setObjectName("primaryBtn")
            self.sync_btn.setMinimumHeight(30)
            self.sync_btn.clicked.connect(self._request_full_sync)
            options_layout.addWidget(self.sync_btn)
        else:
            peer_status = I18n.t('sync_peer_on') if self.allow_peer_sync else I18n.t('sync_peer_off')
            self.peer_sync_label = QLabel(
                f"{I18n.t('sync_peer_sync')}：{peer_status}"
            )
            self.peer_sync_label.setStyleSheet("font-size: 10px;")
            options_layout.addWidget(self.peer_sync_label)
        
        left_layout.addWidget(options_frame)
        left_layout.addStretch()
        
        # 退出按钮
        self.exit_btn = AnimatedButton(I18n.t('sync_exit'))
        self.exit_btn.setObjectName("dangerBtn")
        self.exit_btn.setMinimumHeight(30)
        self.exit_btn.clicked.connect(self.close)
        left_layout.addWidget(self.exit_btn)
        
        # 设置左侧面板最小宽度
        left_panel.setMinimumWidth(120)
        left_panel.setMaximumWidth(150)
        main_layout.addWidget(left_panel)
        
        # 右侧面板 - 同步记录表格
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(5)
        
        records_label = QLabel(I18n.t('sync_records'))
        records_label.setStyleSheet(f"font-weight: bold; color: {COLORS['text_primary']}; font-size: 11px;")
        right_layout.addWidget(records_label)
        
        self.records_table = QTableWidget()
        self.records_table.setColumnCount(4)
        self.records_table.setHorizontalHeaderLabels([
            I18n.t('sync_time'),
            I18n.t('sync_file_name'),
            I18n.t('sync_source'),
            I18n.t('sync_action')
        ])
        
        # 设置表格样式
        header = self.records_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        
        self.records_table.setColumnWidth(0, 60)
        self.records_table.setColumnWidth(2, 70)
        self.records_table.setColumnWidth(3, 50)
        
        self.records_table.setAlternatingRowColors(True)
        self.records_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.records_table.setSelectionBehavior(QTableWidget.SelectRows)
        
        right_layout.addWidget(self.records_table)
        
        main_layout.addWidget(right_panel, stretch=1)
    
    def _on_room_label_clicked(self, event):
        """点击房间号标签复制房间号"""
        clipboard = QApplication.clipboard()
        clipboard.setText(self.room_code)
        # 不显示弹窗，只打印日志
        self._add_log(f"房间号 {self.room_code} 已复制到剪贴板")
    
    def _start_sync(self):
        """开始同步"""
        if self.mode == 'host':
            self._start_host()
        else:
            self._start_client()
    
    def _start_host(self):
        """启动主机端"""
        self.server = SyncServer(self)
        self.server.client_connected.connect(self._on_client_connected)
        self.server.client_disconnected.connect(self._on_client_disconnected)
        self.server.file_received.connect(self._on_file_received)
        self.server.delete_received.connect(self._on_delete_received)
        self.server.log_message.connect(self._add_log)
        self.server.error_occurred.connect(self._show_error)
        
        success = self.server.start(
            self.room_code,
            self.password,
            self.sync_folder,
            DEFAULT_PORT,
            self.allow_peer_sync
        )
        
        if success:
            self.status_label.setText(f"{I18n.t('common_status')}：{I18n.t('common_connected')}")
            self.status_label.setStyleSheet(f"color: {COLORS['success']}; font-size: 11px;")
            self._start_file_watcher()
        else:
            self.status_label.setText(f"{I18n.t('common_status')}：{I18n.t('common_error')}")
            self.status_label.setStyleSheet(f"color: {COLORS['danger']}; font-size: 11px;")
    
    def _start_client(self):
        """启动连接端"""
        self.client = SyncClient(self)
        self.client.connected.connect(self._on_connected)
        self.client.disconnected.connect(self._on_disconnected)
        self.client.auth_success.connect(self._on_auth_success)
        self.client.auth_failed.connect(self._on_auth_failed)
        self.client.file_received.connect(self._on_file_received_client)
        self.client.delete_received.connect(self._on_delete_received_client)
        self.client.log_message.connect(self._add_log)
        self.client.error_occurred.connect(self._show_error)
        
        success = self.client.connect_to_server(
            self.host_address,
            DEFAULT_PORT,
            self.room_code,
            self.password,
            self.sync_folder
        )
        
        if not success:
            self.status_label.setText(f"{I18n.t('common_status')}：{I18n.t('common_error')}")
            self.status_label.setStyleSheet(f"color: {COLORS['danger']}; font-size: 11px;")
    
    def _start_file_watcher(self):
        """启动文件监控"""
        self.file_watcher = FileWatcher(self)
        self.file_watcher.file_created.connect(self._on_file_created)
        self.file_watcher.file_modified.connect(self._on_file_modified)
        self.file_watcher.file_deleted.connect(self._on_file_deleted)
        self.file_watcher.directory_created.connect(self._on_directory_created)
        self.file_watcher.error_occurred.connect(self._show_error)
        self.file_watcher.start(self.sync_folder)
    
    def _on_client_connected(self, client_id):
        self._update_clients_count()
        self._add_record(f"{I18n.t('common_connect')} {client_id}", client_id, I18n.t('common_connect'))
    
    def _on_client_disconnected(self, client_id):
        self._update_clients_count()
        self._add_record(f"{I18n.t('common_disconnected')} {client_id}", client_id, I18n.t('common_disconnected'))
    
    def _update_clients_count(self):
        if self.server:
            clients = self.server.get_client_list()
            self.clients_count_label.setText(f"{I18n.t('common_online')}：{len(clients)}")
    
    def _on_connected(self):
        self.status_label.setText(f"{I18n.t('common_status')}：...")
    
    def _on_disconnected(self):
        self.status_label.setText(f"{I18n.t('common_status')}：{I18n.t('common_disconnected')}")
        self.status_label.setStyleSheet(f"color: {COLORS['danger']}; font-size: 11px;")
        if self.file_watcher:
            self.file_watcher.stop()
        # 不使用弹窗，改为日志记录
        self._add_log(I18n.t('sync_client_disconnected'))
    
    def _on_auth_success(self):
        self.status_label.setText(f"{I18n.t('common_status')}：{I18n.t('common_connected')}")
        self.status_label.setStyleSheet(f"color: {COLORS['success']}; font-size: 11px;")
        
        # 根据主机端设置决定是否显示隐藏按钮
        if self.mode == 'client' and self.client:
            if self.client.allow_peer_sync:
                self.hide_checkbox.show()  # 主机允许互相同步，显示隐藏按钮
            else:
                self.hide_checkbox.hide()  # 主机不允许互相同步，隐藏按钮
        
        self._start_file_watcher()
        self._request_full_sync()
    
    def _on_auth_failed(self, message):
        self.status_label.setText(f"{I18n.t('common_status')}：{I18n.t('common_error')}")
        self.status_label.setStyleSheet(f"color: {COLORS['danger']}; font-size: 11px;")
        self._add_log(f"{I18n.t('common_error')}: {message}")
        
        # 如果是密码错误，弹出密码输入对话框
        if "密码错误" in message or "Password" in message:
            self._show_password_dialog()
        else:
            self.close()
    
    def _show_password_dialog(self):
        """显示密码输入对话框"""
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton
        
        dialog = QDialog(self)
        dialog.setWindowTitle(I18n.t('join_password_label').rstrip('：'))
        dialog.setMinimumWidth(250)
        dialog.setStyleSheet(STYLESHEET)
        
        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)
        
        label = QLabel(I18n.t('join_password_placeholder'))
        layout.addWidget(label)
        
        password_input = QLineEdit()
        password_input.setEchoMode(QLineEdit.Password)
        password_input.setPlaceholderText(I18n.t('join_password_label'))
        layout.addWidget(password_input)
        
        btn_layout = QHBoxLayout()
        
        cancel_btn = QPushButton(I18n.t('create_cancel'))
        cancel_btn.setObjectName("dangerBtn")
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(cancel_btn)
        
        confirm_btn = QPushButton(I18n.t('common_sync'))
        confirm_btn.setObjectName("successBtn")
        confirm_btn.clicked.connect(dialog.accept)
        btn_layout.addWidget(confirm_btn)
        
        layout.addLayout(btn_layout)
        dialog.setLayout(layout)
        
        if dialog.exec_() == QDialog.Accepted:
            password = password_input.text()
            if password:
                # 重新连接
                self._add_log(f"重新连接，使用新密码")
                self.client.disconnect()
                # 重新连接使用新密码
                self.client.connect_to_server(
                    self.host_address,
                    DEFAULT_PORT,
                    self.room_code,
                    password,
                    self.sync_folder
                )
            else:
                self.close()
        else:
            self.close()
    
    def _on_file_created(self, filepath):
        print(f"[DEBUG] _on_file_created called: {filepath}")
        # 文件监控器已经处理了重命名逻辑，这里直接同步文件
        # 等待文件完全写入后再同步
        def do_sync():
            # 检查文件是否存在
            if os.path.exists(filepath):
                self._sync_file(filepath)
            else:
                print(f"[DEBUG] File not exists, skip: {filepath}")
        
        QTimer.singleShot(200, do_sync)
    
    def _on_file_modified(self, filepath):
        print(f"[DEBUG] _on_file_modified called: {filepath}")
        self._sync_file(filepath)
    
    def _on_directory_created(self, dirpath):
        """处理目录创建事件"""
        print(f"[DEBUG] _on_directory_created called: {dirpath}")
        # 同步目录创建
        if self.mode == 'host':
            # 主机端广播目录创建到所有客户端
            self.server.broadcast_dir_create(dirpath)
        else:
            # 连接端发送目录创建到主机
            self.client.send_dir_create(dirpath)
        self._add_record(dirpath, "本机", I18n.t('common_folder'))
    
    def _on_file_deleted(self, filepath):
        if self.mode == 'host':
            self.server.broadcast_delete(filepath)
        else:
            self.client.send_delete(filepath)
        self._add_record(filepath, "本机", I18n.t('common_delete'))
    
    def _sync_file(self, filepath):
        """同步文件"""
        print(f"[DEBUG] _sync_file called: {filepath}, mode: {self.mode}")
        
        # 检查文件是否存在
        if not os.path.exists(filepath):
            print(f"[DEBUG] File does not exist: {filepath}")
            return
        
        # 检查是否是文件（不是目录）
        if not os.path.isfile(filepath):
            print(f"[DEBUG] Not a file: {filepath}")
            return
        
        if self.mode == 'host':
            # 主机端广播文件到所有客户端
            print(f"[DEBUG] Broadcasting file from host: {filepath}")
            self.server.broadcast_file(filepath, exclude_client=None, hide_from_others=False)
        else:
            # 连接端发送文件到主机
            print(f"[DEBUG] Sending file from client: {filepath}")
            self.client.send_file(filepath, hide_from_others=self.hide_from_others)
        self._add_record(filepath, "本机", I18n.t('common_send'))
    
    def _on_file_received(self, client_id, filename, hide_from_others):
        filepath = os.path.join(self.sync_folder, filename)
        # 添加到忽略列表，避免接收后的 modified 事件触发同步
        if self.file_watcher:
            self.file_watcher.add_ignore(filepath, duration=2.0)
        self._add_record(filename, client_id, I18n.t('common_receive'))
        # 注意：文件转发逻辑已在 server._handle_file_receive 中处理，此处不再重复转发
    
    def _on_delete_received(self, client_id, filename):
        self._add_record(filename, client_id, I18n.t('common_delete'))
    
    def _on_file_received_client(self, filename, size):
        filepath = os.path.join(self.sync_folder, filename)
        # 添加到忽略列表，避免接收后的 modified 事件触发同步
        if self.file_watcher:
            self.file_watcher.add_ignore(filepath, duration=2.0)
        self._add_record(filename, I18n.t('sync_role_host'), I18n.t('common_receive'))
    
    def _on_delete_received_client(self, filename):
        self._add_record(filename, I18n.t('sync_role_host'), I18n.t('common_delete'))
    
    def _on_hide_changed(self, state):
        self.hide_from_others = state == Qt.Checked
        if self.client:
            self.client.update_hide_status(self.hide_from_others)
    
    def _request_full_sync(self):
        if self.client and self.client.authenticated:
            self.client.request_full_sync()
            self._add_record(I18n.t('sync_full_sync'), "本机", I18n.t('common_sync'))
    
    def _add_record(self, filename, source, operation):
        from datetime import datetime
        time_str = datetime.now().strftime("%H:%M:%S")
        
        row_count = self.records_table.rowCount()
        self.records_table.insertRow(row_count)
        
        self.records_table.setItem(row_count, 0, QTableWidgetItem(time_str))
        self.records_table.setItem(row_count, 1, QTableWidgetItem(filename))
        self.records_table.setItem(row_count, 2, QTableWidgetItem(source))
        self.records_table.setItem(row_count, 3, QTableWidgetItem(operation))
        
        if operation == I18n.t('common_delete'):
            self.records_table.item(row_count, 3).setBackground(QColor(COLORS['danger']))
            self.records_table.item(row_count, 3).setForeground(QColor('white'))
        elif operation == I18n.t('common_receive'):
            self.records_table.item(row_count, 3).setBackground(QColor(COLORS['success']))
            self.records_table.item(row_count, 3).setForeground(QColor('white'))
    
    def _add_log(self, message):
        print(f"[LOG] {message}")
    
    def _show_error(self, message):
        print(f"[ERROR] {message}")
    
    def _on_exit(self):
        """清理资源（不关闭窗口）"""
        if self.file_watcher:
            self.file_watcher.stop()
        
        if self.mode == 'host':
            if self.server:
                self.server.stop()
            room_manager = RoomManager()
            room_manager.close_room(self.room_code)
        else:
            if self.client:
                self.client.disconnect()
    
    def closeEvent(self, event):
        """关闭窗口事件"""
        # 从主窗口的同步窗口列表中移除
        if self.parent() and hasattr(self.parent(), 'sync_windows'):
            if self in self.parent().sync_windows:
                self.parent().sync_windows.remove(self)
            if hasattr(self.parent(), '_update_tray_menu'):
                self.parent()._update_tray_menu()
        
        # 清理资源
        self._on_exit()
        
        # 显示主窗口
        if self.parent():
            self.parent().show()
            self.parent().activateWindow()
            self.parent().raise_()
        
        event.accept()