# -*- coding: utf-8 -*-
"""
LANSyncBox 同步状态界面 - 横向布局（紧凑版）
"""

import os
import time
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
from sync import SyncEngine, OpType
from room.room_manager import RoomManager
from i18n import I18n


class SyncWindow(QMainWindow):
    """同步状态界面 - 横向布局（紧凑）"""
    
    def __init__(self, mode='host', room_code='', password='', sync_folder='',
                 host_address='', parent=None):
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
        
        # 网络组件
        self.server: SyncServer = None
        self.client: SyncClient = None
        
        # 同步引擎（新架构）
        self.sync_engine: SyncEngine = None
        
        # 文件监控（使用 watchdog，但事件传递给 SyncEngine）
        self._file_observer = None
        
        # 隐藏状态
        self.hide_from_others = False
        
        # 同步文件记录
        self.sync_records = []
        
        # 后台线程控制
        self._sync_threads = []  # 存储活跃的后台同步线程
        self._stop_sync = False  # 停止同步标志
        
        # 传输进度跟踪
        self._transfer_rows = {}  # 文件名 -> 行号映射
        
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
        
        # 传输状态标签（显示当前传输的文件名）
        self.transfer_status_label = QLabel("")
        self.transfer_status_label.setStyleSheet(f"color: {COLORS['primary']}; font-size: 11px; font-weight: bold;")
        self.transfer_status_label.setWordWrap(True)
        self.transfer_status_label.hide()  # 默认隐藏
        left_layout.addWidget(self.transfer_status_label)
        
        # 进度条（用于大文件传输）
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumHeight(20)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #ced4da;
                border-radius: 4px;
                text-align: center;
                background-color: #e9ecef;
                font-size: 10px;
            }
            QProgressBar::chunk {
                background-color: #51cf66;
                border-radius: 3px;
            }
        """)
        self.progress_bar.hide()  # 默认隐藏
        left_layout.addWidget(self.progress_bar)
        
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
        self.records_table.setColumnCount(5)
        self.records_table.setHorizontalHeaderLabels([
            I18n.t('sync_time'),
            I18n.t('sync_file_name'),
            I18n.t('sync_source'),
            I18n.t('sync_action'),
            I18n.t('sync_progress')  # 新增进度列
        ])
        
        # 设置表格样式
        header = self.records_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)  # 进度列固定
        
        self.records_table.setColumnWidth(0, 60)
        self.records_table.setColumnWidth(2, 70)
        self.records_table.setColumnWidth(3, 60)  # 操作列宽度调整为60
        self.records_table.setColumnWidth(4, 150)  # 进度列宽度150
        
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
        self.server.transfer_started.connect(self._on_transfer_started)
        self.server.transfer_progress.connect(self._on_transfer_progress)
        self.server.transfer_finished.connect(self._on_transfer_finished)
        self.server.log_message.connect(self._add_log)
        self.server.error_occurred.connect(self._show_error)
        
        success = self.server.start(
            self.room_code,
            self.password,
            self.sync_folder,
            DEFAULT_PORT
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
        self.client.file_receiving.connect(self._on_file_receiving)  # 新增：提前添加到忽略列表
        self.client.delete_received.connect(self._on_delete_received_client)
        self.client.transfer_started.connect(self._on_transfer_started)
        self.client.transfer_progress.connect(self._on_transfer_progress)
        self.client.transfer_finished.connect(self._on_transfer_finished)
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
        """启动文件监控（使用新架构）"""
        # 1. 初始化同步引擎
        self.sync_engine = SyncEngine(self)
        self.sync_engine.initialize(self.sync_folder, f"{self.mode}_{self.room_code}")
        
        # 连接同步引擎信号
        self.sync_engine.operation_ready.connect(self._on_operation_ready)
        self.sync_engine.conflict_detected.connect(self._on_conflict_detected)
        self.sync_engine.error_occurred.connect(self._show_error)
        
        # 设置网络发送回调
        self.sync_engine.set_send_operation_callback(self._send_operation_to_network)
        
        # 2. 启动文件监控（使用 watchdog）
        self._start_file_observer()
    
    def _start_file_observer(self):
        """启动 watchdog 文件监控"""
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileSystemEvent
        
        class EventHandler(FileSystemEventHandler):
            def __init__(self, window):
                self.window = window
            
            def on_created(self, event):
                if not event.is_directory:
                    rel_path = os.path.relpath(event.src_path, self.window.sync_folder)
                    self.window.sync_engine.on_file_event('created', rel_path)
                else:
                    rel_path = os.path.relpath(event.src_path, self.window.sync_folder)
                    self.window.sync_engine.on_file_event('created', rel_path, is_dir=True)
            
            def on_modified(self, event):
                if not event.is_directory:
                    rel_path = os.path.relpath(event.src_path, self.window.sync_folder)
                    self.window.sync_engine.on_file_event('modified', rel_path)
            
            def on_deleted(self, event):
                rel_path = os.path.relpath(event.src_path, self.window.sync_folder)
                is_dir = event.is_directory
                self.window.sync_engine.on_file_event('deleted', rel_path, is_dir=is_dir)
            
            def on_moved(self, event):
                # 重命名：触发删除旧路径 + 创建新路径
                old_rel_path = os.path.relpath(event.src_path, self.window.sync_folder)
                new_rel_path = os.path.relpath(event.dest_path, self.window.sync_folder)
                self.window.sync_engine.on_file_event('deleted', old_rel_path)
                self.window.sync_engine.on_file_event('created', new_rel_path)
        
        self._file_observer = Observer()
        handler = EventHandler(self)
        self._file_observer.schedule(handler, self.sync_folder, recursive=True)
        self._file_observer.start()
    
    def _stop_file_observer(self):
        """停止文件监控"""
        if self._file_observer:
            self._file_observer.stop()
            self._file_observer.join()
            self._file_observer = None
    
    def _on_operation_ready(self, op):
        """操作准备发送"""
        # 这个信号已经被 _send_operation_to_network 回调处理
        pass
    
    def _send_operation_to_network(self, op):
        """发送操作到网络"""
        if op.op_type in (OpType.CREATE, OpType.MODIFY):
            # 发送文件
            filepath = os.path.join(self.sync_folder, op.path)
            if os.path.exists(filepath):
                if self.mode == 'host':
                    self.server.broadcast_file(filepath, exclude_client=None, hide_from_others=False)
                else:
                    hide = self.hide_checkbox.isChecked() if self.hide_checkbox.isVisible() else False
                    self.client.send_file(filepath, hide_from_others=hide)
                self._add_record(op.path, "本机", I18n.t('common_sync'))
        
        elif op.op_type == OpType.DELETE:
            # 发送删除指令（传递绝对路径）
            filepath = os.path.join(self.sync_folder, op.path)
            if self.mode == 'host':
                self.server.broadcast_delete(filepath)
            else:
                self.client.send_delete(filepath)
            self._add_record(op.path, "本机", I18n.t('common_delete'))
    
    def _on_conflict_detected(self, op):
        """冲突检测"""
        self._add_log(f"冲突检测: {op.path}")
        # 简化处理：接受远程操作（覆盖本地）
        # 实际应用中可以提示用户选择
    
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
        self._stop_file_observer()
        # 不使用弹窗，改为日志记录
        self._add_log(I18n.t('sync_client_disconnected'))
    
    def _on_auth_success(self):
        self.status_label.setText(f"{I18n.t('common_status')}：{I18n.t('common_connected')}")
        self.status_label.setStyleSheet(f"color: {COLORS['success']}; font-size: 11px;")
        
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
    
    def _on_file_received(self, client_id, filename, hide_from_others):
        filepath = os.path.join(self.sync_folder, filename)
        rel_path = filename
        # 取消标记文件正在同步（接收完成）
        if self.sync_engine:
            # 标准化路径：统一使用正斜杠
            normalized_path = filename.replace('\\', '/')
            self.sync_engine.unmark_syncing(normalized_path)
        self._add_record(filename, client_id, I18n.t('common_receive'))
        # 注意：文件转发逻辑已在 server._handle_file_receive 中处理，此处不再重复转发
    
    def _on_delete_received(self, client_id, filename):
        # 标记文件正在同步（删除）
        if self.sync_engine:
            # 标准化路径：统一使用正斜杠
            normalized_path = filename.replace('\\', '/')
            self.sync_engine.mark_syncing(normalized_path)
            # 删除完成后立即取消标记（DELETE 操作不需要等待传输）
            self.sync_engine.unmark_syncing(normalized_path)
        self._add_record(filename, client_id, I18n.t('common_delete'))
    
    def _on_file_received_client(self, filename, size):
        filepath = os.path.join(self.sync_folder, filename)
        rel_path = filename
        # 取消标记文件正在同步（接收完成）
        if self.sync_engine:
            # 标准化路径：统一使用正斜杠
            normalized_path = filename.replace('\\', '/')
            self.sync_engine.unmark_syncing(normalized_path)
        self._add_record(filename, I18n.t('sync_role_host'), I18n.t('common_receive'))
    
    def _on_file_receiving(self, filepath):
        """文件开始接收时标记正在同步"""
        if self.sync_engine:
            rel_path = os.path.relpath(filepath, self.sync_folder)
            # 标准化路径：统一使用正斜杠
            normalized_path = rel_path.replace('\\', '/')
            self.sync_engine.mark_syncing(normalized_path)
    
    def _on_delete_received_client(self, filename):
        # 标记文件正在同步（删除）
        if self.sync_engine:
            # 标准化路径：统一使用正斜杠
            normalized_path = filename.replace('\\', '/')
            self.sync_engine.mark_syncing(normalized_path)
            # 删除完成后立即取消标记（DELETE 操作不需要等待传输）
            self.sync_engine.unmark_syncing(normalized_path)
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
        self.records_table.setItem(row_count, 4, QTableWidgetItem(""))  # 进度列
        
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
        # 设置停止标志，停止所有后台同步线程
        self._stop_sync = True
        
        # 等待后台线程结束（最多等待2秒）
        import time
        for thread in self._sync_threads:
            if thread.is_alive():
                thread.join(timeout=0.5)
        
        self._stop_file_observer()
        
        if self.mode == 'host':
            if self.server:
                self.server.stop()
            room_manager = RoomManager()
            room_manager.close_room(self.room_code)
        else:
            if self.client:
                self.client.disconnect()
    
    def _on_transfer_started(self, filename: str, file_size: int, direction: str):
        """处理传输开始"""
        
        # 显示传输状态标签
        direction_text = "发送中" if direction == 'send' else "接收中"
        self.transfer_status_label.setText(f"{direction_text}: {filename}")
        self.transfer_status_label.show()
        
        # 如果已存在该文件的传输记录，先清理旧的
        if filename in self._transfer_rows:
            old_row = self._transfer_rows[filename]
            # 移除旧的进度条
            self.records_table.setCellWidget(old_row, 4, None)
            # 更新旧行的状态为"重新传输"
            self.records_table.setItem(old_row, 3, QTableWidgetItem("重新传输"))
            self.records_table.item(old_row, 3).setBackground(QColor(COLORS['warning'] if 'warning' in COLORS else COLORS['primary']))
            self.records_table.item(old_row, 3).setForeground(QColor('white'))
            del self._transfer_rows[filename]
        
        # 在表格中添加一行显示进度条
        from PyQt5.QtWidgets import QProgressBar
        from datetime import datetime
        
        row_count = self.records_table.rowCount()
        self.records_table.insertRow(row_count)
        
        # 时间
        time_str = datetime.now().strftime("%H:%M:%S")
        self.records_table.setItem(row_count, 0, QTableWidgetItem(time_str))
        
        # 文件名
        self.records_table.setItem(row_count, 1, QTableWidgetItem(filename))
        
        # 来源
        source = "本机" if direction == 'send' else "远程"
        self.records_table.setItem(row_count, 2, QTableWidgetItem(source))
        
        # 操作
        self.records_table.setItem(row_count, 3, QTableWidgetItem(direction_text))
        self.records_table.item(row_count, 3).setBackground(QColor(COLORS['primary']))
        self.records_table.item(row_count, 3).setForeground(QColor('white'))
        
        # 进度条
        progress_bar = QProgressBar()
        progress_bar.setMinimumHeight(20)
        progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #ced4da;
                border-radius: 4px;
                text-align: center;
                background-color: #e9ecef;
                font-size: 10px;
            }
            QProgressBar::chunk {
                background-color: #51cf66;
                border-radius: 3px;
            }
        """)
        progress_bar.setValue(0)
        if file_size > 0:
            size_mb = file_size / 1024 / 1024
            progress_bar.setFormat(f"0% (0/{size_mb:.1f} MB)")
        else:
            progress_bar.setFormat(f"0% (0/未知大小)")
        self.records_table.setCellWidget(row_count, 4, progress_bar)
        
        # 记录行号
        self._transfer_rows[filename] = row_count
        
        # 隐藏左侧的进度条（现在显示在表格中）
        self.progress_bar.hide()
    
    def _on_transfer_progress(self, filename: str, current: int, total: int):
        """处理传输进度"""
        if filename in self._transfer_rows:
            row = self._transfer_rows[filename]
            progress_bar = self.records_table.cellWidget(row, 4)
            if progress_bar and total > 0:
                progress_percent = int(current / total * 100)
                current_mb = current / 1024 / 1024
                total_mb = total / 1024 / 1024
                progress_bar.setValue(progress_percent)
                progress_bar.setFormat(f"{progress_percent}% ({current_mb:.1f}/{total_mb:.1f} MB)")
    
    def _on_transfer_finished(self, filename: str, direction: str):
        """处理传输结束"""
        # 隐藏传输状态标签
        QTimer.singleShot(2000, self.transfer_status_label.hide)
        
        rel_path = filename
        
        # 解锁路径（发送端）
        if direction == 'send' and self.sync_engine:
            self.sync_engine.unlock_path(rel_path)
        
        # 更新文件哈希值缓存（仅对发送方向，接收方向已通过 file_hash_update 信号更新）
        if direction == 'send':
            filepath = os.path.join(self.sync_folder, filename)
            if self.sync_engine and os.path.exists(filepath):
                hash_value = self.sync_engine._file_hash_cache.calculate_hash_for_path(rel_path)
                size, mtime = self.sync_engine._file_hash_cache.get_file_size_mtime(rel_path)
                self.sync_engine._file_hash_cache.update_file_info(
                    rel_path,
                    hash_value=hash_value,
                    size=size,
                    mtime=mtime,
                    last_sync_time=time.time()
                )
        
        # 更新表格中的操作状态
        if filename in self._transfer_rows:
            row = self._transfer_rows[filename]
            
            # 更新操作列
            operation_text = "已发送" if direction == 'send' else "已接收"
            self.records_table.setItem(row, 3, QTableWidgetItem(operation_text))
            self.records_table.item(row, 3).setBackground(QColor(COLORS['success']))
            self.records_table.item(row, 3).setForeground(QColor('white'))
            
            # 移除进度条（显示完成状态）
            QTimer.singleShot(2000, lambda: self._remove_progress_bar(row))
            
            # 清除跟踪记录
            del self._transfer_rows[filename]
    
    def _remove_progress_bar(self, row: int):
        """移除进度条"""
        if row < self.records_table.rowCount():
            self.records_table.setCellWidget(row, 4, None)
    
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