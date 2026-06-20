"""
同步窗口
"""
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QPushButton, QTextEdit, QFrame, QSplitter, QMessageBox,
    QTableWidget, QTableWidgetItem, QProgressBar, QHeaderView
)
from PySide6.QtCore import Qt, Signal, QMetaObject, Q_ARG
from PySide6.QtGui import QColor
from pathlib import Path

from i18n import I18n
from config import Config
from ui.file_list_widget import FileListWidget
from ui.widgets import AnimatedButton, BUTTON_STYLES
from network.server import SyncServer
from network.client import SyncClient
from network.discovery import RoomResponder


class SyncWindow(QMainWindow):
    """同步窗口"""
    
    # 信号
    closed = Signal()
    
    def __init__(self, is_host: bool, room_code: str, password: str = "", host_address: str = ""):
        super().__init__()
        self.is_host = is_host
        self.room_code = room_code
        self.password = password
        self.host_address = host_address
        
        # 获取房间文件夹
        self.room_folder = Config.get_room_folder(room_code)
        
        # 网络组件
        self.server = None
        self.client = None
        self.responder = None
        
        # 传输进度跟踪
        self._transfer_rows = {}  # 文件名 -> 行号映射
        
        self.init_ui()
        self.init_network()
    
    def init_ui(self):
        """初始化界面"""
        # 窗口设置
        self.setWindowTitle(f"{I18n.tr('app_name')} - {I18n.tr('room_info', code=self.room_code)}")
        self.setMinimumSize(600, 400)
        
        # 创建中心部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 主布局
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # 分隔器（左侧：信息+日志，右侧：文件列表）
        splitter = QSplitter(Qt.Horizontal)
        
        # 左侧面板
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setSpacing(10)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        # 左侧上方：信息显示
        info_frame = QFrame()
        info_frame.setFrameShape(QFrame.StyledPanel)
        info_layout = QVBoxLayout(info_frame)
        info_layout.setContentsMargins(10, 10, 10, 10)
        info_layout.setSpacing(8)
        
        # 模式标签
        mode_text = I18n.tr('host_mode') if self.is_host else I18n.tr('client_mode')
        mode_label = QLabel(f"<b>{mode_text}</b>")
        mode_label.setAlignment(Qt.AlignCenter)
        info_layout.addWidget(mode_label)
        
        # 房间号
        room_label = QLabel(I18n.tr('room_info', code=self.room_code))
        room_label.setAlignment(Qt.AlignCenter)
        info_layout.addWidget(room_label)
        
        # 连接状态（主机端显示连接数）
        if self.is_host:
            self.clients_label = QLabel(f"{I18n.tr('online_count')}: 0")
            self.clients_label.setAlignment(Qt.AlignCenter)
            info_layout.addWidget(self.clients_label)
        
        # 状态标签
        self.status_label = QLabel(I18n.tr('status_synced'))
        self.status_label.setStyleSheet("color: green;")
        self.status_label.setAlignment(Qt.AlignCenter)
        info_layout.addWidget(self.status_label)
        
        # 断开连接按钮
        disconnect_btn = AnimatedButton(I18n.tr('disconnect'))
        disconnect_btn.clicked.connect(self.on_disconnect)
        disconnect_btn.setStyleSheet(BUTTON_STYLES['danger'])
        info_layout.addWidget(disconnect_btn)
        
        left_layout.addWidget(info_frame)
        
        # 左侧下方：同步记录表格
        log_frame = QFrame()
        log_frame.setFrameShape(QFrame.StyledPanel)
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(10, 10, 10, 10)
        log_layout.setSpacing(5)
        
        # 日志标题
        log_title = QLabel(I18n.tr('transfer_log'))
        log_title.setStyleSheet("font-weight: bold;")
        log_layout.addWidget(log_title)
        
        # 同步记录表格
        self.records_table = QTableWidget()
        self.records_table.setColumnCount(2)
        self.records_table.setHorizontalHeaderLabels([
            I18n.tr('log_action'),
            I18n.tr('log_info')
        ])
        
        # 设置表格样式
        header = self.records_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        
        self.records_table.setColumnWidth(0, 60)
        
        self.records_table.setAlternatingRowColors(True)
        self.records_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.records_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.records_table.verticalHeader().setVisible(False)
        self.records_table.setShowGrid(False)
        self.records_table.verticalHeader().setDefaultSectionSize(25)
        self.records_table.setStyleSheet("""
            QTableWidget {
                border: none;
                gridline-color: transparent;
            }
            QTableWidget::item {
                padding: 2px;
                border-bottom: 1px solid #e9ecef;
            }
            QHeaderView::section {
                font-weight: bold;
                padding: 4px;
                border: none;
                border-bottom: 1px solid #dee2e6;
            }
        """)
        
        log_layout.addWidget(self.records_table)
        
        left_layout.addWidget(log_frame)
        
        splitter.addWidget(left_widget)
        
        # 右侧：文件列表
        self.file_list = FileListWidget(self.room_folder)
        # 连接文件操作信号
        self.file_list.file_added.connect(self.on_file_added)
        self.file_list.file_deleted.connect(self.on_file_deleted)
        self.file_list.file_renamed.connect(self.on_file_renamed)
        splitter.addWidget(self.file_list)
        
        # 设置分隔器比例
        splitter.setSizes([280, 720])
        
        main_layout.addWidget(splitter)
        
        # 底部状态栏（行高减小）
        bottom_frame = QFrame()
        bottom_frame.setFixedHeight(28)
        bottom_layout = QHBoxLayout(bottom_frame)
        bottom_layout.setContentsMargins(10, 2, 10, 2)
        
        # 同步文件夹路径
        folder_label = QLabel(I18n.tr('sync_folder_path', path=str(self.room_folder)))
        bottom_layout.addWidget(folder_label)
        
        bottom_layout.addStretch()
        
        main_layout.addWidget(bottom_frame)
    
    def init_network(self):
        """初始化网络"""
        if self.is_host:
            # 主机端：启动服务器和响应服务
            self.server = SyncServer(self.room_code, self.password)
            self.server.client_connected.connect(self.on_client_connected)
            self.server.client_disconnected.connect(self.on_client_disconnected)
            self.server.error_occurred.connect(self.on_network_error)
            self.server.file_receive_start.connect(self.on_file_receive_start)
            self.server.file_receive_progress.connect(self.on_file_receive_progress)
            self.server.file_received.connect(self.on_remote_file_received)
            self.server.file_deleted.connect(self.on_remote_file_deleted)
            self.server.log_message.connect(self.add_log_from_network)
            
            if self.server.start():
                self._add_record(f"端口: {Config.DEFAULT_PORT}", "启动", "")
                
                # 启动房间响应服务
                self.responder = RoomResponder(self)
                if self.responder.start(self.room_code):
                    pass
            else:
                self._add_record("启动失败", "错误", "")
        else:
            # 客户端：连接到服务器
            self.client = SyncClient(self.room_code, self.password)
            self.client.connected.connect(self.on_connected)
            self.client.disconnected.connect(self.on_disconnected)
            self.client.error_occurred.connect(self.on_network_error)
            self.client.file_receive_start.connect(self.on_file_receive_start)
            self.client.file_receive_progress.connect(self.on_file_receive_progress)
            self.client.file_received.connect(self.on_remote_file_received)
            self.client.file_deleted.connect(self.on_remote_file_deleted)
            self.client.log_message.connect(self.add_log_from_network)
            
            # 连接到服务器
            host = self.host_address or "127.0.0.1"
            if self.client.connect_to_server(host):
                self._add_record(f"{host}:{Config.DEFAULT_PORT}", "连接", "")
            else:
                self._add_record("连接失败", "错误", "")
    
    def add_log_from_network(self, message: str):
        """从网络层添加日志（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "add_log", Qt.QueuedConnection,
                                 Q_ARG(str, "网络"), Q_ARG(str, message))
    
    def on_client_connected(self, client_id: str):
        """客户端连接"""
        self._add_record(client_id, "连接", "")
        self._update_clients_count()
    
    def on_client_disconnected(self, client_id: str):
        """客户端断开"""
        self._add_record(client_id, "断开", "")
        self._update_clients_count()
    
    def _update_clients_count(self):
        """更新连接数"""
        if self.is_host and self.server:
            with self.server._lock:
                count = len([c for c in self.server.clients.values() if c['authenticated']])
            self.clients_label.setText(f"{I18n.tr('online_count')}: {count}")
    
    def on_connected(self):
        """连接成功"""
        self.status_label.setText(I18n.tr('status_synced'))
        self.status_label.setStyleSheet("color: green;")
    
    def on_disconnected(self):
        """断开连接"""
        self._add_record("", "断开", "")
        self.status_label.setText(I18n.tr('status_disconnected'))
        self.status_label.setStyleSheet("color: red;")
    
    def on_network_error(self, error: str):
        """网络错误"""
        self.add_log("错误", error)
    
    def on_file_receive_start(self, filename: str):
        """开始接收远程文件（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_file_receive_start", Qt.QueuedConnection,
                                 Q_ARG(str, filename))
    
    def _do_file_receive_start(self, filename: str):
        """实际执行：开始接收远程文件"""
        # 标记文件正在同步，避免循环同步
        file_path = str(self.room_folder / filename)
        self.file_list.mark_syncing(file_path)
        
        # 在表格中添加进度条
        self._add_transfer_progress(filename, 0, 0)
    
    def on_file_receive_progress(self, filename: str, current: int, total: int):
        """文件接收进度（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_file_receive_progress", Qt.QueuedConnection,
                                 Q_ARG(str, filename), Q_ARG(int, current), Q_ARG(int, total))
    
    def _do_file_receive_progress(self, filename: str, current: int, total: int):
        """实际执行：更新文件接收进度"""
        self._update_transfer_progress(filename, current, total)
    
    def on_remote_file_received(self, filename: str):
        """收到远程文件（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_remote_file_received", Qt.QueuedConnection,
                                 Q_ARG(str, filename))
    
    def _do_remote_file_received(self, filename: str):
        """实际执行：收到远程文件"""
        # 取消同步标记
        file_path = str(self.room_folder / filename)
        self.file_list.unmark_syncing(file_path)
        
        # 更新进度为完成
        self._finish_transfer_progress(filename)
        
        # 刷新文件列表（不会触发同步信号）
        self.file_list.refresh()
        
        self.status_label.setText(I18n.tr('status_synced'))
        self.status_label.setStyleSheet("color: green;")
    
    def on_remote_file_deleted(self, filename: str):
        """远程文件已删除（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_remote_file_deleted", Qt.QueuedConnection,
                                 Q_ARG(str, filename))
    
    def _do_remote_file_deleted(self, filename: str):
        """实际执行：远程文件已删除"""
        # 刷新文件列表
        self.file_list.refresh()
        
        from pathlib import Path
        self._add_record(Path(filename).name, "删除", "")
        self.status_label.setText(I18n.tr('status_synced'))
        self.status_label.setStyleSheet("color: green;")
    
    def _add_record(self, content: str, action: str, status: str = ""):
        """添加同步记录
        
        Args:
            content: 内容（文件名/IP等）
            action: 操作（连接、添加、删除等）
            status: 状态/进度（可选）
        """
        from pathlib import Path
        
        # 组合内容和状态
        display_text = content
        if status:
            display_text = f"{content} - {status}"
        
        # 截断显示文本
        if len(display_text) > 40:
            display_text = display_text[:37] + "..."
        
        # 添加新行
        row_count = self.records_table.rowCount()
        self.records_table.insertRow(row_count)
        
        # 操作
        action_item = QTableWidgetItem(action)
        action_item.setTextAlignment(Qt.AlignCenter)
        self.records_table.setItem(row_count, 0, action_item)
        
        # 内容
        content_item = QTableWidgetItem(display_text)
        content_item.setToolTip(content)
        self.records_table.setItem(row_count, 1, content_item)
        
        # 滚动到底部
        self.records_table.scrollToBottom()
        
        # 限制记录数量
        while self.records_table.rowCount() > 100:
            self.records_table.removeRow(0)
    
    def _add_transfer_progress(self, filename: str, current: int, total: int):
        """添加传输进度条"""
        from pathlib import Path
        
        # 截断文件名
        display_name = Path(filename).name
        if len(display_name) > 25:
            display_name = display_name[:22] + "..."
        
        # 添加新行
        row_count = self.records_table.rowCount()
        self.records_table.insertRow(row_count)
        
        # 操作
        action_item = QTableWidgetItem("接收")
        action_item.setTextAlignment(Qt.AlignCenter)
        self.records_table.setItem(row_count, 0, action_item)
        
        # 进度条（放在第二列）
        progress_bar = QProgressBar()
        progress_bar.setMinimum(0)
        progress_bar.setMaximum(100)
        progress_bar.setValue(0)
        progress_bar.setTextVisible(True)
        progress_bar.setFormat(f"{display_name} - 0%")
        progress_bar.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                text-align: left;
                background-color: transparent;
            }
            QProgressBar::chunk {
                background-color: #51cf66;
            }
        """)
        self.records_table.setCellWidget(row_count, 1, progress_bar)
        
        # 记录行号
        self._transfer_rows[filename] = row_count
        
        # 滚动到底部
        self.records_table.scrollToBottom()
    
    def _update_transfer_progress(self, filename: str, current: int, total: int):
        """更新传输进度"""
        if filename in self._transfer_rows:
            row = self._transfer_rows[filename]
            progress_bar = self.records_table.cellWidget(row, 1)
            if progress_bar and total > 0:
                progress_percent = int(current / total * 100)
                progress_bar.setValue(progress_percent)
                
                # 显示文件名和进度
                from pathlib import Path
                display_name = Path(filename).name
                if len(display_name) > 25:
                    display_name = display_name[:22] + "..."
                
                # 显示大小（简化格式）
                current_mb = current / 1024 / 1024
                total_mb = total / 1024 / 1024
                if total_mb >= 1:
                    progress_bar.setFormat(f"{display_name} - {progress_percent}% ({current_mb:.1f}/{total_mb:.1f}M)")
                else:
                    current_kb = current / 1024
                    total_kb = total / 1024
                    progress_bar.setFormat(f"{display_name} - {progress_percent}% ({current_kb:.0f}/{total_kb:.0f}K)")
    
    def _finish_transfer_progress(self, filename: str):
        """完成传输进度"""
        if filename in self._transfer_rows:
            row = self._transfer_rows[filename]
            
            # 移除进度条，显示完成状态
            from pathlib import Path
            display_name = Path(filename).name
            if len(display_name) > 25:
                display_name = display_name[:22] + "..."
            
            self.records_table.setCellWidget(row, 1, None)
            status_item = QTableWidgetItem(f"{display_name} - 完成")
            status_item.setForeground(QColor("#51cf66"))
            self.records_table.setItem(row, 1, status_item)
            
            # 清除记录
            del self._transfer_rows[filename]
    
    def add_log(self, log_type: str, message: str):
        """添加日志（兼容旧代码）"""
        self._add_record(message, log_type, "")
    
    def on_file_added(self, file_path: str):
        """文件添加事件（本地操作）"""
        from pathlib import Path
        file_name = Path(file_path).name
        self._add_record(file_name, "添加", "")
        
        # 根据设计文档的同步逻辑：
        # 主机端：直接广播给所有连接端
        # 连接端：发送给主机端，主机端转发给其他连接端
        if self.is_host and self.server:
            self.server.broadcast_file(file_path)
        elif self.client:
            self.client.send_file(file_path)
        
        self.status_label.setText(I18n.tr('status_syncing'))
        self.status_label.setStyleSheet("color: #339af0;")
        
        # 模拟同步完成
        from PySide6.QtCore import QTimer
        QTimer.singleShot(500, lambda: (
            self.status_label.setText(I18n.tr('status_synced')),
            self.status_label.setStyleSheet("color: green;")
        ))
    
    def on_file_deleted(self, file_path: str):
        """文件删除事件（本地操作）"""
        from pathlib import Path
        file_name = Path(file_path).name
        self._add_record(file_name, "删除", "")
        
        # 同步逻辑
        if self.is_host and self.server:
            self.server.broadcast_delete(file_path)
        elif self.client:
            self.client.send_delete(file_path)
        
        self.status_label.setText(I18n.tr('status_syncing'))
        self.status_label.setStyleSheet("color: #339af0;")
        
        from PySide6.QtCore import QTimer
        QTimer.singleShot(500, lambda: (
            self.status_label.setText(I18n.tr('status_synced')),
            self.status_label.setStyleSheet("color: green;")
        ))
    
    def on_file_renamed(self, old_path: str, new_path: str):
        """文件重命名事件（本地操作）"""
        from pathlib import Path
        old_name = Path(old_path).name
        new_name = Path(new_path).name
        self.add_log("重命名", f"{old_name} -> {new_name}")
        
        # 同步逻辑
        if self.is_host and self.server:
            self.server.broadcast_rename(old_path, new_path)
        elif self.client:
            self.client.send_rename(old_path, new_path)
        
        self.status_label.setText(I18n.tr('status_syncing'))
        self.status_label.setStyleSheet("color: #339af0;")
        
        from PySide6.QtCore import QTimer
        QTimer.singleShot(500, lambda: (
            self.status_label.setText(I18n.tr('status_synced')),
            self.status_label.setStyleSheet("color: green;")
        ))
    
    def on_disconnect(self):
        """断开连接"""
        reply = QMessageBox.question(
            self,
            I18n.tr('confirm_leave'),
            I18n.tr('confirm_leave_msg'),
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.close()
    
    def closeEvent(self, event):
        """窗口关闭事件"""
        # 停止网络服务
        if self.server:
            self.server.stop()
        if self.client:
            self.client.disconnect()
        if self.responder:
            self.responder.stop()
        
        self.closed.emit()
        event.accept()
